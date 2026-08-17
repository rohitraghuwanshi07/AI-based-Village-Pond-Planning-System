"""
DEM (Digital Elevation Model) client using OpenTopography's free Global DEM API.

Fetches a GeoTIFF elevation raster for a bounding box and caches it to disk
so we don't re-download the same area on every request.

Docs: https://portal.opentopography.org/apidocs/#/Public/getGlobalDem
"""

import hashlib
from pathlib import Path

import httpx

from app.config import settings

OPENTOPO_URL = "https://portal.opentopography.org/API/globaldem"

# Where cached DEM GeoTIFFs are stored (relative to backend/ working dir)
CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "dem_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_key(south: float, north: float, west: float, east: float, demtype: str) -> str:
    raw = f"{demtype}_{south:.4f}_{north:.4f}_{west:.4f}_{east:.4f}"
    return hashlib.md5(raw.encode()).hexdigest()


async def fetch_dem(
    south: float,
    north: float,
    west: float,
    east: float,
    demtype: str = "SRTMGL1",
) -> Path:
    """
    Fetch (or return cached) elevation GeoTIFF for a bounding box.

    Args:
        south, north, west, east: bounding box in decimal degrees
        demtype: "SRTMGL1" (30m resolution) is the default free global option.
                 Other options include "SRTMGL3" (90m) if you want smaller files.

    Returns:
        Path to the cached .tif file on disk.
    """
    if not settings.opentopography_api_key:
        raise RuntimeError(
            "OPENTOPOGRAPHY_API_KEY is not set. Create backend/.env from "
            ".env.example and add your free key from https://portal.opentopography.org/myopentopo"
        )

    key = _cache_key(south, north, west, east, demtype)
    cached_path = CACHE_DIR / f"{key}.tif"

    if cached_path.exists():
        return cached_path

    params = {
        "demtype": demtype,
        "south": south,
        "north": north,
        "west": west,
        "east": east,
        "outputFormat": "GTiff",
        "API_Key": settings.opentopography_api_key,
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(OPENTOPO_URL, params=params)
        resp.raise_for_status()
        cached_path.write_bytes(resp.content)

    return cached_path
