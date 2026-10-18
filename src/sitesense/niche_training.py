"""Explicit read-only training of exact niche/ZIP archived-cohort projections."""

import hashlib
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sitesense import repository
from sitesense.database import connect
from sitesense.models import BUSINESS_CATEGORY_LABELS, CITY_SCOPES
from sitesense.niche_forecast import (
    MIN_BUSINESSES,
    MIN_TRAINING_CHECKINS,
    MODEL_NAMES,
    PRIOR_BUSINESS_DAYS,
    NicheGroup,
    code_sha256,
    group_key,
    lock_sha256,
    membership_sha256,
)


@dataclass(frozen=True)
class TrainingBusiness:
    business_id: str
    city: str
    state: str
    area_id: int
    postal_code: str
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
    groups: tuple[NicheGroup, ...]
    members: dict[tuple[str, str, str, str, int], tuple[str, ...]]
    start: date
    end: date


def load_training_data() -> TrainingData:
    """Use canonical city membership and DB area IDs, without future-last-activity selection."""
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
            """SELECT b.business_id, b.canonical_city, b.canonical_state, b.area_id,
                      z.postal_code, z.latitude, z.longitude, b.categories,
                      min(a.activity_date)
               FROM sitesense.businesses AS b
               JOIN sitesense.candidate_areas AS z ON z.id = b.area_id
               JOIN sitesense.business_activity_hourly AS a USING (business_id)
               WHERE b.canonical_city = ANY(%s::text[])
                 AND (b.categories @> ARRAY['Restaurants']::text[]
                      OR b.categories @> ARRAY['Day Spas']::text[])
                 AND (z.city, z.state) = (b.canonical_city, b.canonical_state)
               GROUP BY b.business_id, z.postal_code, z.latitude, z.longitude
               ORDER BY b.business_id""",
            (cities,),
        ).fetchall()
        daily = connection.execute(
            """SELECT b.business_id, a.activity_date, sum(a.checkin_count)
               FROM sitesense.businesses AS b
               JOIN sitesense.candidate_areas AS z ON z.id = b.area_id
               JOIN sitesense.business_activity_hourly AS a USING (business_id)
               WHERE b.canonical_city = ANY(%s::text[])
                 AND (b.categories @> ARRAY['Restaurants']::text[]
                      OR b.categories @> ARRAY['Day Spas']::text[])
                 AND (z.city, z.state) = (b.canonical_city, b.canonical_state)
                 AND a.activity_date BETWEEN DATE '2017-01-01' AND DATE '2021-12-31'
               GROUP BY b.business_id, a.activity_date
               ORDER BY b.business_id, a.activity_date""",
            (cities,),
        ).fetchall()
    businesses = tuple(
        TrainingBusiness(
            row[0], row[1], row[2], row[3], row[4], row[5], row[6], tuple(row[7]), row[8]
        )
        for row in rows
        if (row[1], row[2]) in {(scope.city, scope.state) for scope in CITY_SCOPES}
    )
    return TrainingData(
        businesses,
        tuple(TrainingDay(*row) for row in daily),
        date.fromisoformat(coverage["start_date"]),
        date.fromisoformat(coverage["end_date"]),
    )


def _days(start: date, end: date) -> tuple[date, ...]:
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _cell(day: date) -> int:
    return (day.month - 1) * 7 + day.weekday()


