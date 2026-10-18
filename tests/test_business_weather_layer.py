"""Mapped station colors keep exact business scope and observed-weather coverage."""

import json
from dataclasses import replace
from datetime import date
from typing import Any

import psycopg
import pytest
from streamlit.testing.v1 import AppTest

from sitesense import app as app_module
from sitesense import business_weather_layer as layer
from sitesense import repository, ui, zip_boundaries
from sitesense.database import ConfigurationError
from sitesense.models import (
    Business,
    CandidateArea,
    Catalog,
    DateCoverage,
    Filters,
    StationWeatherSummary,
    WeatherCell,
)
from sitesense.ui import ScreenContext

START, END = date(2024, 2, 1), date(2024, 2, 3)
HIGH, LOW, RAIN, OFF = layer.WEATHER_METRICS


def _business(identifier: str, station: str | None = "acis_a", **changes: Any) -> Business:
    business = Business(
        identifier,
        1,
        "Same name",
        "Philadelphia",
        "PA",
        "",
        40.0,
        -75.0,
        ("Restaurants", "Pizza"),
        4.0,
        10,
        True,
        station,
    )
    return replace(business, **changes)


STATIONS = (
    WeatherCell("acis_a", 41.0, -74.0, "acis_station", "Airport A"),
    WeatherCell("acis_b", 42.0, -73.0, "acis_station", "Airport B"),
    WeatherCell("acis_unrelated", 43.0, -72.0, "acis_station", "Unrelated"),
    WeatherCell("grid", 40.0, -75.0, "era5"),
)
CATALOG = Catalog(
    (
        CandidateArea(1, "Philadelphia", "PA", "19103", "Centre", None, None),
        CandidateArea(2, "Philadelphia", "PA", "19104", "West", None, None),
        CandidateArea(3, "Philadelphia", "PA", "19105", "Other", None, None),
    ),
    (
        _business("a"),
        _business("b"),
        _business("c", "acis_b", area_id=2),
        _business("unmapped", None, area_id=2),
        _business("grid", "grid", area_id=3),
        _business("other-niche", "acis_unrelated", categories=("Restaurants", "Italian")),
        _business("spa", "acis_unrelated", categories=("Day Spas", "Pizza")),
        _business("other-city", "acis_unrelated", city="Nashville", state="TN"),
        _business("same-city-other-state", "acis_unrelated", state="XX"),
    ),
    STATIONS,
    DateCoverage(date(2020, 1, 1), date(2020, 1, 3)),
)
CONTEXT = ScreenContext(
    Filters("Philadelphia", "PA", "Restaurants", START, END, source_category="Pizza"),
    CATALOG,
    (),
    (),
    "import-one",
)
IDS = tuple(business.business_id for business in CATALOG.businesses)


def _boundary(postal_code: str) -> dict[str, Any]:
    return {
        "type": "Feature",
        "properties": {"postal_code": postal_code, "boundary_source": "Packaged Census ZCTA"},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[-75.0, 40.0], [-75.1, 40.0], [-75.1, 40.1], [-75.0, 40.0]]],
        },
    }


BOUNDARIES = {postal_code: _boundary(postal_code) for postal_code in ("19103", "19104", "19105")}


def _summary(identifier: str = "acis_a", **changes: Any) -> StationWeatherSummary:
    summary = StationWeatherSummary(
        identifier, "Ignored summary label", 3, 2, 3, 2, -2.0, 10.0, 0.0, END
    )
    return replace(summary, **changes)


def test_exact_business_scope_and_station_join_exclude_other_categories_cities_and_grids() -> None:
    built = layer.build_layer(
        CONTEXT,
        IDS,
        START,
        END,
        HIGH,
        (_summary(), _summary("acis_b", mean_temp_max_c=20), _summary("acis_unrelated")),
    )
    assert set(built.by_business) == {"a", "b", "c", "unmapped", "grid"}
    assert (built.low, built.high) == (10, 20)
    assert built.by_business["a"] == built.by_business["b"]
    assert built.by_business["a"]["weather_station"] == "Airport A"
    assert built.by_business["a"]["weather_source"] == "ACIS daily station observations"
    assert built.by_business["a"]["weather_period"] == "2024-02-01 to 2024-02-03"
    assert built.by_business["a"]["weather_coverage"] == "3 / 3 accepted days"
    assert built.by_business["unmapped"]["weather_color"] == list(layer.UNKNOWN_COLOR)
    assert built.by_business["grid"]["weather_station_id"] is None


