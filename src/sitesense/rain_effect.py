"""Check-in change on rain days versus comparable days, per ZIP and city-wide.

Method (an association, not a causal effect):
- A ZIP's daily rain is the business-weighted mean of the stations mapped to its listed
  businesses that reported precipitation that day; unreported days are not evaluated.
- A rain day has at least the chosen daily total (WMO/ETCCDI R10mm = 10 mm, R20mm = 20 mm).
- Each rain day is compared with the same weekday in the 4 weeks before and after, using only
  days with reported rain below 10 mm. US federal holidays are excluded from both.
- Effect = total rain-day check-ins / total expected - 1, with a 95% bootstrap interval over
  rain days. Fewer than 20 usable rain days, or fewer than 2 expected check-ins per rain day,
  is reported as insufficient: quiet ZIPs produce large but meaningless swings.
Everything is computed from tables that already exist; nothing is written to the database.
"""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from sitesense.location_weather import DailyWeather
from sitesense.models import ActivityRecord, AreaSummary, Business

RAIN_LEVELS_MM = {"r10": 10.0, "r20": 20.0}
RAIN_LEVEL_LABELS = {"r10": "≥ 10 mm (R10mm)", "r20": "≥ 20 mm (R20mm)"}
DRY_BASELINE_MM = 10.0
BASELINE_WEEKS = 4
MIN_RAIN_DAYS = 20
MIN_EXPECTED_PER_DAY = 2.0  # quieter ZIPs swing on noise (mostly zero-check-in days)
BOOTSTRAP = 1000
SEED = 20261024
CITY = 0  # key for the city-wide estimate


@dataclass(frozen=True)
class RainEffect:
    n_days: int
    effect_pct: float | None
    ci_low: float | None
    ci_high: float | None
    expected_per_day: float = 0.0

    @property
    def status(self) -> str:
        """clear, no_clear, low_activity or insufficient."""
        if self.n_days < MIN_RAIN_DAYS or self.ci_low is None or self.ci_high is None:
            return "insufficient"
        if self.expected_per_day < MIN_EXPECTED_PER_DAY:
            return "low_activity"
        return "no_clear" if self.ci_low <= 0 <= self.ci_high else "clear"


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    return (
        day - timedelta(days=1)
        if day.weekday() == 5
        else (day + timedelta(days=1) if day.weekday() == 6 else day)
    )


def us_federal_holidays(years: Iterable[int]) -> set[date]:
    """Federal holidays with observed dates, plus Christmas Eve and New Year's Eve."""
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
    return days


def zip_daily_rain(
    daily: DailyWeather, station_weights: Counter[str], start: date, end: date
) -> dict[date, float]:
    """Business-weighted mean precipitation of reporting stations, per day."""
    rain: dict[date, float] = {}
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        weighted = [
            (weight, daily[station][day]["pcpn"])
            for station, weight in station_weights.items()
            if "pcpn" in daily.get(station, {}).get(day, {})
        ]
        total = sum(weight for weight, _ in weighted)
        if total:
            rain[day] = sum(weight * value for weight, value in weighted) / total
    return rain


def estimate(
    checkins: dict[date, int],
    rain: dict[date, float],
    threshold_mm: float,
    holidays: set[date],
    rng: np.random.Generator,
) -> RainEffect:
    """Compare rain-day check-ins with the same weekday in nearby dry-ish weeks."""
    observed, expected = [], []
    for day, mm in sorted(rain.items()):
        if mm < threshold_mm or day in holidays or day not in checkins:
            continue
        base = [
            checkins[other]
            for k in range(1, BASELINE_WEEKS + 1)
            for other in (day - timedelta(weeks=k), day + timedelta(weeks=k))
            if other in checkins
            and other not in holidays
            and other in rain
            and rain[other] < DRY_BASELINE_MM
        ]
        if len(base) >= 3 and sum(base) > 0:
            observed.append(checkins[day])
            expected.append(sum(base) / len(base))
    n = len(observed)
    if n < 3:
        return RainEffect(n, None, None, None)
    obs, exp = np.asarray(observed, float), np.asarray(expected, float)
    effect = obs.sum() / exp.sum() - 1
    picks = rng.integers(0, n, (BOOTSTRAP, n))
    boot = obs[picks].sum(axis=1) / exp[picks].sum(axis=1) - 1
    low, high = np.percentile(boot, [2.5, 97.5])
    return RainEffect(
        n, float(effect * 100), float(low * 100), float(high * 100), float(exp.mean())
    )


def _daily_checkins(
    records: Iterable[ActivityRecord], area_ids: set[int], start: date, end: date
) -> dict[date, int]:
    days = {start + timedelta(days=i): 0 for i in range((end - start).days + 1)}
    for record in records:
        if record.area_id in area_ids and record.activity_date in days:
            days[record.activity_date] += record.checkin_count
    return days


def rain_effects(
    records: tuple[ActivityRecord, ...],
    businesses: tuple[Business, ...],
    summaries: tuple[AreaSummary, ...],
    daily: DailyWeather,
    start: date,
    end: date,
    level: str,
) -> dict[int, RainEffect]:
    """Rain effect per candidate area id, plus CITY for all candidate areas pooled."""
    threshold = RAIN_LEVELS_MM[level]
    holidays = us_federal_holidays(range(start.year, end.year + 1))
    rng = np.random.default_rng(SEED)
    weights_by_area: dict[int, Counter[str]] = {}
    for business in businesses:
        if business.area_id is not None and business.weather_cell_id in daily:
            weights_by_area.setdefault(business.area_id, Counter())[business.weather_cell_id] += 1
    results: dict[int, RainEffect] = {}
    city_weights: Counter[str] = Counter()
    for summary in summaries:
        weights = weights_by_area.get(summary.area.id, Counter())
        city_weights.update(weights)
        if not weights:
            results[summary.area.id] = RainEffect(0, None, None, None)
            continue
        results[summary.area.id] = estimate(
            _daily_checkins(records, {summary.area.id}, start, end),
            zip_daily_rain(daily, weights, start, end),
            threshold,
            holidays,
            rng,
        )
    area_ids = {summary.area.id for summary in summaries}
    results[CITY] = (
        estimate(
            _daily_checkins(records, area_ids, start, end),
            zip_daily_rain(daily, city_weights, start, end),
            threshold,
            holidays,
            rng,
        )
        if city_weights
        else RainEffect(0, None, None, None)
    )
    return results
