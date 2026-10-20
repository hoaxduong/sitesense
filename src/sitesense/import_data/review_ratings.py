"""Import dated star totals from the complete local Yelp review source explicitly.

The notebook's 10,000-row review sample is not a historical-rating source. This
adapter streams the full archive, retains no review text or user data, and uses a
temporary SQLite index to reject duplicate review IDs without growing Python RAM.
"""

import hashlib
import json
import math
import re
import sqlite3
import tarfile
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO, Any
from uuid import uuid4

import psycopg
from psycopg.rows import TupleRow
from psycopg.types.json import Jsonb

from sitesense.database import connect

from .shared import IMPORT_LOCK

IMPORT_KEY = "basic_app_review_ratings_v1"
BUSINESS_IMPORT_KEY = "basic_app_businesses_v1"
DEFAULT_SOURCE = Path("data/downloads/yelp.zip")
SAMPLE_SOURCE = Path("data/raw/yelp/review.jsonl")
TIME_POLICY = "assumed_source_local"
_REVIEW_MEMBER = "yelp_academic_dataset_review.json"
_BUSINESS_MEMBER = "yelp_academic_dataset_business.json"
_BATCH_SIZE = 10_000


class ReviewRatingImportError(ValueError):
    """A full review source or its business provenance could not be verified."""


@dataclass(frozen=True)
class BusinessBinding:
    import_revision: str
    source_sha256: str
    business_ids: frozenset[str]


@dataclass(frozen=True)
class ReviewRatingSnapshot:
    source: Path
    staging_path: Path
    binding: BusinessBinding
    source_sha256: str
    source_review_count: int
    matched_review_count: int
    daily_row_count: int
    matched_business_count: int
    start_date: date
    end_date: date


@dataclass(frozen=True)
class ReviewRatingImportSummary:
    source_review_count: int
    matched_review_count: int
    daily_row_count: int
    matched_business_count: int
    start_date: date
    end_date: date
    import_revision: str


def _business_binding(connection: psycopg.Connection[TupleRow]) -> BusinessBinding:
    rows = connection.execute(
        """SELECT id, metadata FROM sitesense.datasets
           WHERE metadata ->> 'import_key' = %s""",
        (BUSINESS_IMPORT_KEY,),
    ).fetchall()
    if len(rows) != 1:
        raise ReviewRatingImportError("Import the Yelp business catalog before dated ratings.")
    dataset_id, metadata = rows[0]
    try:
        revision = metadata["import_revision"]
        source_hash = metadata["source_fingerprints"]["businesses"]["sha256"]
    except (KeyError, TypeError):
        raise ReviewRatingImportError("The business catalog lacks source provenance.") from None
    if (
        metadata.get("status") != "complete"
        or not isinstance(revision, str)
        or not revision
        or not isinstance(source_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
    ):
        raise ReviewRatingImportError("The business catalog lacks completed source provenance.")
    business_ids = frozenset(
        row[0]
        for row in connection.execute(
            "SELECT business_id FROM sitesense.businesses WHERE dataset_id = %s", (dataset_id,)
        )
    )
    if not business_ids:
        raise ReviewRatingImportError("The imported business catalog is empty.")
    return BusinessBinding(revision, source_hash, business_ids)


def _hash_stream(source: IO[bytes]) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: source.read(1024 * 1024), b""):
        digest.update(block)
    return digest.hexdigest()


def _member_kind(name: str) -> str | None:
    basename = Path(name).name
    return {_REVIEW_MEMBER: "reviews", _BUSINESS_MEMBER: "businesses"}.get(basename)


@contextmanager
def _source_members(
    source: Path, business_source: Path | None
) -> Iterator[Iterator[tuple[str, IO[bytes]]]]:
    """Yield fixed-name source streams; never extract archive paths to disk."""
    if source.resolve() == SAMPLE_SOURCE.resolve():
        raise ReviewRatingImportError(
            "The notebook review.jsonl is a prefix sample; use the complete Yelp archive."
        )
    if not zipfile.is_zipfile(source):
        if business_source is None:
            raise ReviewRatingImportError("A full JSONL review source also needs --businesses.")

        def direct() -> Iterator[tuple[str, IO[bytes]]]:
            with business_source.open("rb") as business_handle:
                yield "businesses", business_handle
            with source.open("rb") as review_handle:
                yield "reviews", review_handle

        yield direct()
        return
    with zipfile.ZipFile(source) as archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        direct_members = [item for item in members if _member_kind(item.filename)]
        if direct_members:

            def zipped() -> Iterator[tuple[str, IO[bytes]]]:
                for item in direct_members:
                    kind = _member_kind(item.filename)
                    assert kind is not None
                    with archive.open(item) as handle:
                        yield kind, handle

            yield zipped()
            return
        nested = [
            item
            for item in members
            if not item.filename.startswith("__MACOSX/")
            and item.filename.lower().endswith((".tar", ".tar.gz", ".tgz"))
        ]
        if len(nested) != 1:
            raise ReviewRatingImportError("The Yelp ZIP must contain JSON tables or one TAR.")
        with archive.open(nested[0]) as binary:
            with tarfile.open(fileobj=binary, mode="r|*") as inner:

                def tarred() -> Iterator[tuple[str, IO[bytes]]]:
                    for item in inner:
                        kind = _member_kind(item.name)
                        if kind is None or not item.isfile():
                            continue
                        handle = inner.extractfile(item)
                        assert handle is not None
                        with handle:
                            yield kind, handle

                yield tarred()
            # Reach the enclosing ZIP member's EOF, including its CRC check.
            for _ in iter(lambda: binary.read(1024 * 1024), b""):
                pass


