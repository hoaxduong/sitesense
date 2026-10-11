"""Load the packaged Census ZCTA polygons without runtime downloads."""

import json
import math
import re
from importlib.resources import files
from typing import Any


class BoundaryDataError(ValueError):
    """The packaged map boundaries cannot be used safely."""


def validate_boundary_feature(feature: Any) -> str:
    """Validate a closed WGS84 Polygon/MultiPolygon and return its ZIP label."""
    if not isinstance(feature, dict) or feature.get("type") != "Feature":
        raise BoundaryDataError("ZIP boundaries must be GeoJSON features.")
    properties = feature.get("properties")
    postal_code = properties.get("postal_code") if isinstance(properties, dict) else None
    if not isinstance(postal_code, str) or not re.fullmatch(r"[0-9]{5}", postal_code):
        raise BoundaryDataError("ZIP boundaries need five-digit ZIP labels.")
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise BoundaryDataError("ZIP boundaries must have polygon geometry.")
    coordinates = geometry.get("coordinates")
    polygons = [coordinates] if geometry["type"] == "Polygon" else coordinates
    if not isinstance(polygons, list) or not polygons:
        raise BoundaryDataError("ZIP boundaries must contain coordinates.")
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise BoundaryDataError("ZIP boundaries must contain polygon rings.")
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
                raise BoundaryDataError("ZIP polygon rings must be closed.")
            distinct = set()
            for point in ring:
                if (
                    not isinstance(point, list)
                    or len(point) != 2
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        for value in point
                    )
                    or not -180 <= point[0] <= 180
                    or not -90 <= point[1] <= 90
                ):
                    raise BoundaryDataError("ZIP coordinates must be WGS84 longitude/latitude.")
                distinct.add(tuple(point))
            if len(distinct) < 3:
                raise BoundaryDataError("ZIP polygon rings must enclose an area.")
    return postal_code


def load_zip_boundaries() -> dict[str, dict[str, Any]]:
    """Return immutable-source polygons keyed by ZIP; callers enrich copies."""
    path = files("sitesense").joinpath("assets", "zip_boundaries.geojson")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BoundaryDataError("The packaged ZIP boundaries are unavailable.") from error
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise BoundaryDataError("The packaged ZIP boundaries need a FeatureCollection.")
    features = payload.get("features")
    if not isinstance(features, list) or not features:
        raise BoundaryDataError("The packaged ZIP boundaries are empty.")
    boundaries = {}
    for feature in features:
        postal_code = validate_boundary_feature(feature)
        if postal_code in boundaries:
            raise BoundaryDataError("The packaged ZIP boundaries contain duplicate ZIP labels.")
        boundaries[postal_code] = feature
    return boundaries
