"""Explain/AI tests (TC-130..TC-159, TC-197). Real loopback HTTP server, no external network."""

import json
import logging
import re
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

import pytest

from sitesense.site_recommendation import explain
from sitesense.site_recommendation.explain import (
    GROUNDING_NOTICES,
    MAX_OUTPUT_CHARS,
    SYSTEM_PROMPT,
    ExplainerConfig,
    ExplainerConfigError,
    ExplainerError,
    ExplainRequest,
    OpenAICompatibleExplainer,
    TemplateExplainer,
    build_explainer,
    build_facts,
    explain_with_fallback,
    sanitize_question,
    unmatched_figures,
    urllib_transport,
    why_this_rank,
)
from sitesense.site_recommendation.models import (
    METRIC_KEYS,
    METRIC_LABELS,
    AreaMetrics,
    AreaScore,
    CoverageInfo,
    EstimatorReport,
    ExplainKind,
    FactorContribution,
    JsonObject,
    JsonValue,
    Language,
    MetricKey,
    RecommendationResult,
    ScopeKey,
    ScoreWeights,
    UnscoredArea,
)

FX_KEY: Final = "sk-test-123"
FX_ENV_OK: Final[Mapping[str, str]] = {
    "SITESENSE_AI_BASE_URL": "https://api.example.test/v1",
    "SITESENSE_AI_MODEL": "m-test",
    "SITESENSE_AI_API_KEY": FX_KEY,
}
LLM_OK_TEXT: Final = "ZIP 33606 ranks first on demand."
LLM_OK_BODY: Final = json.dumps({"choices": [{"message": {"content": LLM_OK_TEXT}}]}).encode()
LEAK_NAME: Final = "LEAK-Pizza-Palace"
LEAK_ID: Final = "LEAK-biz-001"
LEAK_COORDS: Final = ("27.9506", "-82.4572")
ZIPS: Final = (
    "33606", "33602", "33609", "33611", "33603", "33605",
    "33610", "33601", "33604", "33607", "33608", "33612",
)  # fmt: skip
FORBIDDEN_KEYS: Final = {"name", "business_id", "address", "latitude", "longitude", "display_name"}


# ---------------------------------------------------------------------------
# Result builders (FX-SCOPE-12 equivalent, built directly from the frozen models)
# ---------------------------------------------------------------------------


def _metrics(area_id: int, postal: str, *, fallback: bool = False) -> AreaMetrics:
    return AreaMetrics(
        area_id=area_id,
        postal_code=postal,
        display_name=f"{LEAK_NAME} {postal} ({LEAK_ID})",
        niche_business_count=6,
        niche_open_count=5,
        niche_checkins=900,
        covered_days=2210,
        covered_month_count=72,
        raw_demand_rate=0.0123,
        model_rate=0.01,
        demand_rate=0.0123,
        demand_source="observed",
        model_fallback=fallback,
        competition=4,
        quality_gap=1.25,
        mean_competitor_stars=3.75,
        momentum=0.12,
        stability=0.8,
        weather_resilience=None,
        rain_effect=None,
        heat_effect=None,
        weather_station_id="STATION-1",
        weather_observed_days=0,
        wet_days=0,
        dry_days=0,
        hot_days=0,
        confidence="high",
        unavailable=("weather_resilience",),
    )


def _factors(unavailable: tuple[MetricKey, ...]) -> tuple[FactorContribution, ...]:
    values: dict[MetricKey, float] = {
        "demand_rate": 0.0123,
        "competition": 4.0,
        "quality_gap": 1.25,
        "momentum": 0.12,
        "stability": 0.8,
        "weather_resilience": 0.7,
    }
    out: list[FactorContribution] = []
    for key in METRIC_KEYS:
        avail = key not in unavailable
        out.append(
            FactorContribution(
                metric=key,
                label=METRIC_LABELS[key],
                raw_value=values[key] if avail else None,
                percentile=0.9 if avail else None,
                base_weight=0.2,
                effective_weight=0.25 if avail else 0.0,
                contribution=31.54 if avail else 0.0,
                delta_vs_median=14.04 if key == "demand_rate" else (-3.04 if avail else 0.0),
                available=avail,
            )
        )
    return tuple(out)


def _score(
    index: int,
    postal: str,
    *,
    score: float | None = None,
    tied: bool = False,
    rank_range: tuple[int, int] | None = None,
    fallback: bool = False,
    unavailable: tuple[MetricKey, ...] = ("weather_resilience",),
    intervals: bool = True,
) -> AreaScore:
    value = 72.4 - 5.1 * index if score is None else score
    low, high = rank_range if rank_range else (index + 1, index + 3)
    return AreaScore(
        area_id=int(postal),
        postal_code=postal,
        display_name=f"{LEAK_NAME} {postal}",
        rank=index + 1,
        score=value,
        score_low=value - 6.4 if intervals else None,
        score_high=value + 6.4 if intervals else None,
        rank_low=low if intervals else None,
        rank_high=high if intervals else None,
        tied_with_above=tied,
        factors=_factors(unavailable),
        top_factors=("demand_rate", "competition", "momentum"),
        key_strength="demand_rate",
        key_risk="quality_gap",
        confidence="high" if index % 2 == 0 else "low",
        demand_source="observed",
        unavailable=unavailable,
        equal_weight_fallback=False,
        metrics=_metrics(int(postal), postal, fallback=fallback),
    )


