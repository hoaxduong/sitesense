"""Small source archives for import tests; no real research data needed."""

import json
from pathlib import Path

import pytest


@pytest.fixture
def yelp_archive(tmp_path: Path) -> Path:
    businesses = []
    for business_id, city, category in (
        ("selected", " TAMPA ", "Restaurants, Cafes"),
        ("no-checkins", "Tampa", "Restaurants"),
        ("other-category", "Tampa", "Shopping"),
        ("other-region", "Nashville", "Restaurants"),
    ):
        businesses.append(
            {
                "business_id": business_id,
                "name": "Example",
                "address": "1 Main St",
                "city": city,
                "state": "FL" if "tampa" in city.casefold() else "TN",
                "postal_code": "00000",
                "latitude": 28.0,
                "longitude": -82.5,
                "stars": 4.0,
                "review_count": 10,
                "is_open": 1,
                "categories": category,
            }
        )
    checkins = [
        {
            "business_id": "selected",
            "date": "2019-01-01 12:00:00, 2019-01-01 12:00:00, 2019-01-02 13:00:00",
        },
        {"business_id": "other-category", "date": "2019-01-01 14:00:00"},
        {"business_id": "other-region", "date": "2019-01-01 14:00:00"},
    ]
    for name, rows in (("business", businesses), ("checkin", checkins)):
        (tmp_path / f"yelp_academic_dataset_{name}.json").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    (tmp_path / "weather_cells.csv").write_text(
        "weather_cell_id,latitude,longitude\nera5_112_-330,28.0,-82.5\n", encoding="utf-8"
    )
    return tmp_path
