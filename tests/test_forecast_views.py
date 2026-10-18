"""Forecast views preserve missing dates and the model's cohort-rate unit."""

import json
from dataclasses import replace
from datetime import date
from typing import Any

import altair as alt
import pytest
from streamlit.testing.v1 import AppTest

from sitesense.forecast_range import AreaForecast, ForecastPoint, ForecastRange
from sitesense.forecast_views import (
    DISPLAY_RATE_UNIT,
    RATE_UNIT,
    _activity_weather_rows,
    _forecast_gap_rows,
    _forecast_rows,
    _recorded_rows,
    _timeline_weather_rows,
    _view_activity_rows,
    forecast_export,
)
from sitesense.models import ActivityRecord


def _point(day: int, rate: float, *, area_id: int = 1) -> ForecastPoint:
    return ForecastPoint(
        area_id=area_id,
        day=date(2022, 1, day),
        rate=rate,
        baseline_rate=0.001,
        weather_source="observed_station_weather",
        station_id="station-a",
        tmax_c=12.0,
        tmin_c=3.0,
        precip_mm=1.0,
        outside_training_weather_range=False,
    )


def _result() -> ForecastRange:
    return ForecastRange(
        areas=(
            AreaForecast(
                area_id=1,
                points=(_point(20, 0.001), _point(22, 0.003)),
                requested_days=3,
                reason="Station weather is unavailable on one date.",
            ),
        ),
        uncovered_days=3,
        model_name="hgb_weather",
        trained_end=date(2021, 12, 31),
    )


def _area_script() -> None:
    import streamlit as st

    from sitesense.forecast_views import render_area_forecast

    render_area_forecast(st.session_state["forecast"], 1, heading=False)


def test_area_view_explains_partial_forecast_without_changing_rate_unit() -> None:
    app = AppTest.from_function(_area_script)
    app.session_state["forecast"] = _result()
    app.run()

    assert not app.exception
    assert [(metric.label, metric.value) for metric in app.metric] == [
        ("Forecast check-ins", "0.2")
    ]
    captions = " ".join(caption.value for caption in app.caption)
    assert DISPLAY_RATE_UNIT.capitalize() in captions
    assert "Estimated days: 2; unavailable days: 1" in captions
    assert "baseline" not in captions.lower()
    assert "retrospective conditional estimates" in captions
    assert "Station weather is unavailable on one date" in captions
    assert not app.markdown


def test_area_view_keeps_hgb_rate_when_baseline_has_partial_coverage() -> None:
    result = _result()
    first, second = result.areas[0].points
    area = replace(result.areas[0], points=(first, replace(second, baseline_rate=None)))
    app = AppTest.from_function(_area_script)
    app.session_state["forecast"] = replace(result, areas=(area,))
    app.run()

    assert not app.exception
    captions = " ".join(caption.value for caption in app.caption)
    assert "baseline" not in captions.lower()
    assert app.metric[0].value == "0.2"


def test_unavailable_area_renders_reason_without_zero_forecast() -> None:
    app = AppTest.from_function(_area_script)
    app.session_state["forecast"] = ForecastRange(
        areas=(), uncovered_days=7, reason="Forecast model unavailable."
    )
    app.run()

    assert not app.exception
    assert app.metric[0].value == "Unavailable"
    assert "Estimated days: 0; unavailable days: 7" in app.caption[0].value
    assert app.caption[1].value == "Forecast model unavailable."


def test_recorded_only_range_has_no_forecast_view() -> None:
    app = AppTest.from_function(_area_script)
    app.session_state["forecast"] = ForecastRange(areas=(), uncovered_days=0)
    app.run()

    assert not app.exception
    assert not app.metric
    assert not app.caption


def test_trend_keeps_each_zip_and_missing_date_in_separate_segments() -> None:
    result = replace(
        _result(),
        areas=(
            AreaForecast(1, (_point(20, 0.0), _point(21, 0.002), _point(23, 0.004)), 4),
            AreaForecast(2, (_point(20, 0.005, area_id=2),), 4),
        ),
    )
    rows = _forecast_rows(result, (1, 2), {1: "Rittenhouse (19103)", 2: "Old City (19106)"})

    assert [row["Segment"] for row in rows] == ["1:0", "1:0", "1:1", "2:0"]
    assert rows[0]["Forecast check-ins"] == 0.0
    assert rows[1]["Forecast check-ins"] == 0.2
    assert rows[0]["ZIP area"] == "Rittenhouse (19103)"
    assert all("baseline" not in key.lower() for row in rows for key in row)
    assert rows[0]["Weather station"] == "station-a"
    assert all(row["Date"] != "2022-01-22" for row in rows)


