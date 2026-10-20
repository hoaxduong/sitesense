"""Use recorded coverage as the boundary for individual-business projections."""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import streamlit as st

from sitesense import business_hgb, business_observed, location_businesses, location_weather
from sitesense.business_forecast import BusinessProjection
from sitesense.models import Filters, WeatherCell

if TYPE_CHECKING:
    from sitesense.ui import ScreenContext


@dataclass(frozen=True)
class ProjectionResult:
    interval: tuple[date, date] | None
    projections: tuple[BusinessProjection, ...] = ()
    issue: str | None = None
    model_revision: str | None = None
    weather_basis: str | None = None
    daily_values: tuple[business_observed.DailyEstimate, ...] = ()


@st.cache_resource(ttl=300, max_entries=4, show_spinner=False)
def _hgb_bundle(directory: str, revision: str, import_revision: str) -> business_hgb.HgbBundle:
    return business_hgb.load_bundle(Path(directory), import_revision)


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _hgb_projection(
    directory: str,
    revision: str,
    import_revision: str,
    filters: Filters,
    interval: tuple[date, date],
    business_ids: tuple[str, ...],
    weather: business_hgb.Weather | None,
    weather_mode: str,
    stations: tuple[WeatherCell, ...],
    business_stations: tuple[tuple[str, str | None], ...],
) -> tuple[tuple[BusinessProjection, ...], tuple[business_observed.DailyEstimate, ...]]:
    bundle = _hgb_bundle(directory, revision, import_revision)
    if weather_mode == "Observed weather":
        observations = location_weather.load_daily_station_weather(stations, *interval)
        return business_observed.project(
            bundle, filters, interval, business_ids, dict(business_stations), observations
        )
    projections = business_hgb.project(
        bundle,
        filters.city,
        filters.state,
        filters.category,
        filters.source_category,
        *interval,
        business_ids,
        weather,
    )
    return projections, ()


def scenario() -> tuple[business_hgb.Weather | None, str]:
    if st.session_state.get("business_weather_mode", "Observed weather") == "Observed weather":
        return None, "HGB · observed station weather"
    if st.session_state.get("business_weather_mode") == "Custom weather":
        values = tuple(
            float(st.session_state.get(key, default))
            for key, default in (
                ("business_weather_low", 15.0),
                ("business_weather_high", 25.0),
                ("business_weather_rain", 0.0),
            )
        )
        weather = (values[0], values[1], values[2])
        business_hgb.validate_weather(weather)
        return weather, (
            f"HGB · custom scenario: {weather[0]:g}–{weather[1]:g} °C, "
            f"{weather[2]:g} mm rain/day · same weather on each selected day"
        )
    return None, "HGB · seasonal weather scenario from 2019–2021 station/month averages"


def projection_caption(result: ProjectionResult) -> str:
    if result.interval is None or not result.weather_basis:
        return ""
    if result.weather_basis == "HGB · observed station weather":
        return (
            "HGB uses real station observations. Weather is recorded; check-ins after the "
            "activity archive are model estimates. A business needs complete weather for "
            "the selected projected period. Check-ins are not customer counts or sales."
        )
    return (
        result.weather_basis + ". This is a scenario, not a live weather forecast. "
        "Check-ins are not customer counts or sales."
    )


def projected_interval(context: "ScreenContext") -> tuple[date, date] | None:
    """Project only selected dates after the imported archive, never covered zeros."""
    filters = context.filters
    coverage_end = context.catalog.coverage.end_date
    if filters.start_date > filters.end_date or filters.end_date <= coverage_end:
        return None
    start = max(filters.start_date, coverage_end + timedelta(days=1))
    return start, filters.end_date


def load_projection(
    context: "ScreenContext", business_ids: tuple[str, ...] | None = None
) -> ProjectionResult:
    """Use HGB for business scenarios; never silently substitute a calendar baseline."""
    interval = projected_interval(context)
    if interval is None:
        return ProjectionResult(None)
    if (interval[1] - interval[0]).days + 1 > 3660:
        return ProjectionResult(interval, issue="Choose at most 3,660 projected calendar days.")
    eligible = {
        business.business_id
        for business in location_businesses.scoped_businesses(context.catalog, context.filters)
    }
    if business_ids is not None:
        eligible.intersection_update(business_ids)
    if not eligible:
        return ProjectionResult(interval)
    try:
        weather, basis = scenario()
        directory = business_hgb.MODEL_DIR
        revision = business_hgb.artifact_revision(directory)
        projections, daily = _hgb_projection(
            str(directory),
            revision,
            context.import_revision,
            context.filters,
            interval,
            tuple(sorted(eligible)),
            weather,
            st.session_state.get("business_weather_mode", "Observed weather"),
            context.catalog.weather_cells,
            tuple(
                sorted(
                    (business.business_id, business.weather_cell_id)
                    for business in context.catalog.businesses
                    if business.business_id in eligible
                )
            ),
        )
        from sitesense.app import _import_is_unchanged

        if not _import_is_unchanged(context.import_revision):
            st.stop()
        return ProjectionResult(
            interval,
            tuple(row for row in projections if row.group.business_id in eligible),
            issue=(
                "No HGB estimates are available for these businesses and dates. "
                "Observed-weather estimates need complete station records for the period. "
                "Try a shorter period or explicitly choose a weather scenario."
                if not projections and basis == "HGB · observed station weather"
                else None
            ),
            model_revision=revision,
            weather_basis=basis,
            daily_values=daily,
        )
    except (OSError, ValueError, psycopg.Error):
        return ProjectionResult(
            interval,
            issue=(
                "HGB business estimates are unavailable. The model may need retraining, "
                "or the weather scenario may be invalid. No average-model substitute is shown."
            ),
        )


def daily_projection(result: ProjectionResult, business_id: str) -> list[dict[str, Any]]:
    """Expose each supported business's own calendar-day estimates for charting."""
    if result.interval is None:
        return []
    if result.weather_basis == "HGB · observed station weather":
        return [
            {"Date": day.isoformat(), "Projected check-ins": value}
            for identifier, day, value in result.daily_values
            if identifier == business_id
        ]
    projection = next(
        (row for row in result.projections if row.group.business_id == business_id), None
    )
    if projection is None:
        return []
    start, end = result.interval
    rows = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        rows.append(
            {
                "Date": day.isoformat(),
                "Projected check-ins": projection.group.rates[(day.month - 1) * 7 + day.weekday()],
            }
        )
    return rows
