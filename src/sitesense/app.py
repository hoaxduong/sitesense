"""Shared filters and native navigation for populated SiteSense screens."""

from datetime import date
from pathlib import Path

import psycopg
import streamlit as st

from sitesense import repository
from sitesense.analytics import summarize_areas
from sitesense.database import ConfigurationError
from sitesense.date_filters import DATE_PRESETS, default_activity_range, preset_range
from sitesense.models import (
    BUSINESS_CATEGORY_LABELS,
    CITY_SCOPES,
    ActivityRecord,
    Catalog,
    DateCoverage,
    Filters,
)
from sitesense.ui import ScreenContext, render_setup

SCREEN_KEYS = {
    "Site ranking": "ranking",
    "Location map": "location_map",
    "Customer activity": "activity",
    "Weather observations": "weather",
    "Compare sites": "comparison",
}


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


@st.cache_data(ttl=300, max_entries=16, show_spinner=False)
def _weather_coverage(revision: str) -> DateCoverage | None:
    return repository.load_weather_coverage()


def _apply_date_preset(screen: str, coverage: DateCoverage | None) -> None:
    preset = st.session_state[f"{screen}_date_preset"]
    if preset != "Custom" and coverage is not None:
        st.session_state[f"{screen}_date_range"] = preset_range(preset, coverage)


def _mark_custom_range(screen: str) -> None:
    st.session_state[f"{screen}_date_preset"] = "Custom"


def _screen_dates(screen: str, coverage: DateCoverage | None) -> tuple[date, date]:
    range_key = f"{screen}_date_range"
    preset_key = f"{screen}_date_preset"
    st.session_state.setdefault(
        preset_key, "30 days" if screen == "weather" and coverage else "Custom"
    )
    if coverage is None:
        st.session_state[preset_key] = "Custom"
    if range_key not in st.session_state:
        if coverage is not None:
            st.session_state[range_key] = (
                preset_range("30 days", coverage)
                if screen == "weather"
                else default_activity_range(coverage)
            )
        else:
            st.session_state[range_key] = (date.today(), date.today())
    if coverage is not None:
        # Relative presets follow newly imported data; custom dates stay untouched.
        _apply_date_preset(screen, coverage)
    st.pills(
        "Date presets",
        DATE_PRESETS if coverage is not None else ("Custom",),
        key=preset_key,
        required=True,
        on_change=_apply_date_preset,
        args=(screen, coverage),
        persist_state="session",
    )
    dates = st.date_input(
        "Weather date range" if screen == "weather" else "Activity date range",
        min_value=date.min,
        max_value=date.max,
        key=range_key,
        on_change=_mark_custom_range,
        args=(screen,),
        persist_state="session",
    )
    st.caption("Dates are saved for this screen. You can select any custom range.")
    if coverage is not None:
        source = "station report dates" if screen == "weather" else "check-in dates"
        st.caption(
            f"Available {source}: {coverage.start_date:%Y-%m-%d} to {coverage.end_date:%Y-%m-%d}. "
            "Presets end at the latest available date."
        )
        if screen == "weather":
            st.caption(
                "Accepted observations vary by station and variable; report dates can be missing."
            )
    elif screen == "weather":
        st.caption("Station weather is unavailable for this selection.")
    if not isinstance(dates, tuple) or len(dates) != 2:
        st.info("Select both the start and end date.")
        st.stop()
    if dates[1] < dates[0]:
        st.info("The end date must be on or after the start date.")
        st.stop()
    return dates[0], dates[1]


def _reset_area_selections(scope: tuple[str, str]) -> None:
    if st.session_state.get("area_scope") != scope:
        for key in (
            "ranking_area",
            "activity_area",
            "weather_area",
            "compare_areas",
        ):
            st.session_state.pop(key, None)
        st.session_state["area_scope"] = scope


