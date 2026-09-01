"""
Fetches real buildings, roads, and water bodies near a candidate pond site
from OpenStreetMap's free Overpass API, so we can check whether a proposed
pond footprint would actually collide with existing structures or an
existing water body -- instead of blindly assuming a fixed amount of open
land exists.

Docs: https://wiki.openstreetmap.org/wiki/Overpass_API
Public endpoint, free, no API key required.
"""

import httpx
from shapely.geometry import LineString, Polygon

from app.services.overpass_client import query_overpass


async def fetch_obstructions(lat: float, lon: float, radius_m: float = 150.0) -> dict:
    """
    Query OpenStreetMap for buildings, roads, and water bodies within
    radius_m of (lat, lon).

    Returns:
        {
          "buildings": [shapely Polygon, ...],
          "roads": [shapely LineString, ...],
          "water": [shapely Polygon or LineString, ...],
          "query_succeeded": bool,
          "error": str or None,
        }
    On any network/parsing failure, returns an empty result with
    query_succeeded=False rather than raising -- callers should treat this as
    "obstruction data unavailable" and fall back to a conservative default,
    not silently pretend the land is empty.
    """
    query = f"""
    [out:json][timeout:25];
    (
      way["building"](around:{radius_m},{lat},{lon});
      way["highway"](around:{radius_m},{lat},{lon});
      way["natural"="water"](around:{radius_m},{lat},{lon});
      way["landuse"="reservoir"](around:{radius_m},{lat},{lon});
      way["waterway"](around:{radius_m},{lat},{lon});
      relation["natural"="water"](around:{radius_m},{lat},{lon});
    );
    out body;
    >;
    out skel qt;
    """

    try:
        data = await query_overpass(query)
    except Exception as e:
        return {"buildings": [], "roads": [], "water": [], "query_succeeded": False, "error": str(e)}

    nodes = {}
    ways = []
    for el in data.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el["type"] == "way":
            ways.append(el)

    buildings, roads, water = [], [], []
    for way in ways:
        coords = [nodes[n] for n in way.get("nodes", []) if n in nodes]
        if len(coords) < 2:
            continue
        tags = way.get("tags", {})

        is_water = tags.get("natural") == "water" or tags.get("landuse") == "reservoir" or "waterway" in tags

        if "building" in tags:
            if len(coords) >= 3:
                try:
                    buildings.append(Polygon(coords))
                except Exception:
                    pass
        elif is_water:
            if len(coords) >= 3 and coords[0] == coords[-1]:
                try:
                    water.append(Polygon(coords))
                    continue
                except Exception:
                    pass
            try:
                water.append(LineString(coords))
            except Exception:
                pass
        elif "highway" in tags:
            try:
                roads.append(LineString(coords))
            except Exception:
                pass

    return {"buildings": buildings, "roads": roads, "water": water, "query_succeeded": True, "error": None}
