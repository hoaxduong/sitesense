"""Recorded activity, station weather, and linked comparison choices on one ZIP map."""

import math
from dataclasses import replace
from datetime import date, timedelta
from functools import partial
from statistics import fmean
from typing import Any, cast

import psycopg
import pydeck as pdk  # type: ignore[import-untyped]
import streamlit as st

from sitesense import (
    exploration,
    forecast_range,
    forecast_views,
    hourly_weather,
    repository,
    scientific_views,
    ui,
    zip_boundaries,
)
from sitesense.database import ConfigurationError
from sitesense.location_weather import DailyWeather, load_daily_station_weather
from sitesense.models import (
    ActivityRecord,
    AreaSummary,
    StationWeatherSummary,
    WeatherCell,
)
from sitesense.rain_effect import CITY, RAIN_LEVEL_LABELS, RainEffect, rain_effects
from sitesense.weather_charts import (
    weather_boxplot_caption,
    weather_boxplot_display_rows,
    weather_boxplot_layer,
    weather_boxplot_rows,
)
from sitesense.weather_map import (
    MISSING_COLOR,
    PRECIPITATION_COLORS,
    TEMPERATURE_COLORS,
    WEATHER_MAP_METRICS,
    _fit_view,
    _geometry_points,
    build_weather_features,
)

NO_FILL = "none"
RAIN_EFFECT = "rain_effect"
FILL_OPTIONS = {
    RAIN_EFFECT: "Rain-day change in check-ins (%)",
    **WEATHER_MAP_METRICS,
    NO_FILL: "No weather fill",
}
DECLINE_COLOR = (214, 96, 43)  # fewer check-ins on rain days
RISE_COLOR = (38, 116, 90)  # more check-ins on rain days
DIVERGING_MID = (236, 236, 232)
FILL_SHORT = {
    RAIN_EFFECT: "Rain-day change in check-ins",
    "temp_max": "Average daily high",
    "temp_min": "Average daily low",
    "precipitation": "Total rainfall",
    NO_FILL: "None (circles only)",
}
CIRCLE_COLOR = [38, 116, 90, 175]  # theme primary #26745a
FORECAST_CIRCLE_COLOR = [95, 108, 177, 175]
SELECTED_LINE = [25, 47, 41, 255]
NEUTRAL_FILL = [220, 229, 223, 120]
MAX_RADIUS_PX = 26.0  # screen pixels, so the legend circles match the map
MIN_RADIUS_PX = 3.0
MAP_HEIGHT = 470
WEEKLY_LIMIT_DAYS = 3 * 366  # longer selections are bucketed by month


# ---------- data preparation (pure, testable) ----------


