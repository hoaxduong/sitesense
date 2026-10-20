"""Streamlit rendering. The only module importing streamlit."""

import csv
import hashlib
import io
import json
import logging
import time
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, cast

import pandas as pd  # type: ignore[import-untyped]
import psycopg
import pydeck as pdk  # type: ignore[import-untyped]
import streamlit as st

from sitesense import repository, zip_boundaries
from sitesense.models import DateCoverage, Filters
from sitesense.site_recommendation import data, explain, scoring
from sitesense.site_recommendation.models import (
    DEFAULT_CONFIG,
    METRIC_KEYS,
    METRIC_LABELS,
    PRESETS,
    PROXY_NOTICE,
    AreaScore,
    Confidence,
    EstimatorReport,
    ExplainKind,
    JsonObject,
    Language,
    MetricKey,
    PreparedScope,
    RecommendationResult,
    ScopeKey,
    ScopeUnavailableError,
    ScoreWeights,
    UnscoredArea,
)
from sitesense.ui import ScreenContext, render_setup
from sitesense.weather_map import _fit_view, _geometry_points

_LOGGER = logging.getLogger("sitesense.site_recommendation")

_DASH = "—"
_RANGE_DASH = "–"
_MAP_HEIGHT = 430
_NEUTRAL_COLOR = [190, 190, 190, 120]
_LOW_COLOR = (255, 255, 204)
_HIGH_COLOR = (37, 52, 148)
_PRESET_OPTIONS = (*PRESETS, "Custom")
_CONFIDENCE_OPTIONS = ("Low", "Medium", "High")
_LANGUAGES: Mapping[str, Language] = {"English": "en", "Tiếng Việt": "vi"}
_TOP_PICKS = 3
_MAX_AI_QUESTION = explain.MAX_QUESTION_CHARS
_CSV_HEADER = (
    "rank",
    "rank_low",
    "rank_high",
    "tied_with_above",
    "postal_code",
    "display_name",
    "score",
    "score_low",
    "score_high",
    "confidence",
    "demand_source",
    "niche_business_count",
    "demand_rate_per_1000_business_days",
    "competition",
    "quality_gap",
    "momentum",
    "stability",
    "weather_resilience",
    "rain_effect",
    "heat_effect",
    "key_strength",
    "key_risk",
    "unavailable_metrics",
    "notes",
)
_METRIC_ROW_LABELS: Mapping[MetricKey, str] = {
    "demand_rate": "Demand (check-ins per 1,000 business-days)",
    "competition": "Competition (open competitors)",
    "quality_gap": "Quality gap",
    "momentum": "Momentum",
    "stability": "Stability",
    "weather_resilience": "Weather resilience",
}
_MAP_TOOLTIP_STYLE = {
    "transform": "none",
    "top": "12px",
    "left": "12px",
    "backgroundColor": "#ffffff",
    "color": "#192f29",
    "border": "1px solid #dce5df",
    "borderRadius": "8px",
    "boxShadow": "0 4px 16px rgba(25, 47, 41, 0.16)",
    "padding": "12px 14px",
    "fontFamily": "sans-serif",
    "fontSize": "13px",
    "lineHeight": "1.5",
    "width": "220px",
    "maxWidth": "calc(100% - 24px)",
    "boxSizing": "border-box",
}
_MAP_TOOLTIP_HTML = (
    '<div style="font-weight:600">ZIP {postal_code}</div>'
    '<div style="font-size:12px;margin-top:6px">Score</div>'
    '<div style="font-size:24px;font-weight:600;line-height:1.3">{score_text}</div>'
    '<div style="font-size:12px">Rank {rank_text} · Confidence {confidence}</div>'
)
_MAP_CSS = (
    "<style>.st-key-site_rec_map_view .deck-widgets-root "
    "{position:absolute;inset:0;pointer-events:none}</style>"
)


# ---------------------------------------------------------------------------------------------
# Pure helpers (no Streamlit calls)
# ---------------------------------------------------------------------------------------------


def format_rank(score: AreaScore) -> str:
    """'<rank_low>–<rank_high>' (en dash) when tied_with_above and both are set, else str(rank)."""
    if score.tied_with_above and score.rank_low is not None and score.rank_high is not None:
        return f"{score.rank_low}{_RANGE_DASH}{score.rank_high}"
    return str(score.rank)


def _score_range(score: AreaScore) -> str:
    if score.score_low is None or score.score_high is None:
        return _DASH
    return f"{score.score_low:.1f}{_RANGE_DASH}{score.score_high:.1f}"


def _label(key: MetricKey | None) -> str:
    return _DASH if key is None else METRIC_LABELS[key]


