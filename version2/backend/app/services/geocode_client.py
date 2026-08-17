"""
Geocoding client using Open-Meteo's free Geocoding API — no API key, no strict
IP/User-Agent blocking (unlike Nominatim, which can be finicky about IPs).

Docs: https://open-meteo.com/en/docs/geocoding-api

We use this same Open-Meteo family of APIs for rainfall later, so this keeps
our external dependencies simple.
"""

import httpx

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

HEADERS = {"User-Agent": "village-pond-planner-student-project"}


async def geocode_village(query: str, country_codes: str | None = "IN") -> dict | None:
    """
    Look up a village/place name and return its location.

    Args:
        query: place name, e.g. "Nagpur" or "Kondhali"
        country_codes: ISO-2 country code to bias results (default "IN" for India);
                       pass None to search globally.

    Returns:
        dict with name, lat, lon, and an approximate bounding box, or None if not found.
    """
    params = {
        "name": query,
        "count": 5,
        "language": "en",
        "format": "json",
    }
    if country_codes:
        params["countryCode"] = country_codes

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(GEOCODING_URL, params=params, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results")
    if not results:
        return None

    place = results[0]
    lat = float(place["latitude"])
    lon = float(place["longitude"])

    # Open-Meteo's geocoder doesn't return a bounding box directly, so we
    # approximate a small AOI (~5km) around the point — good enough for our
    # DEM/catchment fetches later. We'll refine this once we add DEM fetching.
    delta = 0.045  # roughly 5 km in degrees latitude
    bbox = {
        "south": lat - delta,
        "north": lat + delta,
        "west": lon - delta,
        "east": lon + delta,
    }

    display_parts = [place.get("name")]
    if place.get("admin1"):
        display_parts.append(place["admin1"])
    if place.get("country"):
        display_parts.append(place["country"])

    return {
        "name": ", ".join(p for p in display_parts if p),
        "lat": lat,
        "lon": lon,
        "bbox": bbox,
        "country": place.get("country"),
        "admin1": place.get("admin1"),
        "population": place.get("population"),
    }
