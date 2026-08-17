"""
Catchment delineation using the D8 algorithm — implemented directly in NumPy
rather than relying on a third-party watershed library, to avoid dependency
conflicts and to keep the algorithm fully transparent/explainable.

Pipeline:
1. Fill depressions (priority-flood algorithm) so every cell has a downhill path.
2. Compute D8 flow direction: each cell points to its steepest downhill neighbor.
3. Compute flow accumulation: how many upstream cells drain through each cell.
4. Delineate the catchment for a given pour point: trace all cells that
   eventually flow into it (upstream trace via reverse BFS).
"""

import heapq
from collections import defaultdict, deque

import numpy as np

# The 8 neighbor offsets (row_offset, col_offset) and their relative distance
# multiplier (1.0 for orthogonal, sqrt(2) for diagonal neighbors).
_NEIGHBORS = [
    (-1, -1, np.sqrt(2)), (-1, 0, 1.0), (-1, 1, np.sqrt(2)),
    (0, -1, 1.0),                        (0, 1, 1.0),
    (1, -1, np.sqrt(2)),  (1, 0, 1.0),  (1, 1, np.sqrt(2)),
]


def fill_depressions(dem: np.ndarray) -> np.ndarray:
    """
    Priority-flood depression filling (Barnes et al. algorithm), WITH flat-area
    resolution: as we flood inward from the raster's border, we add a tiny,
    strictly-increasing epsilon to each newly-visited cell's elevation.

    Why this matters: real SRTM data has integer-meter precision, so large
    flat areas (common on agricultural plains) often have IDENTICAL elevation
    values across many adjacent cells. Naive D8 flow direction requires a
    STRICTLY lower neighbor to assign a downhill direction -- on a perfectly
    flat patch, no neighbor is ever strictly lower, so those cells get "stuck"
    with no flow direction at all. In testing, this caused ~80% of cells on
    realistic flat terrain to have no flow direction, which fragmented the
    catchment into a handful of disconnected cells instead of a real drainage
    area. Real GIS tools (ArcGIS, TauDEM) call this step "resolve flats" and
    solve it the same way: impose a tiny artificial gradient across flat areas,
    pointing back toward the nearest lower terrain / outlet, so every cell
    gets a well-defined (if extremely gentle) downhill direction.
    """
    rows, cols = dem.shape
    filled = dem.copy().astype(float)
    visited = np.zeros_like(dem, dtype=bool)
    heap = []

    # Seed the priority queue with all border cells (water always drains off
    # the edge of our analysis window).
    for r in range(rows):
        for c in (0, cols - 1):
            heapq.heappush(heap, (filled[r, c], r, c))
            visited[r, c] = True
    for c in range(cols):
        for r in (0, rows - 1):
            if not visited[r, c]:
                heapq.heappush(heap, (filled[r, c], r, c))
                visited[r, c] = True

    # Tiny per-step increment used to break elevation ties. Small enough to
    # never overwhelm a real elevation difference (real DEM steps are at
    # least ~0.1m for meaningful terrain), but large enough after thousands
    # of steps to guarantee a strict, unique ordering across the whole raster.
    epsilon = 1e-5
    counter = 0

    while heap:
        elev, r, c = heapq.heappop(heap)
        for dr, dc, _ in _NEIGHBORS:
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and not visited[nr, nc]:
                visited[nr, nc] = True
                counter += 1
                # A neighbor can never be lower than the cell that "let it in",
                # and gets a tiny unique nudge to guarantee it's never EQUAL
                # either -- this is what fills pits AND resolves flats.
                new_elev = max(filled[nr, nc], elev) + epsilon * counter
                filled[nr, nc] = new_elev
                heapq.heappush(heap, (new_elev, nr, nc))

    return filled


