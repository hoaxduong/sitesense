"""Temporal niche cohorts, calendar exposure, artifact provenance and future projections."""

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from sitesense import niche_forecast as prediction
from sitesense import niche_training as training


@pytest.fixture
def history() -> training.TrainingData:
    businesses = tuple(
        training.TrainingBusiness(
            f"zip-{area}-{index}",
            "Philadelphia",
            "PA",
            area,
            str(19100 + area),
            40.0 + area / 100,
            -75.0,
            ("Restaurants", "Pizza", "Italian"),
            date(2016, 1, 1),
        )
        for area in (1, 2)
        for index in range(5)
    )
    activity = tuple(
        training.TrainingDay(business.business_id, date(year, month, 1), business.area_id)
        for business in businesses
        for year in range(2017, 2022)
        for month in range(1, 13)
    )
    return training.TrainingData(businesses, activity, date(2010, 1, 1), date(2022, 1, 19))


def test_fit_uses_exact_multitag_groups_calendar_zeros_and_training_only_cohorts(
    history: training.TrainingData,
) -> None:
    late = replace(history.businesses[0], business_id="late", first_activity=date(2018, 1, 1))
    substring = tuple(
        replace(
            business, business_id=f"delivery-{index}", categories=("Restaurants", "Pizza Delivery")
        )
        for index, business in enumerate(history.businesses[:4])
    )
    altered = replace(
        history,
        businesses=(*history.businesses, late, *substring),
        activity=(*history.activity, training.TrainingDay("late", date(2019, 1, 1), 10000)),
    )
    early = training.fit(altered, date(2017, 1, 1), date(2019, 12, 31))
    assert len(early.groups) == 4
    assert {group.source_category for group in early.groups} == {"Pizza", "Italian"}
    assert {group.n_businesses for group in early.groups} == {5}
    pizza = next(
        group for group in early.groups if group.area_id == 1 and group.source_category == "Pizza"
    )
    assert pizza.training_checkins == 180
    assert pizza.overall_rate == pytest.approx(180 / (5 * 1095))
    assert pizza.membership_sha256 == prediction.membership_sha256(
        tuple(business.business_id for business in history.businesses[:5])
    )
    later = training.fit(altered, date(2018, 1, 1), date(2020, 12, 31))
    assert (
        next(
            group
            for group in later.groups
            if group.area_id == 1 and group.source_category == "Pizza"
        ).n_businesses
        == 6
    )


def test_fit_rebuilds_support_and_pooled_prior_without_held_out_information(
    history: training.TrainingData,
) -> None:
    baseline = training.fit(history, date(2017, 1, 1), date(2019, 12, 31))
    changed = replace(
        history,
        activity=tuple(
            replace(row, checkins=row.checkins * 100000) if row.day.year >= 2020 else row
            for row in history.activity
        ),
    )
    assert training.fit(changed, baseline.start, baseline.end) == baseline
    scarce = replace(
        history,
        activity=tuple(
            replace(row, checkins=0) if row.business_id.startswith("zip-1") else row
            for row in history.activity
        ),
    )
    assert {
        group.area_id for group in training.fit(scarce, baseline.start, baseline.end).groups
    } == {2}


def test_evaluation_retains_all_zero_days_and_independent_zip_ranking_comparison(
    history: training.TrainingData,
) -> None:
    fitted = training.fit(history, date(2017, 1, 1), date(2019, 12, 31))
    zero_holdout = replace(
        history, activity=tuple(row for row in history.activity if row.day.year < 2020)
    )
    measured = training.evaluate(zero_holdout, fitted, 2020, "overall_mean")
    assert measured["groups"] == 4
    assert measured["observations"] == 4 * 366
    mean_prediction = sum(group.overall_rate for group in fitted.groups) / 4
    assert measured["mae"] == pytest.approx(mean_prediction)
    assert measured["poisson_deviance"] == pytest.approx(2 * mean_prediction)
    assert measured["ranked_niches"] == 2
    assert 0 <= measured["top_zip_agreement"] <= 1


def test_train_selects_only_on_2020_then_refits_2019_2021_and_records_provenance(
    history: training.TrainingData,
) -> None:
    groups, manifest = training.train(history, "import-one")
    changed_assessment = replace(
        history,
        activity=tuple(
            replace(row, checkins=10000) if row.day.year == 2021 else row
            for row in history.activity
        ),
    )
    _, changed = training.train(changed_assessment, "import-one")
    assert manifest["model_name"] == changed["model_name"]
    assert manifest["validation_metrics"] == changed["validation_metrics"]
    assert manifest["assessment_metrics"] != changed["assessment_metrics"]
    assert manifest["source_sha256"] != changed["source_sha256"]
    assert manifest["training_start"] == "2019-01-01"
    assert manifest["training_end"] == "2021-12-31"
    assert manifest["validation_training_start"] == "2017-01-01"
    assert manifest["assessment_training_start"] == "2018-01-01"
    assert manifest["source_categories_overlap"] is True
    assert manifest["weather_features"] is False
    assert (
        manifest["metric_weighting"]
        == "equal_group_calendar_days; counts_divided_by_fixed_cohort_businesses"
    )
    assert {group.n_businesses for group in groups} == {5}
    for group in groups:
        assert group.overall_rate == pytest.approx(group.training_checkins / (5 * 1096))