def ranked_table(scores: Sequence[AreaScore]) -> pd.DataFrame:
    """Columns (exact): Rank, ZIP, Score, Score range, Confidence, Demand source, Key strength,
    Key risk. Score '%.1f'; Score range '%.1f–%.1f' or '—'; Key labels via METRIC_LABELS or '—'."""
    rows = [
        {
            "Rank": format_rank(score),
            "ZIP": score.postal_code,
            "Score": f"{score.score:.1f}",
            "Score range": _score_range(score),
            "Confidence": score.confidence,
            "Demand source": score.demand_source,
            "Key strength": _label(score.key_strength),
            "Key risk": _label(score.key_risk),
        }
        for score in scores
    ]
    return pd.DataFrame(
        rows,
        columns=[
            "Rank",
            "ZIP",
            "Score",
            "Score range",
            "Confidence",
            "Demand source",
            "Key strength",
            "Key risk",
        ],
    )


def _display_value(key: MetricKey, value: float | None) -> str:
    if value is None:
        return _DASH
    if key == "demand_rate":
        return f"{value * 1000:.2f}"
    if key == "competition":
        return str(round(value))
    if key == "momentum":
        return f"{value * 100:+.0f}%"
    return f"{value:.2f}"


def compare_table(scores: Sequence[AreaScore]) -> pd.DataFrame:
    """Rows = Score, Score range, Rank, Confidence, Demand source, Businesses (niche) and the six
    metrics (display units, '—' for unavailable), Top factors; columns = 'ZIP <code>'."""
    columns: dict[str, list[str]] = {}
    for score in scores:
        values = [
            f"{score.score:.1f}",
            _score_range(score),
            format_rank(score),
            score.confidence,
            score.demand_source,
            str(score.metrics.niche_business_count),
            *(_display_value(key, score.metrics.value(key)) for key in METRIC_KEYS),
            ", ".join(METRIC_LABELS[key] for key in score.top_factors) or _DASH,
        ]
        columns[f"ZIP {score.postal_code}"] = values
    index = [
        "Score",
        "Score range",
        "Rank",
        "Confidence",
        "Demand source",
        "Businesses (niche)",
        *(_METRIC_ROW_LABELS[key] for key in METRIC_KEYS),
        "Top factors",
    ]
    return pd.DataFrame(columns, index=index)


def _csv_text(text: str) -> str:
    return f"'{text}" if text.startswith(("=", "+", "-", "@")) else text


def _general(value: float | None) -> str:
    return "" if value is None else f"{value:.6g}"


def _optional_int(value: int | None) -> str:
    return "" if value is None else str(value)


