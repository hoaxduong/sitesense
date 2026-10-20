"""Frozen data contracts for site recommendations (pure; no Streamlit/DB imports)."""

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Final, Literal

import numpy as np
from numpy.typing import NDArray

from sitesense.models import Filters

MetricKey = Literal[
    "demand_rate",
    "competition",
    "quality_gap",
    "momentum",
    "stability",
    "weather_resilience",
]
Confidence = Literal["high", "medium", "low"]
DemandSource = Literal["observed", "blended", "model"]
Language = Literal["en", "vi"]
ExplainKind = Literal["top_picks", "compare", "question"]
NarrativeSource = Literal["template", "llm"]
EstimatorReason = Literal["ok", "insufficient_training_areas", "no_skill"]
UnscoredReason = Literal["no_parent_businesses"]
WarningCode = Literal[
    "few_areas",
    "estimator_disabled",
    "short_coverage",
    "partial_coverage",
    "weather_sparse",
    "equal_weight_fallback",
]

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]

PROXY_NOTICE: Final[str] = "Check-ins are an activity proxy, not customers or revenue."

METRIC_KEYS: Final[tuple[MetricKey, ...]] = (
    "demand_rate",
    "competition",
    "quality_gap",
    "momentum",
    "stability",
    "weather_resilience",
)
CONFIDENCE_ORDER: Final[Mapping[Confidence, int]] = MappingProxyType(
    {"low": 0, "medium": 1, "high": 2}
)
METRIC_LABELS: Final[Mapping[MetricKey, str]] = MappingProxyType(
    {
        "demand_rate": "Demand",
        "competition": "Competition",
        "quality_gap": "Quality gap",
        "momentum": "Momentum",
        "stability": "Stability",
        "weather_resilience": "Weather resilience",
    }
)
# True when a higher raw value is better; competition is the only inverted metric.
METRIC_HIGHER_IS_BETTER: Final[Mapping[MetricKey, bool]] = MappingProxyType(
    {
        "demand_rate": True,
        "competition": False,
        "quality_gap": True,
        "momentum": True,
        "stability": True,
        "weather_resilience": True,
    }
)
METRIC_UNITS: Final[Mapping[MetricKey, str]] = MappingProxyType(
    {
        "demand_rate": "check-ins per business-day",
        "competition": "open niche businesses within radius",
        "quality_gap": "5 minus weighted mean stars",
        "momentum": "(last 12 mo - prior 12 mo) / (sum)",
        "stability": "1 - min(1, CV of monthly check-ins)",
        "weather_resilience": "1 - mean(|rain effect|, |heat effect|)",
    }
)


class ScopeUnavailableError(ValueError):
    """The scope cannot be scored; `reason` selects the UI message."""

    def __init__(self, reason: Literal["no_coverage", "no_scoreable_areas"], message: str) -> None:
        super().__init__(message)
        self.reason: Final = reason


@dataclass(frozen=True)
class ScoringConfig:
    """Single home of every tunable constant. Defaults are the authoritative design values."""

    shrinkage_k: float = 3.0
    min_observed_businesses: int = 3  # N >= this -> demand_source "observed"
    min_radius_km: float = 1.0  # R = max(filters.radius_km, min_radius_km)
    momentum_window_months: int = 12
    min_months_momentum: int = 24
    min_months_stability: int = 12  # months with check-ins > 0
    wet_day_mm: float = 2.5  # wet: precip >= 2.5 mm
    hot_day_c: float = 32.0  # hot: tmax >= 32 C
    min_stratum_days: int = 3  # per calendar month, per group
    min_weather_days: int = 20  # overall wet, dry and hot days
    high_min_businesses: int = 5
    high_min_checkins: int = 500
    high_min_days: int = 365
    low_max_businesses: int = 2  # N < this -> low
    low_checkins: int = 100  # checkins < this -> low
    n_bootstrap: int = 200
    interval_low_pct: float = 5.0
    interval_high_pct: float = 95.0
    n_top_factors: int = 3
    min_scored_areas: int = 5  # below -> warning "few_areas"
    short_coverage_days: int = 365
    min_estimator_train_areas: int = 8
    ridge_alphas: tuple[float, ...] = (0.1, 1.0, 10.0, 100.0)
    max_compare_areas: int = 5
    min_compare_areas: int = 2


DEFAULT_CONFIG: Final[ScoringConfig] = ScoringConfig()


@dataclass(frozen=True)
class ScoreWeights:
    """Non-negative finite weights; need not sum to 1 (they are renormalised per area)."""

    demand_rate: float = 0.35
    competition: float = 0.20
    quality_gap: float = 0.10
    momentum: float = 0.15
    stability: float = 0.05
    weather_resilience: float = 0.15

    def __post_init__(self) -> None:
        values = self.as_tuple()
        if any((not math.isfinite(v)) or v < 0.0 for v in values):
            raise ValueError("Weights must be finite and non-negative.")
        if sum(values) <= 0.0:
            raise ValueError("At least one weight must be positive.")

    def get(self, key: MetricKey) -> float:
        """Return the weight for `key`."""
        return self.as_tuple()[METRIC_KEYS.index(key)]

    def as_tuple(self) -> tuple[float, ...]:
        """Weights in METRIC_KEYS order."""
        return (
            self.demand_rate,
            self.competition,
            self.quality_gap,
            self.momentum,
            self.stability,
            self.weather_resilience,
        )


