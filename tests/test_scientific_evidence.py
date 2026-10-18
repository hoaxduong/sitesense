"""Backtest provenance, temporal separation and physically valid model inspection."""

import csv
import hashlib
import io
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
from test_forecast_cohort import AREA, GROUP  # noqa: I001
from test_forecast_cohort import artifact as artifact

from sitesense import forecast
from sitesense import scientific_evidence as evidence
from sitesense.models import Filters


def _csv(rows: list[dict[str, Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode()


@pytest.fixture
def evaluation(artifact: tuple[Path, Path]) -> tuple[Path, Path]:
    directory, run = artifact
    model = json.loads((directory / "manifest.json").read_text())
    parent = json.loads((run / "manifest.json").read_text())
    weather = _csv(
        [
            dict(
                station_id="station-a",
                date="2021-01-01",
                complete_weather=True,
                tmax_c=5,
                tmin_c=0,
                precip_mm=0,
            )
        ]
    )
    (directory / "weather_daily.csv").write_bytes(weather)
    sha = hashlib.sha256(weather).hexdigest()
    model["artifact_sha256"]["weather_daily.csv"] = sha
    parent["output_sha256"]["artifact/weather_daily.csv"] = sha
    area = dict(
        group_id=GROUP,
        area_id=GROUP.rsplit("|", 1)[0],
        metro="Philadelphia",
        city="philadelphia",
        state="PA",
        zip="19147",
        category="Restaurants",
        n_businesses=2,
        station_id="station-a",
    )
    row = dict(
        group_id=GROUP,
        area_id=area["area_id"],
        metro="Philadelphia",
        category="Restaurants",
        date="2021-01-01",
        n_businesses=2,
        checkins=2,
        rate=1,
        predicted_rate=0.5,
        model="hgb_weather",
        period="test_2021",
    )
    for name, rows in (
        ("evaluation_areas.csv", [area]),
        (
            "evaluation_predictions.csv",
            [
                row,
                dict(row, model="calendar_mean", predicted_rate=20),
                dict(row, date="2020-01-01", period="validation_2020"),
            ],
        ),
    ):
        raw = _csv(rows)
        (run / name).write_bytes(raw)
        parent["output_sha256"][name] = hashlib.sha256(raw).hexdigest()
    parent["evaluation_protocol"] = evidence.PROTOCOL
    parent["features"] = forecast.FEATURES
    (directory / "manifest.json").write_text(json.dumps(model))
    (run / "manifest.json").write_text(json.dumps(parent))
    return directory, run


def _load(directory: Path, period: str = "Test 2021") -> evidence.Evaluation:
    # Future UI dates do not become an evaluation period.
    return evidence.load_evaluation(
        directory,
        Filters("Philadelphia", "PA", "Restaurants", date(2026, 1, 1), date(2026, 1, 7)),
        (AREA,),
        period,
    )


def test_prepared_evidence_preserves_model_and_uses_saved_evaluation_cohort(
    evaluation: tuple[Path, Path],
) -> None:
    directory, run = evaluation
    weights = (directory / "model.joblib").read_bytes()
    evidence.prepare_evaluation(run, directory)
    loaded = _load(directory)
    assert loaded.reason is None
    assert len(loaded.rows) == 1
    row = loaded.rows[0]
    assert row["Recorded"] == 100 and row["Predicted"] == 50 and row["Residual"] == 50
    assert row["Cohort businesses"] == 2
    assert row["Date"] == "2021-01-01" and row["Trained through"] == "2020-12-31"
    assert row["Precipitation mm"] == 0 and row["Weather station"] == "station-a"
    assert len(loaded.parent_sha256) == 64
    assert _load(directory, "Validation 2020").rows[0]["Trained through"] == "2019-12-31"
    assert (directory / "model.joblib").read_bytes() == weights


@pytest.mark.parametrize(
    "target",
    [
        "evaluation/predictions.csv.gz",
        "evaluation/areas.csv",
        "evaluation/manifest.json",
        "cohort/parent_manifest.json",
        "model.joblib",
    ],
)
def test_changed_evidence_or_current_model_is_unavailable(
    evaluation: tuple[Path, Path],
    target: str,
) -> None:
    directory, run = evaluation
    evidence.prepare_evaluation(run, directory)
    before = evidence.revision(directory)
    path = directory / target
    path.write_bytes(path.read_bytes() + b"changed")
    assert evidence.revision(directory) != before
    assert not _load(directory).rows
    assert _load(directory).reason


@pytest.mark.parametrize(
    "field,value",
    [
        ("date", "2019-12-31"),
        ("n_businesses", "3"),
        ("rate", "50"),
        ("predicted_rate", "-1"),
    ],
)
def test_invalid_saved_rows_fail_without_repredicting(
    evaluation: tuple[Path, Path],
    field: str,
    value: str,
) -> None:
    directory, run = evaluation
    path = run / "evaluation_predictions.csv"
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    rows[0][field] = value
    raw = _csv(rows)
    path.write_bytes(raw)
    parent = json.loads((run / "manifest.json").read_text())
    parent["output_sha256"][path.name] = hashlib.sha256(raw).hexdigest()
    (run / "manifest.json").write_text(json.dumps(parent))
    evidence.prepare_evaluation(run, directory)
    assert not _load(directory).rows
    assert _load(directory).reason


def test_original_source_corruption_is_rejected_before_preparing_predictions(
    evaluation: tuple[Path, Path],
) -> None:
    directory, run = evaluation
    path = run / "evaluation_predictions.csv"
    path.write_bytes(path.read_bytes() + b"corrupt")
    with pytest.raises(ValueError):
        evidence.prepare_evaluation(run, directory)
    assert not (directory / "evaluation").exists()


def test_missing_and_wrong_scope_do_not_invent_backtest_observations(
    evaluation: tuple[Path, Path],
) -> None:
    directory, run = evaluation
    assert _load(directory).reason
    evidence.prepare_evaluation(run, directory)
    filters = Filters("Philadelphia", "PA", "Day Spas", date(2021, 1, 1), date(2021, 12, 31))
    assert not evidence.load_evaluation(directory, filters, (AREA,), "Test 2021").rows
    assert not evidence.load_evaluation(
        directory,
        replace(filters, category="Restaurants"),
        (replace(AREA, city="Other"),),
        "Test 2021",
    ).rows


def test_ice_uses_only_features_valid_temperature_support_and_actual_contexts() -> None:
    model = Mock()
    model.predict.side_effect = lambda frame: np.full(len(frame), 0.25)
    weather = pd.DataFrame(
        [
            {
                "station_id": "s",
                "date": date(2021, 1, i),
                "complete_weather": True,
                "tmax_c": float(i + 4),
                "tmin_c": float(i - 2),
                "precip_mm": float(i),
            }
            for i in range(1, 10)
        ]
    )
    locations = pd.DataFrame(
        [
            {
                "metro": "Philadelphia",
                "state": "PA",
                "city": "philadelphia",
                "zip": "19147",
                "category": "Restaurants",
                "station_id": "s",
                "latitude": 40,
                "longitude": -75,
            }
        ]
    )
    bundle = forecast.ForecastBundle(
        model,
        locations,
        weather,
        pd.DataFrame(),
        {
            "trained_start": "2021-01-01",
            "trained_end": "2021-01-09",
        },
    )
    filters = Filters("Philadelphia", "PA", "Restaurants", date(2021, 1, 1), date(2021, 1, 9))
    curves = evidence.response_curves(bundle, filters, (AREA,), "tmax_c")
    inputs = model.predict.call_args.args[0]
    assert list(inputs.columns) == forecast.FEATURES
    assert (inputs["tmax_c"] >= inputs["tmin_c"]).all()
    assert inputs["precip_mm"].min() >= 0
    assert len(curves) == 120  # five 20-point ICE curves plus their 20-point average
    assert all(row["Predicted"] == 25 for row in curves)
    assert {row["Reference days"] for row in curves if row["Kind"] == "Average"} == {5}
    assert not evidence.response_curves(bundle, filters, (replace(AREA, city="Other"),), "tmax_c")
    assert not evidence.response_curves(bundle, filters, (AREA, AREA), "tmax_c")
