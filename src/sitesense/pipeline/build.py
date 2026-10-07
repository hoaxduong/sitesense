"""Build every serving table for one metro and category from the raw sources.

Provisional choices (pending team decisions) are module constants so they are easy to find:
check-in timestamps are treated as UTC, and weather events follow proposed decision #4.
"""

import math
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from sitesense.pipeline import sources, weather
from sitesense.pipeline.sources import Scope

CHECKIN_TIMEZONE = "UTC"  # evidence: UTC gives a coffee-shop daily profile; team to confirm
LOCAL_TIMEZONE = "America/New_York"
MIN_BUSINESSES = 5  # candidate areas: ZIP codes with at least this many businesses
COMPETITOR_RADIUS_KM = 1.0
EFFECT_START, EFFECT_END = "2015-01-01", "2020-02-29"  # pre-COVID study period
FACTOR_START, FACTOR_END = "2017-01-01", "2019-12-31"  # recent pre-COVID years
SEASON_START, SEASON_END = "2015-01-01", "2019-12-31"
THRESHOLD_START, THRESHOLD_END = "2010-01-01", "2021-12-31"  # base period for percentiles
BASELINE_WEEKS = 4
TREND_YEARS = range(2017, 2022)
SEASON_PRIOR = 2000  # check-ins at which an area's own seasonality gets half the weight
MODEL_VERSION = "seasonal-level-v1"
HORIZON = 12
BOOTSTRAP = 1000
SEED = 20261024

# Hand-made, approximate neighbourhood names; Yelp has none.
ZIP_NAMES = {
    "19102": "Center City West", "19103": "Rittenhouse", "19104": "University City",
    "19106": "Old City", "19107": "Center City East", "19123": "Northern Liberties",
    "19125": "Fishtown", "19130": "Fairmount", "19146": "Graduate Hospital",
    "19147": "Queen Village", "19148": "South Philadelphia", "19122": "Temple / Kensington",
    "19145": "Point Breeze / Girard Estates", "19143": "Cedar Park / Kingsessing",
    "19128": "Roxborough / Manayunk", "19127": "Manayunk", "19119": "Mount Airy",
    "19118": "Chestnut Hill", "19121": "Brewerytown", "19144": "Germantown",
    "19111": "Fox Chase", "19149": "Mayfair", "19134": "Port Richmond",
    "19124": "Frankford", "19139": "West Philadelphia", "19131": "Wynnefield",
}  # fmt: skip


def haversine_km(lat1: np.ndarray, lon1: np.ndarray, lat2: float, lon2: float) -> np.ndarray:
    p1, p2 = np.radians(lat1), math.radians(lat2)
    dlat, dlon = p2 - p1, np.radians(lon2 - lon1)
    a = np.sin(dlat / 2) ** 2 + np.cos(p1) * math.cos(p2) * np.sin(dlon / 2) ** 2
    return np.asarray(2 * 6371 * np.arcsin(np.sqrt(a)))


