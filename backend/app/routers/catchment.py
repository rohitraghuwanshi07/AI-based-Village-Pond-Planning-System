"""
Catchment-related endpoints: delineate the drainage area for a clicked pour point.
"""

import numpy as np
from fastapi import APIRouter, HTTPException, Query

from app.services.elevation_client import fetch_dem
from app.services.terrain_engine import load_dem, pixel_size_meters
from app.services.catchment_engine import (
    build_reverse_graph,
    delineate_catchment,
    fill_depressions,
    flow_accumulation,
    flow_direction_d8,
    snap_to_channel,
)

router = APIRouter(prefix="/api/catchment", tags=["catchment"])


def _latlon_to_rowcol(transform, lat: float, lon: float):
    """Convert geographic coordinates to raster row/col indices using the inverse affine transform."""
    col, row = ~transform * (lon, lat)
    return int(round(row)), int(round(col))


def _rowcol_to_latlon(transform, row: int, col: int):
    lon, lat = transform * (col, row)
    return lat, lon


@router.get("/delineate")
async def delineate(
    south: float = Query(..., description="Southern latitude bound of the analysis area"),
    north: float = Query(..., description="Northern latitude bound"),
    west: float = Query(..., description="Western longitude bound"),
    east: float = Query(..., description="Eastern longitude bound"),
    pour_lat: float = Query(..., description="Latitude of the clicked pond site (pour point)"),
    pour_lon: float = Query(..., description="Longitude of the clicked pond site"),
):
    """
    Delineate the catchment (watershed) area draining to a clicked point.

    The bounding box should be a reasonably tight area around the village/site
    of interest — a large bbox means a bigger DEM and slower processing.

    Example:
    GET /api/catchment/delineate?south=21.10&north=21.19&west=79.04&east=79.13&pour_lat=21.145&pour_lon=79.09
    """
    try:
        dem_path = await fetch_dem(south, north, west, east)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    elevation, transform, crs = load_dem(dem_path)

    try:
        return _delineate_sync(elevation, transform, pour_lat, pour_lon)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _delineate_sync(elevation: np.ndarray, transform, pour_lat: float, pour_lon: float) -> dict:
    """
    The actual (CPU-bound, no I/O) catchment delineation, given an
    already-loaded DEM. Split out from the /delineate endpoint so callers
    that already have the DEM loaded (e.g. the single manually-clicked site)
    can reuse it directly, avoiding a redundant fetch_dem+load_dem
    round-trip.

    This computes the full terrain prep (fill_depressions, flow direction,
    flow accumulation) itself. When that prep has ALREADY been done for this
    exact DEM (e.g. ranking several candidate sites within the same search
    area, where site SELECTION already needed this exact computation),
    call _catchment_from_precomputed directly instead -- see its docstring
    for why that matters a lot for performance.
    """
    # Fill NaNs (nodata) with a very high value so they're never picked as
    # flow targets, then run the catchment pipeline.
    dem_filled_nan = np.nan_to_num(elevation, nan=99999.0)
    filled = fill_depressions(dem_filled_nan)

    # NOTE: pixel width in meters must use the cos(latitude) correction for
    # longitude (1 degree of longitude is shorter than 1 degree of latitude
    # away from the equator) -- using a flat 111,320 m/degree for both axes,
    # as this used to do, overstates cell area (and therefore catchment area
    # and downstream runoff volume) by 1/cos(latitude): ~7% too large at
    # 21°N, ~19% too large at 45°N. pixel_size_meters() is the shared,
    # correct conversion also used by terrain_engine.compute_slope_degrees.
    px_m, py_m = pixel_size_meters(transform, elevation.shape)
    downstream_r, downstream_c = flow_direction_d8(filled, px_m, py_m)
    acc = flow_accumulation(filled, downstream_r, downstream_c)

    return _catchment_from_precomputed(elevation, transform, downstream_r, downstream_c, acc, pour_lat, pour_lon)


