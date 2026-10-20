"""Representative mapped-station weather behind individual business activity circles."""

import math
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from html import escape
from typing import Any

import psycopg
import streamlit as st

from sitesense import repository, ui, zip_boundaries
from sitesense.database import ConfigurationError
from sitesense.import_data.yelp import canonical_scope
from sitesense.models import Business, StationWeatherSummary, WeatherCell
from sitesense.ui import ScreenContext

WEATHER_METRICS = (
    "Mean daily high (°C)",
    "Mean daily low (°C)",
    "Precipitation from valid reports (mm)",
    "Off",
)
SOURCE = "ACIS daily station observations"
UNKNOWN_COLOR = (151, 159, 166, 180)


@dataclass(frozen=True)
class WeatherLayer:
    metric: str
    start_date: date
    end_date: date
    by_business: dict[str, dict[str, Any]]
    low: float | None
    high: float | None
    features: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_values(self) -> bool:
        return self.low is not None and self.high is not None


def _scope(context: ScreenContext, business_ids: tuple[str, ...]) -> tuple[Business, ...]:
    eligible = set(business_ids)
    filters = context.filters
    selected_scope = canonical_scope(filters.city, filters.state)
    return tuple(
        {
            business.business_id: business
            for business in context.catalog.businesses
            if business.business_id in eligible
            and selected_scope is not None
            and canonical_scope(business.city, business.state) == selected_scope
            and filters.category in business.categories
            and (filters.source_category is None or filters.source_category in business.categories)
        }.values()
    )


def _stations(context: ScreenContext, businesses: tuple[Business, ...]) -> tuple[WeatherCell, ...]:
    identifiers = {business.weather_cell_id for business in businesses}
    # One request per mapped station, even when many businesses share its observations.
    stations = {
        cell.weather_cell_id: cell
        for cell in context.catalog.weather_cells
        if cell.source_kind == "acis_station" and cell.weather_cell_id in identifiers
    }
    return tuple(stations[identifier] for identifier in sorted(stations))


def _measure(summary: StationWeatherSummary, metric: str) -> tuple[float | None, int]:
    if metric == WEATHER_METRICS[0]:
        return summary.mean_temp_max_c, summary.temp_max_days
    if metric == WEATHER_METRICS[1]:
        return summary.mean_temp_min_c, summary.temp_min_days
    return summary.precipitation_sum_mm, summary.precipitation_days


def _color(value: float, low: float, high: float, *, rain: bool) -> list[int]:
    fraction = (value - low) / (high - low) if high > low else 0.5
    anchors = (
        ((198, 219, 239), (8, 81, 156))
        if rain
        else (
            (33, 102, 172),
            (246, 215, 70),
            (178, 24, 43),
        )
    )
    position = max(0.0, min(1.0, fraction)) * (len(anchors) - 1)
    left = min(int(position), len(anchors) - 2)
    weight = position - left
    return [
        round(anchors[left][channel] * (1 - weight) + anchors[left + 1][channel] * weight)
        for channel in range(3)
    ] + [210]


def build_layer(
    context: ScreenContext,
    business_ids: tuple[str, ...],
    start: date,
    end: date,
    metric: str,
    summaries: tuple[StationWeatherSummary, ...],
) -> WeatherLayer:
    """Join accepted summary values by station ID; never use activity coverage or area IDs."""
    if end < start:
        raise ValueError("The end date must be on or after the start date.")
    if metric not in WEATHER_METRICS[:-1]:
        raise ValueError("Choose an observed weather metric.")
    businesses = _scope(context, business_ids)
    stations = {cell.weather_cell_id: cell for cell in _stations(context, businesses)}
    requested_days = (end - start).days + 1
    accepted: dict[str, tuple[float, int]] = {}
    rain = metric == WEATHER_METRICS[2]
    for summary in summaries:
        value, days = _measure(summary, metric)
        if (
            summary.weather_cell_id in stations
            and summary.selected_days == requested_days
            and 0 < days <= requested_days
            and value is not None
            and math.isfinite(value)
            and (not rain or value >= 0)
        ):
            accepted[summary.weather_cell_id] = (value, days)
    values = [value for value, _ in accepted.values()]
    low, high = (min(values), max(values)) if values else (None, None)
    period = f"{start:%Y-%m-%d} to {end:%Y-%m-%d}"
    by_business: dict[str, dict[str, Any]] = {}
    for business in businesses:
        station = stations.get(business.weather_cell_id or "")
        measured = accepted.get(business.weather_cell_id or "")
        value, days = measured if measured is not None else (None, 0)
        by_business[business.business_id] = {
            "weather_value": value,
            "weather_days": days,
            "weather_color": _color(value, low, high, rain=rain)
            if value is not None and low is not None and high is not None
            else list(UNKNOWN_COLOR),
            "weather_text": f"{metric}: {value:,.1f}"
            if value is not None
            else f"{metric}: Unknown",
            "weather_station": (station.station_name or station.weather_cell_id)
            if station is not None
            else "No mapped ACIS station",
            "weather_station_id": station.weather_cell_id if station is not None else None,
            "weather_coverage": f"{days:,} / {requested_days:,} accepted days",
            "weather_period": period,
            "weather_source": SOURCE if station is not None else "No mapped ACIS station",
        }
    return WeatherLayer(metric, start, end, by_business, low, high)


