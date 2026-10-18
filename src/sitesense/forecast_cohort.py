"""Verified training-cohort membership for comparable recorded activity rates.

The current catalog is a snapshot, so its business count is never a model
normalization denominator. This reference preserves the exact original cohort.
Preparing it is an explicit local step and never trains or modifies the model.
"""

import csv
import hashlib
import io
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sitesense.models import CandidateArea, Catalog, Filters

_MODEL_FILES = ("model.joblib", "locations.csv", "baseline.csv", "weather_daily.csv")
_REFERENCE_FILES = ("final_cohort.csv", "final_areas.csv")
_UNAVAILABLE = "Verified model cohort unavailable; recorded and forecast rates cannot be joined."


@dataclass(frozen=True)
class CohortArea:
    area_id: int
    business_ids: tuple[str, ...]
    n_businesses: int
    reason: str | None = None


@dataclass(frozen=True)
class CohortReference:
    areas: tuple[CohortArea, ...]
    reason: str | None = None

    @property
    def by_area(self) -> dict[int, CohortArea]:
        return {area.area_id: area for area in self.areas}

    @property
    def business_ids(self) -> tuple[str, ...]:
        return tuple(sorted({member for area in self.areas for member in area.business_ids}))

    @property
    def denominators(self) -> dict[int, int]:
        return {area.area_id: area.n_businesses for area in self.areas if area.n_businesses > 0}


def artifact_revision(directory: Path) -> str:
    """Fingerprint manifests/reference content and model-file identities for caches.

    Full model hashes are checked on reference load. File identities here ensure
    replacement or corruption of model files invalidates an already warm cache.
    """
    digest = hashlib.sha256()
    try:
        for path in (
            directory / "manifest.json",
            directory / "cohort" / "parent_manifest.json",
            *(directory / "cohort" / name for name in _REFERENCE_FILES),
        ):
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        for name in _MODEL_FILES:
            stat = (directory / name).stat()
            digest.update(f"{name}:{stat.st_size}:{stat.st_mtime_ns}:{stat.st_ctime_ns}".encode())
    except OSError:
        digest.update(b"missing-reference")
    return digest.hexdigest()


def _json_object(raw: bytes) -> dict[str, Any]:
    result = json.loads(raw)
    if not isinstance(result, dict):
        raise ValueError("Invalid manifest")
    return result


def _csv_rows(raw: bytes, required: set[str]) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    if not required.issubset(reader.fieldnames or ()):
        raise ValueError("Invalid reference columns")
    rows = list(reader)
    if not rows or any(any(row.get(name) in (None, "") for name in required) for row in rows):
        raise ValueError("Incomplete reference")
    return rows


