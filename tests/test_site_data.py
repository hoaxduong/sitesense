"""Site recommendation data layer: unit conversion, pure assembly and SQL integration."""

import itertools
import math
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from typing import Any

import numpy as np
import pytest

from sitesense import analytics, database, repository
from sitesense.models import DateCoverage, Filters
from sitesense.repository import DataUnavailableError, _StationReport
from sitesense.site_recommendation import data
from sitesense.site_recommendation.data import (
    AreaDayRow,
    AreaRow,
    BusinessRow,
    MonthlyRow,
    StationObsRow,
    assemble_scope_data,
    convert_station_value,
    covered_months,
    dominant_station,
    fahrenheit_to_celsius,
    haversine_km_vector,
    inches_to_millimetres,
    load_scope_data,
)
from sitesense.site_recommendation.models import ScopeKey

CENTER = (27.9506, -82.4572)
COVERAGE = DateCoverage(date(2010, 1, 16), date(2022, 1, 19))
JUNE = (date(2020, 6, 1), date(2020, 6, 30))


def _key(
    start: date = JUNE[0],
    end: date = JUNE[1],
    radius_km: float = 1.0,
    source: str | None = None,
) -> ScopeKey:
    return ScopeKey("Tampa", "FL", "Restaurants", source, start, end, radius_km, "rev")


def _area(
    area_id: int = 1,
    postal: str = "33601",
    lat: float | None = CENTER[0],
    lon: float | None = CENTER[1],
) -> AreaRow:
    return AreaRow(area_id, postal, f"Area {postal}", lat, lon)


def _business(
    business_id: str,
    area_id: int | None = 1,
    *,
    niche: bool = True,
    is_open: bool = True,
    lat: float | None = CENTER[0],
    lon: float | None = CENTER[1],
    stars: float | None = 4.0,
    reviews: int = 10,
    cell: str | None = None,
) -> BusinessRow:
    return BusinessRow(business_id, area_id, lat, lon, stars, reviews, is_open, cell, niche)


def _assemble(
    areas: list[AreaRow],
    businesses: list[BusinessRow],
    *,
    key: ScopeKey | None = None,
    coverage: DateCoverage = COVERAGE,
    monthly: list[MonthlyRow] | None = None,
    area_days: list[AreaDayRow] | None = None,
    obs: list[StationObsRow] | None = None,
    stations: frozenset[str] = frozenset(),
) -> Any:
    return assemble_scope_data(
        key or _key(),
        coverage,
        areas,
        businesses,
        monthly or [],
        area_days or [],
        obs or [],
        stations,
    )


# ---- units (TC-030 .. TC-035) ----


def test_tc030_fahrenheit_to_celsius() -> None:
    assert fahrenheit_to_celsius(89.6) == pytest.approx(32.0, abs=1e-9)
    assert fahrenheit_to_celsius(32) == 0.0
    assert fahrenheit_to_celsius(-4) == pytest.approx(-20.0, abs=1e-9)


def test_tc031_inches_to_millimetres() -> None:
    assert inches_to_millimetres(0.1) == pytest.approx(2.54, abs=1e-9)
    assert inches_to_millimetres(0.09) == pytest.approx(2.286, abs=1e-9)
    assert inches_to_millimetres(0) == 0.0


def test_tc032_convert_valid_tmax() -> None:
    value = convert_station_value("tmax", 89.6, "degreeF", "89.6", "", False)
    assert value == pytest.approx(32.0, abs=1e-9)
    assert convert_station_value("pcpn", 0.1, "inch", "0.1", "", False) == pytest.approx(2.54)


@pytest.mark.parametrize(
    "args",
    [
        ("tmax", 80.0, "degreeF", "80", "M", False),
        ("tmax", 80.0, "degreeC", "80", "", False),
        ("tmax", -9999.0, "degreeF", "-9999", "", False),
        ("tmax", -999.0, "degreeF", "-999", "", False),
        ("tmax", float("nan"), "degreeF", "NaN", "", False),
        ("tmax", None, "degreeF", "", "", False),
        ("pcpn", -0.1, "inch", "-0.1", "", False),
        ("pcpn", 0.5, "mm", "0.5", "", False),
        ("tmin", 50.0, "degreeF", "50", "", False),
    ],
)
def test_tc033_rejected_observations_are_missing_never_zero(args: tuple[Any, ...]) -> None:
    assert convert_station_value(*args) is None


