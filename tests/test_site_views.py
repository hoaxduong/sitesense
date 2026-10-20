"""Site recommendations page: pure helpers and AppTest flows (TC-160..TC-200).

The shell is exercised through ``AppTest`` without a database: repository reads are patched and
``data.load_scope_data`` is replaced by a spy returning a small synthetic ``ScopeData``.
"""

import csv
import io
import json
import logging
import re
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import psycopg
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import repository, zip_boundaries
from sitesense.app import _activity, _catalog, _categories
from sitesense.models import Business, Catalog, DateCoverage, Filters
from sitesense.site_recommendation import data, explain, scoring, views
from sitesense.site_recommendation.models import (
    METRIC_KEYS,
    PROXY_NOTICE,
    AreaInputs,
    AreaMetrics,
    AreaScore,
    BusinessPoint,
    CompetitorPoint,
    Confidence,
    DemandSource,
    EstimatorReport,
    MetricKey,
    PreparedScope,
    RecommendationResult,
    ScopeData,
    ScopeKey,
    ScoreWeights,
    UnscoredArea,
    WeatherDay,
)

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"
PAGE = "src/sitesense/app_pages/site_recommendations.py"
COVERAGE = DateCoverage(date(2010, 1, 16), date(2022, 1, 19))
KEY = ScopeKey(
    "Tampa", "FL", "Restaurants", "Pizza", date(2020, 1, 1), date(2021, 12, 31), 1.0, "rev1"
)
ENV_OK = {
    "SITESENSE_AI_BASE_URL": "https://api.example.test/v1",
    "SITESENSE_AI_MODEL": "m-test",
    "SITESENSE_AI_API_KEY": "sk-test-123",
}
LLM_OK = json.dumps(
    {"choices": [{"message": {"content": "ZIP 33606 ranks first on demand."}}]}
).encode()
FORBIDDEN = re.compile(r"(?i)\b(customers? (visited|count)|revenue of|foot traffic of|sales)\b")
LATITUDE, LONGITUDE = 27.9506, -82.4572
CSV_HEADER = (
    "rank,rank_low,rank_high,tied_with_above,postal_code,display_name,score,"
    "score_low,score_high,confidence,demand_source,niche_business_count,"
    "demand_rate_per_1000_business_days,competition,quality_gap,momentum,stability,"
    "weather_resilience,rain_effect,heat_effect,key_strength,key_risk,unavailable_metrics,notes"
)


# ---- builders -------------------------------------------------------------------------------


def make_metrics(
    area_id: int,
    postal_code: str,
    *,
    demand: float = 0.01,
    competition: int | None = None,
    quality_gap: float | None = None,
    weather: float | None = None,
    confidence: Confidence = "medium",
    source: DemandSource = "observed",
) -> AreaMetrics:
    values: dict[MetricKey, float | None] = {
        "demand_rate": demand,
        "competition": competition,
        "quality_gap": quality_gap,
        "momentum": None,
        "stability": None,
        "weather_resilience": weather,
    }
    return AreaMetrics(
        area_id=area_id,
        postal_code=postal_code,
        display_name=postal_code,
        niche_business_count=4,
        niche_open_count=4,
        niche_checkins=100,
        covered_days=365,
        covered_month_count=24,
        raw_demand_rate=demand,
        model_rate=demand,
        demand_rate=demand,
        demand_source=source,
        model_fallback=True,
        competition=competition,
        quality_gap=quality_gap,
        mean_competitor_stars=None,
        momentum=None,
        stability=None,
        weather_resilience=weather,
        rain_effect=None,
        heat_effect=None,
        weather_station_id=None,
        weather_observed_days=0,
        wet_days=0,
        dry_days=0,
        hot_days=0,
        confidence=confidence,
        unavailable=tuple(k for k in METRIC_KEYS if values[k] is None),
    )


def fx_s4_scores() -> dict[str, AreaScore]:
    metrics = (
        make_metrics(1, "33601", demand=0.010, competition=5, quality_gap=1.0, weather=0.9),
        make_metrics(2, "33602", demand=0.020, competition=2, quality_gap=2.0, weather=0.8),
        make_metrics(3, "33603", demand=0.020, competition=2, quality_gap=None, weather=0.7),
        make_metrics(4, "33604", demand=0.030, competition=0, quality_gap=3.0, weather=None),
    )
    return {s.postal_code: s for s in scoring.score_areas(metrics, ScoreWeights())}


