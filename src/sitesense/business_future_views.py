"""Plan future dates with projections attached to individual archived businesses."""

import calendar
import hashlib
import math
from collections import defaultdict
from datetime import date, timedelta
from statistics import fmean
from typing import TYPE_CHECKING

import pandas as pd  # type: ignore[import-untyped]
import pydeck as pdk  # type: ignore[import-untyped]
import streamlit as st

from sitesense import business_forecast, business_tooltip, business_weather_layer

if TYPE_CHECKING:
    from sitesense.ui import ScreenContext


@st.cache_resource(ttl=300, max_entries=4, show_spinner=False)
def _bundle(revision: str, import_revision: str) -> business_forecast.BusinessBundle:
    return business_forecast.load_bundle(business_forecast.MODEL_DIR, import_revision)


def _dates(key: str) -> tuple[date, date]:
    today = date.today()
    period = st.segmented_control(
        "Planning period",
        ("Month", "Day"),
        default="Month",
        required=True,
        key=key + "_period",
        persist_state="session",
    )
    if period == "Day":
        stored = st.session_state.get(key + "_day")
        if isinstance(stored, date) and not today <= stored <= today + timedelta(days=365):
            st.session_state[key + "_day"] = today + timedelta(days=1)
        day = st.date_input(
            "Future day",
            value=today + timedelta(days=1),
            min_value=today,
            max_value=today + timedelta(days=365),
            key=key + "_day",
            persist_state="session",
        )
        if not isinstance(day, date):
            raise ValueError("Choose a future day.")
        return day, day
    months = tuple(
        date(today.year + (today.month - 1 + offset) // 12, (today.month - 1 + offset) % 12 + 1, 1)
        for offset in range(1, 13)
    )
    if st.session_state.get(key + "_month") not in months:
        st.session_state[key + "_month"] = months[0]
    month = st.selectbox(
        "Future month",
        months,
        format_func=lambda day: day.strftime("%B %Y"),
        key=key + "_month",
        persist_state="session",
    )
    return month, month.replace(day=calendar.monthrange(month.year, month.month)[1])


def projection_rows(
    projections: tuple[business_forecast.BusinessProjection, ...],
) -> list[dict[str, object]]:
    return [
        {
            "Business": row.group.name,
            "Projected check-ins": row.period_rate,
            "Training check-ins": row.group.training_checkins,
            "Training days with check-ins": row.group.active_training_days,
            "Source categories": ", ".join(row.group.source_categories),
            "Business ID": row.group.business_id,
        }
        for row in sorted(projections, key=lambda row: (-row.period_rate, row.group.business_id))
    ]


def source_rows(
    projections: tuple[business_forecast.BusinessProjection, ...],
) -> list[dict[str, object]]:
    grouped: dict[str, list[business_forecast.BusinessProjection]] = defaultdict(list)
    for row in projections:
        for source in row.group.source_categories:
            grouped[source].append(row)
    return sorted(
        [
            {
                "Source category": source,
                "Projected check-ins / business": fmean(row.period_rate for row in rows),
                "Modeled businesses": len(rows),
                "Training check-ins": sum(row.group.training_checkins for row in rows),
            }
            for source, rows in grouped.items()
        ],
        key=lambda row: (
            -float(str(row["Projected check-ins / business"])),
            str(row["Source category"]),
        ),
    )


def _map(
    projections: tuple[business_forecast.BusinessProjection, ...],
    key: str,
    context: "ScreenContext | None" = None,
    start: date | None = None,
    end: date | None = None,
) -> None:
    located = sorted(
        (
            row
            for row in projections
            if row.group.latitude is not None and row.group.longitude is not None
        ),
        key=lambda row: (-row.period_rate, row.group.business_id),
    )
    if not located:
        st.info(
            "These businesses have no usable map coordinates. "
            "Their projections remain in the table."
        )
        return
    limit = st.selectbox(
        "Top businesses on map",
        (10, 20, 50, 100),
        index=1,
        key=key + "_map_limit",
        help=(
            "Ranked by projected check-ins for the planning dates. "
            "The table and download retain all projections."
        ),
        persist_state="session",
    )
    mapped_count = len(located)
    located = located[:limit]
    maximum = max(row.period_rate for row in located) or 1.0
    points = [
        {
            "latitude": row.group.latitude,
            "longitude": row.group.longitude,
            "Business": row.group.name,
            "Business ID": row.group.business_id,
            "business_id": row.group.business_id,
            "Projected check-ins": round(row.period_rate, 4),
            "radius": 250 * math.sqrt(row.period_rate / maximum),
        }
        for row in located
    ]
    weather = (
        business_weather_layer.prepare(
            context,
            tuple(row.group.business_id for row in projections),
            start,
            end,
            key + "_weather",
        )
        if context is not None and start is not None and end is not None
        else None
    )
    show_weather = weather is not None and weather.has_values
    layers = []
    if show_weather:
        points = business_weather_layer.annotate(points, weather)
    for point in points:
        point.update(
            business_tooltip.fields(
                str(point["Business"]),
                f"{point['Projected check-ins']:,.2f}",
                period=business_tooltip.period_label(start, end)
                if start is not None and end is not None
                else None,
                projected=True,
                weather=point if show_weather else None,
                weather_period=business_tooltip.period_label(weather.start_date, weather.end_date)
                if show_weather and weather is not None
                else None,
            )
        )
    if weather is not None and weather.features:
        layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                data={"type": "FeatureCollection", "features": weather.features},
                id=key + "_weather_observations",
                get_fill_color="properties.weather_color",
                filled=True,
                stroked=True,
                get_line_color=[255, 255, 255, 90],
                get_line_width=1,
                line_width_units="'pixels'",
                pickable=False,
            )
        )
    layers.append(
        pdk.Layer(
            "ScatterplotLayer",
            points,
            id=key + "_businesses",
            pickable=True,
            get_position=["longitude", "latitude"],
            get_radius="radius",
            radius_min_pixels=4,
            get_fill_color=[113, 85, 138, 175],
            stroked=show_weather,
            get_line_color=[255, 255, 255, 220],
            line_width_min_pixels=1,
        )
    )
    # Keep the native deck.gl hover card positioned inside this map.
    container_key = key + "_map_view"
    st.html(
        f"<style>.st-key-{container_key} .deck-widgets-root "
        "{position:absolute;inset:0;pointer-events:none}</style>"
    )
    with st.container(key=container_key):
        st.pydeck_chart(
            pdk.Deck(
                layers=layers,
                initial_view_state=pdk.ViewState(
                    latitude=fmean(
                        float(row.group.latitude)
                        for row in located
                        if row.group.latitude is not None
                    ),
                    longitude=fmean(
                        float(row.group.longitude)
                        for row in located
                        if row.group.longitude is not None
                    ),
                    zoom=11,
                ),
                map_provider="carto",
                map_style=pdk.map_styles.CARTO_LIGHT,
                tooltip=business_tooltip.TOOLTIP,
            ),
            key=key + "_map",
            width="stretch",
            alt="Businesses sized by projected check-ins with weather for the planning dates",
        )
    if show_weather and weather is not None:
        business_weather_layer.render_legend(weather)
        st.caption(
            "The weather overlay shows observations only. These calendar activity "
            "projections do not use weather as an input."
        )
    st.caption(
        f"{mapped_count:,} of {len(projections):,} modeled businesses have map coordinates. "
        f"Showing the top {len(located):,} by projected check-ins for the planning dates. "
        "Each point is an individual business; minimum-size markers retain low or zero projections."
    )


