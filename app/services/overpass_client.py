"""
Shared helper for querying Overpass API, with fallback across multiple free
public mirror servers. We've now seen two separate real-world cases in this
project where a specific free geodata host rejected requests for a particular
network/environment (Nominatim earlier, and overpass-api.de intermittently) --
rather than assume one single endpoint will always be reachable, we try a
short list of known public Overpass mirrors in order and use the first one
that responds successfully.

All of these are free, community-run public Overpass instances -- no API key
for any of them.
"""

import httpx

# Tried in order; first success wins. All serve the same Overpass QL query language.
OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]

HEADERS = {
    "User-Agent": "village-pond-planner-student-project (contact: student@example.com)",
    "Accept": "application/json",
}


async def query_overpass(query: str, timeout: float = 30.0) -> dict:
    """
    POST an Overpass QL query, trying each mirror in order until one succeeds.

    Returns the parsed JSON response dict.
    Raises the last encountered exception if every mirror fails, with a
    message listing which mirrors were tried -- so a caller/log clearly shows
    this wasn't a single-endpoint fluke.
    """
    last_error = None
    errors_by_mirror = []

    for url in OVERPASS_MIRRORS:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # Let httpx set its own Content-Type for form data -- some
                # mirrors are stricter about header formatting than others,
                # and this is the most compatible/standard approach.
                resp = await client.post(url, data={"data": query}, headers=HEADERS)
                resp.raise_for_status()
                return resp.json()
        except Exception as e:
            errors_by_mirror.append(f"{url}: {e}")
            last_error = e
            continue

    raise RuntimeError(
        f"All Overpass mirrors failed. Tried {len(OVERPASS_MIRRORS)} servers: "
        f"{'; '.join(errors_by_mirror)}"
    ) from last_error
