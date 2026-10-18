"""Explicit read-only fitting at the individual business/day target grain."""

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sitesense import repository
from sitesense.business_forecast import (
    COHORT_RULE,
    MIN_TRAINING_CHECKINS,
    MODEL_NAMES,
    POOL_RULE,
    PRIOR_BUSINESS_DAYS,
    RANK_SCOPE,
    TARGET,
    WEIGHTING,
    BusinessGroup,
    code_sha256,
    lock_sha256,
)
from sitesense.database import connect
from sitesense.models import BUSINESS_CATEGORY_LABELS, CITY_SCOPES


@dataclass(frozen=True)
class TrainingBusiness:
    business_id: str
    name: str
    city: str
    state: str
    latitude: float | None
    longitude: float | None
    categories: tuple[str, ...]
    first_activity: date


@dataclass(frozen=True)
class TrainingDay:
    business_id: str
    day: date
    checkins: int


@dataclass(frozen=True)
class TrainingData:
    businesses: tuple[TrainingBusiness, ...]
    activity: tuple[TrainingDay, ...]
    coverage_start: date
    coverage_end: date


@dataclass(frozen=True)
class Fit:
    groups: tuple[BusinessGroup, ...]
    start: date
    end: date


def load_training_data() -> TrainingData:
    """Read real business coordinates and counts; no area joins or listing-status gates."""
    cities = [scope.city for scope in CITY_SCOPES]
    with connect() as connection:
        connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        metadata = connection.execute(
            """SELECT metadata FROM sitesense.datasets
               WHERE metadata ->> 'import_key' = 'basic_app_activity_v1'
                 AND metadata ->> 'status' = 'complete'
               ORDER BY id DESC LIMIT 1"""
        ).fetchone()
        if metadata is None:
            raise ValueError("A completed activity import is required.")
        coverage = metadata[0]["coverage"]
        rows = connection.execute(
            """SELECT b.business_id, b.name, b.canonical_city, b.canonical_state,
                      b.latitude, b.longitude, b.categories, min(a.activity_date)
               FROM sitesense.businesses AS b
               JOIN sitesense.business_activity_hourly AS a USING (business_id)
               WHERE b.canonical_city = ANY(%s::text[])
                 AND (b.categories @> ARRAY['Restaurants']::text[]
                      OR b.categories @> ARRAY['Day Spas']::text[])
               GROUP BY b.business_id ORDER BY b.business_id""",
            (cities,),
        ).fetchall()
        daily = connection.execute(
            """SELECT b.business_id, a.activity_date, sum(a.checkin_count)
               FROM sitesense.businesses AS b
               JOIN sitesense.business_activity_hourly AS a USING (business_id)
               WHERE b.canonical_city = ANY(%s::text[])
                 AND (b.categories @> ARRAY['Restaurants']::text[]
                      OR b.categories @> ARRAY['Day Spas']::text[])
                 AND a.activity_date BETWEEN DATE '2017-01-01' AND DATE '2021-12-31'
               GROUP BY b.business_id, a.activity_date ORDER BY b.business_id, a.activity_date""",
            (cities,),
        ).fetchall()
    scopes = {(scope.city, scope.state) for scope in CITY_SCOPES}
    businesses = tuple(
        TrainingBusiness(row[0], row[1], row[2], row[3], row[4], row[5], tuple(row[6]), row[7])
        for row in rows
        if (row[2], row[3]) in scopes
    )
    identifiers = {business.business_id for business in businesses}
    return TrainingData(
        businesses,
        tuple(TrainingDay(*row) for row in daily if row[0] in identifiers),
        date.fromisoformat(coverage["start_date"]),
        date.fromisoformat(coverage["end_date"]),
    )