def weather_block(wet_checkins: int) -> tuple[WeatherDay, ...]:
    days: list[WeatherDay] = []
    for month in (6, 9, 11):
        for index in range(30):
            wet = index < 10
            hot = index < 3 if wet else (index - 10) < 6
            days.append(
                WeatherDay(
                    date(2020, month, index + 1),
                    wet_checkins if wet else 10,
                    35.0 if hot else 25.0,
                    5.0 if wet else 0.0,
                )
            )
    return tuple(days)


def make_area(index: int, businesses: int) -> AreaInputs:
    rng = np.random.default_rng(index)
    members = tuple(
        BusinessPoint(int(row.sum()), tuple(int(v) for v in row))
        for row in (rng.integers(0, 8 + index, size=24) for _ in range(businesses))
    )
    rated = tuple(CompetitorPoint(3.0 + (i % 3) * 0.7, 5 * i) for i in range(index % 5))
    weather = weather_block(3 + index) if index % 2 == 0 else ()
    return AreaInputs(
        area_id=index,
        postal_code=f"{33600 + index}",
        display_name=f"LEAK-Pizza-Palace-{index}",
        latitude=LATITUDE,
        longitude=LONGITUDE,
        covered_days=730,
        covered_month_count=24,
        members=members,
        niche_open_count=businesses,
        competitors=rated if index != 7 else None,
        parent_business_count=5 + businesses,
        parent_checkins=200 + 20 * index,
        parent_mean_stars=4.0,
        parent_mean_review_count=12.0,
        weather_station_id="USW1" if weather else None,
        weather_days=weather,
    )


