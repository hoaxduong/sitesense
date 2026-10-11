"""ZIP weather aggregation preserves mapping and observation coverage separately."""

import copy
import json
import math
from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from sitesense.models import (
    AreaSummary,
    Business,
    CandidateArea,
    Catalog,
    DateCoverage,
    Filters,
    StationWeatherSummary,
    WeatherCell,
)
from sitesense.weather_map import (
    MISSING_COLOR,
    WEATHER_MAP_METRICS,
    build_weather_features,
    render_weather_map,
)


def _boundary(postal_code: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"postal_code": postal_code, "boundary_source": "Census 2020 ZCTA"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-75.2, 39.9], [-75.1, 39.9], [-75.1, 40.0], [-75.2, 39.9]]],
        },
    }


@pytest.fixture
def evidence() -> tuple[
    dict[str, dict[str, Any]],
    Catalog,
    tuple[AreaSummary, ...],
    tuple[StationWeatherSummary, ...],
    Filters,
]:
    areas = tuple(
        CandidateArea(index, "Philadelphia", "PA", f"1900{index}", f"Area {index}", 39.9, -75.1)
        for index in (1, 2, 3)
    )
    businesses = tuple(
        Business(
            f"business-{area_id}-{index}",
            area_id,
            f"Business {index}",
            "Philadelphia",
            "PA",
            f"1900{area_id}",
            39.9,
            -75.1,
            ("Restaurants",),
            4.0,
            10,
            True,
            station,
        )
        for area_id, mappings in (
            (1, ("a", "a", "a", "b", "c", None)),
            (2, ("c",) * 5),
            (3, ("a",) * 5),
        )
        for index, station in enumerate(mappings)
    )
    catalog = Catalog(
        areas,
        businesses,
        tuple(
            WeatherCell(key, 40.0, -75.0, "acis_station", key.upper()) for key in ("a", "b", "c")
        ),
        DateCoverage(date(2015, 1, 1), date(2021, 12, 31)),
    )
    summaries = tuple(
        AreaSummary(area, 6 if area.id == 1 else 5, 0, 0.0, 4.0, 5, 1.0, None) for area in areas
    )
    reports = (
        StationWeatherSummary("a", "Station A", 7, 7, 7, 7, 0.0, 10.0, 0.0, date(2026, 10, 9)),
        StationWeatherSummary("b", "Station B", 7, 3, 3, 3, 10.0, 20.0, 30.0, date(2026, 10, 8)),
        StationWeatherSummary("c", "Station C", 7, 2, 0, 0, 5.0, None, None, date(2026, 10, 5)),
    )
    return (
        {"19001": _boundary("19001"), "19002": _boundary("19002")},
        catalog,
        summaries,
        reports,
        Filters("Philadelphia", "PA", "Restaurants", date(2026, 10, 3), date(2026, 10, 9)),
    )


def test_temperature_uses_business_weights_and_separate_report_coverage(evidence: Any) -> None:
    features = build_weather_features(*evidence, "temp_max")
    assert [feature["properties"]["postal_code"] for feature in features] == ["19001", "19002"]
    first = features[0]["properties"]
    assert first["value"] == pytest.approx(12.5)
    assert first["valid_days"] == pytest.approx(6.0)
    assert first["usable_businesses"] == 4
    assert first["total_businesses"] == 6
    assert first["business_coverage_rate"] == pytest.approx(4 / 6)
    assert first["report_coverage"] == "6.0 / 7 days"
    assert first["station_count"] == 2
    assert "Station A: 7/7 days" in first["station_coverage"]
    assert "Station B: 3/7 days" in first["station_coverage"]
    assert first["unit"] == "°C"
    assert first["boundary_source"] == "Census 2020 ZCTA"
    assert features[1]["properties"]["value"] is None
    assert features[1]["properties"]["usable_businesses"] == 0


def test_minimum_temperature_uses_only_that_variables_usable_stations(evidence: Any) -> None:
    feature = build_weather_features(*evidence, "temp_min")[0]["properties"]
    assert feature["value"] == pytest.approx(3.0)
    assert feature["valid_days"] == pytest.approx(5.2)
    assert feature["usable_businesses"] == 5
    assert feature["station_count"] == 3


