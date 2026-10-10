"""Shared filters and native navigation for the five SiteSense screens."""

from datetime import date
from pathlib import Path

import psycopg
import streamlit as st

from sitesense import repository
from sitesense.analytics import summarize_areas
from sitesense.database import ConfigurationError
from sitesense.models import (
    BUSINESS_CATEGORY_LABELS,
    CITY_SCOPES,
    ActivityRecord,
    Catalog,
    Filters,
)
from sitesense.ui import ScreenContext, render_setup


@st.cache_data(ttl=300, max_entries=16, show_spinner=False)
def _categories(city: str, state: str, revision: str) -> tuple[str, ...]:
    available = repository.list_categories(city, state)
    return tuple(category for category in BUSINESS_CATEGORY_LABELS if category in available)


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _catalog(city: str, state: str, category: str, revision: str) -> Catalog:
    return repository.load_catalog(city, state, category)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def _activity(filters: Filters, revision: str) -> tuple[ActivityRecord, ...]:
    return repository.load_activity(filters)


def _reset_area_selections(scope: tuple[str, str]) -> None:
    if st.session_state.get("area_scope") != scope:
        for key in (
            "ranking_area",
            "activity_area",
            "weather_area",
            "forecast_area",
            "compare_areas",
        ):
            st.session_state.pop(key, None)
        st.session_state["area_scope"] = scope


def _shared_filters() -> tuple[Filters, Catalog, str]:
    with st.sidebar:
        st.subheader("Explore locations", icon=":material/tune:")
        city = st.selectbox("Metro area", [scope.city for scope in CITY_SCOPES], key="city")
        scope = next(scope for scope in CITY_SCOPES if scope.city == city)
        revision = repository.get_import_revision()
        categories = _categories(scope.city, scope.state, revision)
        if not categories:
            raise repository.DataUnavailableError(
                "Import Restaurant or Spa data for this city before exploring locations."
            )
        if st.session_state.get("category") not in categories:
            st.session_state["category"] = categories[0]
        category = st.selectbox(
            "Business category",
            categories,
            format_func=lambda value: BUSINESS_CATEGORY_LABELS[value],
            key="category",
        )
        st.caption("Restaurant: Yelp Restaurants · Spa: Yelp Day Spas")
        _reset_area_selections((city, category))
        catalog = _catalog(city, scope.state, category, revision)
        coverage = catalog.coverage
        default_start = max(coverage.start_date, date(2015, 1, 1))
        default_end = min(coverage.end_date, date(2021, 12, 31))
        if default_start > default_end:
            default_start, default_end = coverage.start_date, coverage.end_date
        current_dates = st.session_state.get("date_range", (default_start, default_end))
        if len(current_dates) == 2:
            start = min(max(current_dates[0], coverage.start_date), coverage.end_date)
            end = min(max(current_dates[1], start), coverage.end_date)
            st.session_state["date_range"] = (start, end)
        dates = st.date_input(
            "Historical date range",
            min_value=coverage.start_date,
            max_value=coverage.end_date,
            key="date_range",
        )
        if not isinstance(dates, tuple) or len(dates) != 2:
            st.info("Select both the start and end date.")
            st.stop()
        radius = st.slider(
            "Nearby-business radius (km)",
            min_value=0.5,
            max_value=5.0,
            value=1.0,
            step=0.5,
            key="radius_km",
            help="Changes nearby-category counts only. ZIP membership and activity stay the same.",
        )
        st.divider()
        st.caption("Data sources")
        st.markdown("**Yelp archive** · listed businesses and recorded check-ins")
        st.caption(
            f"Imported coverage: {coverage.start_date:%Y-%m-%d} to {coverage.end_date:%Y-%m-%d}. "
            "Ratings and operating status describe the business snapshot."
        )
        st.caption(
            f"Check-in dates/hours: assumed source-local calendar labels ({scope.timezone}); "
            "source timestamps have no verified timezone."
        )
        st.caption("Weather match: spatial mapping to ERA5 cells, not daily weather completeness.")
    return Filters(city, scope.state, category, dates[0], dates[1], radius), catalog, revision


def _import_is_unchanged(revision: str) -> bool:
    if repository.get_import_revision() == revision:
        st.session_state["import_load_retries"] = 0
        return True
    _categories.clear()
    _catalog.clear()
    _activity.clear()
    retries = int(st.session_state.get("import_load_retries", 0))
    if retries < 2:
        st.session_state["import_load_retries"] = retries + 1
        st.rerun()
    st.warning("The dataset is still changing during import. Retry when the import has finished.")
    if st.button("Retry after import", key="retry_import", icon=":material/refresh:"):
        st.session_state["import_load_retries"] = 0
        st.rerun()
    return False


def render_app() -> None:
    st.set_page_config(page_title="SiteSense AI", page_icon=":material/storefront:", layout="wide")
    pages = Path(__file__).parent / "app_pages"
    page = st.navigation(
        [
            st.Page(
                pages / "site_ranking.py",
                title="Site ranking",
                icon=":material/location_on:",
                default=True,
            ),
            st.Page(
                pages / "customer_activity.py", title="Customer activity", icon=":material/groups:"
            ),
            st.Page(pages / "weather_impact.py", title="Weather impact", icon=":material/cloud:"),
            st.Page(
                pages / "demand_forecast.py", title="Demand forecast", icon=":material/trending_up:"
            ),
            st.Page(
                pages / "compare_sites.py", title="Compare sites", icon=":material/compare_arrows:"
            ),
        ],
        position="sidebar",
    )
    st.title("SiteSense AI")
    st.caption("Location. Weather. Activity. · Three-city research workspace")
    st.header(page.title, icon=page.icon)
    st.session_state.setdefault(
        "score_weights",
        {
            "activity": 35.0,
            "growth": 20.0,
            "competition": 15.0,
            "rating": 15.0,
            "weather": 15.0,
        },
    )
    try:
        filters, catalog, revision = _shared_filters()
        with st.spinner("Loading recorded activity…"):
            records = _activity(filters, revision)
        if not _import_is_unchanged(revision):
            return
        summaries = summarize_areas(catalog, records, filters)
    except ConfigurationError:
        render_setup("configuration")
        return
    except psycopg.errors.UndefinedTable:
        render_setup("migration")
        return
    except repository.DataUnavailableError:
        render_setup("import")
        return
    except psycopg.Error:
        render_setup("connection")
        return
    st.session_state["screen_context"] = ScreenContext(filters, catalog, records, summaries)
    st.caption(
        f"{filters.city}, {filters.state} · {filters.category_label} · "
        f"{filters.start_date:%Y-%m-%d} to {filters.end_date:%Y-%m-%d}"
    )
    page.run()
    st.divider()
    st.caption("Check-ins are an activity proxy. Weather scenarios do not prove causal effects.")
