"""Covered daily business activity paired with accepted station observations."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from math import isfinite
from statistics import correlation

from sitesense.models import DateCoverage

WEATHER_METRICS = {
    "Max temperature (°C)": "tmax",
    "Min temperature (°C)": "tmin",
    "Precipitation (mm)": "pcpn",
}


@dataclass(frozen=True)
class WeatherBand:
    """Ordered band ending at an optional upper boundary."""

    label: str
    upper_bound: float | None
    upper_inclusive: bool = False


TEMPERATURE_BANDS = (
    WeatherBand("Temperature < 0 °C", 0),
    WeatherBand("0 ≤ temperature < 10 °C", 10),
    WeatherBand("10 ≤ temperature < 20 °C", 20),
    WeatherBand("20 ≤ temperature < 30 °C", 30),
    WeatherBand("Temperature ≥ 30 °C", None),
)

WEATHER_BANDS = {
    "tmax": TEMPERATURE_BANDS,
    "tmin": TEMPERATURE_BANDS,
    "pcpn": (
        WeatherBand("0 mm (dry / trace)", 0, upper_inclusive=True),
        WeatherBand("0 < rain < 1 mm", 1),
        WeatherBand("1 ≤ rain < 5 mm", 5),
        WeatherBand("5 ≤ rain < 10 mm", 10),
        WeatherBand("10 ≤ rain < 20 mm", 20),
        WeatherBand("Rain ≥ 20 mm", None),
    ),
}


@dataclass(frozen=True)
class BusinessDay:
    business_id: str
    activity_date: date
    checkin_count: int


@dataclass(frozen=True)
class BusinessWeatherDay:
    business_id: str
    activity_date: date
    checkin_count: int
    weather_value: float | None


@dataclass(frozen=True)
class BusinessWeatherCorrelation:
    total_days: int
    paired_days: int
    missing_days: int
    pearson_r: float | None


@dataclass(frozen=True)
class WeatherBandSummary:
    band: str
    day_count: int
    zero_activity_days: int
    median_checkins: float | None
    q1: float | None
    q3: float | None
    min_checkins: int | None
    max_checkins: int | None


def weather_band_order(variable: str) -> tuple[str, ...]:
    """Return every predefined band label in increasing weather order."""
    if variable not in WEATHER_BANDS:
        raise ValueError("Choose an available weather variable.")
    return tuple(band.label for band in WEATHER_BANDS[variable])


def weather_band(value: float, variable: str) -> str:
    """Classify accepted finite weather; rainfall must be nonnegative."""
    weather_band_order(variable)
    if not isfinite(value) or (variable == "pcpn" and value < 0):
        raise ValueError("Weather values must be finite, with nonnegative precipitation.")
    for band in WEATHER_BANDS[variable]:
        if (
            band.upper_bound is None
            or value < band.upper_bound
            or (band.upper_inclusive and value == band.upper_bound)
        ):
            return band.label
    raise ValueError("Weather value has no defined band.")


def _linear_quantile(values: list[int], probability: float) -> float:
    """Interpolate sorted empirical values at index (n - 1) * probability."""
    position = (len(values) - 1) * probability
    lower = int(position)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (position - lower)


def summarize_weather_bands(
    days: Iterable[BusinessWeatherDay], variable: str
) -> tuple[WeatherBandSummary, ...]:
    """Summarize one business's distinct dates, including zero activity days.

    Every predefined band is returned; empty bands have zero counts and undefined
    statistics. Missing or invalid weather does not enter any band. Quartiles use
    linear interpolation, matching the empirical quartiles in Vega-Lite boxplots.
    """
    groups: dict[str, list[int]] = {label: [] for label in weather_band_order(variable)}
    business_id = None
    seen_dates: set[date] = set()
    for day in days:
        if business_id is None:
            business_id = day.business_id
        elif business_id != day.business_id:
            raise ValueError("Weather bands require one business.")
        if day.activity_date in seen_dates:
            raise ValueError("Expected one paired observation per business and date.")
        seen_dates.add(day.activity_date)
        if day.checkin_count < 0:
            raise ValueError("Recorded check-ins must be nonnegative.")
        value = day.weather_value
        if value is None or not isfinite(value) or (variable == "pcpn" and value < 0):
            continue
        groups[weather_band(value, variable)].append(day.checkin_count)
    summaries = []
    for label, values in groups.items():
        values.sort()
        summaries.append(
            WeatherBandSummary(
                band=label,
                day_count=len(values),
                zero_activity_days=values.count(0),
                median_checkins=_linear_quantile(values, 0.5) if values else None,
                q1=_linear_quantile(values, 0.25) if values else None,
                q3=_linear_quantile(values, 0.75) if values else None,
                min_checkins=values[0] if values else None,
                max_checkins=values[-1] if values else None,
            )
        )
    return tuple(summaries)


def pair_business_weather(
    records: Iterable[BusinessDay],
    business_id: str,
    start: date,
    end: date,
    coverage: DateCoverage,
    station_days: Mapping[date, Mapping[str, float]],
    variable: str,
) -> tuple[BusinessWeatherDay, ...]:
    """Fill activity zeros inside coverage; preserve missing weather separately.

    ``station_days`` contains only the selected business's accepted mapped-station
    reports. Source report dates and assumed-local activity dates are not verified
    to have the same time alignment. Other business IDs and dates are excluded.
    """
    if end < start:
        raise ValueError("The end date must be on or after the start date.")
    if variable not in WEATHER_METRICS.values():
        raise ValueError("Choose an available weather variable.")
    start, end = max(start, coverage.start_date), min(end, coverage.end_date)
    if end < start:
        return ()
    counts: dict[date, int] = {}
    for record in records:
        if record.business_id != business_id or not start <= record.activity_date <= end:
            continue
        if record.checkin_count < 0:
            raise ValueError("Recorded check-ins must be nonnegative.")
        if record.activity_date in counts:
            raise ValueError("Expected one activity aggregate per business and date.")
        counts[record.activity_date] = record.checkin_count
    result = []
    for offset in range((end - start).days + 1):
        day = start + timedelta(days=offset)
        value = station_days.get(day, {}).get(variable)
        if value is not None and (not isfinite(value) or (variable == "pcpn" and value < 0)):
            value = None
        result.append(BusinessWeatherDay(business_id, day, counts.get(day, 0), value))
    return tuple(result)


def summarize_correlation(days: Iterable[BusinessWeatherDay]) -> BusinessWeatherCorrelation:
    """Return an unadjusted Pearson correlation, without significance inference.

    Zero-activity days are retained. Missing weather does not participate. At least
    three paired days and variation in both variables are required; sparse or
    autocorrelated observations do not establish a reliable weather effect.
    """
    selected = tuple(days)
    paired = tuple(day for day in selected if day.weather_value is not None)
    r = None
    if len(paired) >= 3:
        weather = [day.weather_value for day in paired if day.weather_value is not None]
        activity = [day.checkin_count for day in paired]
        if min(weather) != max(weather) and min(activity) != max(activity):
            r = max(-1.0, min(1.0, correlation(weather, activity)))
    return BusinessWeatherCorrelation(len(selected), len(paired), len(selected) - len(paired), r)
