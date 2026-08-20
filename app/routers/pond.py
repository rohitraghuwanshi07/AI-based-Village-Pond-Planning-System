"""
Pond recommendation endpoint: the final piece that chains catchment area,
rainfall, runoff estimation, and pond sizing into one complete recommendation.
"""

from fastapi import APIRouter, HTTPException, Query

from app.services.rainfall_client import get_historical_rainfall
from app.services.runoff_engine import estimate_daily_series_runoff, DEFAULT_CURVE_NUMBER, CURVE_NUMBERS
from app.services.pond_sizing_engine import recommend_pond
from app.services.land_use_client import fetch_obstructions
from app.services.site_suitability import compute_available_site_area
from app.routers.catchment import delineate as delineate_catchment_endpoint

router = APIRouter(prefix="/api/pond", tags=["pond"])


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
    Full pipeline: catchment delineation -> historical rainfall -> SCS-CN runoff
    estimate -> pond depth/area/storage capacity recommendation.

    By default, the available site area is no longer a guessed constant --
    it's derived from real OpenStreetMap building/road data near the clicked
    point, so the recommended pond footprint won't be sized based on land
    that's actually occupied by real structures.

    Example:
    GET /api/pond/recommend?south=21.10&north=21.19&west=79.04&east=79.13
        &pour_lat=21.145&pour_lon=79.09&land_cover=cultivated_land
        &target_capture_fraction=0.5
    """
    curve_number = CURVE_NUMBERS.get(land_cover, DEFAULT_CURVE_NUMBER)

    # 1. Catchment delineation (reuses the same logic as /api/catchment/delineate)
    catchment_result = await delineate_catchment_endpoint(
        south=south, north=north, west=west, east=east,
        pour_lat=pour_lat, pour_lon=pour_lon,
    )
    catchment_area_m2 = catchment_result["catchment_area_m2"]

    # 2. Historical rainfall for the pour point
    rainfall_result = await get_historical_rainfall(pour_lat, pour_lon, years=rainfall_years)
    daily_values = rainfall_result["daily_series"]["precipitation_mm"]

    # 3. Runoff estimation (SCS-CN, applied per-day for realism -- see runoff_engine.py)
    # We compute this per year-of-record and average, for a more representative estimate
    # than using only the single most recent year.
    dates = rainfall_result["daily_series"]["dates"]
    years_seen = sorted(set(d[:4] for d in dates))
    complete_years = years_seen[1:-1] or years_seen  # drop partial first/last years

    annual_runoff_depths = []
    for year in complete_years:
        year_values = [v for d, v in zip(dates, daily_values) if d.startswith(year)]
        result = estimate_daily_series_runoff(year_values, curve_number=curve_number)
        annual_runoff_depths.append(result["total_runoff_depth_mm"])

    avg_annual_runoff_depth_mm = (
        sum(annual_runoff_depths) / len(annual_runoff_depths) if annual_runoff_depths else 0.0
    )
    avg_annual_runoff_volume_m3 = (avg_annual_runoff_depth_mm / 1000) * catchment_area_m2

    # 4. Determine real available site area from OpenStreetMap building/road data,
    # unless the caller explicitly overrode it.
    site_check = None
    if available_site_area_m2 is None:
        obstruction_data = await fetch_obstructions(pour_lat, pour_lon, radius_m=150.0)
        if obstruction_data["query_succeeded"]:
            site_check = compute_available_site_area(
                pour_lat, pour_lon,
                buildings=obstruction_data["buildings"],
                roads=obstruction_data["roads"],
                search_radius_m=150.0,
            )
            available_site_area_m2 = site_check["available_area_m2"]
        else:
            # OSM query failed (network issue, rate limit, etc) -- fall back to a
            # clearly-flagged conservative estimate rather than silently guessing.
            available_site_area_m2 = 5000.0
            site_check = {
                "available_area_m2": available_site_area_m2,
                "note": f"OpenStreetMap obstruction check failed ({obstruction_data['error']}); "
                        f"using a conservative fallback. Verify this site manually before construction.",
            }

    # 5. Pond sizing recommendation
    pond_result = recommend_pond(
        required_volume_m3=avg_annual_runoff_volume_m3,
        available_site_area_m2=available_site_area_m2,
        target_capture_fraction=target_capture_fraction,
    )

    return {
        "location": {"lat": pour_lat, "lon": pour_lon},
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
        "pond_recommendation": pond_result,
    }