def _review_record(line: bytes) -> tuple[str, str, date, int]:
    try:
        value = json.loads(line)
        review_id, business_id = value["review_id"], value["business_id"]
        stars = value["stars"]
        timestamp = datetime.fromisoformat(value["date"])
        if (
            not isinstance(review_id, str)
            or not review_id.strip()
            or not isinstance(business_id, str)
            or not business_id.strip()
            or isinstance(stars, bool)
            or not isinstance(stars, (int, float))
            or not math.isfinite(stars)
            or stars != int(stars)
            or not 1 <= stars <= 5
            or timestamp.tzinfo is not None
        ):
            raise ValueError
        return review_id, business_id, timestamp.date(), int(stars)
    except (ValueError, TypeError, KeyError, OverflowError):
        raise ReviewRatingImportError(
            "A review has an invalid ID, source-local date, or integer star rating (1–5)."
        ) from None


def _flush_reviews(
    stage: sqlite3.Connection,
    review_ids: list[tuple[str]],
    daily: dict[tuple[str, str], list[int]],
) -> None:
    try:
        stage.executemany("INSERT INTO review_ids VALUES (?)", review_ids)
    except sqlite3.IntegrityError:
        raise ReviewRatingImportError("The full review source has duplicate review IDs.") from None
    stage.executemany(
        """INSERT INTO daily VALUES (?, ?, ?, ?)
           ON CONFLICT (business_id, review_date) DO UPDATE SET
             star_sum = star_sum + excluded.star_sum,
             review_count = review_count + excluded.review_count""",
        ((*key, *totals) for key, totals in daily.items()),
    )
    review_ids.clear()
    daily.clear()


def prepare_review_ratings(
    source: Path,
    binding: BusinessBinding,
    staging_path: Path,
    *,
    business_source: Path | None = None,
    progress: Callable[[int], None] | None = None,
) -> ReviewRatingSnapshot:
    """Validate every review to EOF and stage only daily totals for the catalog."""
    if staging_path.exists():
        raise ReviewRatingImportError("The temporary review staging path must be new.")
    if not binding.business_ids or not re.fullmatch(r"[0-9a-f]{64}", binding.source_sha256):
        raise ReviewRatingImportError("Dated ratings require a verified business binding.")
    count = matched_count = 0
    first_day: date | None = None
    last_day: date | None = None
    source_hash = ""
    business_hash = ""
    seen_members: set[str] = set()
    try:
        with closing(sqlite3.connect(staging_path)) as stage, stage:
            stage.execute("PRAGMA journal_mode = OFF")
            stage.execute("PRAGMA synchronous = OFF")
            stage.execute("PRAGMA cache_size = -32768")
            stage.execute("CREATE TABLE review_ids (review_id TEXT PRIMARY KEY) WITHOUT ROWID")
            stage.execute(
                """CREATE TABLE daily (business_id TEXT, review_date TEXT, star_sum INTEGER,
                   review_count INTEGER, PRIMARY KEY (business_id, review_date)) WITHOUT ROWID"""
            )
            with _source_members(source, business_source) as members:
                for kind, handle in members:
                    if kind in seen_members:
                        raise ReviewRatingImportError(
                            "The Yelp archive contains duplicate source tables."
                        )
                    seen_members.add(kind)
                    if kind == "businesses":
                        business_hash = _hash_stream(handle)
                        if business_hash != binding.source_sha256:
                            raise ReviewRatingImportError(
                                "The review archive's businesses differ from the imported catalog."
                            )
                        continue
                    digest = hashlib.sha256()
                    review_ids: list[tuple[str]] = []
                    daily: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])

                    for line in handle:
                        digest.update(line)
                        if not line.strip():
                            continue
                        review_id, business_id, day, stars = _review_record(line)
                        count += 1
                        review_ids.append((review_id,))
                        first_day = day if first_day is None else min(first_day, day)
                        last_day = day if last_day is None else max(last_day, day)
                        if business_id in binding.business_ids:
                            totals = daily[business_id, day.isoformat()]
                            totals[0] += stars
                            totals[1] += 1
                            matched_count += 1
                        if len(review_ids) >= _BATCH_SIZE:
                            _flush_reviews(stage, review_ids, daily)
                            if progress and count % 100_000 == 0:
                                progress(count)
                    _flush_reviews(stage, review_ids, daily)
                    source_hash = digest.hexdigest()
            if seen_members != {"reviews", "businesses"} or first_day is None or last_day is None:
                raise ReviewRatingImportError(
                    "The complete Yelp review and business tables are required."
                )
            daily_count = int(stage.execute("SELECT count(*) FROM daily").fetchone()[0])
            business_count = int(
                stage.execute("SELECT count(DISTINCT business_id) FROM daily").fetchone()[0]
            )
            stage.execute("DROP TABLE review_ids")
        return ReviewRatingSnapshot(
            source,
            staging_path,
            binding,
            source_hash,
            count,
            matched_count,
            daily_count,
            business_count,
            first_day,
            last_day,
        )
    except (OSError, UnicodeError, tarfile.TarError, zipfile.BadZipFile, EOFError) as error:
        staging_path.unlink(missing_ok=True)
        raise ReviewRatingImportError(
            "The complete Yelp source could not be read and verified."
        ) from error
    except Exception:
        staging_path.unlink(missing_ok=True)
        raise