def fit(data: TrainingData, start: date, end: date) -> Fit:
    """Rebuild eligibility and priors independently using only this fit's history."""
    if end < start or data.coverage_start > start or data.coverage_end < end:
        raise ValueError("The training interval must have complete imported activity coverage.")
    businesses = {business.business_id: business for business in data.businesses}
    if len(businesses) != len(data.businesses):
        raise ValueError("Duplicate training business IDs.")
    member_lists: dict[tuple[str, str, str, str, int], list[str]] = defaultdict(list)
    for business in data.businesses:
        if business.first_activity > start:
            continue
        for category in BUSINESS_CATEGORY_LABELS:
            if category not in business.categories:
                continue
            for source in sorted(set(business.categories) - {category}):
                member_lists[
                    (business.city, business.state, category, source, business.area_id)
                ].append(business.business_id)
    member_keys: dict[str, list[tuple[str, str, str, str, int]]] = defaultdict(list)
    for key, identifiers in member_lists.items():
        if len(identifiers) >= MIN_BUSINESSES:
            for identifier in identifiers:
                member_keys[identifier].append(key)
    totals: dict[tuple[str, str, str, str, int], list[int]] = defaultdict(lambda: [0] * 84)
    seen: set[tuple[str, date]] = set()
    for record in data.activity:
        if (
            not isinstance(record.checkins, int)
            or isinstance(record.checkins, bool)
            or record.checkins < 0
        ):
            raise ValueError("Training activity must contain nonnegative integer counts.")
        business_day = (record.business_id, record.day)
        if business_day in seen:
            raise ValueError("Duplicate business/day training records.")
        seen.add(business_day)
        if not start <= record.day <= end:
            continue
        for key in member_keys.get(record.business_id, ()):
            totals[key][_cell(record.day)] += record.checkins
    eligible = {
        key: tuple(sorted(member_lists[key]))
        for key, values in totals.items()
        if sum(values) >= MIN_TRAINING_CHECKINS
    }
    exposure = [0] * 84
    for day in _days(start, end):
        exposure[_cell(day)] += 1
    pooled_counts: dict[tuple[str, str, str, str], list[int]] = defaultdict(lambda: [0] * 84)
    pooled_businesses: dict[tuple[str, str, str, str], int] = defaultdict(int)
    for key, members in eligible.items():
        pooled_businesses[key[:4]] += len(members)
        for cell, count in enumerate(totals[key]):
            pooled_counts[key[:4]][cell] += count
    groups = []
    days = (end - start).days + 1
    for key, members in sorted(eligible.items()):
        business = businesses[members[0]]
        counts = totals[key]
        pool = key[:4]
        rates = tuple(
            (
                counts[cell]
                + PRIOR_BUSINESS_DAYS
                * pooled_counts[pool][cell]
                / (pooled_businesses[pool] * exposure[cell])
            )
            / (len(members) * exposure[cell] + PRIOR_BUSINESS_DAYS)
            for cell in range(84)
        )
        groups.append(
            NicheGroup(
                *key[:4],
                key[4],
                business.postal_code,
                business.latitude,
                business.longitude,
                len(members),
                sum(counts),
                sum(counts) / (len(members) * days),
                rates,
                membership_sha256(members),
            )
        )
    return Fit(tuple(groups), eligible, start, end)


