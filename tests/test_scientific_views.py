"""Weather timelines retain daily activity independently of weather coverage."""

from typing import Any
from unittest.mock import patch

import pytest

from sitesense import scientific_views


@pytest.mark.parametrize(
    "metric, field",
    [
        ("Precipitation", "Precipitation mm"),
        ("Daily high temperature", "Daily high °C"),
    ],
)
def test_timelines_keep_zip_identity_activity_zeros_and_missing_weather(
    metric: str, field: str
) -> None:
    rows: list[dict[str, Any]] = [
        {
            "Date": "2021-01-01",
            "ZIP area": "First ZIP",
            "Activity": 50.0,
            field: 0.0,
            "Activity source": "Verified model cohort",
            "Recorded check-ins": 1,
            "Cohort businesses": 2,
        },
        {
            "Date": "2021-01-02",
            "ZIP area": "First ZIP",
            "Activity": 0.0,
            field: None,
            "Activity source": "Verified model cohort",
            "Recorded check-ins": 0,
            "Cohort businesses": 2,
        },
        {
            "Date": "2021-01-01",
            "ZIP area": "Second ZIP",
            "Activity": 20.0,
            field: 10.0,
            "Activity source": "Verified model cohort",
            "Recorded check-ins": 1,
            "Cohort businesses": 5,
        },
    ]
    with (
        patch.object(scientific_views, "_chart") as chart,
        patch.object(scientific_views.st, "caption"),
    ):
        result = scientific_views._timeline(
            rows, metric, scientific_views.RATE_UNIT, ["First ZIP", "Second ZIP"], "science"
        )

    activity = [row for row in result if row["Kind"] == "Activity"]
    weather = [row for row in result if row["Kind"] == "Weather"]
    assert [row["Activity"] for row in activity] == [50.0, 0.0, 20.0]
    assert [row["Weather value"] for row in weather] == [0.0, 10.0]
    assert all(row["Weather source"] == "Recorded station weather" for row in weather)
    assert all("Kind" not in row for row in rows)
    assert chart.call_count == 2
    for call, label in zip(chart.call_args_list, ("First ZIP", "Second ZIP"), strict=True):
        plotted, spec = call.args[:2]
        assert {row["ZIP area"] for row in plotted} == {label}
        assert spec["resolve"]["scale"]["y"] == "independent"
        boxes, line = spec["layer"]
        assert boxes["layer"][0]["mark"]["type"] == "rule"
        assert boxes["layer"][0]["encoding"]["y"]["field"] == "Minimum"
        assert boxes["layer"][0]["encoding"]["y2"]["field"] == "Maximum"
        assert boxes["layer"][3]["encoding"]["y"]["field"] == "Q1"
        assert boxes["layer"][3]["encoding"]["y2"]["field"] == "Q3"
        assert boxes["layer"][4]["encoding"]["y"]["field"] == "Median"
        assert boxes["layer"][5]["encoding"]["y"]["field"] == "Mean"
        assert boxes["layer"][5]["encoding"]["y"]["aggregate"] == "min"
        assert boxes["resolve"]["scale"]["y"] == "shared"
        assert boxes["encoding"]["x"]["type"] == "temporal"
        assert boxes["encoding"]["y"]["field"] == "Weather value"
        assert boxes["encoding"]["y"]["title"] == (
            "Precipitation (mm/day)" if metric == "Precipitation" else field
        )
        assert boxes["encoding"]["y"]["axis"]["orient"] == "right"
        assert line["mark"]["type"] == "line"
        assert line["encoding"]["x"]["field"] == "Date"
        assert line["encoding"]["x"]["type"] == "temporal"
        assert line["encoding"]["x"]["scale"] == boxes["encoding"]["x"]["scale"]
        assert line["encoding"]["y"]["title"] == scientific_views.RATE_UNIT
        assert line["encoding"]["y"]["axis"]["orient"] == "left"


def test_timeline_with_no_weather_still_renders_recorded_activity() -> None:
    rows: list[dict[str, Any]] = [
        {"Date": "2021-01-01", "ZIP area": "ZIP", "Activity": 0.0, "Precipitation mm": None}
    ]
    with (
        patch.object(scientific_views, "_chart") as chart,
        patch.object(scientific_views.st, "caption") as caption,
    ):
        result = scientific_views._timeline(rows, "Precipitation", "Recorded / day", ["ZIP"], "s")

    assert len(result) == 1
    assert result[0]["Kind"] == "Activity"
    assert result[0]["Activity"] == 0
    chart.assert_called_once()
    assert any("No accepted weather" in call.args[0] for call in caption.call_args_list)