def _result() -> RecommendationResult:
    scores = tuple(_score(i, postal) for i, postal in enumerate(ZIPS))
    key = ScopeKey(
        "Tampa", "FL", "Restaurants", "Pizza", date(2016, 1, 1), date(2022, 1, 19), 1.0, "rev-1"
    )
    return RecommendationResult(
        key=key,
        weights=ScoreWeights(),
        scores=scores,
        unscored=(UnscoredArea(33620, "33620", f"{LEAK_NAME} 33620", "no_parent_businesses"),),
        estimator=EstimatorReport(
            enabled=True,
            reason="ok",
            n_train=9,
            n_features=5,
            feature_names=("a",),
            mae_model=0.2,
            mae_baseline=0.3,
            skill=0.31,
            residual_sd=0.1,
            alpha=1.0,
        ),
        coverage=CoverageInfo(
            requested_start=date(2016, 1, 1),
            requested_end=date(2022, 1, 19),
            covered_start=date(2016, 1, 1),
            covered_end=date(2022, 1, 19),
            covered_days=2210,
            covered_month_count=72,
            areas_scored=12,
            areas_unscored=1,
            areas_with_weather=3,
        ),
        warnings=(),
        n_bootstrap=200,
        seed=1,
    )


@pytest.fixture(scope="module")
def result() -> RecommendationResult:
    return _result()


def _request(
    result: RecommendationResult,
    kind: ExplainKind = "top_picks",
    language: Language = "en",
    question: str | None = None,
    selected: tuple[int, ...] = (),
) -> ExplainRequest:
    facts = build_facts(result, kind=kind, selected_area_ids=selected, question=question)
    return ExplainRequest(kind, facts, language, question=sanitize_question(question))


# ---------------------------------------------------------------------------
# Loopback HTTP server
# ---------------------------------------------------------------------------


@dataclass
class Reply:
    status: int = 200
    body: bytes = LLM_OK_BODY
    delay: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Seen:
    path: str
    headers: dict[str, str]
    body: bytes


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    reply: Reply
    seen: list[Seen]
    release: threading.Event


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.seen.append(
            Seen(self.path, {k.lower(): v for k, v in self.headers.items()}, body)
        )
        reply = self.server.reply
        if reply.delay:
            self.server.release.wait(reply.delay)
        try:
            self.send_response(reply.status)
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)
        except OSError:
            pass  # client gave up (timeout test)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@dataclass
class LocalServer:
    server: _Server

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    @property
    def seen(self) -> list[Seen]:
        return self.server.seen

    def respond(self, reply: Reply) -> None:
        self.server.reply = reply

    def config(self, *, timeout_s: float = 5.0, key: str = FX_KEY) -> ExplainerConfig:
        return ExplainerConfig(self.base_url, "m-test", key, timeout_s)


@pytest.fixture
def local_server() -> Iterator[LocalServer]:
    server = _Server(("127.0.0.1", 0), _Handler)
    server.reply = Reply()
    server.seen = []
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield LocalServer(server)
    finally:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _walk_keys(value: JsonValue) -> Iterator[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _fake_transport(
    status: int = 200,
    body: bytes = LLM_OK_BODY,
    error: Exception | None = None,
) -> tuple[explain.HttpTransport, list[tuple[str, Mapping[str, str], bytes, float]]]:
    calls: list[tuple[str, Mapping[str, str], bytes, float]] = []

    def transport(
        url: str, headers: Mapping[str, str], payload: bytes, timeout: float
    ) -> tuple[int, bytes]:
        calls.append((url, dict(headers), payload, timeout))
        if error is not None:
            raise error
        return status, body

    return transport, calls


def _llm(
    status: int = 200, body: bytes = LLM_OK_BODY, error: Exception | None = None
) -> OpenAICompatibleExplainer:
    cfg = ExplainerConfig.from_env(FX_ENV_OK)
    assert cfg is not None
    transport, _ = _fake_transport(status, body, error)
    return OpenAICompatibleExplainer(cfg, transport)


def _content(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode()


# ---------------------------------------------------------------------------
# Config (TC-130..135)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"SITESENSE_AI_BASE_URL": "https://x.test/v1"},
        {"SITESENSE_AI_MODEL": "m"},
        {"SITESENSE_AI_BASE_URL": "  ", "SITESENSE_AI_MODEL": "m"},
        {"SITESENSE_AI_BASE_URL": "https://x.test/v1", "SITESENSE_AI_MODEL": "   "},
    ],
)
def test_tc130_from_env_missing_returns_none(env: dict[str, str]) -> None:
    assert ExplainerConfig.from_env(env) is None


