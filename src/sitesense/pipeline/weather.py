"""Local-time daily weather, ETCCDI-style thresholds and event days per grid cell.

Event definitions follow the team's proposed decision #4 (pending confirmation). Snowfall is
not in the weather download, so snow events are not produced.
"""

import numpy as np
import pandas as pd

MIN_HOURS = 23  # a local day has 23-25 hours around DST changes
WINDOW_DAYS = 2  # calendar-day window for percentile thresholds (ETCCDI style)

# (event_type, threshold_level) in display order.
EVENT_LEVELS = (
    ("heavy_rain", "moderate"),
    ("heavy_rain", "heavy"),
    ("heavy_rain", "very_heavy"),
    ("hot_day", "local_p90"),
    ("hot_day", "fixed_35"),
    ("cold_day", "local_p10"),
    ("cold_day", "ice_day"),
)


def daily_weather(hourly: pd.DataFrame, timezone: str) -> pd.DataFrame:
    """Daily precipitation sum and max/min/mean temperature per cell and local date.

    Precipitation stamped at hour t fell during the preceding hour, so it counts toward the
    local date of t - 1 hour. Days with fewer than 23 hours are dropped.
    """
    local = hourly.timestamp_utc.dt.tz_convert(timezone)
    temperature = hourly.assign(local_date=local.dt.tz_localize(None).dt.normalize())
    rain_time = (hourly.timestamp_utc - pd.Timedelta(hours=1)).dt.tz_convert(timezone)
    rain = hourly.assign(local_date=rain_time.dt.tz_localize(None).dt.normalize())
    temps = temperature.groupby(["weather_cell_id", "local_date"]).agg(
        tmax=("temperature_2m", "max"),
        tmin=("temperature_2m", "min"),
        tmean=("temperature_2m", "mean"),
        temp_hours=("temperature_2m", "count"),
    )
    precip = rain.groupby(["weather_cell_id", "local_date"]).agg(
        precip_mm=("precipitation", "sum"), rain_hours=("precipitation", "count")
    )
    table = temps.join(precip, how="inner").reset_index()
    complete = (table.temp_hours >= MIN_HOURS) & (table.rain_hours >= MIN_HOURS)
    return table[complete].drop(columns=["temp_hours", "rain_hours"]).reset_index(drop=True)


def _calendar_day(dates: pd.Series) -> np.ndarray:
    """Day of a non-leap year (1-365); 29 February maps to 28 February."""
    day = dates.dt.dayofyear.to_numpy()
    leap_after_feb = dates.dt.is_leap_year.to_numpy() & (dates.dt.month.to_numpy() > 2)
    return np.where(leap_after_feb | (dates.dt.strftime("%m-%d").to_numpy() == "02-29"), day - 1, day)


def thresholds(daily: pd.DataFrame, base_start: str, base_end: str) -> pd.DataFrame:
    """Per cell and calendar day: tmax p90 and tmin p10 over a +/-2-day window."""
    rows = []
    for cell, group in daily.groupby("weather_cell_id"):
        base = group[(group.local_date >= base_start) & (group.local_date <= base_end)]
        days = _calendar_day(base.local_date)
        tmax, tmin = base.tmax.to_numpy(), base.tmin.to_numpy()
        for day in range(1, 366):
            distance = np.abs(days - day)
            window = np.minimum(distance, 365 - distance) <= WINDOW_DAYS
            rows.append(
                {
                    "weather_cell_id": cell,
                    "calendar_day": day,
                    "tmax_p90": float(np.percentile(tmax[window], 90)),
                    "tmin_p10": float(np.percentile(tmin[window], 10)),
                }
            )
    table = pd.DataFrame(rows)
    wet = daily[(daily.precip_mm >= 1) & daily.local_date.between(base_start, base_end)]
    p95 = wet.groupby("weather_cell_id").precip_mm.quantile(0.95).rename("wet_p95")
    return table.merge(p95, left_on="weather_cell_id", right_index=True)


def events(daily: pd.DataFrame, limits: pd.DataFrame) -> pd.DataFrame:
    """Long table of event days: weather_cell_id, local_date, event_type, threshold_level."""
    table = daily.assign(calendar_day=_calendar_day(daily.local_date)).merge(
        limits, on=["weather_cell_id", "calendar_day"]
    )
    flags = {
        ("heavy_rain", "moderate"): table.precip_mm >= 10,
        ("heavy_rain", "heavy"): table.precip_mm >= 20,
        ("heavy_rain", "very_heavy"): table.precip_mm > table.wet_p95,
        ("hot_day", "local_p90"): table.tmax > table.tmax_p90,
        ("hot_day", "fixed_35"): table.tmax >= 35,
        ("cold_day", "local_p10"): table.tmin < table.tmin_p10,
        ("cold_day", "ice_day"): table.tmax < 0,
    }
    frames = [
        table.loc[mask, ["weather_cell_id", "local_date"]].assign(
            event_type=event, threshold_level=level
        )
        for (event, level), mask in flags.items()
    ]
    return pd.concat(frames, ignore_index=True)


def monthly_normals(daily: pd.DataFrame, base_start: str, base_end: str) -> pd.DataFrame:
    """Mean daily temperature per cell and calendar month over the base period."""
    base = daily[daily.local_date.between(base_start, base_end)]
    return (
        base.assign(month=base.local_date.dt.month)
        .groupby(["weather_cell_id", "month"], as_index=False)
        .agg(normal_tmean=("tmean", "mean"))
    )
