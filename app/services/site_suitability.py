"""
Given a candidate pond location and nearby real-world obstructions (buildings,
roads), computes how much open land is actually available -- so pond sizing
is constrained by reality, not a guessed constant.

Method: build a search circle around the site, buffer roads to approximate
their right-of-way width, union all obstructions, and subtract that from the
search circle. What's left is "genuinely open land" within the search radius.
This is a conservative, explainable estimate -- not a precise "find the
largest empty rectangle" solver (a harder computational geometry problem),
but it directly fixes the actual bug: a hardcoded site-area assumption that
ignored real buildings/roads entirely.
"""

import math

from shapely.geometry import Point
from shapely.ops import unary_union

ROAD_BUFFER_M = 6.0  # approximate half-width + shoulder margin for a typical rural road
BUILDING_SETBACK_M = 3.0  # small safety margin around buildings, not just their exact footprint


def _meters_to_degrees(meters: float, lat: float):
    """Rough local conversion, adequate for a small search radius (<1km)."""
    lat_deg = meters / 111_320.0
    lon_deg = meters / (111_320.0 * math.cos(math.radians(lat)))
    return lon_deg, lat_deg


def compute_available_site_area(
    lat: float,
    lon: float,
    buildings: list,
    roads: list,
    search_radius_m: float = 150.0,
) -> dict:
    """
    Compute how much open (non-building, non-road) land exists within
    search_radius_m of (lat, lon).

    Returns a dict with the available area in m2, plus counts of nearby
    obstructions for transparency in the API response.
    """
    lon_deg_per_m, lat_deg_per_m = _meters_to_degrees(1.0, lat)
    radius_deg_lon = search_radius_m * lon_deg_per_m
    radius_deg_lat = search_radius_m * lat_deg_per_m
    # Use the smaller of the two as a conservative circular radius in degrees
    # (approximating a circle on a locally-flattened patch of the ellipsoid)
    radius_deg = min(radius_deg_lon, radius_deg_lat)

    search_circle = Point(lon, lat).buffer(radius_deg)

    obstruction_geoms = []
    for b in buildings:
        try:
            obstruction_geoms.append(b.buffer(BUILDING_SETBACK_M * lon_deg_per_m))
        except Exception:
            continue
    for r in roads:
        try:
            obstruction_geoms.append(r.buffer(ROAD_BUFFER_M * lon_deg_per_m))
        except Exception:
            continue

    if obstruction_geoms:
        obstruction_union = unary_union(obstruction_geoms)
        free_land = search_circle.difference(obstruction_union)
    else:
        free_land = search_circle

    # Convert the free-land area from square-degrees to square-meters using
    # our local meters-per-degree scale factors.
    deg2_to_m2 = (1 / lon_deg_per_m) * (1 / lat_deg_per_m)
    free_area_m2 = free_land.area * deg2_to_m2
    search_circle_area_m2 = search_circle.area * deg2_to_m2

    return {
        "available_area_m2": round(free_area_m2, 1),
        "search_circle_area_m2": round(search_circle_area_m2, 1),
        "search_radius_m": search_radius_m,
        "buildings_found_nearby": len(buildings),
        "roads_found_nearby": len(roads),
        "percent_of_search_area_open": round(100 * free_area_m2 / search_circle_area_m2, 1) if search_circle_area_m2 > 0 else 0.0,
    }
