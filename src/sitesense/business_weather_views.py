"""Daily observed weather and activity for one selected business."""

from dataclasses import replace

import pandas as pd  # type: ignore[import-untyped]
import psycopg
import streamlit as st

from sitesense import repository
from sitesense.business_weather import (
    WEATHER_METRICS,
    BusinessDay,
    BusinessWeatherDay,
    pair_business_weather,
    summarize_correlation,
    summarize_weather_bands,
    weather_band,
    weather_band_order,
)
from sitesense.database import ConfigurationError
from sitesense.location_weather import DailyWeather, load_daily_station_weather
from sitesense.models import Business, Filters, WeatherCell
from sitesense.ui import ScreenContext, render_setup


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def _business_daily(filters: Filters, business_id: str, revision: str) -> tuple[BusinessDay, ...]:
    return repository.load_business_daily(filters, business_id)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def _business_weather(
    filters: Filters, business_id: str, station: WeatherCell, revision: str
) -> DailyWeather:
    return load_daily_station_weather((station,), filters.start_date, filters.end_date)


def _render_weather_boxplot(
    days: tuple[BusinessWeatherDay, ...], metric: str, variable: str
) -> pd.DataFrame:
    """Plot raw daily observations, keeping empty bands visible without inventing boxes."""
    summaries = summarize_weather_bands(days, variable)
    short_labels = (
        ("<0 °C", "0–<10 °C", "10–<20 °C", "20–<30 °C", "≥30 °C")
        if variable == "tmax"
        else ("0 mm (dry/trace)", ">0–<1 mm", "1–<5 mm", "5–<10 mm", "10–<20 mm", "≥20 mm")
    )
    labels = {
        summary.band: f"{label} (n={summary.day_count:,})"
        for summary, label in zip(summaries, short_labels, strict=True)
    }
    ordered_labels = [labels[band] for band in weather_band_order(variable)]
    paired = pd.DataFrame(
        [
            {
                "Weather band": labels[weather_band(day.weather_value, variable)],
                "Recorded check-ins": day.checkin_count,
            }
            for day in days
            if day.weather_value is not None
        ]
    )
    st.vega_lite_chart(
        paired,
        {
            "title": "Daily recorded activity by weather band",
            "mark": {
                "type": "boxplot",
                "extent": 1.5,
                "orient": "horizontal",
                "size": 24,
                "ticks": True,
                "color": "#3076AB",
                "box": {"fillOpacity": 0.3, "stroke": "#3076AB"},
                "median": {"color": "#27313A"},
                "outliers": {"filled": True, "opacity": 0.65},
            },
            "encoding": {
                "y": {
                    "field": "Weather band",
                    "type": "ordinal",
                    "sort": ordered_labels,
                    "scale": {"domain": ordered_labels},
                    "axis": {"title": metric + " band", "labelLimit": 220},
                },
                "x": {
                    "field": "Recorded check-ins",
                    "type": "quantitative",
                    "scale": {
                        "domain": [0, max(1, int(paired["Recorded check-ins"].max()))],
                        "nice": True,
                    },
                    "axis": {"title": "Recorded check-ins / day", "tickMinStep": 1},
                },
            },
        },
        width="stretch",
        height=350,
        alt=f"Daily recorded check-in distributions by {metric} band, with paired-day counts",
    )
    st.caption(
        "The line inside each box is the median; the box spans the middle 50% of daily "
        "check-ins. Whiskers reach the observations within 1.5 times the interquartile range; "
        "points beyond them are outliers. n is the number of paired days, including zero "
        "check-ins. Empty bands have no box; zero-heavy bands can collapse to a line. "
        "Pearson r above still uses individual paired days, not band medians."
    )
    if any(0 < summary.day_count < 5 for summary in summaries):
        st.warning(
            "Some weather bands have fewer than five paired days. "
            "Read their sample counts before comparing distributions."
        )
    return pd.DataFrame(
        [
            {
                "Weather band": summary.band,
                "Paired days": summary.day_count,
                "Zero-activity days": summary.zero_activity_days,
                "Median check-ins/day": summary.median_checkins,
                "Q1": summary.q1,
                "Q3": summary.q3,
                "Minimum": summary.min_checkins,
                "Maximum": summary.max_checkins,
            }
            for summary in summaries
        ]
    )