def test_tc130_from_env_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SITESENSE_AI_BASE_URL", "SITESENSE_AI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    assert ExplainerConfig.from_env() is None
    monkeypatch.setenv("SITESENSE_AI_BASE_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("SITESENSE_AI_MODEL", "llama")
    cfg = ExplainerConfig.from_env()
    assert cfg is not None
    assert cfg.model == "llama"


def test_tc131_from_env_valid_and_trailing_slash() -> None:
    cfg = ExplainerConfig.from_env(FX_ENV_OK)
    assert cfg is not None
    assert (cfg.base_url, cfg.model, cfg.timeout_s) == (
        "https://api.example.test/v1",
        "m-test",
        30.0,
    )
    assert cfg.api_key == FX_KEY
    assert cfg.host() == "api.example.test"
    slashed = ExplainerConfig.from_env({**FX_ENV_OK, "SITESENSE_AI_BASE_URL": "https://a.test/v1/"})
    assert slashed is not None
    assert slashed.base_url == "https://a.test/v1"


def test_tc132_timeout_parsing() -> None:
    cfg = ExplainerConfig.from_env({**FX_ENV_OK, "SITESENSE_AI_TIMEOUT": "5"})
    assert cfg is not None
    assert cfg.timeout_s == 5.0
    for bad in ("abc", "0", "301", "nan", "-1"):
        with pytest.raises(ExplainerConfigError):
            ExplainerConfig.from_env({**FX_ENV_OK, "SITESENSE_AI_TIMEOUT": bad})


@pytest.mark.parametrize(
    ("url", "ok"),
    [
        ("http://example.com/v1", False),
        ("ftp://x", False),
        ("http://localhost:11434/v1", True),
        ("http://127.0.0.1:8080/v1", True),
        ("http://[::1]:11434/v1", True),
        ("https://api.example.test/v1", True),
        ("https://user:pw@api.example.test/v1", False),
    ],
)
def test_tc133_scheme_rules(url: str, ok: bool) -> None:
    env = {**FX_ENV_OK, "SITESENSE_AI_BASE_URL": url}
    if ok:
        assert ExplainerConfig.from_env(env) is not None
    else:
        with pytest.raises(ExplainerConfigError) as info:
            ExplainerConfig.from_env(env)
        assert FX_KEY not in str(info.value)
        assert "pw" not in str(info.value)


def test_tc134_key_optional_means_no_authorization_header() -> None:
    env = {k: v for k, v in FX_ENV_OK.items() if k != "SITESENSE_AI_API_KEY"}
    cfg = ExplainerConfig.from_env(env)
    assert cfg is not None
    assert cfg.api_key == ""
    transport, calls = _fake_transport()
    OpenAICompatibleExplainer(cfg, transport).explain(ExplainRequest("top_picks", {"areas": []}))
    assert "Authorization" not in calls[0][1]


def test_tc135_repr_masks_key() -> None:
    cfg = ExplainerConfig.from_env(FX_ENV_OK)
    assert cfg is not None
    for text in (repr(cfg), str(cfg), f"{cfg}", f"{cfg!r}", repr([cfg])):
        assert FX_KEY not in text
    assert "m-test" in repr(cfg)


# ---------------------------------------------------------------------------
# Facts (TC-136..140)
# ---------------------------------------------------------------------------


def test_tc136_facts_top_picks(result: RecommendationResult) -> None:
    facts = build_facts(result, kind="top_picks", top_n=3)
    areas = facts["areas"]
    assert isinstance(areas, list)
    assert [a["postal_code"] for a in areas if isinstance(a, dict)] == list(ZIPS[:3])
    assert facts["schema_version"] == 1
    notes = facts["data_notes"]
    assert isinstance(notes, dict)
    assert notes["checkins_are_activity_proxy"] is True
    assert (notes["areas_scored"], notes["areas_not_scored"]) == (12, 1)
    assert notes["estimator"] == {"enabled": True, "skill": 0.31, "n_train": 9}
    first = areas[0]
    assert isinstance(first, dict)
    assert first["score"] == 72.4
    assert first["score_range"] == [66.0, 78.8]
    assert first["rank_range"] == {"best": 1, "worst": 3}
    assert facts["scope"] == {
        "metro": "Tampa, FL",
        "business_category": "Restaurants",
        "source_category": "Pizza",
        "date_range": {"start": "2016-01-01", "end": "2022-01-19"},
        "covered_days": 2210,
        "radius_km": 1.0,
    }
    weights = facts["weights"]
    assert isinstance(weights, dict)
    assert weights["demand_rate"] == 0.35
    factors = first["factors"]
    assert isinstance(factors, list)
    assert len(factors) == 6
    demand = factors[0]
    assert isinstance(demand, dict)
    assert demand["contribution"] == 31.5
    assert first["unavailable_metrics"] == ["weather_resilience"]
    assert "low_confidence" not in first["flags"]  # type: ignore[operator]
    with pytest.raises(ValueError):
        build_facts(result, kind="top_picks", top_n=0)
    with pytest.raises(ValueError):
        build_facts(result, kind="top_picks", top_n=6)


def test_tc136_flags(result: RecommendationResult) -> None:
    facts = build_facts(result, kind="top_picks", top_n=2)
    areas = facts["areas"]
    assert isinstance(areas, list)
    second = areas[1]
    assert isinstance(second, dict)
    assert second["flags"] == ["low_confidence"]


def test_tc137_payload_has_no_leaks(result: RecommendationResult) -> None:
    for kind, selected in (("top_picks", ()), ("compare", (33606, 33602)), ("question", (33612,))):
        facts = build_facts(result, kind=kind, selected_area_ids=selected, question="hello?")  # type: ignore[arg-type]
        text = json.dumps(facts)
        for needle in ("LEAK-", LEAK_NAME, LEAK_ID, *LEAK_COORDS, "display_name"):
            assert needle not in text
        assert not FORBIDDEN_KEYS & set(_walk_keys(facts))


def test_tc138_facts_compare(result: RecommendationResult) -> None:
    facts = build_facts(result, kind="compare", selected_area_ids=(33612, 33606, 33603))
    areas = facts["areas"]
    assert isinstance(areas, list)
    assert [a["postal_code"] for a in areas if isinstance(a, dict)] == ["33606", "33603", "33612"]
    with pytest.raises(ValueError):
        build_facts(result, kind="compare", selected_area_ids=(33606,))
    with pytest.raises(ValueError):
        build_facts(result, kind="compare", selected_area_ids=tuple(int(z) for z in ZIPS[:6]))
    with pytest.raises(ValueError):
        build_facts(result, kind="compare", selected_area_ids=(33606, 33620))
    with pytest.raises(ValueError):
        build_facts(result, kind="compare", selected_area_ids=(33606, 33606))


def test_tc139_facts_question_sanitised_and_capped(result: RecommendationResult) -> None:
    raw = "Where?\x00\x07\n\t  " + "x" * 500
    facts = build_facts(result, kind="question", selected_area_ids=(33612,), question=raw)
    question = facts["question"]
    assert isinstance(question, str)
    assert len(question) <= 300
    assert not any(ord(c) < 32 for c in question)
    assert question.startswith("Where? xxx")
    areas = facts["areas"]
    assert isinstance(areas, list)
    assert len(areas) == 5
    assert "33612" in [a["postal_code"] for a in areas if isinstance(a, dict)]
    assert sanitize_question("  \x00 ") is None
    assert sanitize_question(None) is None


def test_tc140_payload_size(result: RecommendationResult) -> None:
    facts = build_facts(result, kind="top_picks", top_n=5)
    assert len(json.dumps(facts).encode()) <= 12_000


# ---------------------------------------------------------------------------
# Template explainer and why_this_rank (TC-141..145)
# ---------------------------------------------------------------------------


def test_tc141_template_deterministic(result: RecommendationResult) -> None:
    req = _request(result)
    one = TemplateExplainer().explain(req)
    two = TemplateExplainer().explain(req)
    assert one == two
    assert one.source == "template"
    assert one.notice is None
    assert one.model is None
    assert len(one.text) <= MAX_OUTPUT_CHARS


def test_tc142_template_numbers_match_facts(result: RecommendationResult) -> None:
    req = _request(result)
    text = TemplateExplainer().explain(req).text
    assert "activity proxy" in text
    assert "33606" in text
    assert "72.4" in text
    facts_text = json.dumps(req.facts)
    for number in re.findall(r"[+-]?\d+\.\d(?!\d)", text):
        assert number.lstrip("+-") in facts_text, number
    for kind in ("compare", "question"):
        other = _request(result, kind, selected=(33606, 33602), question="Why?")
        narrative = TemplateExplainer().explain(other)
        assert "activity proxy" in narrative.text
        for number in re.findall(r"[+-]?\d+\.\d(?!\d)", narrative.text):
            assert number.lstrip("+-") in json.dumps(other.facts), number
    question = TemplateExplainer().explain(_request(result, "question", question="Why?")).text
    assert "cannot answer free-text questions" in question
    assert "Why?" not in question


def test_tc143_template_vietnamese(result: RecommendationResult) -> None:
    text = TemplateExplainer().explain(_request(result, language="vi")).text
    assert "proxy" in text
    assert "check-in" in text.lower()
    assert "33606" in text
    assert "xếp hạng" in text
    assert text.count("doanh thu") == 1
    assert "không phải lượng khách hay doanh thu" in text
    compare = TemplateExplainer().explain(
        _request(result, "compare", "vi", selected=(33606, 33602))
    )
    assert compare.language == "vi"
    assert "xếp cao hơn" in compare.text


def test_template_output_is_capped() -> None:
    facts: JsonObject = {
        "data_notes": {"areas_scored": 1},
        "areas": [{"postal_code": "9" * 5000, "rank": 1}],
    }
    text = TemplateExplainer().explain(ExplainRequest("top_picks", facts)).text
    assert len(text) <= MAX_OUTPUT_CHARS
    assert text.endswith("…")


def test_template_tolerates_malformed_facts() -> None:
    text = TemplateExplainer().explain(ExplainRequest("compare", {"areas": "nope"})).text
    assert "No areas" in text


def test_tc144_why_this_rank() -> None:
    text = why_this_rank(_score(0, "33606"), 14, radius_km=1.0)
    assert "ZIP 33606 ranks #1 of 14 (score 72.4, range 66.0–78.8)." in text
    assert "Strongest factor: Demand (12.30 check-ins per 1,000 business-days; +14.0" in text
    assert "Biggest risk: Quality gap (1.25; -3.0)." in text
    assert "Confidence: high; demand is observed." in text
    assert "Check-ins are an activity proxy, not customers or revenue." in text
    assert "Rank is statistically close" not in text
    competition = why_this_rank(
        replace(_score(0, "33606"), key_risk="competition"), 14, radius_km=1.0
    )
    assert "4 open competitors within 1 km" in competition


def test_tc144_why_this_rank_vi_and_momentum() -> None:
    base = _score(0, "33606")
    momentum = replace(base, key_strength="momentum")
    assert "+12%" in why_this_rank(momentum, 14)
    vi = why_this_rank(base, 14, "vi")
    assert vi.startswith("ZIP 33606 xếp hạng #1/14 (điểm 72.4")
    assert "check-in" in vi.lower()
    assert "the search radius" in why_this_rank(replace(base, key_risk="competition"), 14)


def test_tc145_why_this_rank_tie_unavailable_fallback() -> None:
    score = _score(1, "33602", tied=True, rank_range=(2, 4), fallback=True)
    text = why_this_rank(score, 14)
    assert "rank range 2–4" in text
    assert "Not available for this area: Weather resilience." in text
    assert "The model part is the metro average because the demand model is disabled." in text
    no_interval = why_this_rank(_score(0, "33606", intervals=False), 14)
    assert "range" not in no_interval


# ---------------------------------------------------------------------------
# OpenAI-compatible explainer with injected transport (TC-146..149)
# ---------------------------------------------------------------------------


def test_tc146_llm_success(result: RecommendationResult) -> None:
    cfg = ExplainerConfig.from_env(FX_ENV_OK)
    assert cfg is not None
    transport, _ = _fake_transport()
    narrative = OpenAICompatibleExplainer(cfg, transport).explain(_request(result))
    assert narrative == explain.Narrative(LLM_OK_TEXT, "llm", "en", model="m-test", notice=None)


def test_tc147_request_shape(result: RecommendationResult) -> None:
    cfg = ExplainerConfig.from_env(FX_ENV_OK)
    assert cfg is not None
    transport, calls = _fake_transport()
    req = _request(result)
    OpenAICompatibleExplainer(cfg, transport).explain(req)
    url, headers, body, timeout = calls[0]
    assert url == "https://api.example.test/v1/chat/completions"
    assert headers["Authorization"] == f"Bearer {FX_KEY}"
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json"
    assert timeout == 30.0
    payload = json.loads(body)
    assert payload["model"] == "m-test"
    assert payload["temperature"] == 0.2
    assert payload["max_tokens"] == 800
    system, user = payload["messages"]
    assert system == {
        "role": "system",
        "content": SYSTEM_PROMPT.replace("{language_name}", "English"),
    }
    assert user["role"] == "user"
    assert user["content"].startswith("FACTS:\n```json\n")
    assert "TASK: Explain why the top areas rank where they do" in user["content"]
    facts_json = user["content"].split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(facts_json) == req.facts
    assert facts_json == json.dumps(
        req.facts, separators=(",", ":"), sort_keys=True, ensure_ascii=False
    )
    assert FX_KEY not in body.decode()


def test_tc147_question_and_compare_tasks(result: RecommendationResult) -> None:
    cfg = ExplainerConfig.from_env(FX_ENV_OK)
    assert cfg is not None
    explainer = OpenAICompatibleExplainer(cfg)
    question = "q" * 500 + "\x00"
    body = explainer.build_request_body(
        ExplainRequest("question", {"areas": []}, question=question)
    )
    user = json.loads(body)["messages"][1]["content"]
    assert user.endswith("TASK: Answer this QUESTION using FACTS only: " + "q" * 300)
    compare = explainer.build_request_body(ExplainRequest("compare", {"areas": []}))
    assert "Compare the selected areas" in json.loads(compare)["messages"][1]["content"]


def test_tc148_prompt_grounding_phrases() -> None:
    for phrase in (
        "ONLY numbers",
        "activity proxy",
        "associations, not causes",
        "unavailable_metrics",
        "at most 110 words",
        "tied_with_above",
        "No business names",
    ):
        assert phrase in SYSTEM_PROMPT


def test_tc149_language_name(result: RecommendationResult) -> None:
    cfg = ExplainerConfig.from_env(FX_ENV_OK)
    assert cfg is not None
    explainer = OpenAICompatibleExplainer(cfg)
    for language, name in (("vi", "Vietnamese"), ("en", "English")):
        body = explainer.build_request_body(_request(result, language=language))  # type: ignore[arg-type]
        system = json.loads(body)["messages"][0]["content"]
        assert f"Answer in {name}." in system
        assert "{language_name}" not in system


# ---------------------------------------------------------------------------
# Failures -> fallback (TC-150..157) with injected transport
# ---------------------------------------------------------------------------

SUFFIX = "Showing the offline summary."


def test_tc150_timeout_fallback(
    result: RecommendationResult, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    narrative = explain_with_fallback(
        _llm(error=TimeoutError("boom")), _request(result), timeout_s=30
    )
    assert narrative.source == "template"
    assert narrative.notice == f"AI provider timed out after 30 s. {SUFFIX}"
    assert any(r.levelno == logging.WARNING and "timeout" in r.getMessage() for r in caplog.records)
    assert "33606" in narrative.text


def test_tc151_auth_fallback_hides_key_and_body(
    result: RecommendationResult, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    llm = _llm(401, b'{"error":"bad key"}')
    narrative = explain_with_fallback(llm, _request(result))
    assert narrative.notice == f"AI provider rejected the credentials (HTTP 401). {SUFFIX}"
    assert narrative.source == "template"
    for text in (narrative.text, narrative.notice or "", caplog.text):
        assert FX_KEY not in text
        assert "bad key" not in text


@pytest.mark.parametrize(
    ("status", "notice"),
    [
        (500, f"AI provider returned HTTP 500. {SUFFIX}"),
        (429, f"AI provider returned HTTP 429. {SUFFIX}"),
        (302, f"AI provider returned HTTP 302. {SUFFIX}"),
        (403, f"AI provider rejected the credentials (HTTP 403). {SUFFIX}"),
    ],
)
def test_tc152_http_errors(result: RecommendationResult, status: int, notice: str) -> None:
    assert explain_with_fallback(_llm(status, b""), _request(result)).notice == notice


@pytest.mark.parametrize(
    "body",
    [
        b"<html>oops</html>",
        b'{"choices": []}',
        b'{"choices":[{"message":{"content":123}}]}',
        b'{"choices":[{"message":{}}]}',
        b'{"choices":[1]}',
        b"[1,2]",
        b"\xff\xfe",
        b"[" * 100_000,
    ],
)
def test_tc153_bad_json(result: RecommendationResult, body: bytes) -> None:
    narrative = explain_with_fallback(_llm(200, body), _request(result))
    assert narrative.notice == f"AI provider returned an unreadable response. {SUFFIX}"
    assert narrative.source == "template"


@pytest.mark.parametrize("content", ["   \n", "", "\x00\x07"])
def test_tc154_empty_content(result: RecommendationResult, content: str) -> None:
    narrative = explain_with_fallback(_llm(200, _content(content)), _request(result))
    assert narrative.notice == f"AI provider returned an empty answer. {SUFFIX}"


def test_tc155_network_error(result: RecommendationResult) -> None:
    narrative = explain_with_fallback(
        _llm(error=ConnectionRefusedError("refused")), _request(result)
    )
    assert narrative.notice == f"AI provider could not be reached. {SUFFIX}"


def test_tc156_output_truncated() -> None:
    cfg = ExplainerConfig.from_env(FX_ENV_OK)
    assert cfg is not None
    transport, _ = _fake_transport(body=_content("x" * 5000))
    narrative = OpenAICompatibleExplainer(cfg, transport).explain(ExplainRequest("top_picks", {}))
    assert len(narrative.text) <= 2000
    assert narrative.text.endswith("…")


def test_tc157_no_leaks_on_any_failure(
    result: RecommendationResult, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    req = _request(result, "question", question="secret question text")
    failures: list[OpenAICompatibleExplainer] = [
        _llm(error=TimeoutError("t")),
        _llm(401, b'{"error":"bad key"}'),
        _llm(500, b""),
        _llm(200, b"<html>"),
        _llm(200, _content("  ")),
        _llm(error=ConnectionRefusedError("c")),
    ]
    for llm in failures:
        with pytest.raises(ExplainerError) as info:
            llm.explain(req)
        narrative = explain_with_fallback(llm, req)
        for text in (repr(info.value), str(info.value), narrative.notice or "", narrative.text):
            assert FX_KEY not in text
            assert "Authorization" not in text
    facts_json = json.dumps(req.facts, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    assert FX_KEY not in caplog.text
    assert "Authorization" not in caplog.text
    assert facts_json not in caplog.text
    assert "secret question text" not in caplog.text
    assert "ai_request provider_host=api.example.test model=m-test kind=question" in caplog.text


# ---------------------------------------------------------------------------
# Real loopback server (TC-146/150..158, NFR-004)
# ---------------------------------------------------------------------------


def test_server_success_payload_and_key_only_in_header(
    local_server: LocalServer, result: RecommendationResult, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    narrative = OpenAICompatibleExplainer(local_server.config()).explain(_request(result))
    assert narrative.text == LLM_OK_TEXT
    assert narrative.source == "llm"
    (seen,) = local_server.seen
    assert seen.path == "/v1/chat/completions"
    assert seen.headers["authorization"] == f"Bearer {FX_KEY}"
    assert seen.headers["content-type"] == "application/json"
    body = seen.body.decode()
    assert FX_KEY not in body
    for needle in ("LEAK-", LEAK_NAME, LEAK_ID, *LEAK_COORDS, "display_name"):
        assert needle not in body
    assert not FORBIDDEN_KEYS & set(_walk_keys(json.loads(body)))
    assert FX_KEY not in caplog.text
    assert FX_KEY not in repr(local_server.config())


def test_server_no_key_no_authorization_header(
    local_server: LocalServer, result: RecommendationResult
) -> None:
    OpenAICompatibleExplainer(local_server.config(key="")).explain(_request(result))
    assert "authorization" not in local_server.seen[0].headers


def test_server_slow_response_times_out(
    local_server: LocalServer, result: RecommendationResult
) -> None:
    local_server.respond(Reply(delay=3.0))
    started = time.monotonic()
    narrative = explain_with_fallback(
        OpenAICompatibleExplainer(local_server.config(timeout_s=0.3)),
        _request(result),
        timeout_s=0.3,
    )
    assert time.monotonic() - started < 2.5
    assert narrative.notice == f"AI provider timed out after 0.3 s. {SUFFIX}"
    assert narrative.source == "template"


@pytest.mark.parametrize(
    ("reply", "notice"),
    [
        (Reply(401, b'{"error":"bad key"}'), f"rejected the credentials (HTTP 401). {SUFFIX}"),
        (Reply(429, b"{}"), f"returned HTTP 429. {SUFFIX}"),
        (Reply(500, b"boom"), f"returned HTTP 500. {SUFFIX}"),
        (Reply(200, b"not json"), f"returned an unreadable response. {SUFFIX}"),
        (Reply(200, _content("  \n")), f"returned an empty answer. {SUFFIX}"),
    ],
)
def test_server_failures_fall_back(
    local_server: LocalServer,
    result: RecommendationResult,
    caplog: pytest.LogCaptureFixture,
    reply: Reply,
    notice: str,
) -> None:
    caplog.set_level(logging.DEBUG)
    local_server.respond(reply)
    narrative = explain_with_fallback(
        OpenAICompatibleExplainer(local_server.config()), _request(result)
    )
    assert narrative.notice == f"AI provider {notice}"
    assert narrative.source == "template"
    assert "33606" in narrative.text
    assert FX_KEY not in caplog.text + narrative.text + (narrative.notice or "")
    assert "bad key" not in caplog.text + narrative.text + (narrative.notice or "")


def test_server_huge_body_is_capped(
    local_server: LocalServer, result: RecommendationResult
) -> None:
    huge = b'{"choices":[{"message":{"content":"' + b"a" * (3 * 1024 * 1024) + b'"}}]}'
    local_server.respond(Reply(200, huge))
    status, body = urllib_transport(local_server.base_url + "/chat/completions", {}, b"{}", 5.0)
    assert status == 200
    assert len(body) == 1024 * 1024
    narrative = explain_with_fallback(
        OpenAICompatibleExplainer(local_server.config()), _request(result)
    )
    assert narrative.source == "template"
    assert narrative.notice == f"AI provider returned an unreadable response. {SUFFIX}"


def test_tc158_redirect_not_followed(
    local_server: LocalServer, result: RecommendationResult
) -> None:
    target = f"http://127.0.0.1:{local_server.server.server_address[1]}/stolen"
    local_server.respond(Reply(302, b"", headers={"Location": target}))
    status, _ = urllib_transport(
        local_server.base_url + "/chat/completions",
        {"Authorization": f"Bearer {FX_KEY}"},
        b"{}",
        5.0,
    )
    assert status == 302
    assert [s.path for s in local_server.seen] == ["/v1/chat/completions"]
    narrative = explain_with_fallback(
        OpenAICompatibleExplainer(local_server.config()), _request(result)
    )
    assert narrative.notice == f"AI provider returned HTTP 302. {SUFFIX}"
    assert all(s.path != "/stolen" for s in local_server.seen)


def test_urllib_transport_error_mapping(local_server: LocalServer) -> None:
    local_server.respond(Reply(500, b"x" * 100_000))
    status, body = urllib_transport(local_server.base_url + "/chat/completions", {}, b"{}", 5.0)
    assert status == 500
    assert len(body) == 64 * 1024
    local_server.respond(Reply(delay=3.0))
    with pytest.raises(TimeoutError):
        urllib_transport(local_server.base_url + "/chat/completions", {}, b"{}", 0.3)
    dead = LocalServer(local_server.server)
    local_server.server.shutdown()
    local_server.server.server_close()
    with pytest.raises(OSError):  # noqa: PT011
        urllib_transport(dead.base_url + "/chat/completions", {}, b"{}", 2.0)


# ---------------------------------------------------------------------------
# Factory and transport seam (TC-159, TC-197)
# ---------------------------------------------------------------------------


def test_tc159_build_explainer() -> None:
    assert isinstance(build_explainer(None), TemplateExplainer)
    cfg = ExplainerConfig.from_env(FX_ENV_OK)
    assert isinstance(build_explainer(cfg), OpenAICompatibleExplainer)
    assert build_explainer(None).name == "template"
    assert build_explainer(cfg).name == "openai-compatible"


def test_tc197_default_transport_resolved_at_call_time(
    monkeypatch: pytest.MonkeyPatch, result: RecommendationResult
) -> None:
    cfg = ExplainerConfig.from_env(FX_ENV_OK)
    assert cfg is not None
    explainer = OpenAICompatibleExplainer(cfg)
    transport, calls = _fake_transport()
    monkeypatch.setattr(explain, "urllib_transport", transport)
    assert explainer.explain(_request(result)).text == LLM_OK_TEXT
    assert len(calls) == 1


def test_custom_fallback_explainer_is_used(result: RecommendationResult) -> None:
    class Stub:
        name = "stub"

        def explain(self, request: ExplainRequest) -> explain.Narrative:
            return explain.Narrative("stub text", "template", request.language)

    narrative = explain_with_fallback(_llm(500, b""), _request(result), fallback=Stub())
    assert narrative.text == "stub text"
    assert narrative.notice == f"AI provider returned HTTP 500. {SUFFIX}"


# ---------------------------------------------------------------------------
# Grounding hardening (TC-203..TC-208)
# ---------------------------------------------------------------------------


def test_tc203_facts_total_and_unambiguous_fields(result: RecommendationResult) -> None:
    facts = build_facts(result, kind="top_picks", top_n=3)
    assert facts["total_areas_scored"] == 12
    areas = facts["areas"]
    assert isinstance(areas, list)
    for area in areas:
        assert isinstance(area, dict)
        assert set(area["rank_range"]) == {"best", "worst"}  # type: ignore[arg-type]
        factors = area["factors"]
        assert isinstance(factors, list)
        for factor in factors:
            assert isinstance(factor, dict)
            assert "value" not in factor  # raw per-business-day values are never sent
            assert factor["label_en"] == METRIC_LABELS[factor["metric"]]  # type: ignore[index]
            assert isinstance(factor["label_vi"], str)
    first = areas[0]
    assert isinstance(first, dict)
    by_metric = {f["metric"]: f for f in first["factors"] if isinstance(f, dict)}  # type: ignore[union-attr]
    assert by_metric["demand_rate"]["display_value"] == "12.30"
    assert by_metric["demand_rate"]["unit"] == "check-ins per 1,000 business-days"
    assert by_metric["competition"]["display_value"] == "4"
    assert by_metric["competition"]["unit"] == "open competitors within 1 km"
    assert by_metric["momentum"]["display_value"] == "+12%"
    assert by_metric["weather_resilience"]["display_value"] is None
    assert by_metric["demand_rate"]["label_vi"] == "Nhu cầu"
    assert not FORBIDDEN_KEYS & set(_walk_keys(facts))
    assert "LEAK-" not in json.dumps(facts)


def test_tc204_prompt_glossary_and_rank_rule() -> None:
    pairs = (
        ("Demand", "Nhu cầu"),
        ("Competition", "Cạnh tranh"),
        ("Quality gap", "Khoảng trống chất lượng"),
        ("Momentum", "Đà tăng trưởng"),
        ("Stability", "Độ ổn định"),
        ("Weather resilience", "Khả năng chống chịu thời tiết"),
    )
    for en, vi in pairs:
        assert f"{en} / {vi}" in SYSTEM_PROMPT
    for key in ("demand_rate", "quality_gap", "weather_resilience"):
        assert key in SYSTEM_PROMPT
    assert "rank X of total_areas_scored" in SYSTEM_PROMPT
    assert "never use them as a count of areas" in SYSTEM_PROMPT
    assert "Never print raw JSON keys" in SYSTEM_PROMPT
    assert "at most 2 bullets per area" in SYSTEM_PROMPT
    assert "{language_name}" in SYSTEM_PROMPT
    assert "display_value" in SYSTEM_PROMPT


def test_tc205_unmatched_figures_matching_rules(result: RecommendationResult) -> None:
    facts = build_facts(result, kind="top_picks", top_n=3)
    good = (
        "ZIP 33606 ranks 1 of 12 (score 72.4, range 66.0–78.8; best rank 1, worst 3). "
        "Demand 12.30 check-ins per 1,000 business-days (+14.0), momentum +12%, 1000 days, "
        "score 72, 90% percentile, 2 bullets, 2016."
    )
    assert unmatched_figures(good, facts) == ()
    assert unmatched_figures("Demand is 0.0123 per business-day and 99.9 points.", facts) == (
        "0.0123",
        "99.9",
    )
    assert unmatched_figures("Điểm 72,4 trên 1.000 business-day", facts) == ()


@pytest.mark.parametrize(
    ("text", "bad"),
    [
        ("Xếp hạng: 1 trong số 19 khu vực", ("trong số 19",)),
        ("rank 2 of 34 areas", ("of 34",)),
        ("#3/7", ("#3/7",)),
        ("ranks 1 of 12 areas", ()),
        ("Xếp hạng 1 trong số 12", ()),
        ("score of 72.4", ()),
    ],
)
def test_tc206_population_structure(
    result: RecommendationResult, text: str, bad: tuple[str, ...]
) -> None:
    facts = build_facts(result, kind="top_picks", top_n=5)
    found = unmatched_figures(text, facts)
    assert tuple(f.strip() for f in found if "of" in f or "số" in f or "#" in f) == bad


def test_tc207_server_notice_only_on_ungrounded_figures(
    local_server: LocalServer, result: RecommendationResult
) -> None:
    for language in ("en", "vi"):
        req = _request(result, language=language)
        wrong = "Xếp hạng: 1 trong số 19 khu vực" if language == "vi" else "ZIP 33606 is 1 of 19."
        local_server.respond(Reply(200, _content(wrong)))
        bad = OpenAICompatibleExplainer(local_server.config()).explain(req)
        assert bad.source == "llm"
        assert bad.text == wrong  # text is kept
        assert bad.notice == GROUNDING_NOTICES[language]
        right = "Xếp hạng 1 trong số 12, điểm 72.4." if language == "vi" else "ZIP 33606: 1 of 12."
        local_server.respond(Reply(200, _content(right)))
        ok = explain_with_fallback(OpenAICompatibleExplainer(local_server.config()), req)
        assert ok.notice is None
        assert ok.text == right
    assert GROUNDING_NOTICES["en"] == (
        "Some figures in this AI text could not be matched to the data. "
        "Verify against the table above."
    )


def test_tc208_template_reads_legacy_fact_shapes() -> None:
    facts: JsonObject = {
        "data_notes": {"areas_scored": 14},
        "areas": [
            {
                "postal_code": "33606",
                "rank": 1,
                "rank_range": [1, 3],
                "tied_with_above": True,
                "score": 72.4,
                "key_strength": "demand_rate",
                "factors": [
                    {"metric": "demand_rate", "value": 0.0123, "delta_vs_median": 14.0},
                ],
            }
        ],
    }
    text = TemplateExplainer().explain(ExplainRequest("top_picks", facts)).text
    assert "#1 of 14" in text
    assert "12.30 check-ins per 1,000 business-days" in text
    assert "rank range 1–3" in text
