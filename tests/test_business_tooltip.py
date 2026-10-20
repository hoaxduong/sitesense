"""Hover fields preserve plain source text for Streamlit's escaped static template."""

import re
from datetime import date

import pytest

from sitesense.business_tooltip import TOOLTIP, fields, period_label


def test_source_text_is_not_preescaped_before_streamlit_substitutes_it() -> None:
    values = {
        "name": '<img src=x onerror="alert(1)"> & Cafe',
        "count": '<script>alert("count")</script>',
        "period": '<svg onload="alert(2)">activity</svg>',
        "weather_text": '<iframe srcdoc="unsafe">12 °C</iframe>',
        "weather_station": '<a href="javascript:alert(3)">Airport</a>',
        "weather_coverage": '<input autofocus onfocus="alert(4)">5 / 10 days',
        "weather_period": "<style>body{display:none}</style>",
    }
    result = fields(
        values["name"],
        values["count"],
        period=values["period"],
        weather={
            key: values[key] for key in ("weather_text", "weather_station", "weather_coverage")
        },
        weather_period=values["weather_period"],
    )
    assert result["tooltip_name"] == values["name"]
    assert result["tooltip_count"] == values["count"]
    assert result["tooltip_period"] == values["period"]
    assert result["tooltip_weather"] == values["weather_text"]
    assert result["tooltip_station"] == values["weather_station"] + " · ACIS"
    assert result["tooltip_coverage"] == values["weather_coverage"]
    assert result["tooltip_weather_dates"] == "Weather: " + values["weather_period"]
    assert all(value not in TOOLTIP["html"] for value in values.values())
    assert "<div" in TOOLTIP["html"]
    assert "{tooltip_html}" not in TOOLTIP["html"]


def test_names_with_braces_are_preserved_in_plain_text_fields() -> None:
    name = "Cafe {weather_text} & {business_id} {properties.secret}"
    assert fields(name, "2,133")["tooltip_name"] == name
    placeholders = re.findall(r"\{([^{}]+)\}", TOOLTIP["html"])
    assert all(key.startswith("tooltip_") and "." not in key for key in placeholders)
    assert "text" not in TOOLTIP


def test_zero_activity_remains_distinct_from_unavailable_activity() -> None:
    zero = fields("Cafe", "0")
    unavailable = fields("Cafe", "Unavailable")
    assert zero["tooltip_count"] == "0"
    assert unavailable["tooltip_count"] == "Unavailable"


def test_mixed_window_keeps_actual_and_projected_totals_and_dates_distinct() -> None:
    result = fields(
        "Cafe",
        "0",
        period="01 Jan 2022 – 19 Jan 2022",
        projected_count="2.50",
        projected_period="20 Jan 2022 – 31 Jan 2022",
        rating="4.5 / 5",
    )
    assert result["tooltip_label"] == "Recorded check-ins"
    assert result["tooltip_count"] == "0"
    assert result["tooltip_projected_count"] == "2.50"
    assert result["tooltip_projected_period"] == "20 Jan 2022 – 31 Jan 2022"
    assert result["tooltip_projected_display"] == "block"
    assert result["tooltip_rating_display"] == "block"
    assert result["tooltip_rating"] == "4.5 / 5"


@pytest.mark.parametrize("weather", [None, {}, {"weather_value": None}])
@pytest.mark.parametrize("projected", [False, True])
def test_every_template_field_exists_even_without_weather(
    weather: dict[str, object] | None, projected: bool
) -> None:
    result = fields("Cafe", "0", weather=weather, projected=projected)
    placeholders = set(re.findall(r"\{([^{}]+)\}", TOOLTIP["html"]))
    assert placeholders == result.keys()
    assert all(isinstance(result[key], str) for key in placeholders)
    assert result["tooltip_weather_display"] == ("block" if weather else "none")
    assert result["tooltip_weather"] == "Weather unavailable"
    assert result["tooltip_coverage"] == "No accepted observations"
    assert result["tooltip_station"] == "No mapped ACIS station · ACIS"
    assert result["tooltip_period"] == result["tooltip_weather_dates"] == ""
    assert "undefined" not in result.values()


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2024, 2, 29), date(2024, 2, 29), "29 Feb 2024"),
        (date(2024, 2, 1), date(2024, 2, 29), "01 Feb 2024 – 29 Feb 2024"),
        (date(2023, 12, 31), date(2024, 1, 1), "31 Dec 2023 – 01 Jan 2024"),
    ],
)
def test_period_labels_are_unambiguous(start: date, end: date, expected: str) -> None:
    assert period_label(start, end) == expected


def test_weather_keeps_its_dates_when_recorded_activity_is_clipped() -> None:
    activity = "01 Jan 2020 – 10 Jan 2020"
    weather = "01 Dec 2019 – 01 Feb 2020"
    result = fields(
        "Cafe",
        "20",
        period=activity,
        weather={"weather_text": "Mean daily high: 12.0 °C"},
        weather_period=weather,
    )
    assert result["tooltip_period"] == activity
    assert result["tooltip_weather_dates"] == f"Weather: {weather}"


def test_shared_activity_and_weather_dates_appear_once() -> None:
    period = "01 Jan 2020 – 10 Jan 2020"
    result = fields(
        "Cafe", "20", period=period, weather={"weather_text": "12.0 °C"}, weather_period=period
    )
    assert result["tooltip_period"] == period
    assert result["tooltip_weather_dates"] == ""


@pytest.mark.parametrize("projected", [False, True])
def test_activity_labels_show_period_totals_and_distinguish_projections(projected: bool) -> None:
    result = fields("Cafe", "123", projected=projected)
    expected = "Projected check-ins" if projected else "Recorded check-ins"
    other = "Recorded check-ins" if projected else "Projected check-ins"
    text = " ".join(result.values())
    assert result["tooltip_label"] == expected
    assert other not in text
    assert result["tooltip_count"] == "123"
    assert "/ day" not in text and "per day" not in text.lower()
