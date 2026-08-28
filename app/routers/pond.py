"""
Pond recommendation endpoint: the final piece that chains catchment area,
rainfall, runoff estimation, and pond sizing into one complete recommendation.

Three entry points:
    GET  /api/pond/recommend      -- caller specifies the exact pour point
                                      (e.g. a manual map click)
    GET  /api/pond/suggest-site   -- caller only gives an area (e.g. a searched
                                      village's bounding box); the system
                                      automatically finds the lowest-elevation,
                                      best-draining point in that area and
                                      recommends a pond there, with no manual
                                      click required.
    POST /api/pond/suggest-from-landrecord -- caller uploads an actual land
                                      record document (e.g. a Bhu-Naksha-style
                                      GeoJSON/KML export) with per-parcel
                                      ownership classification; the system uses
                                      THAT real data instead of the OSM-tag
                                      ownership heuristic, which is a stronger
                                      basis for eligibility when available.
All three share the same underlying pipeline (catchment -> rainfall -> runoff
-> land-use check -> pond sizing -> soil check).
"""

from fastapi import APIRouter, File, HTTPException, Query, UploadFile
import numpy as np
from rasterio.features import rasterize
from shapely.geometry import shape

from app.services.rainfall_client import get_historical_rainfall
from app.services.runoff_engine import estimate_daily_series_runoff, DEFAULT_CURVE_NUMBER, CURVE_NUMBERS
from app.services.pond_sizing_engine import recommend_pond
from app.services.land_use_client import fetch_obstructions
from app.services.ownership_client import fetch_ownership_zones
from app.services.site_suitability import find_eligible_patches, find_eligible_patches_from_parcels
from app.services.soil_client import fetch_soil_composition
from app.services.elevation_client import fetch_dem
from app.services.terrain_engine import load_dem, compute_slope_degrees
from app.services.catchment_engine import fill_depressions, flow_direction_d8, flow_accumulation
from app.services.pond_site_selector import select_pond_site
from app.services.land_record_parser import parse_land_record_file
from app.routers.catchment import delineate as delineate_catchment_endpoint

router = APIRouter(prefix="/api/pond", tags=["pond"])


