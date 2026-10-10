"""File provenance and locking shared by the import adapters."""

import hashlib
from pathlib import Path

IMPORT_LOCK = 1_892_575_310


def fingerprint(path: Path) -> dict[str, object]:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return {"path": str(path), "sha256": hasher.hexdigest()}
