"""Shared filters and native navigation for populated SiteSense screens."""

from dataclasses import replace
from datetime import date
from pathlib import Path

import psycopg
import streamlit as st

from sitesense import repository
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
    Catalog,
    DateCoverage,
    Filters,
)
from sitesense.ui import ScreenContext, render_setup
from sitesense.use_cases import render_destination


@st.cache_data(ttl=300, max_entries=16, show_spinner=False)
def _categories(city: str, state: str, revision: str) -> tuple[str, ...]:
    available = repository.list_categories(city, state)
    return tuple(category for category in BUSINESS_CATEGORY_LABELS if category in available)


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _catalog(city: str, state: str, category: str, revision: str) -> Catalog:
    return repository.load_catalog(city, state, category)


def _set_date_draft(dates: tuple[date, date]) -> None:
    st.session_state["date_start_draft"], st.session_state["date_end_draft"] = dates


def _apply_date_preset(coverage: DateCoverage) -> None:
    preset = st.session_state["date_preset"]
    st.session_state.pop("date_range_error", None)
    if preset != CUSTOM:
        st.session_state["date_range"] = preset_range(preset, coverage)
    _set_date_draft(st.session_state["date_range"])


def _apply_custom_dates() -> None:
    dates = (st.session_state["date_start_draft"], st.session_state["date_end_draft"])
    if not all(isinstance(day, date) for day in dates):
        st.session_state["date_range_error"] = "Select both the start and end date."
    elif dates[1] < dates[0]:
        st.session_state["date_range_error"] = "The end date must be on or after the start date."
    else:
        st.session_state["date_range"] = dates
        st.session_state["date_preset"] = CUSTOM
        st.session_state.pop("date_range_error", None)


def _screen_dates(coverage: DateCoverage) -> tuple[date, date]:
    st.session_state.setdefault("date_preset", OVERVIEW)
    st.session_state.setdefault("date_range", default_activity_range(coverage))
    preset = st.session_state["date_preset"]
    if preset != CUSTOM:
        # Relative presets follow newly imported data; custom dates stay untouched.
        dates = preset_range(preset, coverage)
        if dates != st.session_state["date_range"]:
            _set_date_draft(dates)
        st.session_state["date_range"] = dates
    st.session_state.setdefault("date_start_draft", st.session_state["date_range"][0])
    st.session_state.setdefault("date_end_draft", st.session_state["date_range"][1])
    st.markdown("**Date range**")
    for label, key in (("Start date", "date_start_draft"), ("End date", "date_end_draft")):
        st.date_input(
            label,
            min_value=date.min,
            max_value=date.max,
            key=key,
            format="DD/MM/YYYY",
            on_change=_apply_custom_dates,
            persist_state="session",
        )
    st.pills(
        "Date preset",
        DATE_PRESETS,
        required=True,
        wrap=True,
        help=(
            "History → forecast shows 30 recorded days and 30 forecast days. "
            "Forecast shows the next 30 days."
        ),
        format_func=lambda value: {
            OVERVIEW: "Overview",
            "Seasonal patterns": "Seasonal patterns",
            "History → forecast preview": "History → forecast",
            "Forecast period preview": "Forecast",
            "Latest recorded 7 days": "Latest 7 days",
            "Latest recorded 30 days": "Latest 30 days",
            "Latest recorded 90 days": "Latest 90 days",
            CUSTOM: "Custom",
        }[value],
        key="date_preset",
        on_change=_apply_date_preset,
        args=(coverage,),
        persist_state="session",
    )
    if error := st.session_state.get("date_range_error"):
        st.info(error)
    dates = st.session_state["date_range"]
    st.caption(f"Applied: {dates[0]:%d %b %Y} – {dates[1]:%d %b %Y}")
    return dates[0], dates[1]


def _reset_area_selections(scope: tuple[str, str, str | None]) -> None:
    if st.session_state.get("area_scope") != scope:
        if st.session_state.get("area_scope") is not None:
            st.session_state["exploration_notice"] = (
                "Metro area, business category or source category changed. "
                "Choose businesses for a new comparison."
            )
        st.session_state["compare_areas"] = []
        st.session_state["business_compare_ids"] = []
        st.session_state.pop("business_compare_scope", None)
        st.session_state.pop("business_map_selected", None)
        st.session_state["reset_map_picks"] = True
        for key in (
            "map_area",
            "map_business",
            "pending_map_business",
            "pending_map_area",
            "location_last_map_pick",
            "location_table_notice",
        ):
            st.session_state.pop(key, None)
        st.session_state["area_scope"] = scope


def _scope_catalog(catalog: Catalog, source_category: str | None) -> Catalog:
    """Keep the parent geography and weather context while restricting its businesses."""
    if source_category is None:
        return catalog
    return replace(
        catalog,
        businesses=tuple(
            business for business in catalog.businesses if source_category in business.categories
        ),
    )