def test_rainfall_averages_station_totals_and_preserves_zero(evidence: Any) -> None:
    feature = build_weather_features(*evidence, "precipitation")[0]["properties"]
    assert feature["value"] == pytest.approx(7.5)
    assert feature["unit"] == "mm"
    assert feature["display_value"] == "7.5 mm"
    boundaries, catalog, summaries, reports, filters = evidence
    zero = replace(reports[1], precipitation_sum_mm=0.0)
    features = build_weather_features(
        boundaries, catalog, summaries, (reports[0], zero), filters, "precipitation"
    )
    assert features[0]["properties"]["value"] == 0.0
    assert features[0]["properties"]["display_value"] == "0.0 mm"


@pytest.mark.parametrize("metric", WEATHER_MAP_METRICS)
def test_missing_invalid_or_other_period_reports_never_become_zero(
    evidence: Any, metric: str
) -> None:
    boundaries, catalog, summaries, _, filters = evidence
    invalid = (
        StationWeatherSummary("a", "A", 7, 0, 0, 0, 0.0, 0.0, 0.0, None),
        StationWeatherSummary("b", "B", 30, 3, 3, 3, 10.0, 20.0, 30.0, date(2026, 10, 8)),
        StationWeatherSummary("c", "C", 7, 2, 2, 2, float("nan"), float("inf"), -1.0, None),
    )
    features = build_weather_features(boundaries, catalog, summaries, invalid, filters, metric)
    assert all(feature["properties"]["value"] is None for feature in features)


def test_station_coordinates_without_acis_observations_are_not_weather_values(
    evidence: Any,
) -> None:
    boundaries, catalog, summaries, reports, filters = evidence
    catalog = replace(
        catalog,
        weather_cells=tuple(replace(cell, source_kind="era5") for cell in catalog.weather_cells),
    )
    features = build_weather_features(boundaries, catalog, summaries, reports, filters, "temp_max")
    assert all(feature["properties"]["value"] is None for feature in features)


def test_weather_scope_filters_business_weights_without_activity_coverage(evidence: Any) -> None:
    boundaries, catalog, summaries, reports, filters = evidence
    unrelated = replace(catalog.businesses[0], business_id="outside", categories=("Day Spas",))
    catalog = replace(catalog, businesses=catalog.businesses + (unrelated,))
    features = build_weather_features(boundaries, catalog, summaries, reports, filters, "temp_max")
    assert features[0]["properties"]["total_businesses"] == 6
    assert features[0]["properties"]["value"] == pytest.approx(12.5)
    assert features[0]["properties"]["period"] == "2026-10-03 to 2026-10-09"
    other_scope = replace(filters, city="Tampa", state="FL")
    assert not build_weather_features(
        boundaries, catalog, summaries, reports, other_scope, "temp_max"
    )


def test_missing_or_nonpolygon_geometry_is_omitted_without_mutating_boundaries(
    evidence: Any,
) -> None:
    boundaries, catalog, summaries, reports, filters = evidence
    original = copy.deepcopy(boundaries)
    features = build_weather_features(boundaries, catalog, summaries, reports, filters, "temp_max")
    assert len(features) == 2
    assert boundaries == original
    boundaries["19001"] = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-75.0, 40.0]},
    }
    features = build_weather_features(boundaries, catalog, summaries, reports, filters, "temp_max")
    assert [feature["properties"]["postal_code"] for feature in features] == ["19002"]


def test_unknown_metric_and_reversed_dates_are_rejected(evidence: Any) -> None:
    with pytest.raises(ValueError, match="metric"):
        build_weather_features(*evidence, "rainfall_volume")
    boundaries, catalog, summaries, reports, filters = evidence
    filters = replace(filters, end_date=date(2026, 10, 1))
    with pytest.raises(ValueError, match="end date"):
        build_weather_features(boundaries, catalog, summaries, reports, filters, "temp_max")