@pytest.mark.parametrize(
    "args",
    [("pcpn", None, "inch", "T", "T", True), ("pcpn", 0.0, "inch", "", "", True)],
)
def test_tc034_trace_precipitation_is_a_dry_observation(args: tuple[Any, ...]) -> None:
    assert convert_station_value(*args) == 0.0


def test_tc035_policy_matches_repository() -> None:
    cases = list(
        itertools.product(
            ("tmax", "pcpn"),
            (None, 0.0, 0.5, -0.5, 89.6, -999.0, -9999.0, float("nan")),
            ("degreeF", "inch", "degreeC"),
            ("", "T", "0.5"),
            ("", "T", "A", " t "),
            (False, True),
        )
    )
    assert len(cases) > 20
    for variable, value, unit, raw, flag, trace in cases:
        report = _StationReport("s", date(2020, 1, 1), variable, value, unit, raw, flag, trace)
        expected = repository._accepted_station_value(report)
        actual = convert_station_value(variable, value, unit, raw, flag, trace)
        if expected is None:
            assert actual is None, report
        else:
            assert actual == pytest.approx(expected, abs=1e-9), report


# ---- dominant station, months, distance ----


def test_tc046_dominant_station() -> None:
    eligible = frozenset({"a", "b"})
    assert dominant_station(["a", "b", "b", "c", None, "a", "b"], eligible) == "b"
    assert dominant_station(["b", "a", "a", "b"], eligible) == "a"
    assert dominant_station(["c", None], eligible) is None
    assert dominant_station([], eligible) is None


def test_tc111_covered_months_drop_partial_months() -> None:
    months = covered_months(date(2020, 1, 15), date(2020, 12, 10))
    assert months == tuple(date(2020, month, 1) for month in range(2, 12))
    assert covered_months(date(2020, 12, 1), date(2021, 1, 31)) == (
        date(2020, 12, 1),
        date(2021, 1, 1),
    )
    assert covered_months(date(2020, 2, 3), date(2020, 2, 20)) == ()


def test_tc113_vector_haversine_matches_scalar() -> None:
    lats = np.array([CENTER[0] + 0.0089, CENTER[0] + 0.0091, CENTER[0]], dtype=np.float64)
    lons = np.array([CENTER[1], CENTER[1], CENTER[1] + 0.01], dtype=np.float64)
    vector = haversine_km_vector(CENTER[0], CENTER[1], lats, lons)
    for index in range(3):
        scalar = analytics.haversine_km(
            CENTER[0], CENTER[1], float(lats[index]), float(lons[index])
        )
        assert abs(float(vector[index]) - scalar) <= 1e-9
    assert vector[0] == pytest.approx(0.98964, abs=1e-4)
    assert vector[1] == pytest.approx(1.01188, abs=1e-4)
    assert vector[0] <= 1.0 < vector[1]


# ---- assembly: coverage, niche, competitors ----


def test_tc112_coverage_intersection() -> None:
    scope = _assemble(
        [_area()],
        [_business("a")],
        key=_key(date(2009, 6, 1), date(2010, 3, 31)),
    )
    assert scope.covered_start == date(2010, 1, 16)
    assert scope.covered_end == date(2010, 3, 31)
    assert scope.covered_days == 75
    assert scope.requested_start == date(2009, 6, 1)
    assert scope.areas[0].covered_days == 75
    assert scope.covered_months == (date(2010, 2, 1), date(2010, 3, 1))


def test_range_outside_coverage_yields_zero_days_not_error() -> None:
    scope = _assemble([_area()], [_business("a")], key=_key(date(2025, 1, 1), date(2025, 2, 1)))
    assert (scope.covered_start, scope.covered_end, scope.covered_days) == (None, None, 0)
    assert scope.covered_months == ()
    assert scope.metro_mean_rate == 0.0
    assert scope.areas[0].members[0].monthly == ()


