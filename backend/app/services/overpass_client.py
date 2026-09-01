"""
Shared helper for querying Overpass API, with fallback across multiple free
public mirror servers, AND a local disk cache.

Why the cache matters: public Overpass instances enforce strict per-IP rate
limits (often just ~2 concurrent requests). Repeated testing during
development -- clicking the same or nearby locations many times in a short
period -- can trip these limits, which shows up as inconsistent failures
(connection resets, 500s, 403s, 406s) that vary between attempts and between
mirrors. Caching successful responses means we only hit the live API once
per distinct query, which both avoids re-triggering rate limits and makes
repeat testing/demos much faster.

We've now seen two separate real-world cases in this project where a
specific free geodata host rejected requests for a particular network/
environment (Nominatim earlier, and overpass-api.de intermittently) --
rather than assume one single endpoint will always be reachable, we try a
short list of known public Overpass mirrors in order and use the first one
that responds successfully.

All of these are free, community-run public Overpass instances -- no API key
for any of them.
"""

import hashlib
import json
import time
from pathlib import Path

import httpx

# Tried in order; first success wins. All serve the same Overpass QL query language.
# Note: overpass.openstreetmap.fr was removed from this list -- direct testing
# showed it explicitly rejects all requests with "This service is only
# available to white-listed usages" (a 403 with that exact message), meaning
# it requires prior approval and will never work for anonymous public use.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

HEADERS = {
    "User-Agent": "village-pond-planner-student-project (contact: student@example.com)",
    # Deliberately NOT setting an explicit Accept header. A literal
    # "406 Not Acceptable" response means the server can't produce a
    # representation matching our Accept value -- Overpass's JSON output is
    # controlled by [out:json] inside the query itself, not content
    # negotiation, so asserting "Accept: application/json" can cause exactly
    # this failure on servers that don't special-case it. Omitting it lets
    # httpx send the default "*/*", which every server accepts.
}

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "overpass_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE_TTL_SECONDS = 3600  # 1 hour -- long enough to cover repeat testing of the same area


def _cache_path_for(query: str) -> Path:
    key = hashlib.md5(query.encode()).hexdigest()
    return CACHE_DIR / f"{key}.json"


def _read_cache(query: str) -> dict | None:
    path = _cache_path_for(query)
    if not path.exists():
        return None
    try:
        age = time.time() - path.stat().st_mtime
        if age > CACHE_TTL_SECONDS:
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _write_cache(query: str, data: dict) -> None:
    path = _cache_path_for(query)
    try:
        with open(path, "w") as f:
            json.dump(data, f)
    except Exception:
        pass  # caching is a best-effort optimization, never fail the request over it


async def query_overpass(query: str, timeout: float = 30.0, retries_per_mirror: int = 2) -> dict:
    """
    POST an Overpass QL query, trying each mirror in order until one succeeds,
    with a short retry per mirror and a local disk cache to avoid repeat
    live calls for the same query within CACHE_TTL_SECONDS.

    Returns the parsed JSON response dict.
    Raises the last encountered exception if every mirror fails, with a
    message listing which mirrors were tried -- so a caller/log clearly shows
    this wasn't a single-endpoint fluke.
    """
    cached = _read_cache(query)
    if cached is not None:
        return cached

    last_error = None
    errors_by_mirror = []

    for url in OVERPASS_MIRRORS:
        for attempt in range(retries_per_mirror):
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, data={"data": query}, headers=HEADERS)
                    resp.raise_for_status()
                    data = resp.json()
                    _write_cache(query, data)
                    return data
            except Exception as e:
                last_error = e
                if attempt < retries_per_mirror - 1:
                    continue  # brief retry against the SAME mirror once before moving on
                errors_by_mirror.append(f"{url}: {e}")

    raise RuntimeError(
        f"All Overpass mirrors failed. Tried {len(OVERPASS_MIRRORS)} servers "
        f"({retries_per_mirror} attempts each): {'; '.join(errors_by_mirror)}"
    ) from last_error