PRESETS: Final[Mapping[str, ScoreWeights]] = MappingProxyType(
    {
        "Balanced": ScoreWeights(0.35, 0.20, 0.10, 0.15, 0.05, 0.15),
        "Demand-focused": ScoreWeights(0.55, 0.10, 0.05, 0.15, 0.05, 0.10),
        "Low-competition": ScoreWeights(0.25, 0.40, 0.10, 0.10, 0.05, 0.10),
        "Weather-resilient": ScoreWeights(0.25, 0.10, 0.05, 0.10, 0.10, 0.40),
    }
)


@dataclass(frozen=True)
class ScopeKey:
    """Identity of a scoring scope; the cache key and the bootstrap seed source."""

    city: str
    state: str
    category: str
    source_category: str | None
    start_date: date
    end_date: date
    radius_km: float
    import_revision: str

    @classmethod
    def from_filters(cls, filters: Filters, import_revision: str) -> "ScopeKey":
        """Copy the scope fields of `filters`. Raises ValueError if end_date < start_date."""
        if filters.end_date < filters.start_date:
            raise ValueError("The end date must be on or after the start date.")
        return cls(
            filters.city,
            filters.state,
            filters.category,
            filters.source_category,
            filters.start_date,
            filters.end_date,
            filters.radius_km,
            import_revision,
        )

    def seed(self) -> int:
        """Stable 32-bit seed: sha256 of '|'-joined fields (source '' if None, radius '%.3f',
        dates ISO), first 8 bytes big-endian, modulo 2**32. Independent of process/hash salt."""
        text = "|".join(
            (
                self.city,
                self.state,
                self.category,
                self.source_category or "",
                self.start_date.isoformat(),
                self.end_date.isoformat(),
                f"{self.radius_km:.3f}",
                self.import_revision,
            )
        )
        return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big") % 2**32

    def effective_radius_km(self, config: ScoringConfig = DEFAULT_CONFIG) -> float:
        """R = max(radius_km, config.min_radius_km)."""
        return max(self.radius_km, config.min_radius_km)

    def label(self) -> str:
        """'Tampa, FL · Restaurants · Pizza · 2016-01-01 to 2022-01-19' (source omitted if None)."""
        parts = [f"{self.city}, {self.state}", self.category]
        if self.source_category:
            parts.append(self.source_category)
        parts.append(f"{self.start_date.isoformat()} to {self.end_date.isoformat()}")
        return " · ".join(parts)


# ---- raw aggregates produced by data.py (units already converted: Celsius, millimetres) ----


@dataclass(frozen=True)
class BusinessPoint:
    """One niche business assigned to an area. Carries no identity, name or coordinates."""

    checkins: int  # total over covered days, >= 0
    monthly: tuple[int, ...]  # len == covered_month_count; fully covered calendar months


@dataclass(frozen=True)
class CompetitorPoint:
    """Open niche business within R km of an area centroid (any area, even unassigned)."""

    stars: float | None
    review_count: int  # >= 0


@dataclass(frozen=True)
class WeatherDay:
    """Observed day for the area's dominant station; `None` means missing (never zero)."""

    day: date
    checkins: int  # niche check-ins of the area that day, 0 if none
    tmax_c: float | None
    precip_mm: float | None


@dataclass(frozen=True)
class AreaInputs:
    area_id: int
    postal_code: str
    display_name: str
    latitude: float | None
    longitude: float | None
    covered_days: int  # D >= 0, identical for every area in a scope
    covered_month_count: int  # M >= 0
    members: tuple[BusinessPoint, ...]  # all niche businesses (open + closed), N = len
    niche_open_count: int
    competitors: tuple[CompetitorPoint, ...] | None  # None: centroid unknown
    parent_business_count: int  # > 0 for every scored area
    parent_checkins: int
    parent_mean_stars: float | None
    parent_mean_review_count: float
    weather_station_id: str | None
    weather_days: tuple[WeatherDay, ...]  # empty when no mapped station

    @property
    def niche_business_count(self) -> int:
        return len(self.members)

    @property
    def niche_checkins(self) -> int:
        return sum(member.checkins for member in self.members)


@dataclass(frozen=True)
class UnscoredArea:
    area_id: int
    postal_code: str
    display_name: str
    reason: UnscoredReason


@dataclass(frozen=True)
class ScopeData:
    key: ScopeKey
    requested_start: date
    requested_end: date
    covered_start: date | None  # None when the range misses imported coverage
    covered_end: date | None
    covered_days: int
    covered_months: tuple[date, ...]  # first day of each fully covered calendar month
    radius_km: float  # effective R
    metro_mean_rate: float  # sum(niche check-ins) / (sum(N) * D); 0.0 if N total 0
    areas: tuple[AreaInputs, ...]  # scored candidates, sorted by area_id
    unscored: tuple[UnscoredArea, ...]  # sorted by postal_code


