"""Business-level experiment invariants independent of a local database."""

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from sitesense import business_research as research
from sitesense.business_training import TrainingBusiness, TrainingData, TrainingDay
from sitesense.models import Business, Catalog, DateCoverage
from sitesense.repository import _StationReport


def sources() -> research.ResearchSources:
    businesses = (
        TrainingBusiness(
            "pizza-a",
            "A",
            "Philadelphia",
            "PA",
            40.0,
            -75.0,
            ("Restaurants", "Pizza", "Italian"),
            date(2016, 1, 1),
        ),
        TrainingBusiness(
            "pizza-b",
            "B",
            "Philadelphia",
            "PA",
            None,
            None,
            ("Restaurants", "Pizza"),
            date(2016, 1, 1),
        ),
        TrainingBusiness(
            "other",
            "C",
            "Philadelphia",
            "PA",
            40.0,
            -75.0,
            ("Restaurants", "Burgers"),
            date(2016, 1, 1),
        ),
        TrainingBusiness(
            "future",
            "Future",
            "Philadelphia",
            "PA",
            40.0,
            -75.0,
            ("Restaurants", "Pizza"),
            date(2018, 1, 1),
        ),
        TrainingBusiness(
            "unsupported",
            "Small",
            "Philadelphia",
            "PA",
            40.0,
            -75.0,
            ("Restaurants", "Pizza"),
            date(2016, 1, 1),
        ),
    )
    activity = tuple(
        TrainingDay(identifier, date(year, 1, 1), count)
        for year in range(2017, 2022)
        for identifier, count in (
            ("pizza-a", 40),
            ("pizza-b", 20),
            ("other", 100),
            ("future", 1000 if year >= 2018 else 0),
            ("unsupported", 1),
        )
    )
    catalog_businesses = tuple(
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
            4.5,
            1000,
            True,
            "acis_test",
        )
        for b in businesses
    )
    weather = pd.DataFrame(
        {
            "weather_cell_id": "acis_test",
            "day": pd.date_range("2017-01-01", "2021-12-31").date,
            "tmin_c": 5.0,
            "tmax_c": 15.0,
            "precipitation_mm": 0.0,
        }
    )
    # Missing weather is distinct from a zero count. Remove one held-out day.
    weather = weather.loc[weather.day != date(2020, 2, 1)].reset_index(drop=True)
    data = TrainingData(businesses, activity, date(2017, 1, 1), date(2021, 12, 31))
    catalog = Catalog(
        (), catalog_businesses, (), DateCoverage(data.coverage_start, data.coverage_end)
    )
    return research.ResearchSources(data, catalog, weather, "fixed-import", "raw-weather-hash")


def test_cohort_uses_only_fit_history_and_exact_source_tag() -> None:
    data = sources()
    config = research.ExperimentConfig()
    groups, eligible = research.select_groups(data, config, date(2017, 1, 1), date(2019, 12, 31))
    assert eligible == 2
    assert {g.business_id for g in groups} == {"pizza-a", "pizza-b"}
    changed = replace(
        data,
        training=replace(
            data.training,
            activity=tuple(
                replace(r, checkins=10_000_000) if r.day.year >= 2020 else r
                for r in data.training.activity
            ),
        ),
    )
    assert research.select_groups(changed, config, date(2017, 1, 1), date(2019, 12, 31)) == (
        groups,
        eligible,
    )
    # Rebuild support at the later fit start, rather than holding the 2017 cohort forever.
    later, _ = research.select_groups(data, config, date(2018, 1, 1), date(2020, 12, 31))
    assert "future" in {g.business_id for g in later}
    with pytest.raises(ValueError, match="No businesses"):
        research.select_groups(
            data, replace(config, source_category="Pizz"), date(2017, 1, 1), date(2019, 12, 31)
        )