def build_weather_background(
    layer: WeatherLayer,
    context: ScreenContext,
    business_ids: tuple[str, ...],
    boundaries: Mapping[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Represent business-weighted station weather within existing area boundaries.

    This joins existing geometry, without aggregating business activity or requiring
    a minimum category listing count. Rainfall averages station totals across the
    businesses with accepted observations; it never adds rainfall across businesses.
    """
    if layer.end_date < layer.start_date:
        raise ValueError("The end date must be on or after the start date.")
    if not layer.has_values:
        return []
    assert layer.low is not None and layer.high is not None
    members: dict[int, list[Business]] = defaultdict(list)
    for business in _scope(context, business_ids):
        if business.area_id is not None:
            members[business.area_id].append(business)
    requested_days = (layer.end_date - layer.start_date).days + 1
    rain = layer.metric == WEATHER_METRICS[2]
    unit = "mm" if rain else "°C"
    features = []
    for area in sorted(context.catalog.areas, key=lambda item: item.postal_code):
        businesses = members.get(area.id, [])
        if not businesses or (area.city, area.state) != (
            context.filters.city,
            context.filters.state,
        ):
            continue
        boundary = boundaries.get(area.postal_code)
        try:
            if zip_boundaries.validate_boundary_feature(boundary) != area.postal_code:
                continue
        except zip_boundaries.BoundaryDataError:
            continue
        assert boundary is not None
        accepted = []
        for business in businesses:
            observation = layer.by_business.get(business.business_id)
            if observation is None:
                continue
            value = observation.get("weather_value")
            days = observation.get("weather_days", 0)
            if (
                isinstance(value, int | float)
                and math.isfinite(value)
                and 0 < days <= requested_days
                and (not rain or value >= 0)
            ):
                accepted.append(observation)
        value = sum(row["weather_value"] for row in accepted) / len(accepted) if accepted else None
        days = sum(row["weather_days"] for row in accepted) / len(accepted) if accepted else None
        features.append(
            {
                "type": "Feature",
                "id": area.postal_code,
                "geometry": boundary["geometry"],
                "properties": {
                    "weather_color": list(UNKNOWN_COLOR[:3]) + [100],
                    "metric": layer.metric,
                    "value": value,
                    "unit": unit,
                    "weather_text": f"{layer.metric}: {value:,.1f}"
                    if value is not None
                    else f"{layer.metric}: Unknown",
                    "weather_coverage": f"{len(accepted):,} / {len(businesses):,} businesses; "
                    + (
                        f"mean {days:,.1f} / {requested_days:,} accepted days"
                        if days is not None
                        else "no accepted days"
                    ),
                    "weather_period": f"{layer.start_date:%Y-%m-%d} to {layer.end_date:%Y-%m-%d}",
                    "weather_source": SOURCE,
                    "accepted_businesses": len(accepted),
                    "total_businesses": len(businesses),
                    "weather_days": days,
                    "weather_station_count": len({row["weather_station_id"] for row in accepted}),
                    "boundary_source": boundary.get("properties", {}).get("boundary_source", ""),
                },
            }
        )
    values = [
        feature["properties"]["value"]
        for feature in features
        if feature["properties"]["value"] is not None
    ]
    if not values:
        return []
    low, high = min(values), max(values)
    for feature in features:
        properties = feature["properties"]
        if properties["value"] is not None:
            properties["weather_color"] = _color(properties["value"], low, high, rain=rain)[:3] + [
                100
            ]
    return features


def prepare(
    context: ScreenContext,
    business_ids: tuple[str, ...],
    start: date,
    end: date,
    key: str,
) -> WeatherLayer | None:
    """Select and load observed weather for the exact map interval, independently of check-ins."""
    if end < start:
        raise ValueError("The end date must be on or after the start date.")
    if st.session_state.get(key) not in WEATHER_METRICS:
        st.session_state[key] = WEATHER_METRICS[0]
    metric = st.selectbox(
        "Weather layer", WEATHER_METRICS, key=key, persist_state="session", width=320
    )
    if metric == "Off":
        return None
    stations = _stations(context, _scope(context, business_ids))
    summaries: tuple[StationWeatherSummary, ...] = ()
    if stations:
        try:
            summaries = ui._station_weather(stations, start, end, context.import_revision)
            from sitesense.app import _import_is_unchanged

            if not _import_is_unchanged(context.import_revision):
                # Prevent a caller from displaying stale values alongside a different import.
                st.stop()
        except (ConfigurationError, psycopg.Error, repository.DataUnavailableError):
            st.info("Station observations unavailable. Activity pins are still shown.")
            return None
    layer = build_layer(context, business_ids, start, end, metric, summaries)
    if not layer.has_values:
        st.info(
            "No accepted observed weather for these dates. Activity pins remain. "
            "Observations, not weather forecasts."
        )
        return layer
    try:
        boundaries = ui._zip_boundaries()
    except (zip_boundaries.BoundaryDataError, OSError):
        st.info("Weather boundaries are unavailable. Activity circles remain.")
        return layer
    features = build_weather_background(layer, context, business_ids, boundaries)
    if not features:
        st.info("No matching weather background. Activity circles remain.")
    values = [
        feature["properties"]["value"]
        for feature in features
        if feature["properties"]["value"] is not None
    ]
    layer = replace(
        layer,
        features=features,
        low=min(values) if values else layer.low,
        high=max(values) if values else layer.high,
    )
    return layer


def annotate(points: list[dict[str, Any]], layer: WeatherLayer | None) -> list[dict[str, Any]]:
    """Add weather tooltips/colors without changing a business's geometry, size or identity."""
    if layer is None:
        return [dict(point) for point in points]
    return [dict(point, **layer.by_business.get(str(point["business_id"]), {})) for point in points]


def render_legend(layer: WeatherLayer | None, *, circle_metric: str = "activity") -> None:
    """Render the background color scale, with station coverage and source context."""
    if layer is None or not layer.has_values or not layer.features:
        return
    assert layer.low is not None and layer.high is not None
    rain = layer.metric == WEATHER_METRICS[2]
    stops = []
    for step in range(21):
        value = layer.low + (layer.high - layer.low) * step / 20
        color = _color(value, layer.low, layer.high, rain=rain)
        stops.append(
            f'<stop offset="{step * 5}%" stop-color="rgb({color[0]},{color[1]},{color[2]})"/>'
        )
    unit = "mm" if rain else "°C"
    low, high = f"{layer.low:,.1f} {unit}", f"{layer.high:,.1f} {unit}"
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="420" height="46" viewBox="0 0 420 46">'
        '<defs><linearGradient id="weather">' + "".join(stops) + "</linearGradient></defs>"
        '<rect x="1" y="1" width="418" height="16" rx="3" '
        'fill="url(#weather)" fill-opacity="0.392"/>'
        f'<text x="1" y="37" font-size="13" fill="#59636C">{escape(low)}</text>'
        f'<text x="419" y="37" text-anchor="end" font-size="13" fill="#59636C">'
        f"{escape(high)}</text></svg>"
    )
    st.image(
        svg,
        width=420,
        alt=f"{layer.metric} color scale from {low} to {high}; gray means unknown weather",
    )
    st.caption(f"Weather: mapped-station observations · Circles: {circle_metric} · Gray: unknown")
    available = [row for row in layer.by_business.values() if row["weather_value"] is not None]
    station_count = len({row["weather_station_id"] for row in available})
    with st.expander("Weather layer details"):
        st.caption(
            f"{len(available):,} / {len(layer.by_business):,} businesses have accepted weather "
            f"from {station_count:,} mapped stations · report dates "
            f"{layer.start_date:%Y-%m-%d} to {layer.end_date:%Y-%m-%d}. "
            f"{layer.metric}. Gray means unknown weather. "
            "Hover a business for its accepted-day count. "
            f"{SOURCE} are averaged across matching businesses within existing area boundaries. "
            "Area shading represents linked stations, not measurements throughout the area "
            "or an interpolated weather field. Businesses linked to the same station share "
            "weather values. The scale spans representative area weather values. "
            "The layer shows observations, not weather forecasts."
        )
        if rain:
            st.caption(
                "Precipitation sums only accepted reports; missing days are excluded, so this "
                "is not necessarily rainfall for the full period. Trace rainfall counts as 0 mm."
            )
