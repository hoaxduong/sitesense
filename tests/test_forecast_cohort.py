"""Cohort provenance and exact membership prevent misleading joined chart rates."""

import csv
import hashlib
import io
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from sitesense import forecast_cohort, repository
from sitesense.models import Business, CandidateArea, Catalog, DateCoverage, Filters

FILTERS = Filters("Philadelphia", "PA", "Restaurants", date(2022, 1, 19), date(2022, 1, 26))
AREA = CandidateArea(7, "Philadelphia", "PA", "19147", "Queen Village", 40, -75)
OTHER = CandidateArea(8, "Philadelphia", "PA", "19106", "Old City", 40, -75)
GROUP = "Philadelphia|PA|philadelphia|19147|Restaurants"
OTHER_GROUP = "Philadelphia|PA|philadelphia|19106|Restaurants"


def _csv(rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _business(identifier: str, area: CandidateArea = AREA) -> Business:
    return Business(
        identifier,
        area.id,
        identifier,
        area.city,
        area.state,
        area.postal_code,
        area.latitude,
        area.longitude,
        ("Restaurants",),
        4,
        10,
        True,
        None,
    )


def _catalog() -> Catalog:
    return Catalog(
        (AREA, OTHER),
        (
            _business("member-a"),
            _business("member-b"),
            _business("snapshot-only"),
            _business("member-c", OTHER),
        ),
        (),
        DateCoverage(date(2010, 1, 1), date(2022, 1, 19)),
    )


@pytest.fixture
def artifact(tmp_path: Path) -> tuple[Path, Path]:
    directory, source = tmp_path / "model", tmp_path / "run"
    directory.mkdir()
    source.mkdir()
    areas = [
        dict(
            group_id=group,
            area_id=group.rsplit("|", 1)[0],
            metro="Philadelphia",
            city="philadelphia",
            state="PA",
            zip=code,
            category="Restaurants",
            n_businesses=count,
        )
        for group, code, count in ((GROUP, "19147", "2"), (OTHER_GROUP, "19106", "1"))
    ]
    locations = [
        {key: value for key, value in row.items() if key != "n_businesses"} for row in areas
    ]
    members = [
        dict(business_id=identifier, group_id=group, category="Restaurants")
        for identifier, group in (
            ("member-a", GROUP),
            ("member-b", GROUP),
            ("member-c", OTHER_GROUP),
        )
    ]
    outputs: dict[str, str] = {}
    for name, raw in (
        ("model.joblib", b"fake-weight-fixture"),
        ("locations.csv", _csv(locations)),
        ("baseline.csv", b"baseline"),
        ("weather_daily.csv", b"weather"),
    ):
        (directory / name).write_bytes(raw)
        outputs[f"artifact/{name}"] = _sha(raw)
    for name, raw in (("final_cohort.csv", _csv(members)), ("final_areas.csv", _csv(areas))):
        (source / name).write_bytes(raw)
        outputs[name] = _sha(raw)
    metadata: dict[str, Any] = dict(
        status="complete",
        source_hashes={"business": "source-business", "checkin": "source-checkin"},
        final_catalog={"cohort_business_category_rows": 3},
    )
    parent = dict(metadata, final_training_end="2022-01-18", output_sha256=outputs)
    model = dict(
        metadata,
        schema_version=1,
        model_name="hgb_weather",
        trained_end="2022-01-18",
        cohort_selection_end="2022-01-18",
        artifact_sha256={
            name: outputs[f"artifact/{name}"]
            for name in ("model.joblib", "locations.csv", "baseline.csv", "weather_daily.csv")
        },
    )
    (source / "manifest.json").write_text(json.dumps(parent))
    (directory / "manifest.json").write_text(json.dumps(model))
    return directory, source


def _prepared(artifact: tuple[Path, Path]) -> Path:
    directory, source = artifact
    forecast_cohort.prepare_reference(source, directory)
    return directory


def _load(directory: Path, catalog: Catalog | None = None) -> forecast_cohort.CohortReference:
    return forecast_cohort.load_reference(directory, FILTERS, catalog or _catalog(), (AREA, OTHER))


def test_reference_keeps_exact_original_members_and_denominator(
    artifact: tuple[Path, Path],
) -> None:
    directory = _prepared(artifact)
    reference = _load(directory)
    assert reference.reason is None
    assert reference.denominators == {7: 2, 8: 1}
    assert reference.by_area[7].business_ids == ("member-a", "member-b")
    assert reference.business_ids == ("member-a", "member-b", "member-c")
    assert "snapshot-only" not in reference.business_ids


def test_preparation_copies_original_bytes_and_preserves_model(artifact: tuple[Path, Path]) -> None:
    directory, source = artifact
    before = {name: (directory / name).read_bytes() for name in ("manifest.json", "model.joblib")}
    output = forecast_cohort.prepare_reference(source, directory)
    assert (output / "parent_manifest.json").read_bytes() == (source / "manifest.json").read_bytes()
    for name in ("final_cohort.csv", "final_areas.csv"):
        assert (output / name).read_bytes() == (source / name).read_bytes()
    for name, raw in before.items():
        assert (directory / name).read_bytes() == raw


@pytest.mark.parametrize("name", ["final_cohort.csv", "final_areas.csv"])
def test_prepare_rejects_tampered_original_reference(
    artifact: tuple[Path, Path], name: str
) -> None:
    directory, source = artifact
    (source / name).write_bytes((source / name).read_bytes() + b"altered")
    with pytest.raises(ValueError):
        forecast_cohort.prepare_reference(source, directory)
    assert not (directory / "cohort").exists()


@pytest.mark.parametrize(
    "name", ["model.joblib", "locations.csv", "baseline.csv", "weather_daily.csv"]
)
def test_changed_current_model_artifact_rejects_cohort(
    artifact: tuple[Path, Path], name: str
) -> None:
    directory = _prepared(artifact)
    before = forecast_cohort.artifact_revision(directory)
    (directory / name).write_bytes(b"stale or replacement artifact")
    reference = _load(directory)
    assert reference.reason
    assert reference.denominators == {}
    assert forecast_cohort.artifact_revision(directory) != before


@pytest.mark.parametrize("kind", ["source_hashes", "status", "training", "artifact_hash"])
def test_parent_provenance_must_match_current_model(artifact: tuple[Path, Path], kind: str) -> None:
    directory = _prepared(artifact)
    path = directory / "cohort" / "parent_manifest.json"
    parent = json.loads(path.read_bytes())
    if kind == "source_hashes":
        parent["source_hashes"]["checkin"] = "other-source"
    elif kind == "status":
        parent["status"] = "running"
    elif kind == "training":
        parent["final_training_end"] = "2022-01-17"
    else:
        parent["output_sha256"]["artifact/model.joblib"] = "other-model"
    path.write_text(json.dumps(parent))
    assert _load(directory).business_ids == ()


@pytest.mark.parametrize("kind", ["missing", "moved", "category", "zip", "state"])
def test_changed_catalog_member_invalidates_only_its_area(
    artifact: tuple[Path, Path], kind: str
) -> None:
    directory = _prepared(artifact)
    catalog = _catalog()
    member = catalog.businesses[0]
    changes: dict[str, Any] = {
        "moved": {"area_id": 999},
        "category": {"categories": ("Day Spas",)},
        "zip": {"postal_code": "19106"},
        "state": {"state": "NJ"},
    }
    businesses = (
        catalog.businesses[1:]
        if kind == "missing"
        else (replace(member, **changes[kind]), *catalog.businesses[1:])
    )
    reference = _load(directory, replace(catalog, businesses=businesses))
    assert reference.by_area[7].n_businesses == 0
    assert reference.by_area[7].reason
    assert reference.denominators == {8: 1}


def test_scope_requires_exact_category_city_state_and_unique_zip(
    artifact: tuple[Path, Path],
) -> None:
    directory = _prepared(artifact)
    for filters in (
        replace(FILTERS, category="Day Spas"),
        replace(FILTERS, city="Tampa"),
        replace(FILTERS, state="NJ"),
    ):
        assert (
            forecast_cohort.load_reference(directory, filters, _catalog(), (AREA,)).business_ids
            == ()
        )
    duplicate = replace(AREA, id=999)
    assert (
        forecast_cohort.load_reference(
            directory, FILTERS, _catalog(), (AREA, duplicate)
        ).business_ids
        == ()
    )
    neighbor = replace(AREA, city="Nearby town")
    assert (
        forecast_cohort.load_reference(directory, FILTERS, _catalog(), (neighbor,)).business_ids
        == ()
    )


def test_missing_reference_is_safe_and_no_snapshot_fallback(tmp_path: Path) -> None:
    reference = _load(tmp_path)
    assert reference.reason
    assert reference.denominators == {}
    assert reference.business_ids == ()
    assert str(tmp_path) not in reference.reason


def test_modified_reference_changes_cache_revision_and_is_rejected(
    artifact: tuple[Path, Path],
) -> None:
    directory = _prepared(artifact)
    before = forecast_cohort.artifact_revision(directory)
    path = directory / "cohort" / "final_cohort.csv"
    path.write_bytes(path.read_bytes() + b"corrupted")
    assert forecast_cohort.artifact_revision(directory) != before
    assert _load(directory).reason


def test_verified_hash_but_duplicate_membership_is_rejected(artifact: tuple[Path, Path]) -> None:
    directory = _prepared(artifact)
    path = directory / "cohort" / "final_cohort.csv"
    raw = path.read_bytes()
    raw += raw.splitlines(keepends=True)[1]
    path.write_bytes(raw)
    parent_path = directory / "cohort" / "parent_manifest.json"
    parent = json.loads(parent_path.read_bytes())
    parent["output_sha256"]["final_cohort.csv"] = _sha(raw)
    parent_path.write_text(json.dumps(parent))
    assert _load(directory).reason


def test_repository_cohort_filter_is_parameterized_and_keeps_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, Any]] = []

    class Connection:
        def execute(self, query: str, parameters: Any) -> "Connection":
            calls.append((query, parameters))
            return self

        def fetchall(self) -> list[Any]:
            return [(7, date(2022, 1, 19), 12, 4)]

    @contextmanager
    def connect() -> Any:
        yield Connection()

    monkeypatch.setattr(repository, "connect", connect)
    identifiers = ("member-a' OR TRUE", "member-b")
    result = repository.load_activity(FILTERS, business_ids=identifiers)
    assert result[0].checkin_count == 4
    query, parameters = calls[0]
    assert "b.business_id = ANY(%s::text[])" in query
    assert identifiers[0] not in query
    assert parameters == (
        "Philadelphia",
        "PA",
        "Restaurants",
        date(2022, 1, 19),
        date(2022, 1, 26),
        date(2021, 1, 19),
        date(2021, 1, 26),
        list(identifiers),
    )
    repository.load_activity(FILTERS, include_previous_year=False)
    query, parameters = calls[1]
    assert "ANY" not in query
    assert parameters[-2:] == (FILTERS.start_date, FILTERS.end_date)


def test_repository_empty_cohort_never_queries_database(monkeypatch: pytest.MonkeyPatch) -> None:
    def connect() -> Any:
        raise AssertionError("Empty cohort must not load all snapshot activity")

    monkeypatch.setattr(repository, "connect", connect)
    assert repository.load_activity(FILTERS, business_ids=()) == ()
