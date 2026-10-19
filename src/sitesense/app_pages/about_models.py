"""Plain-language guide to the business HGB model and its measured baselines."""

import streamlit as st

from sitesense import business_hgb, model_guide

st.caption("How our model works")
st.markdown(
    "SiteSense uses **HGB** to estimate daily Yelp check-ins for individual businesses. "
    "It learns patterns from each business's history, the month, the day of the week, "
    "temperature and rain. We study restaurants and day spas in Philadelphia, Nashville "
    "and Tampa. Check-ins are recorded activity, not customer counts or sales."
)
st.info(
    "HGB uses real recorded weather by default. Scenarios are a separate option for "
    "dates without observations or for what-if comparisons. The daily average remains a "
    "comparison model. Using HGB does not mean it has beaten that simpler model."
)

st.subheader("1. What HGB does")
st.markdown("""
Think of HGB as a series of small decision trees. Each new tree learns how to reduce
mistakes made by the earlier ones. Together, they can learn patterns such as how
check-ins vary by business, weekday and weather.

For example, you can compare **a dry day** with **a rainy day** for the same business.
HGB calculates an estimate for each scenario. It may give similar estimates if it
has not learned a weather difference for that business.

**A simple comparison:** a business with 30 check-ins over 100 days has a daily
average of 0.3 check-ins. That model gives the same daily estimate regardless of rain.
HGB can change its estimate when the calendar or weather inputs change.
Both give expected counts over time, not a count of individual customers.
""")

st.subheader("2. How we train")
st.markdown("""
1. **Prepare each business's daily records.** Include days with no check-ins inside
   the recorded period. Use only businesses with enough earlier history.
2. **Add weather.** Match daily temperatures and rain from the business's mapped station.
   Train and compare weather models only on days with complete, accepted weather records.
3. **Learn from 2017–2019 and check 2020.** This lets us examine errors before the later test.
4. **Learn from 2018–2020 and check 2021.** Keep the model settings fixed for this later check.
5. **Prepare the app model using 2019–2021.** The app then uses HGB for weather scenarios.

We keep the daily average and a seasonal model as comparisons. They help us see
whether the extra complexity adds value. We do not automatically switch the app
back to the average when it has a lower error score.
""")

st.subheader("3. How we evaluate")
st.markdown("""
We compare estimates with the actual recorded check-ins on the same business-days.
All three models are scored on the same days with accepted weather, including zero
check-in days. This makes the comparison fair.

- **Error score (Poisson deviance):** measures how well the estimates match check-in
  counts. Lower is better. It is not an accuracy percentage.
- **Average daily error (MAE):** the average size of a mistake in daily check-ins.
  Lower is better.

A simpler model can win. We keep that result visible while developing HGB for
weather-based analysis. Past-year tests do not establish accuracy for today's market.
""")

st.subheader("4. Compare the results")
st.caption(
    "Saved training-run results, checked against local code and artifact hashes. "
    "This page does not connect to the database to check its current revision."
)
try:
    summary = model_guide.load_summary(business_hgb.MODEL_DIR)
except (OSError, ValueError, TypeError, KeyError, AttributeError):
    st.info(
        "Saved HGB evaluation results are unavailable or need to be refreshed after retraining."
    )
else:
    manifest = summary["manifest"]
    st.caption(
        f"Archived run: {manifest['created_at'][:10]} · "
        f"{manifest['final_businesses']:,} business/category records in the final model. "
        "The evaluation uses the businesses eligible in each earlier training window."
    )
    for metric, label in (
        ("Error score", "Error score — lower is better"),
        ("Daily error", "Average daily error in check-ins — lower is better"),
    ):
        st.vega_lite_chart(
            summary["scores"],
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
                    "color": {
                        "field": "Model",
                        "type": "nominal",
                        "scale": {"range": ["#467caa", "#287359", "#b07835"]},
                        "legend": {"orient": "top"},
                    },
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
            alt=f"{label}: HGB compared with the daily average and seasonal model",
        )
    later = [row for row in summary["scores"] if row["Period"] == "2021: later check"]
    best = min(later, key=lambda row: row["Error score"])
    st.markdown(
        f"**What this shows:** {best['Model']} has the lowest 2021 error score in this run. "
        "HGB stays the weather-scenario model; this result is a measure of its current "
        "limitations, not a reason to label it the most accurate model."
    )
    with st.expander("View the exact scores"):
        st.dataframe(summary["scores"], hide_index=True, alt="Saved matched-day evaluation scores")

