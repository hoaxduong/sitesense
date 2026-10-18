"""Future planning shows individual model identities, never area-derived rates."""

import csv
import io
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import business_forecast, business_future_views, business_weather_layer
from sitesense.business_forecast import BusinessBundle, BusinessGroup, BusinessProjection
from sitesense.ui import ScreenContext


def _group(identifier: str, rate: float = 1) -> BusinessGroup:
    return BusinessGroup(
        business_id=identifier,
        name="Same business name",
        city="Philadelphia",
        state="PA",
        category="Restaurants",
        source_categories=("Pizza", "Italian"),
        latitude=39.95,
        longitude=-75.15,
        training_checkins=int(rate * 1096),
        active_training_days=20,
        overall_rate=rate,
        rates=(rate,) * 84,
    )


def _model() -> BusinessBundle:
    metrics = {
        name: {"poisson_deviance": 0.3, "mae": 0.1, "businesses": 2, "observations": 730}
        for name in business_forecast.MODEL_NAMES
    }
    return BusinessBundle(
        (_group("a"), replace(_group("b", 2), latitude=None, longitude=None)),
        {
            "model_name": "overall_mean",
            "training_start": "2019-01-01",
            "training_end": "2021-12-31",
            "validation_metrics": metrics,
            "assessment_metrics": metrics,
        },
    )


def _app(source: str = "Pizza", ids: tuple[str, ...] | None = None) -> AppTest:
    return AppTest.from_string(
        f"""
from datetime import date
from sitesense.models import Catalog, DateCoverage, Filters
from sitesense.ui import ScreenContext
from sitesense.business_future_views import render
filters = Filters(
    'Philadelphia', 'PA', 'Restaurants', date(2020,1,1), date(2020,1,31),
    source_category={source!r},
)
catalog = Catalog((), (), (), DateCoverage(date(2010,1,1), date(2022,1,19)))
render(ScreenContext(filters, catalog, (), (), 'import'), {ids!r})
""",
        default_timeout=10,
    )