def publish_review_ratings(snapshot: ReviewRatingSnapshot) -> ReviewRatingImportSummary:
    """Atomically replace only dated ratings after rechecking the business catalog."""
    revision = uuid4().hex
    metadata: dict[str, Any] = {
        "import_key": IMPORT_KEY,
        "status": "complete",
        "import_revision": revision,
        "prepared_at": datetime.now(UTC).isoformat(),
        "timestamp_policy": TIME_POLICY,
        "coverage": {
            "start_date": snapshot.start_date.isoformat(),
            "end_date": snapshot.end_date.isoformat(),
        },
        "business_import_revision": snapshot.binding.import_revision,
        "business_source_sha256": snapshot.binding.source_sha256,
        "source_fingerprints": {
            "reviews": {
                "path": str(snapshot.source),
                "sha256": snapshot.source_sha256,
                "available": True,
            },
            "businesses": {"sha256": snapshot.binding.source_sha256, "available": True},
        },
        "source_review_count": snapshot.source_review_count,
        "matched_review_count": snapshot.matched_review_count,
        "daily_row_count": snapshot.daily_row_count,
        "matched_business_count": snapshot.matched_business_count,
    }
    with connect() as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK,))
        if _business_binding(connection) != snapshot.binding:
            raise ReviewRatingImportError(
                "The business catalog changed; prepare dated ratings again."
            )
        old = connection.execute(
            "SELECT id FROM sitesense.datasets WHERE metadata ->> 'import_key' = %s FOR UPDATE",
            (IMPORT_KEY,),
        ).fetchall()
        if len(old) > 1:
            raise ReviewRatingImportError("The dated-rating dataset registry is ambiguous.")
        if old:
            dataset_id = int(old[0][0])
            connection.execute(
                "UPDATE sitesense.datasets SET metadata = %s, source_uri = %s WHERE id = %s",
                (Jsonb(metadata), str(snapshot.source), dataset_id),
            )
        else:
            inserted = connection.execute(
                """INSERT INTO sitesense.datasets (name, source_uri, version, metadata)
                   VALUES ('Yelp dated review ratings', %s, 'review-ratings-v1', %s)
                   RETURNING id""",
                (str(snapshot.source), Jsonb(metadata)),
            ).fetchone()
            assert inserted is not None
            dataset_id = int(inserted[0])
        connection.execute("DELETE FROM sitesense.review_rating_daily")
        copied = matched_count = 0
        with (
            closing(sqlite3.connect(snapshot.staging_path)) as stage,
            connection.cursor() as cursor,
            cursor.copy(
                """COPY sitesense.review_rating_daily
                   (business_id, review_date, star_sum, review_count, dataset_id) FROM STDIN"""
            ) as copy,
        ):
            for business_id, day, star_sum, review_count in stage.execute(
                """SELECT business_id, review_date, star_sum, review_count FROM daily
                   ORDER BY business_id, review_date"""
            ):
                copy.write_row((business_id, day, star_sum, review_count, dataset_id))
                copied += 1
                matched_count += review_count
        if copied != snapshot.daily_row_count or matched_count != snapshot.matched_review_count:
            raise ReviewRatingImportError("The staged dated-rating counts are inconsistent.")
    return ReviewRatingImportSummary(
        snapshot.source_review_count,
        snapshot.matched_review_count,
        snapshot.daily_row_count,
        snapshot.matched_business_count,
        snapshot.start_date,
        snapshot.end_date,
        revision,
    )


def import_review_ratings(
    source: Path = DEFAULT_SOURCE,
    *,
    business_source: Path | None = None,
    progress: Callable[[int], None] | None = None,
) -> ReviewRatingImportSummary:
    """Explicitly prepare the full local source, then publish one complete revision."""
    with connect() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        binding = _business_binding(connection)
    with tempfile.TemporaryDirectory(prefix="sitesense-review-ratings-") as temporary:
        snapshot = prepare_review_ratings(
            source,
            binding,
            Path(temporary) / "daily.sqlite3",
            business_source=business_source,
            progress=progress,
        )
        return publish_review_ratings(snapshot)