async def _full_recommendation(
    south: float, north: float, west: float, east: float,
    pour_lat: float, pour_lon: float,
    land_cover: str,
    available_site_area_m2: float | None,
    target_capture_fraction: float,
    rainfall_years: int,
    auto_selected_info: dict | None = None,
):
    """Shared pipeline used by both /recommend (manual point) and
    /suggest-site (auto-detected point)."""
    curve_number = CURVE_NUMBERS.get(land_cover, DEFAULT_CURVE_NUMBER)

    # 1. Catchment delineation
    catchment_result = await delineate_catchment_endpoint(
        south=south, north=north, west=west, east=east,
        pour_lat=pour_lat, pour_lon=pour_lon,
    )
    catchment_area_m2 = catchment_result["catchment_area_m2"]

    # 2. Historical rainfall
    rainfall_result = await get_historical_rainfall(pour_lat, pour_lon, years=rainfall_years)
    daily_values = rainfall_result["daily_series"]["precipitation_mm"]
    dates = rainfall_result["daily_series"]["dates"]
    years_seen = sorted(set(d[:4] for d in dates))
    complete_years = years_seen[1:-1] or years_seen

    annual_runoff_depths = []
    for year in complete_years:
        year_values = [v for d, v in zip(dates, daily_values) if d.startswith(year)]
        result = estimate_daily_series_runoff(year_values, curve_number=curve_number)
        annual_runoff_depths.append(result["total_runoff_depth_mm"])

    avg_annual_runoff_depth_mm = (
        sum(annual_runoff_depths) / len(annual_runoff_depths) if annual_runoff_depths else 0.0
    )
    avg_annual_runoff_volume_m3 = (avg_annual_runoff_depth_mm / 1000) * catchment_area_m2

    # 3. Real vacant-land patch analysis from OpenStreetMap, unless overridden.
    # This finds actual CONTIGUOUS, GOVERNMENT-OWNED, vacant land near the site.
    # IMPORTANT: there is no free authoritative land-ownership API. Government
    # ownership is inferred from OpenStreetMap tags as a heuristic signal ONLY
    # (see ownership_client.py). Land that isn't EXPLICITLY tagged government
    # is treated as ownership-unverified and EXCLUDED, per a strict policy --
    # this commonly means the eligible area will be small or zero, which is
    # honest, correct behavior, not a bug. Buildings, roads, and water bodies
    # are also excluded, and patches split apart by a road are NOT combined.
    site_check = None
    selected_patch = None
    ownership_layers = None
    if available_site_area_m2 is None:
        obstruction_data = await fetch_obstructions(pour_lat, pour_lon, radius_m=150.0)
        ownership_data = await fetch_ownership_zones(pour_lat, pour_lon, radius_m=150.0)

        if obstruction_data["query_succeeded"] and ownership_data["query_succeeded"]:
            patch_result = find_eligible_patches(
                pour_lat, pour_lon,
                buildings=obstruction_data["buildings"],
                roads=obstruction_data["roads"],
                water=obstruction_data["water"],
                government_zones=ownership_data["government_zones"],
                private_zones=ownership_data["private_zones"],
                search_radius_m=150.0,
            )
            ownership_layers = patch_result["layer_boundaries"]
            if patch_result["patches"] and patch_result["selected_patch_index"] is not None:
                selected_patch = patch_result["patches"][patch_result["selected_patch_index"]]
                available_site_area_m2 = selected_patch["area_m2"]
            else:
                available_site_area_m2 = 0.0
            site_check = {
                "available_area_m2": available_site_area_m2,
                "area_breakdown": patch_result["area_breakdown"],
                "total_separate_eligible_patches_found": len(patch_result["patches"]),
                "government_zones_found_nearby": patch_result["government_zones_found_nearby"],
                "private_zones_found_nearby": patch_result["private_zones_found_nearby"],
                "buildings_found_nearby": patch_result["buildings_found_nearby"],
                "roads_found_nearby": patch_result["roads_found_nearby"],
                "water_bodies_found_nearby": patch_result["water_bodies_found_nearby"],
                "limitation": patch_result["ownership_data_limitation"],
            }
        else:
            # Ownership/obstruction data unavailable -- per the strict policy,
            # we do NOT guess a fallback area. State the limitation plainly.
            failed_reasons = []
            if not obstruction_data["query_succeeded"]:
                failed_reasons.append(f"obstruction check failed ({obstruction_data['error']})")
            if not ownership_data["query_succeeded"]:
                failed_reasons.append(f"ownership check failed ({ownership_data['error']})")
            available_site_area_m2 = 0.0
            site_check = {
                "available_area_m2": 0.0,
                "note": (
                    f"Could not verify land ownership/availability ({'; '.join(failed_reasons)}). "
                    f"Per a strict policy, unverifiable land is not assumed available -- "
                    f"0 m2 eligible area is reported rather than a guess. Retry, or verify "
                    f"this site manually through official government land records."
                ),
            }

    # 4. Soil composition (seepage risk) at the site
    soil_check = await fetch_soil_composition(pour_lat, pour_lon)

    # 5. Pond sizing recommendation
    pond_result = recommend_pond(
        required_volume_m3=avg_annual_runoff_volume_m3,
        available_site_area_m2=available_site_area_m2,
        target_capture_fraction=target_capture_fraction,
    )

    return {
        "location": {"lat": pour_lat, "lon": pour_lon},
        "vacant_land_boundary_geojson": selected_patch["boundary_geojson"] if selected_patch else None,
        "ownership_layers": ownership_layers,
        "auto_selected": auto_selected_info,
        "catchment": {
            "area_m2": catchment_area_m2,
            "area_hectares": catchment_result["catchment_area_hectares"],
            "boundary_geojson": catchment_result["catchment_boundary_geojson"],
            "snapped_pour_point": catchment_result["pour_point_snapped"],
        },
        "rainfall": {
            "years_analyzed": len(complete_years),
            "annual_average_mm": rainfall_result["annual_average_mm"],
            "monsoon_average_mm": rainfall_result["monsoon_average_mm"],
        },
        "runoff": {
            "land_cover_assumed": land_cover,
            "curve_number_used": curve_number,
            "avg_annual_runoff_depth_mm": round(avg_annual_runoff_depth_mm, 1),
            "avg_annual_runoff_volume_m3": round(avg_annual_runoff_volume_m3, 1),
        },
        "site_check": site_check,
        "soil_check": soil_check,
        "pond_recommendation": pond_result,
    }