def _verified_files(
    directory: Path, reference_directory: Path, parent_name: str
) -> tuple[dict[str, bytes], list[dict[str, str]], list[dict[str, str]]]:
    model = _json_object((directory / "manifest.json").read_bytes())
    parent_raw = (reference_directory / parent_name).read_bytes()
    parent = _json_object(parent_raw)
    if (
        model.get("schema_version") != 1
        or model.get("status") != "complete"
        or model.get("model_name") != "hgb_weather"
        or parent.get("status") != "complete"
        or not isinstance(model.get("source_hashes"), dict)
        or not model["source_hashes"]
        or model["source_hashes"] != parent.get("source_hashes")
        or model.get("trained_end") != parent.get("final_training_end")
        or model.get("cohort_selection_end") != parent.get("final_training_end")
        or model.get("final_catalog") != parent.get("final_catalog")
    ):
        raise ValueError("Cohort provenance does not match model")
    outputs = parent["output_sha256"]
    for name in _MODEL_FILES:
        expected = model["artifact_sha256"][name]
        if (
            expected != outputs[f"artifact/{name}"]
            or hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected
        ):
            raise ValueError("Model artifact does not match cohort source")
    files = {"parent_manifest.json": parent_raw}
    for name in _REFERENCE_FILES:
        raw = (reference_directory / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != outputs[name]:
            raise ValueError("Cohort content does not match source manifest")
        files[name] = raw
    members = _csv_rows(files["final_cohort.csv"], {"business_id", "group_id", "category"})
    areas = _csv_rows(
        files["final_areas.csv"],
        {"group_id", "area_id", "metro", "city", "state", "zip", "category", "n_businesses"},
    )
    locations = _csv_rows(
        (directory / "locations.csv").read_bytes(),
        {"group_id", "area_id", "metro", "city", "state", "zip", "category"},
    )
    _validate_membership(members, areas, locations)
    return files, members, areas


def _validate_membership(
    members: list[dict[str, str]],
    areas: list[dict[str, str]],
    locations: list[dict[str, str]],
) -> None:
    groups = {row["group_id"]: row for row in areas}
    model_groups = {row["group_id"]: row for row in locations}
    if (
        len(groups) != len(areas)
        or len(model_groups) != len(locations)
        or groups.keys() != model_groups.keys()
    ):
        raise ValueError("Ambiguous cohort groups")
    group_members: dict[str, set[str]] = defaultdict(set)
    seen: set[tuple[str, str]] = set()
    for row in members:
        key = row["business_id"], row["category"]
        group = groups.get(row["group_id"])
        if group is None or key in seen or row["category"] != group["category"]:
            raise ValueError("Ambiguous cohort membership")
        seen.add(key)
        group_members[row["group_id"]].add(row["business_id"])
    for group_id, row in groups.items():
        if any(
            row[name] != model_groups[group_id][name]
            for name in ("area_id", "metro", "city", "state", "zip", "category")
        ) or len(group_members[group_id]) != int(row["n_businesses"]):
            raise ValueError("Cohort denominator does not match membership")
        if int(row["n_businesses"]) <= 0:
            raise ValueError("Empty cohort")


def prepare_reference(run_directory: Path, directory: Path) -> Path:
    """Copy verified original small reference files from one explicit completed run."""
    files, _, _ = _verified_files(directory, run_directory, "manifest.json")
    output = directory / "cohort"
    output.mkdir(parents=True, exist_ok=True)
    for name in (*_REFERENCE_FILES, "parent_manifest.json"):
        temporary = output / f".{name}.tmp"
        temporary.write_bytes(files[name])
        temporary.replace(output / name)
    return output


def verified_training_manifest(directory: Path) -> dict[str, Any]:
    """Return the parent manifest only after verifying its complete model/cohort chain."""
    files, _, _ = _verified_files(directory, directory / "cohort", "parent_manifest.json")
    return _json_object(files["parent_manifest.json"])


def load_reference(
    directory: Path,
    filters: Filters,
    catalog: Catalog,
    areas: tuple[CandidateArea, ...],
) -> CohortReference:
    """Match model groups to exact current ZIPs and validate every original member.

    An absent, moved, or recategorized member makes its area unavailable. It is
    never dropped from the denominator or replaced with a snapshot business.
    """
    try:
        _, members, cohort_areas = _verified_files(
            directory, directory / "cohort", "parent_manifest.json"
        )
    except (OSError, ValueError, KeyError, TypeError, OverflowError, csv.Error):
        return CohortReference(
            tuple(CohortArea(area.id, (), 0, _UNAVAILABLE) for area in areas), _UNAVAILABLE
        )
    scoped: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in cohort_areas:
        if (
            row["metro"] == filters.city
            and row["state"] == filters.state
            and row["city"].casefold() == filters.city.casefold()
            and row["category"] == filters.category
        ):
            scoped[(row["city"].casefold(), row["zip"])].append(row)
    candidates: dict[tuple[str, str], list[int]] = defaultdict(list)
    for area in areas:
        candidates[(area.city.casefold(), area.postal_code)].append(area.id)
    catalog_members = {business.business_id: business for business in catalog.businesses}
    by_group: dict[str, list[str]] = defaultdict(list)
    for member in members:
        by_group[member["group_id"]].append(member["business_id"])
    results: list[CohortArea] = []
    for area in areas:
        key = area.city.casefold(), area.postal_code
        matches = scoped.get(key, [])
        if area.state != filters.state or len(matches) != 1 or len(candidates[key]) != 1:
            results.append(
                CohortArea(area.id, (), 0, "No exact model cohort for this city, ZIP and industry.")
            )
            continue
        group = matches[0]
        ids = tuple(sorted(by_group[group["group_id"]]))
        if any(
            member not in catalog_members
            or catalog_members[member].area_id != area.id
            or catalog_members[member].state != area.state
            or catalog_members[member].postal_code != area.postal_code
            or filters.category not in catalog_members[member].categories
            for member in ids
        ):
            results.append(
                CohortArea(
                    area.id,
                    (),
                    0,
                    "Original model-cohort businesses are missing or changed in this import.",
                )
            )
        else:
            results.append(CohortArea(area.id, ids, int(group["n_businesses"])))
    return CohortReference(tuple(results))
