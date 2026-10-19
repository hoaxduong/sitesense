"""Real HGB fitting, weather sensitivity, temporal boundaries and artifact verification."""

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import joblib
import pandas as pd
import pytest

from sitesense import (
    business_forecast,
    business_hgb,
    business_hgb_training,
    business_observed,
    business_research,
    model_guide,
)
from sitesense.business_training import TrainingBusiness, TrainingData, TrainingDay
from sitesense.models import Business, Catalog, DateCoverage, Filters


@pytest.fixture(scope="module")
def fitted() -> business_hgb.HgbBundle:
    days = pd.date_range("2017-01-01", "2021-12-31").date
    businesses = tuple(
        TrainingBusiness(
            identifier,
            identifier,
            "Philadelphia",
            "PA",
            40.0,
            -75.0,
            ("Restaurants", "Pizza"),
            date(2016, 1, 1),
        )
        for identifier in ("a", "b")
    )
    activity = tuple(
        TrainingDay(b.business_id, day, (8 if day.day % 2 else 1) if b.business_id == "a" else 2)
        for b in businesses
        for day in days
    )
    profiles = tuple(
        Business(
            b.business_id,
            None,
            b.name,
            b.city,
            b.state,
            "",
            b.latitude,
            b.longitude,
            b.categories,
            None,
            0,
            True,
            "station",
        )
        for b in businesses
    )
    weather = pd.DataFrame(
        {
            "weather_cell_id": "station",
            "day": days,
            "tmin_c": 10.0,
            "tmax_c": 20.0,
            "precipitation_mm": [10.0 if day.day % 2 else 0.0 for day in days],
        }
    )
    sources = business_research.ResearchSources(
        TrainingData(businesses, activity, date(2017, 1, 1), date(2021, 12, 31)),
        Catalog((), profiles, (), DateCoverage(date(2017, 1, 1), date(2021, 12, 31))),
        weather,
        "test-import",
        "source-weather",
    )
    models, normals, metrics, counts = business_hgb_training.fit_scope(
        sources, "Philadelphia", "PA", "Restaurants"
    )
    assert counts["final"]["with_training_weather"] == 2
    assert len(metrics) == 6
    for stage in ("validation", "test"):
        scores = {r["model"]: r for r in metrics if r["stage"] == stage}
        assert len({r["observations"] for r in scores.values()}) == 1
        assert scores["hgb"]["mae"] < scores["overall_mean"]["mae"]
    return business_hgb.HgbBundle(
        models,
        normals,
        {
            "training_start": "2019-01-01",
            "training_end": "2021-12-31",
            "evaluation_metrics": metrics,
        },
    )


def _project(
    bundle: business_hgb.HgbBundle,
    weather: business_hgb.Weather | None = None,
    start: date = date(2026, 1, 1),
    end: date = date(2026, 1, 7),
) -> tuple[business_forecast.BusinessProjection, ...]:
    return business_hgb.project(
        bundle, "Philadelphia", "PA", "Restaurants", "Pizza", start, end, ("a",), weather
    )


def test_hgb_weather_changes_estimates_and_daily_values_sum_to_total(
    fitted: business_hgb.HgbBundle,
) -> None:
    dry = _project(fitted, (10, 20, 0))[0]
    wet = _project(fitted, (10, 20, 10))[0]
    assert wet.daily_rate > dry.daily_rate + 1
    assert dry.group.business_id == "a"
    assert dry.period_rate == pytest.approx(sum(dry.group.rates[i] for i in range(7)))
    assert dry.calendar_days == 7


def test_missing_normals_are_unknown_but_custom_weather_can_be_used(
    fitted: business_hgb.HgbBundle,
) -> None:
    normals = dict(fitted.normals)
    normals["a", "Restaurants"] = (None,) + normals["a", "Restaurants"][1:]
    missing = replace(fitted, normals=normals)
    assert _project(missing) == ()
    assert len(_project(missing, (10, 20, 0))) == 1


@pytest.mark.parametrize("weather", [(20, 10, 0), (10, 20, -1), (float("nan"), 20, 0)])
def test_invalid_weather_is_rejected(
    fitted: business_hgb.HgbBundle,
    weather: business_hgb.Weather,
) -> None:
    with pytest.raises(ValueError):
        _project(fitted, weather)


