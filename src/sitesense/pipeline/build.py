"""Build the serving tables for every configured city and category."""

import math
from collections.abc import Iterable

import numpy as np
import pandas as pd

from sitesense.pipeline import config, effects, events, sources, weather
from sitesense.pipeline.config import City

# Hand-made, approximate neighbourhood names for well-known ZIP codes; Yelp has none.
ZIP_NAMES = {
    "19102": "Center City West", "19103": "Rittenhouse", "19104": "University City",
    "19106": "Old City", "19107": "Center City East", "19123": "Northern Liberties",
    "19125": "Fishtown", "19130": "Fairmount", "19146": "Graduate Hospital",
    "19147": "Queen Village", "19148": "South Philadelphia", "19122": "Temple / Kensington",
    "70112": "Central Business District", "70130": "Warehouse / Lower Garden District",
    "70116": "French Quarter / Tremé", "70115": "Uptown", "70117": "Bywater / Marigny",
    "70119": "Mid-City", "70118": "Carrollton", "70113": "Central City",
    "46204": "Downtown", "46202": "Fall Creek / Mass Ave", "46203": "Fountain Square",
    "46220": "Broad Ripple", "33602": "Downtown", "33606": "Hyde Park",
    "33609": "Westshore", "33603": "Seminole Heights", "33605": "Ybor City",
    "37203": "Midtown / The Gulch", "37201": "Downtown", "37206": "East Nashville",
    "37204": "12 South / Melrose", "37208": "Germantown / North Nashville",
}  # fmt: skip