@router.get("/recommend")
async def recommend(
    south: float = Query(..., description="Southern latitude bound of the analysis area"),
    north: float = Query(..., description="Northern latitude bound"),
    west: float = Query(..., description="Western longitude bound"),
    east: float = Query(..., description="Eastern longitude bound"),
    pour_lat: float = Query(..., description="Latitude of the clicked pond site"),
    pour_lon: float = Query(..., description="Longitude of the clicked pond site"),
    land_cover: str = Query("cultivated_land", description=f"One of: {list(CURVE_NUMBERS.keys())}"),
    available_site_area_m2: float | None = Query(
        None,
        description="Override: force a specific site area in m2. If omitted, the real "
                    "available area is auto-detected from OpenStreetMap buildings/roads "
                    "near the clicked point.",
    ),
    target_capture_fraction: float = Query(0.5, ge=0.05, le=1.0, description="Fraction of annual runoff to target capturing"),
    rainfall_years: int = Query(10, ge=1, le=30),
):
    """
    Full pipeline for a MANUALLY specified pour point: catchment delineation
    -> historical rainfall -> SCS-CN runoff estimate -> land-use check ->
    soil check -> pond depth/area/storage capacity recommendation.

    Example:
    GET /api/pond/recommend?south=21.10&north=21.19&west=79.04&east=79.13
        &pour_lat=21.145&pour_lon=79.09&land_cover=cultivated_land
    """
    return await _full_recommendation(
        south, north, west, east, pour_lat, pour_lon,
        land_cover, available_site_area_m2, target_capture_fraction, rainfall_years,
        auto_selected_info=None,
    )


@router.get("/suggest-site")
async def suggest_site(
    south: float = Query(..., description="Southern latitude bound of the search area (e.g. a searched village's bbox)"),
    north: float = Query(..., description="Northern latitude bound"),
    west: float = Query(..., description="Western longitude bound"),
    east: float = Query(..., description="Eastern longitude bound"),
    land_cover: str = Query("cultivated_land", description=f"One of: {list(CURVE_NUMBERS.keys())}"),
    available_site_area_m2: float | None = Query(None, description="Override for available site area in m2"),
    target_capture_fraction: float = Query(0.5, ge=0.05, le=1.0),
    rainfall_years: int = Query(10, ge=1, le=30),
    max_slope_deg: float = Query(8.0, ge=1.0, le=45.0, description="Max slope considered suitable for excavation"),
):
    """
    Automatically finds the best pond site within the given area -- no manual
    click required. Fetches real elevation data for the area, then picks the
    lowest-elevation, best-draining, low-slope point (water physically
    collects at the lowest point of a basin -- this is weighted as the
    primary factor, not just an incidental correlation with flow accumulation).
    Then runs the full recommendation pipeline on that auto-selected point.

    Example:
    GET /api/pond/suggest-site?south=21.10&north=21.19&west=79.04&east=79.13
    """
    try:
        dem_path = await fetch_dem(south, north, west, east)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    elevation, transform, crs = load_dem(dem_path)
    slope = compute_slope_degrees(elevation, transform)

    filled = fill_depressions(np.nan_to_num(elevation, nan=99999.0))
    px_m = abs(transform.a) * 111320
    py_m = abs(transform.e) * 111320
    downstream_r, downstream_c = flow_direction_d8(filled, px_m, py_m)
    acc = flow_accumulation(filled, downstream_r, downstream_c)

    row, col, site_info = select_pond_site(slope, acc, elevation=elevation, max_slope_deg=max_slope_deg)
    lon, lat = transform * (col, row)

    result = await _full_recommendation(
        south, north, west, east, lat, lon,
        land_cover, available_site_area_m2, target_capture_fraction, rainfall_years,
        auto_selected_info=site_info,
    )
    return result