def test_no_training_overlap_unknown_ids_or_wrong_source(fitted: business_hgb.HgbBundle) -> None:
    with pytest.raises(ValueError, match="cutoff"):
        _project(fitted, start=date(2021, 12, 31))
    with pytest.raises(ValueError):
        _project(fitted, end=date(2025, 12, 31))
    assert (
        business_hgb.project(
            fitted,
            "Philadelphia",
            "PA",
            "Restaurants",
            "Burgers",
            date(2026, 1, 1),
            date(2026, 1, 7),
            ("a", "unknown"),
        )
        == ()
    )


def test_real_weather_is_used_per_day_even_on_matching_weekdays(
    fitted: business_hgb.HgbBundle,
) -> None:
    from datetime import timedelta

    from sitesense.business_activity import ProjectionResult, daily_projection

    start, end = date(2026, 1, 1), date(2026, 1, 8)
    filters = Filters("Philadelphia", "PA", "Restaurants", start, end, source_category="Pizza")
    observations = {
        "station": {
            start + timedelta(days=i): {"tmin": 10.0, "tmax": 20.0, "pcpn": 10.0 if i == 7 else 0.0}
            for i in range(8)
        }
    }
    rows, values = business_observed.project(
        fitted,
        filters,
        (start, end),
        ("a", "b"),
        {"a": "station", "b": "missing"},
        observations,
    )
    assert len(rows) == 1
    assert rows[0].group.business_id == "a"
    result = ProjectionResult(
        (start, end),
        rows,
        weather_basis="HGB · observed station weather",
        daily_values=values,
    )
    daily = daily_projection(result, "a")
    assert len(daily) == 8
    assert daily[-1]["Projected check-ins"] > daily[0]["Projected check-ins"] + 1
    assert rows[0].period_rate == pytest.approx(sum(row["Projected check-ins"] for row in daily))
    assert daily_projection(result, "b") == []
    # One missing variable makes the total unavailable; never fill it with a scenario or zero.
    del observations["station"][start]["pcpn"]
    assert business_observed.project(
        fitted,
        filters,
        (start, end),
        ("a",),
        {"a": "station"},
        observations,
    ) == ((), ())


def test_bundle_round_trip_and_rejection_before_deserialization(
    fitted: business_hgb.HgbBundle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models.joblib"
    joblib.dump({"models": fitted.models, "normals": fitted.normals}, path)
    manifest = {
        **fitted.manifest,
        "schema_version": 1,
        "status": "complete",
        "model_name": "hgb",
        "grain": "business_id_parent_day",
        "features": business_hgb.FEATURES,
        "serving_policy": business_hgb.POLICY,
        "protocol": business_research.PROTOCOL,
        "code_sha256": business_hgb.code_sha256(),
        "lock_sha256": business_forecast.lock_sha256(),
        "versions": business_hgb.versions(),
        "import_revision": "test-import",
        "final_businesses": 2,
        "payload_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "evaluation_metrics": [
            {**row, "city": "Philadelphia", "state": "PA", "category": "Restaurants"}
            for row in fitted.manifest["evaluation_metrics"]
        ],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    loaded = business_hgb.load_bundle(tmp_path, "test-import")
    assert _project(loaded)[0].period_rate == pytest.approx(_project(fitted)[0].period_rate)
    monkeypatch.setattr(joblib, "load", lambda *args: pytest.fail("Must verify before loading"))
    summary = model_guide.load_summary(tmp_path)
    assert len(summary["scores"]) == 6
    assert {r["Business-days"] for r in summary["scores"]} == {730, 732}
    unequal = json.loads(json.dumps(manifest))
    unequal["evaluation_metrics"][0]["observations"] += 1
    (tmp_path / "manifest.json").write_text(json.dumps(unequal))
    with pytest.raises(ValueError, match="matching evaluation"):
        model_guide.load_summary(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        business_hgb.load_bundle(tmp_path, "changed-import")
    for key, value in (("code_sha256", "stale"), ("versions", {}), ("model_name", "overall_mean")):
        (tmp_path / "manifest.json").write_text(json.dumps({**manifest, key: value}))
        with pytest.raises(ValueError):
            business_hgb.load_bundle(tmp_path, "test-import")
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    path.write_bytes(b"tampered")
    with pytest.raises(ValueError):
        business_hgb.load_bundle(tmp_path, "test-import")