def test_native_trend_connects_available_estimates_without_segment_breaks() -> None:
    def script() -> None:
        import streamlit as st

        from sitesense.forecast_views import render_forecast_trend

        render_forecast_trend(st.session_state["forecast"], (1,), {1: "Rittenhouse (19103)"})

    app = AppTest.from_function(script)
    app.session_state["forecast"] = _result()
    app.run()

    assert not app.exception
    assert app.subheader[0].value == "Forecast for uncovered dates"
    chart = app.get("vega_lite_chart")[0]
    spec = json.loads(chart.proto.spec)
    assert spec["mark"]["strokeDash"] == [6, 4]
    assert "detail" not in spec["encoding"]
    assert "100 businesses" in spec["encoding"]["y"]["title"]
    assert spec["encoding"]["y"]["field"] == "Forecast check-ins"
    assert spec["encoding"]["y"]["axis"]["format"] == ".1f"
    assert "baseline" not in chart.proto.spec.lower()
    assert "retrospective conditional estimates" in app.caption[0].value
    assert DISPLAY_RATE_UNIT in app.caption[0].value


def test_recorded_rows_sum_hours_and_keep_covered_zeros_without_forecast_zeroes() -> None:
    rows = _recorded_rows(
        (
            ActivityRecord(1, date(2022, 1, 18), 8, 2),
            ActivityRecord(1, date(2022, 1, 18), 12, 3),
            ActivityRecord(1, date(2022, 1, 17), 12, 99),
            ActivityRecord(1, date(2022, 1, 20), 12, 99),
            ActivityRecord(99, date(2022, 1, 18), 12, 99),
        ),
        (1, 2),
        {1: "Old City", 2: "Rittenhouse"},
        (date(2022, 1, 17), date(2022, 1, 19)),
        date(2022, 1, 18),
        date(2022, 1, 22),
    )

    assert [(row["ZIP area"], row["Date"], row["Recorded check-ins"]) for row in rows] == [
        ("Old City", "2022-01-18", 5),
        ("Old City", "2022-01-19", 0),
        ("Rittenhouse", "2022-01-18", 0),
        ("Rittenhouse", "2022-01-19", 0),
    ]
    assert all(row["Activity source"] == "Recorded activity" for row in rows)


def test_forecast_rows_clip_selection_and_do_not_overwrite_recorded_dates() -> None:
    result = replace(
        _result(),
        areas=(AreaForecast(1, (_point(19, 0.9), _point(20, 0.001), _point(22, 0.003)), 3),),
    )
    rows = _forecast_rows(
        result,
        (1,),
        {1: "Old City"},
        start=date(2022, 1, 18),
        end=date(2022, 1, 21),
        covered=(date(2022, 1, 18), date(2022, 1, 19)),
    )

    assert [(row["Date"], row["Forecast check-ins"]) for row in rows] == [("2022-01-20", 0.1)]
    assert rows[0]["Activity source"] == "Forecast check-ins"


def _comparison_script() -> None:
    from datetime import date

    import streamlit as st

    from sitesense.forecast_views import render_activity_forecast_comparison

    render_activity_forecast_comparison(
        st.session_state["forecast"],
        (1, 2),
        {1: "Old City", 2: "Rittenhouse"},
        st.session_state["records"],
        st.session_state["covered"],
        date(2022, 1, 18),
        date(2022, 1, 22),
    )


def test_comparison_uses_one_timeline_with_explicit_count_and_rate_axes() -> None:
    app = AppTest.from_function(_comparison_script)
    app.session_state["forecast"] = _result()
    app.session_state["records"] = (ActivityRecord(1, date(2022, 1, 18), 12, 7),)
    app.session_state["covered"] = (date(2022, 1, 18), date(2022, 1, 19))
    app.run()

    assert not app.exception
    assert app.subheader[0].value == "Recorded activity and forecast"
    charts = app.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    recorded, estimated = spec["layer"]
    assert spec["resolve"]["scale"] == {"y": "independent", "color": "shared"}
    assert spec["encoding"]["x"]["scale"]["domain"] == ["2022-01-18", "2022-01-22"]
    assert "strokeDash" not in recorded["mark"]
    assert recorded["encoding"]["y"]["field"] == "Recorded check-ins"
    assert recorded["encoding"]["y"]["axis"]["orient"] == "left"
    assert estimated["mark"]["strokeDash"] == [6, 4]
    assert estimated["encoding"]["y"]["field"] == "Forecast check-ins"
    assert estimated["encoding"]["y"]["axis"]["orient"] == "right"
    assert "detail" not in estimated["encoding"]
    assert recorded["encoding"]["color"] == estimated["encoding"]["color"]
    assert recorded["encoding"]["color"]["scale"]["domain"] == ["Old City", "Rittenhouse"]
    assert "baseline" not in charts[0].proto.spec.lower()
    captions = " ".join(caption.value for caption in app.caption)
    assert DISPLAY_RATE_UNIT in captions
    assert "heights are not directly comparable" in captions


def test_comparison_without_recorded_coverage_shows_only_one_forecast_chart() -> None:
    app = AppTest.from_function(_comparison_script)
    app.session_state["forecast"] = _result()
    app.session_state["records"] = (ActivityRecord(1, date(2022, 1, 18), 12, 7),)
    app.session_state["covered"] = None
    app.run()

    assert not app.exception
    assert app.subheader[0].value == "Forecast for uncovered dates"
    charts = app.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    assert "layer" not in spec
    assert spec["encoding"]["y"]["field"] == "Forecast check-ins"