def evaluate(data: TrainingData, fitted: Fit, year: int, model: str) -> dict[str, float | int]:
    """Score every covered held-out day, retaining real zeros for the fixed training cohort."""
    if model not in MODEL_NAMES:
        raise ValueError("Unknown calendar projection model.")
    start, end = date(year, 1, 1), date(year, 12, 31)
    if fitted.end >= start or data.coverage_start > start or data.coverage_end < end:
        raise ValueError("Assessment requires a fully covered year after the training cutoff.")
    member_keys: dict[str, list[tuple[str, str, str, str, int]]] = defaultdict(list)
    for key, members in fitted.members.items():
        for identifier in members:
            member_keys[identifier].append(key)
    counts: dict[tuple[tuple[str, str, str, str, int], date], int] = defaultdict(int)
    for record in data.activity:
        if start <= record.day <= end:
            for key in member_keys.get(record.business_id, ()):
                counts[key, record.day] += record.checkins
    deviance, absolute_error, observations = 0.0, 0.0, 0
    rankings: dict[tuple[str, str, str, str], list[tuple[int, float, float]]] = defaultdict(list)
    for group in fitted.groups:
        key = group_key(group)
        observed_sum, predicted_sum = 0.0, 0.0
        for day in _days(start, end):
            observed = counts[key, day] / group.n_businesses
            predicted = group.overall_rate if model == "overall_mean" else group.rates[_cell(day)]
            mu = max(predicted, 1e-12)
            deviance += 2 * (
                (observed * math.log(observed / mu) if observed else 0) - observed + mu
            )
            absolute_error += abs(observed - predicted)
            observed_sum += observed
            predicted_sum += predicted
            observations += 1
        rankings[key[:4]].append((group.area_id, observed_sum, predicted_sum))
    if not observations:
        raise ValueError("No supported groups for temporal assessment.")
    comparisons = [rows for rows in rankings.values() if len(rows) >= 2]
    agreements = sum(
        min(rows, key=lambda row: (-row[1], row[0]))[0]
        == min(rows, key=lambda row: (-row[2], row[0]))[0]
        for rows in comparisons
    )
    return {
        "poisson_deviance": max(0.0, deviance / observations),
        "mae": absolute_error / observations,
        "groups": len(fitted.groups),
        "observations": observations,
        "ranked_niches": len(comparisons),
        "top_zip_agreements": agreements,
        "top_zip_agreement": agreements / len(comparisons) if comparisons else 0.0,
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
) -> tuple[tuple[NicheGroup, ...], dict[str, Any]]:
    """Select on 2020 only, assess 2021 independently, then fit the archived 2019–2021 cohort."""
    validation_fit = fit(data, date(2017, 1, 1), date(2019, 12, 31))
    validation = {model: evaluate(data, validation_fit, 2020, model) for model in MODEL_NAMES}
    selected = min(
        MODEL_NAMES,
        key=lambda model: (validation[model]["poisson_deviance"], MODEL_NAMES.index(model)),
    )
    assessment_fit = fit(data, date(2018, 1, 1), date(2020, 12, 31))
    assessment = {model: evaluate(data, assessment_fit, 2021, model) for model in MODEL_NAMES}
    final_fit = fit(data, date(2019, 1, 1), date(2021, 12, 31))
    groups = tuple(
        replace(group, rates=(group.overall_rate,) * 84) if selected == "overall_mean" else group
        for group in final_fit.groups
    )
    if not groups:
        raise ValueError("No source-category/ZIP groups meet training support thresholds.")
    manifest = {
        "schema_version": 1,
        "status": "complete",
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
        "min_businesses": MIN_BUSINESSES,
        "min_training_checkins": MIN_TRAINING_CHECKINS,
        "prior_business_days": PRIOR_BUSINESS_DAYS,
        "cohort_rule": "first_recorded_activity_on_or_before_each_training_start; fixed_within_fit",
        "selection_rule": (
            "minimum_mean_daily_per_business_poisson_deviance_on_2020; overall_mean_wins_ties"
        ),
        "pool_rule": "same_canonical_city_state_parent_source_supported_training_cohorts",
        "target": "recorded_checkins_per_cohort_business_per_calendar_day",
        "weather_features": False,
        "source_categories_overlap": True,
        "validation_metrics": validation,
        "assessment_metrics": assessment,
        "final_groups": len(groups),
        "metric_weighting": "equal_group_calendar_days; counts_divided_by_fixed_cohort_businesses",
        "limitations": (
            "Archived fixed cohorts; calendar projection only. No verified future weather, "
            "market growth, revenue or opening-success prediction. Source tags and ZIP "
            "membership are listing snapshot attributes."
        ),
    }
    return groups, manifest


def write_bundle(directory: Path, groups: tuple[NicheGroup, ...], manifest: dict[str, Any]) -> None:
    payload = json.dumps(
        [asdict(group) for group in groups], sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    directory.mkdir(parents=True, exist_ok=True)
    # Manifest is replaced last, so concurrent readers reject incomplete pairings.
    pending_payload = directory / "groups.json.tmp"
    pending_manifest = directory / "manifest.json.tmp"
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
    pending_payload.replace(directory / "groups.json")
    pending_manifest.replace(directory / "manifest.json")


def train_from_database(directory: Path) -> dict[str, Any]:
    revision = repository.get_import_revision()
    data = load_training_data()
    groups, manifest = train(data, revision)
    if repository.get_import_revision() != revision:
        raise ValueError("The import changed during training; retry after the import finishes.")
    write_bundle(directory, groups, manifest)
    return manifest
