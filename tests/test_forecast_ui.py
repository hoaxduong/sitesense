"""Exercise forecast form behavior with an actual fitted, local test model."""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from sklearn.dummy import DummyRegressor
from sklearn.pipeline import Pipeline
from streamlit.testing.v1 import AppTest

from sitesense import forecast, repository
from sitesense.forecast_ui import _artifact_revision, _forecast_bundle


@pytest.fixture(autouse=True)
def forecast_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    _forecast_bundle.clear()


@pytest.fixture
def bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> forecast.ForecastBundle:
    rows = [
        {
            "group_id": f"area-{index}|Day Spas",
            "area_id": f"area-{index}",
            "metro": "Tampa",
            "city": "Tampa",
            "state": "FL",
            "zip": f"00{index:03d}",
            "category": "Day Spas",
            "latitude": 27.9,
            "longitude": -82.4,
            "station_id": f"station-{index}",
        }
        for index in (1, 2)
    ]
    locations = pd.DataFrame(rows)
    features = locations.assign(
        calendar_month=12, day_of_week=1, tmax_c=27.0, tmin_c=19.0, precip_mm=2.0
    )[forecast.FEATURES]
    model = Pipeline([("model", DummyRegressor(strategy="constant", constant=0.3))])
    model.fit(features, [0.2, 0.4])
    weather = pd.DataFrame(
        [
            {
                "station_id": f"station-{index}",
                "date": date(2026, 9, 1),
                "tmax_c": 30.0,
                "tmin_c": 21.0,
                "precip_mm": 1.0,
                "complete_weather": index == 1,
            }
            for index in (1, 2)
        ]
    )
    baseline = pd.DataFrame(
        [
            {
                "area_id": f"area-{index}",
                "category": "Day Spas",
                "calendar_month": month,
                "mean_rate": 0.2,
            }
            for index in (1, 2)
            for month in range(1, 13)
        ]
    )
    manifest: dict[str, Any] = {
        "model_name": "hgb_weather",
        "trained_start": "2010-01-01",
        "trained_end": "2021-12-31",
        "weather_range": {
            "tmax_c": {"min": -20, "max": 40},
            "tmin_c": {"min": -30, "max": 30},
            "precip_mm": {"min": 0, "max": 100},
        },
        "evaluation_metrics": [
            {"period": "test_2019", "model": "hgb_weather", "poisson_deviance": 0.06}
        ],
    }
    result = forecast.ForecastBundle(model, locations, weather, baseline, manifest)
    (tmp_path / "manifest.json").write_text(json.dumps({"revision": 1}))
    monkeypatch.setattr(forecast, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(forecast, "load_bundle", lambda directory: result)
    return result


def _forecast_component() -> None:
    from sitesense.forecast_ui import render_forecast

    render_forecast()


def _app(monkeypatch: pytest.MonkeyPatch) -> AppTest:

    def database_not_used(*args: object, **kwargs: object) -> None:
        pytest.fail("Forecast component must not query the database")

    for name in ("get_import_revision", "load_catalog", "load_activity", "list_categories"):
        monkeypatch.setattr(repository, name, database_not_used)
    app = AppTest.from_function(_forecast_component, default_timeout=15).run()
    assert not app.exception
    return app


def test_missing_model_is_safe_and_does_not_query_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(forecast, "MODEL_DIR", tmp_path)
    app = _app(monkeypatch)
    assert app.info[0].value == "Forecast model unavailable"
    assert not app.metric
    assert not app.dataframe
    assert "04_full_history_forecast_training.ipynb" in app.caption[-1].value
    visible = " ".join(element.value for element in app.markdown)
    assert str(tmp_path) not in visible


def test_future_manual_prediction_and_export_include_actual_inputs(
    bundle: forecast.ForecastBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch)
    assert not app.metric
    app.date_input(key="forecast_date").set_value(date(2035, 4, 15))
    app.number_input(key="forecast_tmax").set_value(29.0)
    app.number_input(key="forecast_tmin").set_value(20.0)
    app.number_input(key="forecast_rain").set_value(3.0)
    app.button(key="forecast_predict").click().run()
    assert not app.exception
    assert not app.warning
    result = app.session_state["forecast_result"]
    assert len(result) == 2
    assert set(result["predicted_rate"]) == {0.3}
    assert set(result["date"]) == {date(2035, 4, 15)}
    assert set(result["tmax_c"]) == {29.0}
    assert set(result["weather_source"]) == {"supplied_weather"}
    assert "activity" not in result.columns
    assert list(app.dataframe[0].value["ZIP"]) == ["00001", "00002"]
    assert any("2035-04-15" in element.value for element in app.subheader)
    assert app.get("download_button")[0].label == "Download all predictions (CSV)"


def test_observed_weather_reports_exclusions_and_missing_future_weather(
    bundle: forecast.ForecastBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch)
    app.button_group(key="forecast_weather_mode").select("Use observed station weather").run()
    app.date_input(key="forecast_date").set_value(date(2026, 9, 1))
    app.button(key="forecast_predict").click().run()
    assert not app.exception
    result = app.session_state["forecast_result"]
    assert len(result) == 1
    assert result.attrs["weather_excluded_candidates"] == 1
    assert any("excluded" in element.value for element in app.info)
    app.date_input(key="forecast_date").set_value(date(2035, 4, 15))
    app.button(key="forecast_predict").click().run()
    assert not app.exception
    assert "No complete observed weather" in app.warning[0].value
    assert app.session_state["forecast_result"] is None
    assert not app.metric


def test_weather_mode_switch_clears_result_and_preserves_scenario_inputs(
    bundle: forecast.ForecastBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch)
    scenario = {"forecast_tmax": 31.0, "forecast_tmin": 23.0, "forecast_rain": 7.5}
    for key, value in scenario.items():
        app.number_input(key=key).set_value(value)
    app.button(key="forecast_predict").click().run()
    assert not app.exception
    assert app.session_state["forecast_result"] is not None

    app.button_group(key="forecast_weather_mode").select("Use observed station weather").run()
    assert not app.exception
    assert app.session_state["forecast_result"] is None
    assert not app.metric

    app.button_group(key="forecast_weather_mode").select("Enter a weather scenario").run()
    assert not app.exception
    assert app.session_state["forecast_result"] is None
    for key, value in scenario.items():
        assert app.number_input(key=key).value == value


def test_invalid_temperature_clears_previous_result(
    bundle: forecast.ForecastBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(monkeypatch)
    app.button(key="forecast_predict").click().run()
    assert app.session_state["forecast_result"] is not None
    app.number_input(key="forecast_tmin").set_value(28.0)
    app.button(key="forecast_predict").click().run()
    assert not app.exception
    assert "minimum temperature must not exceed" in app.warning[0].value
    assert app.session_state["forecast_result"] is None
    assert not app.metric


def test_manifest_revision_reloads_resource_and_clears_predictions(
    bundle: forecast.ForecastBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    loads: list[Path] = []

    def loader(directory: Path) -> forecast.ForecastBundle:
        loads.append(directory)
        return bundle

    monkeypatch.setattr(forecast, "load_bundle", loader)
    app = _app(monkeypatch)
    app.button(key="forecast_predict").click().run()
    app.run()
    assert len(loads) == 1
    initial_revision = _artifact_revision(forecast.MODEL_DIR)
    (forecast.MODEL_DIR / "manifest.json").write_text(json.dumps({"revision": 2}))
    assert _artifact_revision(forecast.MODEL_DIR) != initial_revision
    app.run()
    assert not app.exception
    assert len(loads) == 2
    assert app.session_state["forecast_result"] is None


def test_smaller_location_scope_clears_results_and_adjusts_display_limit(
    bundle: forecast.ForecastBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    additional = bundle.locations.iloc[[0]].assign(
        group_id="nashville-1|Restaurants",
        area_id="nashville-1",
        metro="Nashville",
        city="Nashville",
        state="TN",
        zip="37203",
        category="Restaurants",
    )
    baseline = bundle.baseline.loc[bundle.baseline["area_id"] == "area-1"].assign(
        area_id="nashville-1", category="Restaurants"
    )
    expanded = replace(
        bundle,
        locations=pd.concat([bundle.locations, additional], ignore_index=True),
        baseline=pd.concat([bundle.baseline, baseline], ignore_index=True),
    )
    monkeypatch.setattr(forecast, "load_bundle", lambda directory: expanded)
    app = _app(monkeypatch)
    app.button(key="forecast_predict").click().run()
    assert app.session_state["forecast_result"] is not None
    app.selectbox(key="forecast_metro").select("Nashville").run()
    assert not app.exception
    assert app.session_state["forecast_result"] is None
    assert app.number_input(key="forecast_top_n").value == 1
    app.button(key="forecast_predict").click().run()
    assert not app.exception
    assert len(app.session_state["forecast_result"]) == 1


def test_incompatible_bundle_does_not_display_private_failure_details(
    bundle: forecast.ForecastBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(directory: Path) -> forecast.ForecastBundle:
        raise forecast.ArtifactUnavailableError("Private local path and failure details")

    monkeypatch.setattr(forecast, "load_bundle", unavailable)
    app = _app(monkeypatch)
    assert app.info[0].value == "Forecast model unavailable"
    assert not app.metric
    assert not any("Private" in element.value for element in app.markdown)