def _historical_weather() -> list[dict[str, object]]:
    return [
        {
            "Date": "2022-01-18",
            "Mean daily high °C": 0.0,
            "Mean daily low °C": -5.0,
            "Precipitation mm": 0.0,
        },
        {
            "Date": "2022-01-19",
            "Mean daily high °C": None,
            "Mean daily low °C": None,
            "Precipitation mm": None,
        },
        {
            "Date": "2022-01-20",
            "Mean daily high °C": 999.0,
            "Mean daily low °C": 999.0,
            "Precipitation mm": 999.0,
        },
    ]


@pytest.mark.parametrize(
    ("metric", "expected_actual", "expected_forecast", "title"),
    [
        ("Daily high temperature", 0.0, 12.0, "Daily high temperature (°C)"),
        ("Daily low temperature", -5.0, 3.0, "Daily low temperature (°C)"),
        ("Precipitation", 0.0, 1.0, "Precipitation (mm/day)"),
    ],
)
def test_weather_pairs_match_dates_and_exact_forecast_inputs(
    metric: str, expected_actual: float, expected_forecast: float, title: str
) -> None:
    recorded = _recorded_rows(
        (ActivityRecord(1, date(2022, 1, 18), 12, 7),),
        (1,),
        {1: "Old City"},
        (date(2022, 1, 18), date(2022, 1, 19)),
        date(2022, 1, 18),
        date(2022, 1, 22),
    )
    estimated = _forecast_rows(_result(), (1,), {1: "Old City"})
    rows = _activity_weather_rows(recorded, estimated, _historical_weather(), metric)

    assert [row["Date"] for row in rows] == ["2022-01-18", "2022-01-20", "2022-01-22"]
    assert [row["Weather value"] for row in rows] == [
        expected_actual,
        expected_forecast,
        expected_forecast,
    ]
    assert rows[0]["Recorded check-ins"] == 7
    assert rows[1]["Forecast check-ins"] == 0.1
    assert rows[2]["Forecast check-ins"] == 0.3
    assert all(row["Weather metric"] == title for row in rows)
    assert [row["Weather source"] for row in rows] == [
        "Recorded-period observations",
        "Observed model inputs",
        "Observed model inputs",
    ]
    assert rows[1]["Weather station"] == "station-a"
    assert rows[1]["Input weather source"] == "observed_station_weather"
    assert all("baseline" not in key.lower() for row in rows for key in row)


def test_weather_pairs_keep_covered_activity_zeros_and_true_weather_zeros() -> None:
    recorded = _recorded_rows(
        (),
        (1,),
        {1: "Old City"},
        (date(2022, 1, 18), date(2022, 1, 19)),
        date(2022, 1, 18),
        date(2022, 1, 22),
    )
    result = replace(
        _result(),
        areas=(AreaForecast(1, (replace(_point(20, 0.0), precip_mm=0.0),), 3),),
    )
    rows = _activity_weather_rows(
        recorded,
        _forecast_rows(result, (1,), {1: "Old City"}),
        _historical_weather(),
        "Precipitation",
    )

    assert len(rows) == 2
    assert rows[0]["Recorded check-ins"] == 0
    assert rows[1]["Forecast check-ins"] == 0.0
    assert [row["Weather value"] for row in rows] == [0.0, 0.0]


def test_weather_pairs_skip_only_missing_selected_measurements_and_never_fallback() -> None:
    recorded = _recorded_rows(
        (),
        (1,),
        {1: "Old City"},
        (date(2022, 1, 18), date(2022, 1, 19)),
        date(2022, 1, 18),
        date(2022, 1, 22),
    )
    historical = [
        {"Date": "2022-01-17", "Mean daily high °C": 999.0},
        {"Date": "2022-01-18", "Mean daily high °C": None, "Mean daily low °C": 0.0},
        {"Date": "2022-01-19", "Mean daily high °C": 8.0, "Mean daily low °C": None},
        {"Date": "2022-01-20", "Mean daily high °C": 999.0, "Mean daily low °C": 999.0},
    ]
    result = replace(
        _result(),
        areas=(AreaForecast(1, (replace(_point(20, 0.001), tmax_c=float("nan")),), 3),),
    )
    estimated = _forecast_rows(result, (1,), {1: "Old City"})
    high = _activity_weather_rows(recorded, estimated, historical, "Daily high temperature")
    low = _activity_weather_rows(recorded, estimated, historical, "Daily low temperature")

    assert [(row["Date"], row["Weather value"]) for row in high] == [("2022-01-19", 8.0)]
    assert [(row["Date"], row["Weather value"]) for row in low] == [
        ("2022-01-18", 0.0),
        ("2022-01-20", 3.0),
    ]