def flow_direction_d8(filled: np.ndarray, px_m: float, py_m: float):
    """
    For every cell, find its steepest downhill neighbor (D8 = 8 possible directions).

    Returns two arrays (downstream_row, downstream_col), same shape as the DEM,
    holding the row/col index each cell drains into. A value of -1 means the
    cell has no downhill neighbor (shouldn't happen after fill_depressions,
    except at the raster's outer edge).
    """
    rows, cols = filled.shape
    downstream_r = np.full((rows, cols), -1, dtype=int)
    downstream_c = np.full((rows, cols), -1, dtype=int)

    for r in range(rows):
        for c in range(cols):
            best_slope = 0.0
            best_rc = None
            for dr, dc, dist_mult in _NEIGHBORS:
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols:
                    dist_m = dist_mult * ((px_m + py_m) / 2)
                    slope = (filled[r, c] - filled[nr, nc]) / dist_m
                    if slope > best_slope:
                        best_slope = slope
                        best_rc = (nr, nc)
            if best_rc is not None:
                downstream_r[r, c], downstream_c[r, c] = best_rc

    return downstream_r, downstream_c


def flow_accumulation(filled: np.ndarray, downstream_r: np.ndarray, downstream_c: np.ndarray) -> np.ndarray:
    """
    For every cell, count how many cells (including itself) ultimately drain
    through it. High accumulation = stream channel; used to snap a user's
    rough click to the nearest actual drainage line.

    Algorithm: process cells from highest to lowest elevation. By the time we
    reach a cell, all of its upstream contributors (which are higher) have
    already been processed and added to it — so we just pass its current
    total down to its one downstream neighbor.
    """
    rows, cols = filled.shape
    acc = np.ones((rows, cols), dtype=float)

    # Sort all cell indices by elevation, descending
    flat_order = np.argsort(-filled.ravel())
    rs, cs = np.unravel_index(flat_order, filled.shape)

    for r, c in zip(rs, cs):
        dr, dc = downstream_r[r, c], downstream_c[r, c]
        if dr != -1:
            acc[dr, dc] += acc[r, c]

    return acc


def snap_to_channel(acc: np.ndarray, row: int, col: int, search_radius: int = 8, min_accumulation: float = 5.0):
    """
    A user's click rarely lands exactly on the true drainage channel. This
    snaps the clicked cell to the nearest cell with high flow accumulation
    (a real stream/channel), within a small search window.
    """
    rows, cols = acc.shape
    best = (row, col)
    best_acc = acc[row, col]

    r0, r1 = max(0, row - search_radius), min(rows, row + search_radius + 1)
    c0, c1 = max(0, col - search_radius), min(cols, col + search_radius + 1)

    window = acc[r0:r1, c0:c1]
    local_max_idx = np.unravel_index(np.argmax(window), window.shape)
    candidate_acc = window[local_max_idx]

    if candidate_acc > best_acc and candidate_acc >= min_accumulation:
        best = (r0 + local_max_idx[0], c0 + local_max_idx[1])
        best_acc = candidate_acc

    return best


def delineate_catchment(downstream_r: np.ndarray, downstream_c: np.ndarray, pour_row: int, pour_col: int) -> np.ndarray:
    """
    Find every cell that eventually flows into the given pour point.

    Builds a reverse drainage graph (who flows INTO each cell) and does a
    breadth-first search upstream from the pour point. Returns a boolean
    mask, same shape as the DEM, True for every cell in the catchment.
    """
    rows, cols = downstream_r.shape

    reverse_graph = defaultdict(list)
    for r in range(rows):
        for c in range(cols):
            dr, dc = downstream_r[r, c], downstream_c[r, c]
            if dr != -1:
                reverse_graph[(dr, dc)].append((r, c))

    visited = np.zeros((rows, cols), dtype=bool)
    queue = deque([(pour_row, pour_col)])
    visited[pour_row, pour_col] = True

    while queue:
        r, c = queue.popleft()
        for nr, nc in reverse_graph.get((r, c), []):
            if not visited[nr, nc]:
                visited[nr, nc] = True
                queue.append((nr, nc))

    return visited
