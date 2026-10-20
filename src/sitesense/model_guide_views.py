"""Plain-language evidence for choosing a business weather model."""

from pathlib import Path
from typing import Any

import streamlit as st

from sitesense import business_hgb, model_guide


@st.cache_data(ttl=300, max_entries=2, show_spinner=False)
def _research(root: str, revision: str, import_revision: str) -> dict[str, Any]:
    return model_guide.load_research(Path(root), import_revision)


def _error_chart(scores: list[dict[str, Any]], metric: str, label: str, alt: str) -> None:
    st.vega_lite_chart(
        scores,
        {
            "mark": "bar",
            "encoding": {
                "x": {"field": "Period", "type": "nominal", "axis": {"labelAngle": 0}},
                "xOffset": {"field": "Model"},
                "y": {
                    "field": metric,
                    "type": "quantitative",
                    "title": label,
                    "scale": {"zero": True},
                },
                "color": {"field": "Model", "type": "nominal", "legend": {"orient": "top"}},
                "tooltip": [
                    {"field": "Model"},
                    {"field": "Period"},
                    {"field": metric, "format": ".4f"},
                    {"field": "Business-days"},
                ],
            },
        },
        width="stretch",
        height=280,
        alt=alt,
    )


def render_full_results(summary: dict[str, Any]) -> None:
    manifest = summary["manifest"]
    scores = [r for r in summary["scores"] if r["Model"] != "Seasonal model"]
    st.caption(
        f"App training run: {manifest['created_at'][:10]} · "
        f"{manifest['final_businesses']:,} business/category records in the final model. "
        "This covers all supported city/category groups, not just the research sample below."
    )
    deltas = []
    for period in ("2020: validation", "2021: later check"):
        selected = {r["Model"]: r for r in scores if r["Period"] == period}
        change = model_guide.error_reduction(
            selected["Daily average"]["Error score"], selected["HGB — weather model"]["Error score"]
        )
        deltas.append((period, change))
    with st.container(horizontal=True):
        for period, change in deltas:
            direction = "lower" if change is not None and change >= 0 else "higher"
            st.metric(
                period,
                f"{abs(change):.2f}% {direction}" if change is not None else "Not defined",
                border=True,
                help="HGB error score compared with the daily-average model.",
            )
    st.caption(
        "These percentages compare error scores, not accuracy percentages. "
        "Each year's eligible businesses differ; comparisons are within each year."
    )
    for metric, label in (
        ("Error score", "Error score — lower is better"),
        ("Daily error", "Average daily error in check-ins — lower is better"),
    ):
        _error_chart(
            scores, metric, label, f"Full app evaluation: {label}, HGB versus daily average"
        )
    st.markdown(
        "**Why the later check matters:** the year after validation tells us whether the "
        "earlier result carries forward. A validation win alone is not enough to claim "
        "HGB is the most accurate model."
    )
    st.markdown("**Where does HGB help? Results by city and business type**")
    scopes = summary["scopes"]
    st.vega_lite_chart(
        scopes,
        {
            "mark": "bar",
            "encoding": {
                "y": {"field": "Group", "type": "nominal", "title": None, "sort": None},
                "x": {
                    "field": "Error reduction (%)",
                    "type": "quantitative",
                    "title": "2021 error reduction versus daily average (%)",
                },
                "color": {
                    "field": "HGB has lower error",
                    "type": "nominal",
                    "scale": {"domain": [True, False], "range": ["#287359", "#b07835"]},
                    "legend": None,
                },
                "tooltip": [
                    {"field": "Group"},
                    {"field": "Businesses"},
                    {"field": "Business-days"},
                    {"field": "Error reduction (%)", "format": ".2f"},
                ],
            },
        },
        width="stretch",
        height=300,
        alt="2021 HGB error reduction by city and category; negative values mean higher error",
    )
    wins = sum(row["HGB has lower error"] for row in scopes)
    st.caption(
        f"HGB has lower 2021 error in {wins} of {len(scopes)} groups. Positive bars favor HGB; "
        "negative bars favor the average. Small spa groups have limited evidence. "
        "These are descriptive differences, not statistical significance tests."
    )
    with st.expander("Exact city/category results and sample sizes"):
        st.dataframe(scopes, hide_index=True, alt="2021 HGB and average error by city and category")


