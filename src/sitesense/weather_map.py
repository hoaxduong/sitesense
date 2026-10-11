"""Representative ZIP weather from mapped stations and Census ZCTA geometry.

A ZIP value weights each station summary by the number of listed category
businesses mapped to it. Missing station variables are excluded and the remaining
business weights are normalized. These are station summaries represented within
ZIP boundaries, not measurements throughout the ZIP or a spatial interpolation.
"""

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

import pydeck as pdk  # type: ignore[import-untyped]
import streamlit as st

from sitesense.models import AreaSummary, Catalog, Filters, StationWeatherSummary

GeoJSONFeature = dict[str, Any]
WEATHER_MAP_METRICS = {
    "temp_max": "Mean daily maximum °C",
    "temp_min": "Mean daily minimum °C",
    "precipitation": "Precipitation over valid reports (mm)",
}
_TOOLTIP_METRICS = {
    "temp_max": "Average daily high",
    "temp_min": "Average daily low",
    "precipitation": "Rainfall over valid reports",
}
MISSING_COLOR = [166, 174, 184, 150]
TEMPERATURE_COLORS = ([54, 120, 180], [231, 137, 62])
PRECIPITATION_COLORS = ([188, 221, 238], [28, 99, 155])
_MAP_HEIGHT = 430
_MIN_MAP_WIDTH = 320
_MAP_PADDING = 32


def _measurement(summary: StationWeatherSummary, metric: str) -> tuple[float | None, int]:
    if metric == "temp_max":
        return summary.mean_temp_max_c, summary.temp_max_days
    if metric == "temp_min":
        return summary.mean_temp_min_c, summary.temp_min_days
    return summary.precipitation_sum_mm, summary.precipitation_days


def build_weather_features(
    boundaries: Mapping[str, GeoJSONFeature],
    catalog: Catalog,
    summaries: Sequence[AreaSummary],
    station_summaries: Sequence[StationWeatherSummary],
    filters: Filters,
    metric: str,
) -> list[GeoJSONFeature]:
    """Join eligible ZIP geometry to business-weighted usable station summaries.

    Business coverage counts mappings with usable reports for this metric, divided
    by all listed category businesses in the ZIP. Report coverage is a separate
    business-weighted mean of valid days, not a count of distinct observation days.
    Precipitation averages station totals; it never sums rainfall across a ZIP.
    The caller loads station summaries for the exact selected report-date period.
    """
    if metric not in WEATHER_MAP_METRICS:
        raise ValueError("Unknown weather map metric.")
    selected_days = (filters.end_date - filters.start_date).days + 1
    if selected_days <= 0:
        raise ValueError("The end date must be on or after the start date.")
    station_ids = {
        cell.weather_cell_id for cell in catalog.weather_cells if cell.source_kind == "acis_station"
    }
    stations = {summary.weather_cell_id: summary for summary in station_summaries}
    unit = "mm" if metric == "precipitation" else "°C"
    features: list[GeoJSONFeature] = []
    for summary in sorted(summaries, key=lambda item: item.area.postal_code):
        area = summary.area
        if (area.city, area.state) != (filters.city, filters.state):
            continue
        boundary = boundaries.get(area.postal_code)
        geometry = boundary.get("geometry") if boundary else None
        if not isinstance(geometry, dict) or geometry.get("type") not in (
            "Polygon",
            "MultiPolygon",
        ):
            continue
        members = tuple(
            business
            for business in catalog.businesses
            if business.area_id == area.id
            and business.city.strip().casefold() == filters.city.strip().casefold()
            and business.state.strip().casefold() == filters.state.strip().casefold()
            and filters.category in business.categories
        )
        weights = Counter(
            business.weather_cell_id
            for business in members
            if business.weather_cell_id in station_ids
        )
        usable: list[tuple[StationWeatherSummary, int, float, int]] = []
        for station_id, weight in weights.items():
            station = stations.get(station_id or "")
            if station is None or station.selected_days != selected_days:
                continue
            value, valid_days = _measurement(station, metric)
            if value is None or not math.isfinite(value) or not 0 < valid_days <= selected_days:
                continue
            if metric == "precipitation" and value < 0:
                continue
            usable.append((station, weight, value, valid_days))
        usable_businesses = sum(weight for _, weight, _, _ in usable)
        total_businesses = len(members)
        value = (
            sum(weight * measured for _, weight, measured, _ in usable) / usable_businesses
            if usable_businesses
            else None
        )
        weighted_valid_days = (
            sum(weight * days for _, weight, _, days in usable) / usable_businesses
            if usable_businesses
            else None
        )
        station_names = "; ".join(sorted(station.station_name for station, _, _, _ in usable))
        station_coverage = "; ".join(
            f"{station.station_name}: {days:,}/{selected_days:,} days"
            for station, _, _, days in sorted(usable, key=lambda item: item[0].station_name)
        )
        boundary_properties = boundary.get("properties", {}) if boundary else {}
        features.append(
            {
                "type": "Feature",
                "id": area.postal_code,
                "geometry": geometry,
                "properties": {
                    "postal_code": area.postal_code,
                    "area_label": area.label,
                    "boundary_source": boundary_properties.get("boundary_source", ""),
                    "metric": metric,
                    "value": value,
                    "unit": unit,
                    "display_value": f"{value:,.1f} {unit}"
                    if value is not None
                    else "No usable reports",
                    "period": f"{filters.start_date:%Y-%m-%d} to {filters.end_date:%Y-%m-%d}",
                    "usable_businesses": usable_businesses,
                    "total_businesses": total_businesses,
                    "business_coverage": f"{usable_businesses:,} / {total_businesses:,}",
                    "business_coverage_rate": (
                        usable_businesses / total_businesses if total_businesses else None
                    ),
                    "valid_days": weighted_valid_days,
                    "selected_days": selected_days,
                    "report_coverage": (
                        f"{weighted_valid_days:,.1f} / {selected_days:,} days"
                        if weighted_valid_days is not None
                        else "No usable reports"
                    ),
                    "station_count": len(usable),
                    "station_names": station_names or "No usable stations",
                    "station_coverage": station_coverage or "No usable reports for this metric",
                },
            }
        )
    return features


