from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sitesense import sample_data

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture(autouse=True)
def sample_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Page tests use the deterministic sample tables, never a developer database."""
    monkeypatch.delenv("DATABASE_URL", raising=False)


PAGES = {
    "ranking": "Where should the next store open?",
    "activity": "When are customers active?",
    "weather": "How does weather change activity?",
    "forecast": "What does a normal year look like?",
    "compare": "Compare candidate sites",
}
CAVEATS = {
    "ranking": "proxy",
    "activity": "proxy",
    "weather": "associations",
    "forecast": "not a forecast",
    "compare": "proxy",
}


def run_page(page: str) -> AppTest:
    script = (
        "from sitesense.components.sidebar import render_sidebar\n"
        f"from sitesense.pages import {page}\n"
        "render_sidebar()\n"
        f"{page}.render()\n"
    )
    return AppTest.from_string(script, default_timeout=120).run()


def test_entry_point_opens_on_the_site_ranking_page() -> None:
    app = AppTest.from_file(str(APP_FILE), default_timeout=120).run()

    assert not app.exception
    assert app.title[0].value == PAGES["ranking"]
    assert app.selectbox(key="metro").value == sample_data.METRO
    assert app.selectbox(key="category").value == sample_data.CATEGORY


@pytest.mark.parametrize("page", list(PAGES))
def test_each_page_renders_with_sample_and_caveat_banners(page: str) -> None:
    app = run_page(page)

    assert not app.exception, [error.message for error in app.exception]
    assert app.title[0].value == PAGES[page]
    assert any("Sample data" in item.value for item in app.warning)
    assert any(CAVEATS[page] in item.value for item in app.info)


def _set_weights(app: AppTest, only: str) -> AppTest:
    for key in ("demand", "growth", "rating", "resilience", "competition"):
        app.slider(key=f"w_{key}").set_value(100 if key == only else 0)
    return app.run()


def test_changing_weights_reorders_the_ranking() -> None:
    app = _set_weights(run_page("ranking"), "demand")
    by_demand = app.dataframe[0].value
    assert by_demand["Check-ins / wk"].iloc[0] == by_demand["Check-ins / wk"].max()

    app = _set_weights(app, "competition")
    by_competition = app.dataframe[0].value
    assert not app.exception
    assert by_competition.Competitors.iloc[0] == by_competition.Competitors.min()
    assert by_competition.Area.iloc[0] != by_demand.Area.iloc[0]


def test_weather_page_explains_events_with_too_few_days() -> None:
    app = run_page("weather")
    app.selectbox[0].set_value("heat").run()
    app.selectbox(key="level_heat").set_value("warning").run()

    assert not app.exception
    assert any("Too few days" in item.value for item in app.markdown)
    assert any("fewer than 20" in item.value for item in app.info)


def test_typical_year_scenario_lowers_only_the_disruption_week() -> None:
    app = run_page("forecast")

    assert not app.exception
    assert app.radio[0].value != "Typical season"
    impact = next(metric for metric in app.metric if metric.label == "Impact in disruption week")
    assert impact.value.startswith("−")
    assert any("not a forecast model" in item.value for item in app.caption)