def _activity_weather_script() -> None:
    from datetime import date

    import streamlit as st

    from sitesense.forecast_views import render_activity_weather_relationship

    render_activity_weather_relationship(
        st.session_state["forecast"],
        1,
        "Old City",
        st.session_state["records"],
        st.session_state["covered"],
        date(2022, 1, 18),
        date(2022, 1, 22),
        st.session_state["weather"],
        st.session_state["weather_metric"],
        cohort_records=st.session_state.get("cohort_records"),
        cohort_businesses=st.session_state.get("cohort_businesses"),
    )


def _assert_single_scatter_plot(spec: dict[str, Any]) -> None:
    """A single chart element must also contain just one plotting region."""

    def check(node: Any) -> None:
        if isinstance(node, dict):
            assert not set(node).intersection({"vconcat", "hconcat", "concat", "facet"})
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(spec)
    assert all(layer["mark"]["type"] == "point" for layer in spec["layer"])
    alt.LayerChart.from_dict(spec, validate=True)


def test_relationship_uses_one_weather_scatter_with_explicit_count_and_rate_axes() -> None:
    app = AppTest.from_function(_activity_weather_script)
    app.session_state["forecast"] = _result()
    app.session_state["records"] = (ActivityRecord(1, date(2022, 1, 18), 12, 7),)
    app.session_state["covered"] = (date(2022, 1, 18), date(2022, 1, 19))
    app.session_state["weather"] = _historical_weather()
    app.session_state["weather_metric"] = "Daily high temperature"
    app.run()

    assert not app.exception
    assert [heading.value for heading in app.subheader] == ["Activity and weather relationship"]
    charts = app.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    _assert_single_scatter_plot(spec)
    assert spec["encoding"]["x"]["field"] == "Weather value"
    assert spec["encoding"]["x"]["title"] == "Daily high temperature (°C)"
    assert spec["resolve"]["scale"] == {"y": "independent", "color": "shared", "shape": "shared"}
    recorded, estimated = spec["layer"]
    assert all("x" not in layer["encoding"] for layer in spec["layer"])
    assert recorded["encoding"]["y"]["field"] == "Recorded check-ins"
    assert recorded["encoding"]["y"]["axis"]["orient"] == "left"
    assert estimated["encoding"]["y"]["field"] == "Forecast check-ins"
    assert estimated["encoding"]["y"]["axis"]["orient"] == "right"
    color = recorded["encoding"]["color"]
    assert estimated["encoding"]["color"] == color
    assert color["legend"]["columns"] == 2
    assert color["scale"] == {
        "domain": ["Recorded activity", "Forecast check-ins"],
        "range": ["#1b9e77", "#756bb1"],
    }
    assert recorded["encoding"]["shape"] == estimated["encoding"]["shape"]
    assert recorded["encoding"]["shape"]["scale"]["range"] == ["circle", "diamond"]
    assert "baseline" not in charts[0].proto.spec.lower()
    captions = " ".join(caption.value for caption in app.caption)
    assert "Recorded pairs: 1 of 2 covered days; forecast pairs: 2 of 3 uncovered days" in captions
    assert DISPLAY_RATE_UNIT in captions
    assert "heights are not directly comparable" in captions
    assert "not independent evidence of a weather effect" in captions
    assert "not an operational forecast" in captions


@pytest.mark.parametrize(
    ("metric", "title"),
    [
        ("Daily high temperature", "Daily high temperature (°C)"),
        ("Daily low temperature", "Daily low temperature (°C)"),
        ("Precipitation", "Precipitation (mm/day)"),
    ],
)
def test_forecast_only_relationship_has_one_rate_axis_and_selected_weather_unit(
    metric: str, title: str
) -> None:
    app = AppTest.from_function(_activity_weather_script)
    app.session_state["forecast"] = _result()
    app.session_state["records"] = ()
    app.session_state["covered"] = None
    app.session_state["weather"] = _historical_weather()
    app.session_state["weather_metric"] = metric
    app.run()

    assert not app.exception
    charts = app.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    _assert_single_scatter_plot(spec)
    (activity,) = spec["layer"]
    assert activity["encoding"]["y"]["field"] == "Forecast check-ins"
    assert activity["encoding"]["y"]["axis"]["orient"] == "left"
    assert spec["encoding"]["x"]["title"] == title
    assert spec["encoding"]["x"]["scale"]["zero"] is (metric == "Precipitation")


def test_recorded_only_relationship_has_one_count_axis() -> None:
    app = AppTest.from_function(_activity_weather_script)
    app.session_state["forecast"] = ForecastRange(areas=(), uncovered_days=0)
    app.session_state["records"] = (ActivityRecord(1, date(2022, 1, 18), 12, 7),)
    app.session_state["covered"] = (date(2022, 1, 18), date(2022, 1, 19))
    app.session_state["weather"] = _historical_weather()
    app.session_state["weather_metric"] = "Daily high temperature"
    app.run()

    assert not app.exception
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    _assert_single_scatter_plot(spec)
    (activity,) = spec["layer"]
    assert activity["encoding"]["y"]["field"] == "Recorded check-ins"
    assert activity["encoding"]["y"]["axis"]["orient"] == "left"
    assert activity["encoding"]["color"]["scale"]["domain"] == ["Recorded activity"]


