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
import asyncio
import math
import numpy as np
from rasterio.features import rasterize
from shapely.geometry import shape

from app.services.rainfall_client import get_historical_rainfall, fallback_rainfall_result
from app.services.runoff_engine import (
    estimate_daily_series_runoff,
    estimate_annual_runoff_volume,
    DEFAULT_CURVE_NUMBER,
    CURVE_NUMBERS,
)
from app.services.pond_sizing_engine import recommend_pond
from app.services.land_use_client import fetch_water_bodies_in_bbox
from app.services.site_suitability import find_eligible_patches_from_parcels
from app.services.soil_client import fetch_soil_composition
from app.services.elevation_client import fetch_dem
from app.services.terrain_engine import load_dem, compute_slope_degrees, pixel_size_meters
from app.services.catchment_engine import fill_depressions, flow_direction_d8, flow_accumulation
from app.services.pond_site_selector import select_pond_site, select_top_n_pond_sites, _generate_rank_explanation
from app.services.land_record_parser import parse_land_record_file
from app.routers.catchment import delineate as delineate_catchment_endpoint

router = APIRouter(prefix="/api/pond", tags=["pond"])

# Live Available Lands / OSM ownership checks are disabled.
# When the caller does not provide a site area, this planning value is used
# only for pond sizing; it is NOT a claim that the land is legally available.
DEFAULT_PLANNING_SITE_AREA_M2 = 1000.0


async def _fetch_water_polygons(south: float, north: float, west: float, east: float) -> list:
    """Network-only half of the water-exclusion mask: fetch the raw water
    geometries for a bbox. Split out from the rasterize step so callers can
    kick this off CONCURRENTLY with the DEM download (fetch_dem) instead of
    waiting for the DEM before even starting the water query -- the two are
    independent network calls over the same bbox, so there's no reason to
    pay for them one after another."""
    water_data = await fetch_water_bodies_in_bbox(south, north, west, east)
    return water_data.get("water", [])


def _rasterize_water_exclusion_mask(
    water_polys: list, south: float, north: float,
    transform, out_shape: tuple[int, int], buffer_m: float = 50.0,
) -> np.ndarray:
    """
    Rasterize already-fetched water-body geometries, buffered by buffer_m,
    onto the DEM's own grid -- so pond-site selection can hard-exclude any
    cell that's already water. Shared by /suggest-site and
    /suggest-top-sites so both auto-siting paths apply the exact same
    real-water-body safety check.
    """
    lat_mid = (north + south) / 2
    lon_deg_per_m = 1.0 / (111_320.0 * math.cos(math.radians(lat_mid)))
    buffer_deg = buffer_m * lon_deg_per_m

    water_shapes = []
    for w in water_polys:
        try:
            water_shapes.append((w.buffer(buffer_deg), 1))
        except Exception:
            continue

    if not water_shapes:
        return np.zeros(out_shape, dtype=bool)

    return rasterize(
        water_shapes, out_shape=out_shape, transform=transform,
        fill=0, default_value=1, dtype="uint8",
    ).astype(bool)


def _years_analyzed(rainfall_result: dict) -> int:
    """How many complete years of rainfall the runoff average above was
    computed over -- kept as its own tiny helper (rather than threading an
    extra return value out of _avg_annual_runoff_depth_mm) so the response
    can report it without duplicating the runoff math itself."""
    dates = rainfall_result["daily_series"]["dates"]
    years_seen = sorted(set(d[:4] for d in dates))
    complete_years = years_seen[1:-1] or years_seen
    return len(complete_years)


def _avg_annual_runoff_depth_mm(rainfall_result: dict, curve_number: float) -> float:
    """
    Average annual runoff depth (mm), computed by running the SCS-CN formula
    on EACH day of each complete year in the fetched rainfall series (more
    accurate than applying it once to an annual total -- see
    estimate_daily_series_runoff's docstring), then averaging across years.
    Shared between the single-site recommendation and the top-N ranked-site
    pond sizing, so both use the exact same runoff methodology.
    """
    daily_values = rainfall_result["daily_series"]["precipitation_mm"]
    dates = rainfall_result["daily_series"]["dates"]
    years_seen = sorted(set(d[:4] for d in dates))
    complete_years = years_seen[1:-1] or years_seen

    annual_runoff_depths = []
    for year in complete_years:
        year_values = [v for d, v in zip(dates, daily_values) if d.startswith(year)]
        result = estimate_daily_series_runoff(year_values, curve_number=curve_number)
        annual_runoff_depths.append(result["total_runoff_depth_mm"])

    return sum(annual_runoff_depths) / len(annual_runoff_depths) if annual_runoff_depths else 0.0


