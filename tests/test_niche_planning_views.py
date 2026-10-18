"""Future planning stays separate from recorded totals and uses exact niche support."""

import csv
import io
from datetime import date
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import niche_forecast, niche_planning_views
from sitesense.niche_forecast import NicheBundle, NicheGroup, NicheProjection


def _group(source: str = "Pizza", area: int = 1, n: int = 5, rate: float = 1) -> NicheGroup:
    return NicheGroup(
        "Philadelphia",
        "PA",
        "Restaurants",
        source,
        area,
        f"1910{area}",
        39.95,
        -75.15,
        n,
        int(rate * n * 1096),
        rate,
        (rate,) * 84,
        "a" * 64,
    )


def _bundle() -> NicheBundle:
    metrics = {
        name: {"poisson_deviance": 0.2, "mae": 0.1, "groups": 2, "observations": 730}
        for name in niche_forecast.MODEL_NAMES
    }
    return NicheBundle(
        (_group(), _group(area=2), _group("Sushi Bars", 3, rate=2)),
        {
            "model_name": "overall_mean",
            "training_start": "2019-01-01",
            "training_end": "2021-12-31",
            "validation_metrics": metrics,
            "assessment_metrics": metrics,
        },
    )


def _app(source: str | None) -> AppTest:
    return AppTest.from_string(
        f"""
from datetime import date
from sitesense.models import Catalog, DateCoverage, Filters
from sitesense.ui import ScreenContext
from sitesense.niche_planning_views import render
filters = Filters(
    'Philadelphia', 'PA', 'Restaurants', date(2020,1,1), date(2020,1,31),
    source_category={source!r},
)
catalog = Catalog((), (), (), DateCoverage(date(2010,1,1), date(2022,1,19)))
context = ScreenContext(filters, catalog, (), (), 'import')
render(context)
""",
        default_timeout=10,
    )


@pytest.fixture
def planning(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[str]:
    (tmp_path / "manifest.json").write_text("{}")
    (tmp_path / "groups.json").write_text("[]")
    monkeypatch.setattr(niche_forecast, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(niche_forecast, "code_sha256", lambda: "code")
    monkeypatch.setattr(niche_forecast, "lock_sha256", lambda: "lock")
    monkeypatch.setattr(niche_planning_views, "_bundle", lambda *_: _bundle())
    downloads: list[str] = []

    def capture(_label: str, data: bytes, **_kwargs: object) -> bool:
        downloads.append(data.decode())
        return False

    monkeypatch.setattr(st, "download_button", capture)
    return downloads


def test_category_projection_weights_fixed_cohorts_and_exposes_geographic_support() -> None:
    a, b = _group(n=5, rate=2), _group(area=2, n=15, rate=0)
    rows = niche_planning_views.category_rows(
        (NicheProjection(a, 30, 2, 60), NicheProjection(b, 30, 0, 0))
    )
    assert rows[0]["Projected check-ins / business / day"] == 0.5
    assert rows[0]["Projected check-ins / business / period"] == 15
    assert rows[0]["Historical cohort businesses"] == 20
    assert rows[0]["Supported ZIPs"] == 2


def test_month_planning_ignores_recorded_dates_and_keeps_scope_in_export(
    planning: list[str],
) -> None:
    app = _app(None).run()
    assert not app.exception
    assert app.segmented_control(key="niche_planning_period").value == "Month"
    assert app.selectbox(key="niche_planning_month").value > date.today()
    rows = list(csv.DictReader(io.StringIO(planning[-1])))
    assert {row["Source category"] for row in rows} == {"Pizza", "Sushi Bars"}
    assert rows[0]["Source category"] == "Sushi Bars"
    assert all(row["Category"] == "Restaurants" and row["Metro"] == "Philadelphia" for row in rows)
    assert all(row["Estimate_kind"] == "archived_cohort_calendar_projection" for row in rows)
    assert all(date.fromisoformat(row["Planning_start"]) > date.today() for row in rows)
    assert any("Historical check-in data ends in 2022" in item.value for item in app.warning)


def test_selected_niche_gets_only_its_zips_and_can_switch_to_single_future_day(
    planning: list[str],
) -> None:
    app = _app("Pizza").run()
    assert not app.exception
    rows = list(csv.DictReader(io.StringIO(planning[-1])))
    assert {row["ZIP"] for row in rows} == {"19101", "19102"}
    assert all(row["Source category"] == "Pizza" for row in rows)
    app.segmented_control(key="niche_planning_period").set_value("Day").run()
    assert not app.exception
    rows = list(csv.DictReader(io.StringIO(planning[-1])))
    assert all(row["Planning_start"] == row["Planning_end"] for row in rows)
    assert all(
        float(row["Projected check-ins / business / period"])
        == pytest.approx(float(row["Projected check-ins / business / day"]))
        for row in rows
    )


def test_unsupported_source_does_not_substitute_broad_or_other_niche(planning: list[str]) -> None:
    app = _app("Pizza Delivery").run()
    assert not app.exception
    assert not planning
    assert any("No supported historical cohort" in item.value for item in app.info)
    assert not app.dataframe


def test_missing_or_incompatible_bundle_is_safe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(niche_forecast, "MODEL_DIR", tmp_path)
    app = _app("Pizza").run()
    assert not app.exception
    assert any("unavailable" in item.value for item in app.info)