def test_relationship_without_complete_pairs_shows_counts_instead_of_empty_plot() -> None:
    app = AppTest.from_function(_activity_weather_script)
    app.session_state["forecast"] = ForecastRange(areas=(), uncovered_days=3)
    app.session_state["records"] = (ActivityRecord(1, date(2022, 1, 18), 12, 7),)
    app.session_state["covered"] = (date(2022, 1, 18), date(2022, 1, 19))
    app.session_state["weather"] = []
    app.session_state["weather_metric"] = "Daily high temperature"
    app.run()

    assert not app.exception
    assert not app.get("vega_lite_chart")
    assert (
        "Recorded pairs: 0 of 2 covered days; forecast pairs: 0 of 3 uncovered days"
        in app.caption[0].value
    )
    assert "No complete same-day" in app.caption[1].value


def test_weather_timeline_breaks_on_missing_dates_and_adjacent_source_changes() -> None:
    recorded = _recorded_rows(
        (),
        (1,),
        {1: "Old City"},
        (date(2022, 1, 18), date(2022, 1, 19)),
        date(2022, 1, 18),
        date(2022, 1, 22),
    )
    pairs = _activity_weather_rows(
        recorded,
        _forecast_rows(_result(), (1,), {1: "Old City"}),
        [
            {"Date": "2022-01-18", "Mean daily high °C": 0.0},
            {"Date": "2022-01-19", "Mean daily high °C": 8.0},
        ],
        "Daily high temperature",
    )
    rows = _timeline_weather_rows(pairs, "Daily high temperature")

    assert [row["Date"] for row in rows] == ["2022-01-18", "2022-01-19", "2022-01-20", "2022-01-22"]
    assert [row["Weather value"] for row in rows] == [0.0, 8.0, 12.0, 12.0]
    assert [row["Weather segment"] for row in rows] == ["0", "0", "1", "2"]
    assert all(row["Activity source"] == "Observed weather" for row in rows)
    assert all(row["Series"] == "Daily high temperature" for row in rows)
    assert rows[2]["Weather station"] == "station-a"


def _activity_weather_timeline_script() -> None:
    from datetime import date

    import streamlit as st

    from sitesense.forecast_views import render_activity_weather_timeline

    render_activity_weather_timeline(
        st.session_state["forecast"],
        1,
        "Old City",
        st.session_state["records"],
        st.session_state["covered"],
        date(2022, 1, 18),
        date(2022, 1, 22),
        st.session_state["weather"],
        st.session_state["weather_metric"],
        cohort_records=st.session_state.get("cohort_records"),
        cohort_businesses=st.session_state.get("cohort_businesses"),
    )


def _assert_single_line_plot(spec: dict[str, Any]) -> None:
    def check(node: Any) -> None:
        if isinstance(node, dict):
            assert not set(node).intersection({"vconcat", "hconcat", "concat", "facet"})
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(spec)
    assert all(layer["mark"]["type"] in {"line", "rect"} for layer in spec["layer"])
    alt.LayerChart.from_dict(spec, validate=True)