def test_temperature_combines_daily_low_and_high_readings_on_one_weather_axis() -> None:
    rows: list[dict[str, Any]] = [
        {
            "Date": f"2021-01-0{day}",
            "ZIP area": "ZIP",
            "Activity": activity,
            "Recorded check-ins": activity,
            "Cohort businesses": 100,
            "Daily low °C": low,
            "Daily high °C": high,
        }
        for day, activity, low, high in ((4, 0, -5, None), (5, 50, None, 10), (6, 100, 2, 12))
    ]
    with (
        patch.object(scientific_views, "_chart") as chart,
        patch.object(scientific_views.st, "caption"),
    ):
        result = scientific_views._timeline(
            rows, "Temperature", scientific_views.RATE_UNIT, ["ZIP"], "temperature"
        )

    activity = [row for row in result if row["Kind"] == "Activity"]
    assert len(activity) == len(rows)
    assert sum(row["Recorded check-ins"] for row in activity) == 150
    weather = [row for row in result if row["Kind"] == "Weather"]
    assert len(weather) == 1
    assert tuple(
        weather[0][field] for field in ("Minimum", "Q1", "Median", "Q3", "Maximum", "Mean")
    ) == (
        -5,
        0.25,
        6,
        10.5,
        12,
        4.75,
    )
    assert {row["Series"] for row in weather} == {"Temperature"}
    assert {row["Observed days"] for row in weather} == {3}
    assert {row["Observed readings"] for row in weather} == {4}
    assert {row["Maximum"] for row in weather} == {12}
    assert {row["Minimum"] for row in weather} == {-5}
    assert {row["Mean"] for row in weather} == {4.75}
    _, spec = chart.call_args.args[:2]
    boxes, line = spec["layer"]
    assert boxes["layer"][0]["mark"]["type"] == "rule"
    assert boxes["layer"][0]["encoding"]["y"]["field"] == "Minimum"
    assert boxes["layer"][0]["encoding"]["y2"]["field"] == "Maximum"
    assert boxes["layer"][3]["encoding"]["y"]["field"] == "Q1"
    assert boxes["layer"][3]["encoding"]["y2"]["field"] == "Q3"
    assert boxes["layer"][4]["encoding"]["y"]["field"] == "Median"
    assert boxes["encoding"]["y"]["title"] == "Temperature (°C)"
    assert boxes["encoding"]["y"]["axis"]["orient"] == "right"
    assert boxes["encoding"]["color"]["scale"]["domain"] == ["Temperature"]
    assert boxes["encoding"]["color"]["scale"]["range"] == ["#e67e22"]
    assert {field["field"] for field in boxes["encoding"]["detail"]} >= {"Series"}
    assert {field["field"] for field in boxes["encoding"]["tooltip"]} >= {"Series"}
    assert line["transform"] == [{"filter": "datum.Kind === 'Activity'"}]
    assert line["encoding"]["y"]["axis"]["orient"] == "left"
    assert spec["resolve"]["scale"]["y"] == "independent"
    assert spec["resolve"]["scale"]["color"] == "independent"


def test_scientific_hourly_temperature_samples_are_shared_once_per_zip_and_keep_activity() -> None:
    activity = [
        {
            "Date": "2021-01-01",
            "ZIP area": label,
            "Activity": count,
            "Daily low °C": -99,
            "Daily high °C": 99,
        }
        for label, count in (("First ZIP", 0), ("Second ZIP", 4))
    ]
    hourly = [
        {
            "Date": "2021-01-01",
            "Observation time": f"2021-01-01T{hour:02}:54:00-05:00",
            "Weather value": value,
            "Series": "Temperature",
            "Observation frequency": "Hourly",
            "Weather metric": "Temperature (°C)",
            "Weather source": "NOAA LCDv2",
            "Weather station": "Philadelphia airport (USW00013739)",
        }
        for hour, value in ((0, -3), (1, 0), (2, 9))
    ]
    with (
        patch.object(scientific_views, "_chart") as chart,
        patch.object(scientific_views.st, "caption"),
    ):
        result = scientific_views._timeline(
            activity,
            "Temperature",
            "Recorded / day",
            ["First ZIP", "Second ZIP"],
            "hourly",
            hourly_weather_rows=hourly,
        )
    assert [row["Activity"] for row in result if row["Kind"] == "Activity"] == [0, 4]
    weather = [row for row in result if row["Kind"] == "Weather"]
    assert len(weather) == 2
    for label in ("First ZIP", "Second ZIP"):
        subset = [row for row in weather if row["ZIP area"] == label]
        assert len(subset) == 1
        assert tuple(
            subset[0][field] for field in ("Minimum", "Q1", "Median", "Q3", "Maximum", "Mean")
        ) == (
            -3,
            -1.5,
            0,
            4.5,
            9,
            2,
        )
        assert {row["Observed readings"] for row in subset} == {3}
        assert {row["Mean"] for row in subset} == {2}
        assert {row["Weather station"] for row in subset} == {"Philadelphia airport (USW00013739)"}
    assert chart.call_count == 2
    for call in chart.call_args_list:
        plotted, spec = call.args[:2]
        assert len([row for row in plotted if row["Kind"] == "Activity"]) == 1
        assert spec["layer"][0]["layer"][0]["mark"]["type"] == "rule"


def test_precipitation_uses_daily_totals_even_when_hourly_samples_are_available() -> None:
    rows = [
        {"Date": f"2021-01-0{day}", "ZIP area": "ZIP", "Activity": count, "Precipitation mm": rain}
        for day, count, rain in ((1, 0, 0.0), (2, 4, 4.0), (3, 2, None))
    ]
    with (
        patch.object(scientific_views, "_chart") as chart,
        patch.object(scientific_views.st, "caption"),
    ):
        result = scientific_views._timeline(
            rows,
            "Precipitation",
            "Recorded / day",
            ["ZIP"],
            "rain",
            hourly_weather_rows=[{"Weather value": 999}],
        )
    weather = [row for row in result if row["Kind"] == "Weather"]
    assert len(weather) == 1
    assert (
        weather[0]["Minimum"],
        weather[0]["Maximum"],
        weather[0]["Mean"],
        weather[0]["Observed readings"],
    ) == (0, 4, 2, 2)
    assert weather[0]["Observation frequency"] == "Daily summaries"
    assert [row["Activity"] for row in result if row["Kind"] == "Activity"] == [0, 4, 2]
    assert chart.call_args.args[1]["layer"][0]["encoding"]["y"]["title"] == "Precipitation (mm/day)"
