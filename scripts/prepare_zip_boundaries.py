"""Explicitly prepare the scoped Census ZCTA map asset; never run from the UI."""

import argparse
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from sitesense.database import connect
from sitesense.models import CITY_SCOPES
from sitesense.zip_boundaries import BoundaryDataError, validate_boundary_feature

SOURCE = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb/"
    "PUMA_TAD_TAZ_UGA_ZCTA/MapServer/1/query"
)
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "src/sitesense/assets/zip_boundaries.geojson"


def _source_zip_codes() -> list[str]:
    with connect() as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        codes = {
            row[0]
            for scope in CITY_SCOPES
            for row in connection.execute(
                """SELECT DISTINCT postal_code FROM sitesense.candidate_areas
                   WHERE city = %s AND state = %s ORDER BY postal_code""",
                (scope.city, scope.state),
            )
        }
    if not codes or any(not re.fullmatch(r"[0-9]{5}", code) for code in codes):
        raise BoundaryDataError("Import valid city ZIP areas before preparing boundaries.")
    return sorted(codes)


def _download(codes: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    features: list[dict[str, Any]] = []
    hashes = []
    for start in range(0, len(codes), 50):
        batch = codes[start : start + 50]
        query = urlencode(
            {
                "where": "ZCTA5 IN (" + ",".join(f"'{code}'" for code in batch) + ")",
                "outFields": "ZCTA5,GEOID",
                "returnGeometry": "true",
                "outSR": "4326",
                "geometryPrecision": "6",
                "maxAllowableOffset": "0.00005",
                "f": "geojson",
            }
        )
        for attempt in range(3):
            try:
                with urlopen(f"{SOURCE}?{query}", timeout=60) as response:
                    raw = response.read()
                break
            except (HTTPError, URLError, TimeoutError):
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
        hashes.append(hashlib.sha256(raw).hexdigest())
        payload = json.loads(raw)
        if payload.get("type") != "FeatureCollection" or payload.get("exceededTransferLimit"):
            raise BoundaryDataError("Census returned incomplete ZIP boundary data.")
        for feature in payload["features"]:
            code = feature["properties"]["ZCTA5"]
            prepared = {
                "type": "Feature",
                "properties": {"postal_code": code, "boundary_source": "Census 2020 ZCTA"},
                "geometry": feature["geometry"],
            }
            validate_boundary_feature(prepared)
            if code not in batch:
                raise BoundaryDataError("Census returned an unexpected ZIP label.")
            features.append(prepared)
    if len({feature["properties"]["postal_code"] for feature in features}) != len(features):
        raise BoundaryDataError("Census returned duplicate ZIP polygons.")
    if not features:
        raise BoundaryDataError("Census returned no matching ZIP polygons.")
    return features, hashes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    codes = _source_zip_codes()
    features, response_hashes = _download(codes)
    features.sort(key=lambda feature: feature["properties"]["postal_code"])
    found = {feature["properties"]["postal_code"] for feature in features}
    missing = sorted(set(codes) - found)
    payload = {
        "type": "FeatureCollection",
        "source": {
            "name": "U.S. Census Bureau TIGERweb, 2020 Census ZIP Code Tabulation Areas",
            "url": SOURCE,
            "vintage": "2020-01-01",
            "prepared_at": datetime.now(UTC).isoformat(),
            "coordinate_system": "WGS84 (EPSG:4326)",
            "geometry_precision": 6,
            "generalization_degrees": 0.00005,
            "requested_zip_codes": codes,
            "missing_zip_codes": missing,
            "response_sha256": response_hashes,
        },
        "features": features,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(f"Prepared {len(features)} ZIP polygons; {len(missing)} ZIPs have no Census ZCTA.")
    print(f"Asset SHA-256: {hashlib.sha256(args.output.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