def test_line_view_shares_one_date_plot_with_independent_count_rate_weather_axes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import streamlit as st

    captured: list[list[dict[str, Any]]] = []
    original_chart = st.vega_lite_chart

    def capture_chart(data: Any, *args: Any, **kwargs: Any) -> Any:
        captured.append(data)
        return original_chart(data, *args, **kwargs)

    monkeypatch.setattr(st, "vega_lite_chart", capture_chart)
    app = AppTest.from_function(_activity_weather_timeline_script)
    app.session_state["forecast"] = _result()
    app.session_state["records"] = (ActivityRecord(1, date(2022, 1, 18), 12, 7),)
    app.session_state["covered"] = (date(2022, 1, 18), date(2022, 1, 19))
    app.session_state["weather"] = _historical_weather()
    app.session_state["weather_metric"] = "Daily high temperature"
    app.run()

    assert not app.exception
    assert [heading.value for heading in app.subheader] == [
        "Activity, forecast and weather over time"
    ]
    assert len(captured) == 1
    rows = captured[0]
    recorded_rows = [row for row in rows if row["Activity source"] == "Recorded activity"]
    estimated_rows = [row for row in rows if row["Activity source"] == "Forecast check-ins"]
    weather_rows = [row for row in rows if row["Activity source"] == "Observed weather"]
    assert [(row["Date"], row["Recorded check-ins"]) for row in recorded_rows] == [
        ("2022-01-18", 7),
        ("2022-01-19", 0),
    ]
    assert [(row["Date"], row["Forecast check-ins"]) for row in estimated_rows] == [
        ("2022-01-20", 0.1),
        ("2022-01-22", 0.3),
    ]
    assert [row["Segment"] for row in estimated_rows] == ["1:0", "1:1"]
    assert [(row["Date"], row["Weather value"]) for row in weather_rows] == [
        ("2022-01-18", 0.0),
        ("2022-01-20", 12.0),
        ("2022-01-22", 12.0),
    ]
    assert [row["Weather segment"] for row in weather_rows] == ["0", "1", "2"]
    charts = app.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    _assert_single_line_plot(spec)
    assert spec["encoding"]["x"]["field"] == "Date"
    assert spec["encoding"]["x"]["type"] == "temporal"
    assert spec["encoding"]["x"]["title"] == "Date"
    assert spec["encoding"]["x"]["axis"]["title"] == "Date"
    assert spec["encoding"]["x"]["scale"]["domain"] == ["2022-01-18", "2022-01-22"]
    assert spec["resolve"]["scale"] == {"y": "independent", "color": "shared"}
    recorded, estimated, weather = [layer for layer in spec["layer"] if "y" in layer["encoding"]]
    assert all("x" not in layer["encoding"] for layer in (recorded, estimated, weather))
    assert recorded["encoding"]["y"]["field"] == "Recorded check-ins"
    assert recorded["encoding"]["y"]["axis"]["orient"] == "left"
    assert "strokeDash" not in recorded["mark"]
    assert estimated["encoding"]["y"]["field"] == "Forecast check-ins"
    assert estimated["encoding"]["y"]["axis"]["orient"] == "right"
    assert estimated["encoding"]["y"]["axis"]["offset"] == 55
    assert estimated["mark"]["strokeDash"] == [6, 4]
    assert "detail" not in estimated["encoding"]
    assert weather["encoding"]["y"]["field"] == "Weather value"
    assert weather["encoding"]["y"]["axis"]["orient"] == "right"
    assert weather["encoding"]["y"]["axis"]["offset"] == 0
    assert "detail" not in weather["encoding"]
    assert "strokeDash" not in weather["encoding"]
    color = recorded["encoding"]["color"]
    assert estimated["encoding"]["color"] == weather["encoding"]["color"] == color
    assert color["legend"]["columns"] == 1
    assert color["scale"] == {
        "domain": ["Recorded activity", "Forecast check-ins", "Daily high temperature"],
        "range": ["#1b9e77", "#756bb1", "#e67e22"],
    }
    assert "baseline" not in charts[0].proto.spec.lower()
    captions = " ".join(caption.value for caption in app.caption)
    assert "heights are not directly comparable" in captions
    assert "line connects available observed measurements" in captions
    assert "Lines connect known points across missing dates" in captions
    assert "Gray bands mark dates without a forecast estimate" in captions
    assert "not an operational forecast" in captions


@pytest.mark.parametrize(
    ("metric", "weather_axis"),
    [
        ("Daily high temperature", "Weather °C"),
        ("Daily low temperature", "Weather °C"),
        ("Precipitation", "Rain mm/day"),
    ],
)
def test_forecast_only_line_view_has_rate_left_and_selected_weather_right(
    metric: str, weather_axis: str
) -> None:
    app = AppTest.from_function(_activity_weather_timeline_script)
    app.session_state["forecast"] = _result()
    app.session_state["records"] = ()
    app.session_state["covered"] = None
    app.session_state["weather"] = _historical_weather()
    app.session_state["weather_metric"] = metric
    app.run()

    assert not app.exception
    charts = app.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    _assert_single_line_plot(spec)
    activity, weather = [layer for layer in spec["layer"] if "y" in layer["encoding"]]
    assert activity["encoding"]["y"]["field"] == "Forecast check-ins"
    assert activity["encoding"]["y"]["axis"]["orient"] == "left"
    assert activity["encoding"]["y"]["axis"]["offset"] == 0
    assert weather["encoding"]["y"]["axis"]["orient"] == "right"
    assert weather["encoding"]["y"]["axis"]["offset"] == 0
    assert weather["encoding"]["y"]["title"] == weather_axis
    assert weather["encoding"]["y"]["scale"]["zero"] is (metric == "Precipitation")


def test_line_view_retains_recorded_days_when_no_selected_weather_is_available() -> None:
    app = AppTest.from_function(_activity_weather_timeline_script)
    app.session_state["forecast"] = ForecastRange(areas=(), uncovered_days=0)
    app.session_state["records"] = (ActivityRecord(1, date(2022, 1, 18), 12, 7),)
    app.session_state["covered"] = (date(2022, 1, 18), date(2022, 1, 19))
    app.session_state["weather"] = []
    app.session_state["weather_metric"] = "Daily high temperature"
    app.run()

    assert not app.exception
    charts = app.get("vega_lite_chart")
    assert len(charts) == 1
    spec = json.loads(charts[0].proto.spec)
    _assert_single_line_plot(spec)
    (activity,) = spec["layer"]
    assert activity["encoding"]["y"]["field"] == "Recorded check-ins"
    assert activity["encoding"]["y"]["axis"]["orient"] == "left"
    assert "No observed weather values" in app.caption[1].value