def _geometry_points(coordinates: Any) -> list[list[float]]:
    if not isinstance(coordinates, list):
        return []
    if len(coordinates) >= 2 and all(isinstance(value, int | float) for value in coordinates[:2]):
        return [[float(coordinates[0]), float(coordinates[1])]]
    return [point for item in coordinates for point in _geometry_points(item)]


def _fit_view(points: Sequence[Sequence[float]]) -> Any:
    """Fit Mercator bounds within the chart's height and a narrow mobile width."""
    projected = []
    for point in points:
        if len(point) < 2 or not all(math.isfinite(value) for value in point[:2]):
            continue
        longitude = min(180.0, max(-180.0, point[0]))
        latitude = min(85.05, max(-85.05, point[1]))
        x = (longitude + 180.0) / 360.0
        y = (1.0 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2.0
        projected.append((x, y))
    if not projected:
        return pdk.ViewState(latitude=0, longitude=0, zoom=1, pitch=0, bearing=0)
    xs, ys = zip(*projected, strict=True)
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    center_x, center_y = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    longitude = center_x * 360.0 - 180.0
    latitude = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * center_y))))
    available_width = _MIN_MAP_WIDTH - 2 * _MAP_PADDING
    available_height = _MAP_HEIGHT - 2 * _MAP_PADDING
    zoom = math.log2(
        min(
            available_width / (512.0 * max(max_x - min_x, 1e-9)),
            available_height / (512.0 * max(max_y - min_y, 1e-9)),
        )
    )
    return pdk.ViewState(
        latitude=latitude,
        longitude=longitude,
        zoom=max(-2.0, min(14.0, zoom)),
        pitch=0,
        bearing=0,
    )


