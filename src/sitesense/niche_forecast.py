"""Validated archived-cohort calendar projections for exact source categories."""

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sitesense.models import CITY_SCOPES

ROOT = Path(__file__).resolve().parents[2]
MODEL_DIR = ROOT / "models" / "niche_forecast"
MODEL_NAMES = ("overall_mean", "month_weekday_shrunk")
MIN_BUSINESSES = 5
MIN_TRAINING_CHECKINS = 100
PRIOR_BUSINESS_DAYS = 30
CODE_FILES = (
    "src/sitesense/niche_forecast.py",
    "src/sitesense/niche_training.py",
    "scripts/train_niche_forecast.py",
)


class NicheForecastError(ValueError):
    """An unavailable or incompatible projection artifact; safe to show in the UI."""


@dataclass(frozen=True)
class NicheGroup:
    city: str
    state: str
    category: str
    source_category: str
    area_id: int
    postal_code: str
    latitude: float | None
    longitude: float | None
    n_businesses: int
    training_checkins: int
    overall_rate: float
    rates: tuple[float, ...]
    membership_sha256: str


@dataclass(frozen=True)
class NicheBundle:
    groups: tuple[NicheGroup, ...]
    manifest: dict[str, Any]


@dataclass(frozen=True)
class NicheProjection:
    group: NicheGroup
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


def membership_sha256(members: tuple[str, ...]) -> str:
    return hashlib.sha256(json.dumps(sorted(members), separators=(",", ":")).encode()).hexdigest()


def group_key(group: NicheGroup) -> tuple[str, str, str, str, int]:
    return group.city, group.state, group.category, group.source_category, group.area_id


def _sha(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def _number(value: Any, *, minimum: float = 0) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= minimum
    )


