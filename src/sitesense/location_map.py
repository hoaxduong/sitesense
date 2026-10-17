"""Location map: recorded check-ins and station weather by ZIP on one screen.

One map shows check-in volume as circles (area proportional to recorded check-ins) over ZIP
areas filled by representative station weather for the same dates. A linked ranking table and
two aligned time panels (check-ins, weather) complete the screen. Everything here is read-only
and new; existing screens and calculations are reused without modification.
"""

import math
from dataclasses import replace
from datetime import date, timedelta
from typing import Any, cast

import psycopg
import pydeck as pdk  # type: ignore[import-untyped]
import streamlit as st

from sitesense import ui, zip_boundaries
from sitesense.database import ConfigurationError
from sitesense.location_weather import DailyWeather, load_daily_station_weather
from sitesense.models import ActivityRecord, AreaSummary, StationWeatherSummary, WeatherCell
from sitesense.rain_effect import CITY, RAIN_LEVEL_LABELS, RainEffect, rain_effects
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
    return day - timedelta(days=day.weekday()) if bucket == "week" else day.replace(day=1)


def _periods(start: date, end: date, bucket: str) -> list[date]:
    periods, current = [], _period(start, bucket)
    while current <= end:
        periods.append(current)
        if bucket == "week":
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
    if picked is None or picked == st.session_state.get(memory_key):
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
    for picked in (
        _picked_zip("location_deck", "location_last_map_pick", _deck_zip),
        _picked_zip(
            "location_table",
            "location_last_table_pick",
            lambda state: ranked_zips[state["selection"]["rows"][0]],
        ),
    ):
        if picked in ids:
            st.session_state["map_area"] = ids[picked]


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
    largest: int,
    level_label: str = "",
) -> str:
    """Map legend card: area colour scale and sample circle sizes (pixel radii match the map)."""
    parts = ['<div class="ss-legend">']
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
                f"</span>{low:,.1f} {unit} (all ZIPs share one station)</div>"
            )
        else:
            parts.append(
                f'<div class="ss-title">{title}</div>'
                f'<div class="ss-bar" style="background:linear-gradient(90deg,{_hex(start)},'
                f'{_hex(end)})"></div>'
                f'<div class="ss-scale"><span>{low:,.1f}</span>'
                f"<span>{high:,.1f} {unit}</span></div>"
            )
    if metric != NO_FILL and value_range is not None:
        missing = "Not measurable" if metric == RAIN_EFFECT else "No usable reports"
        parts.append(
            f'<div class="ss-row"><span class="ss-swatch" style="background:{_hex(MISSING_COLOR)}">'
            f"</span>{missing}</div>"
        )
    if largest > 0:
        sizes = [MAX_RADIUS_PX, MAX_RADIUS_PX / 2]
        circles = "".join(
            f'<span class="ss-circle-item"><span class="ss-circle" style="width:{2 * r:.0f}px;'
            f'height:{2 * r:.0f}px"></span>{round(largest * (r / MAX_RADIUS_PX) ** 2):,}</span>'
            for r in sizes
        )
        parts.append(
            '<div class="ss-title" style="margin-top:8px">Recorded check-ins</div>'
            f'<div class="ss-circles">{circles}</div>'
        )
    parts.append("</div>")
    return "".join(parts)


LEGEND_CSS = f"""
<style>
.st-key-location_map_view {{position:relative}}
.st-key-location_map_view .deck-widgets-root {{position:absolute;inset:0;pointer-events:none}}
.st-key-location_map_legend {{position:absolute;left:12px;bottom:12px;z-index:5;width:auto;
  pointer-events:none}}
.ss-legend {{background:rgba(255,255,255,0.94);border:1px solid #dce5df;border-radius:8px;
  box-shadow:0 2px 10px rgba(25,47,41,0.12);padding:10px 12px;font:12px/1.4 sans-serif;
  color:#192f29;width:200px}}
.ss-title {{font-weight:600;margin-bottom:4px}}
.ss-bar {{height:10px;border-radius:3px}}
.ss-scale {{display:flex;justify-content:space-between;color:#52514e;margin-top:2px}}
.ss-note {{color:#52514e;font-size:11px}}
.ss-row {{display:flex;align-items:center;gap:6px;margin-top:6px}}
.ss-swatch {{display:inline-block;width:14px;height:10px;border-radius:2px}}
.ss-circles {{display:flex;align-items:flex-end;gap:14px}}
.ss-circle-item {{display:flex;flex-direction:column;align-items:center;gap:2px;color:#52514e}}
.ss-circle {{display:inline-block;border-radius:50%;background:{_hex(CIRCLE_COLOR)};
  opacity:0.7;border:1px solid #fff}}
</style>
"""


def _render_map(
    features: list[dict[str, Any]],
    circles: list[dict[str, Any]],
    metric: str,
    value_range: tuple[float, float] | None,
    level_label: str,
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
            get_fill_color=CIRCLE_COLOR,
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
                '<div style="font-size:12px;margin-top:6px">Recorded check-ins</div>'
                '<div style="font-size:22px;font-weight:600;line-height:1.3">{checkins_text}</div>'
                '<div style="font-size:12px">Average weekly: {weekly_text} · '
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
            alt="ZIP areas filled by station weather with circles sized by recorded check-ins",
        )
        with st.container(key="location_map_legend"):
            st.html(legend_html(metric, value_range, largest, level_label))
    st.caption("Click a ZIP or circle to select it; the dark outline marks the selected ZIP.")