def test_verified_cohort_normalization_uses_filtered_records_and_exact_denominator() -> None:
    result = _result()
    recorded, estimated, normalized = _view_activity_rows(
        result,
        1,
        "Old City",
        (ActivityRecord(1, date(2022, 1, 18), 12, 999),),
        (date(2022, 1, 18), date(2022, 1, 19)),
        date(2022, 1, 18),
        date(2022, 1, 22),
        (
            ActivityRecord(1, date(2022, 1, 18), 12, 25),
            ActivityRecord(1, date(2022, 1, 19), 12, 10),
        ),
        50,
    )

    assert normalized
    assert [row["Recorded check-ins"] for row in recorded] == [25, 10]
    assert [row["Recorded cohort check-ins"] for row in recorded] == [25, 10]
    assert [row["Activity check-ins"] for row in recorded] == [50.0, 20.0]
    assert [row["Activity check-ins"] for row in estimated] == [0.1, 0.3]
    assert all(row["Historical cohort businesses"] == 50 for row in recorded + estimated)
    assert all(not row["Is forecast"] for row in recorded)
    assert all(row["Is forecast"] for row in estimated)
    assert result.areas[0].mean_rate == 0.002


@pytest.mark.parametrize(
    ("cohort_records", "cohort_businesses"),
    [(None, 50), ((), None), ((), 0), ((), -1)],
)
def test_unverified_cohort_metadata_keeps_raw_counts_and_no_common_rate(
    cohort_records: tuple[ActivityRecord, ...] | None, cohort_businesses: int | None
) -> None:
    recorded, estimated, normalized = _view_activity_rows(
        _result(),
        1,
        "Old City",
        (ActivityRecord(1, date(2022, 1, 18), 12, 999),),
        (date(2022, 1, 18), date(2022, 1, 19)),
        date(2022, 1, 18),
        date(2022, 1, 22),
        cohort_records,
        cohort_businesses,
    )

    assert not normalized
    assert recorded[0]["Recorded check-ins"] == 999
    assert all("Activity check-ins" not in row for row in recorded + estimated)


def test_missing_forecast_bands_describe_only_unavailable_dates_without_values() -> None:
    estimated = _forecast_rows(_result(), (1,), {1: "Old City"})
    bands = _forecast_gap_rows(
        estimated,
        (date(2022, 1, 18), date(2022, 1, 19)),
        date(2022, 1, 17),
        date(2022, 1, 23),
    )

    assert [(row["Unavailable from"], row["Unavailable through"]) for row in bands] == [
        ("2022-01-17", "2022-01-17"),
        ("2022-01-21", "2022-01-21"),
        ("2022-01-23", "2022-01-23"),
    ]
    assert bands[1]["Band start"] == "2022-01-20T12:00:00"
    assert bands[1]["Band end"] == "2022-01-21T12:00:00"
    assert all(row["Activity source"] == "Unavailable forecast day" for row in bands)
    assert all(
        key not in row
        for row in bands
        for key in (
            "Activity check-ins",
            "Forecast check-ins",
            "Recorded check-ins",
            "Weather value",
        )
    )
    assert len(estimated) == 2


def test_normalized_line_connects_recorded_anchor_and_known_estimates_without_fake_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import streamlit as st

    captured: list[list[dict[str, Any]]] = []
    original_chart = st.vega_lite_chart

    def capture_chart(data: Any, *args: Any, **kwargs: Any) -> Any:
        captured.append(data)
        return original_chart(data, *args, **kwargs)

    monkeypatch.setattr(st, "vega_lite_chart", capture_chart)
    app = AppTest.from_function(_activity_weather_timeline_script)
    result = _result()
    exported_before = forecast_export(result, 1)
    app.session_state["forecast"] = result
    app.session_state["records"] = (ActivityRecord(1, date(2022, 1, 18), 12, 999),)
    app.session_state["cohort_records"] = (
        ActivityRecord(1, date(2022, 1, 18), 12, 25),
        ActivityRecord(1, date(2022, 1, 19), 12, 10),
    )
    app.session_state["cohort_businesses"] = 50
    app.session_state["covered"] = (date(2022, 1, 18), date(2022, 1, 19))
    app.session_state["weather"] = _historical_weather()
    app.session_state["weather_metric"] = "Daily high temperature"
    app.run()

    assert not app.exception
    rows = captured[0]
    actual = [row for row in rows if row["Activity source"] == "Recorded activity"]
    estimates = [row for row in rows if row["Activity source"] == "Forecast check-ins"]
    anchors = [row for row in rows if row["Activity source"] == "Recorded anchor"]
    bands = [row for row in rows if row["Activity source"] == "Unavailable forecast day"]
    assert [row["Activity check-ins"] for row in actual] == [50.0, 20.0]
    assert [(row["Date"], row["Activity check-ins"]) for row in estimates] == [
        ("2022-01-20", 0.1),
        ("2022-01-22", 0.3),
    ]
    assert len(anchors) == 1
    assert anchors[0]["Date"] == "2022-01-19"
    assert anchors[0]["Activity check-ins"] == 20.0
    assert anchors[0]["Recorded cohort check-ins"] == 10
    assert anchors[0]["Series"] == "Forecast check-ins"
    assert anchors[0]["Is forecast"] is False
    assert "Forecast check-ins" not in anchors[0]
    assert [(row["Unavailable from"], row["Unavailable through"]) for row in bands] == [
        ("2022-01-21", "2022-01-21")
    ]
    assert forecast_export(result, 1) == exported_before
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    _assert_single_line_plot(spec)
    band, activity, weather = spec["layer"]
    assert band["mark"]["type"] == "rect"
    assert band["encoding"]["x"]["field"] == "Band start"
    assert band["encoding"]["x"]["axis"]["title"] == "Date"
    assert band["encoding"]["x2"]["field"] == "Band end"
    assert activity["encoding"]["y"]["field"] == "Activity check-ins"
    assert activity["encoding"]["y"]["axis"]["orient"] == "left"
    assert weather["encoding"]["y"]["field"] == "Weather value"
    assert weather["encoding"]["y"]["axis"]["orient"] == "right"
    assert weather["encoding"]["y"]["axis"]["offset"] == 0
    assert activity["encoding"]["strokeDash"]["scale"] == {
        "domain": ["Recorded activity", "Forecast check-ins"],
        "range": [[], [6, 4]],
    }
    assert "detail" not in activity["encoding"]
    assert "detail" not in weather["encoding"]
    captions = " ".join(caption.value for caption in app.caption)
    assert "same verified historical cohort of 50 businesses" in captions
    assert "Recorded anchor" in captions
    assert "not a forecast estimate" in captions
    assert "Gray bands" in captions


