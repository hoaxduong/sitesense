"""Portable, verified backtest evidence and sampled model-response curves.

Backtests come from saved forward-time predictions, never from predicting the
final model's training dates. Reference preparation is explicit and read-only
with respect to the model and database.
"""

import csv
import gzip
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from threadpoolctl import threadpool_limits  # type: ignore[import-untyped]

from sitesense import forecast, forecast_cohort
from sitesense.models import CandidateArea, Filters

PROTOCOL = {
    "train_start": "2010-01-01",
    "train_end": "2019-12-31",
    "validation_start": "2020-01-01",
    "validation_end": "2020-12-31",
    "test_refit_end": "2020-12-31",
    "test_start": "2021-01-01",
    "test_end": "2021-12-31",
    "cohort_selection_end": "2019-12-31",
}
PERIODS = {
    "Test 2021": ("test_2021", "2021-01-01", "2021-12-31", "2020-12-31"),
    "Validation 2020": ("validation_2020", "2020-01-01", "2020-12-31", "2019-12-31"),
}


@dataclass(frozen=True)
class Evaluation:
    rows: tuple[dict[str, Any], ...] = ()
    reason: str | None = None
    parent_sha256: str = ""


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def revision(directory: Path) -> str:
    digest = hashlib.sha256(forecast_cohort.artifact_revision(directory).encode())
    try:
        digest.update((directory / "evaluation" / "manifest.json").read_bytes())
        for name in ("predictions.csv.gz", "areas.csv"):
            stat = (directory / "evaluation" / name).stat()
            digest.update(f"{name}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}".encode())
    except OSError:
        digest.update(b"missing-evaluation")
    return digest.hexdigest()


