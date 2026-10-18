"""Use recorded coverage as the boundary for individual-business projections."""

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import streamlit as st

from sitesense import business_forecast, location_businesses
from sitesense.business_forecast import BusinessProjection

if TYPE_CHECKING:
    from sitesense.ui import ScreenContext


@dataclass(frozen=True)
class ProjectionResult:
    interval: tuple[date, date] | None
    projections: tuple[BusinessProjection, ...] = ()
    issue: str | None = None
    model_revision: str | None = None


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
    """Verify the existing model and limit its output to this catalog's exact niche."""
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
        # Reuse the same provenance-verified cache as the archived planning view.
        from sitesense.business_future_views import _bundle

        directory = business_forecast.MODEL_DIR
        revision = hashlib.sha256(
            (directory / "manifest.json").read_bytes()
            + (directory / "businesses.json").read_bytes()
            + business_forecast.code_sha256().encode()
            + business_forecast.lock_sha256().encode()
        ).hexdigest()
        bundle = _bundle(revision, context.import_revision)
        from sitesense.app import _import_is_unchanged

        if not _import_is_unchanged(context.import_revision):
            st.stop()
        filters = context.filters
        projections = business_forecast.project(
            bundle,
            filters.city,
            filters.state,
            filters.category,
            filters.source_category,
            *interval,
        )
        return ProjectionResult(
            interval,
            tuple(row for row in projections if row.group.business_id in eligible),
            model_revision=revision,
        )
    except (OSError, ValueError):
        return ProjectionResult(
            interval,
            issue=(
                "Individual-business projections are unavailable or do not match the current data."
            ),
        )


def daily_projection(result: ProjectionResult, business_id: str) -> list[dict[str, Any]]:
    """Expose each supported business's own calendar-day estimates for charting."""
    if result.interval is None:
        return []
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
