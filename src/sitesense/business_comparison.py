"""Business-level comparison and shared selections for the location map."""

from dataclasses import replace
from typing import cast

import pandas as pd  # type: ignore[import-untyped]
import psycopg
import streamlit as st

from sitesense import business_analysis as analysis
from sitesense import repository
from sitesense.business_activity import ProjectionResult, load_projection
from sitesense.comparison_weather_views import render_comparison_weather
from sitesense.database import ConfigurationError
from sitesense.models import Business, Filters
from sitesense.ui import ScreenContext, render_setup


def prepare(context: ScreenContext) -> tuple[Business, ...]:
    """Keep comparison IDs inside the exact city, parent and source-tag scope."""
    filters = context.filters
    source = filters.source_category or "All source categories"
    scope = (filters.city, filters.state, filters.category, source)
    businesses = tuple(
        business
        for business in context.catalog.businesses
        if filters.category in business.categories
        and (filters.source_category is None or filters.source_category in business.categories)
    )
    eligible = {business.business_id for business in businesses}
    if st.session_state.get("business_compare_scope") != scope:
        st.session_state.pop("business_compare_ids", None)
        st.session_state.pop("business_compare_search", None)
        st.session_state.pop("business_compare_focus", None)
        st.session_state["business_compare_scope"] = scope
    if "business_compare_ids" in st.session_state:
        st.session_state["business_compare_ids"] = list(
            dict.fromkeys(
                value for value in st.session_state["business_compare_ids"] if value in eligible
            )
        )[:4]
    pending = st.session_state.pop("pending_comparison_business", None)
    if pending in eligible:
        selected = list(get_comparison_ids())
        if pending not in selected and len(selected) < 4:
            selected.append(pending)
            st.session_state["business_compare_ids"] = selected
        st.session_state["business_compare_focus"] = pending
    return businesses


def get_comparison_ids() -> tuple[str, ...]:
    return tuple(st.session_state.get("business_compare_ids", ()))


def toggle_comparison(business_id: str) -> None:
    selected = list(get_comparison_ids())
    if business_id in selected:
        selected.remove(business_id)
    elif len(selected) < 4:
        selected.append(business_id)
    else:
        st.toast("Compare up to four businesses. Remove one before adding another.")
    st.session_state["business_compare_ids"] = selected


def open_comparison() -> None:
    # Let the entry point resolve navigation after widget callbacks have run.
    st.session_state["requested_exploration_page"] = "compare_sites"


def _label(business: Business) -> str:
    return f"{business.name} · {business.business_id}"


def _comparison_selection_changed() -> None:
    if st.session_state.get("business_compare_focus") not in get_comparison_ids():
        st.session_state.pop("business_compare_focus", None)


def render_comparison_selection(context: ScreenContext) -> tuple[str, ...]:
    businesses = prepare(context)
    by_id = {business.business_id: business for business in businesses}
    controls = st.container(horizontal=True, vertical_alignment="bottom", gap="small")
    with controls.container(width=280):
        query = (
            st.text_input(
                "Find businesses to compare",
                key="business_compare_search",
                persist_state="session",
                help="Search business names or IDs within the selected source category.",
            )
            .strip()
            .casefold()
        )
    matches = sorted(
        (
            business
            for business in businesses
            if not query
            or query in business.name.casefold()
            or query in business.business_id.casefold()
        ),
        key=lambda business: (business.name.casefold(), business.business_id),
    )
    options = list(
        dict.fromkeys(
            [*get_comparison_ids(), *(business.business_id for business in matches[:100])]
        )
    )
    if "business_compare_ids" not in st.session_state:
        st.session_state["business_compare_ids"] = []
    selected = controls.multiselect(
        "Businesses to compare (1–4)",
        options,
        key="business_compare_ids",
        max_selections=4,
        persist_state="session",
        on_change=_comparison_selection_changed,
        format_func=lambda value: _label(by_id[value]),
        help="Choose up to four activity lines to compare, or check rows on Location map.",
        wrap=True,
    )
    if len(matches) > 100:
        st.caption("Showing the first 100 name matches. Search to find more businesses.")
    elif query and not matches:
        st.caption("No businesses match this search. Your comparison remains selected.")
    return tuple(selected)


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _activity(filters: Filters, revision: str) -> tuple[analysis.BusinessActivity, ...]:
    return repository.load_business_activity(filters)