def _runoff_from_rainfall(rainfall_result: dict, curve_number: float, catchment_area_m2: float) -> tuple[float, float]:
    """
    Compute (avg_annual_runoff_depth_mm, avg_annual_runoff_volume_m3) from a
    rainfall_result dict, handling BOTH cases:
      - live data available: run the more accurate day-by-day SCS-CN method
        over the real fetched daily series (_avg_annual_runoff_depth_mm).
      - rainfall_result["data_unavailable"] is True (the live Open-Meteo call
        failed and rainfall_client.fallback_rainfall_result() was used): the
        daily series is empty, so the day-by-day method would silently
        return 0 -- which is wrong, not "no rain". Fall back to the single
        annual-total SCS-CN application against the regional average instead,
        so a failed network call degrades to a labeled estimate rather than
        a fake near-zero result that cascades into a degenerate pond size.
    """
    if rainfall_result.get("data_unavailable"):
        annual_result = estimate_annual_runoff_volume(
            rainfall_result["annual_average_mm"], catchment_area_m2, curve_number,
        )
        return annual_result["runoff_depth_mm"], annual_result["runoff_volume_m3"]

    depth_mm = _avg_annual_runoff_depth_mm(rainfall_result, curve_number)
    volume_m3 = (depth_mm / 1000) * catchment_area_m2
    return depth_mm, volume_m3


