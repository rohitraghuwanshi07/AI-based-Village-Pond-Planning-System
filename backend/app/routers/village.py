"""
Village-related endpoints: search/geocoding.
"""

from fastapi import APIRouter, HTTPException, Query

from app.services.geocode_client import geocode_village

router = APIRouter(prefix="/api/village", tags=["village"])


@router.get("/search")
async def search_village(
    q: str = Query(..., min_length=2, description="Village or place name to search for"),
    country: str | None = Query("in", description="ISO country code to bias search, e.g. 'in'. Pass empty string for global search."),
):
    """
    Geocode a village name to lat/lon + bounding box using Nominatim (OpenStreetMap).

    Example: GET /api/village/search?q=Kondhali&country=in
    """
    country_codes = country if country else None
    result = await geocode_village(q, country_codes=country_codes)

    if result is None:
        raise HTTPException(status_code=404, detail=f"No village found matching '{q}'")

    return result