def _shared_filters(*, full_catalog: bool = False) -> tuple[Filters, Catalog, str]:
    with st.sidebar, st.container(gap="xsmall"):
        st.subheader("Filters", icon=":material/tune:")
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
        catalog = _catalog(city, scope.state, category, revision)
        source_categories = sorted(
            {
                tag
                for business in catalog.businesses
                for tag in business.categories
                if tag != category
            }
        )
        source_options = ["All source categories", *source_categories]
        if (
            st.session_state.get("business_source_scope") != (city, category)
            or st.session_state.get("business_source_category") not in source_options
        ):
            st.session_state["business_source_category"] = source_options[0]
            st.session_state["business_source_scope"] = (city, category)
        selected_source = st.selectbox(
            "Source category",
            source_options,
            key="business_source_category",
            persist_state="session",
            help="Exact source label within the selected business category.",
        )
        source_category = None if selected_source == source_options[0] else selected_source
        _reset_area_selections((city, category, source_category))
        coverage = catalog.coverage
        dates = _screen_dates(coverage)
        radius = 1.0
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
            st.caption(
                "The metro filter uses the imported city cohort. Source categories are exact "
                "business tags and may overlap."
            )
            st.caption(
                "The shared date range applies to all pages. Estimates appear only after "
                "recorded coverage ends; covered days with no check-ins stay zero. "
                "HGB estimates use weather scenarios and archived business patterns. "
                "They do not include later market changes."
            )
        if dates[1] > coverage.end_date and not full_catalog:
            st.subheader("Weather inputs", icon=":material/partly_cloudy_day:")
            st.caption("**WBS: US3 · T026 · Weather scenarios · T029 · Forecast interface**")
            mode = st.pills(
                "Weather source",
                ("Observed weather", "Seasonal weather", "Custom weather"),
                default="Observed weather",
                required=True,
                key="business_weather_mode",
                persist_state="session",
            )
            if mode == "Custom weather":
                st.number_input(
                    "Daily low (°C)",
                    value=15.0,
                    min_value=-80.0,
                    max_value=65.0,
                    key="business_weather_low",
                    persist_state="session",
                )
                st.number_input(
                    "Daily high (°C)",
                    value=25.0,
                    min_value=-80.0,
                    max_value=65.0,
                    key="business_weather_high",
                    persist_state="session",
                )
                st.number_input(
                    "Daily rain (mm)",
                    value=0.0,
                    min_value=0.0,
                    max_value=500.0,
                    key="business_weather_rain",
                    persist_state="session",
                )
            st.caption(
                "Observed weather uses real station records where complete. "
                "Check-in and weather histories cover different dates. "
                "For dates without observations, explicitly choose a scenario. "
                "Seasonal weather uses each station's monthly averages from 2019–2021. "
                "Custom weather applies the same values to every selected day. "
                "Seasonal and custom weather are scenarios, not live weather forecasts."
            )
    filters = Filters(city, scope.state, category, dates[0], dates[1], radius, source_category)
    return filters, catalog if full_catalog else _scope_catalog(catalog, source_category), revision


def _import_is_unchanged(revision: str) -> bool:
    if repository.get_import_revision() == revision:
        st.session_state["import_load_retries"] = 0
        return True
    _categories.clear()
    _catalog.clear()
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
                pages / "compare_sites.py",
                title="Compare businesses",
                icon=":material/compare_arrows:",
            ),
            st.Page(
                pages / "business_analysis.py",
                title="Business analysis",
                icon=":material/storefront:",
            ),
            st.Page(
                pages / "about_models.py",
                title="About our models",
                icon=":material/model_training:",
            ),
            st.Page(
                pages / "about_database.py",
                title="About our database",
                icon=":material/database:",
            ),
            st.Page(
                pages / "use_cases.py",
                title="Use cases",
                icon=":material/checklist:",
            ),
        ],
        position="sidebar",
    )
    requested = st.session_state.pop("requested_exploration_page", None)
    if requested in ("location_map", "compare_sites"):
        st.switch_page(pages / f"{requested}.py")
    st.header(page.title, icon=page.icon)
    wbs_labels = {
        "Location map": "US1 · T017 · Ranking and comparison dashboard",
        "Compare businesses": "US1 · T017 · Business comparison dashboard",
        "Business analysis": "US1 · T017 · Business analysis dashboard",
        "About our models": "US3 · T025 · Baseline · T026 · Forecast model · T027 · Evaluation",
        "About our database": "T007 · Schema · T008 · Yelp import · T009 · Weather import · "
        "T010 · Business–station matching",
    }
    if label := wbs_labels.get(page.title):
        st.caption(f"**WBS: {label}**")
    render_destination(page.url_path or "location_map")
    if page.url_path in ("about_models", "about_database", "use_cases"):
        page.run()
        return
    try:
        filters, catalog, revision = _shared_filters(full_catalog=page.title == "Business analysis")
        if not _import_is_unchanged(revision):
            return
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
    st.session_state["screen_context"] = ScreenContext(filters, catalog, (), (), revision)
    scope_caption = (
        f"{filters.city}, {filters.state} · {filters.category_label} · "
        f"{filters.source_category or 'All source categories'} · "
        f"{filters.start_date:%Y-%m-%d} to {filters.end_date:%Y-%m-%d}"
    )
    st.caption(scope_caption)
    if page.title != "Business analysis":
        if notice := st.session_state.pop("exploration_notice", None):
            st.info(notice)
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
