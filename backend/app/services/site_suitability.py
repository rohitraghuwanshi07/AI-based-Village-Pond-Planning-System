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


def _polygon_boundaries_from_mask(mask: np.ndarray, transform: Affine) -> dict:
    """Trace boundary polygons for all True regions in a boolean mask, as GeoJSON."""
    boundary_paths = measure.find_contours(mask.astype(float), level=0.5)
    polygons = []
    for path in boundary_paths:
        coords = [transform * (c, r) for r, c in path]
        if len(coords) >= 4:
            polygons.append([[lon_, lat_] for lon_, lat_ in coords])
    return {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [poly]}}
            for poly in polygons
        ],
    }


def find_eligible_patches(
    lat: float,
    lon: float,
    buildings: list,
    roads: list,
    water: list,
    government_zones: list,
    private_zones: list,
    search_radius_m: float = 150.0,
    cell_size_m: float = 5.0,
):
    """
    Strict eligibility pipeline: a cell is only eligible for pond siting if it
    is (a) within an explicitly OSM-tagged government/public zone, AND
    (b) not occupied by a building, road, or water body, AND (c) not tagged
    private (defensive double-check, since government/private zones should
    already be disjoint in practice).

    IMPORTANT LIMITATION (see ownership_client.py docstring for the full
    explanation): there is no free, authoritative cadastral ownership API.
    "government_zones" here come from a heuristic OSM-tag classification,
    NOT verified legal records. Untagged land is treated as ownership
    UNVERIFIED and is EXCLUDED by default, per a strict "don't include what
    you can't verify" policy -- this commonly means most of a search area
    will be excluded, since explicit government tagging is sparse in OSM,
    especially in rural areas. This is intentional, honest behavior, not a
    bug -- confirm real ownership through actual state land records
    (e.g. Bhulekh/Bhu-Naksha in India) before construction.

    Returns a dict with:
        - eligible patches (same shape as find_vacant_patches' "patches")
        - a full area breakdown (government/private/unverified/obstacles)
        - boundary polygons for every layer, for map display
    """
    lon_deg_per_m, lat_deg_per_m = _meters_to_deg(lat)

    west = lon - search_radius_m * lon_deg_per_m
    east = lon + search_radius_m * lon_deg_per_m
    south = lat - search_radius_m * lat_deg_per_m
    north = lat + search_radius_m * lat_deg_per_m

    px_deg = cell_size_m * lon_deg_per_m
    py_deg = cell_size_m * lat_deg_per_m
    cols = max(4, int((east - west) / px_deg))
    rows = max(4, int((north - south) / py_deg))
    transform = Affine(px_deg, 0.0, west, 0.0, -py_deg, north)
    cell_area_m2 = cell_size_m * cell_size_m

    def rasterize_polys(polys, buffer_m=0.0):
        shapes = []
        for p in polys:
            try:
                geom = p.buffer(buffer_m * lon_deg_per_m) if buffer_m else p
                shapes.append((geom, 1))
            except Exception:
                continue
        if not shapes:
            return np.zeros((rows, cols), dtype=bool)
        return rasterize(
            shapes, out_shape=(rows, cols), transform=transform,
            fill=0, default_value=1, dtype="uint8",
        ).astype(bool)

    building_mask = rasterize_polys(buildings, BUILDING_SETBACK_M)
    road_mask = rasterize_polys(roads, ROAD_BUFFER_M)
    water_mask = rasterize_polys(water, WATER_BUFFER_M)
    obstacle_mask = building_mask | road_mask | water_mask

    government_mask = rasterize_polys(government_zones, 0.0)
    private_mask = rasterize_polys(private_zones, 0.0)

    row_idx, col_idx = np.indices((rows, cols))
    cell_lon = west + (col_idx + 0.5) * px_deg
    cell_lat = north - (row_idx + 0.5) * py_deg
    dist_m = np.sqrt(((cell_lon - lon) / lon_deg_per_m) ** 2 + ((cell_lat - lat) / lat_deg_per_m) ** 2)
    within_circle = dist_m <= search_radius_m

    eligible_mask = within_circle & government_mask & (~obstacle_mask) & (~private_mask)
    unverified_mask = within_circle & (~obstacle_mask) & (~government_mask) & (~private_mask)
    private_free_mask = within_circle & private_mask & (~obstacle_mask)

    structure = np.ones((3, 3), dtype=int)
    labeled, num_patches = ndimage.label(eligible_mask, structure=structure)

    query_row = min(max(int((north - lat) / py_deg), 0), rows - 1)
    query_col = min(max(int((lon - west) / px_deg), 0), cols - 1)
    query_label = labeled[query_row, query_col] if eligible_mask[query_row, query_col] else None

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
        patches.append({
            "label_id": int(label_id),
            "area_m2": round(area_m2, 1),
            "centroid": {"lat": centroid_lat, "lon": centroid_lon},
            "boundary_geojson": _polygon_boundaries_from_mask(patch_mask, transform),
            "contains_query_point": (query_label is not None and label_id == query_label),
        })
    patches.sort(key=lambda p: p["area_m2"], reverse=True)

    selected_index = None
    for i, p in enumerate(patches):
        if p["contains_query_point"]:
            selected_index = i
            break
    if selected_index is None and patches:
        def dist_to_query(p):
            dlat = (p["centroid"]["lat"] - lat) / lat_deg_per_m
            dlon = (p["centroid"]["lon"] - lon) / lon_deg_per_m
            return math.hypot(dlat, dlon)
        selected_index = min(range(len(patches)), key=lambda i: dist_to_query(patches[i]))

    search_circle_area_m2 = within_circle.sum() * cell_area_m2
    area_breakdown = {
        "search_circle_area_m2": round(search_circle_area_m2, 1),
        "government_owned_area_m2": round((within_circle & government_mask).sum() * cell_area_m2, 1),
        "government_area_occupied_by_development_m2": round((within_circle & government_mask & obstacle_mask).sum() * cell_area_m2, 1),
        "private_area_m2": round((within_circle & private_mask).sum() * cell_area_m2, 1),
        "unverified_ownership_area_m2": round(unverified_mask.sum() * cell_area_m2, 1),
        "buildings_roads_water_area_m2": round((within_circle & obstacle_mask).sum() * cell_area_m2, 1),
        "final_eligible_vacant_government_area_m2": round(eligible_mask.sum() * cell_area_m2, 1),
    }

    layer_boundaries = {
        "government_owned": _polygon_boundaries_from_mask(within_circle & government_mask, transform),
        "private": _polygon_boundaries_from_mask(within_circle & private_mask, transform),
        "unverified_ownership": _polygon_boundaries_from_mask(unverified_mask, transform),
        "buildings_roads_water": _polygon_boundaries_from_mask(within_circle & obstacle_mask, transform),
        "final_eligible": _polygon_boundaries_from_mask(eligible_mask, transform),
    }

    return {
        "patches": patches,
        "selected_patch_index": selected_index,
        "area_breakdown": area_breakdown,
        "layer_boundaries": layer_boundaries,
        "grid_resolution_m": cell_size_m,
        "search_radius_m": search_radius_m,
        "buildings_found_nearby": len(buildings),
        "roads_found_nearby": len(roads),
        "water_bodies_found_nearby": len(water),
        "government_zones_found_nearby": len(government_zones),
        "private_zones_found_nearby": len(private_zones),
        "ownership_data_limitation": (
            "No free authoritative cadastral/land-ownership API exists for automated "
            "verification. Government-zone classification is a heuristic based on "
            "OpenStreetMap tags (government offices, protected areas, village common "
            "land, etc.) and is NOT legal proof of ownership. Untagged land is treated "
            "as ownership UNVERIFIED and excluded from the eligible area by default. "
            "Confirm actual ownership through official state land records (e.g. "
            "Bhulekh/Bhu-Naksha) before any construction."
        ),
    }


