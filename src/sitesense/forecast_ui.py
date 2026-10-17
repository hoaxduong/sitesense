"""Native Streamlit controls for the explicitly trained forecast artifact."""

import hashlib
from datetime import date
from pathlib import Path
from typing import cast

import pandas as pd  # type: ignore[import-untyped]
import streamlit as st

from sitesense import forecast
from sitesense.models import BUSINESS_CATEGORY_LABELS

TRAINING_NOTEBOOK = "notebooks/04_full_history_forecast_training.ipynb"
WEATHER_MODES = ("Enter a weather scenario", "Use observed station weather")


def _artifact_revision(directory: Path) -> str:
    try:
        return hashlib.sha256((directory / "manifest.json").read_bytes()).hexdigest()
    except OSError:
        raise forecast.ArtifactUnavailableError("Forecast model unavailable") from None


@st.cache_resource(ttl=300, max_entries=2, show_spinner=False)
def _forecast_bundle(directory: str, revision: str) -> forecast.ForecastBundle:
    """Manifest revision invalidates the model cache after explicit retraining."""
    return forecast.load_bundle(Path(directory))


def _model_setup() -> None:
    st.info("Forecast model unavailable")
    st.markdown(f"Train the model with `{TRAINING_NOTEBOOK}`, then reload this page.")
    st.code("uv run --frozen --group notebooks jupyter lab --notebook-dir=.", language="bash")
    st.caption(f"Open {TRAINING_NOTEBOOK} and use Restart Kernel and Run All.")


def _prediction_table(results: pd.DataFrame, top_n: int) -> None:
    details = results.attrs
    st.subheader(f"Suggested areas · {details['date']}")
    best = results.iloc[0]
    category = BUSINESS_CATEGORY_LABELS.get(best["category"], best["category"])
    st.caption(
        f"{best['metro']} · {category} · "
        f"{details['weather_complete_candidates']} of {details['candidate_count']} areas scored"
    )
    st.markdown(
        f"**Highest predicted rate · {str(best['city']).title()}, {best['state']} "
        f"· ZIP {best['zip']}**"
    )
    with st.container(horizontal=True, gap="small"):
        st.metric("Weather model", f"{best['predicted_rate']:.4f}", border=True)
        st.metric("Calendar baseline · same area", f"{best['baseline_rate']:.4f}", border=True)
    st.caption("Recorded check-ins per historical cohort business per day.")
    if details["weather_excluded_candidates"]:
        st.info(
            f"{details['weather_excluded_candidates']} areas excluded for missing weather. "
            "Enter a scenario to compare all candidates."
        )
    if details["outside_training_weather_range_count"]:
        st.warning(
            "Some weather values are outside the training range. Predictions for those values "
            "are extrapolations."
        )
    display = (
        results.head(top_n)
        .assign(area=lambda frame: frame.city.str.title() + ", " + frame.state)[
            ["rank", "area", "zip", "predicted_rate", "baseline_rate"]
        ]
        .rename(
            columns={
                "rank": "#",
                "area": "Area",
                "zip": "ZIP",
                "predicted_rate": "Weather model",
                "baseline_rate": "Calendar baseline",
            }
        )
    )
    st.dataframe(
        display,
        hide_index=True,
        column_config={
            "#": st.column_config.NumberColumn(width="small"),
            "Weather model": st.column_config.ProgressColumn(
                format="%.4f",
                min_value=0,
                max_value=float(results.predicted_rate.max()),
                help="Predicted recorded check-ins per historical cohort business per day.",
            ),
            "Calendar baseline": st.column_config.NumberColumn(
                format="%.4f", help="Historical mean for this area, industry and calendar month."
            ),
        },
        alt="Candidate areas ranked by predicted recorded check-ins per cohort business per day",
    )
    st.caption(f"Showing the top {min(top_n, len(results))} areas, ranked by the weather model.")
    st.download_button(
        "Download all predictions (CSV)",
        results.to_csv(index=False).encode("utf-8"),
        file_name=f"sitesense_forecast_{details['date']}.csv",
        mime="text/csv",
        key="forecast_download",
        on_click="ignore",
        icon=":material/download:",
    )
    with st.expander("Weather used for this prediction", icon=":material/cloud:"):
        st.caption(
            "One supplied scenario is shared across this metro."
            if details["weather_source"] == "supplied_weather"
            else "Observed ACIS weather from each area's mapped station, for the selected date."
        )
        st.dataframe(
            results.head(top_n)[["city", "zip", *forecast.WEATHER]].rename(
                columns={
                    "city": "City",
                    "zip": "ZIP",
                    "tmax_c": "High °C",
                    "tmin_c": "Low °C",
                    "precip_mm": "Rain mm",
                }
            ),
            hide_index=True,
            column_config={
                name: st.column_config.NumberColumn(format="%.1f")
                for name in ("High °C", "Low °C", "Rain mm")
            },
            alt="Weather inputs for the displayed candidate areas",
        )