def build_areas(
    business: pd.DataFrame, mapping: pd.DataFrame, scope: Scope
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Candidate areas (ZIP codes) and the business -> area assignment."""
    valid = business[business.postal_code.str.fullmatch(r"\d{5}")]
    counts = valid.postal_code.value_counts()
    keep = valid[valid.postal_code.isin(counts[counts >= MIN_BUSINESSES].index)]
    located = keep.merge(
        mapping[["business_id", "weather_cell_id", "weather_latitude", "weather_longitude"]],
        on="business_id",
        how="left",
    )
    rows = []
    for zip_code, group in located.groupby("postal_code"):
        cell = group.weather_cell_id.mode()
        cell_rows = group[group.weather_cell_id == cell.iloc[0]] if len(cell) else group
        distance = haversine_km(
            cell_rows.latitude.to_numpy(),
            cell_rows.longitude.to_numpy(),
            float(cell_rows.weather_latitude.iloc[0]),
            float(cell_rows.weather_longitude.iloc[0]),
        )
        rows.append(
            {
                "area_id": str(zip_code),
                "metro": scope.metro,
                "name": ZIP_NAMES.get(str(zip_code), f"ZIP {zip_code}"),
                "centroid_lat": float(group.latitude.mean()),
                "centroid_lon": float(group.longitude.mean()),
                "business_count": len(group),
                "weather_cell_id": cell.iloc[0] if len(cell) else None,
                "weather_cell_km": float(np.median(distance)),
            }
        )
    area = pd.DataFrame(rows).sort_values("area_id").reset_index(drop=True)
    return area, keep[["business_id", "postal_code"]].rename(columns={"postal_code": "area_id"})


def localise_checkins(checkin: pd.DataFrame) -> pd.DataFrame:
    """Add local date and hour, treating source timestamps as CHECKIN_TIMEZONE."""
    local = (
        checkin.timestamp.dt.tz_localize(CHECKIN_TIMEZONE)
        .dt.tz_convert(LOCAL_TIMEZONE)
        .dt.tz_localize(None)
    )
    return checkin.assign(obs_date=local.dt.normalize(), hour=local.dt.hour)


def activity_hourly(local: pd.DataFrame, business_area: pd.DataFrame) -> pd.DataFrame:
    rows = local.merge(business_area, on="business_id")
    return (
        rows.groupby(["area_id", "obs_date", "hour"], as_index=False)
        .agg(checkins=("business_id", "size"))
        .astype({"hour": "int64", "checkins": "int64"})
    )


def daily_grid(local: pd.DataFrame, business_area: pd.DataFrame, end: pd.Timestamp) -> pd.DataFrame:
    """Dates x areas matrix of daily check-ins with explicit zeros (2010-01-01 to end)."""
    rows = local.merge(business_area, on="business_id")
    counts = rows.groupby(["obs_date", "area_id"]).size().unstack(fill_value=0)
    dates = pd.date_range("2010-01-01", end, freq="D")
    areas = sorted(business_area.area_id.unique())
    return counts.reindex(index=dates, columns=areas, fill_value=0).astype("float64")


def bootstrap_effect(observed: np.ndarray, expected: np.ndarray, rng: np.random.Generator) -> dict[str, float]:
    """Ratio of totals minus one, with a date-level bootstrap 95% CI and two-sided p-value."""
    effect = observed.sum() / expected.sum() - 1
    picks = rng.integers(0, len(observed), (BOOTSTRAP, len(observed)))
    boot = observed[picks].sum(axis=1) / expected[picks].sum(axis=1) - 1
    low, high = np.percentile(boot, [2.5, 97.5])
    p_value = max(1 / BOOTSTRAP, min(1.0, 2 * min((boot <= 0).mean(), (boot >= 0).mean())))
    return {
        "effect_pct": float(effect * 100),
        "ci_low": float(low * 100),
        "ci_high": float(high * 100),
        "p_value": float(p_value),
    }


def weather_effects(
    grid: pd.DataFrame, area: pd.DataFrame, event_days: pd.DataFrame, rng: np.random.Generator
) -> pd.DataFrame:
    """Event-day check-ins vs the same weekday +/-4 weeks (non-event days), metro and per area."""
    study = grid.loc[EFFECT_START:EFFECT_END]
    position = {date: i for i, date in enumerate(study.index)}
    values = study.to_numpy()
    cell_of = dict(zip(area.area_id, area.weather_cell_id, strict=True))
    any_event = event_days.groupby("weather_cell_id").local_date.apply(set).to_dict()
    pairs: dict[tuple[str, str], list[tuple[pd.Timestamp, str, float, float]]] = {}
    for (cell, event, level), days in event_days.groupby(
        ["weather_cell_id", "event_type", "threshold_level"]
    ):
        blocked = any_event.get(cell, set())
        for area_id in [a for a, c in cell_of.items() if c == cell and a in study.columns]:
            column = study.columns.get_loc(area_id)
            for day in days.local_date:
                if day not in position:
                    continue
                base = [
                    position[d]
                    for k in range(1, BASELINE_WEEKS + 1)
                    for d in (day - pd.Timedelta(weeks=k), day + pd.Timedelta(weeks=k))
                    if d in position and d not in blocked
                ]
                if len(base) < 3:
                    continue
                expected = float(values[base, column].mean())
                if expected > 0:
                    observed = float(values[position[day], column])
                    pairs.setdefault((event, level), []).append((day, area_id, observed, expected))
    rows = []
    for event, level in weather.EVENT_LEVELS:
        table = pd.DataFrame(
            pairs.get((event, level), []), columns=["date", "area_id", "observed", "expected"]
        )
        if table.empty:
            continue
        scopes: list[tuple[str | None, pd.DataFrame]] = [(None, table)]
        scopes += [(str(a), g) for a, g in table.groupby("area_id")]
        for area_id, group in scopes:
            by_date = group.groupby("date")[["observed", "expected"]].sum()
            result = bootstrap_effect(by_date.observed.to_numpy(), by_date.expected.to_numpy(), rng)
            rows.append(
                {"area_id": area_id, "event_type": event, "threshold_level": level,
                 **result, "n_events": len(by_date)}
            )  # fmt: skip
    return pd.DataFrame(rows)


def _minmax(values: pd.Series) -> pd.Series:
    low, high = float(values.min()), float(values.max())
    if high == low:
        return pd.Series(0.5, index=values.index)
    return (values - low) / (high - low)


def area_factors(
    grid: pd.DataFrame,
    local: pd.DataFrame,
    business: pd.DataFrame,
    business_area: pd.DataFrame,
    area: pd.DataFrame,
    effects: pd.DataFrame,
) -> pd.DataFrame:
    recent = grid.loc[FACTOR_START:FACTOR_END]
    season = grid.loc[SEASON_START:SEASON_END]
    # Metro totals include every business in the category, not only candidate areas.
    metro_daily = local.groupby("obs_date").size()
    metro_year = metro_daily.groupby(metro_daily.index.year).sum()
    hours = local.merge(business_area, on="business_id")
    hours = hours[hours.obs_date.between(FACTOR_START, FACTOR_END)]
    open_shops = business[business.is_open == 1]
    metro_heavy = effects[effects.area_id.isna() & (effects.threshold_level == "heavy")]
    rows = []
    for item in area.itertuples():
        area_id = str(item.area_id)
        days = recent[area_id]
        weekday = days.groupby(days.index.weekday).sum()
        monthly = season[area_id].groupby(season.index.month).mean()
        year = grid[area_id].groupby(grid.index.year).sum()
        share = year / metro_year.reindex(year.index)
        stars = business_area[business_area.area_id == area_id].merge(business, on="business_id")
        distance = haversine_km(
            open_shops.latitude.to_numpy(), open_shops.longitude.to_numpy(),
            float(item.centroid_lat), float(item.centroid_lon),
        )  # fmt: skip
        own = effects[
            (effects.area_id == area_id)
            & (effects.event_type == "heavy_rain")
            & (effects.threshold_level == "heavy")
        ]
        use = own if len(own) and own.n_events.iloc[0] >= 20 and own.p_value.iloc[0] < 0.05 else metro_heavy
        hourly = hours[hours.area_id == area_id].hour.value_counts()
        rows.append(
            {
                "area_id": area_id,
                "avg_weekly_checkins": float(days.sum() / (len(days) / 7)),
                "weekend_share": float(weekday.reindex([5, 6]).sum() / weekday.sum()) if weekday.sum() else 0.0,
                "growth_yoy": float(share.loc[2019] / share.loc[2018] - 1) if share.loc[2018] > 0 else 0.0,
                "avg_stars": float(np.average(stars.stars, weights=stars.review_count.clip(lower=1))),
                "competitor_count": int((distance <= COMPETITOR_RADIUS_KM).sum()),
                "weather_resilience": 1 + float(use.effect_pct.iloc[0]) / 100 if len(use) else 1.0,
                "seasonality_ratio": float(monthly.max() / monthly.min()) if monthly.min() > 0 else float("nan"),
                "peak_month": int(monthly.to_numpy().argmax()) + 1,
                "peak_hour": int(hourly.index[0]) if len(hourly) else 12,
                "busiest_day": int(weekday.to_numpy().argmax()),
            }
        )  # fmt: skip
    table = pd.DataFrame(rows)
    table["demand_norm"] = _minmax(table.avg_weekly_checkins)
    table["growth_norm"] = _minmax(table.growth_yoy)
    table["rating_norm"] = _minmax(table.avg_stars)
    table["competition_norm"] = 1 - _minmax(table.competitor_count.astype("float64"))
    table["resilience_norm"] = _minmax(table.weather_resilience)
    return table


def anomaly_response(
    grid: pd.DataFrame, daily_weather: pd.DataFrame, normals: pd.DataFrame, cell: str
) -> pd.DataFrame:
    """Weekly metro temperature anomaly vs check-in change against the +/-4 surrounding weeks."""
    weekly = grid.sum(axis=1).loc[EFFECT_START:EFFECT_END].resample("W-SUN").sum()
    weekly = weekly.iloc[1:-1]  # drop partial edge weeks
    values = weekly.to_numpy()
    neighbours = [
        np.concatenate([values[max(0, i - 4) : i], values[i + 1 : i + 5]]) for i in range(len(values))
    ]  # fmt: skip
    expected = np.array([n.mean() if len(n) == 8 else np.nan for n in neighbours])
    weather_cell = daily_weather[daily_weather.weather_cell_id == cell].merge(
        normals[normals.weather_cell_id == cell].drop(columns="weather_cell_id"),
        left_on=daily_weather.local_date.dt.month.loc[daily_weather.weather_cell_id == cell],
        right_on="month",
    )
    anomaly = (
        weather_cell.assign(anomaly=weather_cell.tmean - weather_cell.normal_tmean)
        .set_index("local_date")
        .anomaly.resample("W-SUN")
        .mean()
    )
    table = pd.DataFrame(
        {
            "week_start": weekly.index - pd.Timedelta(days=6),
            "temp_anomaly_c": anomaly.reindex(weekly.index).to_numpy(),
            "checkin_change_pct": (values / expected - 1) * 100,
        }
    )
    return table.dropna().reset_index(drop=True)


def category_trend(
    reviews: pd.DataFrame, local: pd.DataFrame, business_area: pd.DataFrame
) -> pd.DataFrame:
    year = reviews.date.dt.year
    metro_total = reviews.groupby(year).size()
    first_seen = pd.concat(
        [
            reviews.groupby("business_id").date.min(),
            local.groupby("business_id").timestamp.min(),
        ]
    ).groupby(level=0).min()
    rows = []
    for area_id, ids in business_area.groupby("area_id").business_id:
        own = reviews[reviews.business_id.isin(set(ids))]
        own_year = own.date.dt.year
        firsts = first_seen.reindex(ids).dropna().dt.year
        for y in TREND_YEARS:
            in_year = own[own_year == y]
            rows.append(
                {
                    "area_id": area_id,
                    "year": y,
                    "review_count": len(in_year),
                    "review_share": len(in_year) / metro_total.get(y, 1) if metro_total.get(y) else 0.0,
                    "new_businesses": int((firsts == y).sum()),
                    "avg_stars": float(in_year.stars.mean()) if len(in_year) else float("nan"),
                }
            )  # fmt: skip
    return pd.DataFrame(rows)


def weekly_activity(grid: pd.DataFrame) -> pd.DataFrame:
    """Complete Monday-Sunday weeks per area."""
    start = grid.index[0] + pd.Timedelta(days=(7 - grid.index[0].weekday()) % 7)
    end = grid.index[-1] - pd.Timedelta(days=(grid.index[-1].weekday() + 1) % 7)
    weeks = grid.loc[start:end].resample("W-MON", label="left", closed="left").sum()
    long = weeks.stack().rename("checkins").reset_index()
    long.columns = pd.Index(["week_start", "area_id", "checkins"])
    return long.astype({"checkins": "int64"})


def _season_index(weekly: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Month x area seasonal index (mean 1), shrunk toward the metro pattern for small areas."""
    window = weekly[(weekly.index >= start) & (weekly.index <= end)]
    months = (window.index + pd.Timedelta(days=3)).month
    own = window.groupby(months).mean()
    own = own / own.mean()
    metro = window.sum(axis=1).groupby(months).mean()
    metro = metro / metro.mean()
    weight = window.sum() / (window.sum() + SEASON_PRIOR)
    blended = own.mul(weight, axis=1).add(np.outer(metro, 1 - weight), axis=0).fillna(1.0)
    return blended.reindex(range(1, 13)).fillna(1.0)


def _predict(series: np.ndarray, weeks: pd.DatetimeIndex, origin: int, index: np.ndarray) -> np.ndarray:
    """Recent 8-week level, deseasonalised, times the seasonal index of each target week."""
    recent = slice(origin - 7, origin + 1)
    months = (weeks[recent] + pd.Timedelta(days=3)).month
    level = series[recent].mean() / index[months - 1].mean()
    targets = weeks[origin] + pd.to_timedelta(np.arange(1, HORIZON + 1) * 7 + 3, unit="D")
    return np.asarray(level * index[targets.month - 1])


def forecast_tables(
    weekly_long: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Forecast, drivers and the model-run backtest record."""
    weekly = weekly_long.pivot(index="week_start", columns="area_id", values="checkins").astype(
        "float64"
    )
    weeks = pd.DatetimeIndex(weekly.index)
    # Backtest: season from 2015-2018, origins every 4 weeks through 2019, 12-week horizon.
    back_index = _season_index(weekly, "2015-01-01", "2018-12-31")
    origins = [i for i, w in enumerate(weeks) if pd.Timestamp("2018-12-31") <= w <= pd.Timestamp("2019-10-07")][::4]
    errors, base_errors, actuals, log_residuals = [], [], [], {h: [] for h in range(1, HORIZON + 1)}
    for area_id in weekly.columns:
        series = weekly[area_id].to_numpy()
        index = back_index[area_id].to_numpy()
        for origin in origins:
            prediction = _predict(series, weeks, origin, index)
            actual = series[origin + 1 : origin + 1 + HORIZON]
            baseline = series[origin + 1 - 52 : origin + 1 + HORIZON - 52]
            errors.append(np.abs(prediction - actual))
            base_errors.append(np.abs(baseline - actual))
            actuals.append(actual)
            for h in range(HORIZON):
                log_residuals[h + 1].append(math.log((actual[h] + 1) / (prediction[h] + 1)))
    err, base, act = np.concatenate(errors), np.concatenate(base_errors), np.concatenate(actuals)
    model_run = pd.DataFrame(
        [
            {
                "model_version": MODEL_VERSION,
                "model_type": "Seasonal index × recent 8-week level",
                "trained_at": datetime.now(UTC),
                "train_end": pd.Timestamp("2018-12-31"),
                "mae": float(err.mean()),
                "mape": float(err.sum() / act.sum() * 100),
                "baseline_mae": float(base.mean()),
                "baseline_mape": float(base.sum() / act.sum() * 100),
            }
        ]
    )
    quantiles = {h: np.percentile(r, [10, 90]) for h, r in log_residuals.items()}
    # Final forecast from the last complete week with the 2015-2019 seasonal pattern.
    index_all = _season_index(weekly, SEASON_START, SEASON_END)
    origin = len(weeks) - 1
    forecast_rows, driver_rows = [], []
    for area_id in weekly.columns:
        series = weekly[area_id].to_numpy()
        index = index_all[area_id].to_numpy()
        prediction = _predict(series, weeks, origin, index)
        recent_months = (weeks[origin - 7 : origin + 1] + pd.Timedelta(days=3)).month
        level = series[origin - 7 : origin + 1].mean() / index[recent_months - 1].mean()
        for h in range(1, HORIZON + 1):
            low_q, high_q = quantiles[h]
            yhat = float(prediction[h - 1])
            forecast_rows.append(
                {
                    "area_id": area_id, "week_offset": h,
                    "week_start": weeks[origin] + pd.Timedelta(weeks=h),
                    "model_version": MODEL_VERSION, "yhat": yhat,
                    "lo80": max(0.0, (yhat + 1) * math.exp(low_q) - 1),
                    "hi80": (yhat + 1) * math.exp(high_q) - 1,
                }
            )  # fmt: skip
        driver_rows += [
            {"area_id": area_id, "driver": "Recent level (last 8 weeks)",
             "model_version": MODEL_VERSION, "checkins_per_week": float(level)},
            {"area_id": area_id, "driver": "Time-of-year adjustment",
             "model_version": MODEL_VERSION, "checkins_per_week": float(prediction.mean() - level)},
        ]  # fmt: skip
    return pd.DataFrame(forecast_rows), pd.DataFrame(driver_rows), model_run


def build(scope: Scope) -> dict[str, pd.DataFrame]:
    """Every serving table for the scope, each with metro and category columns."""
    rng = np.random.default_rng(SEED)
    business = sources.businesses(scope)
    ids = set(business.business_id)
    mapping = sources.weather_mapping(ids)
    area, business_area = build_areas(business, mapping, scope)
    local = localise_checkins(sources.checkins(ids))
    grid = daily_grid(local, business_area, local.obs_date.max())
    hourly = sources.hourly_weather(set(area.weather_cell_id.dropna()))
    daily_weather = weather.daily_weather(hourly, LOCAL_TIMEZONE)
    limits = weather.thresholds(daily_weather, THRESHOLD_START, THRESHOLD_END)
    event_days = weather.events(daily_weather, limits)
    normals = weather.monthly_normals(daily_weather, THRESHOLD_START, THRESHOLD_END)
    effects = weather_effects(grid, area, event_days, rng)
    weekly = weekly_activity(grid)
    forecast, drivers, model_run = forecast_tables(weekly)
    main_cell = str(area.weather_cell_id.mode().iloc[0])
    tables = {
        "area": area,
        "activity_hourly": activity_hourly(local, business_area),
        "weekly_activity": weekly,
        "area_factor": area_factors(grid, local, business, business_area, area, effects),
        "weather_effect": effects,
        "anomaly_response": anomaly_response(grid, daily_weather, normals, main_cell),
        "category_trend": category_trend(sources.reviews(scope, ids), local, business_area),
        "forecast": forecast,
        "forecast_driver": drivers,
        "model_run": model_run,
    }
    return {
        name: table.assign(metro=scope.metro, category=scope.category)
        for name, table in tables.items()
    }
