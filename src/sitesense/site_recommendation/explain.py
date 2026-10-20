"""Facts builder and explainers (template + OpenAI-compatible)."""

import http.client
import json
import logging
import math
import os
import re
import time
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from http.client import HTTPMessage
from typing import IO, Final, Literal, Protocol
from urllib.parse import urlsplit

from sitesense.site_recommendation.models import (
    METRIC_KEYS,
    METRIC_LABELS,
    PROXY_NOTICE,
    AreaScore,
    ExplainKind,
    JsonObject,
    JsonValue,
    Language,
    MetricKey,
    NarrativeSource,
    RecommendationResult,
)

HttpTransport = Callable[[str, Mapping[str, str], bytes, float], tuple[int, bytes]]
"""(url, headers, body, timeout_seconds) -> (http_status, response_body).
May raise TimeoutError / OSError; must NOT follow redirects."""

ErrorCode = Literal["timeout", "network", "auth", "http_error", "bad_json", "empty"]

MAX_OUTPUT_CHARS: Final[int] = 2000
MAX_QUESTION_CHARS: Final[int] = 300
MAX_FACT_AREAS: Final[int] = 5
FACTS_SCHEMA_VERSION: Final[int] = 1
SYSTEM_PROMPT: Final[str] = (
    "You are an analyst assistant for SiteSense, a tool that ranks candidate ZIP areas for "
    "opening a new shop.\n"
    "You receive a JSON object named FACTS. Follow these rules strictly:\n"
    "1. Use ONLY numbers, ZIP codes and statements that appear in FACTS. Never invent, estimate "
    "or round to new values. Quote each factor as its display_value plus its unit.\n"
    "2. Check-ins are an activity proxy on a review platform. They are not customers, foot "
    "traffic or revenue. State this once.\n"
    "3. Mention uncertainty: score ranges, rank ranges, ties (tied_with_above), confidence, and "
    'any demand_source other than "observed".\n'
    "4. Weather effects are associations, not causes.\n"
    "5. Mention metrics listed in unavailable_metrics instead of guessing them.\n"
    '6. Do not promise outcomes. Use wording like "ranks higher on" or "is weaker on".\n'
    "7. No business names are provided; do not mention any.\n"
    "8. If the QUESTION cannot be answered from FACTS, say so briefly.\n"
    '9. The only valid statement about how many areas exist is "rank X of '
    'total_areas_scored" (for example "rank 3 of 56"). rank_range.best and rank_range.worst '
    "are an uncertainty interval for the rank; never use them as a count of areas.\n"
    "10. Never print raw JSON keys. Metric names: demand_rate = Demand / Nhu cầu; "
    "competition = Competition / Cạnh tranh; quality_gap = Quality gap / Khoảng trống chất "
    "lượng; momentum = Momentum / Đà tăng trưởng; stability = Stability / Độ ổn định; "
    "weather_resilience = Weather resilience / Khả năng chống chịu thời tiết. Use the English "
    "name when answering in English and the Vietnamese name when answering in Vietnamese. "
    'Always write "check-in" and "proxy" exactly like that, even in Vietnamese.\n'
    "11. Answer in {language_name}. Use at most 110 words and at most 2 bullets per area. "
    "Markdown bullet lists are allowed."
)
GROUNDING_NOTICES: Final[Mapping[Language, str]] = {
    "en": "Some figures in this AI text could not be matched to the data. "
    "Verify against the table above.",
    "vi": "Một số số liệu trong văn bản AI này không khớp với dữ liệu. "
    "Hãy đối chiếu với bảng ở trên.",
}

_LOGGER: Final = logging.getLogger("sitesense.site_recommendation")
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"localhost", "127.0.0.1", "::1"})
_LANGUAGE_NAMES: Final[Mapping[Language, str]] = {"en": "English", "vi": "Vietnamese"}
_TASKS: Final[Mapping[ExplainKind, str]] = {
    "top_picks": "Explain why the top areas rank where they do and the main trade-offs.",
    "compare": "Compare the selected areas: strengths, risks, and which kind of owner each suits.",
    "question": "Answer this QUESTION using FACTS only: ",
}
_MAX_RESPONSE_BYTES: Final[int] = 1024 * 1024
_MAX_ERROR_BYTES: Final[int] = 64 * 1024
_WEATHER_SOURCE: Final[str] = (
    "station observations converted to Celsius and millimetres; associations only"
)
_MIN_COMPARE_AREAS: Final[int] = 2


class ExplainerConfigError(ValueError):
    """Invalid AI environment configuration. Message never contains the API key."""


