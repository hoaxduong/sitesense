"""Read saved HGB evaluation evidence without a database or model deserialization."""

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from sitesense import business_forecast, business_hgb, business_research

RESEARCH_MODELS = {"overall_mean": "Daily average", "poisson": "Poisson", "hgb": "HGB"}


def error_reduction(reference: float, candidate: float) -> float | None:
    """Positive means a smaller error, not an increase in an accuracy percentage."""
    return 100 * (reference - candidate) / reference if reference > 0 else None


def scope_comparison(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    metrics = manifest["evaluation_metrics"]
    for base in metrics:
        if base["stage"] != "test" or base["model"] != "overall_mean":
            continue
        hgb = next(
            r
            for r in metrics
            if r["stage"] == "test"
            and r["model"] == "hgb"
            and all(r[key] == base[key] for key in ("city", "state", "category"))
        )
        rows.append(
            {
                "Group": f"{base['city']} · "
                + ("Restaurants" if base["category"] == "Restaurants" else "Day spas"),
                "Businesses": base["businesses"],
                "Business-days": base["observations"],
                "Average error score": base["poisson_deviance"],
                "HGB error score": hgb["poisson_deviance"],
                "Error reduction (%)": error_reduction(
                    base["poisson_deviance"], hgb["poisson_deviance"]
                ),
                "HGB has lower error": hgb["poisson_deviance"] < base["poisson_deviance"],
            }
        )
    return rows


def load_summary(directory: Path) -> dict[str, Any]:
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if (
        manifest.get("schema_version") != 1
        or manifest.get("status") != "complete"
        or manifest.get("model_name") != "hgb"
        or manifest.get("grain") != "business_id_parent_day"
        or manifest.get("features") != business_hgb.FEATURES
        or manifest.get("serving_policy") != business_hgb.POLICY
        or manifest.get("protocol") != business_research.PROTOCOL
        or manifest.get("code_sha256") != business_hgb.code_sha256()
        or manifest.get("lock_sha256") != business_forecast.lock_sha256()
        or manifest.get("versions") != business_hgb.versions()
        or manifest.get("payload_sha256")
        != hashlib.sha256((directory / "models.joblib").read_bytes()).hexdigest()
    ):
        raise ValueError("No compatible saved HGB evaluation")
    scores = []
    names = ("overall_mean", "month_weekday_shrunk", "hgb")
    labels = {
        "overall_mean": "Daily average",
        "month_weekday_shrunk": "Seasonal model",
        "hgb": "HGB — weather model",
    }
    metrics = manifest["evaluation_metrics"]
    for stage, period in (("validation", "2020: validation"), ("test", "2021: later check")):
        parts = [r for r in metrics if r["stage"] == stage]
        scope_counts: dict[tuple[str, str, str], dict[str, int]] = {}
        for row in parts:
            if (
                row["model"] not in names
                or not isinstance(row["observations"], int)
                or row["observations"] <= 0
                or any(
                    not math.isfinite(row[key]) or row[key] < 0
                    for key in ("mae", "poisson_deviance")
                )
            ):
                raise ValueError("Invalid saved evaluation scores")
            counts = scope_counts.setdefault((row["city"], row["state"], row["category"]), {})
            if row["model"] in counts:
                raise ValueError("Duplicate evaluation score")
            counts[row["model"]] = row["observations"]
        if not scope_counts or any(
            set(counts) != set(names) or len(set(counts.values())) != 1
            for counts in scope_counts.values()
        ):
            raise ValueError("Models need matching evaluation populations")
        for name in names:
            selected = [row for row in parts if row["model"] == name]
            observations = sum(row["observations"] for row in selected)
            scores.append(
                {
                    "Model": labels[name],
                    "Period": period,
                    "Business-days": observations,
                    **{
                        label: sum(row[key] * row["observations"] for row in selected)
                        / observations
                        for key, label in (
                            ("poisson_deviance", "Error score"),
                            ("mae", "Daily error"),
                        )
                    },
                }
            )
    return {"scores": scores, "manifest": manifest, "scopes": scope_comparison(manifest)}


def research_revision(root: Path) -> str:
    """Invalidate cached research summaries when any verified input changes."""
    directory = business_research.latest_run(root)
    digest = hashlib.sha256()
    visited = set()
    while True:
        directory = directory.resolve()
        if directory in visited:
            raise ValueError("Research lineage contains a cycle")
        visited.add(directory)
        for name in (
            "manifest.json",
            "metrics.csv",
            "predictions.csv",
            "business_metrics.csv",
            "ranking.csv",
            "catalog.json",
            "final_models.joblib",
        ):
            digest.update((directory / name).read_bytes())
        manifest = json.loads((directory / "manifest.json").read_bytes())
        if not (parent := manifest.get("parent_run")):
            break
        directory = Path(parent["directory"])
    digest.update(business_research._code_sha().encode())
    digest.update(business_forecast.lock_sha256().encode())
    return digest.hexdigest()


def load_research(root: Path, import_revision: str) -> dict[str, Any]:
    """Read three-model research evidence; verify artifacts without a live DB check.

    Source/catalog re-query is deliberately omitted for this offline guide. The run
    must match the app bundle's import revision, and the standard loader still checks
    code, lockfile, lineage and every payload hash. No estimators are deserialized.
    """
    run = business_research.load_run(business_research.latest_run(root), verify_source=False)
    if run.manifest["import_revision"] != import_revision:
        raise ValueError("Research and app evaluation need the same imported dataset revision.")
    return summarize_research(run)


def summarize_research(run: business_research.RunResult) -> dict[str, Any]:
    """Keep populations and target-day labels matched across the three displayed models."""
    matched = run.metrics.loc[
        run.metrics.population.eq("accepted_weather") & run.metrics.model.isin(RESEARCH_MODELS)
    ]
    scores = []
    for stage in ("validation", "test"):
        rows = matched.loc[matched.stage.eq(stage)]
        if (
            len(rows) != 3
            or set(rows.model) != set(RESEARCH_MODELS)
            or rows.observations.nunique() != 1
            or rows.businesses.nunique() != 1
            or rows.recorded_checkins.nunique() != 1
            or (rows.observations <= 0).any()
        ):
            raise ValueError("Three models need the same evaluation population.")
        reference = None
        for row in rows.itertuples():
            if any(
                not math.isfinite(v) or v < 0
                for v in (
                    row.poisson_deviance,
                    row.mae,
                    row.recorded_checkins,
                    row.predicted_checkins,
                )
            ):
                raise ValueError("Invalid research score")
            prediction = run.predictions.loc[
                run.predictions.stage.eq(stage)
                & run.predictions.model.eq(row.model)
                & run.predictions.population.eq("accepted_weather")
            ]
            labels = prediction.set_index(["business_id", "day"])["recorded_checkins"].sort_index()
            if not labels.index.is_unique or len(labels) != row.observations:
                raise ValueError("Missing or duplicate research prediction keys")
            if reference is None:
                reference = labels
            elif not reference.equals(labels):
                raise ValueError("Research models must use identical business/day labels.")
            scores.append(
                {
                    "Model": RESEARCH_MODELS[row.model],
                    "Stage": stage,
                    "Period": "2020 validation" if stage == "validation" else "2021 later check",
                    "Error score": float(row.poisson_deviance),
                    "Daily error": float(row.mae),
                    "Recorded check-ins": int(row.recorded_checkins),
                    "Estimated check-ins": float(row.predicted_checkins),
                    "Business-days": int(row.observations),
                    "Businesses": int(row.businesses),
                }
            )
    # Show actual held-out patterns, never predictions from the final training model.
    testing = run.predictions.loc[
        run.predictions.stage.eq("test")
        & run.predictions.model.isin(RESEARCH_MODELS)
        & run.predictions.population.eq("accepted_weather")
    ].copy()
    testing["Month"] = testing.day.astype(str).str[:7]
    monthly = []
    for (model, month), frame in testing.groupby(["model", "Month"]):
        monthly.append(
            {
                "Month": month,
                "Series": RESEARCH_MODELS[model],
                "Check-ins": float(frame.predicted_checkins.sum()),
            }
        )
        if model == "overall_mean":
            monthly.append(
                {
                    "Month": month,
                    "Series": "Recorded check-ins",
                    "Check-ins": float(frame.recorded_checkins.sum()),
                }
            )
    validation = [s for s in scores if s["Stage"] == "validation"]
    return {
        "scores": scores,
        "monthly": monthly,
        "manifest": run.manifest,
        "directory": str(run.directory),
        "weather_choice": min(
            (s for s in validation if s["Model"] != "Daily average"), key=lambda s: s["Error score"]
        )["Model"],
    }
