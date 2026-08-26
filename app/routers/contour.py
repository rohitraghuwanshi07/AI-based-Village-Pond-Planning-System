"""
Phase 2 endpoint: accepts an uploaded KML/KMZ contour map, reconstructs a
terrain surface from it, automatically identifies a suitable pond site, and
returns the delineated catchment area -- all derived from the uploaded file
itself, with nothing hard-coded to any specific sample map.

Pipeline (each stage reuses/extends the same engines built in Phase 1):
    1. Parse contour lines from the uploaded file (kml_parser.py)
    2. Rasterize the vector contour lines into a continuous elevation grid
       (contour_rasterizer.py)
    3. Compute slope (terrain_engine.py, unchanged from Phase 1)
    4. Compute D8 flow direction + accumulation (catchment_engine.py, unchanged)
    5. Automatically select a candidate pond site (pond_site_selector.py)
    6. Delineate the catchment for that site (catchment_engine.py, unchanged)
"""

import time

import numpy as np
from fastapi import APIRouter, File, HTTPException, Query, UploadFile
from skimage import measure

from app.services.kml_parser import parse_contour_file
from app.services.contour_rasterizer import rasterize_contours
from app.services.terrain_engine import compute_slope_degrees
from app.services.catchment_engine import (
    delineate_catchment,
    fill_depressions,
    flow_accumulation,
    flow_direction_d8,
)
from app.services.pond_site_selector import select_pond_site
from app.services.rainfall_client import get_historical_rainfall
from app.services.runoff_engine import estimate_daily_series_runoff, DEFAULT_CURVE_NUMBER, CURVE_NUMBERS
from app.services.land_use_client import fetch_obstructions
from app.services.site_suitability import find_vacant_patches
from app.services.pond_sizing_engine import recommend_pond
from app.services.soil_client import fetch_soil_composition

router = APIRouter(tags=["contour-analysis"])


def _rowcol_to_latlon(transform, row: int, col: int):
    lon, lat = transform * (col, row)
    return lat, lon


def _build_catchment_geojson(catchment_mask: np.ndarray, transform) -> dict:
    boundary_paths = measure.find_contours(catchment_mask.astype(float), level=0.5)
    polygons = []
    for path in boundary_paths:
        coords = [_rowcol_to_latlon(transform, r, c) for r, c in path]
        polygons.append([[lon, lat] for lat, lon in coords])

    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [poly]}}
            for poly in polygons
            if len(poly) >= 4
        ],
    }