def test_panel_units_coverage_and_missing_weather_are_preserved() -> None:
    data = sources()
    groups, _ = research.select_groups(
        data, research.ExperimentConfig(), date(2017, 1, 1), date(2019, 12, 31)
    )
    panel = research.build_panel(data, groups, date(2020, 1, 1), date(2020, 12, 31))
    assert len(panel) == 2 * 366
    assert not panel.duplicated(["business_id", "day"]).any()
    assert panel.recorded_checkins.sum() == 60
    assert panel.recorded_checkins.dtype.kind in "iu"
    assert len(panel.loc[~panel.weather_available]) == 2
    assert panel.loc[~panel.weather_available, "recorded_checkins"].eq(0).all()
    assert panel.loc[~panel.weather_available, "precipitation_mm"].isna().all()
    with pytest.raises(ValueError, match="outside declared"):
        research.build_panel(data, groups, date(2021, 12, 31), date(2022, 1, 1))
    duplicate = replace(
        data,
        training=replace(
            data.training, activity=(*data.training.activity, data.training.activity[0])
        ),
    )
    with pytest.raises(ValueError, match="Duplicate"):
        research.build_panel(duplicate, groups, date(2017, 1, 1), date(2017, 1, 2))


def test_full_parent_pool_is_not_replaced_by_source_sample() -> None:
    data = sources()
    full, _ = research.select_groups(
        data, research.ExperimentConfig(), date(2017, 1, 1), date(2019, 12, 31)
    )
    sampled, count = research.select_groups(
        data, research.ExperimentConfig(max_businesses=1), date(2017, 1, 1), date(2019, 12, 31)
    )
    assert count == 2 and len(sampled) == 1
    matching = next(g for g in full if g.business_id == sampled[0].business_id)
    assert sampled[0].rates == matching.rates
    assert sampled[0].training_checkins == matching.training_checkins
    with pytest.raises(ValueError, match="128"):
        research.ExperimentConfig(max_businesses=256)


def test_accepted_weather_applies_units_flags_and_coherence() -> None:
    def report(
        day: int, variable: str, value: float, unit: str, flag: str = "", trace: bool = False
    ) -> _StationReport:
        return _StationReport(
            "acis_test", date(2020, 1, day), variable, value, unit, str(value), flag, trace
        )

    reports = [
        report(1, "tmin", 32, "degreeF"),
        report(1, "tmax", 50, "degreeF"),
        report(1, "pcpn", 1, "inch"),
        report(2, "tmin", 32, "degreeF"),
        report(2, "tmax", 50, "degreeF"),
        report(2, "pcpn", 0, "inch", "T", True),
        report(3, "tmin", 60, "degreeF"),
        report(3, "tmax", 50, "degreeF"),
        report(3, "pcpn", 0, "inch"),
        report(4, "tmin", 32, "degreeF", "X"),
        report(4, "tmax", 50, "degreeF"),
        report(4, "pcpn", 0, "inch"),
    ]
    weather = research.accepted_weather(reports)
    assert list(weather.day) == [date(2020, 1, 1), date(2020, 1, 2)]
    assert weather.iloc[0].tmin_c == 0
    assert weather.iloc[0].tmax_c == 10
    assert weather.iloc[0].precipitation_mm == 25.4
    assert weather.iloc[1].precipitation_mm == 0


def test_weather_candidates_share_keys_and_calendar_covers_missing_weather() -> None:
    data = sources()
    groups, _ = research.select_groups(
        data, research.ExperimentConfig(), date(2017, 1, 1), date(2019, 12, 31)
    )
    train = research.build_panel(data, groups, date(2017, 1, 1), date(2019, 12, 31))
    assessment = research.build_panel(data, groups, date(2020, 1, 1), date(2020, 12, 31))
    models = {n: research.fit_model(n, groups, train) for n in ("overall_mean", "poisson", "hgb")}
    metrics, predictions, business_metrics, _ = research._assess("validation", models, assessment)
    matched = predictions.query("population == 'accepted_weather'")
    keys = {
        name: set(zip(frame.business_id, frame.day, strict=True))
        for name, frame in matched.groupby("model")
    }
    assert keys["overall_mean"] == keys["poisson"] == keys["hgb"]
    assert metrics.query("population == 'all_covered'").observations.tolist() == [732]
    assert metrics.query("population == 'accepted_weather'").observations.eq(730).all()
    assert business_metrics.business_id.nunique() == 2
    unknown = assessment.head(1).assign(business_id="new-venue")
    with pytest.raises(ValueError, match="existing fitted"):
        research.predict(models["hgb"], unknown)
    with pytest.raises(ValueError, match="complete accepted"):
        research.predict(models["hgb"], assessment)
    encoder = (
        models["poisson"].estimator.named_steps["features"].named_transformers_["identity_calendar"]
    )
    assert set(encoder.categories_[0]) == {"pizza-a", "pizza-b"}