def _catchment_from_precomputed(
    elevation: np.ndarray, transform,
    downstream_r: np.ndarray, downstream_c: np.ndarray, acc: np.ndarray,
    pour_lat: float, pour_lon: float,
    reverse_graph: dict | None = None,
) -> dict:
    """
    The pour-point-specific part of catchment delineation ONLY (snap to
    channel, trace upstream, measure/smooth the result) -- given flow
    direction and accumulation that have ALREADY been computed for this DEM.

    Why this is split out: fill_depressions + flow_direction_d8 +
    flow_accumulation depend only on the DEM itself, not on which point
    you're delineating from -- so for N candidate points in the same search
    area they give the EXACT SAME result every time. Sizing every one of the
    top-3 ranked sites used to call the equivalent of _delineate_sync (which
    redoes that DEM-wide prep) once per candidate: 3 full, independent
    re-runs of the expensive part, even though /suggest-top-sites had
    already computed it once already for site SELECTION moments earlier.
    Measured cost of that redundancy: ~2s of DEM-wide prep repeated 3
    times (~6s) versus ~0.2s per candidate here -- almost 10x faster, and
    unlike spreading the 3 calls across threads or processes, this actually
    helps on a single-core machine too, since it does the same total work
    fewer times, rather than just doing the same amount of work with more
    parallelism.
    """
    row, col = _latlon_to_rowcol(transform, pour_lat, pour_lon)
    rows, cols = elevation.shape

    # Allow a small tolerance for floating-point rounding right at the edge
    # (e.g. row == rows due to rounding) by clamping into range, but still
    # reject genuinely out-of-area clicks.
    if -1 <= row <= rows and -1 <= col <= cols:
        row = max(0, min(row, rows - 1))
        col = max(0, min(col, cols - 1))
    else:
        raise ValueError(
            "Pour point is outside the analyzed area. This usually means the "
            "bounding box sent doesn't actually surround the clicked point."
        )

    px_m, py_m = pixel_size_meters(transform, elevation.shape)

    snapped_row, snapped_col = snap_to_channel(acc, row, col, search_radius=8)
    catchment_mask = delineate_catchment(downstream_r, downstream_c, snapped_row, snapped_col, reverse_graph=reverse_graph)

    cell_area_m2 = px_m * py_m
    catchment_area_m2 = float(catchment_mask.sum()) * cell_area_m2

    # Build a smoothed boundary polygon by tracing the outer edge of the mask
    # (marching-squares style, reusing skimage as in terrain_engine), then
    # rounding off the raw pixel staircase so it reads as a real catchment
    # outline on the map rather than a blocky raster mask.
    from app.services.site_suitability import _mask_to_smoothed_polygons
    polygons = _mask_to_smoothed_polygons(catchment_mask, transform)

    from app.services.terrain_engine import compute_slope_degrees
    slope_deg = compute_slope_degrees(elevation, transform)

    snapped_lat, snapped_lon = _rowcol_to_latlon(transform, snapped_row, snapped_col)

    return {
        "pour_point_clicked": {"lat": pour_lat, "lon": pour_lon},
        "pour_point_snapped": {"lat": snapped_lat, "lon": snapped_lon},
        "catchment_area_m2": round(catchment_area_m2, 1),
        "catchment_area_hectares": round(catchment_area_m2 / 10000, 2),
        "catchment_cell_count": int(catchment_mask.sum()),
        "terrain_at_snapped_point": {
            "elevation_m": round(float(elevation[snapped_row, snapped_col]), 2),
            "slope_deg": round(float(slope_deg[snapped_row, snapped_col]), 2),
            "accumulation": int(acc[snapped_row, snapped_col]),
            "elevation_min": float(np.nanmin(elevation)),
            "elevation_max": float(np.nanmax(elevation)),
            "accumulation_max": float(np.nanmax(acc)),
            "slope_min": float(np.nanmin(slope_deg)),
            "slope_max": float(np.nanmax(slope_deg))
        },
        "catchment_boundary_geojson": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {},
                    "geometry": {"type": "Polygon", "coordinates": [poly]},
                }
                for poly in polygons
                if len(poly) >= 4
            ],
        },
    }
