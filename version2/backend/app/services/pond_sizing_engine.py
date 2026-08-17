"""
Pond sizing engine: given an estimated runoff volume and site constraints,
recommend a pond depth, surface area, and compute the achievable storage
capacity using the standard trapezoidal-prism earthwork volume formula.
"""

import math

# Reasonable engineering defaults for small village earthen ponds
MIN_DEPTH_M = 1.5
MAX_DEPTH_M = 4.0
SIDE_SLOPE_RATIO = 1.5  # horizontal:vertical, e.g. 1.5:1 -- a common stable slope for earthen embankments


def trapezoidal_volume_m3(surface_area_m2: float, depth_m: float, side_slope: float = SIDE_SLOPE_RATIO) -> float:
    """
    Volume of a truncated pyramid (trapezoidal pond cross-section), which is
    the standard way to estimate earthwork/storage volume for an excavated
    pond with sloped sides (rather than vertical walls).

    V = D/6 * (A_top + A_bottom + 4*A_mid)

    We derive A_bottom and A_mid from A_top by shrinking the pond's linear
    dimensions inward by (side_slope * depth) at the base, and half that at
    mid-depth, assuming a roughly square pond footprint for simplicity.
    """
    side_top = math.sqrt(surface_area_m2)  # assume square footprint

    inset_bottom = side_slope * depth_m
    inset_mid = side_slope * (depth_m / 2)

    side_bottom = max(side_top - 2 * inset_bottom, 0.1)  # avoid negative/zero
    side_mid = max(side_top - 2 * inset_mid, 0.1)

    area_top = side_top ** 2
    area_bottom = side_bottom ** 2
    area_mid = side_mid ** 2

    volume = (depth_m / 6) * (area_top + area_bottom + 4 * area_mid)
    return volume


def recommend_pond(
    required_volume_m3: float,
    available_site_area_m2: float,
    target_capture_fraction: float = 0.5,
) -> dict:
    """
    Recommend a pond depth + surface area to capture a target fraction of the
    estimated runoff, constrained by the available flat land at the site.

    Args:
        required_volume_m3: total annual runoff volume estimated for the catchment.
        available_site_area_m2: how much flat/suitable land is available at
                                 the chosen site (from our terrain suitability
                                 analysis).
        target_capture_fraction: what fraction of annual runoff we want this
                                  pond to be able to store (0.3-0.6 is a
                                  reasonable planning range; capturing 100%
                                  of a whole year's runoff in one pond is
                                  rarely practical or necessary).

    Returns:
        dict with recommended_depth_m, recommended_surface_area_m2,
        achievable_storage_capacity_m3, and whether the site is large enough
        to meet the target.
    """
    target_volume_m3 = required_volume_m3 * target_capture_fraction

    # Cap the pond's surface area at whatever flat land is actually available,
    # leaving a margin for embankments/access (use at most 70% of the site).
    max_usable_area_m2 = available_site_area_m2 * 0.7

    # Try depths from shallow to deep. At each depth, first check whether the
    # target is even achievable using the maximum usable area -- only if so
    # do we binary-search for the minimal (cheaper-to-excavate) area that
    # meets the target at that depth.
    best_depth = None
    best_area = None
    best_volume = 0.0
    site_sufficient = False

    for depth_cm in range(int(MIN_DEPTH_M * 100), int(MAX_DEPTH_M * 100) + 1, 25):
        depth = depth_cm / 100
        max_possible_volume = trapezoidal_volume_m3(max_usable_area_m2, depth)

        if max_possible_volume >= target_volume_m3:
            # Target is achievable at this depth -- find the smallest area that hits it.
            area_low, area_high = 10.0, max_usable_area_m2
            for _ in range(40):
                mid_area = (area_low + area_high) / 2
                vol = trapezoidal_volume_m3(mid_area, depth)
                if vol < target_volume_m3:
                    area_low = mid_area
                else:
                    area_high = mid_area
            best_depth = depth
            best_area = area_high
            best_volume = trapezoidal_volume_m3(best_area, depth)
            site_sufficient = True
            break  # shallowest depth that fits wins

    if not site_sufficient:
        # Target isn't achievable at any depth up to MAX_DEPTH_M with the
        # available land -- report the best achievable (max depth, max area)
        # and flag the shortfall honestly rather than silently under-delivering.
        best_depth = MAX_DEPTH_M
        best_area = max_usable_area_m2
        best_volume = trapezoidal_volume_m3(best_area, best_depth)

    side_length_m = math.sqrt(best_area)

    return {
        "target_capture_fraction": target_capture_fraction,
        "target_volume_m3": round(target_volume_m3, 1),
        "recommended_depth_m": round(best_depth, 2),
        "recommended_surface_area_m2": round(best_area, 1),
        "recommended_surface_dimensions_m": f"{round(side_length_m,1)} x {round(side_length_m,1)} (approx, assuming square footprint)",
        "achievable_storage_capacity_m3": round(best_volume, 1),
        "achievable_storage_capacity_liters": round(best_volume * 1000, 0),
        "site_area_sufficient_for_target": site_sufficient,
        "percent_of_annual_runoff_captured": round((best_volume / required_volume_m3) * 100, 1) if required_volume_m3 > 0 else 0.0,
        "side_slope_ratio": f"{SIDE_SLOPE_RATIO}:1 (horizontal:vertical)",
    }