def _integer(value: Any, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def load_bundle(directory: Path = MODEL_DIR, import_revision: str = "") -> NicheBundle:
    """Reject stale imports, changed code/lockfiles and unsupported/tampered groups."""
    try:
        manifest = json.loads((directory / "manifest.json").read_bytes())
        payload_bytes = (directory / "groups.json").read_bytes()
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("status") != "complete"
            or manifest.get("model_name") not in MODEL_NAMES
            or not import_revision
            or manifest.get("import_revision") != import_revision
            or manifest.get("payload_sha256") != hashlib.sha256(payload_bytes).hexdigest()
            or manifest.get("code_sha256") != code_sha256()
            or manifest.get("lock_sha256") != lock_sha256()
            or not _sha(manifest.get("source_sha256"))
            or manifest.get("min_businesses") != MIN_BUSINESSES
            or manifest.get("min_training_checkins") != MIN_TRAINING_CHECKINS
            or manifest.get("prior_business_days") != PRIOR_BUSINESS_DAYS
            or manifest.get("training_start") != "2019-01-01"
            or manifest.get("training_end") != "2021-12-31"
            or manifest.get("validation_year") != 2020
            or manifest.get("assessment_year") != 2021
            or manifest.get("validation_training_start") != "2017-01-01"
            or manifest.get("validation_training_end") != "2019-12-31"
            or manifest.get("assessment_training_start") != "2018-01-01"
            or manifest.get("assessment_training_end") != "2020-12-31"
            or manifest.get("metric_weighting")
            != "equal_group_calendar_days; counts_divided_by_fixed_cohort_businesses"
            or manifest.get("source_categories_overlap") is not True
            or manifest.get("weather_features") is not False
            or date.fromisoformat(manifest["coverage_start"]) > date(2017, 1, 1)
            or date.fromisoformat(manifest["coverage_end"]) < date(2021, 12, 31)
        ):
            raise ValueError("Incompatible provenance")
        for metric in ("validation_metrics", "assessment_metrics"):
            values = manifest[metric]
            if not isinstance(values, dict) or not values:
                raise ValueError("Missing evaluation")
            for model in MODEL_NAMES:
                measured = values[model]
                if (
                    not _number(measured["poisson_deviance"])
                    or not _number(measured["mae"])
                    or not _integer(measured["groups"], 1)
                    or not _integer(measured["observations"], 1)
                    or measured["observations"]
                    != measured["groups"] * (366 if metric == "validation_metrics" else 365)
                    or not _integer(measured["ranked_niches"])
                    or not _integer(measured["top_zip_agreements"])
                    or measured["top_zip_agreements"] > measured["ranked_niches"]
                    or not _number(measured["top_zip_agreement"])
                    or measured["top_zip_agreement"] > 1
                ):
                    raise ValueError("Invalid evaluation")
            if values[MODEL_NAMES[0]]["groups"] != values[MODEL_NAMES[1]]["groups"]:
                raise ValueError("Incomparable evaluation groups")
        selected = min(
            MODEL_NAMES,
            key=lambda model: (
                manifest["validation_metrics"][model]["poisson_deviance"],
                MODEL_NAMES.index(model),
            ),
        )
        if selected != manifest["model_name"]:
            raise ValueError("Invalid model selection")
        payload = json.loads(payload_bytes)
        if not isinstance(payload, list):
            raise ValueError("Invalid groups")
        groups: list[NicheGroup] = []
        keys: set[tuple[str, str, str, str, int]] = set()
        for row in payload:
            if not isinstance(row, dict):
                raise ValueError("Invalid group")
            group = NicheGroup(**{**row, "rates": tuple(row["rates"])})
            if (
                not all(
                    isinstance(value, str) and value
                    for value in (
                        group.city,
                        group.state,
                        group.category,
                        group.source_category,
                        group.postal_code,
                    )
                )
                or group.category not in ("Restaurants", "Day Spas")
                or (group.city, group.state)
                not in {(scope.city, scope.state) for scope in CITY_SCOPES}
                or group.source_category == group.category
                or not _integer(group.area_id, 1)
                or not _integer(group.n_businesses, MIN_BUSINESSES)
                or not _integer(group.training_checkins, MIN_TRAINING_CHECKINS)
                or not _number(group.overall_rate)
                or len(group.rates) != 84
                or not all(_number(rate) for rate in group.rates)
                or not _sha(group.membership_sha256)
                or (group.latitude is not None and not _number(group.latitude, minimum=-90))
                or (group.latitude is not None and group.latitude > 90)
                or (group.longitude is not None and not _number(group.longitude, minimum=-180))
                or (group.longitude is not None and group.longitude > 180)
                or group_key(group) in keys
            ):
                raise ValueError("Unsupported group")
            expected_rate = group.training_checkins / (group.n_businesses * 1096)
            if not math.isclose(group.overall_rate, expected_rate, rel_tol=1e-10):
                raise ValueError("Invalid group exposure")
            if manifest["model_name"] == "overall_mean" and not all(
                math.isclose(rate, group.overall_rate, rel_tol=1e-10) for rate in group.rates
            ):
                raise ValueError("Invalid mean-model rates")
            groups.append(group)
            keys.add(group_key(group))
        if not groups or manifest.get("final_groups") != len(groups):
            raise ValueError("Invalid group count")
        return NicheBundle(tuple(groups), manifest)
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        raise NicheForecastError(
            "Source-category projections are unavailable. "
            "Train a compatible niche projection bundle."
        ) from None


def project(
    bundle: NicheBundle,
    city: str,
    state: str,
    category: str,
    source_category: str | None,
    start: date,
    end: date,
) -> tuple[NicheProjection, ...]:
    """Calendar-only per-business projection; no weather, revenue or opening-success claim."""
    if start <= date.fromisoformat(bundle.manifest["training_end"]):
        raise ValueError("Projection dates must be after the archived training period.")
    days = (end - start).days + 1
    if days < 1 or days > 3660:
        raise ValueError("Choose a projection interval of 1 to 3,660 days.")
    exposure = [0] * 84
    for offset in range(days):
        day = start + timedelta(days=offset)
        exposure[(day.month - 1) * 7 + day.weekday()] += 1
    rows = []
    for group in bundle.groups:
        if (group.city, group.state, group.category) != (city, state, category) or (
            source_category is not None and group.source_category != source_category
        ):
            continue
        total = sum(count * rate for count, rate in zip(exposure, group.rates, strict=True))
        rows.append(NicheProjection(group, days, total / days, total))
    return tuple(
        sorted(
            rows,
            key=lambda row: (-row.daily_rate, row.group.source_category, row.group.postal_code),
        )
    )
