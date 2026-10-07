from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from sitesense import sample_data

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"
PAGES = {
    "ranking": "Where should the next store open?",
    "activity": "When are customers active?",
    "weather": "How does weather change activity?",
    "forecast": "What demand should we expect?",
    "compare": "Compare candidate sites",
}
CAVEATS = {
    "ranking": "proxy",
    "activity": "proxy",
    "weather": "associations",
    "forecast": "do not prove causal effects",
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
    assert app.multiselect(key="categories").value == [sample_data.CATEGORY]


@pytest.mark.parametrize("page", list(PAGES))
def test_each_page_renders_with_sample_and_caveat_banners(page: str) -> None:
    app = run_page(page)

    assert not app.exception, [error.message for error in app.exception]
    assert app.title[0].value == PAGES[page]
    assert any("Sample data" in item.value for item in app.warning)
    assert any(CAVEATS[page] in item.value for item in app.info)


def test_changing_weights_reorders_the_ranking() -> None:
    app = run_page("ranking")
    default_top = app.dataframe[0].value.Area.iloc[0]

    for key in ("growth", "rating", "resilience", "demand"):
        app.slider(key=f"w_{key}").set_value(0)
    app.slider(key="w_competition").set_value(100).run()

    ranking = app.dataframe[0].value
    assert not app.exception
    assert ranking.Area.iloc[0] != default_top
    assert ranking.Competitors.iloc[0] == ranking.Competitors.min()


def test_weather_page_hides_events_with_too_few_days() -> None:
    app = run_page("weather")
    app.selectbox[0].set_value("hot_day").run()
    app.select_slider(key="level_hot_day").set_value("fixed_35").run()

    assert not app.exception
    assert any("Hot day (max temp ≥ 35 °C), n = 11" in item.value for item in app.caption)
    assert any(
        metric.label == "Clear result?" and metric.value == "Hidden" for metric in app.metric
    )
    assert any("fewer than 20" in item.value for item in app.info)


def test_without_a_category_pages_ask_for_one() -> None:
    app = run_page("ranking")
    app.multiselect(key="categories").set_value([]).run()

    assert not app.exception
    assert any("Select at least one business category" in item.value for item in app.warning)
    assert not app.title
