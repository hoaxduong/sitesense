"""Explicit HGB fitting and fair calendar-baseline evaluation for every imported scope."""

import hashlib
import json
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import joblib  # type: ignore[import-untyped]

from sitesense import business_forecast, business_hgb, business_training, repository
from sitesense import business_research as research
from sitesense.models import BUSINESS_CATEGORY_LABELS, CITY_SCOPES


def fit_scope(sources: research.ResearchSources, city: str, state: str, category: str) -> Any:
    """Use all eligible businesses, in bounded identity batches rather than a sampled niche."""
    models, normals, counts = [], {}, {}
    metrics: list[dict[str, Any]] = []
    for stage in ("validation", "test", "final"):
        dates = [date.fromisoformat(value) for value in research.PROTOCOL[stage]]
        fitted = business_training.fit(sources.training, dates[0], dates[1])
        eligible = tuple(
            g for g in fitted.groups if (g.city, g.state, g.category) == (city, state, category)
        )
        supported = 0
        for offset in range(0, len(eligible), 128):
            batch = eligible[offset : offset + 128]
            training = research.build_panel(sources, batch, dates[0], dates[1])
            accepted = training.loc[training.weather_available]
            # No weather observations means unknown, never an invented zero or mean fallback.
            identifiers = set(accepted.business_id)
            batch = tuple(g for g in batch if g.business_id in identifiers)
            if not batch or accepted.recorded_checkins.sum() <= 0:
                continue
            model = research.fit_model("hgb", batch, training)
            supported += len(batch)
            if stage == "final":
                models.append(model)
                monthly = accepted.groupby(["business_id", "month"])[
                    research.WEATHER_COLUMNS
                ].mean()
                for group in batch:
                    normals[group.business_id, group.category] = tuple(
                        tuple(float(v) for v in monthly.loc[group.business_id, month])
                        if (group.business_id, month) in monthly.index
                        else None
                        for month in range(1, 13)
                    )
                continue
            assess = research.build_panel(sources, batch, dates[2], dates[3])
            assess = assess.loc[assess.weather_available].copy()
            if assess.empty:
                continue
            for name in ("overall_mean", "month_weekday_shrunk", "hgb"):
                candidate = model if name == "hgb" else research.fit_model(name, batch, training)
                assess["predicted_checkins"] = research.predict(candidate, assess)
                metrics.append({"stage": stage, "model": name, **research._scores(assess)})
        counts[stage] = {"eligible": len(eligible), "with_training_weather": supported}
    # Aggregate error sums on identical accepted business/day rows for all candidates.
    totals = []
    for stage in ("validation", "test"):
        for name in ("overall_mean", "month_weekday_shrunk", "hgb"):
            parts = [r for r in metrics if r["stage"] == stage and r["model"] == name]
            observations = sum(r["observations"] for r in parts)
            if not observations:
                continue
            totals.append(
                {
                    "stage": stage,
                    "model": name,
                    "businesses": sum(r["businesses"] for r in parts),
                    "observations": observations,
                    **{
                        metric: sum(r[metric] * r["observations"] for r in parts) / observations
                        for metric in ("poisson_deviance", "mae")
                    },
                }
            )
    return tuple(models), normals, totals, counts


def train_from_database(directory: Path) -> dict[str, Any]:
    revision = repository.get_import_revision()
    code_hash, lock_hash = business_hgb.code_sha256(), business_forecast.lock_sha256()
    models, normals, scopes = [], {}, []
    evaluations: list[dict[str, Any]] = []
    source_hashes = {}
    for scope in CITY_SCOPES:
        for category in BUSINESS_CATEGORY_LABELS:
            config = research.ExperimentConfig(
                city=scope.city, state=scope.state, category=category, source_category=None
            )
            sources = research.load_sources(config)
            if sources.import_revision != revision:
                raise ValueError("Source import changed during HGB training.")
            print(f"Training HGB: {scope.city} / {category}", flush=True)
            fitted, monthly, scores, counts = fit_scope(sources, scope.city, scope.state, category)
            models.extend(fitted)
            normals.update(monthly)
            context = {"city": scope.city, "state": scope.state, "category": category}
            evaluations.extend({**context, **score} for score in scores)
            scopes.append({**context, "counts": counts})
            source_hashes[f"{scope.city}/{category}"] = research._signatures(sources)
    if (
        not models
        or repository.get_import_revision() != revision
        or business_hgb.code_sha256() != code_hash
        or business_forecast.lock_sha256() != lock_hash
    ):
        raise ValueError("No supported HGB models, or source/code/lockfile changed during fitting.")
    output = BytesIO()
    joblib.dump({"models": tuple(models), "normals": normals}, output)
    payload = output.getvalue()
    manifest = {
        "schema_version": 1,
        "status": "complete",
        "model_name": "hgb",
        "grain": "business_id_parent_day",
        "features": business_hgb.FEATURES,
        "serving_policy": business_hgb.POLICY,
        "training_start": "2019-01-01",
        "training_end": "2021-12-31",
        "created_at": datetime.now(UTC).isoformat(),
        "versions": business_hgb.versions(),
        "code_sha256": code_hash,
        "lock_sha256": lock_hash,
        "import_revision": revision,
        "source_hashes": source_hashes,
        "final_businesses": len(normals),
        "model_parameters": models[0].parameters,
        "protocol": research.PROTOCOL,
        "scopes": scopes,
        "evaluation_metrics": evaluations,
        "weather_scenario": "2019-2021 accepted station/month means or explicit user input",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "models.joblib.tmp").write_bytes(payload)
    (directory / "manifest.json.tmp").write_text(
        json.dumps(manifest, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    (directory / "models.joblib.tmp").replace(directory / "models.joblib")
    (directory / "manifest.json.tmp").replace(directory / "manifest.json")
    return manifest
