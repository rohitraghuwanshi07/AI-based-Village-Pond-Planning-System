"""
Runoff estimation using the SCS (Soil Conservation Service) Curve Number method —
a standard, widely-used, explainable technique in watershed engineering.

Reference formulas (USDA-NRCS National Engineering Handbook, Part 630, Ch. 10):
    S  = (25400 / CN) - 254        # potential maximum retention, mm
    Ia = 0.2 * S                    # initial abstraction (interception, infiltration
                                     # before runoff starts), mm
    Q  = (P - Ia)^2 / (P - Ia + S)  # runoff depth, mm  (only when P > Ia, else Q = 0)

Where:
    P  = rainfall depth for the event/period, mm
    CN = Curve Number (0-100), depends on land cover + soil type.
         Higher CN = less infiltration = more runoff (e.g. bare/compacted soil).
         Lower CN = more infiltration = less runoff (e.g. forest, sandy soil).
"""

# Typical Curve Number values for common land cover types, assuming average
# ("Hydrologic Soil Group B") soil conditions. These are standard reference
# values from NRCS tables — a reasonable default when we don't have detailed
# local soil survey data.
CURVE_NUMBERS = {
    "bare_fallow": 86,
    "cultivated_land": 78,
    "scrubland": 65,
    "grassland": 61,
    "forest": 55,
    "urban_residential": 85,
    "water_body": 100,
}

DEFAULT_CURVE_NUMBER = 75  # a reasonable general-purpose default for mixed rural land


def runoff_depth_mm(rainfall_mm: float, curve_number: float) -> float:
    """
    SCS Curve Number formula: compute runoff depth (mm) for a given rainfall
    depth (mm) and curve number.

    Returns 0 if rainfall is below the initial abstraction threshold (i.e.
    the ground can absorb it all before any runoff starts).
    """
    if curve_number <= 0 or curve_number > 100:
        raise ValueError("Curve number must be between 0 and 100")

    s = (25400 / curve_number) - 254
    ia = 0.2 * s

    if rainfall_mm <= ia:
        return 0.0

    q = ((rainfall_mm - ia) ** 2) / (rainfall_mm - ia + s)
    return q


def estimate_annual_runoff_volume(
    annual_rainfall_mm: float,
    catchment_area_m2: float,
    curve_number: float = DEFAULT_CURVE_NUMBER,
) -> dict:
    """
    Estimate the annual runoff volume for a catchment.

    Note: applying the SCS-CN formula directly to an ANNUAL rainfall total
    (rather than per-storm-event) is a simplification — real annual runoff is
    the sum of many individual storm events, each partially absorbed. This
    single-application approach is the standard simplified method for a
    quick planning-level estimate, which is appropriate for this assignment's
    scope; a more advanced version would run the formula per rainy day, using
    the daily rainfall series we already fetch.

    Returns:
        dict with runoff_depth_mm, runoff_volume_m3, runoff_coefficient
        (= volume of runoff / volume of rainfall that fell, a sanity-check
        ratio typically between 0.1 and 0.5 for rural catchments).
    """
    q_mm = runoff_depth_mm(annual_rainfall_mm, curve_number)
    runoff_volume_m3 = (q_mm / 1000) * catchment_area_m2
    total_rainfall_volume_m3 = (annual_rainfall_mm / 1000) * catchment_area_m2

    runoff_coefficient = (
        runoff_volume_m3 / total_rainfall_volume_m3 if total_rainfall_volume_m3 > 0 else 0.0
    )

    return {
        "curve_number_used": curve_number,
        "annual_rainfall_mm": annual_rainfall_mm,
        "runoff_depth_mm": round(q_mm, 1),
        "runoff_volume_m3": round(runoff_volume_m3, 1),
        "runoff_coefficient": round(runoff_coefficient, 3),
    }


def estimate_daily_series_runoff(daily_rainfall_mm: list, curve_number: float = DEFAULT_CURVE_NUMBER) -> dict:
    """
    More accurate alternative: apply the SCS-CN formula to EACH day's rainfall
    individually, then sum the results. This better reflects reality, since
    each rain event has its own initial abstraction, rather than treating the
    whole year as one giant storm.

    Args:
        daily_rainfall_mm: list of daily rainfall values (mm), None values are skipped.
        curve_number: as above.

    Returns:
        dict with total runoff volume-equivalent depth (mm) summed over all days,
        and the number of days that actually produced runoff.
    """
    total_runoff_mm = 0.0
    rainy_days_with_runoff = 0

    for val in daily_rainfall_mm:
        if val is None or val <= 0:
            continue
        q = runoff_depth_mm(val, curve_number)
        if q > 0:
            rainy_days_with_runoff += 1
        total_runoff_mm += q

    return {
        "curve_number_used": curve_number,
        "total_runoff_depth_mm": round(total_runoff_mm, 1),
        "days_producing_runoff": rainy_days_with_runoff,
    }