def _days(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _cell(day: date) -> int:
    return (day.month - 1) * 7 + day.weekday()


def _validate(data: TrainingData) -> None:
    identifiers = {business.business_id for business in data.businesses}
    if len(identifiers) != len(data.businesses):
        raise ValueError("Duplicate training business IDs.")
    seen = set()
    for record in data.activity:
        if (
            not isinstance(record.checkins, int)
            or isinstance(record.checkins, bool)
            or record.checkins < 0
            or record.business_id not in identifiers
        ):
            raise ValueError("Known businesses and nonnegative integer counts are required.")
        key = record.business_id, record.day
        if key in seen:
            raise ValueError("Duplicate business/day training records.")
        seen.add(key)


def fit(data: TrainingData, start: date, end: date) -> Fit:
    """Rebuild fixed cohorts, support and unique-parent priors from each fit window only."""
    if end < start or data.coverage_start > start or data.coverage_end < end:
        raise ValueError("The training interval must have complete imported activity coverage.")
    _validate(data)
    businesses = {
        business.business_id: business
        for business in data.businesses
        if business.first_activity <= start
        and (business.city, business.state) in {(scope.city, scope.state) for scope in CITY_SCOPES}
    }
    counts: dict[str, list[int]] = defaultdict(lambda: [0] * 84)
    active: dict[str, int] = defaultdict(int)
    for record in data.activity:
        if record.business_id in businesses and start <= record.day <= end:
            counts[record.business_id][_cell(record.day)] += record.checkins
            active[record.business_id] += record.checkins > 0
    supported = {
        identifier: business
        for identifier, business in businesses.items()
        if sum(counts[identifier]) >= MIN_TRAINING_CHECKINS
    }
    exposure = [0] * 84
    for day in _days(start, end):
        exposure[_cell(day)] += 1
    if min(exposure) < 1:
        raise ValueError("Every month/weekday cell requires training calendar coverage.")
    pooled_counts: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0] * 84)
    pooled_businesses: dict[tuple[str, str, str], int] = defaultdict(int)
    for identifier, business in supported.items():
        for parent in BUSINESS_CATEGORY_LABELS:
            if parent not in business.categories:
                continue
            key = business.city, business.state, parent
            pooled_businesses[key] += 1
            for cell, count in enumerate(counts[identifier]):
                pooled_counts[key][cell] += count
    groups = []
    days = (end - start).days + 1
    for identifier, business in sorted(supported.items()):
        for parent in BUSINESS_CATEGORY_LABELS:
            if parent not in business.categories:
                continue
            pool = business.city, business.state, parent
            rates = tuple(
                (
                    counts[identifier][cell]
                    + PRIOR_BUSINESS_DAYS
                    * pooled_counts[pool][cell]
                    / (pooled_businesses[pool] * exposure[cell])
                )
                / (exposure[cell] + PRIOR_BUSINESS_DAYS)
                for cell in range(84)
            )
            groups.append(
                BusinessGroup(
                    identifier,
                    business.name,
                    business.city,
                    business.state,
                    parent,
                    tuple(sorted(set(business.categories) - {parent})),
                    business.latitude,
                    business.longitude,
                    sum(counts[identifier]),
                    active[identifier],
                    sum(counts[identifier]) / days,
                    rates,
                )
            )
    return Fit(tuple(groups), start, end)


def evaluate(data: TrainingData, fitted: Fit, year: int, model: str) -> dict[str, float | int]:
    """Score all held-out calendar zeros, plus top-business agreement in exact source scopes."""
    if model not in MODEL_NAMES:
        raise ValueError("Unknown calendar projection model.")
    start, end = date(year, 1, 1), date(year, 12, 31)
    if fitted.end >= start or data.coverage_start > start or data.coverage_end < end:
        raise ValueError("Assessment needs a fully covered year after the training cutoff.")
    _validate(data)
    exposure = [0] * 84
    for day in _days(start, end):
        exposure[_cell(day)] += 1
    observed: dict[str, list[TrainingDay]] = defaultdict(list)
    for record in data.activity:
        if start <= record.day <= end and record.checkins:
            observed[record.business_id].append(record)
    deviance, absolute_error, observations = 0.0, 0.0, 0
    rankings: dict[tuple[str, str, str, str], list[tuple[str, int, float]]] = defaultdict(list)
    for group in fitted.groups:
        rates = (group.overall_rate,) * 84 if model == "overall_mean" else group.rates
        predicted_sum = sum(n * rate for n, rate in zip(exposure, rates, strict=True))
        # Sparse evaluation is exactly the dense daily score: start with every real zero,
        # then replace only the observed nonzero dates. No held-out day is dropped.
        deviance += 2 * sum(n * max(rate, 1e-12) for n, rate in zip(exposure, rates, strict=True))
        absolute_error += predicted_sum
        observed_sum = 0
        for record in observed[group.business_id]:
            predicted = rates[_cell(record.day)]
            mu = max(predicted, 1e-12)
            deviance += 2 * (record.checkins * math.log(record.checkins / mu) - record.checkins)
            absolute_error += abs(record.checkins - predicted) - predicted
            observed_sum += record.checkins
        observations += (end - start).days + 1
        for source in group.source_categories:
            rankings[group.city, group.state, group.category, source].append(
                (group.business_id, observed_sum, predicted_sum)
            )
    if not observations:
        raise ValueError("No supported businesses for temporal assessment.")
    comparisons = [rows for rows in rankings.values() if len(rows) >= 2]
    agreements = sum(
        min(rows, key=lambda row: (-row[1], row[0]))[0]
        == min(rows, key=lambda row: (-row[2], row[0]))[0]
        for rows in comparisons
    )
    return {
        "poisson_deviance": max(0.0, deviance / observations),
        "mae": max(0.0, absolute_error / observations),
        "businesses": len(fitted.groups),
        "observations": observations,
        "ranked_niches": len(comparisons),
        "ranked_businesses": sum(len(rows) for rows in comparisons),
        "top_business_agreements": agreements,
        "top_business_agreement": agreements / len(comparisons) if comparisons else 0.0,
    }