def test_normalized_scatter_uses_one_activity_axis_without_an_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import streamlit as st

    captured: list[list[dict[str, Any]]] = []
    original_chart = st.vega_lite_chart

    def capture_chart(data: Any, *args: Any, **kwargs: Any) -> Any:
        captured.append(data)
        return original_chart(data, *args, **kwargs)

    monkeypatch.setattr(st, "vega_lite_chart", capture_chart)
    app = AppTest.from_function(_activity_weather_script)
    app.session_state["forecast"] = _result()
    app.session_state["records"] = (ActivityRecord(1, date(2022, 1, 18), 12, 999),)
    app.session_state["cohort_records"] = (ActivityRecord(1, date(2022, 1, 18), 12, 25),)
    app.session_state["cohort_businesses"] = 50
    app.session_state["covered"] = (date(2022, 1, 18), date(2022, 1, 19))
    app.session_state["weather"] = _historical_weather()
    app.session_state["weather_metric"] = "Daily high temperature"
    app.run()

    assert not app.exception
    rows = captured[0]
    assert [row["Activity check-ins"] for row in rows] == [50.0, 0.1, 0.3]
    assert rows[0]["Recorded cohort check-ins"] == 25
    assert all(row["Historical cohort businesses"] == 50 for row in rows)
    assert not any(row["Activity source"] == "Recorded anchor" for row in rows)
    spec = json.loads(app.get("vega_lite_chart")[0].proto.spec)
    _assert_single_scatter_plot(spec)
    (activity,) = spec["layer"]
    assert activity["encoding"]["y"]["field"] == "Activity check-ins"
    assert activity["encoding"]["y"]["axis"]["orient"] == "left"
    assert spec["resolve"]["scale"]["y"] == "shared"
    captions = " ".join(caption.value for caption in app.caption)
    assert "same verified historical cohort of 50 businesses" in captions
    assert "heights are not directly comparable" not in captions


def test_export_has_actual_estimated_bounds_counts_and_explicit_rate_unit() -> None:
    exported = forecast_export(_result(), 1)

    assert exported == {
        "forecast_mean_rate": 0.002,
        "forecast_checkins_per_100_businesses_per_day": 0.2,
        "forecast_estimated_days": 2,
        "forecast_unavailable_days": 1,
        "forecast_start": "2022-01-20",
        "forecast_end": "2022-01-22",
        "forecast_model": "hgb_weather",
        "forecast_training_end": "2021-12-31",
        "forecast_weather_source": "observed_station_weather",
        "forecast_rate_unit": RATE_UNIT,
        "forecast_unavailable_reason": "Station weather is unavailable on one date.",
    }


def test_export_of_unsupported_area_preserves_unavailability() -> None:
    exported = forecast_export(
        ForecastRange(areas=(), uncovered_days=7, reason="Forecast model unavailable."), 2
    )

    assert exported["forecast_mean_rate"] is None
    assert exported["forecast_checkins_per_100_businesses_per_day"] is None
    assert exported["forecast_estimated_days"] == 0
    assert exported["forecast_unavailable_days"] == 7
    assert exported["forecast_start"] is None
    assert exported["forecast_unavailable_reason"] == "Forecast model unavailable."
