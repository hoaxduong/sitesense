"""Deterministic sample tables with exactly the shapes of the PostgreSQL serving tables.

Used when no published serving data is available (tests, CI, a fresh clone). The values are
generated placeholders, not Yelp or weather results; volumes are on the scale of Philadelphia
Coffee & Tea check-ins so layouts can be judged against realistic numbers.
"""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

SEED = 20261024
METRO = "Philadelphia, PA"
CATEGORY = "Coffee & Tea"
FIRST_DATE = pd.Timestamp("2014-01-01")
LAST_DATE = pd.Timestamp("2022-01-18")
WEATHER_CELL = "era5_160_-301"
TYPICAL_YEARS = (2016, 2017, 2018, 2019)


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
    _Area("19104", "University City", 39.9590, -75.1960, 24, -0.02, 4.0, 42, 95, 1.2,
          "academic", 6.9),
    _Area("19123", "Northern Liberties", 39.9640, -75.1460, 14, 0.06, 4.3, 19, 35, 0.9,
          "summer", 9.6),
    _Area("19125", "Fishtown", 39.9760, -75.1260, 13, 0.08, 4.5, 15, 25, 1.0, "summer", 10.9),
    _Area("19102", "Center City West", 39.9530, -75.1660, 13, -0.01, 3.9, 55, 29, 0.8, "flat", 8.7),
    _Area("19146", "Graduate Hospital", 39.9390, -75.1800, 12, 0.04, 4.4, 24, 36, 1.0, "flat", 8.9),
    _Area("19130", "Fairmount", 39.9670, -75.1740, 10, 0.03, 4.4, 18, 31, 1.1, "summer", 7.3),
)  # fmt: skip

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
_HOURS = np.array(
    [1040, 585, 417, 195, 190, 688, 2434, 7928, 14045, 15560, 18527, 20413, 23716, 22246,
     19194, 16510, 14239, 12511, 10435, 9007, 7777, 5640, 3762, 2137],
    dtype="float64",
)  # fmt: skip
_HOURS /= _HOURS.sum()

# (event_type, threshold_level, label, effect %, CI low, CI high, n, status, days/yr)
_EVENTS = (
    ("heavy_rain", "moderate", "Heavy rain/snow total, moderate (rain/snow ≥ 10 mm/day)",
     -9.8, -13.2, -6.4, 199, "supported", 36.5),
    ("heavy_rain", "heavy", "Heavy rain/snow total, heavy (rain/snow ≥ 20 mm/day)",
     -17.6, -23.9, -12.0, 73, "supported", 14.1),
    ("heavy_rain", "very_heavy",
     "Heavy rain/snow total, very heavy (rain/snow > local wet-day p95)",
     -21.5, -33.7, -10.9, 28, "supported", 6.0),
    ("ice_day", "ice_day", "Ice day (max temp < 0 °C)", -7.2, -14.7, -0.8, 56, "partial", 12.2),
    ("heat", "advisory",
     "Heat, advisory criteria (heat index ≥ 96 °F to 30 Jun, ≥ 100 °F after, 2 h)",
     -6.0, -10.5, -1.3, 39, "supported", 7.2),
    ("heat", "warning", "Heat, warning criteria (heat index ≥ 105 °F, 2 h)",
     -8.3, -14.0, -5.4, 8, "insufficient", 1.1),
    ("cold", "code_blue", "Cold, Code Blue (wind chill ≤ 20 °F, or rain/snow at ≤ 32 °F)",
     -6.1, -9.5, -2.8, 190, "supported", 41.3),
    ("cold", "advisory", "Cold, advisory criteria (wind chill or temp ≤ 0 °F)",
     -8.3, -15.3, -0.2, 17, "insufficient", 3.6),
    ("snow", "advisory", "Snow, advisory criteria (snowfall ≥ 2 in)",
     -24.9, -42.3, -8.9, 18, "insufficient", 3.3),
)  # fmt: skip


def _p_value(effect: float, low: float, high: float) -> float:
    standard_error = (high - low) / 3.92
    return 1.0 if standard_error <= 0 else math.erfc(abs(effect / standard_error) / math.sqrt(2))


