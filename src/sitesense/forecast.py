"""Daily conditional activity predictions from a trusted, locally trained model.

Training runs explicitly in the reporting notebook. This module only loads its
fixed artifact bundle and predicts with weather, location, industry and calendar
features; check-ins and business counts never enter the prediction inputs.
"""

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date as CalendarDate
from importlib.metadata import version
from io import BytesIO
from numbers import Real
from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]
import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
from threadpoolctl import threadpool_limits  # type: ignore[import-untyped]

# Project commands run from the repository root; container WORKDIR is /app.
# Resolve relative to that root, including when this package is installed as a wheel.
MODEL_DIR = Path.cwd() / "models" / "location_forecast"
FEATURES = [
    "metro",
    "category",
    "calendar_month",
    "day_of_week",
    "latitude",
    "longitude",
    "tmax_c",
    "tmin_c",
    "precip_mm",
]
WEATHER = ["tmax_c", "tmin_c", "precip_mm"]
LOCATION_COLUMNS = [
    "group_id",
    "area_id",
    "metro",
    "city",
    "state",
    "zip",
    "category",
    "latitude",
    "longitude",
    "station_id",
]
_ARTIFACTS = ("model.joblib", "baseline.csv", "locations.csv", "weather_daily.csv")
_PACKAGES = {"sklearn": "scikit-learn", "numpy": "numpy", "pandas": "pandas", "joblib": "joblib"}
_UNAVAILABLE = "Forecast model is unavailable. Run the training notebook to create a valid bundle."


class ArtifactUnavailableError(ValueError):
    """A missing or incompatible bundle, safe to display without local paths."""


@dataclass(frozen=True)
class ForecastBundle:
    model: Pipeline
    locations: pd.DataFrame
    weather: pd.DataFrame
    baseline: pd.DataFrame
    manifest: dict[str, Any]


def load_bundle(directory: Path = MODEL_DIR) -> ForecastBundle:
    """Verify provenance and versions before deserializing trusted local files.

    Joblib files must only come from the local training notebook. Hashes detect
    incomplete or accidentally changed artifacts; they do not authenticate an
    untrusted uploaded model.
    """
    try:
        manifest = json.loads((directory / "manifest.json").read_bytes())
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("status") != "complete"
            or manifest.get("model_name") != "hgb_weather"
            or manifest.get("features") != FEATURES
        ):
            raise ValueError("Invalid manifest")
        for key, package in _PACKAGES.items():
            if manifest["versions"][key] != version(package):
                raise ValueError("Incompatible runtime")
        start = CalendarDate.fromisoformat(manifest["trained_start"])
        end = CalendarDate.fromisoformat(manifest["trained_end"])
        if start > end:
            raise ValueError("Invalid training dates")
        for name, bounds in manifest.get("weather_range", {}).items():
            if (
                name not in WEATHER
                or not math.isfinite(bounds["min"])
                or not math.isfinite(bounds["max"])
                or bounds["min"] > bounds["max"]
            ):
                raise ValueError("Invalid training weather range")
        # Consume the same bytes we verify, even if a training run replaces files
        # between verification and parsing. A mixed generation fails its hashes.
        artifact_bytes = {filename: (directory / filename).read_bytes() for filename in _ARTIFACTS}
        for filename, data in artifact_bytes.items():
            if manifest["artifact_sha256"][filename] != hashlib.sha256(data).hexdigest():
                raise ValueError("Changed artifact")

        locations = pd.read_csv(
            BytesIO(artifact_bytes["locations.csv"]),
            dtype={
                column: str
                for column in LOCATION_COLUMNS
                if column not in ("latitude", "longitude")
            },
        )[LOCATION_COLUMNS]
        weather = pd.read_csv(
            BytesIO(artifact_bytes["weather_daily.csv"]), dtype={"station_id": str, "date": str}
        )
        baseline = pd.read_csv(
            BytesIO(artifact_bytes["baseline.csv"]), dtype={"area_id": str, "category": str}
        )
        if (
            locations.empty
            or locations["group_id"].duplicated().any()
            or locations[["area_id", "category"]].duplicated().any()
            or locations.isna().any().any()
            or not np.isfinite(locations[["latitude", "longitude"]].to_numpy()).all()
            or not locations["latitude"].between(-90, 90).all()
            or not locations["longitude"].between(-180, 180).all()
        ):
            raise ValueError("Invalid locations")
        weather = weather[["station_id", "date", *WEATHER, "complete_weather"]].copy()
        weather["date"] = pd.to_datetime(weather["date"], format="%Y-%m-%d").dt.date
        if (
            weather[["station_id", "date"]].duplicated().any()
            or not weather["complete_weather"].isin([True, False]).all()
            or weather[["station_id", "date"]].isna().any().any()
        ):
            raise ValueError("Invalid weather")
        weather["complete_weather"] = weather["complete_weather"].astype(bool)
        complete = weather.loc[weather["complete_weather"], WEATHER]
        if (
            not np.isfinite(complete.to_numpy()).all()
            or (complete["tmin_c"] > complete["tmax_c"]).any()
            or (complete["precip_mm"] < 0).any()
        ):
            raise ValueError("Invalid complete weather")
        baseline = baseline[["area_id", "category", "calendar_month", "mean_rate"]].copy()
        if (
            baseline[["area_id", "category", "calendar_month"]].duplicated().any()
            or baseline.isna().any().any()
            or not baseline["calendar_month"].isin(range(1, 13)).all()
            or not np.isfinite(baseline["mean_rate"].to_numpy()).all()
            or (baseline["mean_rate"] < 0).any()
        ):
            raise ValueError("Invalid baseline")
        model = joblib.load(BytesIO(artifact_bytes["model.joblib"]))
        if not isinstance(model, Pipeline) or list(model.feature_names_in_) != FEATURES:
            raise ValueError("Unexpected model inputs")
        return ForecastBundle(model, locations, weather, baseline, manifest)
    except Exception:
        raise ArtifactUnavailableError(_UNAVAILABLE) from None


