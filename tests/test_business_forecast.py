"""Individual grain, independent temporal cohorts and fail-closed JSON provenance."""

import hashlib
import json
import math
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from sitesense import business_forecast as prediction
from sitesense import business_training as training


@pytest.fixture
def history() -> training.TrainingData:
    businesses = tuple(
        training.TrainingBusiness(
            f"business-{index}",
            f"Business {index}",
            "Philadelphia",
            "PA",
            40.0 + index / 100,
            -75.0,
            ("Restaurants", "Pizza", "Italian"),
            date(2016, 1, 1),
        )
        for index in (1, 2)
    )
    activity = tuple(
        training.TrainingDay(business.business_id, date(year, month, 1), index)
        for index, business in enumerate(businesses, 1)
        for year in range(2017, 2022)
        for month in range(1, 13)
    )
    return training.TrainingData(businesses, activity, date(2010, 1, 1), date(2022, 1, 19))


def test_fit_targets_one_business_once_across_source_tags_and_uses_actual_coordinates(
    history: training.TrainingData,
) -> None:
    fitted = training.fit(history, date(2017, 1, 1), date(2019, 12, 31))
    assert len(fitted.groups) == 2
    first, second = fitted.groups
    assert first.business_id == "business-1"
    assert first.source_categories == ("Italian", "Pizza")
    assert first.training_checkins == 36
    assert first.active_training_days == 36
    assert first.overall_rate == pytest.approx(36 / 1095)
    assert second.overall_rate == pytest.approx(72 / 1095)
    assert first.latitude == history.businesses[0].latitude
    assert not hasattr(first, "postal_code")


def test_training_has_no_minimum_number_of_businesses_gate(history: training.TrainingData) -> None:
    single = replace(
        history,
        businesses=history.businesses[:1],
        activity=tuple(row for row in history.activity if row.business_id == "business-1"),
    )
    assert len(training.fit(single, date(2017, 1, 1), date(2019, 12, 31)).groups) == 1


def test_support_and_first_activity_are_rebuilt_for_each_fit_window(
    history: training.TrainingData,
) -> None:
    late = replace(history.businesses[0], business_id="late", first_activity=date(2018, 1, 1))
    scarce = replace(history.businesses[0], business_id="scarce")
    altered = replace(
        history,
        businesses=(*history.businesses, late, scarce),
        activity=(
            *history.activity,
            training.TrainingDay("late", date(2019, 1, 1), 1000),
            training.TrainingDay("scarce", date(2019, 1, 1), 19),
        ),
    )
    assert len(training.fit(altered, date(2017, 1, 1), date(2019, 12, 31)).groups) == 2
    later = training.fit(altered, date(2018, 1, 1), date(2020, 12, 31))
    assert {row.business_id for row in later.groups} == {"business-1", "business-2", "late"}
    supported = replace(
        altered,
        activity=(*altered.activity, training.TrainingDay("scarce", date(2020, 2, 2), 1)),
    )
    assert "scarce" in {
        row.business_id for row in training.fit(supported, later.start, later.end).groups
    }


def test_future_activity_cannot_change_training_cohort_support_or_parent_prior(
    history: training.TrainingData,
) -> None:
    fitted = training.fit(history, date(2017, 1, 1), date(2019, 12, 31))
    altered = replace(
        history,
        activity=tuple(
            replace(row, checkins=100000) if row.day.year >= 2020 else row
            for row in history.activity
        ),
    )
    assert training.fit(altered, fitted.start, fitted.end) == fitted


def test_parent_prior_deduplicates_multitag_businesses(history: training.TrainingData) -> None:
    fitted = training.fit(history, date(2017, 1, 1), date(2019, 12, 31))
    altered = replace(
        history,
        businesses=(
            replace(history.businesses[0], categories=("Restaurants", "Pizza")),
            history.businesses[1],
        ),
    )
    changed = training.fit(altered, fitted.start, fitted.end)
    assert [row.rates for row in changed.groups] == [row.rates for row in fitted.groups]
    cell = (date(2017, 1, 1).month - 1) * 7 + date(2017, 1, 1).weekday()
    exposure = sum(
        day.month == 1 and day.weekday() == 6 for day in training._days(fitted.start, fitted.end)
    )
    own_count = sum(
        row.checkins
        for row in history.activity
        if fitted.start <= row.day <= fitted.end
        and row.business_id == "business-1"
        and row.day.month == 1
        and row.day.weekday() == 6
    )
    assert fitted.groups[0].rates[cell] == pytest.approx(
        (own_count + 30 * (own_count * 3) / (2 * exposure)) / (exposure + 30)
    )