class ExplainerError(Exception):
    """Expected explainer failure; `message` is safe to display (no key, no payload)."""

    def __init__(self, code: ErrorCode, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code: Final = code
        self.message: Final = message
        self.status: Final = status


@dataclass(frozen=True, repr=False)
class ExplainerConfig:
    base_url: str  # no trailing slash
    model: str
    api_key: str = field(default="", repr=False)  # '' = no Authorization header (e.g. Ollama)
    timeout_s: float = 30.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ExplainerConfig | None":
        """Reads SITESENSE_AI_BASE_URL, SITESENSE_AI_MODEL (required, stripped, non-empty),
        SITESENSE_AI_API_KEY (optional), SITESENSE_AI_TIMEOUT (optional, float in (0, 300],
        default 30). environ=None -> os.environ. Returns None when base URL or model is missing/
        blank. Raises ExplainerConfigError: scheme not http/https; http with non-loopback host
        (localhost, 127.0.0.1, ::1); timeout not a number or out of range."""
        env = os.environ if environ is None else environ
        base_url = env.get("SITESENSE_AI_BASE_URL", "").strip().rstrip("/")
        model = env.get("SITESENSE_AI_MODEL", "").strip()
        if not base_url or not model:
            return None
        parts = urlsplit(base_url)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise ExplainerConfigError("SITESENSE_AI_BASE_URL must start with http:// or https://.")
        host = (parts.hostname or "").lower()
        if not host:
            raise ExplainerConfigError("SITESENSE_AI_BASE_URL must include a host name.")
        if parts.username is not None or parts.password is not None:
            raise ExplainerConfigError("SITESENSE_AI_BASE_URL must not contain credentials.")
        if scheme == "http" and host not in _LOOPBACK_HOSTS:
            raise ExplainerConfigError(
                "SITESENSE_AI_BASE_URL must use https unless the host is localhost, "
                "127.0.0.1 or ::1."
            )
        return cls(
            base_url=base_url,
            model=model,
            api_key=env.get("SITESENSE_AI_API_KEY", "").strip(),
            timeout_s=_parse_timeout(env.get("SITESENSE_AI_TIMEOUT", "")),
        )

    def host(self) -> str:
        """netloc of base_url, for display (never the key)."""
        return urlsplit(self.base_url).netloc

    def __repr__(self) -> str:
        masked = "'***'" if self.api_key else "''"
        return (
            f"ExplainerConfig(base_url={self.base_url!r}, model={self.model!r}, "
            f"api_key={masked}, timeout_s={self.timeout_s!r})"
        )

    __str__ = __repr__


def _parse_timeout(raw: str) -> float:
    text = raw.strip()
    if not text:
        return 30.0
    try:
        value = float(text)
    except ValueError:
        raise ExplainerConfigError("SITESENSE_AI_TIMEOUT must be a number of seconds.") from None
    if not math.isfinite(value) or not 0.0 < value <= 300.0:
        raise ExplainerConfigError("SITESENSE_AI_TIMEOUT must be greater than 0 and at most 300.")
    return value


@dataclass(frozen=True)
class ExplainRequest:
    kind: ExplainKind
    facts: JsonObject
    language: Language = "en"
    question: str | None = None  # <= MAX_QUESTION_CHARS, control chars stripped


@dataclass(frozen=True)
class Narrative:
    text: str  # <= MAX_OUTPUT_CHARS
    source: NarrativeSource
    language: Language
    model: str | None = None  # set when source == "llm"
    notice: str | None = None  # set on fallback, or when LLM figures fail the grounding check


class Explainer(Protocol):
    @property
    def name(self) -> str: ...

    def explain(self, request: ExplainRequest) -> Narrative:
        """Return a narrative or raise ExplainerError (never any other expected exception)."""
        ...


def sanitize_question(question: str | None) -> str | None:
    """Strip control characters, collapse whitespace, truncate to MAX_QUESTION_CHARS;
    None if empty after cleaning."""
    if question is None:
        return None
    kept = (
        " " if char.isspace() else char
        for char in question
        if char.isspace() or not unicodedata.category(char).startswith("C")
    )
    cleaned = " ".join("".join(kept).split())[:MAX_QUESTION_CHARS].rstrip()
    return cleaned or None


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


def _round(value: float, digits: int) -> float:
    return round(value, digits) + 0.0  # + 0.0 turns -0.0 into 0.0


def _round_opt(value: float | None, digits: int) -> float | None:
    return None if value is None else _round(value, digits)


_DISPLAY_UNITS: Final[Mapping[MetricKey, str]] = {
    "demand_rate": "check-ins per 1,000 business-days",
    "competition": "open competitors within {radius:g} km",
    "quality_gap": "5 minus mean stars",
    "momentum": "last 12 vs prior 12 months",
    "stability": "index, higher is steadier",
    "weather_resilience": "index, higher is more resilient",
}


def _display_value(metric: MetricKey, value: float | None) -> str | None:
    """The number exactly as the UI shows it (demand scaled to 1,000 business-days)."""
    if value is None:
        return None
    if metric == "demand_rate":
        return f"{value * 1000:.2f}"
    if metric == "competition":
        return str(round(value))
    if metric == "momentum":
        return f"{value:+.0%}"
    return f"{value:.2f}"


def _display_unit(metric: MetricKey, radius_km: float) -> str:
    return _DISPLAY_UNITS[metric].format(radius=radius_km)


def _area_facts(score: AreaScore, radius_km: float) -> JsonObject:
    factors: list[JsonValue] = []
    for factor in score.factors:
        factors.append(
            {
                "metric": factor.metric,
                "label": factor.label,
                "label_en": METRIC_LABELS[factor.metric],
                "label_vi": _VI_LABELS[factor.metric],
                "display_value": _display_value(factor.metric, factor.raw_value),
                "unit": _display_unit(factor.metric, radius_km),
                "percentile": _round_opt(factor.percentile, 4),
                "contribution": _round(factor.contribution, 1),
                "delta_vs_median": _round(factor.delta_vs_median, 1),
                "available": factor.available,
            }
        )
    flags: list[JsonValue] = []
    if score.metrics.model_fallback:
        flags.append("model_fallback")
    if score.equal_weight_fallback:
        flags.append("equal_weight_fallback")
    if score.confidence == "low":
        flags.append("low_confidence")
    if score.tied_with_above:
        flags.append("tied_with_above")
    rank_low = score.rank if score.rank_low is None else score.rank_low
    rank_high = score.rank if score.rank_high is None else score.rank_high
    score_range: JsonValue = (
        None
        if score.score_low is None or score.score_high is None
        else [_round(score.score_low, 1), _round(score.score_high, 1)]
    )
    top_factors: list[JsonValue] = [str(metric) for metric in score.top_factors]
    unavailable: list[JsonValue] = [str(metric) for metric in score.unavailable]
    return {
        "postal_code": score.postal_code,
        "rank": score.rank,
        "rank_range": {"best": rank_low, "worst": rank_high},
        "tied_with_above": score.tied_with_above,
        "score": _round(score.score, 1),
        "score_range": score_range,
        "confidence": score.confidence,
        "demand_source": score.demand_source,
        "niche_business_count": score.metrics.niche_business_count,
        "factors": factors,
        "top_factors": top_factors,
        "key_strength": score.key_strength,
        "key_risk": score.key_risk,
        "unavailable_metrics": unavailable,
        "flags": flags,
    }


def _select_scores(
    result: RecommendationResult,
    kind: ExplainKind,
    selected_area_ids: tuple[int, ...],
    top_n: int,
) -> tuple[AreaScore, ...]:
    ranked = result.scores
    if kind == "top_picks":
        if not 1 <= top_n <= MAX_FACT_AREAS:
            raise ValueError(f"top_n must be between 1 and {MAX_FACT_AREAS}.")
        return ranked[:top_n]
    known = {score.area_id for score in ranked}
    if kind == "compare":
        wanted = set(selected_area_ids)
        if not _MIN_COMPARE_AREAS <= len(wanted) <= MAX_FACT_AREAS:
            raise ValueError(
                f"Select between {_MIN_COMPARE_AREAS} and {MAX_FACT_AREAS} distinct areas."
            )
        if not wanted <= known:
            raise ValueError("All selected areas must be scored areas.")
        return tuple(score for score in ranked if score.area_id in wanted)
    chosen = [score for score in ranked if score.area_id in set(selected_area_ids)]
    chosen = chosen[:MAX_FACT_AREAS]
    for score in ranked:
        if len(chosen) >= MAX_FACT_AREAS:
            break
        if score not in chosen:
            chosen.append(score)
    return tuple(sorted(chosen, key=lambda score: score.rank))


def build_facts(
    result: RecommendationResult,
    *,
    kind: ExplainKind,
    selected_area_ids: tuple[int, ...] = (),
    top_n: int = 3,
    question: str | None = None,
) -> JsonObject:
    """Aggregated, privacy-safe facts (schema §7.2). kind='top_picks' -> top_n areas by rank;
    'compare' -> selected areas in rank order (ValueError unless 2..5 and all scored);
    'question' -> top 5 plus any selected (max MAX_FACT_AREAS). Floats rounded (score 1 dp,
    others 4 dp). Never includes display_name, business ids/names, coordinates, addresses.
    top_n must be 1..MAX_FACT_AREAS."""
    scores = _select_scores(result, kind, selected_area_ids, top_n)
    key = result.key
    estimator = result.estimator
    weights: dict[str, JsonValue] = {
        metric: _round(result.weights.get(metric), 4) for metric in METRIC_KEYS
    }
    radius = key.effective_radius_km()
    areas: list[JsonValue] = [_area_facts(score, radius) for score in scores]
    return {
        "schema_version": FACTS_SCHEMA_VERSION,
        "kind": kind,
        "total_areas_scored": len(result.scores),
        "scope": {
            "metro": f"{key.city}, {key.state}",
            "business_category": key.category,
            "source_category": key.source_category,
            "date_range": {
                "start": key.start_date.isoformat(),
                "end": key.end_date.isoformat(),
            },
            "covered_days": result.coverage.covered_days,
            "radius_km": _round(radius, 4),
        },
        "data_notes": {
            "checkins_are_activity_proxy": True,
            "weather_source": _WEATHER_SOURCE,
            "areas_scored": len(result.scores),
            "areas_not_scored": len(result.unscored),
            "estimator": {
                "enabled": estimator.enabled,
                "skill": _round_opt(estimator.skill, 4),
                "n_train": estimator.n_train,
            },
        },
        "weights": weights,
        "areas": areas,
        "question": sanitize_question(question) if kind == "question" else None,
    }


# ---------------------------------------------------------------------------
# Template text (shared by why_this_rank and TemplateExplainer)
# ---------------------------------------------------------------------------

_VI_LABELS: Final[Mapping[MetricKey, str]] = {
    "demand_rate": "Nhu cầu",
    "competition": "Cạnh tranh",
    "quality_gap": "Khoảng trống chất lượng",
    "momentum": "Đà tăng trưởng",
    "stability": "Độ ổn định",
    "weather_resilience": "Khả năng chống chịu thời tiết",
}

_TEXT: Final[Mapping[Language, Mapping[str, str]]] = {
    "en": {
        "rank_line": "ZIP {zip} ranks #{rank} of {n} (score {score:.1f}{range}).",
        "rank_short": "ranks #{rank} of {n} (score {score:.1f}{range}).",
        "range": ", range {lo:.1f}–{hi:.1f}",
        "tie": "Rank is statistically close to the area above (rank range {a}–{b}).",
        "strength": "Strongest factor: {label} ({value}; {delta:+.1f} vs. the median area).",
        "strength_short": "Strongest: {label} ({value}; {delta:+.1f} vs. median).",
        "risk": "Biggest risk: {label} ({value}; {delta:+.1f}).",
        "risk_short": "Risk: {label} ({value}; {delta:+.1f}).",
        "confidence": "Confidence: {confidence}; demand is {source}.",
        "unavailable": "Not available for this area: {labels}.",
        "fallback": "The model part is the metro average because the demand model is disabled.",
        "proxy": PROXY_NOTICE,
        "demand": "{value} check-ins per 1,000 business-days",
        "competition": "{count} open competitors within {radius}",
        "radius": "{radius:g} km",
        "radius_unknown": "the search radius",
        "src_observed": "observed",
        "src_blended": "a blend of observed data and a model estimate",
        "src_model": "estimated by a model (no niche businesses observed)",
        "conf_high": "high",
        "conf_medium": "medium",
        "conf_low": "low",
        "top_header": "Offline summary of the top {k} of {n} areas:",
        "compare_header": "Offline summary comparing {k} selected areas:",
        "compare_line": "{zip} ranks higher on {label}",
        "compare_prefix": "Comparison by key strength: ",
        "question": "The offline summary cannot answer free-text questions; "
        "here are the top areas:",
        "empty": "No areas are available for this summary.",
        "unknown": "n/a",
    },
    "vi": {
        "rank_line": "ZIP {zip} xếp hạng #{rank}/{n} (điểm {score:.1f}{range}).",
        "rank_short": "xếp hạng #{rank}/{n} (điểm {score:.1f}{range}).",
        "range": ", khoảng {lo:.1f}–{hi:.1f}",
        "tie": "Hạng gần như ngang với khu vực phía trên (khoảng hạng {a}–{b}).",
        "strength": "Yếu tố mạnh nhất: {label} ({value}; {delta:+.1f} so với khu vực trung vị).",
        "strength_short": "Mạnh nhất: {label} ({value}; {delta:+.1f} so với trung vị).",
        "risk": "Rủi ro lớn nhất: {label} ({value}; {delta:+.1f}).",
        "risk_short": "Rủi ro: {label} ({value}; {delta:+.1f}).",
        "confidence": "Độ tin cậy: {confidence}; nhu cầu {source}.",
        "unavailable": "Không có dữ liệu cho khu vực này: {labels}.",
        "fallback": "Phần mô hình dùng mức trung bình của metro vì mô hình nhu cầu bị tắt.",
        "proxy": "Check-in chỉ là chỉ báo hoạt động (proxy), không phải lượng khách hay doanh thu.",
        "demand": "{value} check-in trên 1,000 business-day",
        "competition": "{count} đối thủ đang mở trong {radius}",
        "radius": "bán kính {radius:g} km",
        "radius_unknown": "bán kính tìm kiếm",
        "src_observed": "được quan sát",
        "src_blended": "kết hợp dữ liệu quan sát và ước lượng của mô hình",
        "src_model": "được mô hình ước lượng (không quan sát được business nào trong ngách)",
        "conf_high": "cao",
        "conf_medium": "trung bình",
        "conf_low": "thấp",
        "top_header": "Tóm tắt ngoại tuyến cho {k}/{n} khu vực hàng đầu:",
        "compare_header": "Tóm tắt ngoại tuyến so sánh {k} khu vực đã chọn:",
        "compare_line": "{zip} xếp cao hơn ở {label}",
        "compare_prefix": "So sánh theo thế mạnh chính: ",
        "question": "Bản tóm tắt ngoại tuyến không thể trả lời câu hỏi tự do; "
        "các khu vực hàng đầu:",
        "empty": "Không có khu vực nào để tóm tắt.",
        "unknown": "n/a",
    },
}


@dataclass(frozen=True)
class _FactorView:
    metric: MetricKey
    value: str | None  # display value, as the UI shows it
    delta: float


@dataclass(frozen=True)
class _AreaView:
    zip_code: str
    rank: int
    n_scored: int
    score: float
    score_range: tuple[float, float] | None
    rank_range: tuple[int, int]
    tied: bool
    confidence: str
    source: str
    strength: _FactorView | None
    risk: _FactorView | None
    unavailable: tuple[MetricKey, ...]
    model_fallback: bool
    radius_km: float | None


def _label(metric: MetricKey, language: Language) -> str:
    return _VI_LABELS[metric] if language == "vi" else METRIC_LABELS[metric]


def _format_value(
    metric: MetricKey, display: str | None, radius_km: float | None, language: Language
) -> str:
    text = _TEXT[language]
    if display is None:
        return text["unknown"]
    if metric == "demand_rate":
        return text["demand"].format(value=display)
    if metric == "competition":
        radius = (
            text["radius_unknown"] if radius_km is None else text["radius"].format(radius=radius_km)
        )
        return text["competition"].format(count=display, radius=radius)
    return display


def _source_text(source: str, language: Language) -> str:
    return _TEXT[language].get(f"src_{source}", source)


def _confidence_text(confidence: str, language: Language) -> str:
    return _TEXT[language].get(f"conf_{confidence}", confidence)


def _head(view: _AreaView, language: Language, *, short: bool) -> str:
    text = _TEXT[language]
    score_range = (
        ""
        if view.score_range is None
        else text["range"].format(lo=view.score_range[0], hi=view.score_range[1])
    )
    key = "rank_short" if short else "rank_line"
    line = text[key].format(
        zip=view.zip_code,
        rank=view.rank,
        n=view.n_scored,
        score=view.score,
        range=score_range,
    )
    if view.tied:
        line += " " + text["tie"].format(a=view.rank_range[0], b=view.rank_range[1])
    return line


def _factor_sentence(
    view: _AreaView, factor: _FactorView | None, kind: str, language: Language
) -> str | None:
    if factor is None:
        return None
    return _TEXT[language][kind].format(
        label=_label(factor.metric, language),
        value=_format_value(factor.metric, factor.value, view.radius_km, language),
        delta=factor.delta,
    )


def _render_full(view: _AreaView, language: Language) -> str:
    text = _TEXT[language]
    lines = [
        _head(view, language, short=False),
        _factor_sentence(view, view.strength, "strength", language),
        _factor_sentence(view, view.risk, "risk", language),
        text["confidence"].format(
            confidence=_confidence_text(view.confidence, language),
            source=_source_text(view.source, language),
        ),
    ]
    caveats = [text["proxy"]]
    if view.unavailable:
        labels = ", ".join(_label(metric, language) for metric in view.unavailable)
        caveats.append(text["unavailable"].format(labels=labels))
    if view.model_fallback:
        caveats.append(text["fallback"])
    lines.append(" ".join(caveats))
    return "\n".join(line for line in lines if line)


def _render_short(view: _AreaView, language: Language) -> str:
    text = _TEXT[language]
    parts = [
        _head(view, language, short=True),
        _factor_sentence(view, view.strength, "strength_short", language),
        _factor_sentence(view, view.risk, "risk_short", language),
        text["confidence"].format(
            confidence=_confidence_text(view.confidence, language),
            source=_source_text(view.source, language),
        ),
    ]
    if view.unavailable:
        labels = ", ".join(_label(metric, language) for metric in view.unavailable)
        parts.append(text["unavailable"].format(labels=labels))
    if view.model_fallback:
        parts.append(text["fallback"])
    return " ".join(part for part in parts if part)


def why_this_rank(
    score: AreaScore,
    n_scored: int,
    language: Language = "en",
    *,
    radius_km: float | None = None,
) -> str:
    """Deterministic 'Why this rank' text (template §7.3). Pure. `radius_km` (optional,
    keyword-only) completes the competition wording; without it 'the search radius' is used."""

    def factor_view(metric: MetricKey | None) -> _FactorView | None:
        if metric is None:
            return None
        for factor in score.factors:
            if factor.metric == metric:
                return _FactorView(
                    metric, _display_value(metric, factor.raw_value), factor.delta_vs_median
                )
        return None

    score_range = (
        (score.score_low, score.score_high)
        if score.score_low is not None and score.score_high is not None
        else None
    )
    view = _AreaView(
        zip_code=score.postal_code,
        rank=score.rank,
        n_scored=n_scored,
        score=score.score,
        score_range=score_range,
        rank_range=(
            score.rank if score.rank_low is None else score.rank_low,
            score.rank if score.rank_high is None else score.rank_high,
        ),
        tied=score.tied_with_above,
        confidence=score.confidence,
        source=score.demand_source,
        strength=factor_view(score.key_strength),
        risk=factor_view(score.key_risk),
        unavailable=score.unavailable,
        model_fallback=score.metrics.model_fallback,
        radius_km=radius_km,
    )
    return _render_full(view, language)


# ---- tolerant readers for facts (the template only trusts request.facts) ----


def _obj(value: JsonValue) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _items(value: JsonValue) -> list[JsonValue]:
    return value if isinstance(value, list) else []


def _num(value: JsonValue) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _str(value: JsonValue, default: str = "") -> str:
    return value if isinstance(value, str) else default


def _metric(value: JsonValue) -> MetricKey | None:
    for key in METRIC_KEYS:
        if value == key:
            return key
    return None


def _optional_factor(
    factors: Mapping[MetricKey, JsonObject], metric: JsonValue
) -> _FactorView | None:
    key = _metric(metric)
    if key is None or key not in factors:
        return None
    entry = factors[key]
    display = entry.get("display_value")
    return _FactorView(
        key,
        display if isinstance(display, str) else _display_value(key, _num(entry.get("value"))),
        _num(entry.get("delta_vs_median")) or 0.0,
    )


def _views_from_facts(facts: JsonObject) -> list[_AreaView]:
    notes = _obj(facts.get("data_notes", {}))
    radius = _num(_obj(facts.get("scope", {})).get("radius_km"))
    areas = [_obj(item) for item in _items(facts.get("areas", []))]
    n_scored = int(_num(facts.get("total_areas_scored")) or _num(notes.get("areas_scored")) or 0)
    n_scored = n_scored or len(areas)
    views: list[_AreaView] = []
    for area in areas:
        factors: dict[MetricKey, JsonObject] = {}
        for item in _items(area.get("factors", [])):
            entry = _obj(item)
            key = _metric(entry.get("metric"))
            if key is not None:
                factors[key] = entry
        rank = int(_num(area.get("rank")) or 0)
        raw_range = area.get("rank_range")
        if isinstance(raw_range, dict):  # {"best": a, "worst": b}; a list is the legacy shape
            raw_range = [raw_range.get("best"), raw_range.get("worst")]
        rank_values = [int(v) for v in map(_num, _items(raw_range)) if v]
        rank_range = (rank_values[0], rank_values[1]) if len(rank_values) == 2 else (rank, rank)
        range_values = [v for v in map(_num, _items(area.get("score_range", []))) if v is not None]
        unavailable = tuple(
            key
            for key in (_metric(item) for item in _items(area.get("unavailable_metrics", [])))
            if key is not None
        )
        flags = {_str(flag) for flag in _items(area.get("flags", []))}
        views.append(
            _AreaView(
                zip_code=_str(area.get("postal_code")),
                rank=rank,
                n_scored=n_scored,
                score=_num(area.get("score")) or 0.0,
                score_range=(range_values[0], range_values[1]) if len(range_values) == 2 else None,
                rank_range=rank_range,
                tied=area.get("tied_with_above") is True,
                confidence=_str(area.get("confidence"), "low"),
                source=_str(area.get("demand_source"), "observed"),
                strength=_optional_factor(factors, area.get("key_strength")),
                risk=_optional_factor(factors, area.get("key_risk")),
                unavailable=unavailable,
                model_fallback="model_fallback" in flags,
                radius_km=radius,
            )
        )
    views.sort(key=lambda view: view.rank)
    return views


_NUMBER = re.compile(r"(?<![A-Za-z])\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![A-Za-z])\d+(?:[.,]\d+)?")
_POPULATION = re.compile(
    r"(?:\bof|trong\s+số)\s+(\d+)(?!\d|[.,]\d)|#\d+\s*/\s*(\d+)(?!\d|[.,]\d)", re.IGNORECASE
)


def _candidates(token: str) -> list[tuple[float, int]]:
    """Readings of a numeric token as (value, decimals); '1,000' / '1.000' are ambiguous."""
    readings: list[tuple[float, int]] = []
    decimal = token.replace(",", ".") if token.count(",") + token.count(".") == 1 else token
    for text in (decimal, token.replace(",", "").replace(".", "")):
        try:
            value = float(text)
        except ValueError:
            continue
        reading = (value, len(text.split(".")[1]) if "." in text else 0)
        if reading not in readings:
            readings.append(reading)
    return readings


def _fact_numbers(value: JsonValue, out: list[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int | float):
        out.append(float(value))
    elif isinstance(value, str):
        for token in _NUMBER.findall(value):
            out.extend(number for number, _ in _candidates(token))
    elif isinstance(value, list):
        for child in value:
            _fact_numbers(child, out)
    else:
        for child in value.values():
            _fact_numbers(child, out)


def unmatched_figures(text: str, facts: JsonObject) -> tuple[str, ...]:
    """Figures in `text` that cannot be found in `facts` (first occurrence order, unique).
    Tolerates rounding to the number of decimals written, '1,000' vs '1000' and percent forms of
    fractions; ignores signs, bare integers 0-9 (ordinals, list markers) and years 1900-2100.
    A population statement ('of N', 'trong số N', '#r/N') with N != total_areas_scored is
    always unmatched."""
    known: list[float] = []
    _fact_numbers(facts, known)
    known.extend(number * 100 for number in list(known) if abs(number) <= 1.5)
    total = _num(facts.get("total_areas_scored"))
    found: list[str] = []
    for match in _POPULATION.finditer(text):
        count = match.group(1) or match.group(2)
        if total is None or float(count) != total:
            found.append(match.group(0).strip())
    for match in _NUMBER.finditer(text):
        token = match.group(0)
        readings = _candidates(token)
        integers = [value for value, decimals in readings if decimals == 0]
        if any(0 <= value <= 9 or 1900 <= value <= 2100 for value in integers):
            continue
        if not any(
            abs(round(k, decimals) - value) < 1e-9 for value, decimals in readings for k in known
        ):
            found.append(token)
    return tuple(dict.fromkeys(found))


def _cap(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[: MAX_OUTPUT_CHARS - 1].rstrip() + "…"


class TemplateExplainer:
    name: str = "template"

    def explain(self, request: ExplainRequest) -> Narrative:
        """Deterministic offline narrative built only from request.facts. source='template',
        no notice. Never raises ExplainerError."""
        language = request.language
        text = _TEXT[language]
        views = _views_from_facts(request.facts)
        blocks: list[str] = []
        if not views:
            blocks.append(text["empty"])
        elif request.kind == "question":
            blocks.append(text["question"])
            blocks.append(self._bullets(views[:3], language))
        elif request.kind == "compare":
            blocks.append(text["compare_header"].format(k=len(views)))
            blocks.append(self._bullets(views, language))
            blocks.append(self._contrast(views, language))
        else:
            n_scored = views[0].n_scored
            blocks.append(text["top_header"].format(k=len(views), n=n_scored))
            blocks.append(self._bullets(views, language))
        blocks.append(text["proxy"])
        body = "\n\n".join(block for block in blocks if block)
        return Narrative(text=_cap(body), source="template", language=language)

    @staticmethod
    def _bullets(views: Sequence[_AreaView], language: Language) -> str:
        return "\n".join(
            f"- **{view.zip_code}**: {_render_short(view, language)}" for view in views
        )

    @staticmethod
    def _contrast(views: Sequence[_AreaView], language: Language) -> str:
        text = _TEXT[language]
        pieces = [
            text["compare_line"].format(
                zip=view.zip_code, label=_label(view.strength.metric, language)
            )
            for view in views
            if view.strength is not None
        ]
        return "" if not pieces else text["compare_prefix"] + "; ".join(pieces) + "."


# ---------------------------------------------------------------------------
# HTTP transport and OpenAI-compatible explainer
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Authorization header is never forwarded; 3xx surfaces as an
    HTTPError and is returned as a plain status."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> None:
        return None


def urllib_transport(
    url: str, headers: Mapping[str, str], body: bytes, timeout: float
) -> tuple[int, bytes]:
    """POST via urllib.request with an opener that refuses redirects (3xx returned as status).
    HTTPError -> (code, up to 64 KiB of body). Response reads capped at 1 MiB. Timeouts raise
    TimeoutError, other URLError/OSError propagate as OSError."""
    handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
    host = (urlsplit(url).hostname or "").lower()
    if host in _LOOPBACK_HOSTS:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")
    try:
        with opener.open(request, timeout=timeout) as response:
            return int(response.status), response.read(_MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(_MAX_ERROR_BYTES)
        except (OSError, http.client.HTTPException):
            return exc.code, b""
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise TimeoutError("request timed out") from None
        raise OSError("connection failed") from None
    except http.client.HTTPException:
        raise OSError("invalid HTTP response") from None


def _strip_controls(text: str) -> str:
    return "".join(
        char for char in text if char in "\n\t" or not unicodedata.category(char).startswith("C")
    )


class OpenAICompatibleExplainer:
    name: str = "openai-compatible"

    def __init__(self, config: ExplainerConfig, transport: HttpTransport | None = None) -> None:
        """transport=None -> resolve module attribute `urllib_transport` at call time."""
        self._config = config
        self._transport = transport

    def build_request_body(self, request: ExplainRequest) -> bytes:
        """UTF-8 JSON: {"model", "messages": [system, user], "temperature": 0.2,
        "max_tokens": 800}; user message per §7.1."""
        system = SYSTEM_PROMPT.replace("{language_name}", _LANGUAGE_NAMES[request.language])
        facts_json = json.dumps(
            request.facts, separators=(",", ":"), sort_keys=True, ensure_ascii=False
        )
        question = sanitize_question(request.question)
        if question is None and request.kind == "question":
            question = sanitize_question(_str(request.facts.get("question")))
        if request.kind == "question" and question is not None:
            task = _TASKS["question"] + question
        else:
            task = _TASKS["top_picks" if request.kind == "question" else request.kind]
        user = f"FACTS:\n```json\n{facts_json}\n```\nTASK: {task}"
        payload = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": 800,
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def parse_response(self, body: bytes) -> str:
        """choices[0].message.content as stripped str. Raises ExplainerError('bad_json') when
        the body is not JSON / lacks the path / content not str; ExplainerError('empty') if the
        stripped text is empty. Truncates to MAX_OUTPUT_CHARS (ending with '…')."""
        unreadable = ExplainerError("bad_json", "The AI provider returned an unreadable response.")
        try:
            data: JsonValue = json.loads(body)
        except (ValueError, RecursionError):
            raise unreadable from None
        choices = _items(_obj(data).get("choices"))
        message = _obj(_obj(choices[0]).get("message")) if choices else {}
        content = message.get("content")
        if not isinstance(content, str):
            raise unreadable
        text = _strip_controls(content).strip()
        if not text:
            raise ExplainerError("empty", "The AI provider returned an empty answer.")
        return _cap(text)

    def explain(self, request: ExplainRequest) -> Narrative:
        """POST {base_url}/chat/completions with headers Content-Type: application/json,
        Accept: application/json and Authorization: Bearer <key> only when key != ''.
        Maps: TimeoutError -> 'timeout'; other OSError -> 'network'; 401/403 -> 'auth';
        other status outside 200..299 -> 'http_error' (status kept); then parse_response.
        Returns Narrative(source='llm', model=config.model). Never logs key/payload."""
        config = self._config
        body = self.build_request_body(request)
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if config.api_key:
            headers["Authorization"] = f"Bearer {config.api_key}"
        _LOGGER.info(
            "ai_request provider_host=%s model=%s kind=%s payload_bytes=%d",
            config.host(),
            config.model,
            request.kind,
            len(body),
        )
        transport = self._transport if self._transport is not None else urllib_transport
        try:
            status, response = transport(
                f"{config.base_url}/chat/completions", headers, body, config.timeout_s
            )
        except TimeoutError:
            raise ExplainerError("timeout", "The AI provider timed out.") from None
        except OSError:
            raise ExplainerError("network", "The AI provider could not be reached.") from None
        if status in (401, 403):
            raise ExplainerError("auth", "The AI provider rejected the credentials.", status)
        if not 200 <= status <= 299:
            raise ExplainerError("http_error", "The AI provider returned an HTTP error.", status)
        text = self.parse_response(response)
        unmatched = unmatched_figures(text, request.facts)
        if unmatched:
            _LOGGER.info("ai_ungrounded count=%d", len(unmatched))
        return Narrative(
            text=text,
            source="llm",
            language=request.language,
            model=config.model,
            notice=GROUNDING_NOTICES[request.language] if unmatched else None,
        )


def fallback_notice(error: ExplainerError, timeout_s: float) -> str:
    """User-facing notice (exact strings, §7.4)."""
    suffix = "Showing the offline summary."
    status = "unknown" if error.status is None else str(error.status)
    match error.code:
        case "timeout":
            return f"AI provider timed out after {timeout_s:g} s. {suffix}"
        case "network":
            return f"AI provider could not be reached. {suffix}"
        case "auth":
            return f"AI provider rejected the credentials (HTTP {status}). {suffix}"
        case "http_error":
            return f"AI provider returned HTTP {status}. {suffix}"
        case "bad_json":
            return f"AI provider returned an unreadable response. {suffix}"
        case "empty":
            return f"AI provider returned an empty answer. {suffix}"


def explain_with_fallback(
    primary: Explainer,
    request: ExplainRequest,
    *,
    fallback: Explainer | None = None,
    timeout_s: float = 30.0,
) -> Narrative:
    """Call primary; on ExplainerError return fallback (default TemplateExplainer()) result with
    notice=fallback_notice(...), source='template'. Logs WARNING with code/status only."""
    started = time.perf_counter()
    try:
        return primary.explain(request)
    except ExplainerError as exc:
        _LOGGER.warning(
            "ai_fallback code=%s status=%s elapsed_ms=%d",
            exc.code,
            exc.status,
            round((time.perf_counter() - started) * 1000),
        )
        narrative = (fallback if fallback is not None else TemplateExplainer()).explain(request)
        return replace(
            narrative,
            source="template",
            model=None,
            notice=fallback_notice(exc, timeout_s),
        )


def build_explainer(
    config: ExplainerConfig | None, transport: HttpTransport | None = None
) -> Explainer:
    """TemplateExplainer when config is None, else OpenAICompatibleExplainer."""
    if config is None:
        return TemplateExplainer()
    return OpenAICompatibleExplainer(config, transport)