def circle_rows(
    summaries: tuple[AreaSummary, ...],
    selected_zip: str | None,
    weather_by_zip: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One circle per ZIP centre; circle area is proportional to recorded check-ins.

    Circles carry the same tooltip fields as the ZIP areas, so hovering either shows the
    same card. `weather_by_zip` maps a ZIP to its area's weather_text and period.
    """
    weather_by_zip = weather_by_zip or {}
    largest = max((s.checkin_count for s in summaries), default=0)
    rows = []
    for summary in sorted(summaries, key=lambda s: -s.checkin_count):  # big first, small on top
        area = summary.area
        if area.latitude is None or area.longitude is None:
            continue
        share = summary.checkin_count / largest if largest else 0.0
        selected = area.postal_code == selected_zip
        rows.append(
            {
                "postal_code": area.postal_code,
                "area_label": area.label,
                "latitude": area.latitude,
                "longitude": area.longitude,
                "checkins": summary.checkin_count,
                "checkins_text": f"{summary.checkin_count:,}",
                "weekly_text": f"{summary.average_weekly_checkins:,.1f}",
                "businesses_text": f"{summary.business_count:,}",
                "weather_text": weather_by_zip.get(area.postal_code, {}).get(
                    "weather_text", "No ZIP boundary for weather"
                ),
                "period": weather_by_zip.get(area.postal_code, {}).get("period", ""),
                "radius": max(MIN_RADIUS_PX, MAX_RADIUS_PX * math.sqrt(share)),
                "line_color": SELECTED_LINE if selected else [255, 255, 255, 230],
                "line_width": 3 if selected else 1,
            }
        )
    return rows


def forecast_circle_rows(
    summaries: tuple[AreaSummary, ...],
    estimates: forecast_range.ForecastRange,
    selected_zip: str | None,
    properties: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    by_zip = {
        summary.area.postal_code: estimates.by_area.get(summary.area.id) for summary in summaries
    }
    largest = max((area.mean_rate or 0 for area in estimates.areas), default=0)
    result = []
    for row in circle_rows(summaries, selected_zip):
        area = by_zip.get(row["postal_code"])
        if area is None or area.mean_rate is None:
            continue
        details = properties.get(row["postal_code"], {})
        row.update(
            checkins=area.mean_rate * 100,
            checkins_text=f"{area.mean_rate * 100:.1f}",
            weekly_text=f"{area.estimated_days} / {area.requested_days}",
            weather_text=details.get("weather_text", "Model weather inputs"),
            period=details.get("period", ""),
            radius=max(MIN_RADIUS_PX, MAX_RADIUS_PX * math.sqrt(area.mean_rate / largest)),
        )
        result.append(row)
    return sorted(result, key=lambda row: -row["checkins"])


def _scale_color(value: float, low: float, high: float, metric: str) -> list[int]:
    start, end = PRECIPITATION_COLORS if metric == "precipitation" else TEMPERATURE_COLORS
    proportion = (value - low) / (high - low) if high > low else 0.5
    return [round(a + proportion * (b - a)) for a, b in zip(start, end, strict=True)] + [170]


def effect_text(effect: RainEffect | None, level_label: str) -> str:
    if effect is None or effect.n_days == 0:
        return "No rain reports for this ZIP"
    if effect.status == "insufficient" or effect.effect_pct is None:
        return f"Rain days {level_label}: too few days to measure ({effect.n_days})"
    if effect.status == "low_activity":
        return (
            f"Rain days {level_label}: too little activity to measure "
            f"({effect.expected_per_day:.1f} check-ins per day)"
        )
    ci = f"95% CI {effect.ci_low:+.0f}% to {effect.ci_high:+.0f}%"
    if effect.status == "no_clear":
        return f"Rain days {level_label}: no clear change ({effect.effect_pct:+.0f}%, {ci})"
    return (
        f"Rain days {level_label}: {effect.effect_pct:+.0f}% check-ins ({ci}, {effect.n_days} days)"
    )


def with_rain_effects(
    features: list[dict[str, Any]],
    effects: dict[str, RainEffect],
    level_label: str,
) -> list[dict[str, Any]]:
    """Use the rain-day change as each ZIP's value; only clear estimates get a value."""
    result = []
    for feature in features:
        properties = dict(feature["properties"])
        effect = effects.get(properties["postal_code"])
        clear = effect is not None and effect.status == "clear"
        properties["value"] = effect.effect_pct if clear and effect else None
        properties["display_value"] = (
            f"{effect.effect_pct:+.1f}%"
            if clear and effect and effect.effect_pct is not None
            else "—"
        )
        properties["effect_text"] = effect_text(effect, level_label)
        result.append({**feature, "properties": properties})
    return result


def _diverging_color(value: float, bound: float) -> list[int]:
    share = min(1.0, abs(value) / bound) if bound else 0.0
    end = DECLINE_COLOR if value < 0 else RISE_COLOR
    return [round(m + share * (e - m)) for m, e in zip(DIVERGING_MID, end, strict=True)] + [185]


def map_features(
    features: list[dict[str, Any]],
    summaries: tuple[AreaSummary, ...],
    metric: str,
    selected_zip: str | None,
) -> tuple[list[dict[str, Any]], tuple[float, float] | None]:
    """Add check-in facts, fill and outline to ZIP features; return them and the value range."""
    by_zip = {s.area.postal_code: s for s in summaries}
    values = [
        f["properties"]["value"] for f in features if f["properties"].get("value") is not None
    ]
    value_range = (min(values), max(values)) if values and metric != NO_FILL else None
    if value_range is not None and metric == RAIN_EFFECT:
        bound = max(abs(value_range[0]), abs(value_range[1]))
        value_range = (-bound, bound)
    colored = []
    for feature in features:
        properties = dict(feature["properties"])
        summary = by_zip.get(properties["postal_code"])
        properties["checkins_text"] = f"{summary.checkin_count:,}" if summary else "—"
        properties["weekly_text"] = f"{summary.average_weekly_checkins:,.1f}" if summary else "—"
        properties["businesses_text"] = f"{summary.business_count:,}" if summary else "—"
        value = properties.get("value")
        if metric == NO_FILL:
            properties["fill_color"] = NEUTRAL_FILL
            properties["weather_text"] = "Weather fill off"
        elif metric == RAIN_EFFECT:
            properties["weather_text"] = properties.get("effect_text", "No rain reports")
            properties["fill_color"] = (
                _diverging_color(value, value_range[1])
                if value is not None and value_range is not None
                else MISSING_COLOR
            )
        elif value is None or value_range is None:
            properties["fill_color"] = MISSING_COLOR
            properties["weather_text"] = "No usable reports"
        else:
            properties["fill_color"] = _scale_color(value, *value_range, metric)
            properties["weather_text"] = f"{FILL_OPTIONS[metric]}: {properties['display_value']}"
        selected = properties["postal_code"] == selected_zip
        properties["line_color"] = SELECTED_LINE if selected else [255, 255, 255, 200]
        properties["line_width"] = 3 if selected else 1
        colored.append({**feature, "properties": properties})
    return colored, value_range


def bucket_for(start: date, end: date) -> str:
    return "week" if (end - start).days <= WEEKLY_LIMIT_DAYS else "month"


def _period(day: date, bucket: str) -> date:
    if bucket == "day":
        return day
    return day - timedelta(days=day.weekday()) if bucket == "week" else day.replace(day=1)


def _periods(start: date, end: date, bucket: str) -> list[date]:
    periods, current = [], _period(start, bucket)
    while current <= end:
        periods.append(current)
        if bucket == "day":
            current += timedelta(days=1)
        elif bucket == "week":
            current += timedelta(days=7)
        else:
            current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return periods


def checkin_series(
    records: tuple[ActivityRecord, ...], area_ids: set[int], start: date, end: date, bucket: str
) -> list[dict[str, Any]]:
    """Recorded check-ins per period inside the covered dates (zero periods are kept)."""
    totals = dict.fromkeys(_periods(start, end, bucket), 0)
    for record in records:
        if record.area_id in area_ids and start <= record.activity_date <= end:
            totals[_period(record.activity_date, bucket)] += record.checkin_count
    return [{"Period": p.isoformat(), "Recorded check-ins": n} for p, n in totals.items()]


def weather_series(
    daily: DailyWeather, station_ids: set[str], start: date, end: date, bucket: str
) -> list[dict[str, Any]]:
    """Per period: mean daily high/low (°C) and precipitation (mm) across mapped stations.

    Each day averages the stations that reported it; precipitation adds those daily means.
    Periods without any accepted report are omitted rather than shown as zero.
    """
    by_period: dict[date, dict[str, list[float]]] = {}
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        measured: dict[str, list[float]] = {"tmax": [], "tmin": [], "pcpn": []}
        for station_id in station_ids:
            for variable, value in daily.get(station_id, {}).get(day, {}).items():
                measured[variable].append(value)
        bucket_values = by_period.setdefault(
            _period(day, bucket), {"tmax": [], "tmin": [], "pcpn": []}
        )
        for variable, values in measured.items():
            if values:
                bucket_values[variable].append(sum(values) / len(values))
    rows = []
    for period, period_values in sorted(by_period.items()):
        if not any(period_values.values()):
            continue
        row: dict[str, Any] = {"Period": period.isoformat()}
        if period_values["tmax"]:
            row["Mean daily high °C"] = round(
                sum(period_values["tmax"]) / len(period_values["tmax"]), 1
            )
        if period_values["tmin"]:
            row["Mean daily low °C"] = round(
                sum(period_values["tmin"]) / len(period_values["tmin"]), 1
            )
        if period_values["pcpn"]:
            row["Precipitation mm"] = round(sum(period_values["pcpn"]), 1)
            row["Days with rain reports"] = len(period_values["pcpn"])
        rows.append(row)
    return rows


# ---------- rendering ----------


@st.cache_data(ttl=300, max_entries=16, show_spinner=False)
def _daily_weather(
    cells: tuple[WeatherCell, ...], start_date: date, end_date: date, revision: str
) -> DailyWeather:
    return load_daily_station_weather(cells, start_date, end_date)


@st.cache_data(ttl=300, max_entries=16, show_spinner="Measuring rain-day changes…")
def _rain_effects(
    city: str,
    category: str,
    start: date,
    end: date,
    level: str,
    revision: str,
    _context: ui.ScreenContext,
    _daily: DailyWeather,
) -> dict[int, RainEffect]:
    return rain_effects(
        _context.records,
        _context.catalog.businesses,
        _context.summaries,
        _daily,
        start,
        end,
        level,
    )


def _picked_zip(state_key: str, memory_key: str, extract: Any) -> str | None:
    """A ZIP newly picked on the map or table since the previous run, else None."""
    try:
        picked = cast(str | None, extract(st.session_state.get(state_key)))
    except (KeyError, IndexError, TypeError, AttributeError):
        picked = None
    if picked is None:
        st.session_state.pop(memory_key, None)
        return None
    if picked == st.session_state.get(memory_key):
        return None
    st.session_state[memory_key] = picked
    return picked


def _deck_zip(state: Any) -> str | None:
    for objects in state["selection"]["objects"].values():
        if objects:
            item = objects[0]
            return str(item.get("postal_code") or item["properties"]["postal_code"])
    return None


def _apply_picks(summaries: tuple[AreaSummary, ...], ranked_zips: list[str]) -> None:
    ids = {s.area.postal_code: s.area.id for s in summaries}
    if st.session_state.get("map_area") not in ids.values():
        st.session_state["map_area"] = ids[ranked_zips[0]]
    pending = st.session_state.pop("pending_map_area", None)
    map_pick = _picked_zip("location_deck", "location_last_map_pick", _deck_zip)
    reset_picks = st.session_state.pop("reset_map_picks", False)
    # Consume remembered map picks before honoring an explicit cross-page target.
    # Otherwise a chart event left behind on the map can undo "View on map".
    if pending in ids.values():
        st.session_state["map_area"] = pending
        return
    if reset_picks:
        return
    if map_pick in ids:
        st.session_state["map_area"] = ids[map_pick]


def _select_comparison_rows(ranked_ids: tuple[int, ...]) -> None:
    """Apply a table event using the row identities from the table the user saw."""
    rows = st.session_state["location_table"]["selection"]["rows"]
    eligible = set(st.session_state.get("comparison_eligible_ids", []))
    requested = list(
        dict.fromkeys(
            ranked_ids[row]
            for row in rows
            if isinstance(row, int) and 0 <= row < len(ranked_ids) and ranked_ids[row] in eligible
        )
    )
    previous = exploration.get_comparison_ids()
    retained = [area_id for area_id in previous if area_id in requested]
    added = [area_id for area_id in requested if area_id not in previous]
    accepted = (retained + added)[: exploration.MAX_COMPARISON_AREAS]
    st.session_state["compare_areas"] = accepted
    newly_accepted = [area_id for area_id in added if area_id in accepted]
    if newly_accepted:
        st.session_state["map_area"] = newly_accepted[-1]
    if len(requested) > exploration.MAX_COMPARISON_AREAS:
        st.session_state["location_table_notice"] = (
            "Compare up to four ZIPs. Your existing choices were kept; remove one to add another."
        )
    st.rerun("location_map")


def _station_cells(context: ui.ScreenContext) -> tuple[WeatherCell, ...]:
    eligible = {s.area.id for s in context.summaries}
    mapped = {
        b.weather_cell_id
        for b in context.catalog.businesses
        if b.area_id in eligible and b.weather_cell_id is not None
    }
    return tuple(
        c
        for c in context.catalog.weather_cells
        if c.weather_cell_id in mapped and c.source_kind == "acis_station"
    )


def _hex(color: tuple[int, ...] | list[int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*color[:3])


def legend_html(
    metric: str,
    value_range: tuple[float, float] | None,
    largest: float,
    level_label: str = "",
    *,
    forecast_mode: bool = False,
) -> str:
    """Wrapping map key with colour and circle scales; pixel radii match the map."""
    parts = ['<div class="ss-legend" role="group" aria-label="Map legend">']
    if metric != NO_FILL:
        parts.append('<div class="ss-legend-section ss-colour-key">')
        if value_range is None:
            parts.append(f'<div class="ss-title">{FILL_SHORT[metric]}</div>')
    if metric == RAIN_EFFECT and value_range is not None:
        bound = value_range[1]
        gradient = f"{_hex(DECLINE_COLOR)}, {_hex(DIVERGING_MID)}, {_hex(RISE_COLOR)}"
        parts.append(
            f'<div class="ss-title">Check-ins on rain days {level_label}</div>'
            f'<div class="ss-bar" style="background:linear-gradient(90deg,{gradient})"></div>'
            f'<div class="ss-scale"><span>−{bound:.0f}%</span><span>0</span>'
            f"<span>+{bound:.0f}%</span></div>"
            '<div class="ss-note">vs same weekday, ±4 weeks</div>'
        )
    elif metric in WEATHER_MAP_METRICS and value_range is not None:
        low, high = value_range
        unit = "mm" if metric == "precipitation" else "°C"
        start, end = PRECIPITATION_COLORS if metric == "precipitation" else TEMPERATURE_COLORS
        title = FILL_SHORT[metric]
        if math.isclose(low, high):
            single = _hex(_scale_color(low, low, high, metric))
            parts.append(
                f'<div class="ss-title">{title}</div><div class="ss-row">'
                f'<span class="ss-swatch" style="background:{single}">'
                f"</span>{low:,.1f} {unit} across mapped ZIPs</div>"
            )
        else:
            parts.append(
                f'<div class="ss-title">{title}</div>'
                f'<div class="ss-bar" style="background:linear-gradient(90deg,{_hex(start)},'
                f'{_hex(end)})"></div>'
                f'<div class="ss-scale"><span>{low:,.1f}</span>'
                f"<span>{high:,.1f} {unit}</span></div>"
            )
    if metric != NO_FILL:
        missing = (
            "Forecast unavailable"
            if forecast_mode
            else "Not measurable"
            if metric == RAIN_EFFECT
            else "No usable reports"
        )
        parts.append(
            f'<div class="ss-row"><span class="ss-swatch" style="background:{_hex(MISSING_COLOR)}">'
            f"</span>{missing}</div></div>"
        )
    if largest > 0:
        sizes = [MAX_RADIUS_PX, MAX_RADIUS_PX / 2]
        circle_values = [largest * (radius / MAX_RADIUS_PX) ** 2 for radius in sizes]
        items = []
        for radius, value in zip(sizes, circle_values, strict=True):
            fill = f"background:{_hex(FORECAST_CIRCLE_COLOR)};" if forecast_mode else ""
            label = f"{value:.1f}" if forecast_mode else f"{round(value):,}"
            items.append(
                '<span class="ss-circle-item"><span class="ss-circle" '
                f'style="width:{2 * radius:.0f}px;height:{2 * radius:.0f}px;{fill}">'
                f"</span>{label}</span>"
            )
        circles = "".join(items)
        circle_label = (
            "Forecast check-ins / 100 businesses / day" if forecast_mode else "Recorded check-ins"
        )
        parts.append(
            f'<div class="ss-legend-section"><div class="ss-title">{circle_label}</div>'
            f'<div class="ss-circles">{circles}</div></div>'
        )
    parts.append("</div>")
    return "".join(parts)


LEGEND_CSS = f"""
<style>
.st-key-location_map_view {{position:relative}}
.st-key-location_map_view .deck-widgets-root {{position:absolute;inset:0;pointer-events:none}}
.st-key-location_map_legend .ss-legend {{display:flex;flex-wrap:wrap;align-items:center;
  gap:12px 28px;padding:4px 0;font:12px/1.4 sans-serif;color:#192f29}}
.st-key-location_map_legend .ss-legend-section {{min-width:180px}}
.st-key-location_map_legend .ss-colour-key {{width:230px;max-width:100%}}
.st-key-location_map_legend .ss-title {{font-weight:600;margin-bottom:4px}}
.st-key-location_map_legend .ss-bar {{height:8px;border-radius:3px}}
.st-key-location_map_legend .ss-scale {{display:flex;justify-content:space-between;
  color:#52514e;margin-top:2px}}
.st-key-location_map_legend .ss-note {{color:#52514e;font-size:11px}}
.st-key-location_map_legend .ss-row {{display:flex;align-items:center;gap:6px;margin-top:4px}}
.st-key-location_map_legend .ss-swatch {{display:inline-block;flex-shrink:0;
  width:14px;height:10px;border-radius:2px}}
.st-key-location_map_legend .ss-circles {{display:flex;align-items:center;gap:18px}}
.st-key-location_map_legend .ss-circle-item {{display:flex;align-items:center;
  gap:6px;color:#52514e}}
.st-key-location_map_legend .ss-circle {{display:inline-block;flex-shrink:0;
  box-sizing:border-box;border-radius:50%;background:{_hex(CIRCLE_COLOR)};
  opacity:0.7;border:1px solid #fff}}
</style>
"""


def _render_map(
    features: list[dict[str, Any]],
    circles: list[dict[str, Any]],
    metric: str,
    value_range: tuple[float, float] | None,
    level_label: str,
    *,
    activity_available: bool = True,
    forecast_mode: bool = False,
) -> None:
    points = [p for f in features for p in _geometry_points(f["geometry"].get("coordinates"))]
    points += [[c["longitude"], c["latitude"]] for c in circles]
    if not points:
        st.caption("No ZIP locations are available to map.")
        return
    layers = [
        pdk.Layer(
            "GeoJsonLayer",
            id="zip-areas",
            data={"type": "FeatureCollection", "features": features},
            filled=True,
            stroked=True,
            get_fill_color="properties.fill_color",
            get_line_color="properties.line_color",
            get_line_width="properties.line_width",
            line_width_units="'pixels'",
            pickable=True,
            auto_highlight=True,
            highlight_color=[255, 255, 255, 90],
        ),
        pdk.Layer(
            "ScatterplotLayer",
            id="zip-checkins",
            data=circles,
            get_position="[longitude, latitude]",
            get_radius="radius",
            radius_units="'pixels'",
            get_fill_color=FORECAST_CIRCLE_COLOR if forecast_mode else CIRCLE_COLOR,
            get_line_color="line_color",
            get_line_width="line_width",
            line_width_units="'pixels'",
            stroked=True,
            pickable=True,
        ),
    ]
    deck = pdk.Deck(
        layers=layers,
        map_style=None,
        initial_view_state=_fit_view(points),
        tooltip={
            "html": (
                '<div style="font-weight:600">{area_label}</div>'
                '<div style="font-size:12px;margin-top:6px">'
                + (
                    "Forecast check-ins / 100 businesses / day"
                    if forecast_mode
                    else "Recorded check-ins"
                )
                + "</div>"
                '<div style="font-size:22px;font-weight:600;line-height:1.3">{checkins_text}</div>'
                '<div style="font-size:12px">'
                + ("Estimated days" if forecast_mode else "Average weekly")
                + ": {weekly_text} · "
                "Listed businesses: {businesses_text}</div>"
                '<div style="border-top:1px solid #dce5df;margin-top:6px;padding-top:6px;'
                'font-size:12px">{weather_text}<br>{period}</div>'
            ),
            "style": {
                "transform": "none",
                "top": "12px",
                "left": "12px",
                "backgroundColor": "#ffffff",
                "color": "#192f29",
                "border": "1px solid #dce5df",
                "borderRadius": "8px",
                "boxShadow": "0 4px 16px rgba(25, 47, 41, 0.16)",
                "padding": "12px 14px",
                "fontFamily": "sans-serif",
                "fontSize": "13px",
                "lineHeight": "1.5",
                "width": "250px",
                "maxWidth": "calc(100% - 24px)",
                "boxSizing": "border-box",
                "zIndex": "10",
            },
        },
    )
    st.html(LEGEND_CSS)
    largest = max((c["checkins"] for c in circles), default=0)
    with st.container(key="location_map_view"):
        st.pydeck_chart(
            deck,
            height=MAP_HEIGHT,
            key="location_deck",
            on_select="rerun",
            selection_mode="single-object",
            alt=(
                "Forecast check-ins per 100 businesses per day by ZIP with model weather inputs"
                if forecast_mode
                else "ZIP areas filled by station weather with circles sized by recorded check-ins"
                if activity_available
                else "Candidate ZIP areas; activity is unavailable for the selected dates"
            ),
        )
    if metric != NO_FILL or largest > 0:
        with st.container(key="location_map_legend"):
            st.html(
                legend_html(metric, value_range, largest, level_label, forecast_mode=forecast_mode)
            )
    st.caption(
        "Click a ZIP or circle to select it; the dark outline marks the selected ZIP."
        if activity_available or forecast_mode
        else "Click a ZIP to select it; the dark outline marks the selected ZIP."
    )


def _render_ranking(
    context: ui.ScreenContext,
    ranked: list[AreaSummary],
    features: list[dict[str, Any]],
    metric: str,
    *,
    activity_available: bool = True,
    estimates: forecast_range.ForecastRange | None = None,
    forecast_mode: bool = False,
) -> None:
    """Browse and select comparison ZIPs, keeping table checks in sync with the fixed bar."""
    weather = {f["properties"]["postal_code"]: f["properties"].get("value") for f in features}
    rows = []
    activity_rank = 0
    previous_count: float | None = None
    forecasts = estimates.by_area if estimates is not None else {}
    for position, summary in enumerate(ranked, start=1):
        area_forecast = forecasts.get(summary.area.id)
        count = (
            (area_forecast.mean_rate if area_forecast is not None else None)
            if forecast_mode
            else summary.checkin_count
        )
        if previous_count != count:
            activity_rank = position
        previous_count = count
        row: dict[str, object] = {
            "#": (activity_rank if count is not None else None)
            if activity_available or forecast_mode
            else position,
            "ZIP area": summary.area.label,
        }
        if activity_available:
            row["Recorded check-ins"] = summary.checkin_count
            row["Avg weekly check-ins"] = round(summary.average_weekly_checkins, 1)
        if estimates is not None and estimates.uncovered_days:
            row["Forecast check-ins"] = (
                round(area_forecast.mean_rate * 100, 1)
                if area_forecast is not None and area_forecast.mean_rate is not None
                else None
            )
            row["Forecast days"] = area_forecast.estimated_days if area_forecast else 0
            row["Unavailable forecast days"] = (
                area_forecast.unavailable_days if area_forecast else estimates.uncovered_days
            )
        row["Listed businesses"] = summary.business_count
        row["Snapshot rating"] = summary.average_rating
        if metric != NO_FILL:
            value = weather.get(summary.area.postal_code)
            row[FILL_OPTIONS[metric]] = round(value, 1) if value is not None else None
        rows.append(row)
    with st.container(border=True, key="location_ranking"):
        st.subheader("Browse ZIP areas")
        st.caption(
            "Select up to four ZIPs to compare. Newly added ZIPs open in the map inspector. "
            + (
                "Ordered by forecast check-ins per 100 businesses per day; compare date coverage."
                if forecast_mode
                else "Ordered by recorded check-ins; ties share a rank."
                if activity_available
                else "Ordered by postal code."
            )
        )
        if notice := st.session_state.pop("location_table_notice", None):
            st.info(notice)
        columns = {
            "#": st.column_config.NumberColumn(
                "Rank" if activity_available or forecast_mode else "#",
                width="small",
                format="localized",
            ),
            "ZIP area": st.column_config.TextColumn("ZIP area", width="large"),
            "Recorded check-ins": st.column_config.NumberColumn("Check-ins", format="localized"),
            "Avg weekly check-ins": st.column_config.NumberColumn("Weekly average", format="%.1f"),
            "Listed businesses": st.column_config.NumberColumn(
                "Listed businesses", format="localized"
            ),
            "Snapshot rating": st.column_config.NumberColumn("Snapshot rating", format="%.1f"),
            "Forecast check-ins": st.column_config.NumberColumn(
                "Forecast / 100 / day", format="%.1f", help=forecast_views.DISPLAY_RATE_UNIT
            ),
            "Forecast days": st.column_config.NumberColumn("Forecast days", format="localized"),
            "Unavailable forecast days": st.column_config.NumberColumn(
                "Unavailable days", format="localized"
            ),
        }
        if metric != NO_FILL:
            unit = "%" if metric == RAIN_EFFECT else "mm" if metric == "precipitation" else "°C"
            columns[FILL_OPTIONS[metric]] = st.column_config.NumberColumn(
                f"{FILL_SHORT[metric]} ({unit})",
                format="%+.1f" if metric == RAIN_EFFECT else "%.1f",
            )
        selected_ids = set(exploration.get_comparison_ids())
        selected_rows = [
            index for index, summary in enumerate(ranked) if summary.area.id in selected_ids
        ]
        current_rows = (
            st.session_state.get("location_table", {}).get("selection", {}).get("rows", [])
        )
        if list(current_rows) != selected_rows:
            st.session_state["location_table"] = {"selection": {"rows": selected_rows}}
        st.dataframe(
            rows,
            placeholder="—",
            hide_index=True,
            height=300,
            column_config=columns,
            key="location_table",
            on_select=partial(
                _select_comparison_rows, tuple(summary.area.id for summary in ranked)
            ),
            selection_mode="multi-row",
            alt=(
                "ZIP areas ordered by forecast check-ins per 100 businesses per day with coverage"
                if forecast_mode
                else "ZIP areas ordered by recorded check-ins with snapshot and weather values"
                if activity_available
                else "ZIP areas ordered by postal code with business snapshot values"
            ),
        )
        st.caption(
            "— means unavailable or not measurable; it is not zero. "
            "Ratings and listed-business counts describe the archive snapshot."
        )
        if estimates is not None and estimates.uncovered_days:
            st.caption(
                f"Forecast: {forecast_views.DISPLAY_RATE_UNIT}. Means use estimated days only."
            )


def _render_selected_zip(
    context: ui.ScreenContext,
    ranked: list[AreaSummary],
    features: list[dict[str, Any]],
    metric: str,
    *,
    activity_available: bool = True,
    estimates: forecast_range.ForecastRange | None = None,
    forecast_mode: bool = False,
) -> AreaSummary:
    """Explain one selected ZIP and offer one clear path into comparison."""
    with st.container(border=True, key="location_selected_zip", gap="small"):
        st.subheader("Selected ZIP")
        selected = ui._selected_area(context, "map_area", on_change=_refresh_location_map)
        if not forecast_mode or activity_available:
            covered = ui._activity_coverage(context)
            recorded_days = (covered[1] - covered[0]).days + 1 if covered else 0
            totals, weekly = st.columns(2, gap="small")
            with totals:
                st.metric(
                    "Recorded check-ins" if forecast_mode else "Check-ins",
                    f"{selected.checkin_count:,}" if activity_available else "Unavailable",
                    help="Recorded check-ins for this ZIP within covered dates.",
                )
            with weekly:
                st.metric(
                    "Recorded / day" if forecast_mode else "Weekly average",
                    f"{selected.checkin_count / recorded_days:,.1f}"
                    if forecast_mode and recorded_days
                    else f"{selected.average_weekly_checkins:,.1f}"
                    if activity_available
                    else "Unavailable",
                    help="Recorded check-ins for the entire ZIP per covered day, including zeros."
                    if forecast_mode
                    else "Recorded check-ins per covered calendar week, including genuine zeros.",
                )
            if forecast_mode and covered:
                st.caption(
                    f"Entire ZIP · {recorded_days} recorded days ({covered[0]} to {covered[1]})."
                )
        total = sum(item.checkin_count for item in ranked)
        if activity_available and total:
            rank = 1 + sum(item.checkin_count > selected.checkin_count for item in ranked)
            tied = sum(item.checkin_count == selected.checkin_count for item in ranked) > 1
            st.caption(
                f"{'Joint rank' if tied else 'Rank'} {rank} of {len(ranked)} by check-ins · "
                f"{selected.checkin_count / total:.1%} of shown activity"
            )
        elif activity_available:
            st.caption("No check-ins were recorded in these ZIPs during the covered dates.")
        if estimates is not None:
            forecast_views.render_area_forecast(
                estimates, selected.area.id, heading=not forecast_mode
            )
        st.markdown("**Weather inputs**" if forecast_mode else "**Weather context**")
        feature = next(
            (
                item["properties"]
                for item in features
                if item["properties"]["postal_code"] == selected.area.postal_code
            ),
            None,
        )
        if feature is None and forecast_mode and estimates is not None:
            feature = forecast_map_properties(context.summaries, estimates, metric).get(
                selected.area.postal_code
            )
        if not activity_available and not forecast_mode:
            st.caption("Weather comparisons are not calculated outside recorded activity coverage.")
        elif metric == NO_FILL:
            st.caption("Weather fill is off. Choose a weather layer to inspect its value here.")
        elif feature is None:
            st.caption("No Census boundary is available for this ZIP's weather summary.")
        elif metric == RAIN_EFFECT:
            detail = str(feature.get("effect_text", "No rain reports for this ZIP"))
            value = feature.get("value")
            status = "No clear change" if "no clear change" in detail else "Not measurable"
            st.metric("Rain-day check-in change", f"{value:+.1f}%" if value is not None else status)
            st.caption(detail)
            st.caption("Matched-day association; not a causal effect.")
        else:
            value = feature.get("value")
            st.metric(
                FILL_SHORT[metric], feature["display_value"] if value is not None else "Unavailable"
            )
            if value is not None:
                source = "model station inputs" if forecast_mode else "mapped-station summary"
                st.caption(f"Valid reports: {feature['report_coverage']} · {source}")
            else:
                st.caption("No usable station reports for this ZIP and weather measure.")
            if feature.get("station_count"):
                with st.expander("Station details"):
                    st.caption(str(feature.get("station_names", "")))
                    st.caption(str(feature.get("station_coverage", "")))
            st.caption("Representative station observations, not measurements throughout the ZIP.")
        st.markdown("**Business snapshot**")
        rating = (
            f" · {selected.average_rating:.1f} / 5 rating"
            if selected.average_rating is not None
            else ""
        )
        st.caption(f"{selected.business_count:,} listed businesses{rating}")
        _render_comparison_actions(selected)
    return selected


def _render_comparison_actions(selected: AreaSummary) -> None:
    comparison_ids = exploration.get_comparison_ids()
    included = selected.area.id in comparison_ids
    st.button(
        "Remove from comparison" if included else "Add to comparison",
        key="map_toggle_comparison",
        type="secondary" if included else "primary",
        disabled=not included and len(comparison_ids) >= 4,
        on_click=exploration.toggle_comparison,
        args=(selected.area.id,),
        width="stretch",
        icon=":material/check:" if included else ":material/add:",
        help=f"{'Remove' if included else 'Add'} ZIP {selected.area.postal_code} "
        f"{'from' if included else 'to'} your comparison.",
    )
    if len(comparison_ids) >= 4 and not included:
        st.caption("Four areas are selected. Remove an area to add another ZIP.")


def combined_series(
    checkins: list[dict[str, Any]], weather: list[dict[str, Any]], right: str
) -> list[dict[str, Any]]:
    """Long rows (Period, Series, Value, Axis) for one chart with a left and a right axis."""
    rows = [
        {"Period": r["Period"], "Series": "Recorded check-ins", "Value": r["Recorded check-ins"],
         "Axis": "left"}
        for r in checkins
    ]  # fmt: skip
    measures = (
        ("Precipitation mm",)
        if right == "Precipitation"
        else ("Mean daily high °C", "Mean daily low °C")
    )
    for r in weather:
        for measure in measures:
            if measure in r:
                rows.append({"Period": r["Period"], "Series": measure, "Value": r[measure],
                             "Axis": "right"})  # fmt: skip
    return rows


def _render_time_panels(
    context: ui.ScreenContext,
    selected: AreaSummary,
    stations: tuple[WeatherCell, ...],
    start: date,
    end: date,
) -> None:
    bucket = bucket_for(start, end)
    with st.container(border=True):
        st.subheader("Check-ins and weather over time")
        controls = st.columns(2)
        with controls[0]:
            scope = st.segmented_control(
                "Show",
                ("Whole city", "Selected ZIP"),
                default="Whole city",
                required=True,
                key="location_series_scope",
                persist_state="session",
            )
        rain_view = st.session_state.get("location_fill") in ("precipitation", RAIN_EFFECT)
        with controls[1]:
            right = st.segmented_control(
                "Right axis",
                ("Temperature", "Precipitation"),
                default="Precipitation" if rain_view else "Temperature",
                required=True,
                key="location_right_axis",
                persist_state="session",
            )
        if scope == "Selected ZIP":
            area_ids = {selected.area.id}
            station_ids = {
                b.weather_cell_id
                for b in context.catalog.businesses
                if b.area_id == selected.area.id and b.weather_cell_id is not None
            }
        else:
            area_ids = {s.area.id for s in context.summaries}
            station_ids = {c.weather_cell_id for c in stations}
        names = {c.weather_cell_id: c.station_name or c.weather_cell_id for c in stations}
        city_total = sum(s.checkin_count for s in context.summaries)
        if scope == "Selected ZIP":
            share = selected.checkin_count / city_total if city_total else 0.0
            st.markdown(
                f"**{selected.area.label}** · {selected.checkin_count:,} recorded check-ins, "
                f"{share:.0%} of all candidate ZIPs. Select another ZIP on the map or in the table."
            )
        else:
            st.markdown(
                f"**Whole city** · all {len(context.summaries)} candidate ZIPs · "
                f"{city_total:,} recorded check-ins"
            )
        precipitation = right == "Precipitation"
        hourly_rows = (
            []
            if precipitation
            else hourly_weather.chart_rows(
                hourly_weather.load_samples(
                    context.filters.city, context.filters.state, start, end
                ),
                str(right),
            )
        )
        weather: list[dict[str, Any]] = []
        if stations and not hourly_rows:
            try:
                daily = _daily_weather(stations, start, end, context.import_revision)
            except (ConfigurationError, psycopg.Error):
                st.info("Station observations are unavailable. Please retry the connection.")
            else:
                weather = weather_series(daily, station_ids, start, end, "day")
        measures = (
            ["Precipitation mm"] if precipitation else ["Mean daily high °C", "Mean daily low °C"]
        )
        domain = ["Recorded check-ins", "Precipitation mm" if precipitation else "Temperature"]
        rows = [
            {
                "Period": row["Period"],
                "Series": "Recorded check-ins",
                "Value": row["Recorded check-ins"],
                "Axis": "left",
            }
            for row in checkin_series(context.records, area_ids, start, end, bucket)
        ]
        rows.extend(
            weather_boxplot_rows(
                [dict(row, Axis="right") for row in hourly_rows]
                or [
                    {
                        "Date": row["Period"],
                        "Series": measure,
                        "Weather value": row[measure],
                        "Weather source": "ACIS · daily mean across selected mapped stations",
                        "Weather station": "; ".join(
                            sorted(names.get(sid, sid) for sid in station_ids)
                        ),
                        "Observation frequency": "Daily summaries",
                        "Axis": "right",
                    }
                    for row in weather
                    for measure in measures
                    if measure in row
                ],
                start,
                end,
                combine_temperature=not precipitation,
            )
        )
        colors = [_hex(CIRCLE_COLOR), "#1c639b" if precipitation else "#e67e22"]
        x = {
            "field": "Period",
            "type": "temporal",
            "title": None,
            "scale": {"type": "utc", "domain": [start.isoformat(), end.isoformat()]},
        }
        color = {
            "field": "Series",
            "type": "nominal",
            "scale": {"domain": domain, "range": colors},
            "legend": {"orient": "top", "title": None},
        }
        tooltip = [
            {"field": "Period", "type": "temporal"},
            {"field": "Series"},
            {"field": "Value", "format": ",.1f"},
        ]
        right_title = "Precipitation (mm/day)" if precipitation else "Temperature (°C)"
        weather_layer = weather_boxplot_layer(
            start, end, title=f"{right_title} (boxes)", color=color, include_zip=False
        )
        weather_layer["transform"] = [
            {"filter": "datum.Axis === 'right' && isValid(datum['Weather value'])"}
        ]
        weather_layer["encoding"]["detail"].append({"field": "Series"})
        weather_layer["encoding"]["tooltip"].append({"field": "Series"})
        st.vega_lite_chart(
            weather_boxplot_display_rows(rows),
            {
                "layer": [
                    {
                        "transform": [{"filter": "datum.Axis === 'left'"}],
                        "mark": {"type": "line", "interpolate": "monotone", "strokeWidth": 2.5},
                        "encoding": {
                            "x": x,
                            "y": {
                                "field": "Value",
                                "type": "quantitative",
                                "title": "Recorded check-ins (line)",
                                "axis": {"titleColor": _hex(CIRCLE_COLOR), "orient": "left"},
                            },
                            "color": color,
                            "tooltip": tooltip,
                        },
                    },
                    weather_layer,
                ],
                "resolve": {"scale": {"y": "independent"}},
                "config": {"numberFormat": ".1f"},
            },
            height=320,
            alt=(
                f"Recorded check-ins per {bucket} as a line and observed station "
                f"{right_title.lower()} distributions as boxes over time"
            ),
        )
        used = sorted(names.get(i, i) for i in station_ids if i in names)
        station_note = (
            f"Weather: {len(used)} station{'s' if len(used) != 1 else ''} ({', '.join(used)})."
            if used
            else "Weather: no mapped stations."
        )
        if scope == "Selected ZIP" and station_ids == {c.weather_cell_id for c in stations}:
            station_note += " These are the same stations as the whole-city view."
        elif scope == "Selected ZIP":
            station_note += " Nearby stations report similar values to the whole-city view."
        st.caption(
            f"Activity has one point per {bucket} "
            f"({'Monday weeks' if bucket == 'week' else 'calendar months'}). "
            "The two axes have different units. "
            + (
                ""
                if hourly_rows
                else f"Daily weather uses selected mapped stations. {station_note}"
            )
        )
        st.caption(hourly_weather.caption(hourly_rows, metric=str(right)))
        st.caption(weather_boxplot_caption(start, end))


def _refresh_location_map() -> None:
    st.rerun("location_map")


def _ensure_current_import(context: ui.ScreenContext) -> None:
    try:
        if repository.get_import_revision() != context.import_revision:
            st.rerun()
    except (ConfigurationError, repository.DataUnavailableError, psycopg.Error):
        # Let the full app rebuild its context or show the existing connection UI.
        st.rerun()


@st.fragment(key="location_map")
def render_location_map() -> None:
    context = ui._context()
    if context is None:
        return
    _ensure_current_import(context)
    covered = ui._activity_coverage(context)
    estimates = ui._activity_forecast(context)
    st.caption(
        "Explore activity and weather for the same ZIPs and dates. "
        "Forecasts cover supported dates after the recorded period."
    )
    ui._activity_coverage_notice(context, covered, estimates)
    has_estimates = bool(estimates.estimated_dates)
    if has_estimates:
        _render_estimated_map(context, estimates, activity_available=covered is not None)
        _render_scientific_analysis(context)
        _ensure_current_import(context)
        return
    if covered is None:
        _render_unavailable_map(context, estimates)
        _render_scientific_analysis(context)
        _ensure_current_import(context)
        return
    start, end = covered
    filters = replace(context.filters, start_date=start, end_date=end)
    stations = _station_cells(context)
    observations: tuple[StationWeatherSummary, ...] = ()
    if stations:
        try:
            observations = ui._station_weather(stations, start, end, context.import_revision)
        except (ConfigurationError, psycopg.Error):
            st.info("Station observations are unavailable. Please retry the connection.")
    try:
        boundaries = ui._zip_boundaries()
    except (zip_boundaries.BoundaryDataError, OSError):
        boundaries = {}
        st.caption("ZIP boundaries are unavailable; only circles are shown.")

    ranked = sorted(context.summaries, key=lambda s: (-s.checkin_count, s.area.postal_code))
    _apply_picks(context.summaries, [s.area.postal_code for s in ranked])
    ui._metrics(
        (
            ("Candidate ZIP areas", str(len(context.summaries))),
            ("Recorded check-ins", f"{sum(s.checkin_count for s in context.summaries):,}"),
            ("Weather stations mapped", str(len(stations)) if stations else None),
            ("Days shown", f"{(end - start).days + 1:,}"),
        )
    )

    options: list[str] = []
    features_by_metric: dict[str, list[dict[str, Any]]] = {}
    for metric in WEATHER_MAP_METRICS:
        built = (
            build_weather_features(
                boundaries, context.catalog, context.summaries, observations, filters, metric
            )
            if boundaries
            else []
        )
        features_by_metric[metric] = built
        if any(f["properties"]["value"] is not None for f in built):
            options.append(metric)
    daily: DailyWeather = {}
    if stations:
        try:
            daily = _daily_weather(stations, start, end, context.import_revision)
        except (ConfigurationError, psycopg.Error):
            daily = {}
    has_rain = any("pcpn" in day for days in daily.values() for day in days.values())
    options = ([RAIN_EFFECT] if has_rain and boundaries else []) + options + [NO_FILL]
    if st.session_state.get("location_fill") not in options:
        st.session_state["location_fill"] = options[0]

    city_effect: RainEffect | None = None
    level = "r10"
    with st.container(border=True, key="location_settings"):
        st.markdown("**Map settings**")
        columns = st.columns([3, 2, 2], vertical_alignment="bottom")
        with columns[0]:
            metric = st.selectbox(
                "Colour ZIP areas by",
                options,
                format_func=FILL_SHORT.__getitem__,
                key="location_fill",
                persist_state="session",
            )
        # With the fill off or the rain effect, ZIP shapes come from the first metric's features.
        base = features_by_metric.get(metric) or features_by_metric[next(iter(WEATHER_MAP_METRICS))]
        if metric == RAIN_EFFECT:
            with columns[1]:
                level = str(
                    st.segmented_control(
                        "Rain day",
                        tuple(RAIN_LEVEL_LABELS),
                        default="r10",
                        required=True,
                        format_func=RAIN_LEVEL_LABELS.__getitem__,
                        key="location_rain_level",
                        persist_state="session",
                    )
                )
            effects = _rain_effects(
                context.filters.city,
                context.filters.category,
                start,
                end,
                level,
                context.import_revision,
                context,
                daily,
            )
            by_zip = {s.area.postal_code: effects[s.area.id] for s in context.summaries}
            base = with_rain_effects(base, by_zip, RAIN_LEVEL_LABELS[level])
            city_effect = effects.get(CITY)
            with columns[2]:
                if city_effect is not None and city_effect.effect_pct is not None:
                    clear = city_effect.status == "clear"
                    st.metric(
                        "All ZIPs on rain days",
                        f"{city_effect.effect_pct:+.1f}%",
                        (
                            f"95% CI {city_effect.ci_low:+.0f}% to {city_effect.ci_high:+.0f}%"
                            if clear
                            else "no clear change"
                        ),
                        delta_color="off",
                        help=effect_text(city_effect, RAIN_LEVEL_LABELS[level]),
                    )
            if end.year >= 2020:
                st.caption(
                    "The dates include 2020–2021, when COVID-19 and lower Yelp use changed "
                    "activity; ending in February 2020 gives a cleaner comparison."
                )
    selected_id = st.session_state.get("map_area")
    selected_zip = next(
        (s.area.postal_code for s in context.summaries if s.area.id == selected_id),
        ranked[0].area.postal_code,
    )
    features, value_range = map_features(base, context.summaries, metric, selected_zip)

    left, right = st.columns([2.2, 1], gap="small")
    with left, st.container(border=True):
        st.subheader("Check-ins and weather by ZIP")
        weather_by_zip = {
            f["properties"]["postal_code"]: {
                "weather_text": f["properties"]["weather_text"],
                "period": f["properties"].get("period") or f"{start:%Y-%m-%d} to {end:%Y-%m-%d}",
            }
            for f in features
        }
        circles = circle_rows(context.summaries, selected_zip, weather_by_zip)
        _render_map(features, circles, metric, value_range, RAIN_LEVEL_LABELS.get(level, ""))
    with right:
        selected = _render_selected_zip(context, ranked, features, metric, estimates=estimates)
    exploration.render_comparison_selection(context.summaries)
    _render_ranking(context, ranked, base, metric, estimates=estimates)
    _render_time_panels(context, selected, stations, start, end)
    _render_scientific_analysis(context)
    _ensure_current_import(context)


def _render_scientific_analysis(context: ui.ScreenContext) -> None:
    selected_id = st.session_state.get("map_area")
    ids = tuple(s.area.id for s in context.summaries if s.area.id == selected_id)
    scientific_views.render(context, ids, "map")


def forecast_map_properties(
    summaries: tuple[AreaSummary, ...],
    estimates: forecast_range.ForecastRange,
    metric: str,
) -> dict[str, dict[str, Any]]:
    """Explain the exact observed weather used on each ZIP's estimated dates."""
    by_area = estimates.by_area
    result = {}
    for summary in summaries:
        area = by_area.get(summary.area.id)
        points = area.points if area else ()
        value = None
        if points and metric != NO_FILL:
            if metric == "precipitation":
                value = sum(point.precip_mm for point in points)
            elif metric == "temp_max":
                value = fmean(point.tmax_c for point in points)
            else:
                value = fmean(point.tmin_c for point in points)
        unit = "mm" if metric == "precipitation" else "°C"
        display_value = f"{value:,.1f} {unit}" if value is not None else "Unavailable"
        stations = sorted({point.station_id for point in points})
        days = len(points)
        unavailable = area.unavailable_days if area else estimates.uncovered_days
        dates = sorted(point.day for point in points)
        reason = (area.reason if area else None) or estimates.reason
        result[summary.area.postal_code] = {
            "value": value,
            "display_value": display_value,
            "checkins_text": f"{area.mean_rate * 100:.1f}"
            if area and area.mean_rate is not None
            else "Unavailable",
            "weekly_text": f"{days} / {estimates.uncovered_days}",
            "weather_text": (
                f"Observed model inputs · {FILL_SHORT[metric]}: {display_value}"
                if value is not None
                else "Observed model weather inputs; weather fill off"
                if points
                else reason or "Forecast unavailable"
            ),
            "period": (f"{dates[0]} to {dates[-1]} · " if dates else "")
            + f"{days} estimated days; {unavailable} unavailable days",
            "report_coverage": f"{days} / {estimates.uncovered_days} uncovered days",
            "station_count": len(stations),
            "station_names": "; ".join(stations),
            "station_coverage": "Exact-day observations used only on estimated dates.",
        }
    return result


def _render_estimated_map(
    context: ui.ScreenContext,
    estimates: forecast_range.ForecastRange,
    *,
    activity_available: bool,
) -> None:
    """Reuse the ZIP workspace with cohort rates and their own weather inputs."""
    try:
        boundaries = ui._zip_boundaries()
    except (zip_boundaries.BoundaryDataError, OSError):
        boundaries = {}
        st.caption("ZIP boundaries are unavailable; only circles are shown.")
    by_area = estimates.by_area
    ranked = sorted(
        context.summaries,
        key=lambda summary: (-(by_area[summary.area.id].mean_rate or 0), summary.area.postal_code),
    )
    _apply_picks(context.summaries, [summary.area.postal_code for summary in ranked])
    ui._metrics(
        (
            ("Candidate ZIP areas", str(len(ranked))),
            ("Recorded check-ins", f"{sum(s.checkin_count for s in ranked):,}")
            if activity_available
            else ("Recorded check-ins", "Unavailable"),
            ("ZIPs with forecasts", str(sum(bool(area.points) for area in estimates.areas))),
            ("Forecast dates", str(len(estimates.estimated_dates))),
        )
    )
    with st.container(border=True, key="location_settings"):
        st.markdown("**Map settings**")
        metric = st.selectbox(
            "Colour ZIP areas by",
            [*WEATHER_MAP_METRICS, NO_FILL],
            format_func=FILL_SHORT.__getitem__,
            key="location_forecast_fill",
            persist_state="session",
        )
        st.caption(
            "Circles show forecast check-ins per 100 businesses per day; "
            "fill shows observed weather used on estimated dates. "
            "Recorded totals remain in the ZIP details and table. "
            "Each ZIP may have different date coverage."
        )
    selected_id = st.session_state.get("map_area")
    selected_zip = next(
        (summary.area.postal_code for summary in ranked if summary.area.id == selected_id),
        ranked[0].area.postal_code,
    )
    base = build_weather_features(
        boundaries,
        context.catalog,
        context.summaries,
        (),
        context.filters,
        metric if metric != NO_FILL else next(iter(WEATHER_MAP_METRICS)),
    )
    properties = forecast_map_properties(context.summaries, estimates, metric)
    for feature in base:
        feature["properties"].update(properties[feature["properties"]["postal_code"]])
    features, value_range = map_features(base, context.summaries, metric, selected_zip)
    # map_features supplies shared geometry styling; forecast tooltips keep their own units.
    for feature in features:
        feature["properties"].update(properties[feature["properties"]["postal_code"]])
    left, right = st.columns([2.2, 1], gap="small")
    with left, st.container(border=True):
        st.subheader("Activity and weather by ZIP")
        circles = forecast_circle_rows(context.summaries, estimates, selected_zip, properties)
        _render_map(
            features, circles, metric, value_range, "", activity_available=False, forecast_mode=True
        )
    with right:
        selected = _render_selected_zip(
            context,
            ranked,
            features,
            metric,
            activity_available=activity_available,
            estimates=estimates,
            forecast_mode=True,
        )
    exploration.render_comparison_selection(context.summaries)
    _render_ranking(
        context,
        ranked,
        features,
        metric,
        activity_available=activity_available,
        estimates=estimates,
        forecast_mode=True,
    )
    _render_activity_weather_chart(context, selected, estimates)


def _render_activity_weather_chart(
    context: ui.ScreenContext,
    selected: AreaSummary,
    estimates: forecast_range.ForecastRange,
) -> None:
    covered = ui._activity_coverage(context)
    cohort, cohort_records = ui._cohort_activity(context)
    cohort_businesses = cohort.denominators.get(selected.area.id)
    historical_weather: list[dict[str, Any]] = []
    stations = _station_cells(context)
    if covered is not None and stations:
        station_ids = {
            business.weather_cell_id
            for business in context.catalog.businesses
            if business.area_id == selected.area.id and business.weather_cell_id is not None
        }
        try:
            daily = _daily_weather(stations, *covered, context.import_revision)
            historical_weather = [
                {**row, "Date": row["Period"]}
                for row in weather_series(daily, station_ids, *covered, "day")
            ]
        except (ConfigurationError, psycopg.Error):
            st.info("Recorded station weather is unavailable. Model weather inputs remain visible.")
    with st.container(border=True):
        weather_options = ("Temperature", "Precipitation")
        if st.session_state.get("location_forecast_weather") not in weather_options:
            st.session_state["location_forecast_weather"] = weather_options[0]
        right = st.segmented_control(
            "Weather measure",
            weather_options,
            required=True,
            key="location_forecast_weather",
            persist_state="session",
        )
        st.caption(
            f"Weather and daily activity for {selected.area.label}. "
            "Select another ZIP on the map or in its details."
        )
        forecast_views.render_activity_weather_timeline(
            estimates,
            selected.area.id,
            selected.area.label,
            context.records,
            covered,
            context.filters.start_date,
            context.filters.end_date,
            historical_weather,
            str(right),
            hourly_weather_rows=[]
            if right == "Precipitation"
            else hourly_weather.chart_rows(
                hourly_weather.load_samples(
                    context.filters.city,
                    context.filters.state,
                    context.filters.start_date,
                    context.filters.end_date,
                ),
                str(right),
                label=selected.area.label,
            ),
            cohort_records=cohort_records if cohort_businesses is not None else None,
            cohort_businesses=cohort_businesses,
        )


def _render_unavailable_map(
    context: ui.ScreenContext,
    estimates: forecast_range.ForecastRange,
) -> None:
    """Keep geography and business snapshots usable without inventing missing activity."""
    try:
        boundaries = ui._zip_boundaries()
    except (zip_boundaries.BoundaryDataError, OSError):
        boundaries = {}
        st.caption("ZIP boundaries are unavailable.")
    ranked = sorted(context.summaries, key=lambda summary: summary.area.postal_code)
    _apply_picks(context.summaries, [summary.area.postal_code for summary in ranked])
    selected_id = st.session_state.get("map_area")
    selected_zip = next(
        (summary.area.postal_code for summary in ranked if summary.area.id == selected_id),
        ranked[0].area.postal_code,
    )
    base = build_weather_features(
        boundaries,
        context.catalog,
        context.summaries,
        (),
        context.filters,
        next(iter(WEATHER_MAP_METRICS)),
    )
    features, _ = map_features(base, context.summaries, NO_FILL, selected_zip)
    for feature in features:
        feature["properties"].update(
            checkins_text="Unavailable",
            weekly_text="Unavailable",
            weather_text="No recorded activity covers these dates",
        )
    ui._metrics(
        (
            ("Candidate ZIP areas", str(len(context.summaries))),
            ("Recorded check-ins", "Unavailable"),
            ("Listed businesses", f"{sum(s.business_count for s in context.summaries):,}"),
        )
    )
    st.caption(
        "Business snapshots and ZIP locations remain available. Activity circles, weather "
        "comparisons, and the timeline need recorded activity for the selected dates."
    )
    left, right = st.columns([2.2, 1], gap="small")
    with left, st.container(border=True):
        st.subheader("Candidate ZIP areas")
        _render_map(features, [], NO_FILL, None, "", activity_available=False)
    with right:
        _render_selected_zip(
            context,
            ranked,
            features,
            NO_FILL,
            activity_available=False,
            estimates=estimates,
        )
    exploration.render_comparison_selection(context.summaries)
    _render_ranking(context, ranked, base, NO_FILL, activity_available=False, estimates=estimates)
