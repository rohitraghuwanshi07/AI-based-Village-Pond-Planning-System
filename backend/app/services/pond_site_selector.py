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
    restrict_mask: np.ndarray | None = None,
    water_exclusion_mask: np.ndarray | None = None,
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
        restrict_mask: optional boolean array (same shape as slope_deg) --
                        if provided, only cells where this is True are ever
                        considered, regardless of slope. Used to restrict site
                        selection to a specific eligible land patch (e.g. from
                        an uploaded land-ownership record), rather than the
                        whole raster.
        water_exclusion_mask: optional boolean array (same shape as slope_deg) --
                        True means the cell is on or near an EXISTING water
                        body (river, canal, lake, reservoir) and must never be
                        selected. This is a hard exclusion, applied before any
                        scoring: the score itself would otherwise actively
                        favor these cells, since "low elevation + high flow
                        accumulation" is exactly what an existing water body
                        looks like on the raster.

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
    if restrict_mask is not None:
        interior_mask = interior_mask & restrict_mask
    if water_exclusion_mask is not None:
        interior_mask = interior_mask & ~water_exclusion_mask

    hard_exclusions_present = restrict_mask is not None or water_exclusion_mask is not None

    low_slope_mask = slope_deg <= max_slope_deg
    candidate_mask = interior_mask & low_slope_mask & ~np.isnan(slope_deg)

    tier = "elevation_and_accumulation_weighted" if elevation is not None else "accumulation_only"
    if not candidate_mask.any():
        candidate_mask = interior_mask
        tier = "relaxed_ignoring_slope"
    if not candidate_mask.any():
        if hard_exclusions_present:
            # A restrict_mask or water_exclusion_mask means "never place a
            # pond outside this eligible land" / "never place a pond on
            # existing water" -- if honoring that leaves nothing, there is no
            # valid site, and falling back to the whole raster would silently
            # pick a location OUTSIDE the eligible land or INSIDE a water
            # body, defeating the entire point of the exclusion. Fail loudly
            # instead.
            raise ValueError(
                "No valid cell found outside the excluded areas (eligible "
                "land patch and/or existing water bodies) -- the search area "
                "may be entirely water/restricted, or too small/fragmented "
                "to site a pond."
            )
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


def _generate_rank_explanation(rank, scores: dict, nearby: dict, composite_score: float, raw_score: float = None, data_unavailable: bool = False) -> str:
    """Generate a plain-language explanation of why a site got its rank."""
    parts = []

    if rank == 1:
        parts.append("Ranked #1 — The absolute best site for a pond in this area.")
    elif rank == 2:
        parts.append("Ranked #2 — An excellent secondary choice with high suitability.")
    elif rank == 3:
        parts.append("Ranked #3 — A very good candidate site.")
    elif rank == 4:
        parts.append("Ranked #4 — A viable site, though slightly less optimal than the top 3.")
    else:
        parts.append(f"Ranked #{rank} — A viable alternative site.")

    # Calculations
    if raw_score is not None and raw_score != composite_score:
        parts.append(f"<b>Calculation:</b> This site achieved a final score of <b>{composite_score:.1f}/100</b> (Base score: {raw_score:.1f}).")
    else:
        parts.append(f"<b>Calculation:</b> This site achieved a composite score of <b>{composite_score:.1f}/100</b>.")
    
    calc_parts = []
    if scores.get("elevation_score") is not None:
        calc_parts.append(f"Elevation ({scores['elevation_score']:.0f}%)")
    if scores.get("accumulation_score") is not None:
        calc_parts.append(f"Drainage ({scores['accumulation_score']:.0f}%)")
    if scores.get("slope_score") is not None:
        calc_parts.append(f"Flatness ({scores['slope_score']:.0f}%)")
    
    parts.append(f"Derived from weighted factors: {', '.join(calc_parts)}.")

    # Penalties -- only meaningful when the obstacle counts behind them were
    # actually verified. When data_unavailable is True, buildings/roads/water
    # are placeholder zeros from a failed query, not a real "0 obstacles"
    # result, so no penalty section is shown for them at all (showing "no
    # penalties applied" would be just as misleading as showing fake ones).
    if not data_unavailable:
        buildings = nearby.get("buildings_nearby", 0)
        roads = nearby.get("roads_nearby", 0)
        water = nearby.get("water_bodies_nearby", 0)

        penalty_parts = []
        if buildings > 0: penalty_parts.append(f"{buildings} building(s) (-{buildings * 5})")
        if roads > 0: penalty_parts.append(f"{roads} road(s) (-{roads * 10})")
        if water > 0: penalty_parts.append(f"{water} water body/ies (-{water * 15})")

        if penalty_parts:
            parts.append(f"<b>Penalties applied:</b> {', '.join(penalty_parts)}.")

    # Reasons
    reasons = []
    # Elevation
    elev_score = scores.get("elevation_score", 0) or 0
    if elev_score > 80:
        reasons.append("It sits in an ideal low-lying depression where water naturally pools.")
    elif elev_score > 50:
        reasons.append("It has a favorable low elevation compared to the surrounding terrain.")

    # Flow accumulation
    acc_score = scores.get("accumulation_score", 0) or 0
    if acc_score > 80:
        reasons.append("It is positioned on a major natural drainage channel, ensuring maximum water capture.")
    elif acc_score > 50:
        reasons.append("It receives significant runoff from a large upstream catchment area.")

    # Slope
    slope_score = scores.get("slope_score", 0) or 0
    if slope_score > 80:
        reasons.append("The terrain is exceptionally flat, making construction easy and cost-effective.")
    elif slope_score > 50:
        reasons.append("The gentle slope is highly suitable for stable pond embankments.")

    if data_unavailable:
        # Never claim "clear of obstacles" from a failed query -- that
        # turns "we don't know" into a false positive, which is worse than
        # saying nothing.
        reasons.append(
            "Nearby obstacle data (buildings/roads/water) could not be verified for this "
            "site, so its true available area is unconfirmed."
        )
    else:
        # Nearby obstacles -- only a genuine "clear" claim when it was actually checked.
        buildings = nearby.get("buildings_nearby", 0)
        roads = nearby.get("roads_nearby", 0)
        water = nearby.get("water_bodies_nearby", 0)

        if buildings == 0 and roads == 0 and water == 0:
            reasons.append("The site is completely clear of buildings and roads, providing maximum usable space.")

    if reasons:
        parts.append("<b>Why it's one of the best:</b> " + " ".join(reasons))

    return " ".join(parts)