def test_tc114_competitors_within_radius() -> None:
    def at(offset_km: float) -> float:
        return CENTER[0] + offset_km / 111.19508

    businesses = [
        _business("n1", None, lat=at(0.5)),
        _business("n2", 1, lat=at(0.98), stars=3.0, reviews=7),
        _business("n3", 1, lat=at(1.2)),
        _business("closed", 1, lat=at(0.3), is_open=False),
        _business("no-coords", 1, lat=None, lon=None),
        _business("parent-only", 1, niche=False, lat=at(0.2)),
        _business("unassigned", None, lat=at(0.6), stars=None, reviews=0),
    ]
    scope = _assemble([_area()], businesses)
    competitors = scope.areas[0].competitors
    assert competitors is not None
    assert [(c.stars, c.review_count) for c in competitors] == [
        (4.0, 10),
        (3.0, 7),
        (None, 0),
    ]


def test_competitors_none_without_centroid_and_empty_without_rivals() -> None:
    scope = _assemble(
        [_area(1, "33601", None, None), _area(2, "33602")],
        [_business("a", 1, niche=False), _business("b", 2, niche=False)],
    )
    assert scope.areas[0].competitors is None
    assert scope.areas[1].competitors == ()


def test_tc115_effective_radius() -> None:
    assert _assemble([_area()], [_business("a")], key=_key(radius_km=0.5)).radius_km == 1.0
    assert _assemble([_area()], [_business("a")], key=_key(radius_km=3.0)).radius_km == 3.0


def test_tc116_niche_members_versus_parent_set() -> None:
    businesses = [_business(f"b{i}", 1, niche=i < 4, stars=float(i % 5 + 1)) for i in range(10)]
    area = _assemble([_area()], businesses).areas[0]
    assert len(area.members) == 4
    assert area.niche_business_count == 4
    assert area.parent_business_count == 10
    assert area.parent_mean_stars == pytest.approx(np.mean([i % 5 + 1 for i in range(10)]))
    assert area.parent_mean_review_count == 10.0
    assert area.niche_open_count == 4


def test_parent_mean_stars_none_when_no_ratings() -> None:
    area = _assemble([_area()], [_business("a", stars=None)]).areas[0]
    assert area.parent_mean_stars is None


def test_tc117_area_without_parent_businesses_is_unscored() -> None:
    scope = _assemble(
        [_area(1, "33601"), _area(3, "33620"), _area(2, "33610")],
        [_business("a", 1)],
    )
    assert [a.postal_code for a in scope.areas] == ["33601"]
    assert [(u.postal_code, u.reason) for u in scope.unscored] == [
        ("33610", "no_parent_businesses"),
        ("33620", "no_parent_businesses"),
    ]


def test_areas_sorted_by_id_and_output_is_deterministic() -> None:
    areas = [_area(9, "33609"), _area(2, "33602"), _area(5, "33605")]
    businesses = [_business("c", 9), _business("a", 2), _business("b", 5)]
    first = _assemble(areas, businesses)
    second = _assemble(areas[::-1], businesses[::-1])
    assert [a.area_id for a in first.areas] == [2, 5, 9]
    assert first == second


def test_tc118_metro_mean_rate_is_ratio_of_sums() -> None:
    businesses = [_business(f"x{i}", 1) for i in range(4)] + [
        _business(f"y{i}", 2) for i in range(2)
    ]
    monthly = [MonthlyRow(f"x{i}", date(2020, 6, 1), 60) for i in range(4)] + [
        MonthlyRow(f"y{i}", date(2020, 6, 1), 15) for i in range(2)
    ]
    scope = _assemble([_area(1, "33601"), _area(2, "33602")], businesses, monthly=monthly)
    assert scope.covered_days == 30
    assert scope.metro_mean_rate == pytest.approx(270 / (6 * 30))
    assert scope.metro_mean_rate == pytest.approx(1.5)
    no_niche = _assemble([_area()], [_business("a", niche=False)], monthly=monthly[:1])
    assert no_niche.metro_mean_rate == 0.0