def test_explicit_business_ids_and_exact_source_tag_are_both_required() -> None:
    catalog = replace(
        CATALOG,
        businesses=CATALOG.businesses
        + (_business("delivery", categories=("Restaurants", "Pizza Delivery")),),
    )
    context = replace(CONTEXT, catalog=catalog)
    built = layer.build_layer(
        context, ("a", "delivery", "unknown"), START, END, HIGH, (_summary(),)
    )
    assert set(built.by_business) == {"a"}
    assert layer.build_layer(context, (), START, END, HIGH, (_summary(),)).has_values is False


def test_canonical_metro_scope_keeps_original_city_and_state_spelling_variants() -> None:
    businesses = (
        _business("upper", city="PHILADELPHIA", state="pa"),
        _business("whitespace", city="  Philadelphia  ", state=" PA "),
        _business("lower", city="philadelphia", state="PA"),
    )
    context = replace(CONTEXT, catalog=replace(CATALOG, businesses=businesses))
    identifiers = tuple(business.business_id for business in businesses)
    built = layer.build_layer(context, identifiers, START, END, HIGH, (_summary(),))
    assert set(built.by_business) == set(identifiers)
    assert all(row["weather_value"] == 10 for row in built.by_business.values())
    background = layer.build_weather_background(built, context, identifiers, BOUNDARIES)
    assert len(background) == 1
    assert background[0]["properties"]["total_businesses"] == 3
    assert background[0]["properties"]["accepted_businesses"] == 3


def test_low_high_and_rain_use_independent_accepted_day_counts_with_zero_rain_preserved() -> None:
    summaries = (_summary(),)
    high = layer.build_layer(CONTEXT, ("a",), START, END, HIGH, summaries)
    low = layer.build_layer(CONTEXT, ("a",), START, END, LOW, summaries)
    rain = layer.build_layer(CONTEXT, ("a",), START, END, RAIN, summaries)
    assert high.by_business["a"]["weather_value"] == 10
    assert low.by_business["a"]["weather_value"] == -2
    assert low.by_business["a"]["weather_coverage"] == "2 / 3 accepted days"
    assert rain.has_values
    assert (rain.low, rain.high) == (0, 0)
    assert rain.by_business["a"]["weather_text"] == f"{RAIN}: 0.0"
    assert rain.by_business["a"]["weather_coverage"] == "2 / 3 accepted days"
    assert rain.by_business["a"]["weather_color"] != list(layer.UNKNOWN_COLOR)


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), float("-inf")])
def test_missing_or_nonfinite_weather_is_unknown(value: float | None) -> None:
    built = layer.build_layer(CONTEXT, ("a",), START, END, HIGH, (_summary(mean_temp_max_c=value),))
    assert not built.has_values
    assert built.low is None and built.high is None
    assert built.by_business["a"]["weather_value"] is None
    assert built.by_business["a"]["weather_color"] == list(layer.UNKNOWN_COLOR)
    assert "Unknown" in built.by_business["a"]["weather_text"]


@pytest.mark.parametrize(
    "changes",
    [
        {"precipitation_sum_mm": -1.0},
        {"precipitation_days": 0},
        {"precipitation_days": 4},
        {"selected_days": 2},
    ],
)
def test_invalid_rain_or_report_counts_do_not_make_measured_values(changes: dict[str, Any]) -> None:
    built = layer.build_layer(CONTEXT, ("a",), START, END, RAIN, (_summary(**changes),))
    assert not built.has_values
    assert built.by_business["a"]["weather_days"] == 0


def test_serialized_rgba_is_finite_and_geometry_activity_selection_are_unchanged() -> None:
    built = layer.build_layer(
        CONTEXT,
        ("a", "c", "unmapped"),
        START,
        END,
        HIGH,
        (_summary(mean_temp_max_c=-10), _summary("acis_b", mean_temp_max_c=30)),
    )
    points = [
        {
            "business_id": "a",
            "latitude": 40.0,
            "longitude": -75.0,
            "radius": 23.0,
            "line_color": [1, 2, 3, 255],
            "line_width": 3,
        }
    ]
    annotated = layer.annotate(points, built)
    assert all(annotated[0][key] == value for key, value in points[0].items())
    assert "weather_color" not in points[0]
    assert layer.annotate(points, None) == points
    assert layer.annotate(points, None)[0] is not points[0]
    assert built.by_business["a"]["weather_color"] == [33, 102, 172, 210]
    assert built.by_business["c"]["weather_color"] == [178, 24, 43, 210]
    for point in built.by_business.values():
        assert len(point["weather_color"]) == 4
        assert all(
            isinstance(channel, int) and 0 <= channel <= 255 for channel in point["weather_color"]
        )
    json.dumps(annotated, allow_nan=False)
    json.dumps(built.by_business, allow_nan=False)


