"""
Automatically selects a candidate pond location from a terrain raster,
without any hard-coded coordinates -- entirely derived from the slope and
flow-accumulation patterns of whatever DEM (real or contour-reconstructed)
is passed in.

Selection logic: a good small-pond site is somewhere that (a) collects a
meaningful amount of upstream drainage (high flow accumulation -- i.e. it
sits on or near an actual drainage channel) and (b) is flat enough to be
practical/affordable to excavate (low local slope). We score every cell on
that basis and pick the best-scoring one, excluding the raster's outer edge
(a real site needs surrounding land, not the boundary of our data window).
"""

import numpy as np


def select_pond_site(
    slope_deg: np.ndarray,
    accumulation: np.ndarray,
    max_slope_deg: float = 8.0,
    edge_margin_cells: int = 3,
):
    """
    Pick the best candidate pond site cell.

    Args:
        slope_deg: 2D array of slope in degrees (from terrain_engine.compute_slope_degrees)
        accumulation: 2D array of flow accumulation (from catchment_engine.flow_accumulation)
        max_slope_deg: cells steeper than this are excluded from consideration
        edge_margin_cells: exclude cells within this many cells of the raster edge

    Returns:
        (row, col, info_dict) for the selected site. info_dict explains why
        it was picked and what fallback tier was used (for transparency in
        the API response -- this is deliberately not a black box).
    """
    rows, cols = slope_deg.shape
    if rows <= 2 * edge_margin_cells or cols <= 2 * edge_margin_cells:
        edge_margin_cells = 0  # raster too small to afford a margin

    interior_mask = np.zeros_like(slope_deg, dtype=bool)
    interior_mask[edge_margin_cells: rows - edge_margin_cells, edge_margin_cells: cols - edge_margin_cells] = True

    low_slope_mask = slope_deg <= max_slope_deg
    candidate_mask = interior_mask & low_slope_mask & ~np.isnan(slope_deg)

    tier = "low_slope_high_accumulation"
    if not candidate_mask.any():
        # Fallback: no cell meets the slope threshold (e.g. uniformly hilly
        # terrain) -- relax to just the interior, ignore slope
        candidate_mask = interior_mask
        tier = "relaxed_ignoring_slope"

    if not candidate_mask.any():
        # Final fallback: whole raster
        candidate_mask = np.ones_like(slope_deg, dtype=bool)
        tier = "whole_raster_fallback"

    masked_acc = np.where(candidate_mask, accumulation, -np.inf)
    best_idx = np.unravel_index(np.argmax(masked_acc), masked_acc.shape)
    row, col = int(best_idx[0]), int(best_idx[1])

    info = {
        "selection_tier": tier,
        "max_slope_deg_threshold": max_slope_deg,
        "slope_at_site_deg": round(float(slope_deg[row, col]), 2),
        "flow_accumulation_at_site": round(float(accumulation[row, col]), 1),
        "candidate_cells_considered": int(candidate_mask.sum()),
    }
    return row, col, info
