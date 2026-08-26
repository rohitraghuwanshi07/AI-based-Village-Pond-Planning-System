"""
Geocoding client. Uses Photon (https://photon.komoot.io) as the primary
geocoder, with Open-Meteo's geocoding API as a fallback.

Why Photon: Open-Meteo's geocoder only indexes populated places (cities,
towns, villages) -- searching a specific landmark or institution (e.g.
"IIT Bhilai") returns nothing useful, or silently falls back to the nearest
city. Photon is built on OpenStreetMap data and indexes named points of
interest -- universities, institutes, hospitals, specific buildings -- not
just administrative places, so a search like "IIT Bhilai" resolves to the
actual campus, not just "Bhilai" the city.

Photon is free, no API key, and (unlike raw Nominatim) has not shown the
aggressive IP-based blocking we hit earlier in this project.
"""

import httpx

PHOTON_URL = "https://photon.komoot.io/api/"
OPENMETEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"

HEADERS = {"User-Agent": "village-pond-planner-student-project"}


async def _geocode_photon(query: str, country_code: str | None) -> dict | None:
    params = {"q": query, "limit": 5}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(PHOTON_URL, params=params, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()

    features = data.get("features", [])
    if not features:
        return None

    # Prefer a result matching the requested country, if we got several
    if country_code:
        matching = [f for f in features if (f["properties"].get("countrycode") or "").upper() == country_code.upper()]
        if matching:
            features = matching

    feat = features[0]
    props = feat["properties"]
    lon, lat = feat["geometry"]["coordinates"]

    # Photon sometimes gives an "extent" bounding box for larger places
    # (administrative areas); for a specific POI/landmark, build a small
    # ~1.5km box around the point instead, since a whole-city box would be
    # too coarse for a single-building/campus-scale search.
    extent = props.get("extent")
    if extent and len(extent) == 4:
        west, north, east, south = extent
    else:
        delta = 0.0135  # roughly 1.5 km
        south, north = lat - delta, lat + delta
        west, east = lon - delta, lon + delta

    name_parts = [props.get("name")]
    for key in ("street", "city", "state", "country"):
        if props.get(key) and props.get(key) not in name_parts:
            name_parts.append(props[key])

    return {
        "name": ", ".join(p for p in name_parts if p),
        "lat": lat,
        "lon": lon,
        "bbox": {"south": south, "north": north, "west": west, "east": east},
        "place_type": props.get("osm_value") or props.get("type"),
        "source": "photon",
    }


async def _geocode_openmeteo(query: str, country_code: str | None) -> dict | None:
    params = {"name": query, "count": 5, "language": "en", "format": "json"}
    if country_code:
        params["countryCode"] = country_code

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(OPENMETEO_GEOCODING_URL, params=params, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results")
    if not results:
        return None

    place = results[0]
    lat = float(place["latitude"])
    lon = float(place["longitude"])
    delta = 0.045
    bbox = {"south": lat - delta, "north": lat + delta, "west": lon - delta, "east": lon + delta}

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
        "place_type": "populated_place",
        "source": "open-meteo",
    }


async def geocode_village(query: str, country_codes: str | None = "IN") -> dict | None:
    """
    Look up a place name -- village, city, or specific landmark/institution --
    and return its location. Tries Photon first (better landmark coverage),
    falls back to Open-Meteo if Photon finds nothing or is unreachable.
    """
    try:
        result = await _geocode_photon(query, country_codes)
        if result:
            return result
    except Exception:
        pass  # fall through to backup geocoder

    return await _geocode_openmeteo(query, country_codes)