def render_weather_map(
    features: Sequence[GeoJSONFeature], metric: str, selected_zip: str | None = None
) -> None:
    """Render filled ZIP polygons, selection outline, tooltips and a native legend."""
    if metric not in WEATHER_MAP_METRICS:
        raise ValueError("Unknown weather map metric.")
    values = [
        feature["properties"]["value"]
        for feature in features
        if feature["properties"]["value"] is not None
    ]
    if not values:
        return
    low, high = min(values), max(values)
    start_color, end_color = (
        PRECIPITATION_COLORS if metric == "precipitation" else TEMPERATURE_COLORS
    )
    colored: list[GeoJSONFeature] = []
    for feature in features:
        properties = dict(feature["properties"])
        station_count = properties["station_count"]
        properties["tooltip_stations"] = (
            f"{station_count} {'station' if station_count == 1 else 'stations'}"
        )
        value = properties["value"]
        if value is None:
            properties["fill_color"] = MISSING_COLOR
        else:
            proportion = (value - low) / (high - low) if high > low else 0.5
            properties["fill_color"] = [
                round(start + proportion * (end - start))
                for start, end in zip(start_color, end_color, strict=True)
            ] + [190]
        selected = properties["postal_code"] == selected_zip
        properties["line_color"] = [28, 48, 61, 255] if selected else [255, 255, 255, 200]
        properties["line_width"] = 3 if selected else 1
        colored.append({**feature, "properties": properties})
    points = [
        point
        for feature in colored
        for point in _geometry_points(feature["geometry"].get("coordinates"))
    ]
    if not points:
        return
    layer = pdk.Layer(
        "GeoJsonLayer",
        id="zip-weather",
        data={"type": "FeatureCollection", "features": colored},
        filled=True,
        stroked=True,
        get_fill_color="properties.fill_color",
        get_line_color="properties.line_color",
        get_line_width="properties.line_width",
        line_width_units="'pixels'",
        pickable=True,
        auto_highlight=True,
    )
    deck = pdk.Deck(
        layers=[layer],
        map_style=None,
        initial_view_state=_fit_view(points),
        tooltip={
            "html": (
                '<div style="font-weight:600">ZIP {postal_code}</div>'
                '<div style="font-size:12px;margin-top:6px">'
                + _TOOLTIP_METRICS[metric]
                + '</div><div style="font-size:24px;font-weight:600;line-height:1.3">'
                "{display_value}</div>"
                '<div style="font-size:12px;margin:6px 0">{period}</div>'
                '<div style="border-top:1px solid #dce5df;padding-top:6px;font-size:12px">'
                "Valid days (weighted): {report_coverage}<br>"
                "Businesses covered: {business_coverage}<br>"
                "Station data · {tooltip_stations}</div>"
            ),
            "style": {
                # A stable corner card stays readable when hovering edge ZIPs.
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
                "width": "240px",
                "maxWidth": "calc(100% - 24px)",
                "boxSizing": "border-box",
            },
        },
    )
    # Streamlit 1.65 omits deck.gl's widget-root positioning stylesheet, which
    # offsets tooltip coordinates by the chart height. Keep the fix map-scoped.
    st.html(
        "<style>.st-key-zip_weather_view .deck-widgets-root "
        "{position:absolute;inset:0;pointer-events:none}</style>"
    )
    with st.container(key="zip_weather_view"):
        st.pydeck_chart(
            deck,
            height=_MAP_HEIGHT,
            key="zip_weather_map",
            alt="ZIP areas filled by representative station weather for the selected report dates",
        )
    unit = "mm" if metric == "precipitation" else "°C"
    if low == high:
        st.caption(
            f"Color scale: {low:,.1f} {unit} across populated ZIPs · Gray: no usable reports."
        )
    else:
        palette = "Light blue → dark blue" if metric == "precipitation" else "Blue → orange"
        st.caption(
            f"{palette}: {low:,.1f} to {high:,.1f} {unit} · Gray: no usable reports. "
            "Colors scale to available ZIP values for the selected dates."
        )
    if selected_zip and any(
        feature["properties"]["postal_code"] == selected_zip for feature in colored
    ):
        st.caption(f"Dark outline: selected ZIP {selected_zip}.")
    elif selected_zip:
        st.caption(f"Selected ZIP {selected_zip} has no Census boundary and is omitted.")
    with st.expander("About this map", expanded=False):
        st.caption(
            "ZIP values represent station summaries weighted by mapped listed businesses. "
            "They do not measure weather throughout each ZIP. Business mapping coverage and "
            "valid report-day coverage are separate."
        )
        if metric == "precipitation":
            st.caption(
                "The precipitation value averages mapped station totals over valid reports; it is "
                "not rainfall volume across a ZIP. Incomplete report periods can affect "
                "comparisons."
            )
