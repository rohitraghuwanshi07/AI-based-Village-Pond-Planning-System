"""
Fetches real buildings and roads near a candidate pond site from OpenStreetMap's
free Overpass API, so we can check whether a proposed pond footprint would
actually collide with existing structures -- instead of blindly assuming a
fixed amount of open land exists, which was the root cause of pond footprints
being recommended right on top of real buildings/roads.

Docs: https://wiki.openstreetmap.org/wiki/Overpass_API
Public endpoint, free, no API key required.
"""

import httpx
from shapely.geometry import LineString, Polygon

OVERPASS_URL = "https://overpass-api.de/api/interpreter"


async def fetch_obstructions(lat: float, lon: float, radius_m: float = 150.0) -> dict:
    """
    Query OpenStreetMap for buildings and roads within radius_m of (lat, lon).

    Returns:
        {
          "buildings": [shapely Polygon, ...],
          "roads": [shapely LineString, ...],
          "query_succeeded": bool,
          "error": str or None,
        }
    On any network/parsing failure, returns an empty result with
    query_succeeded=False rather than raising -- callers should treat this as
    "obstruction data unavailable" and fall back to a conservative default,
    not silently pretend the land is empty.
    """
    query = f"""
    [out:json][timeout:20];
    (
      way["building"](around:{radius_m},{lat},{lon});
      way["highway"](around:{radius_m},{lat},{lon});
    );
    out body;
    >;
    out skel qt;
    """

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(OVERPASS_URL, data={"data": query})
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return {"buildings": [], "roads": [], "query_succeeded": False, "error": str(e)}

    nodes = {}
    ways = []
    for el in data.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el["type"] == "way":
            ways.append(el)

    buildings, roads = [], []
    for way in ways:
        coords = [nodes[n] for n in way.get("nodes", []) if n in nodes]
        if len(coords) < 2:
            continue
        tags = way.get("tags", {})
        if "building" in tags:
            if len(coords) >= 3:
                try:
                    buildings.append(Polygon(coords))
                except Exception:
                    pass
        elif "highway" in tags:
            try:
                roads.append(LineString(coords))
            except Exception:
                pass

    return {"buildings": buildings, "roads": roads, "query_succeeded": True, "error": None}