def test_renderer_fills_polygons_marks_missing_and_outlines_selected_zip(
    evidence: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sitesense.weather_map as weather_map

    features = build_weather_features(*evidence, "temp_max")
    original = copy.deepcopy(features)
    captured: list[Any] = []
    captions: list[str] = []
    monkeypatch.setattr(
        weather_map.st, "pydeck_chart", lambda deck, **kwargs: captured.append(deck)
    )
    monkeypatch.setattr(weather_map.st, "caption", captions.append)
    monkeypatch.setattr(weather_map.st, "expander", lambda *args, **kwargs: nullcontext())
    render_weather_map(features, "temp_max", "19001")
    assert features == original
    deck = json.loads(captured[0].to_json())
    layer = deck["layers"][0]
    assert layer["@@type"] == "GeoJsonLayer"
    assert layer["filled"] and layer["pickable"]
    assert layer["lineWidthUnits"] == "pixels"
    rows = layer["data"]["features"]
    assert rows[1]["properties"]["fill_color"] == MISSING_COLOR
    assert rows[0]["properties"]["line_width"] == 3
    assert rows[1]["properties"]["line_width"] == 1
    tooltip = captured[0]._tooltip["html"]
    assert "Average daily high" in tooltip
    assert "ZIP {postal_code}" in tooltip
    assert rows[0]["properties"]["tooltip_stations"] == "2 stations"
    assert "Valid days (weighted): {report_coverage}" in tooltip
    assert "Businesses covered: {business_coverage}" in tooltip
    assert "{tooltip_stations}" in tooltip
    assert any("Gray" in caption for caption in captions)
    assert any("selected ZIP 19001" in caption for caption in captions)
    captions.clear()
    render_weather_map(features, "temp_max", "19003")
    assert not any("Dark outline" in caption for caption in captions)
    assert "Selected ZIP 19003 has no Census boundary and is omitted." in captions
    omitted_selection = json.loads(captured[-1].to_json())
    assert all(
        feature["properties"]["line_width"] == 1
        for feature in omitted_selection["layers"][0]["data"]["features"]
    )


def test_renderer_omits_wholly_missing_metric_and_keeps_zero_rainfall(
    evidence: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sitesense.weather_map as weather_map

    captured: list[Any] = []
    captions: list[str] = []
    monkeypatch.setattr(
        weather_map.st, "pydeck_chart", lambda deck, **kwargs: captured.append(deck)
    )
    monkeypatch.setattr(weather_map.st, "caption", captions.append)
    monkeypatch.setattr(weather_map.st, "expander", lambda *args, **kwargs: nullcontext())
    boundaries, catalog, summaries, reports, filters = evidence
    missing = build_weather_features(boundaries, catalog, summaries, (), filters, "precipitation")
    render_weather_map(missing, "precipitation")
    assert not captured
    assert not captions
    zero = build_weather_features(
        boundaries, catalog, summaries, reports[:1], filters, "precipitation"
    )
    render_weather_map(zero, "precipitation")
    assert captured
    deck = json.loads(captured[0].to_json())
    assert deck["layers"][0]["data"]["features"][0]["properties"]["fill_color"] != MISSING_COLOR
    assert any("0.0 mm" in caption for caption in captions)
    assert any("not rainfall volume" in caption for caption in captions)


def test_renderer_fits_all_geometry_at_mobile_width_and_fixed_height(
    evidence: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sitesense.weather_map as weather_map

    features = build_weather_features(*evidence, "temp_max")
    corners = [[-75.32, 39.86], [-74.95, 39.86], [-74.95, 40.16], [-75.32, 40.16]]
    # Uneven vertex density must not pull the viewport center toward one edge.
    ring = corners + [corners[0]] * 50
    features[0]["geometry"] = {"type": "Polygon", "coordinates": [ring]}
    captured: list[Any] = []
    monkeypatch.setattr(
        weather_map.st, "pydeck_chart", lambda deck, **kwargs: captured.append(deck)
    )
    monkeypatch.setattr(weather_map.st, "caption", lambda text: None)
    monkeypatch.setattr(weather_map.st, "expander", lambda *args, **kwargs: nullcontext())
    render_weather_map(features, "temp_max")
    view = json.loads(captured[0].to_json())["initialViewState"]
    world_pixels = 512 * 2 ** view["zoom"]
    center_y = 0.5 - math.log(math.tan(math.pi / 4 + math.radians(view["latitude"]) / 2)) / (
        2 * math.pi
    )
    assert view["longitude"] == pytest.approx((-75.32 - 74.95) / 2)
    assert view["latitude"] == pytest.approx(40.01, abs=0.001)
    for longitude, latitude in corners:
        horizontal = abs((longitude - view["longitude"]) / 360 * world_pixels)
        point_y = 0.5 - math.log(math.tan(math.pi / 4 + math.radians(latitude) / 2)) / (2 * math.pi)
        vertical = abs(point_y - center_y) * world_pixels
        assert horizontal <= 320 / 2 - 32 + 1e-6
        assert vertical <= 430 / 2 - 32 + 1e-6


@pytest.mark.parametrize(
    "points,longitude,latitude",
    [
        ([[-2.0, -1.0], [2.0, 1.0]], 0.0, 0.0),
        ([[-75.4, 39.8], [-75.0, 40.2]], -75.2, 40.0),
    ],
)
def test_view_center_matches_known_geographic_bounds(
    points: list[list[float]], longitude: float, latitude: float
) -> None:
    from sitesense.weather_map import _fit_view

    view = _fit_view(points)
    assert view.longitude == pytest.approx(longitude, abs=1e-9)
    # Mercator midpoint differs slightly from arithmetic latitude at Philadelphia.
    assert view.latitude == pytest.approx(latitude, abs=0.001)


def test_degenerate_and_nonfinite_points_have_a_safe_finite_view() -> None:
    from sitesense.weather_map import _fit_view

    view = _fit_view([[-75.1, 39.9]] * 4)
    assert view.longitude == pytest.approx(-75.1)
    assert view.latitude == pytest.approx(39.9)
    assert math.isfinite(view.zoom)
    assert view.zoom <= 14
    polar = _fit_view([[10.0, 90.0]])
    assert abs(polar.latitude) <= 85.05 + 1e-9
    assert math.isfinite(polar.zoom)
    empty = _fit_view([[float("nan"), float("inf")]])
    assert all(math.isfinite(value) for value in (empty.longitude, empty.latitude, empty.zoom))


def test_hover_uses_native_interpolation_without_mutating_values_or_showing_station_names(
    evidence: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sitesense.weather_map as weather_map

    boundaries, catalog, summaries, reports, filters = evidence
    features = build_weather_features(
        boundaries, catalog, summaries, reports[:1], filters, "precipitation"
    )
    hostile = copy.deepcopy(features[0])
    hostile["properties"].update(
        {
            "postal_code": '19003"><img src=x onerror=alert(1)>',
            "display_value": '0.0 mm <script>alert("rain")</script>',
            "period": "2026-10-03 to <svg onload=alert(1)>",
            "report_coverage": '7.0 / 7 days & "reports"',
            "business_coverage": "5 / 6 <b>mapped</b> 'cohort'",
            "station_coverage": "unusually_long_station_name_" * 30 + "<script>station</script>",
        }
    )
    features.append(hostile)
    original = copy.deepcopy(features)
    captured: list[Any] = []
    monkeypatch.setattr(
        weather_map.st, "pydeck_chart", lambda deck, **kwargs: captured.append(deck)
    )
    monkeypatch.setattr(weather_map.st, "caption", lambda text: None)
    monkeypatch.setattr(weather_map.st, "container", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(weather_map.st, "html", lambda *args, **kwargs: None)
    monkeypatch.setattr(weather_map.st, "expander", lambda *args, **kwargs: nullcontext())
    render_weather_map(features, "precipitation")
    assert features == original
    deck = json.loads(captured[0].to_json())
    properties = [feature["properties"] for feature in deck["layers"][0]["data"]["features"]]
    template = captured[0]._tooltip["html"]
    assert "Rainfall over valid reports" in template
    assert properties[0]["display_value"] == "0.0 mm" and properties[0]["value"] == 0.0
    assert properties[1]["display_value"] == "No usable reports" and properties[1]["value"] is None
    assert properties[0]["tooltip_stations"] == "1 station"
    assert properties[1]["tooltip_stations"] == "0 stations"
    for field in ("postal_code", "display_value", "period", "report_coverage", "business_coverage"):
        assert "{" + field + "}" in template
        assert properties[2][field] == hostile["properties"][field]
        assert properties[2][field] not in template
    assert all(tag not in template for tag in ("<img", "<script", "<svg", "<b>"))
    assert "{station_coverage}" not in template
    assert "unusually_long_station_name_" not in template
