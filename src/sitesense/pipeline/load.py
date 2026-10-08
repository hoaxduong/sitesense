"""Publish built tables to PostgreSQL, replacing each metro's rows in one transaction."""

import json
import math
from typing import Any

import pandas as pd
from psycopg import sql

from sitesense import database
from sitesense.pipeline import config

COLUMNS = {
    "area": ["metro", "category", "area_id", "name", "centroid_lat", "centroid_lon",
             "business_count", "weather_cell_id", "weather_cell_km"],
    "activity_daily": ["metro", "category", "area_id", "obs_date", "checkins"],
    "activity_profile": ["metro", "category", "area_id", "year", "weekday", "hour", "checkins"],
    "area_factor": ["metro", "category", "area_id", "avg_weekly_checkins", "weekend_share",
                    "growth_yoy", "avg_stars", "competitor_count", "weather_resilience",
                    "seasonality_ratio", "peak_month", "peak_hour", "busiest_day", "demand_norm",
                    "growth_norm", "rating_norm", "competition_norm", "resilience_norm"],
    "weather_threshold": ["metro", "event_type", "threshold_level", "label", "office", "rule",
                          "condition_note", "source_url", "source_status", "available",
                          "days_per_year", "share_of_days"],
    "weather_effect": ["metro", "category", "area_id", "event_type", "threshold_level",
                       "n_events", "effect_pct", "ci_low", "ci_high", "p_value", "n_strict",
                       "effect_strict", "ci_low_strict", "ci_high_strict", "status"],
    "anomaly_response": ["metro", "category", "week_start", "temp_anomaly_c", "checkin_change_pct"],
    "category_trend": ["metro", "category", "area_id", "year", "review_count", "review_share",
                       "new_businesses", "avg_stars"],
    "typical_week": ["metro", "category", "area_id", "week", "median", "p10", "p90", "years"],
}  # fmt: skip


INTEGER_COLUMNS = {
    "business_count", "checkins", "year", "weekday", "hour", "competitor_count", "peak_month",
    "peak_hour", "busiest_day", "n_events", "n_strict", "review_count", "new_businesses",
    "week", "years",
}  # fmt: skip
SERVING_DIR = config.ROOT / "data/processed/serving"


def save(tables: dict[str, pd.DataFrame]) -> None:
    """Keep the built tables as Parquet so a failed load never needs a rebuild."""
    SERVING_DIR.mkdir(parents=True, exist_ok=True)
    for name, table in tables.items():
        table.to_parquet(SERVING_DIR / f"{name}.parquet", index=False)


def saved() -> dict[str, pd.DataFrame]:
    return {path.stem: pd.read_parquet(path) for path in sorted(SERVING_DIR.glob("*.parquet"))}


def _clean(value: Any, integer: bool = False) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if integer and isinstance(value, float):
        return int(value)
    if isinstance(value, pd.Timestamp):
        return value.date()
    if hasattr(value, "item"):  # numpy scalar
        return _clean(value.item(), integer)
    return value


def publish(tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    metros = sorted(set(tables["area"].metro))
    written: dict[str, int] = {}
    with database.connect() as connection:
        for name, columns in COLUMNS.items():
            table = tables.get(name, pd.DataFrame(columns=columns))
            target = sql.Identifier("sitesense", name)
            connection.execute(
                sql.SQL("DELETE FROM {} WHERE metro = ANY(%s)").format(target), (metros,)
            )
            copy = sql.SQL("COPY {} ({}) FROM STDIN").format(
                target, sql.SQL(", ").join(map(sql.Identifier, columns))
            )
            integer = [column in INTEGER_COLUMNS for column in columns]
            with connection.cursor() as cursor, cursor.copy(copy) as stream:
                for row in table[columns].itertuples(index=False, name=None):
                    stream.write_row([_clean(v, i) for v, i in zip(row, integer, strict=True)])
            written[name] = len(table)
        settings = {
            "categories": sorted(set(tables["area"].category)),
            "checkin_timezone": config.CHECKIN_TIMEZONE,
            "effect_window": [config.EFFECT_START, config.EFFECT_END],
            "typical_years": list(config.TYPICAL_YEARS),
            "thresholds_file": str(config.THRESHOLDS_FILE.relative_to(config.ROOT)),
        }
        for metro in metros:
            connection.execute(
                "INSERT INTO sitesense.publish_runs (metro, method_version, settings) "
                "VALUES (%s, %s, %s)",
                (metro, config.METHOD_VERSION, json.dumps(settings)),
            )
    return written