def test_tc119_monthly_series_and_totals() -> None:
    key = _key(date(2020, 1, 15), date(2020, 4, 10))
    monthly = [
        MonthlyRow("a", date(2020, 1, 1), 5),
        MonthlyRow("a", date(2020, 2, 1), 10),
        MonthlyRow("a", date(2020, 3, 1), 20),
        MonthlyRow("a", date(2020, 4, 1), 7),
        MonthlyRow("p", date(2020, 3, 1), 100),
    ]
    scope = _assemble(
        [_area()],
        [_business("a"), _business("none"), _business("p", niche=False)],
        key=key,
        monthly=monthly,
    )
    area = scope.areas[0]
    assert area.covered_month_count == 2 == len(scope.covered_months)
    by_total = {member.checkins: member for member in area.members}
    assert by_total[42].monthly == (10, 20)
    assert by_total[0].monthly == (0, 0)
    assert all(len(member.monthly) == area.covered_month_count for member in area.members)
    assert area.niche_checkins == 42
    assert area.parent_checkins == 142


def test_monthly_rows_outside_covered_range_are_ignored() -> None:
    monthly = [MonthlyRow("a", date(2019, 5, 1), 99), MonthlyRow("a", date(2020, 6, 1), 3)]
    area = _assemble([_area()], [_business("a")], monthly=monthly).areas[0]
    assert area.niche_checkins == 3


# ---- assembly: weather ----

STATION = "acis_USW00012842"


def _obs(day: int, variable: str, value: float | None, flag: str = "") -> StationObsRow:
    unit = "inch" if variable == "pcpn" else "degreeF"
    return StationObsRow(
        STATION, date(2020, 6, day), variable, value, unit, str(value), flag, False
    )


def test_tc047_no_station_means_no_weather() -> None:
    scope = _assemble(
        [_area(1, "33601"), _area(2, "33602")],
        [_business("a", 1, cell=None), _business("b", 2, cell="era5_cell")],
        obs=[_obs(1, "tmax", 80.0)],
        stations=frozenset({STATION}),
    )
    for area in scope.areas:
        assert area.weather_station_id is None
        assert area.weather_days == ()


def test_tc097_weather_days_convert_units_and_keep_missing_missing() -> None:
    obs = [
        _obs(1, "tmax", 89.6),
        _obs(1, "pcpn", 0.1),
        _obs(2, "tmax", 50.0),
        _obs(2, "pcpn", 0.3, "M"),
        _obs(3, "tmax", 70.0, "M"),
        _obs(3, "pcpn", None),
        _obs(4, "pcpn", None, "T"),
    ]
    area = _assemble(
        [_area()],
        [_business("a", cell=STATION)],
        obs=obs,
        stations=frozenset({STATION}),
    ).areas[0]
    assert area.weather_station_id == STATION
    days = {w.day.day: w for w in area.weather_days}
    assert sorted(days) == [1, 2, 4]
    assert days[1].tmax_c == pytest.approx(32.0)
    assert days[1].precip_mm == pytest.approx(2.54)
    assert days[2].tmax_c == pytest.approx(10.0)
    assert days[2].precip_mm is None
    assert days[4].tmax_c is None
    assert days[4].precip_mm == 0.0
    assert list(days) == sorted(days)


def test_tc098_weather_day_checkins_default_to_zero() -> None:
    area = _assemble(
        [_area()],
        [_business("a", cell=STATION)],
        obs=[_obs(1, "tmax", 80.0), _obs(2, "tmax", 80.0)],
        area_days=[AreaDayRow(1, date(2020, 6, 2), 6), AreaDayRow(1, date(2020, 6, 2), 1)],
        stations=frozenset({STATION}),
    ).areas[0]
    assert [(w.day.day, w.checkins) for w in area.weather_days] == [(1, 0), (2, 7)]


def test_weather_outside_covered_range_and_other_stations_are_excluded() -> None:
    other = StationObsRow("acis_OTHER", date(2020, 6, 5), "tmax", 80.0, "degreeF", "80", "", False)
    early = StationObsRow(STATION, date(2020, 5, 31), "tmax", 80.0, "degreeF", "80", "", False)
    area = _assemble(
        [_area()],
        [_business("a", cell=STATION), _business("b", cell="acis_OTHER")],
        obs=[other, early, _obs(5, "tmax", 80.0)],
        stations=frozenset({STATION, "acis_OTHER"}),
    ).areas[0]
    assert [w.day.day for w in area.weather_days] == [5]


