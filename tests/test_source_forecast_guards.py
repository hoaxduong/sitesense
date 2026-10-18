"""A selected niche must never inherit broad-sector predictions or model cohorts."""

from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
from sklearn.pipeline import Pipeline
from streamlit.testing.v1 import AppTest

from sitesense import (
    forecast_cohort,
    forecast_range,
    forecast_ui,
    hourly_weather,
    repository,
    scientific_views,
    ui,
)
from sitesense.forecast import ForecastBundle
from sitesense.models import (
    ActivityRecord,
    AreaSummary,
    CandidateArea,
    Catalog,
    DateCoverage,
    Filters,
)

AREA = CandidateArea(1, "Philadelphia", "PA", "19101", "Centre", None, None)
COVERAGE = DateCoverage(date(2020, 1, 1), date(2020, 1, 10))
FILTERS = Filters("Philadelphia", "PA", "Restaurants", date(2020, 1, 9), date(2020, 1, 12))
CONTEXT = ui.ScreenContext(
    FILTERS,
    Catalog((AREA,), (), (), COVERAGE),
    (ActivityRecord(1, date(2020, 1, 9), 12, 7),),
    (AreaSummary(AREA, 5, 7, 24.5, 4.0, 0, None, None),),
    "same-import",
)


@pytest.fixture(autouse=True)
def _clear_parent_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hourly_weather, "load_samples", lambda *args: ())
    ui._forecast_estimates.clear()
    ui._recorded_cohort_activity.clear()


def _niche(context: ui.ScreenContext = CONTEXT) -> ui.ScreenContext:
    return replace(context, filters=replace(context.filters, source_category="Pizza"))


def test_source_forecast_does_not_load_or_reuse_a_warm_parent_prediction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = forecast_range.ForecastRange(
        (
            forecast_range.AreaForecast(
                AREA.id,
                (
                    forecast_range.ForecastPoint(
                        AREA.id,
                        date(2020, 1, 11),
                        0.5,
                        0.25,
                        "observed",
                        "station",
                        10.0,
                        2.0,
                        0.0,
                        False,
                    ),
                ),
                2,
            ),
        ),
        2,
    )
    bundle = Mock(return_value=object())
    predict = Mock(return_value=parent)
    revision = Mock(return_value="same-artifact")
    monkeypatch.setattr(forecast_ui, "_artifact_revision", revision)
    monkeypatch.setattr(forecast_ui, "_forecast_bundle", bundle)
    monkeypatch.setattr(forecast_range, "predict_range", predict)

    assert ui._activity_forecast(CONTEXT).estimated_dates == (date(2020, 1, 11),)
    assert ui._activity_forecast(CONTEXT).estimated_dates == parent.estimated_dates
    source = ui._activity_forecast(_niche())
    assert not source.estimated_dates
    assert source.uncovered_days == 2
    assert source.by_area[AREA.id].mean_rate is None
    assert source.by_area[AREA.id].unavailable_days == 2
    assert source.reason and "source categories" in source.reason
    bundle.assert_called_once()
    predict.assert_called_once()
    assert revision.call_count == 2
    assert ui._activity_forecast(CONTEXT).estimated_dates == parent.estimated_dates
    predict.assert_called_once()


def test_source_cohort_does_not_query_or_reuse_a_warm_parent_cohort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = forecast_cohort.CohortReference(
        (forecast_cohort.CohortArea(AREA.id, ("broad-sector-member",), 1),)
    )
    records = (ActivityRecord(AREA.id, date(2020, 1, 9), 12, 9),)
    reference = Mock(return_value=parent)
    activity = Mock(return_value=records)
    revision = Mock(return_value="same-parent-cohort")
    monkeypatch.setattr(forecast_cohort, "artifact_revision", revision)
    monkeypatch.setattr(forecast_cohort, "load_reference", reference)
    monkeypatch.setattr(repository, "load_activity", activity)

    assert ui._cohort_activity(CONTEXT) == (parent, records)
    assert ui._cohort_activity(CONTEXT) == (parent, records)
    source, source_records = ui._cohort_activity(_niche())
    assert source_records is None
    assert not source.business_ids
    assert not source.denominators
    assert source.reason and "source category" in source.reason
    reference.assert_called_once()
    activity.assert_called_once()
    loaded_filters = activity.call_args.args[0]
    assert loaded_filters.source_category is None
    assert loaded_filters.start_date == date(2020, 1, 9)
    assert loaded_filters.end_date == date(2020, 1, 10)
    assert activity.call_args.kwargs["business_ids"] == ("broad-sector-member",)
    assert revision.call_count == 2
    assert ui._cohort_activity(CONTEXT) == (parent, records)
    activity.assert_called_once()


