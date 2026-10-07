"""Shared sidebar filters, kept in `st.session_state` so every page sees the same values."""

from dataclasses import dataclass
from datetime import date

import streamlit as st

from sitesense import queries

DEFAULT_RANGE = (date(2015, 1, 1), date(2021, 12, 31))


@dataclass(frozen=True)
class Filters:
    metro: str
    categories: tuple[str, ...]
    start: date
    end: date


def render_sidebar() -> None:
    with st.sidebar:
        st.caption("FILTERS · ALL PAGES")
        st.selectbox("Metro area", queries.METROS, key="metro")
        st.multiselect(
            "Business category",
            queries.CATEGORIES,
            default=list(queries.CATEGORIES),
            key="categories",
        )
        st.date_input(
            "Date range",
            value=DEFAULT_RANGE,
            min_value=queries.DATA_START,
            max_value=queries.DATA_END,
            key="date_range",
            help="Applies to activity charts. Scores, weather effects and forecasts use "
            "their own precomputed study periods.",
        )
        st.divider()
        st.caption(
            "Data: Yelp Open Dataset (academic use) · Weather: Copernicus ERA5 data "
            "redistributed by Open-Meteo (CC BY 4.0)"
        )


def current_filters() -> Filters:
    """Read the sidebar values; stop the page with a message when they are unusable."""
    metro = st.session_state.get("metro", queries.METROS[0])
    categories = tuple(st.session_state.get("categories", queries.CATEGORIES))
    selected = st.session_state.get("date_range", DEFAULT_RANGE)
    if not categories:
        st.warning("Select at least one business category in the sidebar.")
        st.stop()
    if len(selected) != 2:
        st.info("Pick an end date for the date range in the sidebar.")
        st.stop()
    return Filters(metro, categories, selected[0], selected[1])
