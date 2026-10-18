"""Scientific charts preserve cohorts, daily zeros and honest sample/interval counts."""

from datetime import date, timedelta

import pytest

from sitesense import scientific
from sitesense.models import ActivityRecord, CandidateArea

AREA = CandidateArea(1, "Philadelphia", "PA", "19147", "Queen Village", 40, -75)
OTHER = CandidateArea(2, "Philadelphia", "PA", "19106", "Old City", 40, -75)


def test_daily_zeros_weather_missingness_and_exact_denominator_are_independent() -> None:
    rows = scientific.recorded_rows(
        (ActivityRecord(1, date(2021, 1, 1), 14, 4), ActivityRecord(1, date(2020, 1, 1), 14, 999)),
        (AREA, OTHER),
        date(2021, 1, 1),
        date(2021, 1, 2),
        {"s": {date(2021, 1, 1): {"pcpn": 0.0, "tmax": 5.0}}},
        {1: {"s"}, 2: {"missing"}},
        {1: 2, 2: 4},
    )
    assert len(rows) == 4
    assert rows[0]["Activity"] == 200
    assert rows[0]["Cohort businesses"] == 2
    assert rows[1]["Activity"] == 0
    assert rows[1]["Precipitation mm"] is None
    assert len(scientific.paired(rows, "Precipitation")) == 1
    assert scientific.paired(rows, "Precipitation")[0]["Weather value"] == 0
    assert all(row["Activity source"] == "Verified model cohort" for row in rows)


def test_missing_one_cohort_denominator_keeps_all_zip_counts_in_same_raw_unit() -> None:
    rows = scientific.recorded_rows(
        (ActivityRecord(1, date(2021, 1, 1), 14, 4),),
        (AREA, OTHER),
        date(2021, 1, 1),
        date(2021, 1, 1),
        {},
        {},
        {1: 2},
    )
    assert [row["Activity"] for row in rows] == [4, 0]
    assert all(row["Cohort businesses"] is None for row in rows)
    assert all(row["Activity source"] == "Entire ZIP" for row in rows)


@pytest.mark.parametrize(
    "value,group", [(0, "Dry"), (0.1, "0–5 mm"), (5, "5–15 mm"), (15, "15+ mm")]
)
def test_rain_groups_keep_dry_and_boundary_days(value: float, group: str) -> None:
    assert scientific.weather_group(value, "Precipitation")[1] == group


def _rows(count: int, constant: bool = False) -> list[dict[str, object]]:
    return [
        {
            "Date": (date(2021, 1, 4) + timedelta(days=i)).isoformat(),
            "ZIP area": AREA.label,
            "Activity": 3.0 if constant else float(i % 9),
            "Precipitation mm": 0.0,
            "Cohort businesses": 2,
        }
        for i in range(count)
    ]


def test_sparse_boxes_are_points_and_quartiles_keep_true_outliers() -> None:
    sparse = scientific.box_rows(_rows(7), "Precipitation")  # type: ignore[arg-type]
    assert len(sparse) == 7
    assert all(row["Kind"] == "Point" and row["Days"] == 7 for row in sparse)
    rows = _rows(12, constant=True)
    rows[-1]["Activity"] = 100
    result = scientific.box_rows(rows, "Precipitation")  # type: ignore[arg-type]
    box = next(row for row in result if row["Kind"] == "Box")
    assert box["Median"] == box["Q1"] == box["Q3"] == 3
    assert box["Whisker high"] == 3
    assert box["Days"] == 12
    assert [r["Activity"] for r in result if r["Kind"] == "Point"] == [100]


def test_week_cluster_intervals_are_reproducible_and_need_enough_weeks() -> None:
    rows = _rows(56, constant=True)
    result = scientific.weather_trend(rows, "Precipitation")  # type: ignore[arg-type]
    assert len(result) == 1
    assert result[0]["Days"] == 56 and result[0]["Weeks"] == 8
    assert result[0]["Lower"] == result[0]["Upper"] == result[0]["Activity"] == 3
    assert result == scientific.weather_trend(rows, "Precipitation")  # type: ignore[arg-type]
    sparse = scientific.weather_trend(_rows(9), "Precipitation")  # type: ignore[arg-type]
    assert sparse[0]["Lower"] is sparse[0]["Upper"] is None
    assert scientific.weather_trend(_rows(7), "Precipitation") == []  # type: ignore[arg-type]


def test_calendar_keeps_zip_identity_sample_counts_and_zero_activity() -> None:
    rows = _rows(14, constant=True)
    rows.extend([{"Date": "2021-01-04", "ZIP area": OTHER.label, "Activity": 0.0}])
    data = scientific.calendar_rows(rows)  # type: ignore[arg-type]
    other = next(row for row in data if row["ZIP area"] == OTHER.label)
    assert other["Activity"] == 0 and other["Days"] == 1
    first = next(row for row in data if row["ZIP area"] == AREA.label and row["Weekday"] == "Mon")
    assert first["Activity"] == 3 and first["Days"] == 2
