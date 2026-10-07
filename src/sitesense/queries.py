"""Read-only access to the serving tables, one function per table.

Every page reads data through this module. It currently serves the deterministic sample
tables from `sitesense.sample_data`; when the pipeline publishes the serving tables, each
function switches to a SQL query with the same columns, and pages stay unchanged.
"""

from collections.abc import Sequence
from typing import Any

import pandas as pd
import streamlit as st

from sitesense import sample_data

METROS = (sample_data.METRO,)
CATEGORIES = (sample_data.CATEGORY,)
DATA_START = sample_data.FIRST_DATE.date()
DATA_END = sample_data.LAST_DATE.date()


def using_sample_data() -> bool:
    """True while the pages read generated placeholders instead of pipeline tables."""
    return True


@st.cache_resource(show_spinner="Preparing sample data…")
def _tables() -> dict[str, pd.DataFrame]:
    activity = sample_data.activity_hourly()
    effects = sample_data.weather_effect()
    return {
        "area": sample_data.areas(),
        "activity_hourly": activity,
        "weather_effect": effects,
        "area_factor": sample_data.area_factor(activity, effects),
        "anomaly_response": sample_data.anomaly_response(),
        "category_trend": sample_data.category_trend(),
        "weekly_history": sample_data.weekly_history(activity),
        "forecast": sample_data.forecast(activity),
        "forecast_driver": sample_data.forecast_driver(),
        "model_run": sample_data.model_run(),
    }


def _select(name: str, **filters: Any) -> pd.DataFrame:
    table = _tables()[name]
    mask = pd.Series(True, index=table.index)
    for column, value in filters.items():
        if isinstance(value, tuple):
            mask &= table[column].isin(value)
        else:
            mask &= table[column] == value
    return table[mask].reset_index(drop=True)


def _area_ids(metro: str) -> tuple[str, ...]:
    return tuple(_select("area", metro=metro).area_id)


@st.cache_data
def areas(metro: str) -> pd.DataFrame:
    return _select("area", metro=metro)


@st.cache_data
def activity_hourly(metro: str, categories: Sequence[str]) -> pd.DataFrame:
    return _select("activity_hourly", area_id=_area_ids(metro), category=tuple(categories))


@st.cache_data
def area_factors(metro: str, categories: Sequence[str]) -> pd.DataFrame:
    return _select("area_factor", area_id=_area_ids(metro), category=tuple(categories))


@st.cache_data
def weather_effects(metro: str, categories: Sequence[str]) -> pd.DataFrame:
    table = _select("weather_effect", category=tuple(categories))
    keep = table.area_id.isna() | table.area_id.isin(_area_ids(metro))
    return table[keep].reset_index(drop=True)


@st.cache_data
def anomaly_response(metro: str, categories: Sequence[str]) -> pd.DataFrame:
    return _select("anomaly_response", category=tuple(categories))


@st.cache_data
def category_trends(metro: str, categories: Sequence[str]) -> pd.DataFrame:
    return _select("category_trend", area_id=_area_ids(metro), category=tuple(categories))


@st.cache_data
def weekly_history(metro: str, area_id: str) -> pd.DataFrame:
    return _select("weekly_history", area_id=area_id)


@st.cache_data
def forecasts(metro: str, categories: Sequence[str], area_id: str) -> pd.DataFrame:
    return _select("forecast", area_id=area_id, category=tuple(categories))


@st.cache_data
def forecast_drivers(metro: str, categories: Sequence[str], area_id: str) -> pd.DataFrame:
    return _select("forecast_driver", area_id=area_id, category=tuple(categories))


@st.cache_data
def model_runs() -> pd.DataFrame:
    return _select("model_run")
