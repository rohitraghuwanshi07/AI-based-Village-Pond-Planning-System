"""
Terrain-related endpoints: DEM fetch, slope, and contour lines for an area.
"""

import numpy as np
from fastapi import APIRouter, HTTPException, Query

from app.services.elevation_client import fetch_dem
from app.services.terrain_engine import (
    classify_suitability,
    compute_slope_degrees,
    generate_contours,
    load_dem,
)

router = APIRouter(prefix="/api/terrain", tags=["terrain"])


@router.get("/analyze")
async def analyze_terrain(
    south: float = Query(..., description="Southern latitude bound"),
    north: float = Query(..., description="Northern latitude bound"),
    west: float = Query(..., description="Western longitude bound"),
    east: float = Query(..., description="Eastern longitude bound"),
    contour_interval_m: float = Query(5.0, description="Contour line interval in meters"),
    max_slope_deg: float = Query(8.0, description="Slope threshold (degrees) for 'suitable' land"),
):
    """
    Fetch the DEM for a bounding box and return slope stats + contour lines.

    Example:
    GET /api/terrain/analyze?south=21.10&north=21.19&west=79.04&east=79.13
    """
    try:
        dem_path = await fetch_dem(south, north, west, east)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    elevation, transform, crs = load_dem(dem_path)
    slope = compute_slope_degrees(elevation, transform)
    suitable_mask = classify_suitability(slope, max_slope_deg=max_slope_deg)
    contours = generate_contours(elevation, transform, interval_m=contour_interval_m)

    # Convert contours to GeoJSON FeatureCollection
    features = [
        {
            "type": "Feature",
            "properties": {"elevation_m": c["elevation"]},
            "geometry": {
                "type": "LineString",
                "coordinates": [[lon, lat] for lon, lat in c["coordinates"]],
            },
        }
        for c in contours
    ]

    return {
        "bbox": {"south": south, "north": north, "west": west, "east": east},
        "elevation_min_m": float(np.nanmin(elevation)),
        "elevation_max_m": float(np.nanmax(elevation)),
        "mean_slope_deg": round(float(np.nanmean(slope)), 2),
        "percent_suitable_land": round(float(np.nanmean(suitable_mask.astype(float))) * 100, 1),
        "contours": {"type": "FeatureCollection", "features": features},
    }