def test_invalid_dates_and_unknown_metrics_rejected() -> None:
    with pytest.raises(ValueError, match="end date"):
        layer.build_layer(CONTEXT, IDS, END, START, HIGH, ())
    with pytest.raises(ValueError, match="weather metric"):
        layer.build_layer(CONTEXT, IDS, START, END, OFF, ())


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    ui._station_weather.clear()
    monkeypatch.setattr(app_module, "_import_is_unchanged", lambda revision: True)
    monkeypatch.setattr(ui, "_zip_boundaries", lambda: BOUNDARIES)


def _render() -> None:
    import streamlit as st

    from sitesense import business_weather_layer

    result = business_weather_layer.prepare(
        st.session_state["context"],
        st.session_state["ids"],
        st.session_state["start"],
        st.session_state["end"],
        "map_weather",
    )
    st.session_state["layer"] = result
    business_weather_layer.render_legend(result)


def _app() -> AppTest:
    app = AppTest.from_function(_render)
    app.session_state["context"] = CONTEXT
    app.session_state["ids"] = IDS
    app.session_state["start"] = START
    app.session_state["end"] = END
    return app.run()


def test_prepare_deduplicates_exact_mapped_stations_and_uses_weather_dates_not_yelp_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    def load(
        cells: tuple[WeatherCell, ...], start: date, end: date
    ) -> tuple[StationWeatherSummary, ...]:
        calls.append((cells, start, end))
        return (_summary(),)

    monkeypatch.setattr(repository, "load_station_weather", load)
    app = _app()
    assert not app.exception
    assert app.selectbox(key="map_weather").value == HIGH
    assert len(calls) == 1
    assert [cell.weather_cell_id for cell in calls[0][0]] == ["acis_a", "acis_b"]
    assert calls[0][1:] == (START, END)
    assert START > CATALOG.coverage.end_date
    assert app.session_state["layer"].has_values
    captions = " ".join(item.value for item in app.caption)
    assert "Weather: mapped-station observations · Circles: activity · Gray: unknown" in captions
    assert "2 / 5 businesses" in captions
    assert "1 mapped stations" in captions
    assert "not measurements throughout the area or an interpolated weather field" in captions
    assert len(app.session_state["layer"].features) == 3
    app.run()
    app.selectbox(key="map_weather").select(LOW).run()
    assert len(calls) == 1  # Metric changes share the existing station-summary cache.
    app.session_state["context"] = replace(CONTEXT, import_revision="import-two")
    app.run()
    assert len(calls) == 2
    app.session_state["start"] = END
    app.run()
    assert len(calls) == 3 and calls[-1][1:] == (END, END)


def test_precipitation_legend_names_incomplete_sum_and_off_avoids_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda *args: (_summary(),))
    app = _app()
    app.selectbox(key="map_weather").select(RAIN).run()
    assert not app.exception
    assert app.session_state["layer"].by_business["a"]["weather_value"] == 0
    captions = " ".join(item.value for item in app.caption)
    assert "not necessarily rainfall for the full period" in captions
    assert "Trace rainfall counts as 0 mm" in captions

    def fail(*args: Any) -> Any:
        raise AssertionError("Off must not load weather")

    monkeypatch.setattr(ui, "_station_weather", fail)
    app.selectbox(key="map_weather").select(OFF).run()
    assert not app.exception
    assert app.session_state["layer"] is None


def test_all_missing_future_observations_fall_back_without_inventing_weather_forecasts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda *args: ())
    app = _app()
    assert not app.exception
    assert not app.session_state["layer"].has_values
    assert "No accepted observed weather" in app.info[0].value
    assert "not weather forecasts" in app.info[0].value


@pytest.mark.parametrize(
    "error",
    [
        ConfigurationError("hidden secret"),
        psycopg.OperationalError("hidden secret"),
        repository.DataUnavailableError("hidden secret"),
    ],
)
def test_unavailable_loader_falls_back_without_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def fail(*args: Any) -> Any:
        raise error

    monkeypatch.setattr(repository, "load_station_weather", fail)
    app = _app()
    assert not app.exception
    assert app.session_state["layer"] is None
    assert "Activity pins are still shown" in app.info[0].value
    assert "hidden secret" not in app.info[0].value


