"""
Catchment-related endpoints: delineate the drainage area for a clicked pour point.
"""

import numpy as np
from fastapi import APIRouter, HTTPException, Query

from app.services.elevation_client import fetch_dem
from app.services.terrain_engine import load_dem
from app.services.catchment_engine import (
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

    row, col = _latlon_to_rowcol(transform, pour_lat, pour_lon)
    rows, cols = elevation.shape

    # Allow a small tolerance for floating-point rounding right at the edge
    # (e.g. row == rows due to rounding) by clamping into range, but still
    # reject genuinely out-of-area clicks.
    if -1 <= row <= rows and -1 <= col <= cols:
        row = max(0, min(row, rows - 1))
        col = max(0, min(col, cols - 1))
    else:
        raise HTTPException(
            status_code=400,
            detail="Pour point is outside the analyzed area. This usually means the "
                   "bounding box sent doesn't actually surround the clicked point.",
        )

    # Fill NaNs (nodata) with a very high value so they're never picked as
    # flow targets, then run the catchment pipeline.
    dem_filled_nan = np.nan_to_num(elevation, nan=99999.0)
    filled = fill_depressions(dem_filled_nan)

    px_deg = abs(transform.a)
    py_deg = abs(transform.e)
    px_m = px_deg * 111320
    py_m = py_deg * 111320

    downstream_r, downstream_c = flow_direction_d8(filled, px_m, py_m)
    acc = flow_accumulation(filled, downstream_r, downstream_c)

    snapped_row, snapped_col = snap_to_channel(acc, row, col, search_radius=8)
    catchment_mask = delineate_catchment(downstream_r, downstream_c, snapped_row, snapped_col)

    cell_area_m2 = px_m * py_m
    catchment_area_m2 = float(catchment_mask.sum()) * cell_area_m2

    # Build a simple boundary polygon by tracing the outer edge of the mask,
    # using marching-squares style contour extraction (reuse skimage as in terrain_engine).
    from skimage import measure
    boundary_paths = measure.find_contours(catchment_mask.astype(float), level=0.5)

    polygons = []
    for path in boundary_paths:
        coords = [_rowcol_to_latlon(transform, r, c) for r, c in path]
        # coords are (lat, lon); GeoJSON wants [lon, lat]
        polygons.append([[lon, lat] for lat, lon in coords])

    snapped_lat, snapped_lon = _rowcol_to_latlon(transform, snapped_row, snapped_col)

    return {
        "pour_point_clicked": {"lat": pour_lat, "lon": pour_lon},
        "pour_point_snapped": {"lat": snapped_lat, "lon": snapped_lon},
        "catchment_area_m2": round(catchment_area_m2, 1),
        "catchment_area_hectares": round(catchment_area_m2 / 10000, 2),
        "catchment_cell_count": int(catchment_mask.sum()),
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