def make_scope(count: int = 12, *, unscored: bool = True) -> ScopeData:
    counts = [0, 1, 2, *range(3, 3 + max(0, count - 3))][:count]
    areas = tuple(make_area(index, n) for index, n in enumerate(counts, start=1))
    return ScopeData(
        key=KEY,
        requested_start=KEY.start_date,
        requested_end=KEY.end_date,
        covered_start=KEY.start_date,
        covered_end=KEY.start_date + timedelta(days=729),
        covered_days=730,
        covered_months=tuple(date(2020 + m // 12, m % 12 + 1, 1) for m in range(24)),
        radius_km=1.0,
        metro_mean_rate=0.01,
        areas=areas,
        unscored=(UnscoredArea(20, "33620", "LEAK-Unscored", "no_parent_businesses"),)
        if unscored
        else (),
    )


def square(code: str, offset: float) -> dict[str, Any]:
    ring = [[-82.5 + offset, 27.9], [-82.4 + offset, 27.9], [-82.4 + offset, 28.0]]
    ring += [[-82.5 + offset, 28.0], [-82.5 + offset, 27.9]]
    return {
        "type": "Feature",
        "properties": {"postal_code": code},
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


# ---- AppTest harness ------------------------------------------------------------------------


@dataclass
class Source:
    scope: ScopeData = field(default_factory=make_scope)
    error: Exception | None = None
    calls: list[Filters] = field(default_factory=list)


class Site:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.source = Source()
        self.transport_calls: list[bytes] = []
        self.transport_result: Callable[[bytes], tuple[int, bytes]] = lambda body: (200, LLM_OK)

    def install(self) -> None:
        patch = self.monkeypatch
        patch.setattr(repository, "get_import_revision", lambda: "rev-test")
        patch.setattr(
            repository, "list_categories", lambda city, state: ("Restaurants", "Day Spas")
        )

        def catalog(city: str, state: str, category: str) -> Catalog:
            businesses = tuple(
                Business(
                    f"{city}:{index}",
                    None,
                    f"Business {index}",
                    city,
                    state,
                    "33606",
                    LATITUDE,
                    LONGITUDE,
                    (category, "Pizza" if index % 2 else "Italian"),
                    4.0,
                    10,
                    True,
                    None,
                )
                for index in range(6)
            )
            return Catalog((), businesses, (), COVERAGE)

        patch.setattr(repository, "load_catalog", catalog)

        def load(
            filters: Filters, coverage: DateCoverage, revision: str, *args: object
        ) -> ScopeData:
            self.source.calls.append(filters)
            if self.source.error is not None:
                raise self.source.error
            return self.source.scope

        patch.setattr(data, "load_scope_data", load)

        def transport(url: str, headers: Any, body: bytes, timeout: float) -> tuple[int, bytes]:
            self.transport_calls.append(body)
            return self.transport_result(body)

        patch.setattr(explain, "urllib_transport", transport)

    def open(self) -> AppTest:
        app = AppTest.from_file(str(APP_FILE), default_timeout=30).run()
        self.install()
        app = app.switch_page(PAGE).run()
        assert not app.exception, [e.message for e in app.exception]
        return app


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for name in ENV_OK:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("SITESENSE_AI_TIMEOUT", raising=False)
    for loader in (_categories, _catalog, _activity):
        loader.clear()
    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> Site:
    return Site(monkeypatch)


def by_label(elements: Sequence[Any], label: str) -> Any:
    matches = [element for element in elements if element.label == label]
    assert len(matches) == 1, (label, [element.label for element in elements])
    return matches[0]


def table(app: AppTest, first_column: str = "Rank") -> pd.DataFrame:
    frames = [f.value for f in app.dataframe if first_column in f.value.columns]
    assert frames, f"no table with column {first_column}"
    return frames[0]


def page_text(app: AppTest) -> str:
    parts = [
        str(item.value)
        for elements in (
            app.title,
            app.header,
            app.subheader,
            app.caption,
            app.markdown,
            app.info,
            app.warning,
            app.error,
            app.success,
            app.text,
            app.code,
        )
        for item in elements
    ]
    parts += [frame.value.to_csv() for frame in app.dataframe]
    parts += [
        str(item.label)
        for elements in (app.button, app.selectbox, app.multiselect, app.slider, app.checkbox)
        for item in elements
    ]
    return "\n".join(parts)


def expander_text(app: AppTest, label: str) -> str:
    expander = by_label(app.expander, label)
    return "\n".join(str(item.value) for item in expander.markdown)


def show_all_rows(app: AppTest) -> AppTest:
    by_label(app.slider, "Show top areas").set_value(12)
    return app.run()


def messages(elements: Sequence[Any]) -> list[str]:
    return [str(element.value) for element in elements]


# ---- TC-160 .. TC-161 -----------------------------------------------------------------------


def test_tc160_no_database_shows_setup_and_no_page_body() -> None:
    app = AppTest.from_file(str(APP_FILE), default_timeout=30).run()
    app = app.switch_page(PAGE).run()
    assert not app.exception
    assert app.header[0].value == "Site recommendations"
    assert app.info[0].value == "Database not configured"
    assert not app.dataframe
    assert not app.metric


def test_tc161_layout_with_synthetic_scope(site: Site) -> None:
    app = show_all_rows(site.open())
    assert any("activity proxy" in text for text in messages(app.info))
    assert any(
        text.startswith("Data coverage: 2020-01-01 → 2021-12-") for text in messages(app.caption)
    )
    labels = {expander.label for expander in app.expander}
    assert {"Scoring preferences", "Method and limits"} <= labels
    frame = table(app)
    assert len(frame) == 12
    assert list(frame.columns) == [
        "Rank",
        "ZIP",
        "Score",
        "Score range",
        "Confidence",
        "Demand source",
        "Key strength",
        "Key risk",
    ]
    assert any(text.startswith("Not scored: 33620") for text in messages(app.caption))
    assert "33620" not in set(frame["ZIP"])


def test_tc191_widget_labels_follow_the_design(site: Site) -> None:
    app = site.open()
    assert {s.label for s in app.slider} == {
        "Demand weight",
        "Competition weight",
        "Quality gap weight",
        "Momentum weight",
        "Stability weight",
        "Weather resilience weight",
        "Show top areas",
    }
    assert {s.label for s in app.selectbox} >= {
        "Weight preset",
        "Minimum confidence",
        "Area (ZIP)",
    }
    assert [m.label for m in app.multiselect] == ["Compare areas (2–5)"]
    assert [c.label for c in app.checkbox] == ["Use AI provider for this summary"]
    assert [r.label for r in app.radio] == ["Answer language"]
    assert [t.label for t in app.text_input] == ["Question (optional)"]
    assert {b.label for b in app.button} >= {
        "Explain top picks",
        "Compare selected areas",
        "Ask a question",
    }
    headings = {s.value for s in app.subheader}
    assert {
        "Ranked areas",
        "Score by ZIP",
        "Area detail",
        "Compare areas",
        "AI advisor",
    } <= headings
    assert {"Data that will be sent to the AI provider"} <= {e.label for e in app.expander}


# ---- TC-162 .. TC-165, TC-199 ----------------------------------------------------------------


def test_tc162_preset_overwrites_sliders_without_reloading_data(site: Site) -> None:
    app = show_all_rows(site.open())
    before = list(table(app)["ZIP"])
    by_label(app.selectbox, "Weight preset").set_value("Low-competition")
    app.run()
    assert not app.exception
    expected = {
        "Demand weight": 0.25,
        "Competition weight": 0.40,
        "Quality gap weight": 0.10,
        "Momentum weight": 0.10,
        "Stability weight": 0.05,
        "Weather resilience weight": 0.10,
    }
    for label, value in expected.items():
        assert by_label(app.slider, label).value == pytest.approx(value)
    assert list(table(app)["ZIP"]) != before
    assert len(site.source.calls) == 1


def test_tc163_slider_switches_preset_to_custom(site: Site) -> None:
    app = site.open()
    by_label(app.slider, "Demand weight").set_value(0.7)
    app.run()
    assert not app.exception
    assert by_label(app.selectbox, "Weight preset").value == "Custom"
    assert by_label(app.slider, "Demand weight").value == pytest.approx(0.7)


def test_tc164_top_n_and_minimum_confidence_keep_original_ranks(site: Site) -> None:
    app = show_all_rows(site.open())
    full = table(app)
    ranks = dict(zip(full["ZIP"], full["Rank"], strict=True))
    by_label(app.slider, "Show top areas").set_value(5)
    by_label(app.selectbox, "Minimum confidence").set_value("High")
    app.run()
    assert not app.exception
    frame = table(app)
    assert 0 < len(frame) <= 5
    assert set(frame["Confidence"]) == {"high"}
    for code, rank in zip(frame["ZIP"], frame["Rank"], strict=True):
        assert ranks[code] == rank


def test_tc165_tied_rank_is_shown_as_en_dash_range(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = scoring.rank_areas

    def tied(
        prepared: PreparedScope, weights: ScoreWeights, config: Any = None
    ) -> RecommendationResult:
        result = real(prepared, weights, config)
        scores = list(result.scores)
        scores[2] = replace(scores[2], tied_with_above=True, rank_low=2, rank_high=3)
        return replace(result, scores=tuple(scores))

    monkeypatch.setattr(scoring, "rank_areas", tied)
    app = site.open()
    ranks = list(table(app)["Rank"])
    assert ranks[0] == "1"
    assert ranks[2] == "2–3"


def test_tc199_all_zero_weights_warn_and_keep_last_valid_result(site: Site) -> None:
    app = site.open()
    before = list(table(app)["ZIP"])
    for slider in app.slider:
        if slider.label.endswith(" weight"):
            slider.set_value(0.0)
    app.run()
    assert not app.exception
    assert "Set at least one weight above 0." in messages(app.warning)
    assert list(table(app)["ZIP"]) == before
    with pytest.raises(ValueError):
        views.weights_from_state({f"site_rec_w_{key}": 0.0 for key in METRIC_KEYS})


# ---- TC-166 .. TC-168 ------------------------------------------------------------------------


def test_tc166_area_detail_shows_factors_table_and_why_this_rank(site: Site) -> None:
    app = site.open()
    by_label(app.selectbox, "Area (ZIP)").set_value("33606")
    app.run()
    assert not app.exception
    detail = table(app, "Metric")
    assert len(detail) == 6
    assert list(detail.columns) == [
        "Metric",
        "Value",
        "Percentile",
        "Weight used",
        "Contribution",
        "Note",
    ]
    factors = table(app, "Factor")
    assert len(factors) >= 3
    assert "Why this rank" in page_text(app)
    assert any("ZIP 33606 ranks" in str(item.value) for item in app.markdown)
    by_label(app.selectbox, "Area (ZIP)").set_value("33607")
    app.run()
    missing = table(app, "Metric")
    competition = missing[missing["Metric"] == "Competition"].iloc[0]
    assert competition["Value"] == "—"
    assert competition["Weight used"] == "0.00"


def test_tc167_compare_three_areas(site: Site) -> None:
    app = site.open()
    by_label(app.multiselect, "Compare areas (2–5)").set_value(["33606", "33608", "33610"])
    app.run()
    assert not app.exception
    frame = next(f.value for f in app.dataframe if "ZIP 33606" in f.value.columns)
    assert list(frame.columns) == ["ZIP 33606", "ZIP 33608", "ZIP 33610"]
    for row in ("Score", "Score range", "Confidence", "Demand source"):
        assert row in frame.index


def test_tc168_compare_limits(site: Site) -> None:
    app = site.open()
    picker = by_label(app.multiselect, "Compare areas (2–5)")
    picker.set_value(["33606"])
    app.run()
    assert "Select 2 to 5 areas to compare." in messages(app.info)
    assert not [f for f in app.dataframe if "ZIP 33606" in f.value.columns]
    chosen = ["33601", "33602", "33603", "33604", "33605", "33606"]
    by_label(app.multiselect, "Compare areas (2–5)").set_value(chosen)
    app.run()
    assert not app.exception
    assert "Select at most 5 areas." in messages(app.warning)
    frame = next(f.value for f in app.dataframe if "ZIP 33601" in f.value.columns)
    assert list(frame.columns) == [f"ZIP {code}" for code in chosen[:5]]


# ---- TC-169 .. TC-174 ------------------------------------------------------------------------


def test_tc169_fewer_than_five_areas_warns(site: Site) -> None:
    site.source.scope = make_scope(4)
    app = site.open()
    assert any(
        "Only 4 areas could be scored (fewer than 5)" in text for text in messages(app.warning)
    )
    assert len(table(app)) == 4


def test_tc170_no_scoreable_areas(site: Site) -> None:
    site.source.scope = make_scope(0)
    app = site.open()
    assert "No area has businesses in this category, so nothing can be scored." in messages(
        app.info
    )
    assert not app.dataframe


def test_tc171_no_coverage_names_the_imported_range(site: Site) -> None:
    site.source.scope = replace(make_scope(), covered_days=0, covered_start=None, covered_end=None)
    app = site.open()
    warnings = messages(app.warning)
    assert any(
        "no imported check-in coverage" in w and "2010-01-16" in w and "2022-01-19" in w
        for w in warnings
    )
    assert not app.dataframe


def test_tc172_database_loss_shows_setup_without_details(site: Site) -> None:
    site.source.error = psycopg.OperationalError("boom")
    app = site.open()
    assert "Database temporarily unavailable" in messages(app.info)
    assert "boom" not in page_text(app)
    assert not app.dataframe


def test_tc173_data_unavailable(site: Site) -> None:
    site.source.error = repository.DataUnavailableError("nothing")
    app = site.open()
    assert "No imported data for this selection." in messages(app.info)
    assert not app.dataframe


def force_estimator(monkeypatch: pytest.MonkeyPatch, report: EstimatorReport) -> None:
    real = scoring.prepare_scope
    monkeypatch.setattr(
        scoring, "prepare_scope", lambda scope, *a: replace(real(scope, *a), estimator=report)
    )


def estimator_report(**changes: Any) -> EstimatorReport:
    base = EstimatorReport(True, "ok", 9, 5, ("a",), 0.42, 0.61, 0.31, 0.1, 1.0)
    return replace(base, **changes)


def test_tc174_estimator_on(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    force_estimator(monkeypatch, estimator_report())
    app = site.open()
    assert "Demand model: ON · LOO-CV skill +0.31 (MAE 0.42 vs 0.61 baseline)" in messages(
        app.caption
    )
    assert not any("Demand model: OFF" in text for text in messages(app.info))


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        ("insufficient_training_areas", "fewer than 8 areas with at least 3 businesses"),
        ("no_skill", "it did not beat the metro average"),
    ],
)
def test_tc174_estimator_off(
    site: Site, monkeypatch: pytest.MonkeyPatch, reason: str, text: str
) -> None:
    force_estimator(
        monkeypatch, estimator_report(enabled=False, reason=reason, skill=None, mae_model=None)
    )
    app = site.open()
    expected = (
        f"Demand model: OFF ({text}). The metro average is used where niche businesses are few."
    )
    assert expected in messages(app.info)
    assert not any(c.startswith("Demand model: ON") for c in messages(app.caption))


# ---- TC-175 .. TC-181, TC-196 ----------------------------------------------------------------


def configure_ai(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    for name, value in {**ENV_OK, **extra}.items():
        monkeypatch.setenv(name, value)


def click(app: AppTest, label: str) -> AppTest:
    by_label(app.button, label).click()
    app.run()
    assert not app.exception, [e.message for e in app.exception]
    return app


def test_tc175_unconfigured_ai_uses_template_without_network(site: Site) -> None:
    app = site.open()
    assert "AI provider not configured. Buttons use the offline summary." in messages(app.caption)
    click(app, "Explain top picks")
    assert PROXY_NOTICE in "\n".join(messages(app.markdown))
    assert "33606" in "\n".join(messages(app.markdown)) or "336" in "\n".join(
        messages(app.markdown)
    )
    assert site.transport_calls == []


def test_tc176_configured_but_not_ticked_stays_offline(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_ai(monkeypatch)
    app = site.open()
    assert not by_label(app.checkbox, "Use AI provider for this summary").value
    click(app, "Explain top picks")
    assert site.transport_calls == []
    assert not any("AI-generated" in text for text in messages(app.caption))


def opt_in_and_pick(app: AppTest, picks: Sequence[str] = ("33606", "33608", "33610")) -> None:
    by_label(app.checkbox, "Use AI provider for this summary").check()
    by_label(app.multiselect, "Compare areas (2–5)").set_value(list(picks))
    app.run()
    assert not app.exception


def test_tc177_ai_success_labels_output_and_previews_payload(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_ai(monkeypatch)
    app = site.open()
    opt_in_and_pick(app)
    click(app, "Compare selected areas")
    assert "ZIP 33606 ranks first on demand." in messages(app.markdown)
    assert "AI-generated by m-test — verify against the table above." in messages(app.caption)
    assert len(site.transport_calls) == 1
    payload = "\n".join(messages(app.code))
    assert '"kind": "compare"' in payload
    assert "LEAK-" not in payload
    sent = json.loads(site.transport_calls[0])
    assert "LEAK-" not in json.dumps(sent)
    assert "api.example.test" in page_text(app)


def test_tc178_ai_timeout_falls_back_with_notice(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_ai(monkeypatch)

    def timeout(body: bytes) -> tuple[int, bytes]:
        raise TimeoutError

    site.transport_result = timeout
    app = site.open()
    opt_in_and_pick(app)
    click(app, "Explain top picks")
    assert any("timed out" in text for text in messages(app.warning))
    assert PROXY_NOTICE in "\n".join(messages(app.markdown))
    assert not any("AI-generated" in text for text in messages(app.caption))


def test_tc179_invalid_ai_configuration_shows_error_and_uses_template(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_ai(monkeypatch, SITESENSE_AI_BASE_URL="http://example.com/v1")
    app = site.open()
    errors = messages(app.error)
    assert errors
    assert "sk-test-123" not in "\n".join(errors)
    assert by_label(app.checkbox, "Use AI provider for this summary").disabled
    click(app, "Explain top picks")
    assert site.transport_calls == []
    assert PROXY_NOTICE in "\n".join(messages(app.markdown))


def test_tc180_no_secret_or_business_identity_anywhere(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_ai(monkeypatch)
    app = site.open()
    opt_in_and_pick(app)
    click(app, "Compare selected areas")
    ok_text = page_text(app)

    def refuse(body: bytes) -> tuple[int, bytes]:
        raise ConnectionRefusedError

    site.transport_result = refuse
    click(app, "Explain top picks")
    for text in (ok_text, page_text(app)):
        assert "sk-test-123" not in text
        assert "LEAK-" not in text
        assert "27.9506" not in text
        assert "82.4572" not in text


def test_tc181_free_text_question_is_truncated_and_shown_in_payload(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_ai(monkeypatch)
    app = site.open()
    by_label(app.checkbox, "Use AI provider for this summary").check()
    by_label(app.text_input, "Question (optional)").set_value("q" * 500)
    app.run()
    click(app, "Ask a question")
    assert len(site.transport_calls) == 1
    user = json.loads(site.transport_calls[0])["messages"][1]["content"]
    assert "QUESTION" in user
    assert "q" * 300 in user
    assert "q" * 301 not in user
    assert '"question": "' + "q" * 300 + '"' in "\n".join(messages(app.code))


def test_tc196_ai_markup_is_rendered_as_text(site: Site, monkeypatch: pytest.MonkeyPatch) -> None:
    configure_ai(monkeypatch)
    hostile = json.dumps({"choices": [{"message": {"content": "<script>alert(1)</script>"}}]})
    site.transport_result = lambda body: (200, hostile.encode())
    app = site.open()
    opt_in_and_pick(app)
    click(app, "Explain top picks")
    shown = [item for item in app.markdown if "<script>" in str(item.value)]
    assert shown
    assert all(not item.proto.allow_html for item in shown)


# ---- pure helpers: TC-182 .. TC-185, TC-200 ---------------------------------------------------


def test_tc182_format_rank_and_ranked_table() -> None:
    score = fx_s4_scores()["33603"]
    tied = replace(score, rank=3, tied_with_above=True, rank_low=2, rank_high=4)
    assert views.format_rank(tied) == "2–4"
    assert views.format_rank(replace(tied, tied_with_above=False)) == "3"
    frame = views.ranked_table([tied])
    assert frame.iloc[0]["Rank"] == "2–4"
    assert frame.iloc[0]["Score range"] == "—"
    assert frame.iloc[0]["Key strength"] == "Demand"


def csv_rows(scores: Sequence[AreaScore]) -> tuple[str, list[dict[str, str]]]:
    text = views.scores_to_csv(scores)
    return text, list(csv.DictReader(io.StringIO(text)))


def test_tc183_csv_contract() -> None:
    scores = fx_s4_scores()
    formula = replace(scores["33602"], display_name="=SUM(1)")
    text, rows = csv_rows([scores["33604"], formula, scores["33601"]])
    assert text.splitlines()[0] == CSV_HEADER
    assert "\r" not in text
    by_code = {row["postal_code"]: row for row in rows}
    assert by_code["33604"]["weather_resilience"] == ""
    assert by_code["33604"]["unavailable_metrics"] == "momentum;stability;weather_resilience"
    assert by_code["33602"]["display_name"] == "'=SUM(1)"
    assert by_code["33604"]["score"] == "100.00"
    assert by_code["33604"]["rank_low"] == ""
    assert {row["notes"] for row in rows} == {PROXY_NOTICE}
    assert by_code["33604"]["demand_rate_per_1000_business_days"] == "30"


def test_tc184_csv_is_deterministic() -> None:
    scores = list(fx_s4_scores().values())
    assert views.scores_to_csv(scores).encode() == views.scores_to_csv(scores).encode()


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (None, [190, 190, 190, 120]),
        (0, [255, 255, 204, 190]),
        (100, [37, 52, 148, 190]),
        (150, [37, 52, 148, 190]),
        (-5, [255, 255, 204, 190]),
    ],
)
def test_tc185_choropleth_color(score: float | None, expected: list[int]) -> None:
    assert views.choropleth_color(score) == expected


def test_tc200_metric_table_reweights_unavailable_metrics() -> None:
    score = fx_s4_scores()["33603"]
    frame = views.metric_table(score, 1.0)
    assert list(frame.columns) == [
        "Metric",
        "Value",
        "Percentile",
        "Weight used",
        "Contribution",
        "Note",
    ]
    assert len(frame) == 6
    rows = frame.set_index("Metric")
    assert rows.loc["Demand", "Weight used"] == "0.50"
    assert rows.loc["Competition", "Weight used"] == "0.29"
    assert rows.loc["Weather resilience", "Weight used"] == "0.21"
    for metric in ("Quality gap", "Momentum", "Stability"):
        assert rows.loc[metric, "Value"] == "—"
        assert rows.loc[metric, "Percentile"] == "—"
        assert rows.loc[metric, "Weight used"] == "0.00"
        assert rows.loc[metric, "Note"]
    assert rows.loc["Quality gap", "Note"] == "no competitors"
    assert rows.loc["Demand", "Value"] == "20.00"
    assert sum(float(v) for v in frame["Contribution"]) == pytest.approx(39.3, abs=0.1)


def test_compare_table_rows_and_unavailable_cells() -> None:
    scores = fx_s4_scores()
    frame = views.compare_table([scores["33604"], scores["33603"]])
    assert list(frame.columns) == ["ZIP 33604", "ZIP 33603"]
    assert frame.loc["Score", "ZIP 33604"] == "100.0"
    assert frame.loc["Weather resilience", "ZIP 33604"] == "—"
    assert frame.loc["Competition (open competitors)", "ZIP 33604"] == "0"
    assert "Top factors" in frame.index


# ---- TC-186, TC-187, TC-188, TC-189, TC-195 ---------------------------------------------------


def test_tc186_map_colors_scored_zips_and_greys_unscored(
    site: Site, monkeypatch: pytest.MonkeyPatch
) -> None:
    codes = [f"{33600 + i}" for i in range(1, 13)] + ["33620"]
    monkeypatch.setattr(
        zip_boundaries,
        "load_zip_boundaries",
        lambda: {code: square(code, 0.01 * i) for i, code in enumerate(codes)},
    )
    decks: list[Any] = []
    monkeypatch.setattr(st, "pydeck_chart", lambda deck, **kwargs: decks.append(deck))
    app = site.open()
    assert len(decks) == 1
    features = decks[0].layers[0].data["features"]
    assert len(features) == 13
    colors = {f["properties"]["postal_code"]: f["properties"]["fill_color"] for f in features}
    assert colors["33620"] == [190, 190, 190, 120]
    assert all(colors[code][3] == 190 for code in codes[:-1])
    assert any("0" in text and "100" in text and "Gray" in text for text in messages(app.caption))


def test_tc187_scope_change_clears_selections(site: Site) -> None:
    app = site.open()
    last = list(table(app)["ZIP"])[-1]
    by_label(app.selectbox, "Area (ZIP)").set_value(last)
    by_label(app.multiselect, "Compare areas (2–5)").set_value(["33601", "33602"])
    app.run()
    click(app, "Explain top picks")
    assert "site_rec_ai_result" in app.session_state
    app.selectbox(key="category").set_value("Day Spas")
    app.run()
    assert not app.exception
    assert not app.session_state["site_rec_compare"]
    assert "site_rec_ai_result" not in app.session_state
    assert app.session_state["site_rec_area"] != last


def test_tc188_no_forbidden_wording(site: Site) -> None:
    app = site.open()
    click(app, "Explain top picks")
    text = page_text(app) + views.scores_to_csv(list(fx_s4_scores().values()))
    assert not FORBIDDEN.search(text)


def test_tc189_method_and_limits_content(site: Site) -> None:
    text = expander_text(site.open(), "Method and limits").lower()
    for phrase in ("activity proxy", "not causal", "review text is not loaded", "model estimate"):
        assert phrase in text
    assert "bootstrap" in text


def test_tc195_cache_miss_logs_once(site: Site, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sitesense.site_recommendation")
    app = site.open()
    prepared_logs = [r for r in caplog.records if r.getMessage().startswith("scope_prepared")]
    assert len(prepared_logs) == 1
    assert "estimator_enabled" in prepared_logs[0].getMessage()
    by_label(app.selectbox, "Weight preset").set_value("Demand-focused")
    app.run()
    again = [r for r in caplog.records if r.getMessage().startswith("scope_prepared")]
    assert len(again) == 1


HISTORY_HINT = (
    "Momentum needs at least 24 months and Stability at least 12 months of covered "
    "check-in data. Widen the Date range to include them."
)


def test_tc201_short_history_hint_only_when_most_areas_lack_it(site: Site) -> None:
    long_app = site.open()
    assert HISTORY_HINT not in messages(long_app.caption)
    scope = make_scope()
    short = tuple(
        replace(
            area,
            covered_days=180,
            covered_month_count=6,
            members=tuple(BusinessPoint(m.checkins, m.monthly[:6]) for m in area.members),
        )
        for area in scope.areas
    )
    site.source.scope = replace(
        scope, areas=short, covered_days=180, covered_months=scope.covered_months[:6]
    )
    st.cache_data.clear()
    app = site.open()
    captions = messages(app.caption)
    assert HISTORY_HINT in captions
    coverage = next(i for i, text in enumerate(captions) if text.startswith("Data coverage:"))
    assert captions[coverage + 1] == HISTORY_HINT