def _render_comparison_matrix(
    context: ScreenContext,
    businesses: tuple[Business, ...],
    projection: ProjectionResult,
) -> None:
    """Compare snapshot profiles and activity over the same covered calendar dates."""
    filters = context.filters
    start = max(filters.start_date, context.catalog.coverage.start_date)
    end = min(filters.end_date, context.catalog.coverage.end_date)
    effective = replace(filters, start_date=start, end_date=end) if start <= end else None
    summaries: dict[str, analysis.BusinessSummary] = {}
    if effective is not None:
        try:
            records = _activity(effective, context.import_revision)
        except (ConfigurationError, repository.DataUnavailableError, psycopg.Error):
            st.warning(
                "Business activity summary is unavailable. Profile details remain available."
            )
        else:
            from sitesense.app import _import_is_unchanged

            if not _import_is_unchanged(context.import_revision):
                return
            summaries = {
                row.business.business_id: row
                for row in analysis.summarize_businesses(businesses, records, start, end)
            }
    labels = [
        "Recorded check-ins",
        *(["Projected check-ins"] if projection.interval is not None else []),
        "Days with check-ins",
        "Weekend share (%)",
        "Rating (snapshot)",
        "Reviews (snapshot)",
        "Operating status (snapshot)",
        "Source categories",
    ]
    predicted = {row.group.business_id: row.period_rate for row in projection.projections}
    columns: dict[str, list[str]] = {"Metric": labels}
    for business in businesses:
        summary = summaries.get(business.business_id)
        estimate = predicted.get(business.business_id)
        values = [
            f"{summary.checkin_count:,}" if summary else "Unavailable",
            *(
                [f"{estimate:,.2f}" if estimate is not None else "Unavailable"]
                if projection.interval is not None
                else []
            ),
            f"{summary.active_days:,}" if summary else "Unavailable",
            f"{summary.weekend_share * 100:.1f}"
            if summary is not None and summary.weekend_share is not None
            else "Unavailable",
            f"{business.stars:.1f}" if business.stars is not None else "Unavailable",
            f"{business.review_count:,}",
            "Open" if business.is_open else "Closed",
            ", ".join(business.categories),
        ]
        duplicate_name = (
            sum(other.name.casefold() == business.name.casefold() for other in businesses) > 1
        )
        column = _label(business) if duplicate_name or business.name == "Metric" else business.name
        if column in columns:
            column = _label(business)
        columns[column] = values
    st.subheader("Business comparison")
    periods = [f"Selected: {filters.start_date:%d %b %Y} – {filters.end_date:%d %b %Y}"]
    periods.append(
        f"Recorded: {start:%d %b %Y} – {end:%d %b %Y}"
        if effective is not None
        else "Recorded activity unavailable for these dates"
    )
    if projection.interval is not None:
        periods.append(
            f"Projected: {projection.interval[0]:%d %b %Y} – {projection.interval[1]:%d %b %Y}"
        )
    st.caption(" · ".join(periods))
    st.dataframe(
        pd.DataFrame(columns),
        key="business_comparison_matrix",
        hide_index=True,
        height="content",
        column_config={"Metric": st.column_config.TextColumn(pinned=True)},
        alt="Side-by-side business profiles and activity over matching recorded dates",
    )
    st.caption("Weekend share: percentage of recorded check-ins on Saturday or Sunday.")


def render_business_comparison() -> None:
    context = cast(ScreenContext, st.session_state["screen_context"])
    businesses = prepare(context)
    if not businesses:
        st.info("No businesses match this city, business category and source category.")
        return
    if "business_compare_ids" not in st.session_state:
        filters = context.filters
        start = max(filters.start_date, context.catalog.coverage.start_date)
        end = min(filters.end_date, context.catalog.coverage.end_date)
        defaults = businesses[:2]
        if start <= end:
            effective = replace(filters, start_date=start, end_date=end)
            try:
                records = _activity(effective, context.import_revision)
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
            from sitesense.app import _import_is_unchanged

            if not _import_is_unchanged(context.import_revision):
                return
            summaries = analysis.summarize_businesses(businesses, records, start, end)
            defaults = tuple(row.business for row in summaries[:2])
        st.session_state["business_compare_ids"] = [business.business_id for business in defaults]
    selected = render_comparison_selection(context)
    if not selected:
        st.session_state.pop("business_compare_focus", None)
        st.info("Choose businesses to compare weather and recorded activity.")
        return
    projection = load_projection(context, selected)
    if projection.issue:
        st.info(projection.issue)
    by_id = {business.business_id: business for business in businesses}
    render_comparison_weather(
        context, tuple(by_id[identifier] for identifier in selected), projection
    )
    _render_comparison_matrix(
        context, tuple(by_id[identifier] for identifier in selected), projection
    )
