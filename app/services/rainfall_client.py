"""
Historical rainfall client using Open-Meteo's Archive API — free, no API key.

Docs: https://open-meteo.com/en/docs/historical-weather-api

We pull daily precipitation for a number of past years at a given lat/lon,
then summarize it into stats our runoff engine will use later:
- average annual rainfall (mm)
- monsoon-season (Jun-Sep) total, since that's when most runoff happens in India
- the raw daily series, in case the frontend wants to chart it
"""

from datetime import date

import httpx

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

HEADERS = {"User-Agent": "village-pond-planner-student-project"}


async def get_historical_rainfall(lat: float, lon: float, years: int = 10) -> dict:
    """
    Fetch daily rainfall for the past `years` years at a location and summarize it.

    Args:
        lat, lon: location coordinates
        years: how many years of history to pull (default 10)

    Returns:
        dict with annual_average_mm, monsoon_average_mm, yearly_totals, and
        the raw daily series (dates + precipitation_mm).
    """
    end_date = date.today().replace(day=1)  # avoid partial current month
    start_date = end_date.replace(year=end_date.year - years)

    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "daily": "precipitation_sum",
        "timezone": "auto",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(ARCHIVE_URL, params=params, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    values = daily.get("precipitation_sum", [])

    # Group by year to compute yearly totals
    yearly_totals: dict[str, float] = {}
    monsoon_totals: dict[str, float] = {}

    for d_str, val in zip(dates, values):
        if val is None:
            continue
        d = date.fromisoformat(d_str)
        year_key = str(d.year)
        yearly_totals[year_key] = yearly_totals.get(year_key, 0.0) + val

        if 6 <= d.month <= 9:  # June-September = Indian monsoon window
            monsoon_totals[year_key] = monsoon_totals.get(year_key, 0.0) + val

    # Drop the first/last partial years from the averages (incomplete data skews results)
    complete_years = sorted(yearly_totals.keys())[1:-1] or sorted(yearly_totals.keys())

    annual_average_mm = (
        sum(yearly_totals[y] for y in complete_years) / len(complete_years)
        if complete_years else 0.0
    )
    monsoon_average_mm = (
        sum(monsoon_totals.get(y, 0.0) for y in complete_years) / len(complete_years)
        if complete_years else 0.0
    )

    return {
        "lat": lat,
        "lon": lon,
        "years_analyzed": len(complete_years),
        "annual_average_mm": round(annual_average_mm, 1),
        "monsoon_average_mm": round(monsoon_average_mm, 1),
        "yearly_totals_mm": {y: round(v, 1) for y, v in yearly_totals.items()},
        "daily_series": {"dates": dates, "precipitation_mm": values},
    }
