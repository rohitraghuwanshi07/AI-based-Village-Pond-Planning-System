"""
Rainfall-related endpoints: historical precipitation stats for a location.
"""

from fastapi import APIRouter, Query

from app.services.rainfall_client import get_historical_rainfall

router = APIRouter(prefix="/api/rainfall", tags=["rainfall"])


@router.get("")
async def rainfall_stats(
    lat: float = Query(..., description="Latitude of the location"),
    lon: float = Query(..., description="Longitude of the location"),
    years: int = Query(10, ge=1, le=30, description="Number of past years to analyze"),
):
    """
    Get historical rainfall statistics for a location using Open-Meteo's archive.

    Example: GET /api/rainfall?lat=21.1458&lon=79.0882&years=10
    """
    return await get_historical_rainfall(lat, lon, years=years)