def haversine_km(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> np.ndarray:
    p1, p2 = np.radians(lat), math.radians(lat0)
    a = (
        np.sin((p2 - p1) / 2) ** 2
        + np.cos(p1) * math.cos(p2) * np.sin(np.radians(lon0 - lon) / 2) ** 2
    )
    return np.asarray(2 * 6371 * np.arcsin(np.sqrt(a)))


def _window(frame: pd.DataFrame, start: str, end: str) -> pd.DataFrame:
    """Rows of a date-indexed frame between two ISO dates (inclusive)."""
    index = pd.DatetimeIndex(frame.index)
    return frame[(index >= pd.Timestamp(start)) & (index <= pd.Timestamp(end))]


def _minmax(values: pd.Series) -> pd.Series:
    low, high = float(values.min()), float(values.max())
    if not math.isfinite(low) or high == low:
        return pd.Series(0.5, index=values.index)
    return (values - low) / (high - low)


def _areas(shops: pd.DataFrame, mapping: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    valid = shops[shops.postal_code.str.fullmatch(r"\d{5}")]
    counts = valid.postal_code.value_counts()
    keep = valid[valid.postal_code.isin(counts[counts >= config.MIN_BUSINESSES].index)]
    located = keep.merge(mapping, on="business_id", how="left", suffixes=("", "_map"))
    rows = []
    for zip_code, group in located.groupby("postal_code"):
        cell = str(group.weather_cell_id.mode().iloc[0])
        in_cell = group[group.weather_cell_id == cell]
        distance = haversine_km(
            in_cell.latitude.to_numpy(), in_cell.longitude.to_numpy(),
            float(in_cell.weather_latitude.iloc[0]), float(in_cell.weather_longitude.iloc[0]),
        )  # fmt: skip
        rows.append(
            {
                "area_id": str(zip_code),
                "name": ZIP_NAMES.get(str(zip_code), f"ZIP {zip_code}"),
                "centroid_lat": float(group.latitude.mean()),
                "centroid_lon": float(group.longitude.mean()),
                "business_count": len(group),
                "weather_cell_id": cell,
                "weather_cell_km": float(np.median(distance)),
            }
        )
    area = pd.DataFrame(rows).sort_values("area_id").reset_index(drop=True)
    return area, keep[["business_id", "postal_code"]].rename(columns={"postal_code": "area_id"})


def _grid(local_area: pd.DataFrame, business_area: pd.DataFrame, end: pd.Timestamp) -> pd.DataFrame:
    """Dates x areas daily check-ins with explicit zeros; `local_area` already has area_id."""
    counts = local_area.groupby(["date", "area_id"]).size().unstack(fill_value=0)
    dates = pd.date_range(config.ACTIVITY_START, end, freq="D")
    return counts.reindex(index=dates, columns=sorted(business_area.area_id.unique()), fill_value=0)


def _cell_matrix(values: pd.DataFrame, area: pd.DataFrame, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Per-area copy of a (date x cell) boolean matrix, using each area's weather cell."""
    columns = {
        str(a): values[c].reindex(dates, fill_value=False).to_numpy()
        if c in values
        else np.zeros(len(dates), bool)
        for a, c in zip(area.area_id, area.weather_cell_id, strict=True)
    }
    return pd.DataFrame(columns, index=dates)


def _thresholds(city: City, rules: list[events.Rule], flags: dict[tuple[str, str], pd.Series],
                daily: pd.DataFrame, main_cell: str) -> pd.DataFrame:  # fmt: skip
    climate = daily.date.between(config.CLIMATE_START, config.CLIMATE_END) & (
        daily.weather_cell_id == main_cell
    )
    years = len(set(daily.date[climate].dt.year))
    rows: list[dict[str, object]] = []
    for rule in rules:
        flag = flags.get((rule.event_type, rule.threshold_level))
        days = float(flag[climate].sum()) if flag is not None else float("nan")
        rows.append(
            {
                "event_type": rule.event_type,
                "threshold_level": rule.threshold_level,
                "label": rule.label,
                "office": rule.office,
                "rule": f"{rule.variable} {rule.operator} {rule.value} {rule.unit}".strip(),
                "condition_note": rule.note,
                "source_url": rule.source_url,
                "source_status": rule.status,
                "available": flag is not None,
                "days_per_year": days / years if years else float("nan"),
                "share_of_days": days / int(climate.sum()) if flag is not None else float("nan"),
            }
        )
    return pd.DataFrame(rows)


def _effects(grid: pd.DataFrame, area: pd.DataFrame, rules: list[events.Rule],
             cell_flags: dict[tuple[str, str], pd.DataFrame], holidays: set[pd.Timestamp],
             rng: np.random.Generator) -> pd.DataFrame:  # fmt: skip
    study = _window(grid, config.EFFECT_START, config.EFFECT_END)
    dates = pd.DatetimeIndex(study.index)
    per_area = {key: _cell_matrix(m, area, dates) for key, m in cell_flags.items()}
    strict = np.logical_or.reduce([m.to_numpy() for m in per_area.values()]) if per_area else None
    rows: list[dict[str, object]] = []
    for rule in rules:
        key = (rule.event_type, rule.threshold_level)
        base = {"event_type": rule.event_type, "threshold_level": rule.threshold_level}
        if key not in per_area:
            rows.append({**base, "area_id": None, "status": "not_available", "n_events": 0})
            continue
        family = np.logical_or.reduce(
            [m.to_numpy() for k, m in per_area.items() if k[0] == rule.event_type]
        )
        family_frame = pd.DataFrame(family, index=dates, columns=study.columns)
        strict_frame = pd.DataFrame(strict, index=dates, columns=study.columns)
        fam_pairs = effects.pairs(study, per_area[key], family_frame, holidays)
        strict_pairs = effects.pairs(study, per_area[key], strict_frame, holidays)
        scopes: list[tuple[str | None, pd.DataFrame, pd.DataFrame]] = [
            (None, fam_pairs, strict_pairs)
        ]
        scopes += [
            (str(a), fam_pairs[fam_pairs.area_id == a], strict_pairs[strict_pairs.area_id == a])
            for a in study.columns
        ]
        for area_id, fam, stri in scopes:
            f, s = effects.summarize(fam, rng), effects.summarize(stri, rng)
            rows.append(
                {
                    **base, "area_id": area_id, "n_events": int(f["n"]), "effect_pct": f["effect"],
                    "ci_low": f["low"], "ci_high": f["high"], "p_value": f["p"],
                    "n_strict": int(s["n"]), "effect_strict": s["effect"],
                    "ci_low_strict": s["low"], "ci_high_strict": s["high"],
                    "status": effects.status(f, s),
                }
            )  # fmt: skip
    return pd.DataFrame(rows)


def _factors(grid: pd.DataFrame, local_all: pd.DataFrame, local_area: pd.DataFrame,
             shops: pd.DataFrame, business_area: pd.DataFrame, area: pd.DataFrame,
             effect: pd.DataFrame) -> pd.DataFrame:  # fmt: skip
    recent = _window(grid, config.FACTOR_START, config.FACTOR_END)
    season = _window(grid, config.SEASON_START, config.SEASON_END)
    recent_days = pd.DatetimeIndex(recent.index)
    season_months = pd.DatetimeIndex(season.index).month
    grid_years = pd.DatetimeIndex(grid.index).year
    metro_year = local_all.groupby(local_all.date.dt.year).size()
    hours = local_area[local_area.date.between(config.FACTOR_START, config.FACTOR_END)]
    open_shops = shops[shops.is_open == 1]
    heavy = effect[(effect.event_type == "heavy_rain") & (effect.threshold_level == "heavy")]
    metro_heavy = heavy[heavy.area_id.isna()]
    rows: list[dict[str, float | int | str]] = []
    for item in area.to_dict("records"):
        area_id = str(item["area_id"])
        days = recent[area_id]
        weekday = days.groupby(recent_days.weekday).sum().reindex(range(7), fill_value=0)
        monthly = season[area_id].groupby(season_months).mean()
        year = grid[area_id].groupby(grid_years).sum()
        share = year / metro_year.reindex(year.index)
        members = business_area[business_area.area_id == area_id].merge(shops, on="business_id")
        distance = haversine_km(
            open_shops.latitude.to_numpy(), open_shops.longitude.to_numpy(),
            float(item["centroid_lat"]), float(item["centroid_lon"]),
        )  # fmt: skip
        own = heavy[(heavy.area_id == area_id) & heavy.status.isin(["supported", "partial"])]
        use = own if len(own) else metro_heavy
        effect_pct = (
            float(use.effect_pct.iloc[0]) if len(use) and pd.notna(use.effect_pct.iloc[0]) else 0.0
        )
        hourly = hours[hours.area_id == area_id].hour.value_counts()
        s2018 = float(share.get(2018, float("nan")))
        s2019 = float(share.get(2019, float("nan")))
        rows.append(
            {
                "area_id": area_id,
                "avg_weekly_checkins": float(days.sum() / (len(days) / 7)),
                "weekend_share": (
                    float(weekday.loc[[5, 6]].sum() / weekday.sum()) if weekday.sum() else 0.0
                ),
                "growth_yoy": s2019 / s2018 - 1 if s2018 > 0 and math.isfinite(s2019) else 0.0,
                "avg_stars": float(
                    np.average(members.stars, weights=members.review_count.clip(lower=1))
                ),
                "competitor_count": int((distance <= config.COMPETITOR_RADIUS_KM).sum()),
                "weather_resilience": 1 + effect_pct / 100,
                "seasonality_ratio": (
                    float(monthly.max() / monthly.min()) if monthly.min() > 0 else 0.0
                ),
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


def _anomaly(grid: pd.DataFrame, daily: pd.DataFrame, cell: str) -> pd.DataFrame:
    total = grid.sum(axis=1)
    days = pd.DatetimeIndex(total.index)
    in_window = (days >= pd.Timestamp(config.EFFECT_START)) & (
        days <= pd.Timestamp(config.EFFECT_END)
    )
    weekly = total[in_window].resample("W-SUN").sum()
    weekly = weekly.iloc[1:-1]
    values = weekly.to_numpy(dtype="float64")
    expected = np.array([
        np.concatenate([values[max(0, i - 4): i], values[i + 1: i + 5]]).mean()
        if 4 <= i < len(values) - 4 else np.nan
        for i in range(len(values))
    ])  # fmt: skip
    own = daily[daily.weather_cell_id == cell].set_index("date")
    climate = _window(own, config.CLIMATE_START, config.CLIMATE_END)
    normal = climate.tmean.groupby(pd.DatetimeIndex(climate.index).month).mean()
    months = pd.Series(pd.DatetimeIndex(own.index).month, index=own.index)
    anomaly = (own.tmean - months.map(normal)).resample("W-SUN").mean()
    table = pd.DataFrame(
        {
            "week_start": pd.DatetimeIndex(weekly.index) - pd.Timedelta(days=6),
            "temp_anomaly_c": anomaly.reindex(weekly.index).to_numpy(),
            "checkin_change_pct": (values / expected - 1) * 100,
        }
    )
    return table.dropna().reset_index(drop=True)


def _trend(reviews: pd.DataFrame, local_area: pd.DataFrame, business_area: pd.DataFrame,
           category_ids: set[str]) -> pd.DataFrame:  # fmt: skip
    in_category = reviews[reviews.business_id.isin(category_ids)]
    metro_total = in_category.groupby(in_category.date.dt.year).size()
    first_seen = (
        pd.concat(
            [
                in_category.groupby("business_id").date.min(),
                local_area.groupby("business_id").date.min(),
            ]
        )
        .groupby(level=0)
        .min()
    )
    rows = []
    for area_id, ids in business_area.groupby("area_id").business_id:
        own = in_category[in_category.business_id.isin(set(ids))]
        firsts = first_seen.reindex(list(ids)).dropna().dt.year
        for y in config.TREND_YEARS:
            in_year = own[own.date.dt.year == y]
            total = int(metro_total.get(y, 0))
            rows.append(
                {
                    "area_id": str(area_id), "year": y, "review_count": len(in_year),
                    "review_share": len(in_year) / total if total else 0.0,
                    "new_businesses": int((firsts == y).sum()),
                    "avg_stars": float(in_year.stars.mean()) if len(in_year) else float("nan"),
                }
            )  # fmt: skip
    return pd.DataFrame(rows)


def _typical(grid: pd.DataFrame) -> pd.DataFrame:
    """Median and p10-p90 of weekly check-ins per ISO week across the typical (pre-COVID) years."""
    start = grid.index[0] + pd.Timedelta(days=(7 - grid.index[0].weekday()) % 7)
    weekly = grid.loc[start:].resample("W-MON", label="left", closed="left").sum()
    weekly = weekly[weekly.index + pd.Timedelta(days=6) <= grid.index[-1]]
    iso = weekly.index.isocalendar()
    weekly = weekly[iso.year.isin(config.TYPICAL_YEARS).to_numpy() & (iso.week <= 52).to_numpy()]
    week = weekly.index.isocalendar().week.to_numpy()
    rows = []
    for area_id in weekly.columns:
        by_week = pd.Series(weekly[area_id].to_numpy(dtype="float64")).groupby(week)
        stats = pd.DataFrame(
            {
                "median": by_week.median(),
                "p10": by_week.quantile(0.1),
                "p90": by_week.quantile(0.9),
                "years": by_week.count(),
            }
        )
        rows.append(stats.assign(area_id=str(area_id)).rename_axis("week").reset_index())
    return pd.concat(rows, ignore_index=True).astype({"week": "int64", "years": "int64"})


def build_city(city: City, shops_city: pd.DataFrame, checkins_city: pd.DataFrame,
               reviews: pd.DataFrame, categories: Iterable[str],
               rng: np.random.Generator) -> dict[str, pd.DataFrame]:  # fmt: skip
    print(
        f"[{city.metro}] {len(shops_city):,} businesses, {len(checkins_city):,} check-ins",
        flush=True,
    )
    mapping = sources.weather_mapping(set(shops_city.business_id))[
        ["business_id", "weather_cell_id", "weather_latitude", "weather_longitude"]
    ]
    local_time = (
        checkins_city.timestamp.dt.tz_localize(config.CHECKIN_TIMEZONE)
        .dt.tz_convert(city.timezone)
        .dt.tz_localize(None)
    )
    local = pd.DataFrame(
        {"business_id": checkins_city.business_id, "date": local_time.dt.normalize(),
         "hour": local_time.dt.hour}
    )  # fmt: skip
    end = local.date.max() - pd.Timedelta(days=1)  # last day may be partial

    daily = weather.daily_local(sources.hourly_weather(set(mapping.weather_cell_id)), city.timezone)
    wet = weather.wet_day_p95(daily)
    rules = events.load_rules(city)
    flags: dict[tuple[str, str], pd.Series] = {}
    for rule in rules:
        flag = events.flags(rule, daily, wet)
        if flag is not None:
            flags[(rule.event_type, rule.threshold_level)] = flag
    cell_flags = {
        key: daily.assign(flag=flag.to_numpy())
        .pivot(index="date", columns="weather_cell_id", values="flag")
        .fillna(False)
        .astype(bool)
        for key, flag in flags.items()
    }
    main_cell = str(mapping.weather_cell_id.mode().iloc[0])
    holidays = effects.us_federal_holidays(range(2010, 2023))
    out: dict[str, list[pd.DataFrame]] = {
        "weather_threshold": [_thresholds(city, rules, flags, daily, main_cell)]
    }
    for category in categories:
        shops = shops_city[[category in labels for labels in shops_city.categories]]
        if len(shops) < config.MIN_BUSINESSES:
            continue
        area, business_area = _areas(shops, mapping)
        if area.empty:
            continue
        ids = set(business_area.business_id)
        local_area = local[local.business_id.isin(ids)].merge(business_area, on="business_id")
        local_category = local[local.business_id.isin(set(shops.business_id))]
        grid = _grid(local_area, business_area, end)
        activity = grid.stack().reset_index()
        activity.columns = pd.Index(["obs_date", "area_id", "checkins"])
        activity = activity[activity.checkins > 0]
        profile = (
            local_area[local_area.date >= config.ACTIVITY_START]
            .assign(year=lambda t: t.date.dt.year, weekday=lambda t: t.date.dt.weekday)
            .groupby(["area_id", "year", "weekday", "hour"], as_index=False)
            .agg(checkins=("business_id", "size"))
        )
        effect = _effects(grid, area, rules, cell_flags, holidays, rng)
        tables = {
            "area": area,
            "activity_daily": activity,
            "activity_profile": profile,
            "weather_effect": effect,
            "area_factor": _factors(
                grid, local_category, local_area, shops, business_area, area, effect
            ),
            "anomaly_response": _anomaly(grid, daily, main_cell),
            "category_trend": _trend(
                reviews, local_category, business_area, set(shops.business_id)
            ),
            "typical_week": _typical(grid),
        }
        for name, table in tables.items():
            out.setdefault(name, []).append(table.assign(category=category))
        supported = effect[effect.area_id.isna() & (effect.status == "supported")]
        print(f"  {category}: {len(area)} areas, {int(activity.checkins.sum()):,} check-ins, "
              f"{len(supported)} supported metro effects", flush=True)  # fmt: skip
    return {
        name: pd.concat(parts, ignore_index=True).assign(metro=city.metro)
        for name, parts in out.items()
    }


def build(cities: Iterable[City] = config.CITIES,
          categories: Iterable[str] = config.CATEGORIES) -> dict[str, pd.DataFrame]:  # fmt: skip
    cities, categories = list(cities), list(categories)
    rng = np.random.default_rng(config.SEED)
    shops = sources.businesses(cities)
    shops = shops[[any(c in labels for c in categories) for labels in shops.categories]]
    ids = set(shops.business_id)
    print(f"Reading check-ins for {len(ids):,} businesses…", flush=True)
    checkins = sources.checkins(ids)
    print("Reading reviews (first run streams the Yelp archive)…", flush=True)
    reviews = sources.reviews(ids)
    results: dict[str, list[pd.DataFrame]] = {}
    for city in cities:
        city_shops = shops[shops.metro == city.metro]
        city_checkins = checkins[checkins.business_id.isin(set(city_shops.business_id))]
        for name, table in build_city(
            city, city_shops, city_checkins, reviews, categories, rng
        ).items():
            results.setdefault(name, []).append(table)
    return {name: pd.concat(parts, ignore_index=True) for name, parts in results.items()}
