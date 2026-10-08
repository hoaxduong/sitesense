"""Event-day check-ins vs the same weekday in nearby weeks, with bootstrap intervals.

Status per (city, category, event, level), following the T020 scoring questions:
  not_available  no official threshold (or no data) for this city
  insufficient   fewer than MIN_EVENTS usable event days
  not_supported  95% interval includes zero
  partial        interval excludes zero, but the strict baseline rule disagrees
  supported      interval excludes zero under both baseline rules, same direction
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from sitesense.pipeline import config


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_federal_holidays(years: range) -> set[pd.Timestamp]:
    """Federal holidays (with observed dates) plus Christmas Eve and New Year's Eve."""
    days: set[date] = set()
    for y in years:
        days |= {
            _observed(date(y, 1, 1)), _nth_weekday(y, 1, 0, 3), _nth_weekday(y, 2, 0, 3),
            _last_weekday(y, 5, 0), _observed(date(y, 7, 4)), _nth_weekday(y, 9, 0, 1),
            _nth_weekday(y, 10, 0, 2), _observed(date(y, 11, 11)), _nth_weekday(y, 11, 3, 4),
            _observed(date(y, 12, 25)), date(y, 12, 24), date(y, 12, 31),
        }  # fmt: skip
        if y >= 2021:
            days.add(_observed(date(y, 6, 19)))
    return {pd.Timestamp(d) for d in days}


def pairs(
    counts: pd.DataFrame, event: pd.DataFrame, exclude: pd.DataFrame, holidays: set[pd.Timestamp]
) -> pd.DataFrame:
    """Observed vs expected check-ins for every usable (event day, area).

    `counts`, `event`, `exclude`: dates x areas, same index/columns, study window only.
    """
    dates = counts.index
    position = {d: i for i, d in enumerate(dates)}
    values, events, blocked = counts.to_numpy(), event.to_numpy(), exclude.to_numpy()
    holiday_rows = np.array([d in holidays for d in dates])
    rows = []
    for col, area_id in enumerate(counts.columns):
        for row in np.flatnonzero(events[:, col] & ~holiday_rows):
            day = dates[int(row)]
            base = []
            for k in range(1, config.BASELINE_WEEKS + 1):
                for other in (day - pd.Timedelta(weeks=k), day + pd.Timedelta(weeks=k)):
                    i = position.get(other)
                    if i is not None and not blocked[i, col] and not holiday_rows[i]:
                        base.append(i)
            if len(base) >= 3:
                expected = float(values[base, col].mean())
                if expected > 0:
                    rows.append((day, area_id, float(values[row, col]), expected))
    return pd.DataFrame(rows, columns=["date", "area_id", "observed", "expected"])


def summarize(table: pd.DataFrame, rng: np.random.Generator) -> dict[str, float]:
    """Pool by date; ratio of totals minus one with a date-level bootstrap 95% interval."""
    if table.empty:
        return {"n": 0, "effect": np.nan, "low": np.nan, "high": np.nan, "p": np.nan}
    by_date = table.groupby("date")[["observed", "expected"]].sum()
    observed, expected = by_date.observed.to_numpy(), by_date.expected.to_numpy()
    n = len(by_date)
    effect = observed.sum() / expected.sum() - 1
    if n < 3:
        return {"n": n, "effect": effect * 100, "low": np.nan, "high": np.nan, "p": np.nan}
    picks = rng.integers(0, n, (config.BOOTSTRAP, n))
    boot = observed[picks].sum(axis=1) / expected[picks].sum(axis=1) - 1
    low, high = np.percentile(boot, [2.5, 97.5])
    p = max(1 / config.BOOTSTRAP, min(1.0, 2 * min((boot <= 0).mean(), (boot >= 0).mean())))
    return {"n": n, "effect": effect * 100, "low": low * 100, "high": high * 100, "p": p}


def status(family: dict[str, float], strict: dict[str, float]) -> str:
    if family["n"] < config.MIN_EVENTS or np.isnan(family["low"]):
        return "insufficient"
    if family["low"] <= 0 <= family["high"]:
        return "not_supported"
    strict_clear = strict["n"] >= config.MIN_EVENTS and not (strict["low"] <= 0 <= strict["high"])
    same_sign = np.sign(strict["effect"]) == np.sign(family["effect"])
    return "supported" if strict_clear and same_sign else "partial"
