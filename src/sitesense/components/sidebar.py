"""Shared sidebar filters, kept in `st.session_state` so every page sees the same values."""

from dataclasses import dataclass
from datetime import date

import streamlit as st

from sitesense import queries

DEFAULT_RANGE = (date(2015, 1, 1), date(2021, 12, 31))


@dataclass(frozen=True)
class Filters:
    metro: str
    category: str
    start: date
    end: date

    @property
    def years(self) -> range:
        return range(self.start.year, self.end.year + 1)


def render_sidebar() -> None:
    first, last = queries.data_window()
    with st.sidebar:
        st.caption("FILTERS · ALL PAGES")
        metro = st.selectbox("Metro area", queries.metros(), key="metro")
        st.selectbox(
            "Business category",
            queries.categories(str(metro)),
            key="category",
            help="Categories are listed by number of businesses in the selected city.",
        )
        st.date_input(
            "Date range",
            value=(max(DEFAULT_RANGE[0], first), min(DEFAULT_RANGE[1], last)),
            min_value=first,
            max_value=last,
            key="date_range",
            help="Applies to activity charts. Scores, weather effects and the typical year use "
            "their own precomputed study periods.",
        )
        st.divider()
        st.caption(
            "Data: Yelp Open Dataset (academic use) · Weather: Copernicus ERA5 data "
            "redistributed by Open-Meteo (CC BY 4.0)"
        )


def current_filters() -> Filters:
    """Read the sidebar values; stop the page with a message when they are unusable."""
    metro = st.session_state.get("metro") or queries.metros()[0]
    options = queries.categories(metro)
    category = st.session_state.get("category")
    if not options:
        st.warning("No published data for this city yet.")
        st.stop()
    if category not in options:
        category = options[0]
    selected = st.session_state.get("date_range", DEFAULT_RANGE)
    if len(selected) != 2:
        st.info("Pick an end date for the date range in the sidebar.")
        st.stop()
    return Filters(metro, category, selected[0], selected[1])
