"""Cross-sectional demand estimator (deterministic; scikit-learn RidgeCV)."""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

import numpy as np
from numpy.typing import NDArray
from sklearn.linear_model import RidgeCV  # type: ignore[import-untyped]
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]

from sitesense.site_recommendation.metrics import raw_demand_rate
from sitesense.site_recommendation.models import (
    DEFAULT_CONFIG,
    AreaInputs,
    EstimatorReason,
    EstimatorReport,
    ScoringConfig,
)

_SKILL_TOLERANCE = 1e-9
_BASELINE_FLOOR = 1e-12


class EstimatorNotFittedError(RuntimeError):
    """predict()/report() called before fit()."""


def area_features(area: AreaInputs) -> tuple[float, float | None, float, float]:
    """(log1p(parent_business_count), parent_mean_stars, log1p(parent_mean_review_count),
    log1p(len(competitors) or 0)). Stars may be None (imputed with the training mean inside
    fit/predict). parent_checkins is deliberately excluded: when the niche equals the parent
    category it mechanically determines the target (check-ins per business-day)."""
    return (
        math.log1p(area.parent_business_count),
        area.parent_mean_stars,
        math.log1p(area.parent_mean_review_count),
        math.log1p(len(area.competitors or ())),
    )


_STARS_COLUMN = 1


def _feature_matrix(
    areas: Sequence[AreaInputs], stars_fill: float | None = None
) -> tuple[NDArray[np.float64], float]:
    """Feature matrix with missing stars imputed; returns (matrix, stars fill value).
    When `stars_fill` is None it is the mean of the known stars (0.0 if there are none)."""
    rows = [area_features(area) for area in areas]
    matrix = np.array(
        [[np.nan if value is None else value for value in row] for row in rows],
        dtype=np.float64,
    )
    column = matrix[:, _STARS_COLUMN]
    if stars_fill is None:
        known = column[~np.isnan(column)]
        stars_fill = float(known.mean()) if known.size else 0.0
    matrix[np.isnan(column), _STARS_COLUMN] = stars_fill
    return matrix, stars_fill


@dataclass(frozen=True)
class _FittedRidge:
    stars_fill: float
    scaler: StandardScaler
    model: RidgeCV

    def predict(self, matrix: NDArray[np.float64]) -> NDArray[np.float64]:
        return np.asarray(self.model.predict(self.scaler.transform(matrix)), dtype=np.float64)


def _fit_ridge(
    areas: Sequence[AreaInputs], target: NDArray[np.float64], alphas: tuple[float, ...]
) -> _FittedRidge:
    matrix, stars_fill = _feature_matrix(areas)
    scaler = StandardScaler().fit(matrix)
    model = RidgeCV(alphas=alphas).fit(scaler.transform(matrix), target)
    return _FittedRidge(stars_fill, scaler, model)


def _predict_one(fitted: _FittedRidge, area: AreaInputs) -> float:
    matrix, _ = _feature_matrix([area], fitted.stars_fill)
    return float(fitted.predict(matrix)[0])


class DemandEstimator:
    FEATURE_NAMES: ClassVar[tuple[str, ...]] = (
        "log1p_parent_businesses",
        "parent_mean_stars",
        "log1p_parent_mean_reviews",
        "log1p_open_niche_within_radius",
    )

    def __init__(self, config: ScoringConfig = DEFAULT_CONFIG) -> None:
        self._config = config
        self._metro_mean_rate = 0.0
        self._fitted: _FittedRidge | None = None
        self._report: EstimatorReport | None = None

    def fit(self, areas: Sequence[AreaInputs], metro_mean_rate: float) -> "DemandEstimator":
        """Train on areas with niche_business_count >= config.min_observed_businesses and
        covered_days > 0; target y = log1p(1000 * raw_demand_rate).
        n_train < config.min_estimator_train_areas -> disabled ('insufficient_training_areas',
        no CV computed). Else leave-one-out CV (imputation, StandardScaler and RidgeCV
        refit inside each fold): MAE of model vs baseline (fold-training-mean of y), both in
        target space. skill = 1 - mae_model/mae_baseline (skill = 0.0 if mae_baseline < 1e-12).
        skill <= 1e-9 (float tolerance for 'skill <= 0') -> disabled ('no_skill'). Returns self.
        Deterministic (no RNG)."""
        config = self._config
        self._metro_mean_rate = metro_mean_rate
        self._fitted = None
        train: list[AreaInputs] = []
        targets: list[float] = []
        for area in areas:
            rate = raw_demand_rate(
                area.niche_business_count, area.niche_checkins, area.covered_days
            )
            if rate is not None and area.niche_business_count >= config.min_observed_businesses:
                train.append(area)
                targets.append(math.log1p(1000.0 * rate))
        n_train = len(train)
        if n_train < config.min_estimator_train_areas:
            self._report = self._build_report("insufficient_training_areas", n_train)
            return self

        y = np.array(targets, dtype=np.float64)
        residuals = np.empty(n_train, dtype=np.float64)
        baseline_errors = np.empty(n_train, dtype=np.float64)
        for i in range(n_train):
            rest = train[:i] + train[i + 1 :]
            rest_y = np.delete(y, i)
            fold = _fit_ridge(rest, rest_y, config.ridge_alphas)
            residuals[i] = y[i] - _predict_one(fold, train[i])
            baseline_errors[i] = y[i] - float(rest_y.mean())
        mae_model = float(np.abs(residuals).mean())
        mae_baseline = float(np.abs(baseline_errors).mean())
        skill = 0.0 if mae_baseline < _BASELINE_FLOOR else 1.0 - mae_model / mae_baseline
        residual_sd = float(residuals.std(ddof=1))
        if skill <= _SKILL_TOLERANCE:
            self._report = self._build_report(
                "no_skill", n_train, mae_model, mae_baseline, skill, residual_sd
            )
            return self

        self._fitted = _fit_ridge(train, y, config.ridge_alphas)
        self._report = self._build_report(
            "ok",
            n_train,
            mae_model,
            mae_baseline,
            skill,
            residual_sd,
            float(self._fitted.model.alpha_),
        )
        return self

    def _build_report(
        self,
        reason: EstimatorReason,
        n_train: int,
        mae_model: float | None = None,
        mae_baseline: float | None = None,
        skill: float | None = None,
        residual_sd: float | None = None,
        alpha: float | None = None,
    ) -> EstimatorReport:
        return EstimatorReport(
            enabled=reason == "ok",
            reason=reason,
            n_train=n_train,
            n_features=len(self.FEATURE_NAMES),
            feature_names=self.FEATURE_NAMES,
            mae_model=mae_model,
            mae_baseline=mae_baseline,
            skill=skill,
            residual_sd=residual_sd,
            alpha=alpha,
        )

    def predict(self, area: AreaInputs) -> float:
        """Estimated demand rate (check-ins per business-day), >= 0. When disabled returns
        metro_mean_rate passed to fit(). Raises EstimatorNotFittedError before fit()."""
        if self._report is None:
            raise EstimatorNotFittedError("The estimator must be fitted before predict().")
        if self._fitted is None:
            return self._metro_mean_rate
        return max(0.0, math.expm1(_predict_one(self._fitted, area)) / 1000.0)

    def report(self) -> EstimatorReport:
        """Immutable summary. Raises EstimatorNotFittedError before fit()."""
        if self._report is None:
            raise EstimatorNotFittedError("The estimator must be fitted before report().")
        return self._report

    @property
    def enabled(self) -> bool:
        return self._fitted is not None
