"""Recorded zeros stay recorded; only the selected post-archive tail is projected."""

import hashlib
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import streamlit as st

from sitesense import business_activity, business_forecast, business_future_views
from sitesense.business_forecast import BusinessBundle, BusinessGroup
from sitesense.models import Business, Catalog, DateCoverage, Filters
from sitesense.ui import ScreenContext


def _business(identifier: str, categories: tuple[str, ...] = ("Restaurants", "Pizza")) -> Business:
    return Business(
        identifier,
        None,
        identifier,
        "PHILADELPHIA",
        "PA",
        "",
        None,
        None,
        categories,
        None,
        0,
        True,
        None,
    )


def _context(start: date, end: date) -> ScreenContext:
    return ScreenContext(
        Filters("Philadelphia", "PA", "Restaurants", start, end, source_category="Pizza"),
        Catalog(
            (),
            (
                _business("a"),
                _business("b"),
                _business("unsupported"),
                _business("delivery", ("Restaurants", "Pizza Delivery")),
                _business("spa", ("Day Spas", "Pizza")),
            ),
            (),
            DateCoverage(date(2022, 1, 1), date(2022, 1, 2)),
        ),
        (),
        (),
        "import-revision",
    )


def _group(identifier: str, rate: float = 1) -> BusinessGroup:
    return BusinessGroup(
        identifier,
        identifier,
        "Philadelphia",
        "PA",
        "Restaurants",
        ("Pizza",),
        None,
        None,
        1096,
        20,
        rate,
        (rate,) * 84,
    )


@pytest.fixture
def model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[str, str]]:
    (tmp_path / "manifest.json").write_bytes(b"manifest")
    (tmp_path / "businesses.json").write_bytes(b"businesses")
    monkeypatch.setattr(business_forecast, "MODEL_DIR", tmp_path)
    monkeypatch.setattr(business_forecast, "code_sha256", lambda: "code")
    monkeypatch.setattr(business_forecast, "lock_sha256", lambda: "lock")
    calls: list[tuple[str, str]] = []
    bundle = BusinessBundle(
        (
            _group("a"),
            _group("b", 2),
            _group("not-in-catalog", 3),
            replace(_group("delivery"), source_categories=("Pizza Delivery",)),
            replace(_group("spa"), category="Day Spas"),
        ),
        {"training_end": "2021-12-31"},
    )

    def load(revision: str, import_revision: str) -> BusinessBundle:
        calls.append((revision, import_revision))
        return bundle

    monkeypatch.setattr(business_future_views, "_bundle", load)
    monkeypatch.setattr("sitesense.app._import_is_unchanged", lambda _: True)
    return calls


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2022, 1, 1), date(2022, 1, 2), None),
        (date(2018, 1, 1), date(2018, 1, 31), None),
        (date(2018, 1, 1), date(2022, 1, 5), (date(2022, 1, 3), date(2022, 1, 5))),
        (date(2025, 6, 1), date(2025, 6, 30), (date(2025, 6, 1), date(2025, 6, 30))),
        (date(2025, 6, 30), date(2025, 6, 1), None),
    ],
)
def test_projected_interval_uses_import_end_not_missing_checkin_days(
    start: date, end: date, expected: tuple[date, date] | None
) -> None:
    assert business_activity.projected_interval(_context(start, end)) == expected


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2022, 1, 1), date(2022, 1, 2)),
        (date(2018, 1, 1), date(2018, 1, 31)),
        (date(2018, 1, 1), date(2022, 1, 1)),
    ],
)
def test_empty_recorded_history_and_precoverage_do_not_open_model_files(
    monkeypatch: pytest.MonkeyPatch, start: date, end: date
) -> None:
    def forbidden() -> str:
        pytest.fail("Recorded or precoverage days must not load projection artifacts")

    monkeypatch.setattr(business_forecast, "code_sha256", forbidden)
    context = _context(start, end)
    assert context.records == ()
    assert business_activity.load_projection(context) == business_activity.ProjectionResult(None)


def test_mixed_selection_projects_only_tail_and_uses_verified_cache(
    model: list[tuple[str, str]],
) -> None:
    result = business_activity.load_projection(_context(date(2022, 1, 1), date(2022, 1, 5)))
    assert result.interval == (date(2022, 1, 3), date(2022, 1, 5))
    assert {row.group.business_id: row.period_rate for row in result.projections} == {
        "a": 3,
        "b": 6,
    }
    assert all(row.calendar_days == 3 for row in result.projections)
    expected = hashlib.sha256(b"manifestbusinessescodelock").hexdigest()
    assert model == [(expected, "import-revision")]
    assert result.model_revision == expected
    assert result.issue is None