def test_direct_reference_load_rejects_source_without_reading_parent_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified_files = Mock(side_effect=AssertionError("Source scope must not load parent cohort"))
    monkeypatch.setattr(forecast_cohort, "_verified_files", verified_files)
    result = forecast_cohort.load_reference(
        Path("/nonexistent-parent-bundle"), _niche().filters, CONTEXT.catalog, (AREA,)
    )
    assert not result.business_ids
    assert not result.denominators
    assert result.by_area[AREA.id].n_businesses == 0
    assert result.by_area[AREA.id].reason and "source category" in str(result.reason)
    verified_files.assert_not_called()


def test_direct_range_prediction_rejects_source_before_location_or_weather_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    predict = Mock(side_effect=AssertionError("Source scope must not predict parent locations"))
    monkeypatch.setattr(forecast_range, "predict_locations", predict)
    bundle = ForecastBundle(
        Pipeline([]),
        pd.DataFrame(),
        pd.DataFrame(),
        pd.DataFrame(),
        {"model_name": "hgb_weather", "trained_end": "2020-01-10"},
    )
    result = forecast_range.predict_range(bundle, _niche().filters, COVERAGE, (AREA,))
    assert not result.estimated_dates
    assert result.uncovered_days == 2
    assert result.by_area[AREA.id].mean_rate is None
    assert result.by_area[AREA.id].unavailable_days == 2
    assert result.reason and "source categories" in result.reason
    predict.assert_not_called()


def _render_science() -> None:
    import streamlit as st

    from sitesense import scientific_views

    scientific_views.render(st.session_state["guard_context"], (1,), "guard")


def test_source_scientific_panel_removes_stale_parent_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    history = Mock(return_value=([], "Recorded ZIP check-ins / day"))
    evaluation = Mock(side_effect=AssertionError("Source scope must not load parent diagnostics"))
    response = Mock(side_effect=AssertionError("Source scope must not load parent response"))
    monkeypatch.setattr(repository, "get_import_revision", lambda: "same-import")
    monkeypatch.setattr(scientific_views, "_historical", history)
    monkeypatch.setattr(scientific_views, "_evaluation", evaluation)
    monkeypatch.setattr(scientific_views, "_response", response)
    app = AppTest.from_function(_render_science)
    app.session_state["guard_context"] = CONTEXT
    app.session_state["guard_science_open"] = True
    app.run()
    assert not app.exception
    assert app.segmented_control(key="science_family").options == [
        "Weather patterns",
        "Model diagnostics",
    ]

    app.session_state["guard_context"] = _niche()
    app.session_state["guard_science_open"] = True
    app.session_state["science_family"] = "Model diagnostics"
    app.session_state["science_model_chart"] = "Weather response (ICE)"
    app.run()
    assert not app.exception
    family = app.segmented_control(key="science_family")
    assert family.options == ["Weather patterns"]
    assert family.value == "Weather patterns"
    assert app.selectbox(key="science_weather_chart").options == [
        "Activity and weather over time",
        "Calendar heatmap",
    ]
    assert all(widget.key != "science_model_chart" for widget in app.selectbox)
    assert history.call_args.args[0].filters.source_category == "Pizza"
    assert any("do not apply to this niche" in caption.value for caption in app.caption)
    evaluation.assert_not_called()
    response.assert_not_called()
