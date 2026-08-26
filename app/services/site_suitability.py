"""
Finds genuinely usable (vacant, contiguous) land near a candidate pond site
by rasterizing real obstacles -- buildings, roads, water bodies -- onto a
fine grid and identifying connected components of open space.

This replaces a cruder earlier approach that only computed a single "total
leftover area" number (search circle minus a blob of buffered obstacles).
That approach could report a large "available area" that was actually
several small, disconnected slivers split apart by a road running through
the middle -- which is exactly the kind of unusable, senseless result this
module is built to avoid. A pond needs one contiguous patch of land, not a
sum of scattered fragments separated by a highway.

Method:
    1. Rasterize the search area into a fine grid (default 5m cells).
    2. Burn buildings (+ a safety setback), roads (+ their approximate
       right-of-way width), and water bodies (+ a small buffer) onto the
       grid as "occupied" cells.
    3. Find connected components of the remaining "open" cells (8-connectivity)
       -- this is what correctly SEPARATES two open patches that are cut off
       from each other by a road, instead of treating them as one blob.
    4. Compute each patch's real area and trace its boundary as a polygon.
    5. Select the patch that actually contains (or is nearest to) the
       candidate site -- since that's the patch a pond there could use.
"""

import math

import numpy as np
from affine import Affine
from rasterio.features import rasterize
from scipy import ndimage
from shapely.geometry import Point
from skimage import measure

ROAD_BUFFER_M = 6.0  # approximate half-width + shoulder margin for a typical rural road
BUILDING_SETBACK_M = 3.0  # safety margin around buildings, not just their exact footprint
WATER_BUFFER_M = 4.0  # small margin around water body edges (bank stability, seasonal extent)

MIN_PATCH_AREA_M2 = 100.0  # ignore tiny slivers/noise patches below this size


def _meters_to_deg(lat: float):
    lon_deg_per_m = 1.0 / (111_320.0 * math.cos(math.radians(lat)))
    lat_deg_per_m = 1.0 / 111_320.0
    return lon_deg_per_m, lat_deg_per_m


