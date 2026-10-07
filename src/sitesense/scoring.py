"""Ranking scores, factor contributions, scenario forecasts and template explanations.

Pure functions over the serving tables; no Streamlit imports.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Factor:
    key: str
    label: str
    column: str


FACTORS = (
    Factor("demand", "Demand level", "demand_norm"),
    Factor("growth", "Growth trend", "growth_norm"),
    Factor("rating", "Customer rating", "rating_norm"),
    Factor("resilience", "Weather resilience", "resilience_norm"),
    Factor("competition", "Low competition", "competition_norm"),
)
DEFAULT_WEIGHTS = {"demand": 35, "growth": 20, "rating": 15, "resilience": 15, "competition": 15}
MIN_EVENTS = 20
ALPHA = 0.05


def normalise_weights(weights: Mapping[str, float]) -> dict[str, float]:
    """Scale weights to sum to 1; all-zero weights fall back to equal weights."""
    values = {factor.key: max(0.0, float(weights.get(factor.key, 0))) for factor in FACTORS}
    total = sum(values.values())
    if total == 0:
        return {key: 1 / len(values) for key in values}
    return {key: value / total for key, value in values.items()}


def score_areas(factors: pd.DataFrame, weights: Mapping[str, float]) -> pd.DataFrame:
    """Add a 0-100 `score` and a 1-based `rank`, best first."""
    shares = normalise_weights(weights)
    scored = factors.copy()
    scored["score"] = sum(shares[f.key] * scored[f.column] for f in FACTORS) * 100
    scored = scored.sort_values(["score", "area_id"], ascending=[False, True])
    scored["rank"] = range(1, len(scored) + 1)
    return scored.reset_index(drop=True)


def baseline_score(factors: pd.DataFrame, weights: Mapping[str, float]) -> float:
    """Score of a hypothetical area at the metro average of every factor."""
    shares = normalise_weights(weights)
    return float(sum(shares[f.key] * factors[f.column].mean() for f in FACTORS) * 100)


def contributions(
    factors: pd.DataFrame, area_id: str, weights: Mapping[str, float]
) -> pd.DataFrame:
    """Points each factor adds to (or removes from) the area's score vs the metro average.

    The points sum to the area's score minus `baseline_score`.
    """
    shares = normalise_weights(weights)
    row = factors[factors.area_id == area_id].iloc[0]
    points = [
        shares[f.key] * (float(row[f.column]) - float(factors[f.column].mean())) * 100
        for f in FACTORS
    ]
    table = pd.DataFrame(
        {"factor": [f.key for f in FACTORS], "label": [f.label for f in FACTORS], "points": points}
    )
    return table.sort_values("points", ascending=False).reset_index(drop=True)


def _signed(points: float) -> str:
    return f"{points:+.0f}".replace("-", "−")


def explain_area(name: str, score: float, rank: int, contribution: pd.DataFrame) -> str:
    """One or two plain sentences naming the biggest positive and negative factors."""
    raises = contribution[contribution.points >= 0.5].head(2)
    lowers = contribution[contribution.points <= -0.5].sort_values("points").head(1)
    sentence = f"{name} ranks #{rank} with {score:.0f}/100"
    if len(raises):
        reasons = " and ".join(
            f"{label.lower()} ({_signed(points)})"
            for label, points in zip(raises.label, raises.points, strict=True)
        )
        sentence += f", mainly because of {reasons}."
    else:
        sentence += "; no factor is clearly above the metro average."
    if len(lowers):
        label, points = lowers.label.iloc[0], float(lowers.points.iloc[0])
        sentence += f" {label} pulls the score down ({_signed(points)})."
    return sentence


def is_reliable(n_events: int, p_value: float) -> bool:
    """Enough event days and a confidence interval that excludes zero."""
    return n_events >= MIN_EVENTS and p_value < ALPHA


def sensitivity_label(effect_pct: float) -> str:
    """Low / Medium / High from the size of the heavy-rain change in check-ins."""
    size = abs(effect_pct)
    if size < 10:
        return "Low"
    if size < 18:
        return "Medium"
    return "High"


def scenario_forecast(forecast: pd.DataFrame, effect_pct: float, week: int) -> pd.DataFrame:
    """Apply a weather effect to one forecast week; the 80% band is scaled the same way."""
    table = forecast.sort_values("week_offset").reset_index(drop=True)
    factor = (table.week_offset == week).map({True: 1 + effect_pct / 100, False: 1.0})
    table["scenario"] = table.yhat * factor
    table["scenario_lo"] = table.lo80 * factor
    table["scenario_hi"] = table.hi80 * factor
    return table


def compare_takeaway(cards: pd.DataFrame) -> str:
    """Template takeaway for the comparison page.

    `cards` needs: name, score, growth_yoy, seasonality_ratio, competitor_count, sensitivity.
    """
    table = cards.reset_index(drop=True)
    best_at = int(table.score.to_numpy().argmax())
    best = table.iloc[best_at]
    text = (
        f"{best['name']} scores highest ({float(best['score']):.0f}/100) with "
        f"{str(best['sensitivity']).lower()} weather sensitivity and "
        f"{int(best['competitor_count'])} competitors nearby."
    )
    others = table.drop(index=best_at).reset_index(drop=True)
    if len(others):
        growth = others.iloc[int(others.growth_yoy.to_numpy().argmax())]
        text += (
            f" {growth['name']} grows fastest "
            f"({float(growth['growth_yoy']):+.0%} share change in 2019)."
        )
        seasonal = table.iloc[int(table.seasonality_ratio.to_numpy().argmax())]
        if seasonal["name"] not in (best["name"], growth["name"]):
            text += (
                f" {seasonal['name']} is the most seasonal: its busiest month has "
                f"{float(seasonal['seasonality_ratio']):.1f}× the check-ins of its quietest."
            )
    return text
