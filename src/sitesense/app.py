"""Shared filters and native navigation for populated SiteSense screens."""

from datetime import date
from pathlib import Path

import psycopg
import streamlit as st

from sitesense import exploration, repository
from sitesense.analytics import summarize_areas
from sitesense.database import ConfigurationError
from sitesense.date_filters import (
    CUSTOM,
    DATE_PRESETS,
    OVERVIEW,
    default_activity_range,
    preset_range,
)
from sitesense.models import (
    BUSINESS_CATEGORY_LABELS,
    CITY_SCOPES,
    ActivityRecord,
    Catalog,
    DateCoverage,
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


def _apply_date_preset(coverage: DateCoverage) -> None:
    preset = st.session_state["date_preset"]
    st.session_state.pop("date_range_error", None)
    if preset != CUSTOM:
        st.session_state["date_range"] = preset_range(preset, coverage)
        st.session_state["applied_date_preset"] = preset
    st.session_state["date_range_draft"] = st.session_state["date_range"]


def _apply_custom_dates() -> None:
    dates = st.session_state["date_range_draft"]
    if not isinstance(dates, tuple) or len(dates) != 2:
        st.session_state["date_range_error"] = "Select both the start and end date."
    elif dates[1] < dates[0]:
        st.session_state["date_range_error"] = "The end date must be on or after the start date."
    else:
        st.session_state["date_range"] = dates
        st.session_state["applied_date_preset"] = CUSTOM
        st.session_state.pop("date_range_error", None)


def _cancel_custom_dates() -> None:
    st.session_state["date_preset"] = st.session_state["applied_date_preset"]
    st.session_state["date_range_draft"] = st.session_state["date_range"]
    st.session_state.pop("date_range_error", None)


def _screen_dates(coverage: DateCoverage) -> tuple[date, date]:
    st.session_state.setdefault("date_preset", OVERVIEW)
    st.session_state.setdefault("applied_date_preset", OVERVIEW)
    st.session_state.setdefault("date_range", default_activity_range(coverage))
    preset = st.session_state["date_preset"]
    if preset != CUSTOM:
        # Relative presets follow newly imported data; custom dates stay untouched.
        st.session_state["date_range"] = preset_range(preset, coverage)
        st.session_state["applied_date_preset"] = preset
    st.selectbox(
        "Date range",
        DATE_PRESETS,
        key="date_preset",
        on_change=_apply_date_preset,
        args=(coverage,),
        persist_state="session",
    )
    if preset == CUSTOM:
        st.session_state.setdefault("date_range_draft", st.session_state["date_range"])
        with st.form("custom_date_range"):
            st.date_input(
                "Start and end dates",
                min_value=date.min,
                max_value=date.max,
                key="date_range_draft",
                persist_state="session",
            )
            with st.container(horizontal=True):
                st.form_submit_button(
                    "Apply", key="date_range_apply", on_click=_apply_custom_dates, type="primary"
                )
                st.form_submit_button(
                    "Cancel", key="date_range_cancel", on_click=_cancel_custom_dates
                )
        st.caption("Edit both dates, then Apply. Results use the applied dates below.")
    if error := st.session_state.get("date_range_error"):
        st.info(error)
    dates = st.session_state["date_range"]
    st.caption(f"Applied dates: {dates[0]:%Y-%m-%d} to {dates[1]:%Y-%m-%d}.")
    st.caption("The same dates apply to Location map and Compare sites.")
    st.caption(
        f"Available check-in dates: {coverage.start_date:%Y-%m-%d} "
        f"to {coverage.end_date:%Y-%m-%d}. Recorded presets end at the latest available date."
    )
    if "forecast" in preset.casefold():
        st.caption(
            "Model estimates fill supported dates after recorded coverage. "
            "ZIPs or dates without model weather inputs remain unavailable."
        )
    return dates[0], dates[1]


def _reset_area_selections(scope: tuple[str, str]) -> None:
    if st.session_state.get("area_scope") != scope:
        if st.session_state.get("area_scope") is not None:
            st.session_state["exploration_notice"] = (
                "City or business category changed. Choose ZIP areas for a new comparison."
            )
        st.session_state["compare_areas"] = []
        st.session_state["reset_map_picks"] = True
        for key in (
            "map_area",
            "pending_map_area",
            "location_last_map_pick",
            "location_table_notice",
        ):
            st.session_state.pop(key, None)
        st.session_state["area_scope"] = scope


def _shared_filters() -> tuple[Filters, Catalog, str]:
    with st.sidebar:
        st.subheader("Explore locations", icon=":material/tune:")
        city = st.selectbox(
            "Metro area",
            [scope.city for scope in CITY_SCOPES],
            key="city",
            persist_state="session",
        )
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
            persist_state="session",
        )
        _reset_area_selections((city, category))
        catalog = _catalog(city, scope.state, category, revision)
        coverage = catalog.coverage
        dates = _screen_dates(coverage)
        radius = st.slider(
            "Nearby-business radius (km)",
            min_value=0.5,
            max_value=5.0,
            value=1.0,
            step=0.5,
            key="radius_km",
            persist_state="session",
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
                    "Locations are mapped to nearby stations. Observation coverage depends "
                    "on the selected dates and station."
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
                pages / "location_map.py",
                title="Location map",
                icon=":material/map:",
                default=True,
            ),
            st.Page(
                pages / "compare_sites.py", title="Compare sites", icon=":material/compare_arrows:"
            ),
        ],
        position="sidebar",
    )
    requested = st.session_state.pop("requested_exploration_page", None)
    if requested in ("location_map", "compare_sites"):
        st.switch_page(pages / f"{requested}.py")
    st.title("SiteSense AI")
    st.caption("Explore locations, activity, and weather.")
    st.header(page.title, icon=page.icon)
    try:
        filters, catalog, revision = _shared_filters()
        records: tuple[ActivityRecord, ...]
        if (
            filters.end_date < catalog.coverage.start_date
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
    exploration.prepare_comparison(summaries)
    st.caption(
        f"{filters.city}, {filters.state} · {filters.category_label} · "
        f"{filters.start_date:%Y-%m-%d} to {filters.end_date:%Y-%m-%d} · "
        f"{filters.radius_km:.1f} km nearby radius"
    )
    if notice := st.session_state.pop("exploration_notice", None):
        st.info(notice)
    if page.title == "Location map":
        with st.container(border=True):
            st.markdown("**Explore a ZIP, then compare your candidates.**")
            st.caption(
                "Inspect a ZIP on the map and add it to comparison, or select ZIPs in "
                "Browse ZIP areas. Compare two to four areas with the same filters."
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