def test_changed_import_stops_rendering_before_stale_weather_can_be_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(repository, "load_station_weather", lambda *args: (_summary(),))
    monkeypatch.setattr(app_module, "_import_is_unchanged", lambda revision: False)
    app = _app()
    assert not app.exception
    assert "layer" not in app.session_state
    assert not app.caption


def test_unmapped_businesses_do_not_load_unrelated_weather(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any) -> Any:
        raise AssertionError("No mapped ACIS stations should be loaded")

    monkeypatch.setattr(ui, "_station_weather", fail)
    app = AppTest.from_function(_render)
    app.session_state["context"] = CONTEXT
    app.session_state["ids"] = ("unmapped", "grid")
    app.session_state["start"] = START
    app.session_state["end"] = END
    app.run()
    assert not app.exception
    assert not app.session_state["layer"].has_values
    assert set(app.session_state["layer"].by_business) == {"unmapped", "grid"}


def test_background_weights_five_and_three_businesses_once_and_excludes_unknown_reports() -> None:
    businesses = (
        tuple(_business(f"a-{number}") for number in range(5))
        + tuple(_business(f"b-{number}", "acis_b") for number in range(3))
        + (_business("unknown", None),)
    )
    context = replace(CONTEXT, catalog=replace(CATALOG, businesses=businesses))
    identifiers = tuple(business.business_id for business in businesses)
    built = layer.build_layer(
        context,
        identifiers,
        START,
        END,
        HIGH,
        (_summary(), _summary("acis_b", mean_temp_max_c=30, temp_max_days=1)),
    )
    geometry_before = json.dumps(BOUNDARIES, sort_keys=True)
    features = layer.build_weather_background(built, context, identifiers + identifiers, BOUNDARIES)
    assert len(features) == 1
    properties = features[0]["properties"]
    assert properties["value"] == pytest.approx((5 * 10 + 3 * 30) / 8)
    assert properties["accepted_businesses"] == 8
    assert properties["total_businesses"] == 9
    assert properties["weather_days"] == pytest.approx((5 * 3 + 3 * 1) / 8)
    assert properties["weather_station_count"] == 2
    assert properties["weather_color"][3] == 100
    assert properties["boundary_source"] == "Packaged Census ZCTA"
    assert features[0]["geometry"] == BOUNDARIES["19103"]["geometry"]
    assert json.dumps(BOUNDARIES, sort_keys=True) == geometry_before
    assert all("checkin" not in key.lower() for key in properties)
    json.dumps(features, allow_nan=False)


def test_rain_background_averages_station_totals_and_uses_true_zero() -> None:
    businesses = tuple(_business(f"a-{number}") for number in range(5)) + tuple(
        _business(f"b-{number}", "acis_b") for number in range(3)
    )
    context = replace(CONTEXT, catalog=replace(CATALOG, businesses=businesses))
    identifiers = tuple(business.business_id for business in businesses)
    built = layer.build_layer(
        context,
        identifiers,
        START,
        END,
        RAIN,
        (
            _summary(precipitation_days=3),
            _summary("acis_b", precipitation_sum_mm=8, precipitation_days=1),
        ),
    )
    properties = layer.build_weather_background(built, context, identifiers, BOUNDARIES)[0][
        "properties"
    ]
    assert properties["value"] == pytest.approx(3.0)
    assert properties["value"] != 24.0  # Never sum rain across eight businesses.
    assert properties["unit"] == "mm"
    assert properties["weather_days"] == pytest.approx(18 / 8)
    assert properties["weather_period"] == "2024-02-01 to 2024-02-03"


def test_background_has_no_listing_threshold_and_keeps_unknown_area_gray() -> None:
    built = layer.build_layer(CONTEXT, ("a", "grid"), START, END, HIGH, (_summary(),))
    features = layer.build_weather_background(built, CONTEXT, ("a", "grid"), BOUNDARIES)
    assert [feature["id"] for feature in features] == ["19103", "19105"]
    assert features[0]["properties"]["accepted_businesses"] == 1
    assert features[0]["properties"]["total_businesses"] == 1
    assert features[1]["properties"]["value"] is None
    assert features[1]["properties"]["weather_color"] == list(layer.UNKNOWN_COLOR[:3]) + [100]
    assert features[1]["properties"]["weather_days"] is None
    assert features[1]["properties"]["weather_coverage"] == "0 / 1 businesses; no accepted days"


