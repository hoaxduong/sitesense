"""Verified individual-business archived calendar projections; never ZIP allocations."""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sitesense.models import BUSINESS_CATEGORY_LABELS, CITY_SCOPES

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "business_forecast"
MODEL_NAMES = ("overall_mean", "month_weekday_shrunk")
MIN_TRAINING_CHECKINS = 20
PRIOR_BUSINESS_DAYS = 30
CODE_FILES = (
    "src/sitesense/business_forecast.py",
    "src/sitesense/business_training.py",
    "scripts/train_business_forecast.py",
)
COHORT_RULE = "first_recorded_activity_on_or_before_each_training_start; fixed_within_fit"
POOL_RULE = "same_canonical_city_state_parent; supported_unique_business_ids"
TARGET = "recorded_checkins_per_individual_business_per_calendar_day"
WEIGHTING = "equal_business_parent_calendar_days; recorded_integer_counts"
RANK_SCOPE = "same_city_state_parent_source; supported_fixed_cohort_business_ids"


class BusinessForecastError(ValueError):
    """An unavailable or incompatible business projection artifact, safe for the UI."""


@dataclass(frozen=True)
class BusinessGroup:
    business_id: str
    name: str
    city: str
    state: str
    category: str
    source_categories: tuple[str, ...]
    latitude: float | None
    longitude: float | None
    training_checkins: int
    active_training_days: int
    overall_rate: float
    rates: tuple[float, ...]

    @property
    def parent(self) -> str:
        return self.category


@dataclass(frozen=True)
class BusinessBundle:
    groups: tuple[BusinessGroup, ...]
    manifest: dict[str, Any]


@dataclass(frozen=True)
class BusinessProjection:
    group: BusinessGroup
    calendar_days: int
    daily_rate: float
    period_rate: float


def code_sha256() -> str:
    digest = hashlib.sha256()
    for name in CODE_FILES:
        digest.update(name.encode())
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def lock_sha256() -> str:
    return hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest()


def group_key(group: BusinessGroup) -> tuple[str, str]:
    return group.business_id, group.category


def _number(value: Any, minimum: float = 0) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= minimum
    )


