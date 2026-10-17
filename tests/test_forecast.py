"""Artifact-backed forecasts use only explicit weather/location/calendar inputs."""

import hashlib
import json
from dataclasses import replace
from datetime import date
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
import pytest
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from sitesense.forecast import (
    FEATURES,
    MODEL_DIR,
    ArtifactUnavailableError,
    ForecastBundle,
    load_bundle,
    predict_locations,
)


def test_default_artifact_directory_is_project_working_directory() -> None:
    assert MODEL_DIR == Path.cwd() / "models" / "location_forecast"


@pytest.fixture
def artifact_directory(tmp_path: Path) -> Path:
    locations = pd.DataFrame(
        [
            {
                "group_id": group,
                "area_id": f"area-{group}",
                "metro": "Tampa",
                "city": "Tampa",
                "state": "FL",
                "zip": f"0000{index}",
                "category": "Restaurants",
                "latitude": 27.95,
                "longitude": -82.46,
                "station_id": station,
            }
            for index, (group, station) in enumerate(
                [("g3", "station-a"), ("g1", "station-a"), ("g2", "station-b")], start=1
            )
        ]
    )
    frame = pd.concat([locations] * 12, ignore_index=True)
    frame["calendar_month"] = np.repeat(range(1, 13), len(locations))
    frame["day_of_week"] = np.arange(len(frame)) % 7
    frame["tmax_c"] = 28.0
    frame["tmin_c"] = 20.0
    frame["precip_mm"] = 0.0
    categorical = ["metro", "category", "calendar_month", "day_of_week"]
    model = Pipeline(
        [
            (
                "preprocess",
                ColumnTransformer(
                    [
                        (
                            "categorical",
                            OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                            categorical,
                        ),
                        (
                            "numeric",
                            "passthrough",
                            [name for name in FEATURES if name not in categorical],
                        ),
                    ]
                ),
            ),
            (
                "model",
                HistGradientBoostingRegressor(
                    loss="poisson",
                    max_iter=3,
                    min_samples_leaf=2,
                    early_stopping=False,
                    random_state=42,
                ),
            ),
        ]
    )
    model.fit(frame[FEATURES], np.full(len(frame), 0.3))
    joblib.dump(model, tmp_path / "model.joblib")
    locations.to_csv(tmp_path / "locations.csv", index=False)
    pd.DataFrame(
        [
            {
                "station_id": "station-a",
                "date": "2026-09-01",
                "tmax_c": 28,
                "tmin_c": 20,
                "precip_mm": 0,
                "complete_weather": True,
            },
            {
                "station_id": "station-b",
                "date": "2026-09-01",
                "tmax_c": 28,
                "tmin_c": 20,
                "precip_mm": np.nan,
                "complete_weather": False,
            },
        ]
    ).to_csv(tmp_path / "weather_daily.csv", index=False)
    baseline = frame[["area_id", "category", "calendar_month"]].copy()
    baseline["mean_rate"] = 0.25
    baseline.to_csv(tmp_path / "baseline.csv", index=False)
    manifest = {
        "schema_version": 1,
        "status": "complete",
        "model_name": "hgb_weather",
        "features": FEATURES,
        "versions": {
            name: version(package)
            for name, package in {
                "sklearn": "scikit-learn",
                "numpy": "numpy",
                "pandas": "pandas",
                "joblib": "joblib",
            }.items()
        },
        "trained_start": "2010-01-01",
        "trained_end": "2021-12-31",
        "weather_range": {
            "tmax_c": {"min": 10, "max": 35},
            "tmin_c": {"min": 0, "max": 25},
            "precip_mm": {"min": 0, "max": 80},
        },
        "artifact_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in tmp_path.iterdir()
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return tmp_path


def test_loads_trusted_verified_artifacts_and_preserves_zip(artifact_directory: Path) -> None:
    bundle = load_bundle(artifact_directory)
    assert bundle.locations["zip"].tolist() == ["00001", "00002", "00003"]
    assert bundle.weather["date"].tolist() == [date(2026, 9, 1)] * 2


def test_missing_bundle_has_safe_error(tmp_path: Path) -> None:
    with pytest.raises(ArtifactUnavailableError) as error:
        load_bundle(tmp_path)
    assert str(tmp_path) not in str(error.value)


def test_changed_artifact_is_rejected_before_deserialization(
    artifact_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (artifact_directory / "model.joblib").write_bytes(b"changed")
    calls: list[Path] = []
    monkeypatch.setattr(joblib, "load", lambda path: calls.append(path))
    with pytest.raises(ArtifactUnavailableError):
        load_bundle(artifact_directory)
    assert calls == []


def test_replaced_files_after_verification_do_not_change_loaded_bundle(
    artifact_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = load_bundle(artifact_directory)
    replaced_locations = original.locations.assign(city="New generation", zip="99999")
    replaced_frame = original.locations.copy()
    replaced_frame["calendar_month"] = 9
    replaced_frame["day_of_week"] = 1
    replaced_frame["tmax_c"] = 28.0
    replaced_frame["tmin_c"] = 20.0
    replaced_frame["precip_mm"] = 0.0
    new_model = joblib.load(artifact_directory / "model.joblib")
    new_model.fit(replaced_frame[FEATURES], np.full(len(replaced_frame), 0.9))
    model_bytes = BytesIO()
    joblib.dump(new_model, model_bytes)
    read_csv = pd.read_csv
    replaced = False

    def replace_before_parse(*args: Any, **kwargs: Any) -> Any:
        nonlocal replaced
        if not replaced:
            replaced = True
            replaced_locations.to_csv(artifact_directory / "locations.csv", index=False)
            (artifact_directory / "model.joblib").write_bytes(model_bytes.getvalue())
        return read_csv(*args, **kwargs)

    monkeypatch.setattr(pd, "read_csv", replace_before_parse)
    loaded = load_bundle(artifact_directory)
    assert replaced
    pd.testing.assert_frame_equal(loaded.locations, original.locations)
    assert loaded.manifest == original.manifest
    prediction = predict_locations(loaded, date(2026, 9, 1), "Tampa", "Restaurants")
    assert prediction["predicted_rate"].tolist() == pytest.approx([0.3, 0.3])
    assert read_csv(artifact_directory / "locations.csv")["city"].tolist() == ["New generation"] * 3


@pytest.mark.parametrize("change", ["version", "features", "status"])
def test_incompatible_manifest_is_rejected(artifact_directory: Path, change: str) -> None:
    path = artifact_directory / "manifest.json"
    manifest = json.loads(path.read_text())
    if change == "version":
        manifest["versions"]["sklearn"] = "0.0.0"
    elif change == "features":
        manifest["features"].append("checkins")
    else:
        manifest["status"] = "training"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ArtifactUnavailableError):
        load_bundle(artifact_directory)


def test_any_date_manual_weather_uses_only_explicit_features(artifact_directory: Path) -> None:
    bundle = load_bundle(artifact_directory)
    supplied = {"tmax_c": 28.0, "tmin_c": 20.0, "precip_mm": 0.0}
    future = date(2032, 2, 29)
    original = predict_locations(bundle, future, "Tampa", "Restaurants", supplied)
    changed = bundle.locations.assign(checkins=1e12, rate=-999, n_businesses=1e10, lag1=999)
    mutated = predict_locations(
        replace(bundle, locations=changed), future, "Tampa", "Restaurants", supplied
    )
    pd.testing.assert_frame_equal(original, mutated)
    assert original["group_id"].tolist() == ["g1", "g2", "g3"]
    assert original["predicted_rate"].tolist() == pytest.approx([0.3] * 3)
    assert original["baseline_rate"].tolist() == pytest.approx([0.25] * 3)
    assert original["date"].tolist() == [future] * 3
    assert original["day_of_week"].tolist() == [future.weekday()] * 3
    assert original.attrs["weather_source"] == "supplied_weather"
    assert original.attrs["weather_excluded_candidates"] == 0
    assert not {"checkins", "rate", "n_businesses", "lag1"}.intersection(original.columns)


def test_observed_weather_excludes_incomplete_areas_without_fallback(
    artifact_directory: Path,
) -> None:
    bundle = load_bundle(artifact_directory)
    result = predict_locations(bundle, date(2026, 9, 1), "Tampa", "Restaurants")
    assert result["group_id"].tolist() == ["g1", "g3"]
    assert result["precip_mm"].tolist() == [0.0, 0.0]
    assert result.attrs["weather_source"] == "observed_acis"
    assert result.attrs["candidate_count"] == 3
    assert result.attrs["weather_complete_candidates"] == 2
    assert result.attrs["weather_excluded_candidates"] == 1
    assert result["weather_excluded_candidates"].tolist() == [1, 1]
    with pytest.raises(ValueError, match="No complete observed weather"):
        predict_locations(bundle, date(2032, 2, 29), "Tampa", "Restaurants")


@pytest.mark.parametrize(
    "weather",
    [
        {"tmax_c": 25.0, "tmin_c": 20.0},
        {"tmax_c": float("nan"), "tmin_c": 20.0, "precip_mm": 0.0},
        {"tmax_c": 25.0, "tmin_c": float("inf"), "precip_mm": 0.0},
        {"tmax_c": True, "tmin_c": 20.0, "precip_mm": 0.0},
        {"tmax_c": "25", "tmin_c": 20.0, "precip_mm": 0.0},
        {"tmax_c": 25.0, "tmin_c": 26.0, "precip_mm": 0.0},
        {"tmax_c": 25.0, "tmin_c": 20.0, "precip_mm": -1.0},
    ],
)
def test_invalid_weather_is_rejected(artifact_directory: Path, weather: Any) -> None:
    with pytest.raises(ValueError):
        predict_locations(
            load_bundle(artifact_directory), date(2032, 2, 29), "Tampa", "Restaurants", weather
        )


def test_outside_training_weather_is_marked_not_silently_clipped(artifact_directory: Path) -> None:
    result = predict_locations(
        load_bundle(artifact_directory),
        date(2032, 2, 29),
        "Tampa",
        "Restaurants",
        {"tmax_c": 40.0, "tmin_c": 20.0, "precip_mm": 0.0},
    )
    assert result["tmax_c"].tolist() == [40.0] * 3
    assert result["outside_training_weather_range"].tolist() == [True] * 3
    assert result.attrs["outside_training_weather_range_count"] == 3


def test_unknown_location_is_rejected(artifact_directory: Path) -> None:
    with pytest.raises(ValueError, match="No trained locations"):
        predict_locations(
            load_bundle(artifact_directory), date(2026, 9, 1), "Unknown", "Restaurants"
        )


def test_invalid_model_predictions_have_safe_error(
    artifact_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle: ForecastBundle = load_bundle(artifact_directory)
    monkeypatch.setattr(bundle.model, "predict", lambda inputs: np.full(len(inputs), np.nan))
    with pytest.raises(ArtifactUnavailableError):
        predict_locations(bundle, date(2026, 9, 1), "Tampa", "Restaurants")