def test_evaluation_matches_dense_score_and_retains_all_calendar_zeros(
    history: training.TrainingData,
) -> None:
    fitted = training.fit(history, date(2017, 1, 1), date(2019, 12, 31))
    for model in prediction.MODEL_NAMES:
        measured = training.evaluate(history, fitted, 2020, model)
        deviance = absolute_error = 0.0
        counts = {(row.business_id, row.day): row.checkins for row in history.activity}
        for group in fitted.groups:
            for offset in range(366):
                day = date(2020, 1, 1) + timedelta(days=offset)
                observed = counts.get((group.business_id, day), 0)
                predicted = (
                    group.overall_rate
                    if model == "overall_mean"
                    else group.rates[(day.month - 1) * 7 + day.weekday()]
                )
                mu = max(predicted, 1e-12)
                deviance += 2 * (
                    (observed * math.log(observed / mu) if observed else 0) - observed + mu
                )
                absolute_error += abs(observed - predicted)
        assert measured["observations"] == 2 * 366
        assert measured["poisson_deviance"] == pytest.approx(deviance / (2 * 366))
        assert measured["mae"] == pytest.approx(absolute_error / (2 * 366))
        assert measured["ranked_niches"] == 2
        assert measured["ranked_businesses"] == 4  # Two overlapping source comparisons.
        assert measured["top_business_agreement"] == 1.0


def test_zero_holdout_scores_fixed_cohort_instead_of_dropping_inactive_businesses(
    history: training.TrainingData,
) -> None:
    fitted = training.fit(history, date(2017, 1, 1), date(2019, 12, 31))
    zero = replace(history, activity=tuple(row for row in history.activity if row.day.year < 2020))
    measured = training.evaluate(zero, fitted, 2020, "overall_mean")
    expected = sum(row.overall_rate for row in fitted.groups) / 2
    assert measured["businesses"] == 2
    assert measured["mae"] == pytest.approx(expected)
    assert measured["poisson_deviance"] == pytest.approx(2 * expected)


def test_train_selects_only_validation_and_records_independent_cohorts_and_exposure(
    history: training.TrainingData,
) -> None:
    groups, manifest = training.train(history, "import-one")
    changed = replace(
        history,
        activity=tuple(
            replace(row, checkins=10000) if row.day.year == 2021 else row
            for row in history.activity
        ),
    )
    _, altered = training.train(changed, "import-one")
    assert manifest["model_name"] == altered["model_name"]
    assert manifest["validation_metrics"] == altered["validation_metrics"]
    assert manifest["assessment_metrics"] != altered["assessment_metrics"]
    assert manifest["source_sha256"] != altered["source_sha256"]
    assert manifest["grain"] == "business_id_parent_day"
    assert manifest["pool_rule"] == prediction.POOL_RULE
    assert manifest["cohort_rule"] == prediction.COHORT_RULE
    assert manifest["ranking_scope"] == prediction.RANK_SCOPE
    assert manifest["final_businesses"] == 2
    assert manifest["weather_features"] is False
    assert all(row.overall_rate == pytest.approx(row.training_checkins / 1096) for row in groups)


@pytest.fixture
def bundle_dir(history: training.TrainingData, tmp_path: Path) -> Path:
    groups, manifest = training.train(history, "import-one")
    training.write_bundle(tmp_path, groups, manifest)
    return tmp_path


def test_bundle_roundtrip_exact_scope_and_source_filters_preserve_same_individual_rate(
    bundle_dir: Path,
) -> None:
    bundle = prediction.load_bundle(bundle_dir, "import-one")

    def scoped(source: str | None) -> tuple[prediction.BusinessProjection, ...]:
        return prediction.project(
            bundle,
            "Philadelphia",
            "PA",
            "Restaurants",
            source,
            date(2028, 2, 1),
            date(2028, 2, 29),
        )

    rows = scoped(None)
    assert len(rows) == 2
    assert rows == scoped("Pizza") == scoped("Italian")
    assert scoped("Pizza Delivery") == ()
    assert rows[0].group.business_id == "business-2"
    assert all(row.calendar_days == 29 for row in rows)
    assert all(row.period_rate == pytest.approx(row.daily_rate * 29) for row in rows)
    assert (
        prediction.project(
            bundle, "Nashville", "TN", "Restaurants", None, date(2028, 1, 1), date(2028, 1, 1)
        )
        == ()
    )
    assert (
        prediction.project(
            bundle, "Philadelphia", "PA", "Day Spas", None, date(2028, 1, 1), date(2028, 1, 1)
        )
        == ()
    )


