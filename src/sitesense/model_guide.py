"""Read saved HGB evaluation evidence without a database or model deserialization."""

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from sitesense import business_forecast, business_hgb, business_research


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
    return {"scores": scores, "manifest": manifest}