def scores_to_csv(scores: Sequence[AreaScore]) -> str:
    """Header (exact): rank,rank_low,rank_high,tied_with_above,postal_code,display_name,score,
    score_low,score_high,confidence,demand_source,niche_business_count,
    demand_rate_per_1000_business_days,competition,quality_gap,momentum,stability,
    weather_resilience,rain_effect,heat_effect,key_strength,key_risk,unavailable_metrics,notes.
    LF line endings; score '%.2f', other floats '%.6g'; None -> ''; unavailable_metrics joined
    by ';'; notes = 'Check-ins are an activity proxy, not customers or revenue.'.
    Cells starting with = + - @ get a leading apostrophe (display_name only needs it)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_HEADER)
    for score in scores:
        metrics = score.metrics
        writer.writerow(
            [
                score.rank,
                _optional_int(score.rank_low),
                _optional_int(score.rank_high),
                str(score.tied_with_above),
                _csv_text(score.postal_code),
                _csv_text(score.display_name),
                f"{score.score:.2f}",
                _general(score.score_low),
                _general(score.score_high),
                score.confidence,
                score.demand_source,
                metrics.niche_business_count,
                _general(metrics.demand_rate * 1000),
                _general(metrics.value("competition")),
                _general(metrics.quality_gap),
                _general(metrics.momentum),
                _general(metrics.stability),
                _general(metrics.weather_resilience),
                _general(metrics.rain_effect),
                _general(metrics.heat_effect),
                score.key_strength or "",
                score.key_risk or "",
                ";".join(score.unavailable),
                PROXY_NOTICE,
            ]
        )
    return buffer.getvalue()


def choropleth_color(score: float | None) -> list[int]:
    """RGBA. None -> [190, 190, 190, 120]; else linear blend of (255, 255, 204) at 0 and
    (37, 52, 148) at 100 (score clamped to 0..100), alpha 190."""
    if score is None:
        return list(_NEUTRAL_COLOR)
    share = min(100.0, max(0.0, score)) / 100.0
    channels = [
        round(low + share * (high - low)) for low, high in zip(_LOW_COLOR, _HIGH_COLOR, strict=True)
    ]
    return [*channels, 190]


def _unavailable_reason(key: MetricKey, score: AreaScore) -> str:
    metrics = score.metrics
    if key == "competition":
        return "no centroid"
    if key == "quality_gap":
        return "no competitors"
    if key == "momentum":
        if metrics.covered_month_count < DEFAULT_CONFIG.min_months_momentum:
            return f"fewer than {DEFAULT_CONFIG.min_months_momentum} months"
        return "no check-ins in the last 24 months"
    if key == "stability":
        return f"fewer than {DEFAULT_CONFIG.min_months_stability} months with check-ins"
    if key == "weather_resilience":
        return "no mapped station or too few weather days"
    return "not available"


def _available_note(key: MetricKey, score: AreaScore, radius_km: float) -> str:
    metrics = score.metrics
    if key == "demand_rate":
        notes = {
            "observed": "observed",
            "blended": "blend of observed data and a model estimate",
            "model": "model estimate (no niche businesses observed)",
        }
        note = notes[score.demand_source]
        if score.demand_source != "observed" and metrics.model_fallback:
            note += " (model part = metro mean)"
        return note
    if key == "competition":
        return f"open niche businesses within {radius_km:g} km; fewer is better"
    if key == "quality_gap":
        return "5 minus weighted mean competitor stars"
    if key == "momentum":
        return "last 12 vs prior 12 months"
    if key == "stability":
        return "1 minus variability of monthly check-ins"
    effects = [
        f"{name} {value:+.0%}"
        for name, value in (("rain", metrics.rain_effect), ("heat", metrics.heat_effect))
        if value is not None
    ]
    suffix = f" ({', '.join(effects)})" if effects else ""
    return f"association, not cause{suffix}"


def metric_table(score: AreaScore, radius_km: float) -> pd.DataFrame:
    """Area-detail table, one row per factor in METRIC_KEYS order. Columns (exact): Metric, Value,
    Percentile, Weight used, Contribution, Note. Value uses display units (demand x1000 per
    business-day, competition as integer count, others '%.2f'; momentum '%+.0f%%' of ratio*100);
    unavailable -> Value/Percentile '—', Weight used '0.00', Note = reason (no competitors / fewer
    than 24 months / fewer than 12 months with check-ins / no mapped station or too few weather days
    / no centroid). Percentile '%.2f', Weight used '%.2f', Contribution '%.1f'."""
    rows = []
    for factor in score.factors:
        available = factor.available and factor.raw_value is not None
        rows.append(
            {
                "Metric": factor.label,
                "Value": _display_value(factor.metric, factor.raw_value) if available else _DASH,
                "Percentile": (
                    f"{factor.percentile:.2f}"
                    if available and factor.percentile is not None
                    else _DASH
                ),
                "Weight used": f"{factor.effective_weight if available else 0.0:.2f}",
                "Contribution": f"{factor.contribution if available else 0.0:.1f}",
                "Note": (
                    _available_note(factor.metric, score, radius_km)
                    if available
                    else _unavailable_reason(factor.metric, score)
                ),
            }
        )
    return pd.DataFrame(
        rows,
        columns=["Metric", "Value", "Percentile", "Weight used", "Contribution", "Note"],
    )


def _as_weight(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("Weights must be numbers.")
    return float(value)


def _weight_key(metric: MetricKey) -> str:
    return f"site_rec_w_{metric}"


def weights_from_state(state: Mapping[str, object]) -> ScoreWeights:
    """Build ScoreWeights from the six slider values in session state (§9.2).
    Raises ValueError (from ScoreWeights) when all weights are 0."""
    return ScoreWeights(*(_as_weight(state.get(_weight_key(key))) for key in METRIC_KEYS))


def _map_features(
    scores: Sequence[AreaScore],
    unscored: Sequence[UnscoredArea],
    boundaries: Mapping[str, Mapping[str, Any]],
    highlighted: frozenset[str],
) -> tuple[list[dict[str, Any]], int]:
    """GeoJSON features for every drawable ZIP plus the number of scored ZIPs without a polygon."""
    features: list[dict[str, Any]] = []
    missing = 0
    entries: list[tuple[str, str, str, str, list[int]]] = [
        (
            score.postal_code,
            f"{score.score:.1f}",
            format_rank(score),
            score.confidence,
            choropleth_color(score.score),
        )
        for score in scores
    ]
    entries += [
        (area.postal_code, "Not scored", _DASH, _DASH, choropleth_color(None)) for area in unscored
    ]
    scored_codes = {score.postal_code for score in scores}
    for postal_code, score_text, rank_text, confidence, color in entries:
        feature = boundaries.get(postal_code)
        if feature is None:
            missing += postal_code in scored_codes
            continue
        selected = postal_code in highlighted
        features.append(
            {
                **feature,
                "properties": {
                    "postal_code": postal_code,
                    "score_text": score_text,
                    "rank_text": rank_text,
                    "confidence": confidence,
                    "fill_color": color,
                    "line_color": [28, 48, 61, 255] if selected else [255, 255, 255, 200],
                    "line_width": 3 if selected else 1,
                },
            }
        )
    return features, missing


# ---------------------------------------------------------------------------------------------
# Cached preparation
# ---------------------------------------------------------------------------------------------


@st.cache_data(ttl=3600, max_entries=8, show_spinner="Scoring candidate areas…")
def _load_prepared(
    city: str,
    state: str,
    category: str,
    source_category: str | None,
    start_iso: str,
    end_iso: str,
    radius_km: float,
    import_revision: str,
    cov_start_iso: str,
    cov_end_iso: str,
) -> PreparedScope:
    started = time.perf_counter()
    filters = Filters(
        city,
        state,
        category,
        date.fromisoformat(start_iso),
        date.fromisoformat(end_iso),
        radius_km,
        source_category,
    )
    coverage = DateCoverage(date.fromisoformat(cov_start_iso), date.fromisoformat(cov_end_iso))
    scope = data.load_scope_data(filters, coverage, import_revision)
    prepared = scoring.prepare_scope(scope)
    estimator = prepared.estimator
    _LOGGER.info(
        "scope_prepared scope=%s areas=%d estimator_enabled=%s skill=%s total_ms=%d",
        prepared.key.label(),
        prepared.coverage.areas_scored,
        estimator.enabled,
        "n/a" if estimator.skill is None else f"{estimator.skill:.3f}",
        round((time.perf_counter() - started) * 1000),
    )
    return prepared


@st.cache_data(max_entries=1, show_spinner=False)
def _boundaries() -> dict[str, dict[str, Any]]:
    return zip_boundaries.load_zip_boundaries()


# ---------------------------------------------------------------------------------------------
# State handling
# ---------------------------------------------------------------------------------------------


def _init_state() -> None:
    st.session_state.setdefault("site_rec_preset", "Balanced")
    for key in METRIC_KEYS:
        st.session_state.setdefault(_weight_key(key), PRESETS["Balanced"].get(key))


def _apply_preset() -> None:
    preset = st.session_state.get("site_rec_preset")
    weights = PRESETS.get(preset) if isinstance(preset, str) else None
    if weights is None:
        return
    for key in METRIC_KEYS:
        st.session_state[_weight_key(key)] = weights.get(key)


def _mark_custom() -> None:
    st.session_state["site_rec_preset"] = "Custom"


def _reset_scope_state(key: ScopeKey) -> None:
    previous = st.session_state.get("site_rec_scope")
    if previous is not None and previous != key:
        for name in ("site_rec_compare", "site_rec_area", "site_rec_ai_result"):
            st.session_state.pop(name, None)
    st.session_state["site_rec_scope"] = key


# ---------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------


def render_site_recommendations() -> None:
    """Entry point called by app_pages/site_recommendations.py. Reads
    st.session_state['screen_context'] (ScreenContext). Flow and states: §9. Never raises for
    expected conditions (ScopeUnavailableError, DataUnavailableError, psycopg.Error,
    ExplainerConfigError, ExplainerError); unexpected exceptions propagate to Streamlit."""
    context = cast(ScreenContext, st.session_state["screen_context"])
    try:
        key = ScopeKey.from_filters(context.filters, context.import_revision)
    except ValueError:
        st.warning("The end date must be on or after the start date. Choose a valid date range.")
        return
    _reset_scope_state(key)
    prepared = _prepared_or_notice(context)
    if prepared is None:
        return
    _init_state()
    coverage = prepared.coverage
    st.info(PROXY_NOTICE + " Data comes from Yelp check-ins.")
    st.caption(
        f"Data coverage: {coverage.covered_start} → {coverage.covered_end} "
        f"({coverage.covered_days:,} days, {coverage.covered_month_count} months)"
    )
    _render_history_hint(prepared)
    notices = st.container()
    with st.expander("Scoring preferences", expanded=True):
        top_n, min_confidence = _render_preferences()
    weights = _current_weights(notices)
    result = scoring.rank_areas(prepared, weights)
    with notices:
        _render_warnings(result)
    _render_model_status(result.estimator)
    _render_ranked(result, top_n, min_confidence)
    _render_map(result)
    zip_scores = {score.postal_code: score for score in result.scores}
    _render_area_detail(result, zip_scores)
    chosen = _render_compare(zip_scores)
    _render_ai(result, chosen)
    _render_method(result)


def _render_history_hint(prepared: PreparedScope) -> None:
    metrics = prepared.metrics
    if not metrics:
        return
    for key in ("momentum", "stability"):
        if sum(key in area.unavailable for area in metrics) * 2 >= len(metrics):
            st.caption(
                "Momentum needs at least 24 months and Stability at least 12 months of covered "
                "check-in data. Widen the Date range to include them."
            )
            return


def _prepared_or_notice(context: ScreenContext) -> PreparedScope | None:
    filters = context.filters
    coverage = context.catalog.coverage
    try:
        return _load_prepared(
            filters.city,
            filters.state,
            filters.category,
            filters.source_category,
            filters.start_date.isoformat(),
            filters.end_date.isoformat(),
            filters.radius_km,
            context.import_revision,
            coverage.start_date.isoformat(),
            coverage.end_date.isoformat(),
        )
    except repository.DataUnavailableError:
        st.info("No imported data for this selection.")
    except psycopg.Error:
        render_setup("connection")
    except ScopeUnavailableError as error:
        if error.reason == "no_coverage":
            st.warning(
                "The selected date range has no imported check-in coverage. "
                f"Choose dates between {coverage.start_date} and {coverage.end_date}."
            )
        else:
            st.info("No area has businesses in this category, so nothing can be scored.")
    return None


def _render_preferences() -> tuple[int, str]:
    st.selectbox(
        "Weight preset",
        _PRESET_OPTIONS,
        key="site_rec_preset",
        on_change=_apply_preset,
        help="Choosing a preset overwrites the six sliders; moving a slider switches to Custom.",
    )
    for left, right in ((0, 1), (2, 3), (4, 5)):
        columns = st.columns(2)
        for column, metric in zip(columns, (METRIC_KEYS[left], METRIC_KEYS[right]), strict=True):
            with column:
                st.slider(
                    f"{METRIC_LABELS[metric]} weight",
                    min_value=0.0,
                    max_value=1.0,
                    step=0.05,
                    format="%.2f",
                    key=_weight_key(metric),
                    on_change=_mark_custom,
                )
    columns = st.columns(2)
    with columns[0]:
        top_n = st.slider("Show top areas", 5, 50, 10, key="site_rec_top_n")
    with columns[1]:
        min_confidence = st.selectbox(
            "Minimum confidence", _CONFIDENCE_OPTIONS, key="site_rec_min_conf"
        )
    return top_n, min_confidence or _CONFIDENCE_OPTIONS[0]


def _current_weights(notices: Any) -> ScoreWeights:
    try:
        weights = weights_from_state(
            {_weight_key(key): st.session_state.get(_weight_key(key)) for key in METRIC_KEYS}
        )
    except ValueError:
        with notices:
            st.warning("Set at least one weight above 0.")
        previous = st.session_state.get("site_rec_last_weights")
        return previous if isinstance(previous, ScoreWeights) else PRESETS["Balanced"]
    st.session_state["site_rec_last_weights"] = weights
    return weights


def _render_warnings(result: RecommendationResult) -> None:
    for warning in result.warnings:
        if warning.code == "few_areas":
            st.warning(
                f"Only {result.coverage.areas_scored} areas could be scored (fewer than 5). "
                "Rankings are fragile."
            )
        elif warning.code in ("short_coverage", "partial_coverage"):
            st.warning(warning.message)
        elif warning.code in ("weather_sparse", "equal_weight_fallback"):
            st.info(warning.message)


def _render_model_status(report: EstimatorReport) -> None:
    if report.enabled and report.skill is not None:
        st.caption(
            f"Demand model: ON · LOO-CV skill {report.skill:+.2f} "
            f"(MAE {report.mae_model or 0.0:.2f} vs {report.mae_baseline or 0.0:.2f} baseline)"
        )
    elif not report.enabled:
        reason = (
            "it did not beat the metro average"
            if report.reason == "no_skill"
            else (
                f"fewer than {DEFAULT_CONFIG.min_estimator_train_areas} areas with at least "
                f"{DEFAULT_CONFIG.min_observed_businesses} businesses"
            )
        )
        st.info(
            f"Demand model: OFF ({reason}). "
            "The metro average is used where niche businesses are few."
        )


def _render_ranked(result: RecommendationResult, top_n: int, min_confidence: str) -> None:
    confidence = cast(Confidence, min_confidence.lower())
    filtered = scoring.filter_scores(result.scores, min_confidence=confidence, top_n=top_n)
    heading, download = st.columns([3, 1], vertical_alignment="bottom")
    with heading:
        st.subheader("Ranked areas")
    with download:
        st.download_button(
            "Download CSV",
            data=scores_to_csv(filtered),
            file_name="site_recommendations.csv",
            mime="text/csv",
            on_click="ignore",
            help="Exports the rows shown in the table.",
        )
    if filtered:
        st.dataframe(ranked_table(filtered), hide_index=True, width="stretch")
        st.caption(
            f"Showing {len(filtered)} of {len(result.scores)} scored areas. "
            "A rank range means the area is statistically close to the one above it."
        )
    else:
        st.info(
            "No scored area meets the minimum confidence. Lower Minimum confidence to see areas."
        )
    if result.unscored:
        listed = ", ".join(
            f"{area.postal_code} (no businesses in this category)" for area in result.unscored
        )
        st.caption(f"Not scored: {listed}")


def _render_map(result: RecommendationResult) -> None:
    st.subheader("Score by ZIP")
    try:
        boundaries = _boundaries()
    except (zip_boundaries.BoundaryDataError, OSError):
        st.caption("ZIP boundaries are unavailable, so the map is skipped. Use the table above.")
        return
    selected = st.session_state.get("site_rec_area")
    compared = st.session_state.get("site_rec_compare") or []
    highlighted = frozenset(code for code in (selected, *compared) if isinstance(code, str))
    features, missing = _map_features(result.scores, result.unscored, boundaries, highlighted)
    points = [
        point
        for feature in features
        for point in _geometry_points(feature["geometry"]["coordinates"])
    ]
    if not points:
        st.caption("No ZIP boundaries are available to map.")
        return
    layer = pdk.Layer(
        "GeoJsonLayer",
        id="site-rec-zips",
        data={"type": "FeatureCollection", "features": features},
        filled=True,
        stroked=True,
        get_fill_color="properties.fill_color",
        get_line_color="properties.line_color",
        get_line_width="properties.line_width",
        line_width_units="'pixels'",
        pickable=True,
        auto_highlight=True,
    )
    deck = pdk.Deck(
        layers=[layer],
        map_style=None,
        initial_view_state=_fit_view(points),
        tooltip={"html": _MAP_TOOLTIP_HTML, "style": _MAP_TOOLTIP_STYLE},
    )
    st.html(_MAP_CSS)
    with st.container(key="site_rec_map_view"):
        st.pydeck_chart(
            deck,
            height=_MAP_HEIGHT,
            key="site_rec_map",
            alt="ZIP areas filled by recommendation score from 0 (light) to 100 (dark blue)",
        )
    st.caption(
        "Color scale: score 0 (light yellow) → 100 (dark blue) · Gray: not scored. "
        "Hover a ZIP to read its score, rank and confidence. Dark outline: selected or "
        "compared ZIPs."
    )
    if missing:
        st.caption(f"{missing} scored ZIP(s) have no boundary polygon and are not drawn.")


def _render_area_detail(result: RecommendationResult, zip_scores: Mapping[str, AreaScore]) -> None:
    st.subheader("Area detail")
    postal_code = st.selectbox("Area (ZIP)", list(zip_scores), key="site_rec_area")
    if postal_code is None:
        return
    score = zip_scores[postal_code]
    metrics = score.metrics
    raw = metrics.raw_demand_rate
    st.caption(
        f"Score {score.score:.1f} · rank {format_rank(score)} of {len(result.scores)} · "
        f"confidence {score.confidence} · demand source {score.demand_source} · "
        f"{metrics.niche_business_count} niche businesses · observed demand "
        f"{_DASH if raw is None else f'{raw * 1000:.2f}'} vs model estimate "
        f"{metrics.model_rate * 1000:.2f} check-ins per 1,000 business-days"
    )
    if metrics.weather_station_id is None:
        st.caption("Weather coverage: no mapped station for this ZIP.")
    else:
        st.caption(
            f"Weather coverage: {metrics.weather_observed_days} observed days "
            f"({metrics.wet_days} wet, {metrics.dry_days} dry, {metrics.hot_days} hot)."
        )
    if score.confidence == "low":
        st.warning(
            "Confidence is low for this area: few or no niche businesses, or little check-in "
            "activity. Treat its rank as indicative only."
        )
    elif score.demand_source != "observed":
        st.info("Demand for this area is partly estimated by a model, not fully observed.")
    available = [factor for factor in score.factors if factor.available]
    if available:
        frame = pd.DataFrame(
            {
                "Factor": [factor.label for factor in available],
                "Points vs median area": [round(factor.delta_vs_median, 1) for factor in available],
                "Contribution to score": [round(factor.contribution, 1) for factor in available],
            }
        )
        st.bar_chart(
            frame,
            x="Factor",
            y="Points vs median area",
            horizontal=True,
            sort=False,
            height=60 + 40 * len(available),
        )
        signed = frame.assign(
            **{"Points vs median area": frame["Points vs median area"].map(lambda v: f"{v:+.1f}")}
        )
        st.dataframe(signed, hide_index=True, width="stretch")
    st.dataframe(
        metric_table(score, result.key.effective_radius_km()),
        hide_index=True,
        width="stretch",
    )
    st.markdown("**Why this rank**")
    why = explain.why_this_rank(score, len(result.scores))
    st.markdown(why.replace("\n", "  \n"))


def _render_compare(zip_scores: Mapping[str, AreaScore]) -> tuple[AreaScore, ...]:
    st.subheader("Compare areas")
    picked = list(st.multiselect("Compare areas (2–5)", list(zip_scores), key="site_rec_compare"))
    maximum = DEFAULT_CONFIG.max_compare_areas
    if len(picked) > maximum:
        st.warning(f"Select at most {maximum} areas.")
        picked = picked[:maximum]
    chosen = tuple(zip_scores[code] for code in picked)
    if len(chosen) < DEFAULT_CONFIG.min_compare_areas:
        st.info("Select 2 to 5 areas to compare.")
    else:
        st.dataframe(compare_table(chosen), width="stretch")
    return chosen


def _signature(
    kind: ExplainKind, facts: JsonObject, language: Language, question: str | None, use_ai: bool
) -> str:
    text = json.dumps(
        [kind, facts, language, question if kind == "question" else None, use_ai],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _facts_for(
    result: RecommendationResult,
    kind: ExplainKind,
    chosen: Sequence[AreaScore],
    question: str | None,
) -> JsonObject | None:
    ids = tuple(score.area_id for score in chosen)
    try:
        if kind == "top_picks":
            return explain.build_facts(result, kind=kind, top_n=min(_TOP_PICKS, len(result.scores)))
        if kind == "compare":
            if not DEFAULT_CONFIG.min_compare_areas <= len(ids) <= DEFAULT_CONFIG.max_compare_areas:
                return None
            return explain.build_facts(result, kind=kind, selected_area_ids=ids)
        if question is None:
            return None
        return explain.build_facts(result, kind=kind, selected_area_ids=ids, question=question)
    except ValueError:
        return None


def _ai_config() -> tuple[explain.ExplainerConfig | None, str | None]:
    try:
        return explain.ExplainerConfig.from_env(), None
    except explain.ExplainerConfigError as error:
        return None, str(error)


def _render_ai(result: RecommendationResult, chosen: Sequence[AreaScore]) -> None:
    st.subheader("AI advisor")
    config, config_error = _ai_config()
    if config_error is not None:
        st.error(config_error)
    elif config is None:
        st.caption("AI provider not configured. Buttons use the offline summary.")
    else:
        st.caption(
            f"AI provider: {config.model} @ {config.host()} · API key "
            f"{'set' if config.api_key else 'not set'} (never shown). Nothing is sent unless "
            "you tick the box below and click a button."
        )
    st.checkbox(
        "Use AI provider for this summary",
        key="site_rec_use_ai",
        disabled=config is None,
        help=(
            "Sends only the aggregated data listed below to the configured provider. "
            "Set SITESENSE_AI_BASE_URL and SITESENSE_AI_MODEL to enable it."
        ),
    )
    use_ai = config is not None and bool(st.session_state.get("site_rec_use_ai", False))
    language_label = st.radio(
        "Answer language", list(_LANGUAGES), key="site_rec_ai_lang", horizontal=True
    )
    language = _LANGUAGES[language_label or "English"]

    clicked: ExplainKind | None = None
    first, second = st.columns(2)
    with first:
        if st.button("Explain top picks"):
            clicked = "top_picks"
    with second:
        if st.button("Compare selected areas"):
            clicked = "compare"
    typed, ask = st.columns([4, 1], vertical_alignment="bottom")
    with typed:
        st.text_input(
            "Question (optional)",
            key="site_rec_ai_question",
            max_chars=_MAX_AI_QUESTION,
            help="The offline summary cannot answer free-text questions.",
        )
    with ask:
        if st.button("Ask a question"):
            clicked = "question"
    question = explain.sanitize_question(
        str(st.session_state.get("site_rec_ai_question", "") or "")
    )

    previews: list[tuple[str, JsonObject]] = []
    for kind, title in (
        ("top_picks", "Explain top picks"),
        ("compare", "Compare selected areas"),
        ("question", "Ask a question"),
    ):
        facts = _facts_for(result, cast(ExplainKind, kind), chosen, question)
        if facts is not None:
            previews.append((title, facts))
    with st.expander("Data that will be sent to the AI provider"):
        if use_ai:
            st.caption("Only these aggregated numbers are sent when you click a button.")
        else:
            st.caption(
                "Nothing is sent: the offline summary is built from this same data. "
                "It would be sent only if you tick Use AI provider for this summary."
            )
        for title, facts in previews:
            st.caption(title)
            st.code(
                json.dumps(facts, indent=2, sort_keys=True, ensure_ascii=False), language="json"
            )

    if clicked is not None:
        _run_ai(result, clicked, chosen, question, language, config, use_ai)
    _show_ai_result(result, chosen, question, language, use_ai)


def _run_ai(
    result: RecommendationResult,
    kind: ExplainKind,
    chosen: Sequence[AreaScore],
    question: str | None,
    language: Language,
    config: explain.ExplainerConfig | None,
    use_ai: bool,
) -> None:
    if kind == "compare" and len(chosen) < DEFAULT_CONFIG.min_compare_areas:
        st.info("Select 2 to 5 areas to compare.")
        return
    if kind == "question" and question is None:
        st.info("Type a question first.")
        return
    facts = _facts_for(result, kind, chosen, question)
    if facts is None:
        st.info("This summary is not available for the current selection.")
        return
    primary = explain.build_explainer(config if use_ai else None)
    timeout = config.timeout_s if config is not None else 30.0
    request = explain.ExplainRequest(
        kind, facts, language, question if kind == "question" else None
    )
    narrative = explain.explain_with_fallback(primary, request, timeout_s=timeout)
    st.session_state["site_rec_ai_result"] = {
        "kind": kind,
        "signature": _signature(kind, facts, language, question, use_ai),
        "text": narrative.text,
        "source": narrative.source,
        "model": narrative.model,
        "notice": narrative.notice,
    }


def _show_ai_result(
    result: RecommendationResult,
    chosen: Sequence[AreaScore],
    question: str | None,
    language: Language,
    use_ai: bool,
) -> None:
    stored = st.session_state.get("site_rec_ai_result")
    if not isinstance(stored, dict):
        return
    kind = cast(ExplainKind, stored.get("kind"))
    facts = _facts_for(result, kind, chosen, question)
    if facts is None or stored.get("signature") != _signature(
        kind, facts, language, question, use_ai
    ):
        st.session_state.pop("site_rec_ai_result", None)
        return
    if notice := stored.get("notice"):
        st.warning(str(notice))
    st.markdown(str(stored.get("text", "")))
    if stored.get("source") == "llm":
        st.caption(f"AI-generated by {stored.get('model')} — verify against the table above.")
    else:
        st.caption("Offline summary built from the table above.")


def _render_method(result: RecommendationResult) -> None:
    with st.expander("Method and limits"):
        st.markdown(
            "\n".join(
                [
                    f"- **Check-ins are an activity proxy.** {PROXY_NOTICE} Data comes from "
                    "Yelp check-ins; ratings and review counts describe the business snapshot, "
                    "and review text is not loaded.",
                    "- **Score (0–100)** = 100 × Σ weight × percentile across the six factors "
                    "(demand, competition, quality gap, momentum, stability, weather "
                    "resilience). Competition is inverted: fewer open competitors rank higher. "
                    "A factor that cannot be computed is dropped and the remaining weights are "
                    "re-normalised for that ZIP; it is listed as not available.",
                    "- **Demand** is check-ins per business-day, shrunk toward the metro mean "
                    "(k = 3). With fewer than 3 niche businesses it is blended with a ridge "
                    "regression model estimate (leave-one-out cross-validated against the metro "
                    "average); with none it is a model estimate. Model estimates are labelled "
                    "blended or model and never count as observed.",
                    "- **Weather resilience** compares recorded days with rain or heat against "
                    "other days at the nearest station. These are correlations, not causal "
                    "effects.",
                    f"- **Uncertainty:** score ranges and rank ranges come from "
                    f"{result.n_bootstrap} seeded bootstrap resamples (5th–95th percentile). "
                    "Overlapping ranges mean areas cannot be separated with confidence.",
                    "- **Limits:** results describe past recorded activity in the selected date "
                    "range and cannot predict future outcomes or income. Check-ins undercount "
                    "real visits, and ZIPs with few businesses have low confidence.",
                    "- **Optional AI summary:** set SITESENSE_AI_BASE_URL and SITESENSE_AI_MODEL "
                    "(and SITESENSE_AI_API_KEY if needed) in the environment. Only aggregated "
                    "numbers are sent, and only after you tick the box and click a button; "
                    "the AI never changes scores or ranks.",
                ]
            )
        )