def _manual_weather(weather: Mapping[str, float]) -> dict[str, float]:
    values: dict[str, float] = {}
    for name in WEATHER:
        value = weather.get(name)
        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
            raise ValueError("Provide finite numeric values for all three weather inputs.")
        values[name] = float(value)
    if values["tmin_c"] > values["tmax_c"]:
        raise ValueError("Daily minimum temperature must not exceed the maximum.")
    if values["precip_mm"] < 0:
        raise ValueError("Precipitation must be zero or greater.")
    return values


def predict_locations(
    bundle: ForecastBundle,
    date: CalendarDate,
    metro: str,
    category: str,
    weather: Mapping[str, float] | None = None,
) -> pd.DataFrame:
    """Rank known areas for any date with manual or exact-day observed weather.

    Manual weather represents one shared metro scenario. Observed weather uses
    each area's mapped station and excludes areas missing any daily variable.
    The score is expected recorded check-ins per cohort business per day.
    """
    selected = bundle.locations.loc[
        (bundle.locations["metro"] == metro) & (bundle.locations["category"] == category),
        LOCATION_COLUMNS,
    ].copy()
    if selected.empty:
        raise ValueError("No trained locations match the selected metro and industry.")
    candidate_count = len(selected)
    if weather is not None:
        for name, value in _manual_weather(weather).items():
            selected[name] = value
        source = "supplied_weather"
    else:
        daily = bundle.weather.loc[
            (bundle.weather["date"] == date) & bundle.weather["complete_weather"],
            ["station_id", *WEATHER],
        ]
        selected = selected.merge(daily, on="station_id", how="inner", validate="many_to_one")
        source = "observed_acis"
        if selected.empty:
            raise ValueError(
                "No complete observed weather for this date. Enter a weather scenario."
            )
    selected["calendar_month"] = date.month
    selected["day_of_week"] = date.weekday()
    try:
        with threadpool_limits(limits=2):
            prediction = bundle.model.predict(selected[FEATURES])
    except Exception:
        raise ArtifactUnavailableError(_UNAVAILABLE) from None
    if not np.isfinite(prediction).all() or (prediction <= 0).any():
        raise ArtifactUnavailableError(_UNAVAILABLE)
    selected["predicted_rate"] = prediction
    selected["date"] = date
    selected = selected.merge(
        bundle.baseline.rename(columns={"mean_rate": "baseline_rate"}),
        on=["area_id", "category", "calendar_month"],
        how="left",
        validate="many_to_one",
    )
    selected["outside_training_weather_range"] = False
    for name, bounds in bundle.manifest.get("weather_range", {}).items():
        if name in WEATHER:
            selected["outside_training_weather_range"] |= ~selected[name].between(
                bounds["min"], bounds["max"]
            )
    selected = selected.sort_values(
        ["predicted_rate", "group_id"], ascending=[False, True], kind="stable"
    ).reset_index(drop=True)
    selected["rank"] = np.arange(1, len(selected) + 1)
    selected.attrs = {
        "date": date.isoformat(),
        "weather_source": source,
        "model": bundle.manifest["model_name"],
        "training_start": bundle.manifest["trained_start"],
        "training_end": bundle.manifest["trained_end"],
        "candidate_count": candidate_count,
        "weather_complete_candidates": len(selected),
        "weather_excluded_candidates": candidate_count - len(selected),
        "outside_training_weather_range_count": int(
            selected["outside_training_weather_range"].sum()
        ),
    }
    for name, value in selected.attrs.items():
        if name != "date":
            selected[name] = value
    return selected