def test_background_scale_uses_representative_values_and_excludes_missing_or_unshaded_stations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    businesses = (
        _business("area1-a"),
        _business("area1-b", "acis_b"),
        _business("area2-a", area_id=2),
        _business("unknown", None, area_id=3),
        _business("unshaded-outlier", "acis_unrelated", area_id=None),
    )
    context = replace(CONTEXT, catalog=replace(CATALOG, businesses=businesses))
    identifiers = tuple(business.business_id for business in businesses)
    summaries = (
        _summary(),
        _summary("acis_b", mean_temp_max_c=30),
        _summary("acis_unrelated", mean_temp_max_c=99),
    )
    built = layer.build_layer(context, identifiers, START, END, HIGH, summaries)
    assert built.high == 99
    features = layer.build_weather_background(built, context, identifiers, BOUNDARIES)
    assert [feature["properties"]["value"] for feature in features] == [20, 10, None]
    assert features[0]["properties"]["weather_color"] == [178, 24, 43, 100]
    assert features[1]["properties"]["weather_color"] == [33, 102, 172, 100]
    assert features[2]["properties"]["weather_color"] == list(layer.UNKNOWN_COLOR[:3]) + [100]

    monkeypatch.setattr(repository, "load_station_weather", lambda *args: summaries)
    app = AppTest.from_function(_render)
    app.session_state["context"] = context
    app.session_state["ids"] = identifiers
    app.session_state["start"] = START
    app.session_state["end"] = END
    app.run()
    assert not app.exception
    rendered = app.session_state["layer"]
    assert (rendered.low, rendered.high) == (10, 20)
    assert rendered.by_business["unshaded-outlier"]["weather_value"] == 99
    assert rendered.features == features


def test_background_respects_exact_scope_missing_area_geometry_and_foreign_area() -> None:
    businesses = (
        _business("a"),
        _business("no-area", area_id=None),
        _business("foreign-area", area_id=4),
    )
    foreign = CandidateArea(4, "Nashville", "TN", "37201", "Elsewhere", None, None)
    context = replace(
        CONTEXT, catalog=replace(CATALOG, businesses=businesses, areas=CATALOG.areas + (foreign,))
    )
    identifiers = tuple(business.business_id for business in businesses)
    built = layer.build_layer(context, identifiers, START, END, HIGH, (_summary(),))
    assert layer.build_weather_background(built, context, ("no-area",), BOUNDARIES) == []
    assert (
        layer.build_weather_background(
            built, context, ("foreign-area",), {"37201": _boundary("37201")}
        )
        == []
    )
    assert layer.build_weather_background(built, context, identifiers, {}) == []
    assert set(built.by_business) == set(identifiers)  # Missing polygons never discard businesses.
    wrong_geometry = {"19103": _boundary("19104")}
    assert layer.build_weather_background(built, context, identifiers, wrong_geometry) == []
    broken = _boundary("19103")
    broken["geometry"]["coordinates"][0][0][0] = float("nan")
    assert layer.build_weather_background(built, context, identifiers, {"19103": broken}) == []


def test_background_rejects_invalid_dates_and_all_missing_weather_without_loading_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built = layer.build_layer(CONTEXT, ("a",), START, END, HIGH, (_summary(),))
    assert built.features == []
    with pytest.raises(ValueError, match="end date"):
        layer.build_weather_background(
            replace(built, start_date=END, end_date=START), CONTEXT, IDS, {}
        )
    missing = layer.build_layer(CONTEXT, ("a",), START, END, HIGH, ())
    assert layer.build_weather_background(missing, CONTEXT, IDS, BOUNDARIES) == []

    def fail() -> Any:
        raise AssertionError("Missing future weather must not load boundaries")

    monkeypatch.setattr(repository, "load_station_weather", lambda *args: ())
    monkeypatch.setattr(ui, "_zip_boundaries", fail)
    app = _app()
    assert not app.exception
    assert app.session_state["layer"].features == []


@pytest.mark.parametrize(
    "error",
    [OSError("hidden secret"), zip_boundaries.BoundaryDataError("hidden secret")],
)
def test_unavailable_boundary_fallback_keeps_business_weather_and_sanitizes_message(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    def fail() -> Any:
        raise error

    monkeypatch.setattr(repository, "load_station_weather", lambda *args: (_summary(),))
    monkeypatch.setattr(ui, "_zip_boundaries", fail)
    app = _app()
    assert not app.exception
    assert app.session_state["layer"].has_values
    assert app.session_state["layer"].features == []
    assert app.session_state["layer"].by_business["a"]["weather_value"] == 10
    assert "Weather boundaries are unavailable" in app.info[0].value
    assert "hidden secret" not in app.info[0].value
    assert not app.caption