def prepare_evaluation(run: Path, directory: Path) -> Path:
    """Verify an explicit original run, retain only HGB backtest rows, and compress."""
    forecast_cohort.prepare_reference(run, directory)
    parent = forecast_cohort.verified_training_manifest(directory)
    if parent.get("evaluation_protocol") != PROTOCOL or parent.get("features") != forecast.FEATURES:
        raise ValueError("Unsupported evaluation protocol")
    for name in ("evaluation_predictions.csv", "evaluation_areas.csv"):
        if _sha(run / name) != parent["output_sha256"][name]:
            raise ValueError("Backtest source checksum mismatch")
    output = directory / "evaluation"
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / ".predictions.csv.gz.tmp"
    with (run / "evaluation_predictions.csv").open(newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError("Missing prediction columns")
        with gzip.open(temporary, "wt", newline="") as target:
            writer = csv.DictWriter(target, fieldnames=reader.fieldnames)
            writer.writeheader()
            for row in reader:
                if row["model"] == "hgb_weather" and row["period"] in {
                    "test_2021",
                    "validation_2020",
                }:
                    writer.writerow(row)
    temporary.replace(output / "predictions.csv.gz")
    (output / "areas.csv").write_bytes((run / "evaluation_areas.csv").read_bytes())
    manifest = {
        "schema_version": 1,
        "parent_sha256": _sha(directory / "cohort" / "parent_manifest.json"),
        "source_predictions_sha256": parent["output_sha256"]["evaluation_predictions.csv"],
        "predictions_sha256": _sha(output / "predictions.csv.gz"),
        "areas_sha256": _sha(output / "areas.csv"),
        "model": "hgb_weather",
        "protocol": PROTOCOL,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return output


def load_evaluation(
    directory: Path, filters: Filters, areas: tuple[CandidateArea, ...], period: str
) -> Evaluation:
    try:
        parent = forecast_cohort.verified_training_manifest(directory)
        output = directory / "evaluation"
        metadata = json.loads((output / "manifest.json").read_bytes())
        parent_sha = _sha(directory / "cohort" / "parent_manifest.json")
        if (
            metadata["schema_version"] != 1
            or metadata["model"] != "hgb_weather"
            or metadata["protocol"] != PROTOCOL
            or parent.get("evaluation_protocol") != PROTOCOL
            or parent.get("features") != forecast.FEATURES
            or metadata["parent_sha256"] != parent_sha
            or metadata["source_predictions_sha256"]
            != parent["output_sha256"]["evaluation_predictions.csv"]
            or metadata["areas_sha256"] != parent["output_sha256"]["evaluation_areas.csv"]
            or _sha(output / "areas.csv") != metadata["areas_sha256"]
            or _sha(output / "predictions.csv.gz") != metadata["predictions_sha256"]
        ):
            raise ValueError("Invalid evaluation provenance")
        requested, first, last, trained_end = PERIODS[period]
        with (output / "areas.csv").open(newline="") as source:
            catalog = list(csv.DictReader(source))
        candidates: dict[tuple[str, str], list[CandidateArea]] = {}
        for area in areas:
            if area.state == filters.state and area.city.casefold() == filters.city.casefold():
                candidates.setdefault((area.city.casefold(), area.postal_code), []).append(area)
        matches: dict[str, tuple[CandidateArea, int, str]] = {}
        scoped_groups: dict[tuple[str, str], list[dict[str, str]]] = {}
        for group in catalog:
            if (
                group["metro"] == filters.city
                and group["state"] == filters.state
                and group["city"].casefold() == filters.city.casefold()
                and group["category"] == filters.category
            ):
                scoped_groups.setdefault((group["city"].casefold(), group["zip"]), []).append(group)
        for key, groups in scoped_groups.items():
            if len(groups) == 1 and len(candidates.get(key, [])) == 1:
                group = groups[0]
                matches[group["group_id"]] = (
                    candidates[key][0],
                    int(group["n_businesses"]),
                    group["station_id"],
                )
        weather_frame = pd.read_csv(directory / "weather_daily.csv", dtype={"station_id": str})
        weather = {
            (str(row["station_id"]), str(row["date"])): row
            for row in weather_frame.loc[weather_frame["complete_weather"]].to_dict("records")
        }
        result = []
        seen: set[tuple[int, str]] = set()
        with gzip.open(output / "predictions.csv.gz", "rt", newline="") as source:
            for row in csv.DictReader(source):
                if row["period"] != requested or row["group_id"] not in matches:
                    continue
                area, n, station = matches[row["group_id"]]
                count, actual, predicted = (
                    int(row["checkins"]),
                    float(row["rate"]),
                    float(row["predicted_rate"]),
                )
                day = date.fromisoformat(row["date"]).isoformat()
                if (
                    n <= 0
                    or int(row["n_businesses"]) != n
                    or count < 0
                    or row["model"] != "hgb_weather"
                    or row["metro"] != filters.city
                    or row["category"] != filters.category
                    or not first <= day <= last
                    or not math.isfinite(actual)
                    or not math.isfinite(predicted)
                    or predicted <= 0
                    or not math.isclose(actual, count / n, abs_tol=1e-10)
                    or (area.id, day) in seen
                ):
                    raise ValueError("Invalid evaluation row")
                seen.add((area.id, day))
                observed = weather.get((station, day), {})
                point: dict[str, Any] = {
                    "Area ID": area.id,
                    "ZIP area": area.label,
                    "Date": day,
                    "Recorded": actual * 100,
                    "Predicted": predicted * 100,
                    "Residual": (actual - predicted) * 100,
                    "Recorded check-ins": count,
                    "Cohort businesses": n,
                    "Evaluation period": requested,
                    "Trained through": trained_end,
                    "Cohort selected through": "2019-12-31",
                    "Weather station": station,
                    "Source": "Saved forward-time backtest",
                }
                for source_field, target_field in (
                    ("tmax_c", "Daily high °C"),
                    ("tmin_c", "Daily low °C"),
                    ("precip_mm", "Precipitation mm"),
                ):
                    v = observed.get(source_field)
                    point[target_field] = float(v) if v is not None and math.isfinite(v) else None
                result.append(point)
        return Evaluation(
            tuple(result), None if result else "No exact ZIP/category backtest match.", parent_sha
        )
    except (OSError, EOFError, ValueError, KeyError, TypeError, OverflowError, csv.Error):
        return Evaluation(
            reason="Verified saved backtest results are unavailable for this selection."
        )


def response_curves(
    bundle: forecast.ForecastBundle,
    filters: Filters,
    areas: tuple[CandidateArea, ...],
    variable: str,
) -> tuple[dict[str, Any], ...]:
    """Sampled ICE over five exact observed-day contexts; no new fit or activity input.

    Temperature grids use the intersection of training support and physical
    high/low constraints for every context. Their average is explicitly a
    sampled response, not a population PDP or a causal effect.
    """
    if variable not in forecast.WEATHER:
        raise ValueError("Unsupported weather variable")
    locations = bundle.locations.loc[
        (bundle.locations["metro"] == filters.city)
        & (bundle.locations["state"] == filters.state)
        & (bundle.locations["city"].str.casefold() == filters.city.casefold())
        & (bundle.locations["category"] == filters.category)
    ]
    features: list[dict[str, Any]] = []
    identities: list[dict[str, Any]] = []
    for area in areas:
        if area.state != filters.state or area.city.casefold() != filters.city.casefold():
            continue
        if sum(a.postal_code == area.postal_code for a in areas) != 1:
            continue
        matches = locations.loc[locations["zip"] == area.postal_code]
        if len(matches) != 1:
            continue
        location = matches.iloc[0].to_dict()
        station_weather = bundle.weather.loc[
            (bundle.weather["station_id"] == location["station_id"])
            & bundle.weather["complete_weather"]
        ].sort_values("date")
        available = station_weather.loc[
            station_weather["date"].between(filters.start_date, filters.end_date)
        ]
        if available.empty:
            continue
        latest = available["date"].max()
        available = available.loc[available["date"] >= latest - timedelta(days=29)]
        contexts = available.iloc[
            np.unique(np.linspace(0, len(available) - 1, min(5, len(available))).astype(int))
        ]
        training = station_weather.loc[
            station_weather["date"].between(
                date.fromisoformat(bundle.manifest["trained_start"]),
                date.fromisoformat(bundle.manifest["trained_end"]),
            )
        ]
        if training.empty:
            continue
        low, high = (float(x) for x in training[variable].quantile([0.05, 0.95]))
        if variable == "tmax_c":
            low = max(low, float(contexts["tmin_c"].max()))
        elif variable == "tmin_c":
            high = min(high, float(contexts["tmax_c"].min()))
        else:
            low = max(0, low)
        if low >= high:
            continue
        for reference in contexts.to_dict("records"):
            for x in np.linspace(low, high, 20):
                row = dict(location)
                row.update({name: float(reference[name]) for name in forecast.WEATHER})
                row.update(
                    calendar_month=reference["date"].month, day_of_week=reference["date"].weekday()
                )
                row[variable] = float(x)
                features.append(row)
                identities.append(
                    {
                        "Area ID": area.id,
                        "ZIP area": area.label,
                        "Weather value": float(x),
                        "Reference date": reference["date"].isoformat(),
                        "Curve": f"ICE {reference['date'].isoformat()}",
                        "Kind": "ICE",
                        "Weather station": str(location["station_id"]),
                        "High °C": row["tmax_c"],
                        "Low °C": row["tmin_c"],
                        "Rain mm": row["precip_mm"],
                    }
                )
    if not features:
        return ()
    with threadpool_limits(limits=2):
        predictions = bundle.model.predict(pd.DataFrame(features)[forecast.FEATURES])
    if not np.isfinite(predictions).all() or (predictions <= 0).any():
        raise forecast.ArtifactUnavailableError("Model response is unavailable.")
    for row, prediction in zip(identities, predictions, strict=True):
        row["Predicted"] = float(prediction) * 100
    frame = pd.DataFrame(identities)
    for (_, _), group in frame.groupby(["Area ID", "Weather value"], sort=False):
        identities.append(
            {
                "Area ID": int(group.iloc[0]["Area ID"]),
                "ZIP area": group.iloc[0]["ZIP area"],
                "Weather value": float(group.iloc[0]["Weather value"]),
                "Predicted": float(group["Predicted"].mean()),
                "Kind": "Average",
                "Curve": "Average of sampled contexts",
                "Reference days": len(group),
            }
        )
    return tuple(identities)