@pytest.mark.parametrize(
    "start,end",
    [
        (date(2021, 12, 31), date(2022, 1, 1)),
        (date(2026, 1, 2), date(2026, 1, 1)),
        (date(2026, 1, 1), date(2037, 1, 1)),
    ],
)
def test_projection_rejects_nonfuture_reverse_and_excessive_ranges(
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
def test_bundle_fails_closed_for_changed_provenance(
    bundle_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    if kind == "code":
        monkeypatch.setattr(prediction, "code_sha256", lambda: "f" * 64)
    if kind == "lock":
        monkeypatch.setattr(prediction, "lock_sha256", lambda: "f" * 64)
    if kind == "payload":
        with (bundle_dir / "businesses.json").open("ab") as payload:
            payload.write(b" ")
    with pytest.raises(prediction.BusinessForecastError):
        prediction.load_bundle(bundle_dir, "changed" if kind == "import" else "import-one")


@pytest.mark.parametrize(
    "kind",
    [
        "duplicate",
        "nonfinite",
        "support",
        "exposure",
        "empty",
        "source_string",
        "duplicate_source",
        "bad_coordinate",
        "active_days",
    ],
)
def test_bundle_rejects_invalid_businesses_even_when_payload_is_rehashed(
    bundle_dir: Path,
    kind: str,
) -> None:
    path = bundle_dir / "businesses.json"
    rows = json.loads(path.read_bytes())
    if kind == "duplicate":
        rows.append(rows[0])
    elif kind == "nonfinite":
        rows[0]["rates"][0] = float("nan")
    elif kind == "support":
        rows[0]["training_checkins"] = 19
    elif kind == "exposure":
        rows[0]["overall_rate"] *= 2
    elif kind == "source_string":
        rows[0]["source_categories"] = "Pizza"
    elif kind == "duplicate_source":
        rows[0]["source_categories"] = ["Pizza", "Pizza"]
    elif kind == "bad_coordinate":
        rows[0]["latitude"] = 91
    elif kind == "active_days":
        rows[0]["active_training_days"] = 1097
    else:
        rows = []
    payload = json.dumps(rows).encode()
    path.write_bytes(payload)
    manifest_path = bundle_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["payload_sha256"] = hashlib.sha256(payload).hexdigest()
    manifest["final_businesses"] = len(rows)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(prediction.BusinessForecastError):
        prediction.load_bundle(bundle_dir, "import-one")


@pytest.mark.parametrize(
    "field,value",
    [
        ("validation_training_start", "2018-01-01"),
        ("assessment_training_end", "2021-12-31"),
        ("coverage_end", "2020-12-31"),
        ("weather_features", True),
        ("grain", "zip_day"),
        ("pool_rule", "source_duplicated"),
        ("cohort_rule", "currently_open"),
    ],
)
def test_bundle_rejects_changed_evaluation_and_target_contract(
    bundle_dir: Path,
    field: str,
    value: object,
) -> None:
    path = bundle_dir / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest[field] = value
    path.write_text(json.dumps(manifest))
    with pytest.raises(prediction.BusinessForecastError):
        prediction.load_bundle(bundle_dir, "import-one")


def test_fit_rejects_invalid_counts_duplicates_and_incomplete_coverage(
    history: training.TrainingData,
) -> None:
    cases = [
        replace(history, coverage_start=date(2018, 1, 1)),
        replace(history, businesses=(*history.businesses, history.businesses[0])),
        replace(history, activity=(*history.activity, history.activity[0])),
        replace(
            history,
            activity=(*history.activity, training.TrainingDay("unknown", date(2019, 1, 1), 1)),
        ),
        replace(history, activity=(replace(history.activity[0], checkins=-1),)),
    ]
    for altered in cases:
        with pytest.raises(ValueError):
            training.fit(altered, date(2017, 1, 1), date(2019, 12, 31))
