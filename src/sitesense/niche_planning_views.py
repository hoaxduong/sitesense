"""Future calendar scenarios for exact source-category historical cohorts."""

import calendar
import hashlib
import math
from datetime import date, timedelta
from statistics import fmean
from typing import TYPE_CHECKING, cast

import pandas as pd  # type: ignore[import-untyped]
import pydeck as pdk  # type: ignore[import-untyped]
import streamlit as st

from sitesense import niche_forecast

if TYPE_CHECKING:
    from sitesense.ui import ScreenContext


@st.cache_resource(ttl=300, max_entries=4, show_spinner=False)
def _bundle(revision: str, import_revision: str) -> niche_forecast.NicheBundle:
    return niche_forecast.load_bundle(niche_forecast.MODEL_DIR, import_revision)


def _dates(today: date) -> tuple[date, date]:
    period = st.segmented_control(
        "Planning period",
        ("Month", "Day"),
        default="Month",
        required=True,
        key="niche_planning_period",
        persist_state="session",
    )
    if period == "Day":
        stored = st.session_state.get("niche_planning_day")
        if isinstance(stored, date) and not today <= stored <= today + timedelta(days=365):
            st.session_state["niche_planning_day"] = today + timedelta(days=1)
        selected = st.date_input(
            "Future day",
            value=today + timedelta(days=1),
            min_value=today,
            max_value=today + timedelta(days=365),
            key="niche_planning_day",
            persist_state="session",
        )
        if not isinstance(selected, date):
            raise ValueError("Choose a future day.")
        return selected, selected
    first = today.replace(day=1)
    months = tuple(
        date(first.year + (first.month - 1 + offset) // 12, (first.month - 1 + offset) % 12 + 1, 1)
        for offset in range(1, 13)
    )
    if st.session_state.get("niche_planning_month") not in months:
        st.session_state["niche_planning_month"] = months[0]
    month = st.selectbox(
        "Future month",
        months,
        format_func=lambda day: day.strftime("%B %Y"),
        key="niche_planning_month",
        persist_state="session",
    )
    return month, month.replace(day=calendar.monthrange(month.year, month.month)[1])


def category_rows(
    projections: tuple[niche_forecast.NicheProjection, ...],
) -> list[dict[str, object]]:
    """Weight each supported ZIP by its fixed historical cohort, never today's listings."""
    grouped: dict[str, list[niche_forecast.NicheProjection]] = {}
    for row in projections:
        grouped.setdefault(row.group.source_category, []).append(row)
    result: list[dict[str, object]] = []
    for source, rows in grouped.items():
        businesses = sum(row.group.n_businesses for row in rows)
        result.append(
            {
                "Source category": source,
                "Projected check-ins / business / day": sum(
                    row.daily_rate * row.group.n_businesses for row in rows
                )
                / businesses,
                "Projected check-ins / business / period": sum(
                    row.period_rate * row.group.n_businesses for row in rows
                )
                / businesses,
                "Supported ZIPs": len(rows),
                "Historical cohort businesses": businesses,
                "Training check-ins": sum(row.group.training_checkins for row in rows),
            }
        )
    return sorted(
        result,
        key=lambda row: (
            -cast(float, row["Projected check-ins / business / day"]),
            str(row["Source category"]),
        ),
    )


def render(context: "ScreenContext") -> None:
    st.subheader("Future planning")
    st.caption(
        "Choose a future month or day. Compare source categories, then select a source "
        "category in the sidebar to explore ZIP areas in that niche. These controls have "
        "their own planning dates; the sidebar date range applies to recorded analysis."
    )
    start, end = _dates(date.today())
    try:
        manifest = niche_forecast.MODEL_DIR / "manifest.json"
        model = niche_forecast.MODEL_DIR / "groups.json"
        revision = hashlib.sha256(
            manifest.read_bytes()
            + model.read_bytes()
            + niche_forecast.code_sha256().encode()
            + niche_forecast.lock_sha256().encode()
        ).hexdigest()
        bundle = _bundle(revision, context.import_revision)
    except (OSError, niche_forecast.NicheForecastError):
        st.info("The niche calendar model is unavailable or does not match the current data.")
        st.caption("Prepare the niche model outside the app, then reload this page.")
        st.code(
            "uv run --frozen --env-file .env python scripts/train_niche_forecast.py",
            language="shell",
        )
        return
    st.warning(
        "Future calendar scenario: these are projections of archived-cohort check-in "
        "patterns, not verified future demand or a forecast of a new shop's customers or "
        "revenue. Historical check-in data ends in 2022. Later market changes, closures, "
        "rent and future weather are not modeled."
    )
    st.caption(
        f"Planning dates: {start:%Y-%m-%d} to {end:%Y-%m-%d}. "
        "Rates use each model group's fixed historical cohort. "
        "Source categories overlap; their totals cannot be added together."
    )
    if bundle.manifest["model_name"] == "overall_mean":
        st.caption(
            "Forward validation selected the historical mean over the month/weekday model. "
            "The daily projected rate stays constant; period totals scale with calendar days."
        )
    filters = context.filters
    projections = niche_forecast.project(
        bundle, filters.city, filters.state, filters.category, filters.source_category, start, end
    )
    if not projections:
        st.info(
            "No supported historical cohort for this selection. Each modeled ZIP and niche "
            "needs at least five eligible cohort businesses and 100 training check-ins. "
            "Choose another source category; a broad-sector prediction is not substituted."
        )
        return
    if filters.source_category is None:
        st.subheader("Projected activity by source category")
        rows = category_rows(projections)
        st.caption(
            "Each source category has its own supported ZIP population. Differences can "
            "reflect location composition and do not establish which niche is best to open. "
            "Select a source category in the sidebar to inspect its locations."
        )
    else:
        st.subheader(f"Projected activity by ZIP · {filters.source_category}")
        rows = [
            {
                "ZIP": row.group.postal_code,
                "Source category": row.group.source_category,
                "Projected check-ins / business / day": row.daily_rate,
                "Projected check-ins / business / period": row.period_rate,
                "Historical cohort businesses": row.group.n_businesses,
                "Training check-ins": row.group.training_checkins,
            }
            for row in sorted(projections, key=lambda row: (-row.daily_rate, row.group.postal_code))
        ]
        max_rate = max(row.daily_rate for row in projections) or 1.0
        points = [
            {
                "latitude": row.group.latitude,
                "longitude": row.group.longitude,
                "zip": row.group.postal_code,
                "rate": round(row.daily_rate, 4),
                "businesses": row.group.n_businesses,
                "radius": 1200 * math.sqrt(row.daily_rate / max_rate),
            }
            for row in projections
            if row.group.latitude is not None and row.group.longitude is not None
        ]
        if points:
            st.pydeck_chart(
                pdk.Deck(
                    layers=[
                        pdk.Layer(
                            "ScatterplotLayer",
                            pd.DataFrame(points),
                            get_position=["longitude", "latitude"],
                            get_radius="radius",
                            radius_min_pixels=5,
                            get_fill_color=[113, 85, 138, 170],
                            pickable=True,
                        )
                    ],
                    initial_view_state=pdk.ViewState(
                        latitude=fmean(cast(float, point["latitude"]) for point in points),
                        longitude=fmean(cast(float, point["longitude"]) for point in points),
                        zoom=10,
                    ),
                    map_provider="carto",
                    map_style=pdk.map_styles.CARTO_LIGHT,
                    tooltip={
                        "text": "ZIP {zip}\nProjected check-ins / business / day: {rate}\n"
                        "Historical cohort businesses: {businesses}"
                    },
                ),
                width="stretch",
                alt="ZIP centers sized by projected niche activity with ZIP and cohort tooltips",
            )
        st.caption(
            "Circle area represents projected activity at supported ZIP centers. "
            "Hover for ZIP, rate and cohort size. The table ranks archived-cohort activity "
            "projections; use it to shortlist areas for further research."
        )
    st.dataframe(
        pd.DataFrame(rows[:20]),
        hide_index=True,
        column_config={
            name: st.column_config.NumberColumn(format="%.4f")
            for name in (
                "Projected check-ins / business / day",
                "Projected check-ins / business / period",
            )
        },
        alt="Niche calendar projections with historical cohort and training sample sizes",
    )
    export = pd.DataFrame(rows).assign(
        Metro=filters.city,
        State=filters.state,
        Category=filters.category,
        Planning_start=start.isoformat(),
        Planning_end=end.isoformat(),
        Estimate_kind="archived_cohort_calendar_projection",
        Model_revision=revision,
    )
    st.download_button(
        "Download planning projections (CSV)",
        export.to_csv(index=False).encode(),
        file_name="sitesense_niche_planning.csv",
        mime="text/csv",
        key="niche_planning_export",
        on_click="ignore",
    )
    with st.expander("Model and backtest evidence"):
        st.caption(
            "The selected calendar model was chosen on forward validation, compared with "
            "an overall-mean baseline, and assessed on a later historical year. These "
            "previously examined years are exploratory; they do not verify future accuracy. "
            "Cohort selection is repeated inside each training window. Archive category "
            "and ZIP membership are snapshot labels."
        )
        evidence = bundle.manifest
        st.caption(
            f"Final training: {evidence['training_start']} to {evidence['training_end']}. "
            f"Selected model: {evidence['model_name']}. "
            "Errors measure check-ins per historical cohort business per day. "
            "Lower error is better; it does not measure the success of a new shop."
        )
        measurements = [
            {
                "Period": label,
                "Model": model,
                "Poisson deviance": metrics["poisson_deviance"],
                "Mean absolute error": metrics["mae"],
                "ZIP / niche groups": metrics["groups"],
                "Daily observations": metrics["observations"],
                "Top ZIP agreement": metrics.get("top_zip_agreement"),
                "Niches with multiple ZIPs": metrics.get("ranked_niches"),
            }
            for key, label in (
                ("validation_metrics", "Validation · 2020"),
                ("assessment_metrics", "Assessment · 2021"),
            )
            for model, metrics in evidence[key].items()
        ]
        st.dataframe(
            pd.DataFrame(measurements),
            hide_index=True,
            alt="Forward validation and assessment errors for niche calendar models",
        )
        st.caption(
            "Top ZIP agreement is the fraction of supported city/category/niche groups "
            "where the highest projected ZIP also had the highest recorded rate over "
            "the assessment year. It is descriptive ranking evidence, not opening-success accuracy."
        )


def planning_mode(context: "ScreenContext") -> bool:
    mode = st.segmented_control(
        "Location analysis",
        ("Recorded activity", "Future planning"),
        default="Recorded activity",
        required=True,
        key="location_analysis_mode",
        persist_state="session",
    )
    if mode == "Future planning":
        render(context)
        return True
    return False