def _hourly() -> pd.DataFrame:
    rng = np.random.default_rng(SEED)
    dates = pd.date_range(FIRST_DATE, LAST_DATE, freq="D")
    years, months, weekdays = dates.year.to_numpy(), dates.month.to_numpy(), dates.weekday
    mean_level = np.mean([_YEAR_LEVEL[y] for y in range(2015, 2022)])
    frames = []
    for area in _AREAS:
        drift = np.array([(1 + area.growth) ** (y - 2017) for y in years])
        level = np.array([_YEAR_LEVEL[y] for y in years]) * drift
        season = np.array(_MONTH[area.season])[months - 1]
        week = np.array(_WEEKDAY["academic" if area.season == "academic" else "default"])
        expected = area.weekly / 7 / mean_level * level * season * week[weekdays]
        expected *= rng.lognormal(0, 0.12, len(dates))
        counts = rng.poisson(np.outer(expected, _HOURS))
        frames.append(
            pd.DataFrame(
                {
                    "area_id": area.area_id,
                    "obs_date": np.repeat(dates.to_numpy(), 24),
                    "hour": np.tile(np.arange(24), len(dates)),
                    "checkins": counts.ravel(),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def _minmax(values: pd.Series) -> pd.Series:
    low, high = float(values.min()), float(values.max())
    return pd.Series(0.5, index=values.index) if high == low else (values - low) / (high - low)


def _scope(table: pd.DataFrame, category: bool = True) -> pd.DataFrame:
    table = table.assign(metro=METRO)
    return table.assign(category=CATEGORY) if category else table


def _area() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "area_id": [a.area_id for a in _AREAS],
            "name": [a.name for a in _AREAS],
            "centroid_lat": [a.lat for a in _AREAS],
            "centroid_lon": [a.lon for a in _AREAS],
            "business_count": [a.businesses for a in _AREAS],
            "weather_cell_id": WEATHER_CELL,
            "weather_cell_km": [a.cell_km for a in _AREAS],
        }
    )


def _threshold() -> pd.DataFrame:
    rows = [
        {"event_type": e, "threshold_level": lvl, "label": label, "office": "sample",
         "rule": "", "condition_note": "", "source_url": "", "source_status": "sample",
         "available": True, "days_per_year": days, "share_of_days": days / 365}
        for e, lvl, label, *_, days in _EVENTS
    ]  # fmt: skip
    return pd.DataFrame(rows)


def _effect() -> pd.DataFrame:
    metro_weekly = sum(a.weekly for a in _AREAS)
    rows: list[dict[str, object]] = []
    for event, level, _, effect, low, high, n, status, _days in _EVENTS:
        scopes: list[tuple[str | None, float, float]] = [(None, 1.0, 1.0)]
        scopes += [(a.area_id, a.sensitivity, math.sqrt(metro_weekly / a.weekly)) for a in _AREAS]
        for area_id, scale, widen in scopes:
            value, half = effect * scale, (high - low) / 2 * widen
            lo, hi = value - half, value + half
            area_status = status
            if area_id is not None and status in ("supported", "partial"):
                area_status = "not_supported" if lo <= 0 <= hi else "partial"
            rows.append(
                {"area_id": area_id, "event_type": event, "threshold_level": level,
                 "n_events": n, "effect_pct": value, "ci_low": lo, "ci_high": hi,
                 "p_value": _p_value(value, lo, hi), "n_strict": n,
                 "effect_strict": value * 1.08, "ci_low_strict": lo * 1.08,
                 "ci_high_strict": hi * 1.08, "status": area_status}
            )  # fmt: skip
    return pd.DataFrame(rows)


def _factor(daily: pd.DataFrame, profile: pd.DataFrame, effect: pd.DataFrame) -> pd.DataFrame:
    recent = daily[daily.obs_date.between("2017-01-01", "2019-12-31")]
    season = daily[daily.obs_date.between("2015-01-01", "2019-12-31")]
    totals = daily.groupby(daily.obs_date.dt.year).checkins.sum()
    heavy = effect[(effect.event_type == "heavy_rain") & (effect.threshold_level == "heavy")]
    rows = []
    for area in _AREAS:
        own = recent[recent.area_id == area.area_id]
        weekday = own.groupby(own.obs_date.dt.weekday).checkins.sum()
        in_area = season[season.area_id == area.area_id]
        monthly = in_area.groupby(in_area.obs_date.dt.month).checkins.mean()
        year = daily[daily.area_id == area.area_id].groupby(daily.obs_date.dt.year).checkins.sum()
        share = year / totals
        hours = profile[profile.area_id == area.area_id].groupby("hour").checkins.sum()
        effect_pct = float(heavy.effect_pct[heavy.area_id == area.area_id].iloc[0])
        rows.append(
            {
                "area_id": area.area_id,
                "avg_weekly_checkins": float(own.checkins.sum()) / (own.obs_date.nunique() / 7),
                "weekend_share": float(weekday.loc[[5, 6]].sum() / weekday.sum()),
                "growth_yoy": float(share.loc[2019] / share.loc[2018] - 1),
                "avg_stars": area.stars,
                "competitor_count": area.competitors,
                "weather_resilience": 1 + effect_pct / 100,
                "seasonality_ratio": float(monthly.max() / monthly.min()),
                "peak_month": int(monthly.to_numpy().argmax()) + 1,
                "peak_hour": int(hours.to_numpy().argmax()),
                "busiest_day": int(weekday.to_numpy().argmax()),
            }
        )
    table = pd.DataFrame(rows)
    table["demand_norm"] = _minmax(table.avg_weekly_checkins)
    table["growth_norm"] = _minmax(table.growth_yoy)
    table["rating_norm"] = _minmax(table.avg_stars)
    table["competition_norm"] = 1 - _minmax(table.competitor_count.astype("float64"))
    table["resilience_norm"] = _minmax(table.weather_resilience)
    return table


def _anomaly() -> pd.DataFrame:
    rng = np.random.default_rng(SEED + 1)
    anomaly = rng.uniform(-9, 9, 160)
    change = 2.5 + 0.6 * anomaly - 0.09 * anomaly**2 + rng.normal(0, 3, anomaly.size)
    weeks = pd.date_range("2015-01-05", periods=anomaly.size, freq="W-MON")
    return pd.DataFrame(
        {"week_start": weeks, "temp_anomaly_c": anomaly, "checkin_change_pct": change}
    )


def _trend() -> pd.DataFrame:
    rng = np.random.default_rng(SEED + 2)
    rows = []
    for area in _AREAS:
        for year in range(2017, 2022):
            reviews = area.weekly * 9 * _YEAR_LEVEL[year] * (1 + area.growth * 3) ** (year - 2017)
            rows.append(
                {"area_id": area.area_id, "year": year,
                 "review_count": int(reviews * rng.uniform(0.9, 1.1)),
                 "new_businesses": int(max(0, rng.poisson(1 + area.growth * 30))),
                 "avg_stars": round(area.stars + rng.normal(0, 0.08), 2)}
            )  # fmt: skip
    table = pd.DataFrame(rows)
    table["review_share"] = table.review_count / table.groupby("year").review_count.transform("sum")
    return table


def _typical(daily: pd.DataFrame) -> pd.DataFrame:
    wide = daily.pivot(index="obs_date", columns="area_id", values="checkins").fillna(0)
    weekly = wide.resample("W-MON", label="left", closed="left").sum()
    iso = pd.DatetimeIndex(weekly.index).isocalendar()
    weekly = weekly[iso.year.isin(TYPICAL_YEARS).to_numpy() & (iso.week <= 52).to_numpy()]
    week = pd.DatetimeIndex(weekly.index).isocalendar().week.to_numpy()
    frames = []
    for area_id in weekly.columns:
        grouped = pd.Series(weekly[area_id].to_numpy(dtype="float64")).groupby(week)
        stats = pd.DataFrame(
            {"median": grouped.median(), "p10": grouped.quantile(0.1),
             "p90": grouped.quantile(0.9), "years": grouped.count()}
        )  # fmt: skip
        frames.append(stats.rename_axis("week").reset_index().assign(area_id=area_id))
    return pd.concat(frames, ignore_index=True)


def tables() -> dict[str, pd.DataFrame]:
    """Every serving table, keyed by its PostgreSQL name."""
    hourly = _hourly()
    daily = hourly.groupby(["area_id", "obs_date"], as_index=False).agg(
        checkins=("checkins", "sum")
    )
    profile = (
        hourly.assign(year=hourly.obs_date.dt.year, weekday=hourly.obs_date.dt.weekday)
        .groupby(["area_id", "year", "weekday", "hour"], as_index=False)
        .agg(checkins=("checkins", "sum"))
    )
    effect = _effect()
    return {
        "area": _scope(_area()),
        "activity_daily": _scope(daily[daily.checkins > 0].reset_index(drop=True)),
        "activity_profile": _scope(profile),
        "area_factor": _scope(_factor(daily, profile, effect)),
        "weather_threshold": _scope(_threshold(), category=False),
        "weather_effect": _scope(effect),
        "anomaly_response": _scope(_anomaly()),
        "category_trend": _scope(_trend()),
        "typical_week": _scope(_typical(daily)),
    }