def test_round_trip_verifies_payload_source_parent_and_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = sources()
    monkeypatch.setattr(research, "load_sources", lambda config: data)
    run = research.run_experiment(
        research.ExperimentConfig(), ["overall_mean", "month_weekday_shrunk"], tmp_path
    )
    loaded = research.load_run(run.directory)
    assert loaded.manifest["selection_frozen_before_test"] is True
    assert research.latest_run(tmp_path) == run.directory
    assert loaded.manifest["stages"]["validation"]["sampled_businesses"] == 2
    assert loaded.manifest["stages"]["test"]["sampled_businesses"] == 3
    ranking = research.recommend(run.directory, "2022-01-01", model="overall_mean")
    assert set(ranking.business_id) == {"pizza-a", "pizza-b", "future"}
    assert ranking.weather_basis.eq("calendar_only").all()
    with pytest.raises(ValueError, match="after the final training cutoff"):
        research.recommend(run.directory, "2021-01-01")
    child = research.run_experiment(
        research.ExperimentConfig(),
        ["overall_mean", "month_weekday_shrunk"],
        tmp_path / "comparison",
        parent_run=run.directory,
    )
    assert research.load_run(child.directory).manifest["parent_run"]["directory"] == str(
        run.directory
    )
    changed = replace(data, import_revision="different-import")
    monkeypatch.setattr(research, "load_sources", lambda config: changed)
    with pytest.raises(ValueError, match="source/catalog changed"):
        research.load_run(run.directory)
    monkeypatch.setattr(research, "load_sources", lambda config: data)
    payload = run.directory / "metrics.csv"
    payload.write_text(payload.read_text() + "changed")
    with pytest.raises(ValueError, match="payload changed"):
        research.load_run(run.directory)


def test_load_rejects_incomplete_manifest_before_estimator_loading(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps({"status": "pending"}))
    with pytest.raises(ValueError, match="complete compatible"):
        research.load_run(tmp_path, verify_source=False)


def test_weather_inference_requires_explicit_complete_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = sources()
    monkeypatch.setattr(research, "load_sources", lambda config: data)
    monkeypatch.setattr(research.repository, "load_catalog", lambda *args: data.catalog)
    monkeypatch.setattr(research.repository, "_load_station_reports", lambda *args: ())
    run = research.run_experiment(
        research.ExperimentConfig(), ["overall_mean", "poisson"], tmp_path
    )
    with pytest.raises(ValueError, match="Complete coherent weather"):
        research.recommend(run.directory, "2022-07-01", model="poisson")
    with pytest.raises(ValueError, match="exactly"):
        research.recommend(run.directory, "2022-07-01", model="poisson", weather={"tmax_c": 30})
    with pytest.raises(ValueError, match="Complete coherent weather"):
        research.recommend(
            run.directory,
            "2022-07-01",
            model="poisson",
            weather={"tmin_c": 40, "tmax_c": 30, "precipitation_mm": 0},
        )
    scenario = {"tmin_c": 20.0, "tmax_c": 30.0, "precipitation_mm": 0.0}
    ranking = research.recommend(run.directory, "2022-07-01", model="poisson", weather=scenario)
    assert len(ranking) == run.manifest["stages"]["final"]["sampled_businesses"]
    assert ranking.predicted_checkins.gt(0).all()
    assert ranking.weather_basis.eq("explicit_weather_scenario").all()
    assert ranking.business_id.is_unique
    assert "unsupported" not in set(ranking.business_id)
    # Code/lock compatibility is checked before a serialized estimator is deserialized.
    monkeypatch.setattr(research, "_code_sha", lambda: "changed-code")
    with pytest.raises(ValueError, match="code or lockfile changed"):
        research.load_models(run.directory)