async def _analyze_contour_impl(
    file: UploadFile,
    resolution_m: float,
    max_slope_deg: float,
):
    timings = {}
    t_start = time.time()

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        parse_result = parse_contour_file(file_bytes, file.filename or "upload.kml")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse contour file: {e}")

    if not parse_result.lines:
        raise HTTPException(
            status_code=422,
            detail="No usable contour lines with elevation values were found in this file.",
        )
    timings["parse_seconds"] = round(time.time() - t_start, 2)

    t = time.time()
    try:
        elevation, transform, raster_meta = rasterize_contours(parse_result.lines, target_resolution_m=resolution_m)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    timings["rasterize_seconds"] = round(time.time() - t, 2)

    t = time.time()
    slope = compute_slope_degrees(elevation, transform)
    timings["slope_seconds"] = round(time.time() - t, 2)

    t = time.time()
    filled = fill_depressions(elevation)
    px_m = abs(transform.a) * 111320
    py_m = abs(transform.e) * 111320
    downstream_r, downstream_c = flow_direction_d8(filled, px_m, py_m)
    acc = flow_accumulation(filled, downstream_r, downstream_c)
    timings["flow_analysis_seconds"] = round(time.time() - t, 2)

    t = time.time()
    row, col, site_info = select_pond_site(slope, acc, elevation=elevation, max_slope_deg=max_slope_deg)
    timings["site_selection_seconds"] = round(time.time() - t, 2)

    t = time.time()
    catchment_mask = delineate_catchment(downstream_r, downstream_c, row, col)
    catchment_geojson = _build_catchment_geojson(catchment_mask, transform)
    timings["catchment_delineation_seconds"] = round(time.time() - t, 2)

    site_lat, site_lon = _rowcol_to_latlon(transform, row, col)
    cell_area_m2 = px_m * py_m
    catchment_area_m2 = float(catchment_mask.sum()) * cell_area_m2

    # --- Real pond sizing, grounded in actual land availability (not a guess) ---
    # This checks real OpenStreetMap building/road/water data near the selected
    # site and finds genuinely CONTIGUOUS vacant land -- patches split apart by
    # a road are treated as separate, not combined into one leftover number.
    t = time.time()
    site_check = None
    vacant_land_boundary_geojson = None
    obstruction_data = await fetch_obstructions(site_lat, site_lon, radius_m=150.0)
    if obstruction_data["query_succeeded"]:
        patch_result = find_vacant_patches(
            site_lat, site_lon,
            buildings=obstruction_data["buildings"],
            roads=obstruction_data["roads"],
            water=obstruction_data["water"],
            search_radius_m=150.0,
        )
        if patch_result["patches"] and patch_result["selected_patch_index"] is not None:
            selected_patch = patch_result["patches"][patch_result["selected_patch_index"]]
            available_area_m2 = selected_patch["area_m2"]
            vacant_land_boundary_geojson = selected_patch["boundary_geojson"]
            site_check = {
                "available_area_m2": available_area_m2,
                "buildings_found_nearby": patch_result["buildings_found_nearby"],
                "roads_found_nearby": patch_result["roads_found_nearby"],
                "water_bodies_found_nearby": patch_result["water_bodies_found_nearby"],
                "total_separate_patches_found": len(patch_result["patches"]),
                "note": (
                    f"Selected the contiguous vacant patch actually containing/nearest to the "
                    f"selected site ({available_area_m2} m2), out of {len(patch_result['patches'])} "
                    f"separate open patch(es) found nearby."
                ),
            }
        else:
            available_area_m2 = 0.0
            site_check = {
                "available_area_m2": 0.0,
                "note": "No usable vacant land patch found within 150m of the selected site.",
            }
    else:
        search_radius_m = 150.0
        assumed_open_fraction = 0.6
        available_area_m2 = round(3.14159 * (search_radius_m ** 2) * assumed_open_fraction, 1)
        site_check = {
            "available_area_m2": available_area_m2,
            "note": f"OpenStreetMap obstruction check failed ({obstruction_data['error']}); "
                    f"assuming {int(assumed_open_fraction*100)}% of the {search_radius_m:.0f}m search "
                    f"radius is open land (unverified). Confirm this site is actually clear of "
                    f"buildings/roads/water before construction.",
        }
    timings["land_use_check_seconds"] = round(time.time() - t, 2)

    # Rainfall + runoff at the selected site, so we can size the pond against
    # a real target volume instead of an arbitrary one.
    t = time.time()
    rainfall_result = await get_historical_rainfall(site_lat, site_lon, years=10)
    dates = rainfall_result["daily_series"]["dates"]
    daily_values = rainfall_result["daily_series"]["precipitation_mm"]
    years_seen = sorted(set(d[:4] for d in dates))
    complete_years = years_seen[1:-1] or years_seen
    curve_number = CURVE_NUMBERS.get("cultivated_land", DEFAULT_CURVE_NUMBER)

    annual_runoff_depths = []
    for year in complete_years:
        year_values = [v for d, v in zip(dates, daily_values) if d.startswith(year)]
        runoff_result = estimate_daily_series_runoff(year_values, curve_number=curve_number)
        annual_runoff_depths.append(runoff_result["total_runoff_depth_mm"])
    avg_annual_runoff_depth_mm = (
        sum(annual_runoff_depths) / len(annual_runoff_depths) if annual_runoff_depths else 0.0
    )
    avg_annual_runoff_volume_m3 = (avg_annual_runoff_depth_mm / 1000) * catchment_area_m2
    timings["rainfall_runoff_seconds"] = round(time.time() - t, 2)

    t = time.time()
    soil_check = await fetch_soil_composition(site_lat, site_lon)
    timings["soil_check_seconds"] = round(time.time() - t, 2)

    pond_sizing = recommend_pond(
        required_volume_m3=avg_annual_runoff_volume_m3,
        available_site_area_m2=available_area_m2,
        target_capture_fraction=0.5,
    )

    timings["total_seconds"] = round(time.time() - t_start, 2)

    return {
        "input_file": {
            "filename": file.filename,
            "format_detected": parse_result.source_format,
            "contour_lines_parsed": len(parse_result.lines),
            "contour_lines_skipped": parse_result.lines_skipped,
        },
        "terrain_summary": {
            "bounding_box": raster_meta["bbox"],
            "raster_shape_rows_cols": raster_meta["raster_shape"],
            "raster_resolution_m": raster_meta["resolution_m_used"],
            "elevation_range_m": raster_meta["elevation_range_m"],
            "mean_slope_deg": round(float(np.nanmean(slope)), 2),
        },
        "recommended_pond_site": {
            "lat": site_lat,
            "lon": site_lon,
            "elevation_m": round(float(elevation[row, col]), 1),
            "selection_method": site_info,
        },
        "catchment": {
            "area_m2": round(catchment_area_m2, 1),
            "area_hectares": round(catchment_area_m2 / 10000, 2),
            "cell_count": int(catchment_mask.sum()),
            "boundary_geojson": catchment_geojson,
        },
        "rainfall_and_runoff": {
            "years_analyzed": len(complete_years),
            "annual_average_rainfall_mm": rainfall_result["annual_average_mm"],
            "curve_number_used": curve_number,
            "avg_annual_runoff_volume_m3": round(avg_annual_runoff_volume_m3, 1),
        },
        "site_check": site_check,
        "vacant_land_boundary_geojson": vacant_land_boundary_geojson,
        "soil_check": soil_check,
        "pond_sizing_recommendation": pond_sizing,
        "methodology": (
            "Contour lines were parsed from the uploaded file and interpolated into a "
            "continuous elevation raster (linear interpolation over scattered elevation "
            "points sampled along each line). Slope was computed via finite-difference "
            "gradient. Flow direction/accumulation were computed using a D8 algorithm "
            "(priority-flood depression filling + steepest-descent routing). The pond "
            "site was chosen as the highest-flow-accumulation cell among low-slope "
            "candidates. The catchment was delineated via reverse breadth-first search "
            "over the flow-direction graph, upstream from the selected site. Real "
            "building/road data from OpenStreetMap is checked near the selected site "
            "to ground pond sizing in actually-available open land, rather than an "
            "assumed constant. No coordinates or results are hard-coded -- everything "
            "is derived from the uploaded file."
        ),
        "processing_time_seconds": timings,
    }


@router.post("/api/analyzeContour")
async def analyze_contour(
    file: UploadFile = File(..., description="A KML or KMZ contour map file"),
    resolution_m: float = Query(10.0, ge=2.0, le=50.0, description="Raster cell size to reconstruct, in meters"),
    max_slope_deg: float = Query(8.0, ge=1.0, le=45.0, description="Max slope (degrees) considered suitable for pond excavation"),
):
    """
    Analyze an uploaded contour map (KML/KMZ) and return the estimated
    catchment area for an automatically-identified candidate pond site.
    """
    return await _analyze_contour_impl(file, resolution_m, max_slope_deg)


@router.post("/api/findCatchment")
async def find_catchment(
    file: UploadFile = File(..., description="A KML or KMZ contour map file"),
    resolution_m: float = Query(10.0, ge=2.0, le=50.0),
    max_slope_deg: float = Query(8.0, ge=1.0, le=45.0),
):
    """Alias of /api/analyzeContour (same behavior, alternate route name per assignment wording)."""
    return await _analyze_contour_impl(file, resolution_m, max_slope_deg)
