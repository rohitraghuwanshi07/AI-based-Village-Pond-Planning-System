"""
Fetches soil composition (sand/silt/clay percentages) at a point from
ISRIC SoilGrids -- a free, global, no-API-key soil property dataset.

Why this matters for pond siting: sandy soil has high seepage (water leaks
out through it), so a pond built there loses water fast unless it's lined
(added cost). Clayey soil is naturally more watertight, better for an
unlined earthen pond. This is a real, standard consideration in real pond/
tank siting, not just a cosmetic detail -- it directly affects how much of
the estimated storage capacity is actually usable in practice.

Docs: https://rest.isric.org/soilgrids/v2.0/docs
"""

import httpx

SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"


def _classify_seepage_risk(sand_pct: float, clay_pct: float) -> str:
    """
    Rough, standard-textbook classification: seepage risk rises with sand
    content and falls with clay content. This is a simplification (real
    permeability depends on more than texture percentages -- compaction,
    layering, organic content), but it's a defensible first-pass screening
    signal, and we say so explicitly in the output rather than overstating
    precision we don't have.
    """
    if sand_pct >= 70 and clay_pct < 10:
        return "high"
    if clay_pct >= 30:
        return "low"
    return "moderate"


async def fetch_soil_composition(lat: float, lon: float) -> dict:
    """
    Query SoilGrids for sand/silt/clay percentage at 0-5cm depth (topsoil,
    most relevant for pond bed seepage).

    Returns a dict with the percentages, a plain-language seepage risk
    classification, and query_succeeded/error for transparent failure
    handling (same pattern as our other external-API clients in this
    project) -- never silently pretends to know soil data it doesn't have.
    """
    params = {
        "lon": lon,
        "lat": lat,
        "property": ["sand", "silt", "clay"],
        "depth": "0-5cm",
        "value": "mean",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(SOILGRIDS_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:
        return {
            "query_succeeded": False,
            "error": str(e),
            "note": "Soil data unavailable -- recommend a manual soil test before construction.",
        }

    try:
        layers = data["properties"]["layers"]
        values = {}
        for layer in layers:
            name = layer["name"]  # "sand", "silt", or "clay"
            # SoilGrids returns values in g/kg * 10 (permille), convert to percent
            raw = layer["depths"][0]["values"]["mean"]
            values[name] = round(raw / 10, 1) if raw is not None else None
    except (KeyError, IndexError, TypeError) as e:
        return {
            "query_succeeded": False,
            "error": f"Unexpected SoilGrids response format: {e}",
            "note": "Soil data unavailable -- recommend a manual soil test before construction.",
        }

    sand = values.get("sand")
    clay = values.get("clay")
    seepage_risk = _classify_seepage_risk(sand, clay) if sand is not None and clay is not None else "unknown"

    return {
        "query_succeeded": True,
        "sand_pct": sand,
        "silt_pct": values.get("silt"),
        "clay_pct": clay,
        "seepage_risk": seepage_risk,
        "note": (
            "Estimated from ISRIC SoilGrids (global 250m resolution soil map). "
            "Treat as a screening indicator, not a substitute for an on-site soil test."
        ),
    }