@router.post("/suggest-from-landrecord")
async def suggest_from_landrecord(
    file: UploadFile = File(..., description="A land record document (GeoJSON or KML) with per-parcel ownership classification"),
    land_cover: str = Query("cultivated_land", description=f"One of: {list(CURVE_NUMBERS.keys())}"),
    target_capture_fraction: float = Query(0.5, ge=0.05, le=1.0),
    rainfall_years: int = Query(10, ge=1, le=30),
    max_slope_deg: float = Query(8.0, ge=1.0, le=45.0),
    cell_size_m: float = Query(10.0, ge=2.0, le=30.0, description="Grid resolution for rasterizing the land record"),
):
    """
    Uses an ACTUAL uploaded land record (e.g. a Bhu-Naksha-style parcel map,
    exported/prepared as GeoJSON or KML with a `land_type` classification per
    parcel) to find eligible government-owned vacant land, instead of relying
    on the OpenStreetMap-tag ownership heuristic used by /recommend and
    /suggest-site. See land_record_parser.py for the expected file format and
    the Indian revenue-record classification keywords used (sarkari, nazul,
    krishi, aabadi, sarak, talab, etc).

    This does NOT depend on live OSM/Overpass queries for ownership at all --
    only for the elevation/rainfall data, which come from OpenTopography and
    Open-Meteo as in the rest of the app.
    """
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        record = parse_land_record_file(file_bytes, file.filename or "upload.geojson")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse land record file: {e}")

    if not record.parcels:
        raise HTTPException(status_code=422, detail="No usable parcels found in this land record file.")

    government_polys = [p.polygon for p in record.parcels if p.classification == "government"]
    private_polys = [p.polygon for p in record.parcels if p.classification == "private"]
    obstacle_polys = [p.polygon for p in record.parcels if p.classification in ("building", "road", "water")]

    classification_counts = {}
    for p in record.parcels:
        classification_counts[p.classification] = classification_counts.get(p.classification, 0) + 1

    all_bounds = [p.polygon.bounds for p in record.parcels]  # (minx, miny, maxx, maxy)
    west = min(b[0] for b in all_bounds)
    south = min(b[1] for b in all_bounds)
    east = max(b[2] for b in all_bounds)
    north = max(b[3] for b in all_bounds)
    # Small margin so parcels right at the edge aren't clipped
    margin_deg = 0.001
    west, south, east, north = west - margin_deg, south - margin_deg, east + margin_deg, north + margin_deg

    patch_result = find_eligible_patches_from_parcels(
        west, south, east, north,
        government_polys=government_polys,
        private_polys=private_polys,
        obstacle_polys=obstacle_polys,
        cell_size_m=cell_size_m,
    )

    land_record_summary = {
        "filename": file.filename,
        "source_format": record.source_format,
        "parcels_parsed": len(record.parcels),
        "parcels_skipped": record.parcels_skipped,
        "classification_counts": classification_counts,
        "parcel_details": [
            {"khasra_no": p.khasra_no, "land_type": p.land_type_raw, "classification": p.classification}
            for p in record.parcels
        ],
    }

    if not patch_result["patches"]:
        # No eligible government land found in this record at all -- report
        # the full breakdown so the user can see WHY, but don't fabricate a
        # pond recommendation.
        return {
            "land_record_summary": land_record_summary,
            "site_check": {
                "available_area_m2": 0.0,
                "area_breakdown": patch_result["area_breakdown"],
                "note": "No eligible (government-owned, vacant) parcel found in this land record.",
            },
            "ownership_layers": patch_result["layer_boundaries"],
            "pond_recommendation": {
                "cannot_recommend": True,
                "reason": "No eligible government-owned vacant parcel was found in the uploaded land record.",
            },
        }

    selected_patch = patch_result["patches"][0]  # largest eligible patch

    # Fetch real elevation data for the record's extent, to pick the actual
    # lowest-elevation point WITHIN the eligible patch (not just its centroid).
    try:
        dem_path = await fetch_dem(south, north, west, east)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    elevation, transform, crs = load_dem(dem_path)
    slope = compute_slope_degrees(elevation, transform)
    filled = fill_depressions(np.nan_to_num(elevation, nan=99999.0))
    px_m = abs(transform.a) * 111320
    py_m = abs(transform.e) * 111320
    downstream_r, downstream_c = flow_direction_d8(filled, px_m, py_m)
    acc = flow_accumulation(filled, downstream_r, downstream_c)

    # Rasterize the SELECTED eligible patch's boundary onto the DEM's own
    # grid, so we can restrict site selection to exactly that patch.
    eligible_geoms = [shape(f["geometry"]) for f in selected_patch["boundary_geojson"]["features"]]
    if eligible_geoms:
        restrict_mask = rasterize(
            [(g, 1) for g in eligible_geoms], out_shape=elevation.shape,
            transform=transform, fill=0, default_value=1, dtype="uint8",
        ).astype(bool)
    else:
        restrict_mask = None

    try:
        row, col, site_info = select_pond_site(
            slope, acc, elevation=elevation, max_slope_deg=max_slope_deg, restrict_mask=restrict_mask,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Could not find a viable pond site within the eligible land: {e}",
        )

    site_lon, site_lat = transform * (col, row)

    result = await _full_recommendation(
        south, north, west, east, site_lat, site_lon,
        land_cover, selected_patch["area_m2"], target_capture_fraction, rainfall_years,
        auto_selected_info=site_info,
    )

    # Override the generic OSM-based fields with our real land-record-based results
    result["land_record_summary"] = land_record_summary
    result["site_check"] = {
        "available_area_m2": selected_patch["area_m2"],
        "area_breakdown": patch_result["area_breakdown"],
        "total_eligible_patches_found": len(patch_result["patches"]),
        "source": "user-uploaded land record (not OSM heuristic)",
    }
    result["ownership_layers"] = patch_result["layer_boundaries"]
    result["vacant_land_boundary_geojson"] = selected_patch["boundary_geojson"]

    return result