def _render_ranking(
    context: ui.ScreenContext,
    ranked: list[AreaSummary],
    features: list[dict[str, Any]],
    metric: str,
) -> AreaSummary:
    weather = {f["properties"]["postal_code"]: f["properties"].get("value") for f in features}
    rows = []
    for position, summary in enumerate(ranked, start=1):
        row: dict[str, object] = {
            "#": position,
            "ZIP area": summary.area.label,
            "Recorded check-ins": summary.checkin_count,
            "Avg weekly check-ins": round(summary.average_weekly_checkins, 1),
            "Listed businesses": summary.business_count,
            "Snapshot rating": summary.average_rating,
        }
        if metric != NO_FILL:
            value = weather.get(summary.area.postal_code)
            digits = 0 if metric == RAIN_EFFECT else 1
            row[FILL_OPTIONS[metric]] = round(value, digits) if value is not None else None
        rows.append(row)
    with st.container(border=True):
        st.subheader("ZIP areas by recorded check-ins")
        st.dataframe(
            rows,
            hide_index=True,
            height=300,
            key="location_table",
            on_select="rerun",
            selection_mode="single-row",
            alt="ZIP areas ordered by recorded check-ins with snapshot and weather values",
        )
        st.caption("Click a row to select the ZIP on the map.")
        summary = ui._selected_area(context, "map_area")
        ui._metrics(
            (
                ("Selected ZIP check-ins", f"{summary.checkin_count:,}"),
                ("Selected ZIP weekly average", f"{summary.average_weekly_checkins:,.1f}"),
            )
        )
    return summary


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
            )
        rain_view = st.session_state.get("location_fill") in ("precipitation", RAIN_EFFECT)
        with controls[1]:
            right = st.segmented_control(
                "Right axis",
                ("Temperature", "Precipitation"),
                default="Precipitation" if rain_view else "Temperature",
                required=True,
                key="location_right_axis",
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
        weather: list[dict[str, Any]] = []
        if stations:
            try:
                daily = _daily_weather(stations, start, end, context.import_revision)
            except (ConfigurationError, psycopg.Error):
                st.info("Station observations are unavailable. Please retry the connection.")
            else:
                weather = weather_series(daily, station_ids, start, end, bucket)
        rows = combined_series(
            checkin_series(context.records, area_ids, start, end, bucket), weather, str(right)
        )
        precipitation = right == "Precipitation"
        domain = ["Recorded check-ins"] + (
            ["Precipitation mm"] if precipitation else ["Mean daily high °C", "Mean daily low °C"]
        )
        colors = [_hex(CIRCLE_COLOR)] + (["#1c639b"] if precipitation else ["#e7893e", "#3678b4"])
        x = {
            "field": "Period",
            "type": "temporal",
            "title": None,
            "scale": {"domain": [start.isoformat(), end.isoformat()]},
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
        right_title = "Precipitation (mm)" if precipitation else "Temperature (°C)"
        st.vega_lite_chart(
            rows,
            {
                "layer": [
                    {
                        "transform": [{"filter": "datum.Axis === 'left'"}],
                        "mark": {"type": "line", "strokeWidth": 2.5},
                        "encoding": {
                            "x": x,
                            "y": {
                                "field": "Value",
                                "type": "quantitative",
                                "title": "Recorded check-ins",
                                "axis": {"titleColor": _hex(CIRCLE_COLOR), "orient": "left"},
                            },
                            "color": color,
                            "tooltip": tooltip,
                        },
                    },
                    {
                        "transform": [{"filter": "datum.Axis === 'right'"}],
                        "mark": {"type": "line", "strokeWidth": 1.8},
                        "encoding": {
                            "x": x,
                            "y": {
                                "field": "Value",
                                "type": "quantitative",
                                "title": right_title,
                                "axis": {"orient": "right", "titleColor": colors[1]},
                            },
                            "color": color,
                            "tooltip": tooltip,
                        },
                    },
                ],
                "resolve": {"scale": {"y": "independent"}},
            },
            height=320,
            alt=f"Recorded check-ins (left axis) and station {right_title.lower()} per {bucket}",
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
            f"One point per {bucket} "
            f"({'Monday weeks' if bucket == 'week' else 'calendar months'}). "
            f"The two axes have different units; compare the shapes over time, not where the "
            f"lines cross. {station_note}"
        )


def render_location_map() -> None:
    context = ui._context()
    if context is None:
        return
    covered = ui._activity_coverage(context)
    st.caption(
        "Where recorded check-ins concentrate and what the station weather was, for the same dates."
    )
    ui._activity_coverage_notice(context, covered)
    if covered is None:
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

    left, right = st.columns([3, 2], gap="large")
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
        selected = _render_ranking(context, ranked, base, metric)
    _render_time_panels(context, selected, stations, start, end)