@pytest.fixture
def planning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "businesses.json").write_text("[]")
    monkeypatch.setattr(business_forecast, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(business_forecast, "code_sha256", lambda: "code")
    monkeypatch.setattr(business_forecast, "lock_sha256", lambda: "lock")
    monkeypatch.setattr(business_future_views, "_bundle", lambda *_: _model())
    downloads: list[str] = []

    def capture(_label: str, data: bytes, **_kwargs: object) -> bool:
        downloads.append(data.decode())
        return False

    monkeypatch.setattr(st, "download_button", capture)
    return downloads


def test_business_rows_keep_duplicate_names_distinct_and_use_own_rates() -> None:
    a, b = _group("a", 1), _group("b", 3)
    rows = business_future_views.projection_rows(
        (BusinessProjection(a, 30, 1, 30), BusinessProjection(b, 30, 3, 90))
    )
    assert [row["Business ID"] for row in rows] == ["b", "a"]
    assert [row["Projected check-ins"] for row in rows] == [90, 30]
    assert all("Projected check-ins / day" not in row for row in rows)
    assert all("ZIP" not in row for row in rows)


def test_future_month_exports_individual_scope_and_keeps_unmapped_business(
    planning: list[str],
) -> None:
    app = _app().run()
    assert not app.exception
    assert app.selectbox(key="business_future_month").value > date.today()
    rows = list(csv.DictReader(io.StringIO(planning[-1])))
    assert {row["Business ID"] for row in rows} == {"a", "b"}
    assert rows[0]["Business ID"] == "b"
    assert rows[0]["Selected_source_category"] == "Pizza"
    assert all("ZIP" not in column for column in rows[0])
    assert any("1 of 2" in caption.value for caption in app.caption)
    assert any("Check-in data ends in 2022" in warning.value for warning in app.warning)
    assert all(row["Estimate_kind"] == "individual_business_calendar_projection" for row in rows)


def test_future_comparison_uses_exact_ids_and_reports_unmodeled_choices(
    planning: list[str],
) -> None:
    app = _app(ids=("a", "unsupported")).run()
    assert not app.exception
    rows = list(csv.DictReader(io.StringIO(planning[-1])))
    assert [row["Business ID"] for row in rows] == ["a"]
    assert any("1 selected businesses" in info.value for info in app.info)
    app.segmented_control(key="business_future_period").set_value("Day").run()
    assert not app.exception
    row = list(csv.DictReader(io.StringIO(planning[-1])))[0]
    assert row["Planning_start"] == row["Planning_end"]
    assert float(row["Projected check-ins"]) == pytest.approx(1.0)


def test_weather_uses_exact_planning_dates_without_changing_business_projections(
    planning: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[tuple[str, ...], date, date]] = []

    def no_observations(
        context: ScreenContext, ids: tuple[str, ...], start: date, end: date, key: str
    ) -> business_weather_layer.WeatherLayer:
        assert context.filters.start_date == date(2020, 1, 1)
        assert key == "business_future_weather"
        requests.append((ids, start, end))
        return business_weather_layer.WeatherLayer(
            "Mean daily high (°C)", start, end, {}, None, None
        )

    monkeypatch.setattr(business_weather_layer, "prepare", no_observations)
    app = _app(ids=("a",)).run()
    assert not app.exception
    export = list(csv.DictReader(io.StringIO(planning[-1])))[0]
    assert requests[-1] == (
        ("a",),
        date.fromisoformat(export["Planning_start"]),
        date.fromisoformat(export["Planning_end"]),
    )
    assert requests[-1][1] > date.today()
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    assert len(payload["layers"]) == 1
    assert payload["layers"][0]["getFillColor"] == [113, 85, 138, 175]
    point = payload["layers"][0]["data"][0]
    assert point["Projected check-ins"] == float(export["Projected check-ins"])
    assert point["tooltip_label"] == "Projected check-ins"
    assert point["tooltip_count"] == f"{point['Projected check-ins']:,.2f}"
    app.segmented_control(key="business_future_period").set_value("Day").run()
    assert not app.exception
    assert requests[-1][1] == requests[-1][2] > date.today()


def test_future_top_map_limit_orders_period_totals_with_stable_ids_without_reducing_scope(
    planning: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    groups = tuple(
        _group(f"rank-{number:02}", number // 2 + 1) for number in reversed(range(25))
    ) + (replace(_group("highest-unmapped", 100), latitude=None, longitude=None),)
    bundle = replace(_model(), groups=groups)
    monkeypatch.setattr(business_future_views, "_bundle", lambda *_: bundle)
    requests: list[tuple[str, ...]] = []

    def weather(
        context: ScreenContext, ids: tuple[str, ...], start: date, end: date, key: str
    ) -> None:
        assert key == "business_future_weather"
        assert start > date.today()
        requests.append(ids)

    monkeypatch.setattr(business_weather_layer, "prepare", weather)
    app = _app().run()
    assert not app.exception
    expected_ids = [
        group.business_id
        for group in sorted(groups[:-1], key=lambda group: (-group.overall_rate, group.business_id))
    ]
    assert app.selectbox(key="business_future_map_limit").label == "Top businesses on map"
    assert app.selectbox(key="business_future_map_limit").value == 20
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    points = payload["layers"][-1]["data"]
    assert [point["business_id"] for point in points] == expected_ids[:20]
    assert [point["Projected check-ins"] for point in points] == sorted(
        (point["Projected check-ins"] for point in points), reverse=True
    )
    table = app.dataframe[0].value.copy()
    assert len(table) == 26
    exported = planning[-1]
    assert len(list(csv.DictReader(io.StringIO(exported)))) == 26
    all_ids = {group.business_id for group in groups}
    assert set(requests[-1]) == all_ids

    app.selectbox(key="business_future_map_limit").select(10).run()
    assert not app.exception
    payload = json.loads(app.get("deck_gl_json_chart")[0].proto.json)
    assert [point["business_id"] for point in payload["layers"][-1]["data"]] == expected_ids[:10]
    assert app.dataframe[0].value.equals(table)
    assert planning[-1] == exported
    assert all(set(ids) == all_ids for ids in requests)


def test_exact_source_has_no_other_model_or_area_fallback(planning: list[str]) -> None:
    app = _app("Pizza Delivery").run()
    assert not app.exception
    assert not planning
    assert any("No supported individual-business model" in info.value for info in app.info)


def test_overlapping_tags_summarize_same_individual_rates_once_per_tag() -> None:
    rows = business_future_views.source_rows((BusinessProjection(_group("a"), 30, 1, 30),))
    assert len(rows) == 2
    assert all(row["Modeled businesses"] == 1 for row in rows)
    assert all(row["Projected check-ins / business"] == 30 for row in rows)


def test_missing_individual_artifact_is_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(business_forecast, "MODEL_DIR", tmp_path)
    app = _app().run()
    assert not app.exception
    assert any("individual-business model is unavailable" in info.value for info in app.info)