def render(
    context: "ScreenContext",
    business_ids: tuple[str, ...] | None = None,
    key: str = "business_future",
) -> None:
    st.subheader("Future business planning")
    st.caption(
        "Choose a future day or month. Each estimate belongs to one business ID within "
        "the selected niche. Planning dates are independent of the recorded date range."
    )
    start, end = _dates(key)
    try:
        directory = business_forecast.MODEL_DIR
        revision = hashlib.sha256(
            (directory / "manifest.json").read_bytes()
            + (directory / "businesses.json").read_bytes()
            + business_forecast.code_sha256().encode()
            + business_forecast.lock_sha256().encode()
        ).hexdigest()
        bundle = _bundle(revision, context.import_revision)
    except (OSError, business_forecast.BusinessForecastError):
        st.info("The individual-business model is unavailable or does not match the current data.")
        st.code(
            "uv run --frozen --env-file .env python scripts/train_business_forecast.py",
            language="shell",
        )
        return
    st.warning(
        "Future calendar scenario: the model projects each archived business's activity "
        "patterns. Check-in data ends in 2022. Later closures, market changes, rent and "
        "future weather are not modeled; these estimates do not establish a new shop's "
        "customers, revenue or success."
    )
    st.caption(f"Planning dates: {start:%Y-%m-%d} to {end:%Y-%m-%d}.")
    if bundle.manifest["model_name"] == "overall_mean":
        st.caption(
            "Forward validation selected each business's historical mean over the "
            "month/weekday alternative. Projected totals scale with the number of planning days."
        )
    filters = context.filters
    projections = business_forecast.project(
        bundle, filters.city, filters.state, filters.category, filters.source_category, start, end
    )
    if business_ids is not None:
        selected = set(business_ids)
        projections = tuple(row for row in projections if row.group.business_id in selected)
        missing = selected - {row.group.business_id for row in projections}
        if missing:
            st.info(f"{len(missing)} selected businesses have no supported individual model.")
    if not projections:
        st.info(
            "No supported individual-business model for this selection. A business needs "
            "at least 20 training check-ins and recorded activity by the training window's start."
        )
        return
    st.subheader("Projected activity by business")
    st.caption(
        f"{len(projections):,} supported businesses. Predictions use each business's own "
        "recorded training history. Activity belongs to the whole listing, "
        "including hybrid businesses."
    )
    _map(projections, key, context, start, end)
    rows = projection_rows(projections)
    st.dataframe(
        pd.DataFrame(rows[:100]),
        hide_index=True,
        column_config={"Projected check-ins": st.column_config.NumberColumn(format="%.2f")},
        alt="Individual businesses ranked by their own activity projections with training support",
    )
    st.caption(f"Showing {min(100, len(rows)):,} of {len(rows):,} modeled businesses.")
    if filters.source_category is None and business_ids is None:
        with st.expander("Compare source category projections"):
            st.caption(
                "These summarize different populations of modeled businesses. Source tags "
                "overlap, so differences do not isolate a niche effect and totals are not additive."
            )
            st.dataframe(
                source_rows(projections),
                hide_index=True,
                alt="Source category projections summarized from individual businesses",
            )
    export = pd.DataFrame(rows).assign(
        Metro=filters.city,
        State=filters.state,
        Category=filters.category,
        Selected_source_category=filters.source_category or "All source categories",
        Planning_start=start.isoformat(),
        Planning_end=end.isoformat(),
        Estimate_kind="individual_business_calendar_projection",
        Model_revision=revision,
    )
    st.download_button(
        "Download business projections (CSV)",
        export.to_csv(index=False).encode(),
        file_name="sitesense_business_projections.csv",
        mime="text/csv",
        key=key + "_export",
        on_click="ignore",
    )
    with st.expander("Model and backtest evidence"):
        evidence = bundle.manifest
        st.caption(
            f"Final training: {evidence['training_start']} to {evidence['training_end']}. "
            f"Selected model: {evidence['model_name']}. Eligibility is rebuilt inside every "
            "temporal fit. Validation and assessment use individual business-day outcomes. "
            "These previously examined historical years do not verify current or future accuracy."
        )
        measurements = [
            {
                "Period": label,
                "Model": model,
                "Poisson deviance": metrics["poisson_deviance"],
                "Mean absolute error": metrics["mae"],
                "Businesses": metrics["businesses"],
                "Daily observations": metrics["observations"],
                "Top business agreement": metrics.get("top_business_agreement"),
            }
            for name, label in (
                ("validation_metrics", "Validation · 2020"),
                ("assessment_metrics", "Assessment · 2021"),
            )
            for model, metrics in evidence[name].items()
        ]
        st.dataframe(
            pd.DataFrame(measurements),
            hide_index=True,
            alt="Forward validation and assessment of individual-business models",
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
