"""Verified business/day HGB models with explicit, training-derived weather scenarios."""

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import date, timedelta
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from pickle import UnpicklingError
from typing import Any

import joblib  # type: ignore[import-untyped]
import pandas as pd  # type: ignore[import-untyped]

from sitesense import business_forecast, business_research
from sitesense.business_forecast import BusinessBundle, BusinessProjection
from sitesense.business_research import FittedResearchModel
from sitesense.models import BUSINESS_CATEGORY_LABELS, CITY_SCOPES

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "business_hgb"
CODE_FILES = (
    "src/sitesense/business_hgb.py",
    "src/sitesense/business_hgb_training.py",
    "src/sitesense/business_observed.py",
    "src/sitesense/location_weather.py",
    "src/sitesense/business_research.py",
    "src/sitesense/business_training.py",
    "src/sitesense/business_forecast.py",
    "src/sitesense/repository.py",
    "src/sitesense/models.py",
    "scripts/train_business_hgb.py",
)
FEATURES = ["business_id", "month", "weekday", *business_research.WEATHER_COLUMNS]
POLICY = (
    "hgb_for_observed_weather_and_explicit_scenarios; "
    "baselines_for_comparison; no_accuracy_winner_claim"
)
Weather = tuple[float, float, float]  # minimum °C, maximum °C, precipitation mm/day


@dataclass(frozen=True)
class HgbBundle:
    models: tuple[FittedResearchModel, ...]
    normals: dict[tuple[str, str], tuple[Weather | None, ...]]
    manifest: dict[str, Any]


def code_sha256() -> str:
    digest = hashlib.sha256()
    for name in CODE_FILES:
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def versions() -> dict[str, str]:
    return {name: version(name) for name in ("scikit-learn", "numpy", "pandas", "joblib")}


def artifact_revision(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in ("manifest.json", "models.joblib"):
        digest.update((directory / name).read_bytes())
    digest.update(code_sha256().encode())
    digest.update(business_forecast.lock_sha256().encode())
    return digest.hexdigest()


def validate_weather(weather: Weather) -> None:
    if (
        len(weather) != 3
        or not all(isinstance(v, (float, int)) and math.isfinite(v) for v in weather)
        or weather[0] > weather[1]
        or weather[2] < 0
    ):
        raise ValueError("Choose coherent low/high temperatures and nonnegative daily rain.")


def load_bundle(directory: Path, import_revision: str) -> HgbBundle:
    """Verify trusted local artifacts before deserialization; never accept uploaded models."""
    try:
        manifest = json.loads((directory / "manifest.json").read_bytes())
        payload = (directory / "models.joblib").read_bytes()
        if (
            manifest.get("schema_version") != 1
            or manifest.get("status") != "complete"
            or manifest.get("model_name") != "hgb"
            or manifest.get("grain") != "business_id_parent_day"
            or manifest.get("features") != FEATURES
            or manifest.get("protocol") != business_research.PROTOCOL
            or manifest.get("serving_policy") != POLICY
            or manifest.get("training_start") != "2019-01-01"
            or manifest.get("training_end") != "2021-12-31"
            or manifest.get("code_sha256") != code_sha256()
            or manifest.get("lock_sha256") != business_forecast.lock_sha256()
            or manifest.get("versions") != versions()
            or not import_revision
            or manifest.get("import_revision") != import_revision
            or manifest.get("payload_sha256") != hashlib.sha256(payload).hexdigest()
        ):
            raise ValueError("Incompatible business HGB artifact")
        saved = joblib.load(BytesIO(payload))
        models, normals = saved["models"], saved["normals"]
        keys: set[tuple[str, str]] = set()
        scopes = {(s.city, s.state) for s in CITY_SCOPES}
        for model in models:
            if (
                not isinstance(model, FittedResearchModel)
                or model.model_name != "hgb"
                or not model.weather_features
                or not 1 <= len(model.groups) <= 128
                or model.business_ids != tuple(g.business_id for g in model.groups)
            ):
                raise ValueError("Invalid HGB model")
            for group in model.groups:
                key = group.business_id, group.category
                if (
                    key in keys
                    or key not in normals
                    or len(normals[key]) != 12
                    or (group.city, group.state) not in scopes
                    or group.category not in BUSINESS_CATEGORY_LABELS
                    or not any(normals[key])
                ):
                    raise ValueError("Invalid business/month weather metadata")
                keys.add(key)
                for weather in normals[key]:
                    if weather is not None:
                        validate_weather(weather)
        if not keys or manifest.get("final_businesses") != len(keys) or set(normals) != keys:
            raise ValueError("Invalid HGB business coverage")
        return HgbBundle(tuple(models), normals, manifest)
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError, UnpicklingError):
        raise ValueError("The business HGB model is unavailable or needs retraining.") from None


def project(
    bundle: HgbBundle,
    city: str,
    state: str,
    category: str,
    source_category: str | None,
    start: date,
    end: date,
    business_ids: tuple[str, ...],
    weather: Weather | None = None,
) -> tuple[BusinessProjection, ...]:
    """Predict each business with HGB; monthly normals are an explicit seasonal scenario."""
    if weather is not None:
        validate_weather(weather)
    if start <= date.fromisoformat(bundle.manifest["training_end"]):
        raise ValueError("HGB scenario dates must follow the training cutoff.")
    days = (end - start).days + 1
    if not 1 <= days <= 3660:
        raise ValueError("Choose 1 to 3,660 scenario days.")
    selected_months = {(start + timedelta(days=offset)).month for offset in range(days)}
    requested = set(business_ids)
    projected_groups = []
    for model in bundle.models:
        groups = [
            g
            for g in model.groups
            if (g.city, g.state, g.category) == (city, state, category)
            and g.business_id in requested
            and (source_category is None or source_category in g.source_categories)
            and (
                weather is not None
                or all(bundle.normals[g.business_id, g.category][m - 1] for m in selected_months)
            )
        ]
        if not groups:
            continue
        rows = []
        for group in groups:
            for month in range(1, 13):
                scenario = weather or bundle.normals[group.business_id, group.category][month - 1]
                # Unrequested unsupported months do not contribute to the selected dates.
                if scenario is None:
                    continue
                for weekday in range(7):
                    rows.append(
                        {
                            "business_id": group.business_id,
                            "month": month,
                            "weekday": weekday,
                            "tmin_c": scenario[0],
                            "tmax_c": scenario[1],
                            "precipitation_mm": scenario[2],
                            "weather_available": True,
                        }
                    )
        frame = pd.DataFrame(rows)
        frame["predicted"] = business_research.predict(model, frame)
        for group in groups:
            rates = [0.0] * 84
            for row in frame.loc[frame.business_id == group.business_id].itertuples():
                rates[(row.month - 1) * 7 + row.weekday] = float(row.predicted)
            projected_groups.append(replace(group, rates=tuple(rates)))
    # The existing projection DTO keeps maps, comparisons and daily charts consistent.
    return business_forecast.project(
        BusinessBundle(tuple(projected_groups), bundle.manifest),
        city,
        state,
        category,
        source_category,
        start,
        end,
    )