def select_top_n_pond_sites(
    slope_deg: np.ndarray,
    accumulation: np.ndarray,
    elevation: np.ndarray | None = None,
    max_slope_deg: float = 8.0,
    edge_margin_cells: int = 3,
    elevation_weight: float = 0.40,
    accumulation_weight: float = 0.25,
    slope_weight: float = 0.15,
    road_distance_weight: float = 0.10,
    building_distance_weight: float = 0.10,
    water_exclusion_mask: np.ndarray | None = None,
    n_sites: int = 5,
    min_separation_cells: int = 20,
    transform=None,
):
    """
    Pick the top N candidate pond site cells, ranked by a composite score.

    Each site is separated by at least min_separation_cells from all
    previously selected sites (so they're not clustered together).

    Sites on or near existing water bodies (rivers, canals, lakes) are
    HARD-EXCLUDED via the water_exclusion_mask — a pond cannot be built
    on an existing water source.

    Args:
        slope_deg: 2D array of slope in degrees
        accumulation: 2D array of flow accumulation
        elevation: 2D array of elevation in meters (optional but recommended)
        max_slope_deg: cells steeper than this are penalised (not excluded)
        edge_margin_cells: exclude cells within this many cells of the raster edge
        elevation_weight: weight for low-elevation factor (0-1)
        accumulation_weight: weight for high flow accumulation (0-1)
        slope_weight: weight for low slope factor (0-1)
        road_distance_weight: weight for distance from roads (placeholder, scored
                              via obstacle data post-hoc)
        building_distance_weight: weight for distance from buildings (placeholder)
        water_exclusion_mask: boolean array — True means the cell is EXCLUDED
                              (it's on or near a water body). Must be same shape
                              as slope_deg.
        n_sites: number of sites to return (default 5)
        min_separation_cells: minimum grid distance between selected sites
        transform: rasterio affine transform (needed to convert row/col to lat/lon)

    Returns:
        List of dicts, one per ranked site:
        [
            {
                "rank": 1,
                "row": int,
                "col": int,
                "lat": float,
                "lon": float,
                "composite_score": float (0-100),
                "scores": {
                    "elevation_score": float (0-100),
                    "accumulation_score": float (0-100),
                    "slope_score": float (0-100),
                },
                "selection_info": {...},
            },
            ...
        ]
    """
    rows, cols = slope_deg.shape
    if rows <= 2 * edge_margin_cells or cols <= 2 * edge_margin_cells:
        edge_margin_cells = 0

    # Interior mask (exclude raster edges)
    interior_mask = np.zeros_like(slope_deg, dtype=bool)
    interior_mask[edge_margin_cells: rows - edge_margin_cells,
                  edge_margin_cells: cols - edge_margin_cells] = True

    # Exclude NaN slopes
    valid_mask = interior_mask & ~np.isnan(slope_deg)

    # Hard-exclude water bodies — a pond CANNOT be on an existing river/canal/lake
    if water_exclusion_mask is not None:
        valid_mask = valid_mask & ~water_exclusion_mask

    if not valid_mask.any():
        # Nothing valid — return empty
        return []

    # --- Compute per-factor scores (each normalized to 0–1, higher = better) ---

    # 1. Elevation score: LOW elevation → high score
    if elevation is not None:
        elev_masked = np.where(valid_mask, elevation, np.nan)
        elev_min = np.nanmin(elev_masked)
        elev_max = np.nanmax(elev_masked)
        elev_range = max(elev_max - elev_min, 1e-9)
        norm_elev = 1.0 - (elevation - elev_min) / elev_range  # inverted: low → 1
    else:
        norm_elev = np.zeros_like(slope_deg)

    # 2. Accumulation score: HIGH accumulation → high score
    acc_masked = np.where(valid_mask, accumulation, np.nan)
    acc_min = np.nanmin(acc_masked)
    acc_max = np.nanmax(acc_masked)
    acc_range = max(acc_max - acc_min, 1e-9)
    norm_acc = (accumulation - acc_min) / acc_range

    # 3. Slope score: LOW slope → high score
    slope_masked = np.where(valid_mask, slope_deg, np.nan)
    slope_min = np.nanmin(slope_masked)
    slope_max = np.nanmax(slope_masked)
    slope_range = max(slope_max - slope_min, 1e-9)
    norm_slope = 1.0 - (slope_deg - slope_min) / slope_range  # inverted: low → 1

    # Composite score (road and building distance are scored post-hoc via OSM data)
    # For the raster-based ranking, we use only elevation + accumulation + slope
    raster_total_weight = elevation_weight + accumulation_weight + slope_weight
    if elevation is not None:
        composite = (
            elevation_weight * norm_elev +
            accumulation_weight * norm_acc +
            slope_weight * norm_slope
        ) / raster_total_weight
    else:
        composite = (
            accumulation_weight * norm_acc +
            slope_weight * norm_slope
        ) / (accumulation_weight + slope_weight)

    # Scale to 0–100
    composite_100 = composite * 100.0

    # Apply valid mask
    masked_score = np.where(valid_mask, composite_100, -np.inf)

    # --- Iteratively extract top N sites with minimum separation ---
    selected_sites = []
    selection_mask = valid_mask.copy()

    for rank in range(1, n_sites + 1):
        if not selection_mask.any():
            break

        current_scores = np.where(selection_mask, masked_score, -np.inf)
        best_idx = np.unravel_index(np.argmax(current_scores), current_scores.shape)
        r, c = int(best_idx[0]), int(best_idx[1])

        # Get lat/lon from transform
        if transform is not None:
            site_lon, site_lat = transform * (c, r)
        else:
            site_lon, site_lat = 0.0, 0.0

        # Per-factor scores at this cell (0–100)
        scores_at_site = {
            "elevation_score": round(float(norm_elev[r, c] * 100), 1) if elevation is not None else None,
            "accumulation_score": round(float(norm_acc[r, c] * 100), 1),
            "slope_score": round(float(norm_slope[r, c] * 100), 1),
        }

        site_info = {
            "rank": rank,
            "row": r,
            "col": c,
            "lat": round(float(site_lat), 6),
            "lon": round(float(site_lon), 6),
            "composite_score": round(float(composite_100[r, c]), 1),
            "scores": scores_at_site,
            "selection_info": {
                "slope_at_site_deg": round(float(slope_deg[r, c]), 2),
                "flow_accumulation_at_site": round(float(accumulation[r, c]), 1),
                "elevation_at_site_m": round(float(elevation[r, c]), 1) if elevation is not None else None,
            },
        }

        selected_sites.append(site_info)

        # Mask out a circle around this site so the next pick isn't too close
        row_grid, col_grid = np.ogrid[:rows, :cols]
        dist_sq = (row_grid - r) ** 2 + (col_grid - c) ** 2
        exclusion_radius_sq = min_separation_cells ** 2
        selection_mask = selection_mask & (dist_sq > exclusion_radius_sq)

    return selected_sites