def _integer(value: Any, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _sha(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _validate_metrics(manifest: dict[str, Any]) -> None:
    for metric, days in (("validation_metrics", 366), ("assessment_metrics", 365)):
        values = manifest[metric]
        for model in MODEL_NAMES:
            measured = values[model]
            if (
                not _number(measured["poisson_deviance"])
                or not _number(measured["mae"])
                or not _integer(measured["businesses"], 1)
                or not _integer(measured["observations"], 1)
                or measured["observations"] != measured["businesses"] * days
                or not _integer(measured["ranked_niches"])
                or not _integer(measured["ranked_businesses"])
                or not _integer(measured["top_business_agreements"])
                or measured["top_business_agreements"] > measured["ranked_niches"]
                or not _number(measured["top_business_agreement"])
                or measured["top_business_agreement"] > 1
                or not math.isclose(
                    measured["top_business_agreement"],
                    measured["top_business_agreements"] / measured["ranked_niches"]
                    if measured["ranked_niches"]
                    else 0.0,
                )
            ):
                raise ValueError("Invalid individual-business evaluation")
        for field in ("businesses", "observations", "ranked_niches", "ranked_businesses"):
            if values[MODEL_NAMES[0]][field] != values[MODEL_NAMES[1]][field]:
                raise ValueError("Incomparable evaluation cohorts")
    selected = min(
        MODEL_NAMES,
        key=lambda name: (
            manifest["validation_metrics"][name]["poisson_deviance"],
            MODEL_NAMES.index(name),
        ),
    )
    if manifest["model_name"] != selected:
        raise ValueError("Invalid temporal model selection")


def load_bundle(directory: Path = MODEL_DIR, import_revision: str = "") -> BusinessBundle:
    """Reject changed imports, code, lockfiles, unsupported rows and altered payloads."""
    try:
        manifest = json.loads((directory / "manifest.json").read_bytes())
        payload_bytes = (directory / "businesses.json").read_bytes()
        expected = {
            "schema_version": 1,
            "status": "complete",
            "grain": "business_id_parent_day",
            "training_start": "2019-01-01",
            "training_end": "2021-12-31",
            "validation_year": 2020,
            "validation_training_start": "2017-01-01",
            "validation_training_end": "2019-12-31",
            "assessment_year": 2021,
            "assessment_training_start": "2018-01-01",
            "assessment_training_end": "2020-12-31",
            "min_training_checkins": MIN_TRAINING_CHECKINS,
            "prior_business_days": PRIOR_BUSINESS_DAYS,
            "cohort_rule": COHORT_RULE,
            "pool_rule": POOL_RULE,
            "target": TARGET,
            "metric_weighting": WEIGHTING,
            "ranking_scope": RANK_SCOPE,
            "weather_features": False,
            "source_categories_overlap": True,
        }
        if (
            not isinstance(manifest, dict)
            or any(manifest.get(field) != value for field, value in expected.items())
            or manifest.get("model_name") not in MODEL_NAMES
            or not import_revision
            or manifest.get("import_revision") != import_revision
            or manifest.get("payload_sha256") != hashlib.sha256(payload_bytes).hexdigest()
            or manifest.get("code_sha256") != code_sha256()
            or manifest.get("lock_sha256") != lock_sha256()
            or not _sha(manifest.get("source_sha256"))
            or date.fromisoformat(manifest["coverage_start"]) > date(2017, 1, 1)
            or date.fromisoformat(manifest["coverage_end"]) < date(2021, 12, 31)
        ):
            raise ValueError("Incompatible business provenance")
        _validate_metrics(manifest)
        payload = json.loads(payload_bytes)
        if not isinstance(payload, list):
            raise ValueError("Invalid business payload")
        groups: list[BusinessGroup] = []
        keys: set[tuple[str, str]] = set()
        scopes = {(scope.city, scope.state) for scope in CITY_SCOPES}
        for row in payload:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("rates"), list)
                or not isinstance(row.get("source_categories"), list)
            ):
                raise ValueError("Invalid business record")
            group = BusinessGroup(
                **{
                    **row,
                    "rates": tuple(row["rates"]),
                    "source_categories": tuple(row["source_categories"]),
                }
            )
            if (
                not all(
                    isinstance(value, str) and value
                    for value in (group.business_id, group.name, group.city, group.state)
                )
                or group.category not in BUSINESS_CATEGORY_LABELS
                or (group.city, group.state) not in scopes
                or not all(
                    isinstance(source, str) and source and source != group.category
                    for source in group.source_categories
                )
                or tuple(sorted(set(group.source_categories))) != group.source_categories
                or not _integer(group.training_checkins, MIN_TRAINING_CHECKINS)
                or not _integer(group.active_training_days, 1)
                or group.active_training_days > min(1096, group.training_checkins)
                or not _number(group.overall_rate)
                or len(group.rates) != 84
                or not all(_number(rate) for rate in group.rates)
                or (group.latitude is not None and not _number(group.latitude, -90))
                or (group.latitude is not None and group.latitude > 90)
                or (group.longitude is not None and not _number(group.longitude, -180))
                or (group.longitude is not None and group.longitude > 180)
                or group_key(group) in keys
                or not math.isclose(
                    group.overall_rate, group.training_checkins / 1096, rel_tol=1e-10
                )
                or (
                    manifest["model_name"] == "overall_mean"
                    and not all(
                        math.isclose(rate, group.overall_rate, rel_tol=1e-10)
                        for rate in group.rates
                    )
                )
            ):
                raise ValueError("Unsupported individual-business projection")
            groups.append(group)
            keys.add(group_key(group))
        if not groups or manifest.get("final_businesses") != len(groups):
            raise ValueError("Invalid final business count")
        return BusinessBundle(tuple(groups), manifest)
    except (OSError, ValueError, TypeError, KeyError, OverflowError, AttributeError):
        raise BusinessForecastError(
            "Individual-business projections are unavailable. "
            "Train a compatible business projection bundle."
        ) from None


def project(
    bundle: BusinessBundle,
    city: str,
    state: str,
    category: str,
    source_category: str | None,
    start: date,
    end: date,
) -> tuple[BusinessProjection, ...]:
    """Filter exact tags without dividing or replicating an individual business's target."""
    if start <= date.fromisoformat(bundle.manifest["training_end"]):
        raise ValueError("Projection dates must follow the archived training period.")
    days = (end - start).days + 1
    if not 1 <= days <= 3660:
        raise ValueError("Choose a projection interval of 1 to 3,660 days.")
    exposure = [0] * 84
    for offset in range(days):
        day = start + timedelta(days=offset)
        exposure[(day.month - 1) * 7 + day.weekday()] += 1
    rows = []
    for group in bundle.groups:
        if (group.city, group.state, group.category) != (city, state, category):
            continue
        if source_category is not None and source_category not in group.source_categories:
            continue
        total = sum(n * rate for n, rate in zip(exposure, group.rates, strict=True))
        rows.append(BusinessProjection(group, days, total / days, total))
    return tuple(sorted(rows, key=lambda row: (-row.daily_rate, row.group.business_id)))