def test_pure_tail_keeps_exact_selected_days_and_business_ids(model: list[tuple[str, str]]) -> None:
    result = business_activity.load_projection(
        _context(date(2025, 6, 1), date(2025, 6, 30)), ("a", "not-in-catalog", "delivery", "spa")
    )
    assert result.interval == (date(2025, 6, 1), date(2025, 6, 30))
    assert [(row.group.business_id, row.period_rate) for row in result.projections] == [("a", 30)]


def test_project_uses_exact_canonical_city_state_and_source(
    model: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    scoped = BusinessBundle(
        (
            _group("a"),
            replace(_group("b"), city="Nashville", state="TN"),
            replace(_group("unsupported"), source_categories=("Pizza Delivery",)),
        ),
        {"training_end": "2021-12-31"},
    )
    monkeypatch.setattr(business_future_views, "_bundle", lambda *_: scoped)
    result = business_activity.load_projection(_context(date(2022, 1, 3), date(2022, 1, 3)))
    assert [row.group.business_id for row in result.projections] == ["a"]


def test_unmodeled_business_is_unknown_rather_than_zero(model: list[tuple[str, str]]) -> None:
    result = business_activity.load_projection(
        _context(date(2022, 1, 3), date(2022, 1, 5)), ("unsupported",)
    )
    assert result.interval is not None
    assert result.projections == ()
    assert business_activity.daily_projection(result, "unsupported") == []


@pytest.mark.parametrize(
    "exception", [OSError, ValueError, business_forecast.BusinessForecastError]
)
def test_failed_or_stale_model_keeps_interval_and_sanitizes_failure(
    model: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch, exception: type[Exception]
) -> None:
    def fail(*_: Any) -> BusinessBundle:
        raise exception("secret connection or internal artifact path")

    monkeypatch.setattr(business_future_views, "_bundle", fail)
    result = business_activity.load_projection(_context(date(2022, 1, 1), date(2022, 1, 5)))
    assert result.interval == (date(2022, 1, 3), date(2022, 1, 5))
    assert result.projections == ()
    assert result.issue and "unavailable" in result.issue
    assert "secret" not in result.issue
    assert result.model_revision is None


def test_missing_artifacts_are_safe(model: list[tuple[str, str]]) -> None:
    (business_forecast.MODEL_DIR / "manifest.json").unlink()
    result = business_activity.load_projection(_context(date(2022, 1, 3), date(2022, 1, 5)))
    assert result.issue is not None
    assert result.projections == ()
    assert model == []


def test_changing_artifacts_changes_verified_cache_identity(model: list[tuple[str, str]]) -> None:
    context = _context(date(2022, 1, 3), date(2022, 1, 5))
    first = business_activity.load_projection(context)
    (business_forecast.MODEL_DIR / "businesses.json").write_bytes(b"changed payload")
    second = business_activity.load_projection(context)
    assert first.model_revision != second.model_revision
    assert len(model) == 2


def test_changed_import_stops_before_projecting(
    model: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    class ImportChanged(BaseException):
        pass

    def stop() -> None:
        raise ImportChanged

    monkeypatch.setattr("sitesense.app._import_is_unchanged", lambda _: False)
    monkeypatch.setattr(st, "stop", stop)
    with pytest.raises(ImportChanged):
        business_activity.load_projection(_context(date(2022, 1, 3), date(2022, 1, 5)))


def test_long_tail_has_safe_issue_without_loading_model(model: list[tuple[str, str]]) -> None:
    result = business_activity.load_projection(_context(date(2022, 1, 1), date(2035, 1, 1)))
    assert result.interval == (date(2022, 1, 3), date(2035, 1, 1))
    assert result.issue and "3,660" in result.issue
    assert model == []


def test_empty_requested_or_niche_cohort_does_not_load_model(model: list[tuple[str, str]]) -> None:
    context = _context(date(2022, 1, 3), date(2022, 1, 5))
    assert business_activity.load_projection(context, ()).projections == ()
    context = replace(context, filters=replace(context.filters, source_category="Thai"))
    assert business_activity.load_projection(context).projections == ()
    assert model == []


def test_seasonal_daily_values_sum_to_the_business_period_projection(
    model: list[tuple[str, str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    rates = tuple(index / 10 for index in range(84))
    bundle = BusinessBundle((replace(_group("a"), rates=rates),), {"training_end": "2021-12-31"})
    monkeypatch.setattr(business_future_views, "_bundle", lambda *_: bundle)
    result = business_activity.load_projection(_context(date(2024, 2, 26), date(2024, 3, 4)))
    rows = business_activity.daily_projection(result, "a")
    assert len(rows) == 8
    assert rows[0] == {"Date": "2024-02-26", "Projected check-ins": rates[7]}
    assert rows[-1] == {"Date": "2024-03-04", "Projected check-ins": rates[14]}
    assert sum(row["Projected check-ins"] for row in rows) == pytest.approx(
        result.projections[0].period_rate
    )
    assert business_activity.daily_projection(result, "unsupported") == []
    assert business_activity.daily_projection(business_activity.ProjectionResult(None), "a") == []