def render_three_model_research(import_revision: str) -> None:
    st.subheader("Three-model comparison: average, Poisson and HGB")
    st.markdown(
        "The app evaluation above covers the full supported population but does not include "
        "Poisson. This separate research experiment tests all three models on one smaller "
        "business sample. We keep its results separate from the full app evaluation."
    )
    root = business_hgb.ROOT / "data" / "processed" / "business_model_comparison"
    try:
        evidence = _research(str(root), model_guide.research_revision(root), import_revision)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        st.info("The verified three-model research comparison is not available in this checkout.")
        return
    manifest, scores = evidence["manifest"], evidence["scores"]
    config = manifest["config"]
    size = next(r["Businesses"] for r in scores if r["Stage"] == "validation")
    st.caption(
        f"{config['city']}, {config['state']} · {config['category']} · "
        f"{config['source_category'] or 'All source categories'} · "
        f"{size} validation businesses · run {manifest['created_at'][:10]}. "
        "All three models use identical business/day labels within each period."
    )
    table = []
    for name in ("Daily average", "Poisson", "HGB"):
        yearly = {r["Stage"]: r for r in scores if r["Model"] == name}
        table.append(
            {
                "Model": name,
                "Uses weather": name != "Daily average",
                "2020 error score": yearly["validation"]["Error score"],
                "2021 error score": yearly["test"]["Error score"],
                "2021 daily error": yearly["test"]["Daily error"],
                "2021 recorded check-ins": yearly["test"]["Recorded check-ins"],
                "2021 estimated check-ins": yearly["test"]["Estimated check-ins"],
            }
        )
    st.dataframe(table, hide_index=True, alt="Three-model research comparison on matched days")
    _error_chart(
        scores,
        "Error score",
        "Error score — lower is better",
        "Three-model validation and later-year error comparison",
    )
    validation = {r["Model"]: r for r in scores if r["Stage"] == "validation"}
    change = model_guide.error_reduction(
        validation["Poisson"]["Error score"], validation["HGB"]["Error score"]
    )
    if evidence["weather_choice"] == "HGB" and change is not None:
        st.success(
            f"On 2020 validation, HGB's error is {change:.2f}% lower than Poisson's. "
            "Among the two weather models shown here, this supports choosing HGB "
            "before looking at the 2021 results."
        )
    else:
        st.warning(
            "These validation results do not favor HGB over Poisson. "
            "The model choice needs further review rather than an accuracy claim."
        )
    st.caption(
        "The average is still a valid overall competitor. Beating Poisson does not mean "
        "HGB beats the average or every other model. This sample does not establish "
        "performance in other cities or categories."
    )
    st.markdown("**What do the estimates look like against real check-ins?**")
    st.vega_lite_chart(
        evidence["monthly"],
        {
            "mark": {"type": "line", "point": True},
            "encoding": {
                "x": {"field": "Month", "type": "ordinal", "title": "2021 month"},
                "y": {
                    "field": "Check-ins",
                    "type": "quantitative",
                    "title": "Total check-ins on matched sample days",
                    "scale": {"zero": True},
                },
                "color": {"field": "Series", "type": "nominal", "legend": {"orient": "top"}},
                "tooltip": [
                    {"field": "Month"},
                    {"field": "Series"},
                    {"field": "Check-ins", "format": ".2f"},
                ],
            },
        },
        width="stretch",
        height=300,
        alt="Saved 2021 held-out monthly estimates from three models versus recorded check-ins",
    )
    hgb_test = next(r for r in scores if r["Stage"] == "test" and r["Model"] == "HGB")
    st.caption(
        f"On these 2021 sample days, {hgb_test['Recorded check-ins']:,} check-ins were recorded "
        f"and HGB estimated {hgb_test['Estimated check-ins']:,.1f}. "
        "A small average daily error can still hide a large error in total activity. "
        "These are saved held-out predictions, not a final model fitted to the same year."
    )
    with st.expander("Research scope and verification"):
        st.markdown(
            f"Run: `{Path(evidence['directory']).name}`. Code, lockfile, parent lineage "
            "and every saved payload hash are verified. The research run must share the "
            "app bundle's imported dataset revision. This standalone page does not query "
            "the live database or deserialize research estimators. Previously examined "
            "years remain exploratory backtests."
        )
