"""Deterministic sample tables shaped like the serving tables the pages read.

The values are generated placeholders, not Yelp or weather results. Volumes are scaled to
the order of magnitude seen in the Philadelphia Coffee & Tea check-ins (tens per week per
ZIP code), so layouts and caveats can be judged against realistic numbers.
"""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from sitesense.analysis import label_of_max

SEED = 20261024
METRO = "Philadelphia, PA"
CATEGORY = "Coffee & Tea"
MODEL_VERSION = "sample-v0"
FIRST_DATE = pd.Timestamp("2014-01-01")
LAST_DATE = pd.Timestamp("2022-01-19")
WEATHER_CELL = "era5_160_-301"


@dataclass(frozen=True)
class _Area:
    area_id: str
    name: str
    lat: float
    lon: float
    weekly: float  # average weekly check-ins, 2015-2021
    growth: float  # yearly drift of the area's share
    stars: float
    competitors: int
    businesses: int
    sensitivity: float  # multiplier on metro weather effects
    season: str
    cell_km: float


_AREAS = (
    _Area("19107", "Center City East", 39.9505, -75.1590, 81, 0.01, 4.1, 61, 100, 1.0, "flat", 9.1),
    _Area("19103", "Rittenhouse", 39.9520, -75.1740, 49, 0.02, 4.3, 48, 91, 0.6, "flat", 8.3),
    _Area("19106", "Old City", 39.9490, -75.1450, 40, 0.05, 4.2, 37, 53, 1.3, "summer", 9.9),
    _Area("19147", "Queen Village", 39.9360, -75.1530, 36, 0.03, 4.4, 22, 98, 0.9, "flat", 10.6),
    _Area(
        "19104", "University City", 39.9590, -75.1960, 24, -0.02, 4.0, 42, 95, 1.2, "academic", 6.9
    ),
    _Area(
        "19123", "Northern Liberties", 39.9640, -75.1460, 14, 0.06, 4.3, 19, 35, 0.9, "summer", 9.6
    ),
    _Area("19125", "Fishtown", 39.9760, -75.1260, 13, 0.08, 4.5, 15, 25, 1.0, "summer", 10.9),
    _Area("19102", "Center City West", 39.9530, -75.1660, 13, -0.01, 3.9, 55, 29, 0.8, "flat", 8.7),
    _Area("19146", "Graduate Hospital", 39.9390, -75.1800, 12, 0.04, 4.4, 24, 36, 1.0, "flat", 8.9),
    _Area("19130", "Fairmount", 39.9670, -75.1740, 10, 0.03, 4.4, 18, 31, 1.1, "summer", 7.3),
)

# Metro-wide yearly level relative to 2014, shaped like the decline in Yelp check-in use.
_YEAR_LEVEL = {
    2014: 1.0, 2015: 1.05, 2016: 0.93, 2017: 0.77, 2018: 0.62,
    2019: 0.56, 2020: 0.19, 2021: 0.21, 2022: 0.20,
}  # fmt: skip
_MONTH = {
    "flat": (0.86, 0.88, 0.97, 1.04, 1.09, 1.05, 0.98, 0.95, 1.06, 1.07, 1.02, 1.03),
    "summer": (0.72, 0.74, 0.90, 1.02, 1.17, 1.23, 1.20, 1.15, 1.06, 0.98, 0.86, 0.97),
    "academic": (0.97, 1.03, 1.05, 1.08, 0.92, 0.62, 0.58, 0.75, 1.18, 1.22, 1.14, 0.80),
}
_WEEKDAY = {
    "default": (0.90, 0.92, 0.95, 0.97, 1.05, 1.25, 1.10),
    "academic": (1.12, 1.15, 1.15, 1.12, 1.02, 0.80, 0.70),
}
# Local-hour profile shaped like coffee-shop check-ins (peak around noon).
_HOURS = np.array(
    [1040, 585, 417, 195, 190, 688, 2434, 7928, 14045, 15560, 18527, 20413, 23716, 22246,
     19194, 16510, 14239, 12511, 10435, 9007, 7777, 5640, 3762, 2137],
    dtype="float64",
)  # fmt: skip
_HOURS /= _HOURS.sum()

