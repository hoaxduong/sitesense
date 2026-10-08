"""Hourly derived variables (NWS heat index and wind chill) and local-time daily weather."""

import numpy as np
import pandas as pd

from sitesense.pipeline import config

MIN_HOURS = 23  # a local day has 23-25 hours around DST changes
FROZEN_PRECIP_MM = 0.1


def heat_index_f(t_f: np.ndarray, rh: np.ndarray) -> np.ndarray:
    """NWS heat index (Rothfusz regression with adjustments; simple formula below 80 F)."""
    simple = 0.5 * (t_f + 61.0 + (t_f - 68.0) * 1.2 + rh * 0.094)
    full = (
        -42.379 + 2.04901523 * t_f + 10.14333127 * rh - 0.22475541 * t_f * rh
        - 6.83783e-3 * t_f**2 - 5.481717e-2 * rh**2 + 1.22874e-3 * t_f**2 * rh
        + 8.5282e-4 * t_f * rh**2 - 1.99e-6 * t_f**2 * rh**2
    )  # fmt: skip
    low = (rh < 13) & (t_f >= 80) & (t_f <= 112)
    full = full - np.where(
        low, ((13 - rh) / 4) * np.sqrt(np.clip((17 - np.abs(t_f - 95)) / 17, 0, None)), 0
    )
    high = (rh > 85) & (t_f >= 80) & (t_f <= 87)
    full = full + np.where(high, ((rh - 85) / 10) * ((87 - t_f) / 5), 0)
    return np.asarray(np.where((simple + t_f) / 2 >= 80, full, simple), dtype="float64")


def wind_chill_f(t_f: np.ndarray, wind_mph: np.ndarray) -> np.ndarray:
    """NWS 2001 wind chill; equals the air temperature outside its validity range."""
    chill = 35.74 + 0.6215 * t_f - 35.75 * wind_mph**0.16 + 0.4275 * t_f * wind_mph**0.16
    return np.asarray(np.where((t_f <= 50) & (wind_mph > 3), chill, t_f), dtype="float64")


def daily_local(hourly: pd.DataFrame, timezone: str) -> pd.DataFrame:
    """Daily weather per cell and local date.

    Temperature-based values use the local date of each hour. Precipitation and snowfall at
    hour t fell during the preceding hour, so they count toward the local date of t - 1 hour.
    "hi2h_f" is the highest heat index sustained for 2 consecutive hours within the day.
    """
    table = hourly.copy()
    t_f = table.temperature_2m.to_numpy() * 9 / 5 + 32
    table["t_f"] = t_f
    table["hi_f"] = heat_index_f(t_f, table.relative_humidity_2m.to_numpy())
    table["wc_f"] = wind_chill_f(t_f, table.wind_speed_10m.to_numpy() * 2.23694)
    table["date"] = table.timestamp_utc.dt.tz_convert(timezone).dt.tz_localize(None).dt.normalize()
    shifted = table.timestamp_utc - pd.Timedelta(hours=1)
    table["rain_date"] = shifted.dt.tz_convert(timezone).dt.tz_localize(None).dt.normalize()
    same_day = (table.weather_cell_id == table.weather_cell_id.shift(-1)) & (
        table.date == table.date.shift(-1)
    )
    table["hi_pair"] = np.where(same_day, np.minimum(table.hi_f, table.hi_f.shift(-1)), np.nan)
    table["frozen"] = (table.precipitation > FROZEN_PRECIP_MM) & (table.t_f <= 32)

    temps = table.groupby(["weather_cell_id", "date"]).agg(
        tmax=("temperature_2m", "max"),
        tmin=("temperature_2m", "min"),
        tmean=("temperature_2m", "mean"),
        hi_max_f=("hi_f", "max"),
        hi2h_f=("hi_pair", "max"),
        wc_min_f=("wc_f", "min"),
        frozen_precip_hours=("frozen", "sum"),
        hours=("temperature_2m", "count"),
    )
    precip = table.groupby(["weather_cell_id", "rain_date"]).agg(
        precip_mm=("precipitation", "sum"),
        swe_mm=("snowfall_water_equivalent", lambda s: s.sum(min_count=1)),
        rain_hours=("precipitation", "count"),
    )
    precip.index = precip.index.set_names(["weather_cell_id", "date"])
    daily = temps.join(precip, how="inner").reset_index()
    complete = (daily.hours >= MIN_HOURS) & (daily.rain_hours >= MIN_HOURS)
    daily = daily[complete].drop(columns=["hours", "rain_hours"]).reset_index(drop=True)
    daily["tmax_f"] = daily.tmax * 9 / 5 + 32
    daily["tmin_f"] = daily.tmin * 9 / 5 + 32
    daily["snow_in"] = daily.swe_mm * config.SNOW_CM_PER_MM_SWE / 2.54
    return daily


def wet_day_p95(daily: pd.DataFrame) -> pd.Series:
    """95th percentile of wet-day precipitation per cell over the climate base period."""
    base = daily[
        daily.date.between(config.CLIMATE_START, config.CLIMATE_END)
        & (daily.precip_mm >= config.WET_DAY_MM)
    ]
    return base.groupby("weather_cell_id").precip_mm.quantile(0.95)