def find_vacant_patches(
    lat: float,
    lon: float,
    buildings: list,
    roads: list,
    water: list,
    search_radius_m: float = 150.0,
    cell_size_m: float = 5.0,
):
    """
    Rasterize the search area and find contiguous vacant land patches.

    Returns a dict:
        {
          "patches": [
              {"area_m2": ..., "centroid": {"lat":.., "lon":..},
               "boundary_geojson": {...}, "contains_query_point": bool},
              ...  # sorted largest first
          ],
          "selected_patch_index": int or None,  # which patch the query point falls in / is nearest to
          "total_vacant_area_m2": ...,           # sum across ALL patches (for reference only --
                                                   # NOT what should be used for a single pond,
                                                   # since patches are disconnected)
          "grid_resolution_m": cell_size_m,
          "search_radius_m": search_radius_m,
        }
    """
    lon_deg_per_m, lat_deg_per_m = _meters_to_deg(lat)
    radius_deg = search_radius_m * min(lon_deg_per_m, lat_deg_per_m)

    west = lon - search_radius_m * lon_deg_per_m
    east = lon + search_radius_m * lon_deg_per_m
    south = lat - search_radius_m * lat_deg_per_m
    north = lat + search_radius_m * lat_deg_per_m

    px_deg = cell_size_m * lon_deg_per_m
    py_deg = cell_size_m * lat_deg_per_m
    cols = max(4, int((east - west) / px_deg))
    rows = max(4, int((north - south) / py_deg))
    transform = Affine(px_deg, 0.0, west, 0.0, -py_deg, north)

    # --- Burn obstacles onto the grid ---
    shapes = []
    for b in buildings:
        try:
            shapes.append((b.buffer(BUILDING_SETBACK_M * lon_deg_per_m), 1))
        except Exception:
            continue
    for r in roads:
        try:
            shapes.append((r.buffer(ROAD_BUFFER_M * lon_deg_per_m), 1))
        except Exception:
            continue
    for w in water:
        try:
            shapes.append((w.buffer(WATER_BUFFER_M * lon_deg_per_m), 1))
        except Exception:
            continue

    if shapes:
        obstacle_mask = rasterize(
            shapes, out_shape=(rows, cols), transform=transform,
            fill=0, default_value=1, dtype="uint8",
        ).astype(bool)
    else:
        obstacle_mask = np.zeros((rows, cols), dtype=bool)

    # --- Restrict to the search circle (cells outside it don't count) ---
    row_idx, col_idx = np.indices((rows, cols))
    cell_lon = west + (col_idx + 0.5) * px_deg
    cell_lat = north - (row_idx + 0.5) * py_deg
    dist_m = np.sqrt(((cell_lon - lon) / lon_deg_per_m) ** 2 + ((cell_lat - lat) / lat_deg_per_m) ** 2)
    within_circle = dist_m <= search_radius_m

    free_mask = (~obstacle_mask) & within_circle

    # --- Connected components of open space (8-connectivity: two cells touching
    # even diagonally count as the same patch; this is the standard choice for
    # this kind of "which open cells actually connect" analysis) ---
    structure = np.ones((3, 3), dtype=int)
    labeled, num_patches = ndimage.label(free_mask, structure=structure)

    cell_area_m2 = cell_size_m * cell_size_m
    query_row, query_col = int((north - lat) / py_deg), int((lon - west) / px_deg)
    query_row = min(max(query_row, 0), rows - 1)
    query_col = min(max(query_col, 0), cols - 1)
    query_label = labeled[query_row, query_col] if free_mask[query_row, query_col] else None

    patches = []
    for label_id in range(1, num_patches + 1):
        patch_mask = labeled == label_id
        cell_count = int(patch_mask.sum())
        area_m2 = cell_count * cell_area_m2
        if area_m2 < MIN_PATCH_AREA_M2:
            continue

        rows_idx, cols_idx = np.where(patch_mask)
        centroid_row, centroid_col = rows_idx.mean(), cols_idx.mean()
        centroid_lon, centroid_lat = transform * (centroid_col, centroid_row)

        boundary_paths = measure.find_contours(patch_mask.astype(float), level=0.5)
        polygons = []
        for path in boundary_paths:
            coords = [transform * (c, r) for r, c in path]
            if len(coords) >= 4:
                polygons.append([[lon_, lat_] for lon_, lat_ in coords])

        patches.append({
            "label_id": int(label_id),
            "area_m2": round(area_m2, 1),
            "centroid": {"lat": centroid_lat, "lon": centroid_lon},
            "boundary_geojson": {
                "type": "FeatureCollection",
                "features": [
                    {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [poly]}}
                    for poly in polygons
                ],
            },
            "contains_query_point": (query_label is not None and label_id == query_label),
        })

    patches.sort(key=lambda p: p["area_m2"], reverse=True)

    selected_index = None
    for i, p in enumerate(patches):
        if p["contains_query_point"]:
            selected_index = i
            break
    if selected_index is None and patches:
        # Query point itself is occupied (e.g. clicked on a building) -- fall
        # back to the nearest patch by centroid distance, since that's the
        # most usable real land close to where the user wanted the pond.
        def dist_to_query(p):
            dlat = (p["centroid"]["lat"] - lat) / lat_deg_per_m
            dlon = (p["centroid"]["lon"] - lon) / lon_deg_per_m
            return math.hypot(dlat, dlon)
        selected_index = min(range(len(patches)), key=lambda i: dist_to_query(patches[i]))

    return {
        "patches": patches,
        "selected_patch_index": selected_index,
        "total_vacant_area_m2": round(sum(p["area_m2"] for p in patches), 1),
        "grid_resolution_m": cell_size_m,
        "search_radius_m": search_radius_m,
        "buildings_found_nearby": len(buildings),
        "roads_found_nearby": len(roads),
        "water_bodies_found_nearby": len(water),
    }