# (event_type, threshold_level, effect_pct, ci_low, ci_high, n_events): rough metro-level
# magnitudes from an early feasibility check, used here only as placeholder values.
_EFFECTS = (
    ("heavy_rain", "moderate", -8.9, -12.4, -5.2, 194),
    ("heavy_rain", "heavy", -15.7, -20.9, -10.2, 77),
    ("heavy_rain", "very_heavy", -19.5, -27.0, -12.0, 41),
    ("hot_day", "local_p90", -1.2, -4.3, 2.0, 183),
    ("hot_day", "fixed_35", -4.8, -10.7, 2.8, 11),
    ("cold_day", "local_p10", 0.0, -3.0, 3.0, 180),
    ("cold_day", "ice_day", -4.5, -10.9, 1.8, 59),
    ("snowfall", "light", -21.0, -30.5, -11.5, 31),
    ("snowfall", "heavy", -27.7, -42.0, -13.8, 16),
)


def _p_value(effect: float, low: float, high: float) -> float:
    standard_error = (high - low) / 3.92
    if standard_error <= 0:
        return 1.0
    return math.erfc(abs(effect / standard_error) / math.sqrt(2))


def areas() -> pd.DataFrame:
    """`area` table plus the area's weather grid cell and distance to its centre."""
    return pd.DataFrame(
        {
            "area_id": [a.area_id for a in _AREAS],
            "metro": METRO,
            "name": [a.name for a in _AREAS],
            "centroid_lat": [a.lat for a in _AREAS],
            "centroid_lon": [a.lon for a in _AREAS],
            "business_count": [a.businesses for a in _AREAS],
            "weather_cell_id": WEATHER_CELL,
            "weather_cell_km": [a.cell_km for a in _AREAS],
        }
    )