def test_dominant_station_uses_niche_members_of_the_area() -> None:
    businesses = [
        _business("a", cell="s1"),
        _business("b", cell="s2"),
        _business("c", cell="s2"),
        _business("p1", niche=False, cell="s1"),
        _business("p2", niche=False, cell="s1"),
    ]
    area = _assemble([_area()], businesses, stations=frozenset({"s1", "s2"})).areas[0]
    assert area.weather_station_id == "s2"


# ---- load_scope_data: errors and SQL shape (no database) ----


def _filters(**changes: Any) -> Filters:
    values: dict[str, Any] = {
        "city": "Tampa",
        "state": "FL",
        "category": "Restaurants",
        "start_date": date(2020, 1, 1),
        "end_date": date(2020, 3, 31),
        "radius_km": 1.0,
        "source_category": "Pi'zza; DROP TABLE x",
    }
    values.update(changes)
    return Filters(**values)


class _FakeConnection:
    def __init__(self, results: list[list[tuple[Any, ...]]]) -> None:
        self.results = results
        self.calls: list[tuple[str, Any]] = []
        self._current: list[tuple[Any, ...]] = []

    def execute(self, query: str, parameters: Any = None) -> "_FakeConnection":
        self.calls.append((query, parameters))
        if not query.lstrip().startswith("SET"):
            self._current = self.results.pop(0) if self.results else []
        return self

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._current


def _patch_connect(monkeypatch: pytest.MonkeyPatch, connection: _FakeConnection) -> None:
    @contextmanager
    def fake_connect() -> Iterator[_FakeConnection]:
        yield connection

    monkeypatch.setattr(data, "connect", fake_connect)


def test_tc099_end_before_start_is_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _FakeConnection([])
    _patch_connect(monkeypatch, connection)
    with pytest.raises(ValueError, match="end date"):
        load_scope_data(_filters(end_date=date(2019, 1, 1)), COVERAGE, "rev")
    assert connection.calls == []


def test_tc099_missing_areas_or_businesses_raise_data_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_areas = _FakeConnection([[], []])
    _patch_connect(monkeypatch, no_areas)
    with pytest.raises(DataUnavailableError):
        load_scope_data(_filters(), COVERAGE, "rev")
    no_businesses = _FakeConnection([[(1, "33601", "Area", 27.9, -82.4)], []])
    _patch_connect(monkeypatch, no_businesses)
    with pytest.raises(DataUnavailableError):
        load_scope_data(_filters(), COVERAGE, "rev")


def test_queries_are_parameterised_and_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _FakeConnection(
        [
            [(1, "33601", "Area", 27.9, -82.4)],
            [("b1", 1, 27.9, -82.4, 4.0, 3, True, "s1", True)],
            [("s1",)],
            [("b1", date(2020, 1, 1), 5)],
            [(1, date(2020, 1, 1), 5)],
            [("s1", date(2020, 1, 1), "tmax", 50.0, "degreeF", "50", "", False)],
        ]
    )
    _patch_connect(monkeypatch, connection)
    scope = load_scope_data(_filters(), COVERAGE, "rev")
    assert scope.areas[0].weather_days[0].tmax_c == pytest.approx(10.0)
    assert len(connection.calls) == 7
    assert "READ ONLY" in connection.calls[0][0]
    for query, parameters in connection.calls[1:]:
        assert isinstance(parameters, dict)
        assert "DROP TABLE" not in query and "Tampa" not in query
    assert connection.calls[2][1]["src"] == "Pi'zza; DROP TABLE x"


def test_no_coverage_skips_activity_and_weather_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _FakeConnection(
        [
            [(1, "33601", "Area", 27.9, -82.4)],
            [("b1", 1, 27.9, -82.4, 4.0, 3, True, "s1", True)],
        ]
    )
    _patch_connect(monkeypatch, connection)
    scope = load_scope_data(
        _filters(start_date=date(2030, 1, 1), end_date=date(2030, 2, 1)), COVERAGE, "rev"
    )
    assert scope.covered_days == 0
    assert len(connection.calls) == 3


