"""
Pond recommendation endpoint: the final piece that chains catchment area,
rainfall, runoff estimation, and pond sizing into one complete recommendation.

Two entry points:
    GET /api/pond/recommend      -- caller specifies the exact pour point
                                     (e.g. a manual map click)
    GET /api/pond/suggest-site   -- caller only gives an area (e.g. a searched
                                     village's bounding box); the system
                                     automatically finds the lowest-elevation,
                                     best-draining point in that area and
                                     recommends a pond there, with no manual
                                     click required.
Both share the same underlying pipeline (catchment -> rainfall -> runoff ->
land-use check -> pond sizing -> soil check).
"""

from fastapi import APIRouter, HTTPException, Query
import numpy as np

from app.services.rainfall_client import get_historical_rainfall
from app.services.runoff_engine import estimate_daily_series_runoff, DEFAULT_CURVE_NUMBER, CURVE_NUMBERS
from app.services.pond_sizing_engine import recommend_pond
from app.services.land_use_client import fetch_obstructions
from app.services.site_suitability import find_vacant_patches
from app.services.soil_client import fetch_soil_composition
from app.services.elevation_client import fetch_dem
from app.services.terrain_engine import load_dem, compute_slope_degrees
from app.services.catchment_engine import fill_depressions, flow_direction_d8, flow_accumulation
from app.services.pond_site_selector import select_pond_site
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
    # This finds actual CONTIGUOUS open land (buildings, roads, and water
    # bodies excluded and, critically, used to correctly SEPARATE patches that
    # a road cuts apart -- not just a single leftover-area number that could
    # silently span across a highway).
    site_check = None
    selected_patch = None
    if available_site_area_m2 is None:
        obstruction_data = await fetch_obstructions(pour_lat, pour_lon, radius_m=150.0)
        if obstruction_data["query_succeeded"]:
            patch_result = find_vacant_patches(
                pour_lat, pour_lon,
                buildings=obstruction_data["buildings"],
                roads=obstruction_data["roads"],
                water=obstruction_data["water"],
                search_radius_m=150.0,
            )
            if patch_result["patches"] and patch_result["selected_patch_index"] is not None:
                selected_patch = patch_result["patches"][patch_result["selected_patch_index"]]
                available_site_area_m2 = selected_patch["area_m2"]
                site_check = {
                    "available_area_m2": available_site_area_m2,
                    "buildings_found_nearby": patch_result["buildings_found_nearby"],
                    "roads_found_nearby": patch_result["roads_found_nearby"],
                    "water_bodies_found_nearby": patch_result["water_bodies_found_nearby"],
                    "total_separate_patches_found": len(patch_result["patches"]),
                    "note": (
                        f"Selected the contiguous vacant patch actually containing/nearest to "
                        f"the site ({available_site_area_m2} m2), out of {len(patch_result['patches'])} "
                        f"separate open patch(es) found nearby. Buildings, roads, and water bodies "
                        f"are excluded, and patches split apart by a road are NOT combined."
                    ),
                }
            else:
                # No usable open patch found at all near this point
                available_site_area_m2 = 0.0
                site_check = {
                    "available_area_m2": 0.0,
                    "note": "No usable vacant land patch found within 150m of this site -- "
                            "it appears fully built-up, occupied by roads, or occupied by water. "
                            "Try a different location.",
                }
        else:
            # OSM query failed -- rather than an arbitrarily tiny fallback (which
            # causes a cascading "site too small" failure regardless of the
            # actual catchment size), assume a generous-but-not-certain portion
            # of our search radius is open, since most rural land IS open. This
            # is clearly flagged as unverified, not silently treated as fact.
            search_radius_m = 150.0
            assumed_open_fraction = 0.6
            fallback_area_m2 = 3.14159 * (search_radius_m ** 2) * assumed_open_fraction
            available_site_area_m2 = round(fallback_area_m2, 1)
            site_check = {
                "available_area_m2": available_site_area_m2,
                "note": f"OpenStreetMap obstruction check failed ({obstruction_data['error']}); "
                        f"assuming {int(assumed_open_fraction*100)}% of the {search_radius_m:.0f}m search "
                        f"radius is open land (unverified). Confirm this site is actually clear of "
                        f"buildings/roads/water before construction.",
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
