"""Read-only access to the serving tables, one function per table.

Pages read data only through this module. When DATABASE_URL points at a database with
published serving tables (`python -m sitesense.pipeline`), queries run against PostgreSQL;
otherwise the deterministic sample tables from `sitesense.sample_data` are served, with the
same columns, so tests and a fresh clone work without a database.
"""

from datetime import date
from typing import Any, LiteralString

import pandas as pd
import psycopg
import streamlit as st
from psycopg import sql

from sitesense import database, sample_data

DATE_COLUMNS = ("obs_date", "week_start")


@st.cache_resource(ttl=60, show_spinner=False)
def _database_ready() -> bool:
    try:
        with database.connect() as connection:
            row = connection.execute(
                "SELECT to_regclass('sitesense.area') IS NOT NULL "
                "AND EXISTS (SELECT 1 FROM sitesense.area)"
            ).fetchone()
    except (database.ConfigurationError, psycopg.Error):
        return False
    return bool(row and row[0])


def using_sample_data() -> bool:
    """True when pages show generated placeholders instead of published Yelp/ERA5 results."""
    return not _database_ready()


@st.cache_resource(show_spinner="Preparing sample data…")
def _sample() -> dict[str, pd.DataFrame]:
    return sample_data.tables()


def _sql(query: sql.Composed | LiteralString, params: tuple[Any, ...]) -> pd.DataFrame:
    with database.connect() as connection, connection.cursor() as cursor:
        cursor.execute(query, params)
        columns = [c.name for c in cursor.description or []]
        table = pd.DataFrame(cursor.fetchall(), columns=columns)
    for column in DATE_COLUMNS:
        if column in table:
            table[column] = pd.to_datetime(table[column])
    return table


def _read(name: str, metro: str, category: str | None = None, **filters: Any) -> pd.DataFrame:
    """All rows of a serving table for a metro (and category), with optional equality filters."""
    if using_sample_data():
        table = _sample()[name]
        mask = table.metro == metro
        if category is not None:
            mask &= table.category == category
        for column, value in filters.items():
            mask &= table[column] == value
        return (
            table[mask].drop(columns=["metro", "category"], errors="ignore").reset_index(drop=True)
        )
    conditions = (
        {"metro": metro} | ({"category": category} if category is not None else {}) | filters
    )
    where = sql.SQL(" AND ").join(
        sql.SQL("{} = %s").format(sql.Identifier(column)) for column in conditions
    )
    query = sql.SQL("SELECT * FROM {} WHERE {}").format(sql.Identifier("sitesense", name), where)
    table = _sql(query, tuple(conditions.values()))
    return table.drop(columns=["metro", "category"], errors="ignore")


@st.cache_data(ttl=600)
def metros() -> tuple[str, ...]:
    if using_sample_data():
        return (sample_data.METRO,)
    return tuple(_sql("SELECT DISTINCT metro FROM sitesense.area ORDER BY metro", ()).metro)


@st.cache_data(ttl=600)
def categories(metro: str) -> tuple[str, ...]:
    if using_sample_data():
        return (sample_data.CATEGORY,)
    table = _sql(
        "SELECT category, sum(business_count) AS n FROM sitesense.area WHERE metro = %s "
        "GROUP BY category ORDER BY n DESC",
        (metro,),
    )
    return tuple(table.category)


@st.cache_data(ttl=600)
def data_window() -> tuple[date, date]:
    if using_sample_data():
        return sample_data.FIRST_DATE.date(), sample_data.LAST_DATE.date()
    row = _sql("SELECT min(obs_date) AS lo, max(obs_date) AS hi FROM sitesense.activity_daily", ())
    return pd.Timestamp(row.lo.iloc[0]).date(), pd.Timestamp(row.hi.iloc[0]).date()


@st.cache_data(ttl=600)
def areas(metro: str, category: str) -> pd.DataFrame:
    return _read("area", metro, category).sort_values("area_id").reset_index(drop=True)


@st.cache_data(ttl=600)
def activity_daily(metro: str, category: str) -> pd.DataFrame:
    """Sparse daily check-ins (days without check-ins are absent; see analysis.fill_daily)."""
    return _read("activity_daily", metro, category)


@st.cache_data(ttl=600)
def activity_profile(metro: str, category: str) -> pd.DataFrame:
    return _read("activity_profile", metro, category)


@st.cache_data(ttl=600)
def area_factors(metro: str, category: str) -> pd.DataFrame:
    return _read("area_factor", metro, category)


@st.cache_data(ttl=600)
def weather_thresholds(metro: str) -> pd.DataFrame:
    return _read("weather_threshold", metro)


@st.cache_data(ttl=600)
def weather_effects(metro: str, category: str) -> pd.DataFrame:
    table = _read("weather_effect", metro, category)
    table["area_id"] = table.area_id.astype("object").where(table.area_id.notna(), None)
    return table


@st.cache_data(ttl=600)
def anomaly_response(metro: str, category: str) -> pd.DataFrame:
    return _read("anomaly_response", metro, category)


@st.cache_data(ttl=600)
def category_trends(metro: str, category: str) -> pd.DataFrame:
    return _read("category_trend", metro, category)


@st.cache_data(ttl=600)
def typical_week(metro: str, category: str, area_id: str) -> pd.DataFrame:
    return _read("typical_week", metro, category, area_id=area_id).sort_values("week")