st.subheader("5. Which years are used?")
timeline = [
    {"Step": step, "Start": start, "End": end, "Purpose": purpose}
    for step, start, end, purpose in (
        ("First check", "2017-01-01", "2020-01-01", "Learn"),
        ("First check", "2020-01-01", "2021-01-01", "Check"),
        ("Later check", "2018-01-01", "2021-01-01", "Learn"),
        ("Later check", "2021-01-01", "2022-01-01", "Check"),
        ("App model", "2019-01-01", "2022-01-01", "Learn"),
    )
]
st.vega_lite_chart(
    timeline,
    {
        "mark": {"type": "bar", "height": 28},
        "encoding": {
            "y": {"field": "Step", "type": "nominal", "sort": None, "title": None},
            "x": {"field": "Start", "type": "temporal", "axis": {"format": "%Y"}},
            "x2": {"field": "End"},
            "color": {
                "field": "Purpose",
                "type": "nominal",
                "scale": {"range": ["#b07835", "#467caa"]},
                "legend": {"orient": "top"},
            },
            "tooltip": [
                {"field": "Step"},
                {"field": "Purpose"},
                {"field": "Start"},
                {"field": "End", "title": "End (exclusive)"},
            ],
        },
    },
    width="stretch",
    height=200,
    alt="Training and checking timeline: learn from earlier years, then check a later year",
)
st.caption("Each checking period comes after its training period. The app model ends in 2021.")

st.subheader("6. Real weather and what-if scenarios")
st.markdown("""
Check-in history and weather history end on different dates. A day after the check-in
archive can still have real weather observations. Choose the weather source in the sidebar:

- **Observed weather — default:** use real daily temperature and rain from the mapped
  station. HGB estimates the check-ins; the weather itself is recorded data.
- **Seasonal weather:** use the mapped station's monthly averages from 2019–2021.
- **Custom weather:** enter the daily low, daily high and rain. The same values are
  applied to every selected day, so you can compare scenarios.

Seasonal and custom weather are **what-if inputs**, not live weather forecasts.
For observed-weather estimates, a business needs complete temperature and rain records
for the selected projected period. Missing observations stay unavailable; we do not
silently replace them with a scenario. You can choose a shorter period or explicitly
select seasonal or custom weather. The model still needs enough earlier business history.
""")

st.subheader("7. What to keep in mind")
st.markdown("""
- Check-ins are not customer counts, sales or a prediction that a new store will succeed.
- History ends in 2022. Later openings, closures and market changes are not included.
- A change in an HGB estimate does not prove that weather causes a change in visits.
- We can also compare Poisson regression and CatBoost, using the same business-level tests.
""")

with st.expander("Technical details and sources"):
    st.markdown("""
    Target: integer check-ins per business/calendar day. Features: business identity,
    month, weekday, daily minimum/maximum temperature (°C) and precipitation (mm).
    HGB uses Poisson loss, 100 iterations, learning rate 0.05, at most 15 leaves,
    L2 regularization 1, seed 42 and no random early stopping.

    Businesses need at least 20 training check-ins and first recorded activity on or
    before the training start. Eligibility is rebuilt for each training window.
    Batches contain at most 128 businesses to stay within HGB's categorical limits;
    this is not a 128-business sample cap. Final monthly weather references use training
    data only. Errors are weighted equally by business/parent-category calendar day.

    Source categories may overlap. City groups follow the imported dataset, not verified
    official metro boundaries. Check-in timezone and station observation windows are
    unverified. No calibrated prediction interval is provided.

    Model and evaluation: `models/business_hgb/manifest.json` and `models.joblib`.
    Training: `scripts/train_business_hgb.py`, `src/sitesense/business_hgb_training.py`.
    Serving: `src/sitesense/business_hgb.py` and `src/sitesense/business_activity.py`.
    Saved evaluations check code, lockfile, package versions and payload hashes without
    connecting to the database. Serving additionally requires the current import revision.
    """)