# ---- computed results ----


@dataclass(frozen=True)
class WeatherEffects:
    station_id: str | None
    observed_days: int  # days with tmax or precip present
    wet_days: int
    dry_days: int
    hot_days: int
    nonhot_days: int
    rain_effect: float | None  # signed; (wet - dry) / dry, month-stratified
    heat_effect: float | None  # signed; (hot - non-hot) / non-hot
    weather_resilience: float | None  # None unless both effects available


@dataclass(frozen=True)
class AreaMetrics:
    area_id: int
    postal_code: str
    display_name: str
    niche_business_count: int
    niche_open_count: int
    niche_checkins: int
    covered_days: int
    covered_month_count: int
    raw_demand_rate: float | None  # checkins / (N*D); None if N == 0
    model_rate: float  # r^ used (estimator, or metro mean if disabled)
    demand_rate: float  # r_final, check-ins per business-day
    demand_source: DemandSource
    model_fallback: bool  # True when the model part is the metro mean
    competition: int | None
    quality_gap: float | None
    mean_competitor_stars: float | None
    momentum: float | None
    stability: float | None
    weather_resilience: float | None
    rain_effect: float | None
    heat_effect: float | None
    weather_station_id: str | None
    weather_observed_days: int
    wet_days: int
    dry_days: int
    hot_days: int
    confidence: Confidence
    unavailable: tuple[MetricKey, ...]  # METRIC_KEYS order

    def value(self, key: MetricKey) -> float | None:
        """Raw metric value (competition as float); None when unavailable."""
        raw = getattr(self, key)
        return None if raw is None else float(raw)


@dataclass(frozen=True)
class FactorContribution:
    metric: MetricKey
    label: str
    raw_value: float | None
    percentile: float | None  # in [0, 1], already inverted for competition
    base_weight: float  # user weight
    effective_weight: float  # w' (0.0 if unavailable)
    contribution: float  # 100 * w' * p (0.0 if unavailable)
    delta_vs_median: float  # 100 * w' * (p - 0.5) (0.0 if unavailable)
    available: bool


@dataclass(frozen=True)
class AreaInterval:
    area_id: int
    score_low: float
    score_high: float
    rank_low: int
    rank_high: int
    tied_with_above: bool


@dataclass(frozen=True)
class AreaScore:
    area_id: int
    postal_code: str
    display_name: str
    rank: int  # 1-based, ordered by (-score, postal_code)
    score: float  # 0..100
    score_low: float | None
    score_high: float | None
    rank_low: int | None
    rank_high: int | None
    tied_with_above: bool
    factors: tuple[FactorContribution, ...]  # always 6, METRIC_KEYS order
    top_factors: tuple[MetricKey, ...]  # by |delta| desc; >= 3 when >= 3 available
    key_strength: MetricKey | None
    key_risk: MetricKey | None
    confidence: Confidence
    demand_source: DemandSource
    unavailable: tuple[MetricKey, ...]
    equal_weight_fallback: bool
    metrics: AreaMetrics


@dataclass(frozen=True, eq=False)
class BootstrapDraws:
    """Weight-independent bootstrap metric draws. NaN marks an unavailable metric."""

    area_ids: tuple[int, ...]  # ascending
    values: NDArray[np.float64]  # shape (n_draws, n_areas, 6), METRIC_KEYS order,
    # competition NOT inverted
    seed: int
    n_draws: int


@dataclass(frozen=True)
class EstimatorReport:
    enabled: bool
    reason: EstimatorReason
    n_train: int
    n_features: int
    feature_names: tuple[str, ...]
    mae_model: float | None  # LOO-CV, target space log1p(1000*r)
    mae_baseline: float | None
    skill: float | None  # 1 - mae_model/mae_baseline
    residual_sd: float | None  # sd (ddof=1) of LOO residuals, target space
    alpha: float | None


@dataclass(frozen=True)
class ResultWarning:
    code: WarningCode
    message: str


@dataclass(frozen=True)
class CoverageInfo:
    requested_start: date
    requested_end: date
    covered_start: date | None
    covered_end: date | None
    covered_days: int
    covered_month_count: int
    areas_scored: int
    areas_unscored: int
    areas_with_weather: int


@dataclass(frozen=True)
class PreparedScope:
    """Everything independent of weights; cached per ScopeKey."""

    key: ScopeKey
    coverage: CoverageInfo
    metro_mean_rate: float
    metrics: tuple[AreaMetrics, ...]  # sorted by area_id
    unscored: tuple[UnscoredArea, ...]
    estimator: EstimatorReport
    draws: BootstrapDraws
    config: ScoringConfig


@dataclass(frozen=True)
class RecommendationResult:
    key: ScopeKey
    weights: ScoreWeights
    scores: tuple[AreaScore, ...]  # ranked, rank ascending
    unscored: tuple[UnscoredArea, ...]
    estimator: EstimatorReport
    coverage: CoverageInfo
    warnings: tuple[ResultWarning, ...]
    n_bootstrap: int
    seed: int