# ---- integration (isolated PostgreSQL; skipped without TEST_DATABASE_URL) ----


def _seed(connection: Any) -> None:
    dataset_id = connection.execute(
        "INSERT INTO sitesense.datasets (name, source_uri) VALUES ('fx', 'fixture://site') "
        "RETURNING id"
    ).fetchone()[0]
    area_ids = {}
    for postal, lat, lon in (
        ("33601", 27.9506, -82.4572),
        ("33602", 27.97, -82.46),
        ("33603", 28.0, -82.5),
    ):
        area_ids[postal] = connection.execute(
            "INSERT INTO sitesense.candidate_areas (city, state, postal_code, display_name, "
            "latitude, longitude) VALUES ('Tampa', 'FL', %s, %s, %s, %s) RETURNING id",
            (postal, f"Area {postal}", lat, lon),
        ).fetchone()[0]
    for cell, kind in (("acis_S1", "acis_station"), ("era5_1", "era5")):
        connection.execute(
            "INSERT INTO sitesense.weather_cells (weather_cell_id, latitude, longitude, "
            "dataset_id, source_kind) VALUES (%s, 27.9, -82.4, %s, %s)",
            (cell, dataset_id, kind),
        )
    businesses: tuple[tuple[Any, ...], ...] = (
        ("b1", "Tampa", "FL", ["Restaurants", "Pizza"], "33601", 27.9506, 4.0, 10, True, "acis_S1"),
        ("b2", "Tampa", "FL", ["Restaurants"], "33601", 27.9510, 3.0, 5, True, "acis_S1"),
        ("b3", "Tampa", "FL", ["Restaurants", "Pizza"], "33602", 27.9700, 5.0, 2, False, "era5_1"),
        ("b4", "Tampa", "FL", ["Restaurants", "Pizza"], None, 27.9550, 2.0, 1, True, None),
        ("b5", "Nashville", "TN", ["Restaurants", "Pizza"], None, 36.1, 4.0, 9, True, None),
        ("b6", "Tampa", "FL", ["Day Spas"], "33601", 27.9506, 4.0, 9, True, None),
    )
    for bid, city, state, cats, postal, lat, stars, reviews, is_open, cell in businesses:
        connection.execute(
            "INSERT INTO sitesense.businesses (business_id, area_id, name, address, city, state, "
            "canonical_city, canonical_state, postal_code, latitude, longitude, categories, stars, "
            "review_count, is_open, weather_cell_id, dataset_id) VALUES (%s, %s, %s, '', %s, %s, "
            "%s, %s, '', %s, -82.4572, %s, %s, %s, %s, %s, %s)",
            (
                bid,
                area_ids.get(postal or ""),
                bid,
                city,
                state,
                city,
                state,
                lat,
                cats,
                stars,
                reviews,
                is_open,
                cell,
                dataset_id,
            ),
        )
    for bid, day, hour, count in (
        ("b1", date(2020, 1, 10), 9, 3),
        ("b1", date(2020, 1, 10), 10, 2),
        ("b1", date(2020, 2, 5), 9, 4),
        ("b2", date(2020, 2, 5), 9, 7),
        ("b3", date(2020, 2, 6), 9, 1),
        ("b4", date(2020, 3, 1), 9, 5),
        ("b5", date(2020, 2, 5), 9, 50),
        ("b1", date(2019, 12, 31), 9, 40),
    ):
        connection.execute(
            "INSERT INTO sitesense.business_activity_hourly (business_id, activity_date, "
            "hour_of_day, checkin_count, timestamp_policy, dataset_id) "
            "VALUES (%s, %s, %s, %s, 'assumed_source_local', %s)",
            (bid, day, hour, count, dataset_id),
        )
    for day, variable, value, unit, raw, flag in (
        (date(2020, 2, 5), "tmax", 68.0, "degreeF", "68", ""),
        (date(2020, 2, 5), "pcpn", 0.5, "inch", "0.5", ""),
        (date(2020, 2, 6), "tmax", 70.0, "degreeF", "70", "M"),
        (date(2020, 2, 6), "pcpn", 0.0, "inch", "T", "T"),
        (date(2020, 2, 7), "tmin", 40.0, "degreeF", "40", ""),
        (date(2019, 12, 31), "tmax", 50.0, "degreeF", "50", ""),
    ):
        connection.execute(
            "INSERT INTO sitesense.station_weather_observations (weather_cell_id, "
            "observation_date, variable, value, unit, raw_value, flag, network_id, source_flag, "
            "observation_time_local_standard, is_trace, dataset_id) VALUES ('acis_S1', %s, %s, "
            "%s, %s, %s, %s, 'n', '', 'midnight', %s, %s)",
            (day, variable, 0.0 if raw == "T" else value, unit, raw, flag, raw == "T", dataset_id),
        )