def _shared_filters(screen: str) -> tuple[Filters, Catalog, str]:
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
        _reset_area_selections((city, category))
        catalog = _catalog(city, scope.state, category, revision)
        coverage = catalog.coverage
        date_coverage = (
            _weather_coverage(revision)
            if screen == "weather" and catalog.weather_source in ("acis_station", "mixed")
            else None
            if screen == "weather"
            else coverage
        )
        dates = _screen_dates(screen, date_coverage)
        radius = st.slider(
            "Nearby-business radius (km)",
            min_value=0.5,
            max_value=5.0,
            value=1.0,
            step=0.5,
            key="radius_km",
            help="Changes nearby-category counts only. ZIP membership and activity stay the same.",
        )
        with st.expander("About this data", expanded=False):
            st.markdown("**Yelp archive** · business listings and recorded check-ins")
            st.caption(
                f"Activity coverage: {coverage.start_date:%Y-%m-%d} to "
                f"{coverage.end_date:%Y-%m-%d}. Ratings and operating status describe "
                "the business snapshot."
            )
            st.caption("Restaurant: Yelp Restaurants · Spa: Yelp Day Spas")
            st.caption(
                f"Check-in dates/hours use assumed source-local calendar labels "
                f"({scope.timezone}); the source timezone is unverified."
            )
            if catalog.weather_source == "acis_station":
                st.markdown("**ACIS station observations** · via Climate Explorer")
                st.caption(
                    "Locations are mapped to nearby stations. Observation coverage is shown "
                    "on Weather observations."
                )
            elif catalog.weather_source == "era5":
                st.caption(
                    "Locations are mapped to ERA5 weather cells. Spatial mapping does not "
                    "confirm daily observation coverage."
                )
            else:
                st.caption("Spatial weather mapping does not confirm daily observation coverage.")
            st.caption(
                "Check-ins are an activity proxy. Weather observations do not prove causal effects."
            )
    return Filters(city, scope.state, category, dates[0], dates[1], radius), catalog, revision


def _import_is_unchanged(revision: str) -> bool:
    if repository.get_import_revision() == revision:
        st.session_state["import_load_retries"] = 0
        return True
    _categories.clear()
    _catalog.clear()
    _activity.clear()
    _weather_coverage.clear()
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
            st.Page(pages / "location_map.py", title="Location map", icon=":material/map:"),
            st.Page(
                pages / "customer_activity.py", title="Customer activity", icon=":material/groups:"
            ),
            st.Page(
                pages / "weather_impact.py",
                title="Weather observations",
                icon=":material/cloud:",
            ),
            st.Page(
                pages / "compare_sites.py", title="Compare sites", icon=":material/compare_arrows:"
            ),
        ],
        position="sidebar",
    )
    st.title("SiteSense AI")
    st.caption("Explore locations, activity, and weather.")
    st.header(page.title, icon=page.icon)
    try:
        screen = SCREEN_KEYS[page.title]
        filters, catalog, revision = _shared_filters(screen)
        records: tuple[ActivityRecord, ...]
        if (
            screen == "weather"
            or filters.end_date < catalog.coverage.start_date
            or filters.start_date > catalog.coverage.end_date
        ):
            records = ()
        else:
            with st.spinner("Loading recorded activity…"):
                records = _activity(filters, revision)
        if not _import_is_unchanged(revision):
            return
        summaries = summarize_areas(catalog, records, filters)
    except ConfigurationError:
        render_setup("configuration")
        return
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        render_setup("migration")
        return
    except repository.DataUnavailableError:
        render_setup("import")
        return
    except psycopg.Error:
        render_setup("connection")
        return
    st.session_state["screen_context"] = ScreenContext(
        filters, catalog, records, summaries, revision
    )
    st.caption(
        f"{filters.city}, {filters.state} · {filters.category_label} · "
        f"{filters.start_date:%Y-%m-%d} to {filters.end_date:%Y-%m-%d}"
    )
    page.run()
    if any(cell.source_kind == "acis_station" for cell in catalog.weather_cells):
        try:
            if not _import_is_unchanged(revision):
                return
        except ConfigurationError:
            render_setup("configuration")
            return
        except repository.DataUnavailableError:
            render_setup("import")
            return
        except psycopg.Error:
            render_setup("connection")
            return
