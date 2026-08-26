"""
Automatically selects a candidate pond location from a terrain raster,
without any hard-coded coordinates -- entirely derived from the elevation,
slope, and flow-accumulation patterns of whatever DEM (real or
contour-reconstructed) is passed in.

Selection logic: water collects at the LOWEST point of a drainage basin --
that's the primary physical fact this scoring is built around. A good small
pond site is therefore (a) genuinely low-lying relative to its surroundings
(the main factor -- this is where gravity actually takes the water), (b)
sitting on or near an actual drainage channel (high flow accumulation --
confirms it's a real collection point, not just an isolated low spot that
happens to be disconnected from the catchment), and (c) flat enough to be
practical/affordable to excavate (low local slope). We score every candidate
cell as a weighted combination of (a) and (b), filtered by (c), and exclude
the raster's outer edge (a real site needs surrounding land, not the
boundary of our data window).
"""

import numpy as np


def select_pond_site(
    slope_deg: np.ndarray,
    accumulation: np.ndarray,
    elevation: np.ndarray | None = None,
    max_slope_deg: float = 8.0,
    edge_margin_cells: int = 3,
    elevation_weight: float = 0.6,
    accumulation_weight: float = 0.4,
):
    """
    Pick the best candidate pond site cell.

    Args:
        slope_deg: 2D array of slope in degrees (from terrain_engine.compute_slope_degrees)
        accumulation: 2D array of flow accumulation (from catchment_engine.flow_accumulation)
        elevation: 2D array of elevation in meters. If provided, lowest-elevation
                   cells are explicitly favored (this is the main fix -- without
                   this, the old version could pick a mid-slope point along a
                   channel instead of the true low point where water actually
                   pools). If omitted, falls back to accumulation-only scoring.
        max_slope_deg: cells steeper than this are excluded from consideration
        edge_margin_cells: exclude cells within this many cells of the raster edge
        elevation_weight: how strongly to favor low elevation (0-1)
        accumulation_weight: how strongly to favor high flow accumulation (0-1)

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

    tier = "elevation_and_accumulation_weighted" if elevation is not None else "accumulation_only"
    if not candidate_mask.any():
        candidate_mask = interior_mask
        tier = "relaxed_ignoring_slope"
    if not candidate_mask.any():
        candidate_mask = np.ones_like(slope_deg, dtype=bool)
        tier = "whole_raster_fallback"

    # Normalize accumulation to 0-1 (higher = more drainage collects here)
    acc_masked = np.where(candidate_mask, accumulation, np.nan)
    acc_min, acc_max = np.nanmin(acc_masked), np.nanmax(acc_masked)
    acc_range = max(acc_max - acc_min, 1e-9)
    norm_acc = (accumulation - acc_min) / acc_range

    if elevation is not None:
        # Normalize elevation to 0-1, INVERTED so that LOW elevation -> score near 1
        elev_masked = np.where(candidate_mask, elevation, np.nan)
        elev_min, elev_max = np.nanmin(elev_masked), np.nanmax(elev_masked)
        elev_range = max(elev_max - elev_min, 1e-9)
        norm_low_elev = 1.0 - (elevation - elev_min) / elev_range

        total_weight = elevation_weight + accumulation_weight
        score = (elevation_weight * norm_low_elev + accumulation_weight * norm_acc) / total_weight
    else:
        score = norm_acc

    masked_score = np.where(candidate_mask, score, -np.inf)
    best_idx = np.unravel_index(np.argmax(masked_score), masked_score.shape)
    row, col = int(best_idx[0]), int(best_idx[1])

    info = {
        "selection_tier": tier,
        "max_slope_deg_threshold": max_slope_deg,
        "slope_at_site_deg": round(float(slope_deg[row, col]), 2),
        "flow_accumulation_at_site": round(float(accumulation[row, col]), 1),
        "candidate_cells_considered": int(candidate_mask.sum()),
        "elevation_weight": elevation_weight if elevation is not None else None,
        "accumulation_weight": accumulation_weight if elevation is not None else None,
    }
    if elevation is not None:
        info["elevation_at_site_m"] = round(float(elevation[row, col]), 1)
        info["elevation_percentile_among_candidates"] = round(
            float((elevation[row, col] <= elev_masked[~np.isnan(elev_masked)]).mean() * 100), 1
        )
    return row, col, info
