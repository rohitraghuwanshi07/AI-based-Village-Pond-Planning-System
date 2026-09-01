"""
Classifies nearby land as government/public, private, or unverified ownership,
using OpenStreetMap tags as a heuristic signal.

IMPORTANT, READ THIS: there is no free, authoritative, publicly-queryable
cadastral/land-ownership API for India (or most countries) that can be
looked up by coordinates. Actual land ownership records live in state-specific
government portals (e.g. Bhulekh, Bhu-Naksha in India) that require manual
survey-number lookups, not spatial API queries, and are not open for
anonymous automated access. This module does NOT verify legal ownership.

What it does instead: OpenStreetMap sometimes tags land with clues about who
operates/owns it -- a government office building, a protected forest, a
village common/panchayat land, a clearly private residential plot. We treat
ONLY explicit, plausible government/public tags as "government/public" --
everything else (including most vacant land, which is simply untagged in
OSM) is classified as "unverified", and by default (per the strict policy
this project uses) unverified land is EXCLUDED from any proposed pond area,
not assumed available. This matches a conservative, honest interpretation of
"if ownership cannot be verified, do not include that parcel."

Before any real construction, ownership MUST be confirmed through the actual
government land records for that state/district.
"""

import httpx
from shapely.geometry import LineString, Polygon

from app.services.overpass_client import query_overpass

# Keywords in OSM operator/owner tags that plausibly indicate government/public
# administration (case-insensitive substring match).
_GOVERNMENT_OPERATOR_KEYWORDS = (
    "government", "govt", "panchayat", "gram panchayat", "municipal", "municipality",
    "zila parishad", "forest department", "revenue department", "public works department",
    "pwd", "irrigation department", "state government", "central government",
    "district administration", "collectorate", "nagar nigam", "nagar palika",
)

# OSM tag values that plausibly indicate government/public land use, even
# without an explicit operator tag.
_GOVERNMENT_LANDUSE_VALUES = {"government", "military", "cemetery", "forest"}
_GOVERNMENT_AMENITY_VALUES = {
    "government_office", "townhall", "courthouse", "public_building",
    "police", "fire_station", "post_office",
}
_GOVERNMENT_BOUNDARY_VALUES = {"protected_area", "national_park"}
_GOVERNMENT_LEISURE_VALUES = {"common"}  # village common/grazing land, often panchayat-owned in India

# Tags that plausibly indicate PRIVATE ownership.
_PRIVATE_LANDUSE_VALUES = {"residential", "farmland", "farmyard", "orchard", "vineyard", "commercial", "industrial"}
_PRIVATE_BUILDING_INDICATORS = {"house", "residential", "apartments", "detached", "terrace"}


def _classify_tags(tags: dict) -> str:
    """Returns 'government', 'private', or 'unverified' based on OSM tags."""
    operator = (tags.get("operator") or "").lower()
    owner = (tags.get("owner") or "").lower()
    combined_owner_text = f"{operator} {owner}"

    if any(kw in combined_owner_text for kw in _GOVERNMENT_OPERATOR_KEYWORDS):
        return "government"

    if tags.get("landuse") in _GOVERNMENT_LANDUSE_VALUES:
        return "government"
    if tags.get("amenity") in _GOVERNMENT_AMENITY_VALUES:
        return "government"
    if tags.get("boundary") in _GOVERNMENT_BOUNDARY_VALUES:
        return "government"
    if tags.get("leisure") in _GOVERNMENT_LEISURE_VALUES:
        return "government"

    if any(kw in combined_owner_text for kw in ("private", "individual")):
        return "private"
    if tags.get("landuse") in _PRIVATE_LANDUSE_VALUES:
        return "private"
    if tags.get("building") in _PRIVATE_BUILDING_INDICATORS:
        return "private"
    if tags.get("private") == "yes":
        return "private"

    return "unverified"


async def fetch_ownership_zones(lat: float, lon: float, radius_m: float = 150.0) -> dict:
    """
    Query OpenStreetMap for tagged land parcels/areas near (lat, lon) and
    classify each as government, private, or unverified using the heuristic
    above.

    Returns:
        {
          "government_zones": [shapely Polygon, ...],
          "private_zones": [shapely Polygon, ...],
          "query_succeeded": bool,
          "error": str or None,
        }
    Untagged land is NOT returned in either list -- it's implicitly
    "unverified" by omission, which callers should treat as excluded.
    """
    query = f"""
    [out:json][timeout:25];
    (
      way["landuse"](around:{radius_m},{lat},{lon});
      way["amenity"](around:{radius_m},{lat},{lon});
      way["boundary"](around:{radius_m},{lat},{lon});
      way["leisure"](around:{radius_m},{lat},{lon});
      way["building"](around:{radius_m},{lat},{lon});
      relation["landuse"](around:{radius_m},{lat},{lon});
      relation["boundary"](around:{radius_m},{lat},{lon});
    );
    out body;
    >;
    out skel qt;
    """

    try:
        data = await query_overpass(query)
    except Exception as e:
        return {"government_zones": [], "private_zones": [], "query_succeeded": False, "error": str(e)}

    nodes = {}
    ways = []
    for el in data.get("elements", []):
        if el["type"] == "node":
            nodes[el["id"]] = (el["lon"], el["lat"])
        elif el["type"] == "way":
            ways.append(el)

    government_zones, private_zones = [], []
    for way in ways:
        coords = [nodes[n] for n in way.get("nodes", []) if n in nodes]
        if len(coords) < 3:
            continue
        tags = way.get("tags", {})
        classification = _classify_tags(tags)
        if classification == "unverified":
            continue
        try:
            poly = Polygon(coords)
            if not poly.is_valid or poly.area == 0:
                continue
        except Exception:
            continue

        if classification == "government":
            government_zones.append(poly)
        elif classification == "private":
            private_zones.append(poly)

    return {
        "government_zones": government_zones,
        "private_zones": private_zones,
        "query_succeeded": True,
        "error": None,
    }