def _row_counts(connection: Any) -> list[int]:
    tables = (
        "datasets",
        "candidate_areas",
        "weather_cells",
        "businesses",
        "business_activity_hourly",
        "station_weather_observations",
    )
    return [
        connection.execute(f"SELECT count(*) FROM sitesense.{table}").fetchone()[0]  # noqa: S608
        for table in tables
    ]


def test_integration_load_scope_data_matches_seeded_rows(
    isolated_import_database: None,
) -> None:
    with database.connect() as connection:
        _seed(connection)
    coverage = DateCoverage(date(2020, 1, 1), date(2020, 3, 31))
    with database.connect() as connection:
        before = _row_counts(connection)

    pizza = load_scope_data(_filters(source_category="Pizza"), coverage, "rev")
    assert pizza.covered_days == 91
    assert pizza.covered_months == (date(2020, 1, 1), date(2020, 2, 1), date(2020, 3, 1))
    assert [a.postal_code for a in pizza.areas] == ["33601", "33602"]
    assert [u.postal_code for u in pizza.unscored] == ["33603"]
    first, second = pizza.areas
    assert first.niche_business_count == 1
    assert first.members[0].checkins == 9
    assert first.members[0].monthly == (5, 4, 0)
    assert first.parent_business_count == 2
    assert first.parent_checkins == 16
    assert first.parent_mean_stars == pytest.approx(3.5)
    assert first.parent_mean_review_count == pytest.approx(7.5)
    assert first.weather_station_id == "acis_S1"
    assert [(w.day, w.checkins, w.tmax_c, w.precip_mm) for w in first.weather_days] == [
        (date(2020, 2, 5), 4, pytest.approx(20.0), pytest.approx(12.7)),
        (date(2020, 2, 6), 0, None, 0.0),
    ]
    assert first.competitors is not None
    assert len(first.competitors) == 2  # b1 and unassigned b4 (open, niche, nearby)
    assert second.niche_open_count == 0
    assert second.weather_station_id is None and second.weather_days == ()
    assert pizza.metro_mean_rate == pytest.approx((9 + 1) / (2 * 91))

    everything = load_scope_data(_filters(source_category=None), coverage, "rev")
    assert everything.areas[0].niche_business_count == 2
    assert everything.areas[0].members[0].checkins + everything.areas[0].members[1].checkins == 16
    assert everything.areas[0].parent_business_count == 2

    outside = load_scope_data(
        _filters(start_date=date(2030, 1, 1), end_date=date(2030, 1, 31), source_category=None),
        coverage,
        "rev",
    )
    assert outside.covered_days == 0 and outside.areas[0].members[0].monthly == ()

    with pytest.raises(DataUnavailableError):
        load_scope_data(_filters(city="Nowhere"), coverage, "rev")
    with pytest.raises(DataUnavailableError):
        load_scope_data(_filters(category="Unknown category"), coverage, "rev")

    # TC-198: data loading performs only SELECTs.
    with database.connect() as connection:
        assert _row_counts(connection) == before


def test_integration_scope_data_is_deterministic(isolated_import_database: None) -> None:
    with database.connect() as connection:
        _seed(connection)
    coverage = DateCoverage(date(2020, 1, 1), date(2020, 3, 31))
    one = load_scope_data(_filters(source_category="Pizza"), coverage, "rev")
    two = load_scope_data(_filters(source_category="Pizza"), coverage, "rev")
    assert one == two
    assert math.isfinite(one.metro_mean_rate)