async def _full_recommendation(
    south: float, north: float, west: float, east: float,
    pour_lat: float, pour_lon: float,
    land_cover: str,
    available_site_area_m2: float | None,
    target_capture_fraction: float,
    rainfall_years: int,
    auto_selected_info: dict | None = None,
):
    """Shared pipeline used by /recommend and /suggest-site.

    Live Available Lands / OSM ownership and vacancy checks are intentionally
    disabled.  A caller-provided site area is used directly; when no area is
    supplied, a planning area is used only for pond sizing.  Automatic site
    selection still uses elevation, drainage, slope, and water-body exclusion.
    """
    curve_number = CURVE_NUMBERS.get(land_cover, DEFAULT_CURVE_NUMBER)

    # Catchment, rainfall, and soil are the only per-site network tasks here.
    # No live ownership/Available Lands/Overpass query is made.
    tasks = [
        delineate_catchment_endpoint(
            south=south, north=north, west=west, east=east,
            pour_lat=pour_lat, pour_lon=pour_lon,
        ),
        get_historical_rainfall(pour_lat, pour_lon, years=rainfall_years),
        fetch_soil_composition(pour_lat, pour_lon),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    catchment_result = results[0]
    rainfall_result = results[1]
    soil_check = results[2]

    if isinstance(catchment_result, Exception):
        raise HTTPException(status_code=500, detail=f"Catchment delineation failed: {catchment_result}")
    if isinstance(rainfall_result, Exception):
        rainfall_result = fallback_rainfall_result(pour_lat, pour_lon)
    if isinstance(soil_check, Exception):
        soil_check = {"query_succeeded": False, "note": f"Soil check failed: {soil_check}"}

    catchment_area_m2 = catchment_result["catchment_area_m2"]
    rainfall_data_unavailable = rainfall_result.get("data_unavailable", False)
    avg_annual_runoff_depth_mm, avg_annual_runoff_volume_m3 = _runoff_from_rainfall(
        rainfall_result, curve_number, catchment_area_m2,
    )

    # Live Available Lands / ownership checks are disabled.
    # Use a supplied planning area, or a fixed planning value when omitted.
    if available_site_area_m2 is None:
        available_site_area_m2 = DEFAULT_PLANNING_SITE_AREA_M2
        area_source = "default planning area (live Available Lands check disabled)"
    else:
        area_source = "manually supplied site area"

    data_unavailable = rainfall_data_unavailable
    site_check = {
        "available_area_m2": available_site_area_m2,
        "source": area_source,
        "ownership_checked": False,
        "live_available_lands_check": False,
        "note": (
            "Live land ownership/availability verification is disabled. "
            "The area shown here is a planning value and must be verified "
            "against official land records before construction."
        ),
    }
    selected_patch = None
    ownership_layers = None

    # Pond sizing recommendation
    pond_result = recommend_pond(
        required_volume_m3=avg_annual_runoff_volume_m3,
        available_site_area_m2=available_site_area_m2,
        target_capture_fraction=target_capture_fraction,
        data_unavailable=data_unavailable,
    )

    # Scoring + plain-language explanation for the site actually being shown.
    manual_score_info = None
    if "terrain_at_snapped_point" in catchment_result:
        terrain = catchment_result["terrain_at_snapped_point"]
        elev_range = max(terrain["elevation_max"] - terrain["elevation_min"], 1e-9)
        norm_elev = 1.0 - (terrain["elevation_m"] - terrain["elevation_min"]) / elev_range
        acc_range = max(terrain["accumulation_max"] - 0, 1e-9)
        norm_acc = terrain["accumulation"] / acc_range
        slope_range = max(terrain["slope_max"] - terrain["slope_min"], 1e-9)
        norm_slope = 1.0 - (terrain["slope_deg"] - terrain["slope_min"]) / slope_range

        elevation_weight, accumulation_weight, slope_weight = 0.40, 0.25, 0.15
        total_weight = elevation_weight + accumulation_weight + slope_weight
        composite = (
            elevation_weight * norm_elev
            + accumulation_weight * norm_acc
            + slope_weight * norm_slope
        ) / total_weight
        raw_score = composite * 100.0
        scores = {
            "elevation_score": round(norm_elev * 100, 1),
            "accumulation_score": round(norm_acc * 100, 1),
            "slope_score": round(norm_slope * 100, 1),
        }
        nearby = {
            "buildings_nearby": 0,
            "roads_nearby": 0,
            "water_bodies_nearby": 0,
            "data_unavailable": data_unavailable,
        }
        final_score = raw_score
        rank_label = auto_selected_info["rank"] if auto_selected_info and auto_selected_info.get("rank") else "Manual"
        explanation = _generate_rank_explanation(
            rank_label, scores, nearby, final_score,
            raw_score=raw_score, data_unavailable=data_unavailable,
        )
        manual_score_info = {
            "raw_score": round(raw_score, 1),
            "composite_score": round(final_score, 1),
            "scores": scores,
            "nearby_obstacles": nearby,
            "explanation": explanation,
        }

    return {
        "location": {"lat": pour_lat, "lon": pour_lon},
        "vacant_land_boundary_geojson": None,
        "ownership_layers": None,
        "auto_selected": auto_selected_info,
        "manual_score_info": manual_score_info,
        "catchment": {
            "area_m2": catchment_area_m2,
            "area_hectares": catchment_result["catchment_area_hectares"],
            "boundary_geojson": catchment_result["catchment_boundary_geojson"],
            "snapped_pour_point": catchment_result["pour_point_snapped"],
        },
        "rainfall": {
            "years_analyzed": _years_analyzed(rainfall_result),
            "annual_average_mm": rainfall_result["annual_average_mm"],
            "monsoon_average_mm": rainfall_result["monsoon_average_mm"],
            "data_unavailable": rainfall_data_unavailable,
            "note": rainfall_result.get("note"),
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
    # DEM download and the water-body query are independent network calls
    # over the same bbox -- fire them concurrently instead of paying for
    # them one after another.
    dem_task = asyncio.create_task(fetch_dem(south, north, west, east))
    water_task = asyncio.create_task(_fetch_water_polygons(south, north, west, east))

    try:
        dem_path = await dem_task
    except RuntimeError as e:
        water_task.cancel()
        raise HTTPException(status_code=500, detail=str(e))

    elevation, transform, crs = load_dem(dem_path)
    slope = compute_slope_degrees(elevation, transform)

    filled = fill_depressions(np.nan_to_num(elevation, nan=99999.0))
    px_m, py_m = pixel_size_meters(transform, elevation.shape)
    downstream_r, downstream_c = flow_direction_d8(filled, px_m, py_m)
    acc = flow_accumulation(filled, downstream_r, downstream_c)

    # Hard-exclude existing water bodies BEFORE scoring. Without this, the
    # scoring itself (low elevation + high flow accumulation) would actively
    # favor cells that are already inside a river/lake/canal -- that's what
    # was causing the auto-selected site to sometimes land in water.
    try:
        water_polys = await water_task
    except Exception:
        water_polys = []  # water check is a safety layer, not a hard dependency -- don't fail the whole request over it
    water_exclusion_mask = _rasterize_water_exclusion_mask(
        water_polys, south, north, transform, elevation.shape,
    )

    row, col, site_info = select_pond_site(
        slope, acc, elevation=elevation, max_slope_deg=max_slope_deg,
        water_exclusion_mask=water_exclusion_mask,
    )
    lon, lat = transform * (col, row)

    result = await _full_recommendation(
        south, north, west, east, lat, lon,
        land_cover, available_site_area_m2, target_capture_fraction, rainfall_years,
        auto_selected_info=site_info,
    )
    return result


@router.get("/suggest-top-sites")
async def suggest_top_sites(
    south: float = Query(..., description="Southern latitude bound of the search area"),
    north: float = Query(..., description="Northern latitude bound"),
    west: float = Query(..., description="Western longitude bound"),
    east: float = Query(..., description="Eastern longitude bound"),
    boundary_polygon: str | None = Query(None, description="Optional lon,lat;lon,lat string of the bounding polygon"),
    n_sites: int = Query(3, ge=1, le=10, description="Number of top sites to return"),
    max_slope_deg: float = Query(8.0, ge=1.0, le=45.0, description="Max slope considered suitable"),
    land_cover: str = Query("cultivated_land", description=f"One of: {list(CURVE_NUMBERS.keys())}"),
    target_capture_fraction: float = Query(0.5, ge=0.05, le=1.0, description="Fraction of annual runoff each ranked pond should target capturing"),
    rainfall_years: int = Query(10, ge=1, le=30),
):
    """
    Finds and ranks the top N candidate pond sites within a search area.
    Explicitly excludes sites on or near existing water bodies. Live Available
    Lands/ownership/vacancy checks are disabled. Each ranked site also gets
    its own recommended pond size (depth + surface area),
    sized from that site's own catchment/runoff -- not just its eligible
    land area -- so the "sub-boundary" shown for each rank reflects how big
    that pond should actually be, not just how much land happens to be free.
    """
    curve_number = CURVE_NUMBERS.get(land_cover, DEFAULT_CURVE_NUMBER)

    # Cap the search area. The "whole village" flow uses whatever bbox the
    # geocoder returned -- Photon gives some places (esp. a town/tehsil
    # searched by name rather than a specific landmark) an administrative
    # "extent" that can be tens of km across. That doesn't just mean "a
    # bigger DEM download" -- every step below (Overpass water/obstruction
    # queries, rasterizing the exclusion mask, D8 flow routing) scales with
    # pixel count, so an uncapped bbox is the single biggest thing standing
    # between this endpoint and a fast, predictable response time. A pond
    # site search is inherently local anyway (a village doesn't need a
    # 40km-wide window to find its 3 best pond spots), so the box is
    # shrunk toward its own center rather than toward some fixed corner --
    # this keeps whatever point the person actually searched for centered,
    # just tighter.
    MAX_SEARCH_SPAN_DEG = 0.15  # ~16.5km -- comfortably covers a real village or a generously-drawn boundary selection; only kicks in for outsized admin-area geocode extents
    if (north - south) > MAX_SEARCH_SPAN_DEG or (east - west) > MAX_SEARCH_SPAN_DEG:
        center_lat, center_lon = (south + north) / 2, (west + east) / 2
        half = MAX_SEARCH_SPAN_DEG / 2
        south = max(south, center_lat - half)
        north = min(north, center_lat + half)
        west = max(west, center_lon - half)
        east = min(east, center_lon + half)

    # Enforce a MINIMUM span too. There was no floor here before: a
    # hand-drawn 4-corner boundary selection can easily be a tiny box (a
    # quick demo click, or 4 corners placed close together), and a DEM
    # window that small (a) doesn't give the D8 flow-routing algorithm
    # enough real upstream terrain to find a genuine drainage point, and
    # (b) can trip OpenTopography's own minimum-area requirement, which
    # comes back as a request failure every single time for that box,
    # regardless of exactly where it's drawn. Padding out to a workable
    # minimum here does NOT loosen what the user actually gets recommended
    # -- the drawn polygon is still applied afterwards as a hard exclusion
    # mask, so candidate sites are still confined to inside it; this just
    # gives the terrain analysis enough surrounding context to work with.
    MIN_SEARCH_SPAN_DEG = 0.03  # ~3.3km
    if (north - south) < MIN_SEARCH_SPAN_DEG or (east - west) < MIN_SEARCH_SPAN_DEG:
        center_lat, center_lon = (south + north) / 2, (west + east) / 2
        half = MIN_SEARCH_SPAN_DEG / 2
        south = min(south, center_lat - half)
        north = max(north, center_lat + half)
        west = min(west, center_lon - half)
        east = max(east, center_lon + half)

    # DEM download, the water-body query, and the rainfall lookup are all
    # independent network calls -- rainfall in particular only needs a
    # representative point (climatology doesn't meaningfully vary across a
    # search area this size), so it's fetched ONCE here and shared across
    # every candidate below, rather than once per candidate.
    dem_task = asyncio.create_task(fetch_dem(south, north, west, east))
    water_task = asyncio.create_task(_fetch_water_polygons(south, north, west, east))
    rainfall_task = asyncio.create_task(
        get_historical_rainfall((south + north) / 2, (west + east) / 2, years=rainfall_years)
    )

    try:
        dem_path = await dem_task
    except RuntimeError as e:
        water_task.cancel()
        rainfall_task.cancel()
        raise HTTPException(status_code=500, detail=str(e))

    elevation, transform, crs = load_dem(dem_path)
    slope = compute_slope_degrees(elevation, transform)

    filled = fill_depressions(np.nan_to_num(elevation, nan=99999.0))
    px_m, py_m = pixel_size_meters(transform, elevation.shape)
    downstream_r, downstream_c = flow_direction_d8(filled, px_m, py_m)
    acc = flow_accumulation(filled, downstream_r, downstream_c)

    rows, cols = elevation.shape
    try:
        water_polys = await water_task
    except Exception:
        water_polys = []  # water check is a safety layer, not a hard dependency -- don't fail the whole request over it
    exclusion_mask = _rasterize_water_exclusion_mask(
        water_polys, south, north, transform, (rows, cols),
    )

    if boundary_polygon:
        from shapely.geometry import Polygon as ShapelyPolygon
        try:
            coords = [tuple(map(float, pt.split(','))) for pt in boundary_polygon.split(';')]
            if len(coords) >= 3:
                user_poly = ShapelyPolygon(coords)
                if not user_poly.is_valid:
                    # A self-intersecting ("bowtie") polygon -- e.g. 4 corners
                    # supplied in other than perimeter order -- is technically
                    # invalid and can rasterize to near-ZERO real area even
                    # though it looks like a normal quadrilateral. buffer(0)
                    # is the standard Shapely fix: it resolves self-
                    # intersections into the equivalent valid geometry
                    # (usually via the union of its non-overlapping parts)
                    # instead of silently excluding almost the whole area the
                    # user actually intended to select.
                    user_poly = user_poly.buffer(0)
                # rasterize fills background with 1 (excluded), draws polygon with 0 (allowed)
                poly_mask = rasterize(
                    [(user_poly, 0)], out_shape=(rows, cols), transform=transform,
                    fill=1, dtype="uint8"
                ).astype(bool)
                exclusion_mask = np.logical_or(exclusion_mask, poly_mask)
        except Exception as e:
            print(f"Failed to parse boundary_polygon: {e}")

    # Select top N candidate sites
    candidates = select_top_n_pond_sites(
        slope, acc, elevation=elevation, max_slope_deg=max_slope_deg,
        water_exclusion_mask=exclusion_mask, n_sites=n_sites,
        transform=transform
    )

    if not candidates:
        raise HTTPException(status_code=404, detail="No suitable pond sites found in this area.")

    # Delineate each candidate's own catchment. fill_depressions/flow_direction/
    # flow_accumulation depend only on the DEM (not on which candidate point
    # we're delineating from), and were ALREADY computed above for site
    # SELECTION -- so each candidate here only needs the cheap, genuinely
    # per-point part (snap-to-channel + trace upstream), reusing that same
    # computation instead of redoing it 3 times. That redundant 3x redo used
    # to be the single biggest cost this endpoint added when pond sizing was
    # introduced (~2s of DEM-wide prep repeated per candidate); reusing it
    # brings all 3 candidates down to a fraction of a second combined, and
    # unlike spreading those 3 redone computations across threads/processes,
    # this is a real reduction in total work rather than just parallelizing
    # the same work (which doesn't help on a single-core machine anyway).
    from app.routers.catchment import _catchment_from_precomputed
    from app.services.catchment_engine import build_reverse_graph

    # Built ONCE and reused for every candidate below -- the reverse
    # drainage graph depends only on downstream_r/downstream_c (already
    # fixed for this whole request), not on which candidate is being
    # traced. Without this, delineate_catchment used to rebuild the same
    # graph from scratch inside each of the 3 calls below. See
    # build_reverse_graph's docstring.
    reverse_graph = build_reverse_graph(downstream_r, downstream_c)

    catchment_results = []
    for c in candidates:
        try:
            catchment_results.append(
                _catchment_from_precomputed(
                    elevation, transform, downstream_r, downstream_c, acc, c["lat"], c["lon"],
                    reverse_graph=reverse_graph,
                )
            )
        except Exception as e:
            catchment_results.append(e)

    try:
        rainfall_result = await rainfall_task
    except Exception:
        rainfall_result = fallback_rainfall_result((south + north) / 2, (west + east) / 2)
    rainfall_data_unavailable = rainfall_result.get("data_unavailable", False)

    if rainfall_data_unavailable:
        # Live fetch failed -- use the single-shot SCS-CN runoff depth for
        # the regional fallback average instead of the (empty) daily
        # series, same reasoning as _runoff_from_rainfall above. This path
        # sizes ponds per-candidate against each candidate's own catchment
        # area below, so only the runoff DEPTH (mm) is shared here; each
        # candidate turns it into its own volume once its catchment area is
        # known.
        avg_annual_runoff_depth_mm = estimate_annual_runoff_volume(
            rainfall_result["annual_average_mm"], 1.0, curve_number,
        )["runoff_depth_mm"]
    else:
        avg_annual_runoff_depth_mm = _avg_annual_runoff_depth_mm(rainfall_result, curve_number)

    # Lightweight suitability check for each candidate
    def process_candidate_sync(cand, catchment_result):
        lat, lon = cand["lat"], cand["lon"]

        # Live Available Lands / vacancy checks are disabled.
        # Use a planning area solely for pond sizing.
        available_area = DEFAULT_PLANNING_SITE_AREA_M2
        vacant_geojson = None
        nearby = {
            "buildings_nearby": 0,
            "roads_nearby": 0,
            "water_bodies_nearby": 0,
            "data_unavailable": rainfall_data_unavailable,
        }

        raw_score = cand["composite_score"]
        final_score = raw_score

        if isinstance(catchment_result, Exception) or catchment_result is None:
            pond_sizing = {
                "cannot_recommend": True,
                "data_unavailable": False,
                "reason": f"Catchment could not be computed for this site: {catchment_result}",
                "recommended_surface_area_m2": None,
            }
            catchment_area_m2 = None
        else:
            catchment_area_m2 = catchment_result["catchment_area_m2"]
            runoff_volume_m3 = (avg_annual_runoff_depth_mm / 1000) * catchment_area_m2
            pond_sizing = recommend_pond(
                runoff_volume_m3, available_area, target_capture_fraction,
                data_unavailable=rainfall_data_unavailable,
            )

        return {
            "rank": cand["rank"],
            "location": {"lat": lat, "lon": lon},
            "composite_score": final_score,
            "raw_score": raw_score,
            "scores": cand["scores"],
            "nearby_obstacles": nearby,
            "available_area_m2": available_area,
            "vacant_land_boundary_geojson": vacant_geojson,
            "catchment_area_m2": catchment_area_m2,
            "pond_sizing": pond_sizing,
            "selection_info": cand["selection_info"],
        }

    ranked_sites = [process_candidate_sync(c, catchment_result) for c, catchment_result in zip(candidates, catchment_results)]

    # Re-sort by final score just in case penalties changed the ordering
    ranked_sites.sort(key=lambda x: x["composite_score"], reverse=True)
    # Update ranks after re-sort
    for i, site in enumerate(ranked_sites):
        site["rank"] = i + 1
        site["explanation"] = _generate_rank_explanation(
            site["rank"], 
            site["scores"], 
            site["nearby_obstacles"], 
            site["composite_score"], 
            raw_score=site.get("raw_score"),
            data_unavailable=site["nearby_obstacles"].get("data_unavailable", False),
        )

    return {
        "ranked_sites": ranked_sites,
        "rainfall": {
            "annual_average_mm": rainfall_result["annual_average_mm"],
            "monsoon_average_mm": rainfall_result.get("monsoon_average_mm"),
            "data_unavailable": rainfall_data_unavailable,
            "note": rainfall_result.get("note"),
        },
    }


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
    px_m, py_m = pixel_size_meters(transform, elevation.shape)
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