def _source_sha256(data: TrainingData) -> str:
    digest = hashlib.sha256()
    for business in sorted(data.businesses, key=lambda row: row.business_id):
        digest.update(
            json.dumps(
                asdict(business), default=str, sort_keys=True, separators=(",", ":")
            ).encode()
        )
    for record in sorted(data.activity, key=lambda row: (row.business_id, row.day)):
        digest.update(
            json.dumps(asdict(record), default=str, sort_keys=True, separators=(",", ":")).encode()
        )
    digest.update(f"{data.coverage_start}/{data.coverage_end}".encode())
    return digest.hexdigest()


def train(
    data: TrainingData, import_revision: str
) -> tuple[tuple[BusinessGroup, ...], dict[str, Any]]:
    """Select on 2020, assess on 2021 independently, then fit the 2019–2021 cohort."""
    validation_fit = fit(data, date(2017, 1, 1), date(2019, 12, 31))
    validation = {name: evaluate(data, validation_fit, 2020, name) for name in MODEL_NAMES}
    selected = min(
        MODEL_NAMES,
        key=lambda name: (validation[name]["poisson_deviance"], MODEL_NAMES.index(name)),
    )
    assessment_fit = fit(data, date(2018, 1, 1), date(2020, 12, 31))
    assessment = {name: evaluate(data, assessment_fit, 2021, name) for name in MODEL_NAMES}
    final_fit = fit(data, date(2019, 1, 1), date(2021, 12, 31))
    groups = tuple(
        replace(group, rates=(group.overall_rate,) * 84) if selected == "overall_mean" else group
        for group in final_fit.groups
    )
    if not groups:
        raise ValueError("No individual businesses meet training support thresholds.")
    return groups, {
        "schema_version": 1,
        "status": "complete",
        "grain": "business_id_parent_day",
        "model_name": selected,
        "created_at": datetime.now(UTC).isoformat(),
        "import_revision": import_revision,
        "source_sha256": _source_sha256(data),
        "code_sha256": code_sha256(),
        "lock_sha256": lock_sha256(),
        "training_start": "2019-01-01",
        "training_end": "2021-12-31",
        "coverage_start": str(data.coverage_start),
        "coverage_end": str(data.coverage_end),
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
        "selection_rule": "minimum_mean_individual_daily_poisson_deviance_2020; mean_wins_ties",
        "weather_features": False,
        "source_categories_overlap": True,
        "validation_metrics": validation,
        "assessment_metrics": assessment,
        "final_businesses": len(groups),
        "limitations": (
            "Archived fixed businesses; calendar projection only. No verified future weather, "
            "growth, revenue, new-site demand or opening-success prediction. Source tags and "
            "coordinates are listing snapshot attributes; missing days inside coverage are zeros."
        ),
    }


def write_bundle(
    directory: Path, groups: tuple[BusinessGroup, ...], manifest: dict[str, Any]
) -> None:
    payload = json.dumps(
        [asdict(group) for group in groups], sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    directory.mkdir(parents=True, exist_ok=True)
    pending_payload, pending_manifest = (
        directory / "businesses.json.tmp",
        directory / "manifest.json.tmp",
    )
    pending_payload.write_bytes(payload)
    pending_manifest.write_text(
        json.dumps(
            {**manifest, "payload_sha256": hashlib.sha256(payload).hexdigest()},
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    )
    pending_payload.replace(directory / "businesses.json")
    pending_manifest.replace(directory / "manifest.json")


def train_from_database(directory: Path) -> dict[str, Any]:
    revision = repository.get_import_revision()
    groups, manifest = train(load_training_data(), revision)
    if repository.get_import_revision() != revision:
        raise ValueError("The import changed during training; retry after it finishes.")
    write_bundle(directory, groups, manifest)
    return manifest