@pytest.fixture
def bundle_dir(history: training.TrainingData, tmp_path: Path) -> Path:
    groups, manifest = training.train(history, "import-one")
    training.write_bundle(tmp_path, groups, manifest)
    return tmp_path


def test_bundle_roundtrip_and_exact_scope_calendar_projection(bundle_dir: Path) -> None:
    bundle = prediction.load_bundle(bundle_dir, "import-one")
    rows = prediction.project(
        bundle, "Philadelphia", "PA", "Restaurants", "Pizza", date(2028, 2, 1), date(2028, 2, 29)
    )
    assert len(rows) == 2
    assert all(row.calendar_days == 29 for row in rows)
    assert all(row.group.source_category == "Pizza" for row in rows)
    assert all(row.period_rate == pytest.approx(row.daily_rate * 29) for row in rows)
    assert rows[0].daily_rate >= rows[1].daily_rate
    assert (
        prediction.project(
            bundle,
            "Philadelphia",
            "PA",
            "Restaurants",
            "Pizza Delivery",
            date(2028, 2, 1),
            date(2028, 2, 29),
        )
        == ()
    )
    assert (
        prediction.project(
            bundle, "Nashville", "TN", "Restaurants", None, date(2028, 2, 1), date(2028, 2, 29)
        )
        == ()
    )
    assert (
        len(
            prediction.project(
                bundle,
                "Philadelphia",
                "PA",
                "Restaurants",
                None,
                date(2028, 2, 1),
                date(2028, 2, 29),
            )
        )
        == 4
    )


@pytest.mark.parametrize(
    "start,end",
    [
        (date(2021, 12, 31), date(2022, 1, 2)),
        (date(2026, 1, 2), date(2026, 1, 1)),
        (date(2026, 1, 1), date(2037, 1, 1)),
    ],
)
def test_projection_rejects_in_sample_reverse_and_excessive_intervals(
    bundle_dir: Path,
    start: date,
    end: date,
) -> None:
    with pytest.raises(ValueError):
        prediction.project(
            prediction.load_bundle(bundle_dir, "import-one"),
            "Philadelphia",
            "PA",
            "Restaurants",
            None,
            start,
            end,
        )


@pytest.mark.parametrize("kind", ["import", "code", "lock", "payload"])
def test_bundle_rejects_stale_import_code_lock_or_payload(
    bundle_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    revision = "different-import" if kind == "import" else "import-one"
    if kind == "code":
        monkeypatch.setattr(prediction, "code_sha256", lambda: "f" * 64)
    if kind == "lock":
        monkeypatch.setattr(prediction, "lock_sha256", lambda: "f" * 64)
    if kind == "payload":
        with (bundle_dir / "groups.json").open("ab") as payload:
            payload.write(b" ")
    with pytest.raises(prediction.NicheForecastError, match="unavailable"):
        prediction.load_bundle(bundle_dir, revision)


@pytest.mark.parametrize("kind", ["duplicate", "nonfinite", "unsupported", "exposure", "empty"])
def test_bundle_rejects_invalid_groups_even_with_updated_payload_hash(
    bundle_dir: Path, kind: str
) -> None:
    path = bundle_dir / "groups.json"
    groups = json.loads(path.read_bytes())
    if kind == "duplicate":
        groups.append(groups[0])
    elif kind == "nonfinite":
        groups[0]["rates"][0] = float("nan")
    elif kind == "unsupported":
        groups[0]["n_businesses"] = 4
    elif kind == "exposure":
        groups[0]["overall_rate"] *= 2
    else:
        groups = []
    payload = json.dumps(groups).encode()
    path.write_bytes(payload)
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    manifest["final_groups"] = len(groups)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(prediction.NicheForecastError):
        prediction.load_bundle(bundle_dir, "import-one")


@pytest.mark.parametrize(
    "field,value",
    [
        ("validation_training_start", "2018-01-01"),
        ("assessment_training_end", "2021-12-31"),
        ("coverage_end", "2020-12-31"),
        ("metric_weighting", "business_weighted"),
        ("weather_features", True),
    ],
)
def test_bundle_rejects_changed_training_and_evaluation_contract(
    bundle_dir: Path,
    field: str,
    value: object,
) -> None:
    path = bundle_dir / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(prediction.NicheForecastError):
        prediction.load_bundle(bundle_dir, "import-one")


def test_training_rejects_uncovered_history_duplicates_and_negative_counts(
    history: training.TrainingData,
) -> None:
    for altered in (
        replace(history, coverage_start=date(2018, 1, 1)),
        replace(history, activity=(*history.activity, history.activity[0])),
        replace(history, activity=(replace(history.activity[0], checkins=-1),)),
    ):
        with pytest.raises(ValueError):
            training.fit(altered, date(2017, 1, 1), date(2019, 12, 31))


def test_database_revision_change_prevents_writing_artifact(
    history: training.TrainingData,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revisions = iter(("before", "after"))
    monkeypatch.setattr(training.repository, "get_import_revision", lambda: next(revisions))
    monkeypatch.setattr(training, "load_training_data", lambda: history)
    with pytest.raises(ValueError, match="changed"):
        training.train_from_database(tmp_path)
    assert not (tmp_path / "manifest.json").exists()