def render_forecast() -> None:
    st.caption("Compare ZIP areas for your industry, date and weather scenario.")
    try:
        revision = _artifact_revision(forecast.MODEL_DIR)
        with st.spinner("Loading trained forecast model…"):
            bundle = _forecast_bundle(str(forecast.MODEL_DIR), revision)
    except forecast.ArtifactUnavailableError:
        _model_setup()
        return
    locations = bundle.locations
    with st.sidebar:
        st.subheader("Your market", icon=":material/storefront:")
        metros = sorted(locations["metro"].unique().tolist())
        if st.session_state.get("forecast_metro") not in metros:
            st.session_state["forecast_metro"] = "Tampa" if "Tampa" in metros else metros[0]
        metro = st.selectbox(
            "Metro area",
            metros,
            key="forecast_metro",
            help=(
                "County-based metro groups from the training catalog, broader than the city "
                "filters on historical pages."
            ),
        )
        categories = sorted(
            locations.loc[locations["metro"] == metro, "category"].unique().tolist()
        )
        if st.session_state.get("forecast_category") not in categories:
            st.session_state["forecast_category"] = (
                "Day Spas" if "Day Spas" in categories else categories[0]
            )
        category = st.selectbox(
            "Business category",
            categories,
            format_func=lambda value: BUSINESS_CATEGORY_LABELS.get(value, value),
            key="forecast_category",
        )
        st.caption("County-based metro groups · Yelp ZIP areas")
    candidates = int(((locations["metro"] == metro) & (locations["category"] == category)).sum())
    if not 1 <= st.session_state.get("forecast_top_n", 1) <= min(25, candidates):
        st.session_state["forecast_top_n"] = min(10, candidates)
    with st.container(border=True):
        with st.container(horizontal=True, vertical_alignment="center"):
            st.subheader("Forecast setup", icon=":material/tune:", width="stretch")
            mode = st.segmented_control(
                "Weather input",
                WEATHER_MODES,
                default=WEATHER_MODES[0],
                required=True,
                format_func=lambda value: "Scenario" if value == WEATHER_MODES[0] else "Observed",
                key="forecast_weather_mode",
                width="content",
                label_visibility="collapsed",
            )
        scope = (revision, metro, category, mode)
        st.session_state.setdefault("forecast_result", None)
        if st.session_state.get("forecast_result_scope") != scope:
            st.session_state["forecast_result"] = None
            st.session_state["forecast_result_scope"] = scope
        with st.form("forecast_inputs", border=False):
            with st.container(horizontal=True, vertical_alignment="bottom"):
                selected_date = st.date_input(
                    "Prediction date",
                    value=date(2026, 12, 15),
                    min_value=date.min,
                    max_value=date.max,
                    key="forecast_date",
                )
                weather = None
                if mode == WEATHER_MODES[0]:
                    tmax = st.number_input(
                        "High (°C)",
                        value=27.0,
                        format="%.1f",
                        key="forecast_tmax",
                        persist_state="session",
                    )
                    tmin = st.number_input(
                        "Low (°C)",
                        value=19.0,
                        format="%.1f",
                        key="forecast_tmin",
                        persist_state="session",
                    )
                    rain = st.number_input(
                        "Rain (mm)",
                        min_value=0.0,
                        value=2.0,
                        format="%.1f",
                        key="forecast_rain",
                        persist_state="session",
                    )
                    weather = {"tmax_c": tmax, "tmin_c": tmin, "precip_mm": rain}
                top_n = st.number_input(
                    "Areas to display",
                    min_value=1,
                    max_value=min(25, candidates),
                    value=min(10, candidates),
                    key="forecast_top_n",
                )
                submitted = st.form_submit_button(
                    "Predict",
                    key="forecast_predict",
                    type="primary",
                    icon=":material/online_prediction:",
                    width="stretch",
                )
        st.caption(
            "Your weather scenario applies to all candidate areas."
            if mode == WEATHER_MODES[0]
            else "Use station observations for this date; missing weather is excluded. "
            "For future dates, choose Scenario."
        )
    with st.container(border=True):
        if submitted:
            st.session_state["forecast_result"] = None
            if not isinstance(selected_date, date):
                st.warning("Select a prediction date.")
            else:
                try:
                    with st.spinner("Comparing candidate areas…"):
                        st.session_state["forecast_result"] = forecast.predict_locations(
                            bundle, selected_date, metro, category, weather
                        )
                except forecast.ArtifactUnavailableError:
                    _model_setup()
                    return
                except ValueError as error:
                    st.warning(str(error))
        result = st.session_state["forecast_result"]
        if result is not None:
            _prediction_table(cast(pd.DataFrame, result), int(top_n))
        elif not submitted:
            st.subheader("Find areas worth exploring", icon=":material/travel_explore:")
            st.markdown(f"**{metro} · {BUSINESS_CATEGORY_LABELS.get(category, category)}**")
            st.caption(f"{candidates} candidate ZIP areas ready to compare.")
            st.markdown("Choose a date and weather, then select **Predict** to see the ranking.")
            st.caption("Results include the weather model and a historical calendar baseline.")
    st.caption(
        "A historical activity estimate for area screening. It does not measure customers, "
        "revenue or new-store success."
    )
    with st.expander("About the model and historical evaluation", icon=":material/info:"):
        st.caption(
            f"HGB weather model · trained {bundle.manifest['trained_start']} "
            f"to {bundle.manifest['trained_end']}. "
            "Prediction uses weather, location, industry and calendar date; no activity inputs."
        )
        st.caption(
            "Exploratory temporal evaluation on the historical catalog, before the final fit. "
            "Observed target-day weather makes this a conditional prediction "
            "evaluation. Lower Poisson deviance and MAE are better."
        )
        metrics = bundle.manifest.get("evaluation_metrics", [])
        if metrics:
            st.dataframe(
                metrics, hide_index=True, alt="Historical validation and test model metrics"
            )
        st.caption(
            "The calendar baseline remains a comparison. Historical accuracy does not verify "
            "2026 accuracy because the archive has no activity labels for 2026."
        )
