"""Typed records shared by the basic SiteSense screens and data importer."""

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class CityScope:
    city: str
    state: str
    timezone: str


CITY_SCOPES = (
    CityScope("Philadelphia", "PA", "America/New_York"),
    CityScope("Nashville", "TN", "America/Chicago"),
    CityScope("Tampa", "FL", "America/New_York"),
)

BUSINESS_CATEGORY_LABELS = {"Restaurants": "Restaurant", "Day Spas": "Spa"}


@dataclass(frozen=True)
class CandidateArea:
    id: int
    city: str
    state: str
    postal_code: str
    display_name: str
    latitude: float | None
    longitude: float | None

    @property
    def label(self) -> str:
        return f"{self.display_name} ({self.postal_code})"


@dataclass(frozen=True)
class WeatherCell:
    weather_cell_id: str
    latitude: float
    longitude: float
    source_kind: str = "era5"
    station_name: str | None = None


@dataclass(frozen=True)
class StationWeatherSummary:
    """Per-station accepted report values; missing reports are never zeros."""

    weather_cell_id: str
    station_name: str
    selected_days: int
    temp_min_days: int
    temp_max_days: int
    precipitation_days: int
    mean_temp_min_c: float | None
    mean_temp_max_c: float | None
    precipitation_sum_mm: float | None
    latest_observed_date: date | None


@dataclass(frozen=True)
class Business:
    business_id: str
    area_id: int | None
    name: str
    city: str
    state: str
    postal_code: str
    latitude: float | None
    longitude: float | None
    categories: tuple[str, ...]
    stars: float | None
    review_count: int
    is_open: bool
    weather_cell_id: str | None


@dataclass(frozen=True)
class DateCoverage:
    start_date: date
    end_date: date
    timestamp_policy: str = "assumed_source_local"


@dataclass(frozen=True)
class Catalog:
    areas: tuple[CandidateArea, ...]
    businesses: tuple[Business, ...]
    weather_cells: tuple[WeatherCell, ...]
    coverage: DateCoverage

    @property
    def weather_source(self) -> str:
        sources = {cell.source_kind for cell in self.weather_cells}
        if not sources:
            return "unavailable"
        return next(iter(sources)) if len(sources) == 1 else "mixed"


@dataclass(frozen=True)
class ActivityRecord:
    """Area/date/hour query aggregate; area 0 retains unassigned city activity."""

    area_id: int
    activity_date: date
    hour_of_day: int
    checkin_count: int


@dataclass(frozen=True)
class Filters:
    city: str
    state: str
    category: str
    start_date: date
    end_date: date
    radius_km: float = 1.0

    @property
    def category_label(self) -> str:
        return BUSINESS_CATEGORY_LABELS.get(self.category, self.category)


@dataclass(frozen=True)
class AreaSummary:
    area: CandidateArea
    business_count: int
    checkin_count: int
    average_weekly_checkins: float
    average_rating: float | None
    nearby_business_count: int
    weather_match_rate: float | None
    yoy_percent: float | None


@dataclass(frozen=True)
class ActivitySummary:
    area_id: int
    checkin_count: int
    average_weekly_checkins: float
    weekday_share: float | None
    weekend_share: float | None
    busiest_day: str | None
    peak_hour: int | None
    peak_month: int | None
    seasonality_ratio: float | None
    heatmap: tuple[tuple[int, ...], ...]
    monthly_index: tuple[float | None, ...]
    annual_share: tuple[tuple[int, float | None], ...]
    weekly_activity: tuple[tuple[date, int], ...]
    monthly_calendar_days: tuple[int, ...] = ()
    covered_calendar_days: int = 0
