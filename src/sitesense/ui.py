"""Shared business-screen context, weather caches and safe database setup guidance."""

from dataclasses import dataclass
from datetime import date
from typing import Any

import streamlit as st

from sitesense import repository, zip_boundaries
from sitesense.models import (
    ActivityRecord,
    AreaSummary,
    Catalog,
    Filters,
    StationWeatherSummary,
    WeatherCell,
)


@dataclass(frozen=True)
class ScreenContext:
    filters: Filters
    catalog: Catalog
    records: tuple[ActivityRecord, ...]
    summaries: tuple[AreaSummary, ...]
    import_revision: str = ""


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _station_weather(
    cells: tuple[WeatherCell, ...], start_date: date, end_date: date, revision: str
) -> tuple[StationWeatherSummary, ...]:
    return repository.load_station_weather(cells, start_date, end_date)


@st.cache_data(max_entries=1, show_spinner=False)
def _zip_boundaries() -> dict[str, dict[str, Any]]:
    return zip_boundaries.load_zip_boundaries()


def render_setup(reason: str) -> None:
    messages = {
        "configuration": "Database not configured",
        "migration": "Database schema needs migration",
        "import": "No imported data for this selection",
        "connection": "Database temporarily unavailable",
    }
    with st.container(border=True):
        st.subheader("Connect your data", icon=":material/database:")
        st.info(messages[reason])
        st.markdown(
            "Add a private database connection and load your dataset to explore locations. "
            "Use the setup commands below to prepare your workspace."
        )
        st.code(
            "uv run --frozen --env-file .env python -m sitesense.database migrate\n"
            "uv run --frozen --env-file .env python -m sitesense.import_data",
            language="shell",
        )
        if reason == "connection":
            st.caption("Check the PostgreSQL service and permissions, then retry.")
        st.caption("Connect your data to start exploring locations.")
        if st.button("Retry data connection", icon=":material/refresh:", key="retry_data"):
            st.cache_data.clear()
            st.rerun()
    st.caption("Check-ins are an activity proxy. Weather observations do not prove causal effects.")