def activity_hourly() -> pd.DataFrame:
    """`activity_hourly`: check-ins per area, local date and local hour."""
    rng = np.random.default_rng(SEED)
    dates = pd.date_range(FIRST_DATE, LAST_DATE, freq="D")
    years, months, weekdays = dates.year.to_numpy(), dates.month.to_numpy(), dates.weekday
    mean_level = np.mean([_YEAR_LEVEL[y] for y in range(2015, 2022)])
    frames = []
    for area in _AREAS:
        daily_base = area.weekly / 7 / mean_level
        drift = np.array([(1 + area.growth) ** (y - 2017) for y in years])
        level = np.array([_YEAR_LEVEL[y] for y in years]) * drift
        season = np.array(_MONTH[area.season])[months - 1]
        week = np.array(_WEEKDAY["academic" if area.season == "academic" else "default"])
        expected_day = daily_base * level * season * week[weekdays]
        expected_day *= rng.lognormal(0, 0.12, len(dates))
        counts = rng.poisson(np.outer(expected_day, _HOURS))
        frames.append(
            pd.DataFrame(
                {
                    "area_id": area.area_id,
                    "category": CATEGORY,
                    "obs_date": np.repeat(dates.to_numpy(), 24),
                    "hour": np.tile(np.arange(24), len(dates)),
                    "checkins": counts.ravel(),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def _minmax(values: pd.Series) -> pd.Series:
    low, high = float(values.min()), float(values.max())
    if high == low:
        return pd.Series(0.5, index=values.index)
    return (values - low) / (high - low)


def area_factor(activity: pd.DataFrame, effects: pd.DataFrame) -> pd.DataFrame:
    """`area_factor`, derived from the sample activity so all pages stay consistent."""
    study = activity[(activity.obs_date >= "2015-01-01") & (activity.obs_date < "2022-01-01")]
    daily = study.groupby(["area_id", "obs_date"], as_index=False)["checkins"].sum()
    daily["weekday"] = daily.obs_date.dt.weekday
    daily["year"] = daily.obs_date.dt.year
    daily["month"] = daily.obs_date.dt.month
    metro_by_year = daily.groupby("year").checkins.sum()
    rows = []
    heavy = effects[(effects.event_type == "heavy_rain") & (effects.threshold_level == "heavy")]
    for area in _AREAS:
        d = daily[daily.area_id == area.area_id]
        total = float(d.checkins.sum())
        by_year = d.groupby("year").checkins.sum()
        by_month = d.groupby("month").checkins.mean()
        by_weekday = d.groupby("weekday").checkins.sum()
        hourly = study[study.area_id == area.area_id].groupby("hour").checkins.sum()
        rows.append(
            {
                "area_id": area.area_id,
                "category": CATEGORY,
                "avg_weekly_checkins": total / (len(d) / 7),
                "weekend_share": float(by_weekday.loc[[5, 6]].sum()) / total,
                # Change in the area's share of metro check-ins (Yelp use changes yearly).
                "growth_yoy": float(
                    (by_year.loc[2019] / metro_by_year.loc[2019])
                    / (by_year.loc[2018] / metro_by_year.loc[2018])
                    - 1
                ),
                "avg_stars": area.stars,
                "competitor_count": area.competitors,
                "weather_resilience": 1
                + float(heavy.effect_pct[heavy.area_id == area.area_id].iloc[0]) / 100,
                "seasonality_ratio": float(by_month.max() / by_month.min()),
                "peak_month": label_of_max(by_month),
                "peak_hour": label_of_max(hourly),
                "busiest_day": label_of_max(by_weekday),
            }
        )
    table = pd.DataFrame(rows)
    table["demand_norm"] = _minmax(table.avg_weekly_checkins)
    table["growth_norm"] = _minmax(table.growth_yoy)
    table["rating_norm"] = _minmax(table.avg_stars)
    table["competition_norm"] = 1 - _minmax(table.competitor_count.astype("float64"))
    table["resilience_norm"] = _minmax(table.weather_resilience)
    return table


def weather_effect() -> pd.DataFrame:
    """`weather_effect`: metro rows (area_id NULL) and wider-interval per-area rows."""
    rows: list[dict[str, object]] = []
    metro_weekly = sum(a.weekly for a in _AREAS)
    for event, level, effect, low, high, n in _EFFECTS:
        rows.append(
            dict(area_id=None, event_type=event, threshold_level=level, effect_pct=effect,
                 ci_low=low, ci_high=high, n_events=n)
        )  # fmt: skip
        for area in _AREAS:
            widen = math.sqrt(metro_weekly / area.weekly)
            half = (high - low) / 2 * widen
            value = effect * area.sensitivity
            rows.append(
                dict(area_id=area.area_id, event_type=event, threshold_level=level,
                     effect_pct=value, ci_low=value - half, ci_high=value + half, n_events=n)
            )  # fmt: skip
    table = pd.DataFrame(rows)
    table.insert(0, "effect_id", range(1, len(table) + 1))
    table.insert(2, "category", CATEGORY)
    table["p_value"] = [
        _p_value(e, lo, hi)
        for e, lo, hi in zip(table.effect_pct, table.ci_low, table.ci_high, strict=True)
    ]
    return table


def anomaly_response() -> pd.DataFrame:
    """Weekly temperature anomaly vs check-in change (metro), for the scatter chart."""
    rng = np.random.default_rng(SEED + 1)
    anomaly = rng.uniform(-9, 9, 160)
    change = 2.5 + 0.6 * anomaly - 0.09 * anomaly**2 + rng.normal(0, 3, anomaly.size)
    return pd.DataFrame(
        {
            "area_id": None,
            "category": CATEGORY,
            "week_start": pd.date_range("2015-01-05", periods=anomaly.size, freq="W-MON"),
            "temp_anomaly_c": anomaly,
            "checkin_change_pct": change,
        }
    )


def category_trend() -> pd.DataFrame:
    """`category_trend`: yearly review counts and shares per area."""
    rng = np.random.default_rng(SEED + 2)
    rows = []
    for area in _AREAS:
        for year in range(2017, 2022):
            reviews = area.weekly * 9 * _YEAR_LEVEL[year] * (1 + area.growth * 3) ** (year - 2017)
            rows.append(
                {
                    "area_id": area.area_id,
                    "category": CATEGORY,
                    "year": year,
                    "review_count": int(reviews * rng.uniform(0.9, 1.1)),
                    "new_businesses": int(max(0, rng.poisson(1 + area.growth * 30))),
                    "avg_stars": round(area.stars + rng.normal(0, 0.08), 2),
                }
            )
    table = pd.DataFrame(rows)
    table["review_share"] = table.review_count / table.groupby("year").review_count.transform("sum")
    return table


def weekly_history(activity: pd.DataFrame) -> pd.DataFrame:
    """Weekly check-ins per area for complete Monday-Sunday weeks."""
    daily = activity.groupby(["area_id", "obs_date"], as_index=False).agg(
        checkins=("checkins", "sum")
    )
    daily["week_start"] = daily.obs_date - pd.to_timedelta(daily.obs_date.dt.weekday, unit="D")
    weekly = daily.groupby(["area_id", "week_start"], as_index=False).agg(
        checkins=("checkins", "sum"), days=("obs_date", "count")
    )
    return weekly[weekly.days == 7].drop(columns="days").reset_index(drop=True)


def forecast(activity: pd.DataFrame) -> pd.DataFrame:
    """`forecast`: 12 weeks after the last complete week, typical season only."""
    weekly = weekly_history(activity)
    rows = []
    for area in _AREAS:
        history = weekly[weekly.area_id == area.area_id].sort_values("week_start")
        level = float(history.checkins.tail(8).mean())
        last_month = int(history.week_start.iloc[-1].month)
        start = history.week_start.iloc[-1] + pd.Timedelta(weeks=1)
        season = _MONTH[area.season]
        for offset in range(1, 13):
            month = int((start + pd.Timedelta(weeks=offset - 1)).month)
            yhat = level * season[month - 1] / season[last_month - 1]
            spread = 1.28 * math.sqrt(max(yhat, 1)) * 1.4
            rows.append(
                {
                    "area_id": area.area_id,
                    "category": CATEGORY,
                    "week_offset": offset,
                    "week_start": start + pd.Timedelta(weeks=offset - 1),
                    "model_version": MODEL_VERSION,
                    "yhat": yhat,
                    "lo80": max(0.0, yhat - spread),
                    "hi80": yhat + spread,
                }
            )
    return pd.DataFrame(rows)


def forecast_driver() -> pd.DataFrame:
    """`forecast_driver`: share of the forecast explained by each driver."""
    rng = np.random.default_rng(SEED + 3)
    drivers = ("Seasonality", "Recent 8-week trend", "Weekday mix", "Weather", "Holidays")
    base = np.array([41, 24, 14, 13, 8], dtype="float64")
    rows = []
    for area in _AREAS:
        shares = base * rng.uniform(0.8, 1.2, base.size)
        shares = shares / shares.sum() * 100
        rows += [
            {
                "area_id": area.area_id,
                "category": CATEGORY,
                "driver": driver,
                "model_version": MODEL_VERSION,
                "share_pct": share,
            }
            for driver, share in zip(drivers, shares, strict=True)
        ]
    return pd.DataFrame(rows)


def model_run() -> pd.DataFrame:
    """`model_run`: backtest accuracy against a same-week-last-year baseline."""
    return pd.DataFrame(
        [
            {
                "model_version": MODEL_VERSION,
                "model_type": "Seasonal model + weather (sample)",
                "trained_at": pd.Timestamp("2026-10-07"),
                "train_end": pd.Timestamp("2018-12-31"),
                "mae": 3.1,
                "mape": 7.8,
                "baseline_mae": 3.8,
                "baseline_mape": 9.6,
            }
        ]
    )
