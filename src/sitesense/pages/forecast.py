"""Page 4 · Typical year and weather scenarios (Features 4 and 6, US3).

No forecasting model is used yet: the "typical season" is the median weekly check-ins of
2016-2019 for each week of the year, and a scenario applies a published weather effect to one
week. A trained model can replace the typical line later without changing the scenario logic.
"""

import pandas as pd
import streamlit as st

from sitesense import analysis, charts, queries, scoring
from sitesense.components.notices import FORECAST, page_header
from sitesense.components.sidebar import current_filters
from sitesense.pages.weather import EVENT_NAMES, STATUS, default_level, effect_row

TYPICAL = "Typical season"


def _pct(value: float) -> str:
    return f"{value:+.0f}%".replace("-", "−")


def scenarios(
    thresholds: pd.DataFrame, effects: pd.DataFrame, area_id: str
) -> dict[str, tuple[float, str]]:
    """Scenario label -> (effect %, description) for events with a supported/partial estimate.

    Uses the area's own estimate when it is supported or partial, else the metro estimate.
    """
    options = {TYPICAL: (0.0, "")}
    for event in thresholds.event_type.drop_duplicates():
        level = default_level(thresholds, effects, str(event))
        if level is None:
            continue
        for scope in (area_id, None):
            row = effect_row(effects, scope, str(event), level)
            if row is not None and row.status in ("supported", "partial"):
                match = thresholds[
                    (thresholds.event_type == event) & (thresholds.threshold_level == level)
                ]
                where = "this area" if scope else "whole metro"
                verdict = STATUS[str(row.status)][0]
                note = f"{match.label.iloc[0]} · estimate for {where} · {verdict}"
                options[f"{EVENT_NAMES.get(str(event), str(event))} week"] = (
                    float(row.effect_pct),
                    note,
                )
                break
    return options


def render() -> None:
    filters = current_filters()
    page_header(
        "What does a normal year look like?",
        "Typical weekly check-ins for one area (2016–2019, before COVID-19), and how a weather "
        "event in one week changes them.",
        FORECAST,
    )
    areas = queries.areas(filters.metro, filters.category)
    factors = queries.area_factors(filters.metro, filters.category)
    names = {str(a.area_id): f"{a.name} ({a.area_id})" for a in areas.itertuples()}
    order = list(factors.sort_values("avg_weekly_checkins", ascending=False).area_id)
    effects = queries.weather_effects(filters.metro, filters.category)
    thresholds = queries.weather_thresholds(filters.metro)

    with st.container(border=True):
        top = st.columns(3)
        area_id = top[0].selectbox("Area", order, format_func=names.__getitem__)
        start_week = top[1].slider("Start week of the year", 1, 41, 1)
        horizon = 12
        week = top[2].select_slider(
            "Disruption week",
            options=list(range(start_week, start_week + horizon)),
            value=start_week + 5,
            format_func=lambda w: f"W{w}",
        )
        options = scenarios(thresholds, effects, area_id)
        scenario = st.radio(
            "Scenario", list(options), index=min(1, len(options) - 1), horizontal=True
        )

    typical = queries.typical_week(filters.metro, filters.category, area_id)
    window = typical[typical.week.between(start_week, start_week + horizon - 1)].reset_index(
        drop=True
    )
    if window.empty:
        st.warning("No typical-year data for this area.")
        st.stop()
    table = window.rename(columns={"median": "yhat", "p10": "lo80", "p90": "hi80"}).assign(
        week_offset=window.week
    )
    effect, note = options[scenario]
    result = scoring.scenario_forecast(table, effect, week)
    daily = analysis.fill_daily(
        queries.activity_daily(filters.metro, filters.category), [area_id],
        pd.Timestamp("2018-12-31").date(), pd.Timestamp("2019-12-29").date(),
    )  # fmt: skip
    weekly_2019 = (
        daily.set_index("obs_date").checkins.resample("W-MON", label="left", closed="left").sum()
    )
    weeks_2019 = pd.DatetimeIndex(weekly_2019.index).isocalendar().week.to_numpy()
    actual = pd.Series(weekly_2019.to_numpy(), index=weeks_2019)
    result["actual"] = result.week.map(actual)

    total, typical_total = result.scenario.sum(), result.yhat.sum()
    in_week = result[result.week == week].iloc[0]
    impact = in_week.scenario - in_week.yhat
    columns = st.columns(4)
    columns[0].metric(
        f"Check-ins, weeks W{start_week}–W{start_week + horizon - 1}",
        f"{total:,.0f}",
        f"{_pct((total / typical_total - 1) * 100)} vs typical" if typical_total else None,
        delta_color="normal" if total >= typical_total else "inverse",
    )
    columns[1].metric(
        "Weekly average",
        f"{result.scenario.mean():.0f}",
        f"range {result.scenario_lo.mean():.0f}–{result.scenario_hi.mean():.0f}",
        delta_color="off",
    )
    columns[2].metric(
        "Impact in disruption week",
        f"{impact:+.0f}".replace("-", "−"),
        f"{_pct(effect)} in week W{week}" if effect else "no disruption",
        delta_color="off" if not effect else "normal",
    )
    columns[3].metric("Years averaged", int(result.years.min()), "2016–2019", delta_color="off")

    st.subheader(f"Weekly check-ins · {names[area_id]}")
    st.pyplot(charts.typical_year_chart(result, f"{scenario} scenario", week))
    if note:
        st.caption(f"Scenario effect: {note}. See the Weather impact page for the evidence.")
    st.caption(
        "This is not a forecast model: the typical line is the median of the same weeks in "
        "2016–2019, before COVID-19 and the decline in Yelp check-ins. Choosing and evaluating "
        "a forecasting model is a pending team decision."
    )

    st.subheader(f"Scenario comparison (week W{week})")
    base = result[result.week == week].iloc[0]
    rows = [
        {
            "Scenario": label,
            "Expected check-ins": round(base.yhat * (1 + pct / 100)),
            "vs typical": "—" if label == TYPICAL else _pct(pct),
            "Range (p10–p90)": (
                f"{base.lo80 * (1 + pct / 100):.0f} – {base.hi80 * (1 + pct / 100):.0f}"
            ),
        }
        for label, (pct, _) in options.items()
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    if len(options) == 1:
        st.info("No weather event has a supported effect for this area and category, so only the "
                "typical season is shown.")  # fmt: skip
