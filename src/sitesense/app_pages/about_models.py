"""Plain-language guide to the business HGB model and its measured baselines."""

import streamlit as st

from sitesense import business_hgb, model_guide, model_guide_views

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

st.subheader("Why we chose HGB")
st.markdown("""
We need a model that can use **real weather records** to estimate activity for each
business. We compare three approaches:

| Model | How it estimates activity | What it contributes |
| --- | --- | --- |
| Daily average | Repeats a business's historical daily average | A clear baseline to beat |
| Poisson | Learns separate calendar and weather effects | A simpler weather model |
| HGB | Learns combined business, calendar and weather patterns | Our working weather model |

**Why HGB fits the task:** it can learn different weather patterns for different
businesses and days. Its Poisson training objective is designed for nonnegative
counts such as check-ins. It also uses the scikit-learn stack already in the app.
These are reasons it is a suitable candidate, not proof that it predicts best.

**What justifies choosing it over Poisson?** The matched validation comparison
below answers that with measured scores. We also show the later-year results and
where the daily average still wins. We do not choose a model by looking only at
its most favorable result.
""")

st.subheader("1. What HGB does", anchor="forecast-model")
st.caption("**WBS: US3 · T026 · Forecast model**")
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

st.subheader("2. How we train", anchor="model-training")
st.caption("**WBS: US3 · T025 · Baseline · T026 · Forecast model**")
st.markdown("""
1. **Prepare each business's daily records.** Include days with no check-ins inside
   the recorded period. Use only businesses with enough earlier history.
2. **Add weather.** Match daily temperatures and rain from the business's mapped station.
   Train and compare weather models only on days with complete, accepted weather records.
3. **Learn from 2017–2019 and check 2020.** This lets us examine errors before the later test.
4. **Learn from 2018–2020 and check 2021.** Keep the model settings fixed for this later check.
5. **Prepare the app model using 2019–2021.** The app uses observed weather by default;
   seasonal and custom weather are explicit what-if options.

The daily average is our simple reference. Poisson and HGB both use weather, so
comparing them helps us examine whether HGB's extra flexibility adds value.
The full app evaluation and the smaller three-model experiment are shown separately.
""")

st.subheader("3. How we evaluate", anchor="model-evaluation")
st.caption("**WBS: US3 · T027 · Forecast evaluation**")
st.markdown("""
We compare estimates with the actual recorded check-ins on the same business-days.
Within each experiment, models are scored on the same days with accepted weather, including zero
check-in days. This makes the comparison fair.

The average learns from all covered training days; weather models need complete weather
records for training. Their scores compare the resulting approaches, not weather alone.

- **Error score (Poisson deviance):** measures how well the estimates match check-in
  counts. Lower is better. It is not an accuracy percentage.
- **Average daily error (MAE):** the average size of a mistake in daily check-ins.
  Lower is better.

A simpler model can win. We keep that result visible while developing HGB for
weather-based analysis. Past-year tests do not establish accuracy for today's market.
""")

st.subheader("4. Compare the results")
st.caption("**WBS: US3 · T025 · Baseline · T027 · Evaluation results**")
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
    model_guide_views.render_full_results(summary)
    model_guide_views.render_three_model_research(summary["manifest"]["import_revision"])

st.markdown("**How strong is the case for HGB?**")
st.markdown("""
HGB is a reasonable working model for our weather-aware analysis. Its advantage
over Poisson must be checked on validation data; its advantage over a simple
average is mixed across periods and business groups. The charts do not establish
that weather itself improves accuracy: that needs the same HGB trained with and
without weather on identical data.

Before claiming HGB is the best model overall, we need more forward-time checks,
stronger results in groups where it loses, and uncertainty estimates. We have not
measured a speed advantage or proved that it predicts new-store success.
""")

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
- Validation supports a choice before a later check; later results must still be reported.
- Three models are compared here: daily average, Poisson and HGB.
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
    Three-model research: notebook 05 and its verified `business_model_comparison` run.
    HGB design: [scikit-learn histogram gradient boosting documentation](https://scikit-learn.org/stable/modules/ensemble.html#histogram-based-gradient-boosting).
    Saved evaluations check code, lockfile, package versions and payload hashes without
    connecting to the database. Serving additionally requires the current import revision.
    """)