def render_business_weather(context: ScreenContext, business: Business, filters: Filters) -> None:
    """Render exact-business daily associations within known activity coverage."""
    st.subheader("Weather and recorded activity")
    catalog = context.catalog
    selected = next(
        (row for row in catalog.businesses if row.business_id == business.business_id), None
    )
    if (
        selected is None
        or filters.category not in selected.categories
        or (filters.city, filters.state, filters.category)
        != (context.filters.city, context.filters.state, context.filters.category)
    ):
        st.info("Choose a business in the selected city and category.")
        return
    start = max(filters.start_date, catalog.coverage.start_date)
    end = min(filters.end_date, catalog.coverage.end_date)
    if end < start:
        st.info("Choose dates within the available recorded activity history.")
        return
    effective = replace(filters, start_date=start, end_date=end)
    st.caption(
        f"{selected.name} · business ID {selected.business_id} · "
        f"{start:%Y-%m-%d} to {end:%Y-%m-%d}. Activity is measured per calendar day."
    )
    metric = st.selectbox(
        "Weather variable",
        tuple(WEATHER_METRICS),
        key="business_weather_metric",
        persist_state="session",
    )
    chart_type = st.selectbox(
        "Weather chart",
        ("Box plot", "Scatter plot"),
        key="business_weather_chart",
        persist_state="session",
    )
    station = next(
        (
            cell
            for cell in catalog.weather_cells
            if cell.weather_cell_id == selected.weather_cell_id
            and cell.source_kind == "acis_station"
        ),
        None,
    )
    if station is None:
        st.info("This business has no mapped station observations for weather comparison.")
        return
    st.caption(
        f"Mapped station: {station.station_name or station.weather_cell_id} "
        f"({station.weather_cell_id}) · ACIS daily station observations."
    )
    try:
        with st.spinner("Loading daily business activity and station observations…"):
            records = _business_daily(effective, selected.business_id, context.import_revision)
            weather = _business_weather(
                effective, selected.business_id, station, context.import_revision
            )
        from sitesense.app import _import_is_unchanged

        if not _import_is_unchanged(context.import_revision):
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

    days = pair_business_weather(
        records,
        selected.business_id,
        start,
        end,
        catalog.coverage,
        weather.get(station.weather_cell_id, {}),
        WEATHER_METRICS[metric],
    )
    summary = summarize_correlation(days)
    with st.container(horizontal=True):
        st.metric("Calendar days", f"{summary.total_days:,}", border=True)
        st.metric("Paired days", f"{summary.paired_days:,}", border=True)
        st.metric("Missing weather days", f"{summary.missing_days:,}", border=True)
        st.metric(
            "Pearson r (unadjusted)",
            f"{summary.pearson_r:.3f}" if summary.pearson_r is not None else "Unavailable",
            border=True,
        )
    st.caption(
        "Zero recorded check-ins are included on covered dates; they do not establish "
        "whether the business was open or closed. Missing or rejected weather remains "
        "unknown and is excluded from the paired days. Trace rainfall is treated as 0 mm."
    )
    if summary.paired_days < 30:
        st.warning("Fewer than 30 paired days are available; this association has a small sample.")
    if summary.pearson_r is None:
        st.info(
            "Pearson r requires at least three paired days and variation in both weather "
            "and recorded activity."
        )
    st.caption(
        "This unadjusted correlation does not account for weekdays, seasonality or other "
        "confounding factors. It does not establish causation or statistical significance. "
        "Check-in dates use assumed source-local labels; alignment with station report "
        "dates is unverified. Check-ins are recorded activity, not customers or revenue."
    )
    if not summary.paired_days:
        st.info("No accepted observations for this weather variable match the selected dates.")
        return
    table = pd.DataFrame(
        [
            {
                "Date": day.activity_date,
                metric: day.weather_value,
                "Recorded check-ins": day.checkin_count,
            }
            for day in days
        ]
    )
    table["Date"] = pd.to_datetime(table["Date"])
    paired = table.dropna(subset=[metric])
    band_summary = None
    if chart_type == "Box plot":
        band_summary = _render_weather_boxplot(days, metric, WEATHER_METRICS[metric])
    else:
        st.scatter_chart(
            paired,
            x=metric,
            y="Recorded check-ins",
            x_label=metric,
            y_label="Recorded check-ins / day",
            alt=f"Daily recorded check-ins versus {metric} for the selected business",
        )
        st.caption(
            "Overlapping points can represent multiple dates. Daily observations retain dates."
        )
    if st.toggle("Show daily time series", key="business_weather_series", persist_state="session"):
        st.line_chart(
            table,
            x="Date",
            y="Recorded check-ins",
            y_label="Recorded check-ins / day",
            alt="Daily recorded check-ins including covered zero-activity days",
        )
        st.line_chart(
            table,
            x="Date",
            y=metric,
            y_label=metric,
            alt=f"Daily accepted {metric} for the business's mapped weather station",
        )
    with st.expander("Daily observations"):
        st.dataframe(
            table,
            hide_index=True,
            column_config={"Date": st.column_config.DateColumn(format="YYYY-MM-DD")},
            alt="Dated daily activity and accepted station observations, retaining missing weather",
        )
    if band_summary is not None:
        st.markdown("**Weather band summary**")
        st.caption(
            "All activity statistics below use daily recorded check-ins. — means no paired days."
        )
        st.dataframe(
            band_summary,
            hide_index=True,
            alt=(
                "Paired-day counts, zero-activity days and daily check-in quartiles by weather band"
            ),
        )
