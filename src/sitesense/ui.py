"""Native Streamlit views populated from imported historical evidence."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import psycopg
import streamlit as st

from sitesense import (
    forecast,
    forecast_cohort,
    forecast_range,
    forecast_ui,
    repository,
    zip_boundaries,
)
from sitesense.database import ConfigurationError
from sitesense.models import (
    ActivityRecord,
    AreaSummary,
    CandidateArea,
    Catalog,
    DateCoverage,
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


@st.cache_data(ttl=300, max_entries=32, show_spinner="Generating forecasts…")
def _forecast_estimates(
    city: str,
    state: str,
    category: str,
    start: date,
    end: date,
    coverage: DateCoverage,
    areas: tuple[CandidateArea, ...],
    import_revision: str,
    artifact_revision: str,
    directory: str,
) -> forecast_range.ForecastRange:
    bundle = forecast_ui._forecast_bundle(directory, artifact_revision)
    return forecast_range.predict_range(
        bundle, Filters(city, state, category, start, end), coverage, areas
    )


def _activity_forecast(context: ScreenContext) -> forecast_range.ForecastRange:
    """Resolve uncovered dates once per scope/model, independently of the focused ZIP."""
    covered = _activity_coverage(context)
    recorded_days = (covered[1] - covered[0]).days + 1 if covered else 0
    uncovered_days = (
        (context.filters.end_date - context.filters.start_date).days + 1 - recorded_days
    )
    areas = tuple(summary.area for summary in context.summaries)
    reason = None
    if uncovered_days:
        if context.filters.source_category is not None:
            reason = (
                "The broad-sector weather model does not estimate source categories. "
                "Use Future planning on Location map for niche calendar scenarios."
            )
        elif context.filters.end_date <= context.catalog.coverage.end_date:
            reason = "Dates before recorded coverage are not forecast periods."
        else:
            try:
                revision = forecast_ui._artifact_revision(forecast.MODEL_DIR)
                return _forecast_estimates(
                    context.filters.city,
                    context.filters.state,
                    context.filters.category,
                    context.filters.start_date,
                    context.filters.end_date,
                    context.catalog.coverage,
                    areas,
                    context.import_revision,
                    revision,
                    str(forecast.MODEL_DIR),
                )
            except forecast.ArtifactUnavailableError:
                reason = "Forecast model unavailable."
    return forecast_range.ForecastRange(
        tuple(forecast_range.AreaForecast(area.id, (), uncovered_days, reason) for area in areas),
        uncovered_days,
        reason=reason,
    )


@st.cache_data(ttl=300, max_entries=32, show_spinner="Loading comparable activity…")
def _recorded_cohort_activity(
    city: str,
    state: str,
    category: str,
    start: date,
    end: date,
    areas: tuple[CandidateArea, ...],
    import_revision: str,
    reference_revision: str,
    directory: str,
    _catalog: Catalog,
) -> tuple[forecast_cohort.CohortReference, tuple[ActivityRecord, ...] | None]:
    """Load one exact model cohort per scope, independently of the focused ZIP.

    The import revision identifies the catalog; excluding its large contents from
    hashing keeps map interactions cheap without reusing a different import.
    """
    filters = Filters(city, state, category, start, end)
    reference = forecast_cohort.load_reference(Path(directory), filters, _catalog, areas)
    if not reference.business_ids:
        return reference, None
    records = repository.load_activity(
        filters, include_previous_year=False, business_ids=reference.business_ids
    )
    return reference, records


def _cohort_activity(
    context: ScreenContext,
) -> tuple[forecast_cohort.CohortReference, tuple[ActivityRecord, ...] | None]:
    """A failed query is unavailable, never a valid zero-activity cohort."""
    if context.filters.source_category is not None:
        return forecast_cohort.CohortReference(
            (), "Broad-sector model cohorts do not represent the selected source category."
        ), None
    covered = _activity_coverage(context)
    if covered is None:
        return forecast_cohort.CohortReference(()), None
    try:
        return _recorded_cohort_activity(
            context.filters.city,
            context.filters.state,
            context.filters.category,
            *covered,
            tuple(summary.area for summary in context.summaries),
            context.import_revision,
            forecast_cohort.artifact_revision(forecast.MODEL_DIR),
            str(forecast.MODEL_DIR),
            context.catalog,
        )
    except (ConfigurationError, psycopg.Error):
        return forecast_cohort.CohortReference(()), None


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


def _context() -> ScreenContext | None:
    context = cast(ScreenContext, st.session_state["screen_context"])
    if not context.summaries:
        st.info(
            "No candidate ZIP areas qualify. A candidate needs at least five listed businesses "
            "in the selected category and source category. Try another selection."
        )
        return None
    return context


def _selected_area(
    context: ScreenContext, key: str, *, on_change: Callable[[], None] | None = None
) -> AreaSummary:
    ids = [summary.area.id for summary in context.summaries]
    labels = {summary.area.id: summary.area.label for summary in context.summaries}
    if st.session_state.get(key) not in ids:
        st.session_state[key] = ids[0]
    area_id = st.selectbox(
        "ZIP area",
        ids,
        format_func=labels.__getitem__,
        key=key,
        persist_state="session",
        on_change=on_change,
    )
    return next(summary for summary in context.summaries if summary.area.id == area_id)


def _activity_coverage(context: ScreenContext) -> tuple[date, date] | None:
    start = max(context.filters.start_date, context.catalog.coverage.start_date)
    end = min(context.filters.end_date, context.catalog.coverage.end_date)
    return (start, end) if start <= end else None


def _activity_coverage_notice(
    context: ScreenContext,
    covered: tuple[date, date] | None,
    estimates: forecast_range.ForecastRange | None = None,
) -> None:
    selected_days = (context.filters.end_date - context.filters.start_date).days + 1
    if estimates is not None and estimates.uncovered_days:
        recorded_days = (covered[1] - covered[0]).days + 1 if covered else 0
        supported = sum(bool(area.points) for area in estimates.areas)
        if supported:
            st.info(
                f"Recorded activity: {recorded_days:,} days. Model estimates cover "
                f"{len(estimates.estimated_dates):,} additional dates for "
                f"{supported} of {len(estimates.areas)} ZIPs. "
                "Forecast coverage varies by ZIP; missing days remain unavailable."
            )
            st.caption(
                "Forecasts show estimated check-ins per 100 historical cohort businesses per day. "
                "Recorded activity is a total check-in count. "
                "Estimates use observed station weather for the target dates."
            )
        else:
            reasons = {area.reason for area in estimates.areas if area.reason}
            detail = estimates.reason or (next(iter(reasons)) if len(reasons) == 1 else None)
            st.info(
                f"Recorded: {recorded_days:,} days; estimated: 0 days; "
                f"unavailable: {selected_days - recorded_days:,} days. "
                + (detail or "No supported forecast is available for these dates and ZIPs.")
            )
        return
    if covered is None:
        st.info(
            "No imported activity covers the selected date range. "
            f"Recorded: 0 days; estimated: 0 days; unavailable: {selected_days:,} days. "
            "Forecast estimates have not been generated. Business snapshots remain available."
        )
    elif covered != (context.filters.start_date, context.filters.end_date):
        recorded_days = (covered[1] - covered[0]).days + 1
        st.info(
            f"Imported activity covers {covered[0]:%Y-%m-%d} to {covered[1]:%Y-%m-%d} "
            f"within this selection. Recorded: {recorded_days:,} days; estimated: 0 days; "
            f"unavailable: {selected_days - recorded_days:,} days. "
            "Forecast estimates have not been generated for uncovered dates. "
            "Activity metrics use only the recorded portion."
        )


def _metrics(values: tuple[tuple[str, str | None], ...]) -> None:
    available = [(label, value) for label, value in values if value is not None]
    if not available:
        return
    with st.container(horizontal=True):
        for label, value in available:
            st.metric(label, value, border=True)
