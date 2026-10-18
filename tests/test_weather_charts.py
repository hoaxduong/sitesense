"""Weather distributions preserve dated observations and source boundaries."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import altair as alt

from sitesense.weather_charts import (
    weather_boxplot_caption,
    weather_boxplot_display_rows,
    weather_boxplot_layer,
    weather_boxplot_rows,
)


def test_weekly_boxes_preserve_samples_zeros_sources_and_zip_identity() -> None:
    rows: list[dict[str, Any]] = [
        {"Date": "2022-01-03", "Weather value": 0.0, "ZIP area": "A", "Weather source": "History"},
        {"Date": "2022-01-05", "Weather value": 8.0, "ZIP area": "A", "Weather source": "History"},
        {"Date": "2022-01-06", "Weather value": 2.0, "ZIP area": "A", "Weather source": "Model"},
        {"Date": "2022-01-03", "Weather value": 3.0, "ZIP area": "B", "Weather source": "History"},
        {"Date": "2022-01-10", "Weather value": 4.0, "ZIP area": "A", "Weather source": "History"},
    ]
    result = weather_boxplot_rows(rows, date(2022, 1, 3), date(2022, 1, 10))

    assert [row["Weather value"] for row in result] == [0.0, 8.0, 2.0, 3.0, 4.0]
    assert result[0]["Weather period"] == result[1]["Weather period"]
    assert len({row["Weather period"] for row in result}) == 4
    assert all("Weather period" not in row for row in rows)
    assert all(datetime.fromisoformat(row["Weather period"]).tzinfo == UTC for row in result)


def test_monthly_boxes_do_not_pool_years_and_keep_partial_periods_in_range() -> None:
    start, end = date(2021, 1, 20), date(2022, 1, 22)
    rows = [
        {"Date": day, "Weather value": value}
        for day, value in (
            ("2021-01-19", 99),
            ("2021-01-20", 1),
            ("2021-01-31", 3),
            ("2021-02-01", 4),
            ("2022-01-21", 6),
            ("2022-01-22", 8),
            ("2022-01-23", 99),
        )
    ]
    result = weather_boxplot_rows(rows, start, end)

    assert [row["Weather value"] for row in result] == [1, 3, 4, 6, 8]
    assert result[0]["Weather period"] == result[1]["Weather period"]
    assert result[3]["Weather period"] == result[4]["Weather period"]
    assert result[0]["Weather period"] != result[3]["Weather period"]
    assert all(
        start <= datetime.fromisoformat(row["Weather period"]).date() <= end for row in result
    )


def test_weather_provenance_includes_outliers_and_excludes_missing_samples() -> None:
    rows: list[dict[str, Any]] = [
        {
            "Date": f"2022-01-0{day}",
            "Weather value": value,
            "Series": "High",
            "Weather source": "History",
        }
        for day, value in ((3, 0), (4, 1), (5, 2), (6, 3), (7, 100), (8, None), (9, float("inf")))
    ]
    rows.extend(
        [
            {
                "Date": "2022-01-03",
                "Weather value": -5,
                "Series": "Low",
                "Weather source": "History",
            },
            {
                "Date": "2022-01-03",
                "Weather value": 10,
                "Series": "High",
                "Weather source": "Model",
            },
            {
                "Date": "2022-01-04",
                "Weather value": 20,
                "Series": "High",
                "Weather source": "Model",
            },
        ]
    )
    result = weather_boxplot_rows(rows, date(2022, 1, 3), date(2022, 1, 9))

    high = [row for row in result if row["Series"] == "High" and row["Weather source"] == "History"]
    assert {row["Observed days"] for row in high} == {5}
    assert {row["First report"] for row in high} == {"2022-01-03"}
    assert {row["Last report"] for row in high} == {"2022-01-07"}
    assert {row["Minimum"] for row in high} == {0}
    assert {row["Maximum"] for row in high} == {100}
    assert {row["Mean"] for row in high} == {21.2}
    assert {row["Q1"] for row in high} == {1}
    assert {row["Median"] for row in high} == {2}
    assert {row["Q3"] for row in high} == {3}
    low = next(row for row in result if row["Series"] == "Low")
    assert low["Observed days"] == 1
    assert low["Minimum"] == low["Maximum"] == -5
    assert low["Q1"] == low["Median"] == low["Q3"] == -5
    model = [row for row in result if row["Weather source"] == "Model"]
    assert {row["Minimum"] for row in model} == {10}
    assert {row["Maximum"] for row in model} == {20}


def test_weather_box_tooltips_expose_full_extremes_with_metric_units() -> None:
    start, end = date(2022, 1, 3), date(2022, 1, 9)
    layer = weather_boxplot_layer(start, end, title="Temperature (°C)")
    detail = {field["field"]: field for field in layer["encoding"]["detail"]}

    whisker, minimum, maximum, box, median, mean = layer["layer"]
    assert whisker["mark"]["type"] == "rule"
    assert box["mark"]["type"] == "bar"
    assert all(child["mark"]["type"] == "tick" for child in (minimum, maximum, median))
    assert all(child["mark"]["orient"] == "horizontal" for child in (minimum, maximum, median))
    for child, field in zip(
        layer["layer"], ("Minimum", "Minimum", "Maximum", "Q1", "Median", "Mean"), strict=True
    ):
        assert child["encoding"]["y"]["field"] == field
        assert child["encoding"]["y"]["aggregate"] == "min"
        assert child["encoding"]["y"]["stack"] is None
    assert whisker["encoding"]["y2"] == {"field": "Maximum", "aggregate": "min"}
    assert box["encoding"]["y2"] == {"field": "Q3", "aggregate": "min"}
    assert box["mark"]["size"] == median["mark"]["size"] == 18
    assert mean["mark"]["type"] == "point"
    assert mean["mark"]["shape"] == "diamond"
    assert layer["resolve"]["scale"]["y"] == "shared"
    assert layer["encoding"]["x"]["scale"]["type"] == "utc"
    assert detail["Mean"]["title"] == "Mean Temperature (°C)"
    assert {"Expected readings", "Coverage %"} <= detail.keys()
    assert median["mark"]["color"] == "#e67e22"
    assert all("aggregate" not in field for field in layer["encoding"]["tooltip"])
    for child in layer["layer"]:
        tooltip = {field["field"]: field for field in child["encoding"]["tooltip"]}
        assert set(tooltip) >= {
            "Minimum",
            "Q1",
            "Median",
            "Q3",
            "Mean",
            "Maximum",
            "Observed readings",
            "Observed days",
            "Weather source",
            "Weather station",
            "Observation frequency",
            "Expected readings",
            "Coverage %",
        }
        assert not {"Date", "Observation time", "Weather value", "Weather measure"} & set(tooltip)
        assert all(
            tooltip[field]["aggregate"] == "min"
            for field in ("Minimum", "Q1", "Median", "Q3", "Mean", "Maximum")
        )
        assert child["transform"] == [
            {"filter": "isValid(datum['Minimum']) && isFinite(datum['Minimum'])"}
        ]
    caption = weather_boxplot_caption(start, end)
    assert "inner line marks the median" in caption
    assert "diamond marks the mean" in caption
    assert "Hourly observations and daily summaries remain separate" in caption
    alt.LayerChart.from_dict(layer, validate=True)


def test_precipitation_box_whiskers_axis_and_mean_use_same_blue() -> None:
    layer = weather_boxplot_layer(date(2022, 1, 3), date(2022, 1, 9), title="Rain mm/day")
    whisker, minimum, maximum, box, median, mean = layer["layer"]

    assert layer["encoding"]["y"]["axis"]["titleColor"] == "#1c639b"
    assert layer["encoding"]["y"]["axis"]["labelColor"] == "#1c639b"
    assert box["mark"]["color"] == mean["mark"]["color"] == "#1c639b"
    assert all(child["mark"]["color"] == "#1c639b" for child in (whisker, minimum, maximum, median))
    assert {item["field"] for item in box["encoding"]["tooltip"]} >= {"Minimum", "Mean", "Maximum"}


def test_temperature_combines_low_high_in_one_box_with_full_range_and_mean() -> None:
    rows: list[dict[str, Any]] = [
        {
            "Date": f"2022-01-0{day}",
            "Series": series,
            "Weather value": value,
            "Weather source": "History",
        }
        for day, series, value in (
            (3, "Low", -5),
            (3, "High", 15),
            (4, "Low", 0),
            (4, "High", 20),
            (5, "Low", None),
            (5, "High", 100),
            (6, "Low", -10),
            (6, "High", None),
            (7, "Low", float("inf")),
        )
    ]
    result = weather_boxplot_rows(
        rows, date(2022, 1, 3), date(2022, 1, 9), combine_temperature=True
    )

    assert {row["Series"] for row in result} == {"Temperature"}
    assert {row["Weather measure"] for row in result} == {"Low", "High"}
    assert len({row["Weather period"] for row in result}) == 1
    assert {row["Minimum"] for row in result} == {-10}
    assert {row["Maximum"] for row in result} == {100}
    assert {row["Mean"] for row in result} == {20}
    assert {row["Q1"] for row in result} == {-3.75}
    assert {row["Median"] for row in result} == {7.5}
    assert {row["Q3"] for row in result} == {18.75}
    assert {row["Observed readings"] for row in result} == {6}
    assert {row["Observed days"] for row in result} == {4}
    assert {row["Series"] for row in rows} == {"Low", "High"}


def test_hourly_samples_keep_report_date_station_and_frequency_boundaries() -> None:
    rows: list[dict[str, Any]] = [
        {
            "Date": "2022-01-03",
            "Observation time": f"2022-01-03T{hour}:00:00-05:00",
            "Weather value": value,
            "Weather source": "Observed station",
            "Weather station": "A",
            "Observation frequency": "Hourly",
            "Series": "Temperature",
        }
        for hour, value in (("00", 0), ("01", 10), ("23", None))
    ]
    rows.extend(
        [
            dict(rows[0], **{"Weather station": "B", "Weather value": 100}),
            dict(rows[0], **{"Observation frequency": "Daily summaries", "Weather value": -10}),
        ]
    )
    result = weather_boxplot_rows(rows, date(2022, 1, 3), date(2022, 1, 9))

    station_a = [
        row
        for row in result
        if row["Weather station"] == "A" and row["Observation frequency"] == "Hourly"
    ]
    assert len(station_a) == 3
    assert {row["Weather period"] for row in station_a} == {"2022-01-03T00:00:00+00:00"}
    assert {row["Observed readings"] for row in station_a} == {2}
    assert {row["Observed days"] for row in station_a} == {1}
    assert {row["Expected readings"] for row in station_a} == {7 * 24}
    assert {row["Coverage %"] for row in station_a} == {100 * 2 / (7 * 24)}
    assert {row["Mean"] for row in station_a} == {5}
    assert {row["Q1"] for row in station_a} == {2.5}
    assert {row["Median"] for row in station_a} == {5}
    assert {row["Q3"] for row in station_a} == {7.5}
    assert next(row for row in result if row["Weather station"] == "B")["Mean"] == 100
    daily = next(row for row in result if row["Observation frequency"] == "Daily summaries")
    assert daily["Mean"] == -10
    assert "Expected readings" not in daily
    assert [row["Observation time"] for row in station_a] == [
        row["Observation time"] for row in rows[:3]
    ]
    assert all("Weather period" not in row for row in rows)


def test_hourly_coverage_counts_missing_days_in_partial_month_without_fake_observations() -> None:
    result = weather_boxplot_rows(
        [
            {
                "Date": "2022-01-31T12:00:00-05:00",
                "Observation frequency": "Hourly",
                "Weather value": None,
            }
        ],
        date(2022, 1, 20),
        date(2022, 5, 1),
    )

    assert len(result) == 1
    row = result[0]
    assert row["Expected readings"] == 12 * 24
    assert row["Coverage %"] == row["Observed readings"] == row["Observed days"] == 0
    assert all(row[field] is None for field in ("Minimum", "Q1", "Median", "Q3", "Mean", "Maximum"))
    assert row["First report"] is row["Last report"] is None
    assert row["Weather value"] is None


def test_raw_samples_never_split_boxplot_groups() -> None:
    layer = weather_boxplot_layer(date(2022, 1, 3), date(2022, 1, 9), title="Temperature (°C)")
    detail_fields = {field["field"] for field in layer["encoding"]["detail"]}

    assert {
        "ZIP area",
        "Series",
        "Weather source",
        "Weather station",
        "Observation frequency",
    } <= detail_fields
    assert not {"Date", "Observation time", "Weather value", "Weather measure"} & detail_fields
    without_zip = weather_boxplot_layer(
        date(2022, 1, 3), date(2022, 1, 9), title="Weather °C", include_zip=False
    )
    assert all(field["field"] != "ZIP area" for field in without_zip["encoding"]["detail"])
    for child in without_zip["layer"]:
        assert all(field["field"] != "ZIP area" for field in child["encoding"]["tooltip"])


def test_display_rows_compact_hourly_history_without_downsampling_activity_or_statistics() -> None:
    start, end = date(2021, 1, 1), date(2021, 12, 31)
    timestamp = datetime(2021, 1, 1, tzinfo=UTC)
    hourly = [
        {
            "Date": (timestamp + timedelta(hours=hour)).date().isoformat(),
            "Observation time": (timestamp + timedelta(hours=hour)).isoformat(),
            "Weather value": hour % 24,
            "Series": "Temperature",
            "Weather station": "A",
            "Weather source": "Observed station",
            "Observation frequency": "Hourly",
        }
        for hour in range(365 * 24)
    ]
    raw = weather_boxplot_rows(hourly, start, end)
    first_activity = {"Date": "2021-01-01", "Activity": 0}
    last_activity = {"Date": "2021-12-31", "Activity": 10}
    result = weather_boxplot_display_rows([first_activity, *raw, last_activity])

    assert result[0] is first_activity
    assert result[-1] is last_activity
    summaries = result[1:-1]
    assert len(raw) == 365 * 24
    assert len(summaries) == 12
    assert sum(row["Observed readings"] for row in summaries) == 365 * 24
    assert all(row["Coverage %"] == 100 for row in summaries)
    for summary in summaries:
        assert [
            summary[field] for field in ("Minimum", "Q1", "Median", "Q3", "Maximum", "Mean")
        ] == [0, 5.75, 11.5, 17.25, 23, 11.5]
    assert raw[0] is not summaries[0]
    assert len({row["Observation time"] for row in raw}) == 365 * 24
    assert weather_boxplot_display_rows(result) == result


def test_display_rows_keep_sources_stations_frequencies_series_zip_and_finite_representatives() -> (
    None
):
    rows: list[dict[str, Any]] = [
        {
            "Date": "2022-01-03",
            "Weather value": value,
            "ZIP area": "A",
            "Series": "Temperature",
            "Weather source": "Observed",
            "Weather station": "A",
            "Observation frequency": "Hourly",
        }
        for value in (None, float("inf"), 0, 10)
    ]
    reference = rows[-1]
    rows.extend(
        dict(reference, **{field: value})
        for field, value in (
            ("ZIP area", "B"),
            ("Series", "Precipitation"),
            ("Weather source", "Other source"),
            ("Weather station", "B"),
            ("Observation frequency", "Daily summaries"),
            ("Date", "2022-01-10"),
        )
    )
    raw = weather_boxplot_rows(rows, date(2022, 1, 3), date(2022, 1, 10))
    result = weather_boxplot_display_rows(raw)

    assert len(result) == 7
    assert result[0]["Weather value"] == 0
    assert result[0]["Observed readings"] == 2
    assert result[0]["Minimum"] == 0
    assert result[0]["Maximum"] == 10
    assert result[0]["Mean"] == 5
    assert raw[0]["Weather value"] is None