def find_eligible_patches_from_parcels(
    west: float, south: float, east: float, north: float,
    government_polys: list,
    private_polys: list,
    obstacle_polys: list,  # buildings, roads, water -- anything that occupies but isn't a government/private classification per se
    cell_size_m: float = 10.0,
):
    """
    Same eligibility pipeline as find_eligible_patches, but driven by an
    ACTUAL uploaded land record (e.g. a Bhu-Naksha-style parcel map) rather
    than an OSM-tag heuristic, and covering an explicit bounding box (the
    full extent of the uploaded record) instead of a small radius around a
    single point.

    This is a stronger basis for ownership classification than OSM tags,
    since it reflects real parcel-level land classification from the actual
    document the user supplied -- see land_record_parser.py for the
    classification logic and its own honesty caveats (e.g. it's only as
    good as the uploaded document and the classification keywords matched).
    """
    lat_mid = (north + south) / 2
    lon_deg_per_m, lat_deg_per_m = _meters_to_deg(lat_mid)

    px_deg = cell_size_m * lon_deg_per_m
    py_deg = cell_size_m * lat_deg_per_m
    cols = max(4, int((east - west) / px_deg))
    rows = max(4, int((north - south) / py_deg))
    transform = Affine(px_deg, 0.0, west, 0.0, -py_deg, north)
    cell_area_m2 = cell_size_m * cell_size_m

    def rasterize_polys(polys):
        shapes = [(p, 1) for p in polys if p.is_valid and p.area > 0]
        if not shapes:
            return np.zeros((rows, cols), dtype=bool)
        return rasterize(
            shapes, out_shape=(rows, cols), transform=transform,
            fill=0, default_value=1, dtype="uint8",
        ).astype(bool)

    government_mask = rasterize_polys(government_polys)
    private_mask = rasterize_polys(private_polys)
    obstacle_mask = rasterize_polys(obstacle_polys)

    eligible_mask = government_mask & (~obstacle_mask) & (~private_mask)
    unverified_mask = (~obstacle_mask) & (~government_mask) & (~private_mask)

    structure = np.ones((3, 3), dtype=int)
    labeled, num_patches = ndimage.label(eligible_mask, structure=structure)

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
        patches.append({
            "label_id": int(label_id),
            "area_m2": round(area_m2, 1),
            "centroid": {"lat": centroid_lat, "lon": centroid_lon},
            "boundary_geojson": _polygon_boundaries_from_mask(patch_mask, transform),
            "patch_mask": patch_mask,  # kept for elevation-weighted site selection by the caller
        })
    patches.sort(key=lambda p: p["area_m2"], reverse=True)

    total_area_m2 = (rows * cols) * cell_area_m2
    area_breakdown = {
        "total_record_area_m2": round(total_area_m2, 1),
        "government_owned_area_m2": round(government_mask.sum() * cell_area_m2, 1),
        "government_area_occupied_by_development_m2": round((government_mask & obstacle_mask).sum() * cell_area_m2, 1),
        "private_area_m2": round(private_mask.sum() * cell_area_m2, 1),
        "unverified_ownership_area_m2": round(unverified_mask.sum() * cell_area_m2, 1),
        "buildings_roads_water_area_m2": round(obstacle_mask.sum() * cell_area_m2, 1),
        "final_eligible_vacant_government_area_m2": round(eligible_mask.sum() * cell_area_m2, 1),
    }

    layer_boundaries = {
        "government_owned": _polygon_boundaries_from_mask(government_mask, transform),
        "private": _polygon_boundaries_from_mask(private_mask, transform),
        "unverified_ownership": _polygon_boundaries_from_mask(unverified_mask, transform),
        "buildings_roads_water": _polygon_boundaries_from_mask(obstacle_mask, transform),
        "final_eligible": _polygon_boundaries_from_mask(eligible_mask, transform),
    }

    return {
        "patches": patches,
        "area_breakdown": area_breakdown,
        "layer_boundaries": layer_boundaries,
        "transform": transform,
        "grid_shape": (rows, cols),
        "cell_size_m": cell_size_m,
    }
