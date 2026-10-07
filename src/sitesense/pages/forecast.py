"""Page 4 · Demand forecast: what demand should we expect? (Features 4 and 6, US3)"""

import pandas as pd
import streamlit as st

from sitesense import charts, queries, scoring
from sitesense.components.notices import FORECAST, page_header
from sitesense.components.sidebar import current_filters
from sitesense.pages.weather import EVENTS, effect_row

TYPICAL = "Typical season"
SCENARIOS = {
    TYPICAL: None,
    "Heavy-rain week": "heavy_rain",
    "Hot week": "hot_day",
    "Cold week": "cold_day",
    "Snow week": "snowfall",
}


def scenario_effect(effects: pd.DataFrame, area_id: str, event: str | None) -> tuple[float, str]:
    """Effect (%) for the scenario: the area's own estimate if clear, else the metro one."""
    if event is None:
        return 0.0, ""
    level = EVENTS[event].default
    for scope, source in ((area_id, "this area"), (None, "whole metro")):
        row = effect_row(effects, scope, event, level)
        if row is not None and scoring.is_reliable(int(row.n_events), row.p_value):
            return float(row.effect_pct), source
    return 0.0, "no clear effect"


def _pct(value: float) -> str:
    return f"{value:+.0f}%".replace("-", "−")


def render() -> None:
    filters = current_filters()
    page_header(
        "What demand should we expect?",
        "Weekly check-in forecast for one area, in a typical season or with a weather disruption.",
        FORECAST,
    )
    areas = queries.areas(filters.metro)
    effects = queries.weather_effects(filters.metro, filters.categories)
    names = {str(a.area_id): f"{a.name} ({a.area_id})" for a in areas.itertuples()}

    with st.container(border=True):
        top = st.columns(3)
        area_id = top[0].selectbox("Area", list(names), format_func=names.__getitem__)
        horizon = top[1].slider("Forecast horizon (weeks)", 4, 12, 12)
        week = top[2].select_slider(
            "Disruption week",
            options=list(range(1, horizon + 1)),
            value=min(6, horizon),
            format_func=lambda w: f"W+{w}",
        )
        scenario = st.radio("Scenario", list(SCENARIOS), index=1, horizontal=True)

    forecast = queries.forecasts(filters.metro, filters.categories, area_id)
    forecast = forecast[forecast.week_offset <= horizon]
    effect, source = scenario_effect(effects, area_id, SCENARIOS[scenario])
    result = scoring.scenario_forecast(forecast, effect, week)
    history = queries.weekly_history(filters.metro, area_id).sort_values("week_start").tail(26)
    run = queries.model_runs().iloc[0]

    total, typical_total = result.scenario.sum(), result.yhat.sum()
    in_week = result[result.week_offset == week].iloc[0]
    impact = in_week.scenario - in_week.yhat
    columns = st.columns(4)
    columns[0].metric(
        f"Expected check-ins, next {horizon} wks",
        f"{total:,.0f}",
        f"{_pct((total / typical_total - 1) * 100)} vs typical season",
        delta_color="normal" if total >= typical_total else "inverse",
    )
    columns[1].metric(
        "Weekly average",
        f"{result.scenario.mean():.0f}",
        f"80% range {result.scenario_lo.mean():.0f}–{result.scenario_hi.mean():.0f}",
        delta_color="off",
    )
    columns[2].metric(
        "Impact in disruption week",
        f"{impact:+.0f}".replace("-", "−"),
        f"{_pct(effect)} in week W+{week}" if effect else "no disruption",
        delta_color="off" if not effect else "normal",
    )
    gain = 1 - run.mae / run.baseline_mae
    columns[3].metric(
        "Model vs simple baseline", f"{gain:.0%}", "lower error (MAE)", delta_color="off"
    )

    st.subheader(f"Weekly check-ins · {names[area_id]}")
    st.caption(f"Last {len(history)} weeks of data + {horizon}-week forecast")
    st.pyplot(charts.forecast_chart(history, result, f"{scenario} scenario"))
    st.caption(
        f"The forecast starts after the last complete week of data "
        f"({history.week_start.iloc[-1]:%d %b %Y}). Data from 2020–2021 is depressed by "
        "COVID-19 and lower Yelp use; whether to forecast from the end of the data or show a "
        "typical pre-COVID year is a pending team decision."
    )
    if source:
        st.caption(f"Scenario effect source: {source} ({EVENTS[str(SCENARIOS[scenario])].label}, "
                   "default threshold).")  # fmt: skip

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Scenario comparison")
        rows = []
        base = forecast[forecast.week_offset == week].iloc[0]
        for label, event in SCENARIOS.items():
            pct, _ = scenario_effect(effects, area_id, event)
            factor = 1 + pct / 100
            rows.append(
                {
                    f"Scenario (in week W+{week})": label,
                    "Expected check-ins": round(base.yhat * factor),
                    "vs typical": "—" if event is None else _pct(pct),
                    "80% range": f"{base.lo80 * factor:.0f} – {base.hi80 * factor:.0f}",
                }
            )
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with right, st.container(border=True):
        st.subheader("What drives this forecast")
        drivers = queries.forecast_drivers(filters.metro, filters.categories, area_id)
        st.pyplot(charts.driver_bars(drivers))
        if effect:
            st.write(
                f"Week W+{week} is lower mainly because the {scenario.lower()} scenario "
                f"removes about {abs(impact):.0f} check-ins. Seasonality still sets the "
                "level of the other weeks."
            )
        else:
            st.write(
                "No clear weather disruption applies; the forecast follows the typical season."
            )

    with st.expander(f"Model accuracy (backtest, trained to {run.train_end:%Y-%m-%d})"):
        st.dataframe(
            pd.DataFrame(
                {
                    "Model": ["Baseline: same week last year", run.model_type],
                    "MAE (check-ins / wk)": [run.baseline_mae, run.mae],
                    "MAPE": [f"{run.baseline_mape:.1f}%", f"{run.mape:.1f}%"],
                }
            ),
            hide_index=True,
            width="stretch",
        )
