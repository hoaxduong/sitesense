"""Page 3 · Weather impact: how does weather change activity? (Feature 3, US2)"""

from dataclasses import dataclass

import pandas as pd
import streamlit as st

from sitesense import charts, queries, scoring
from sitesense.components.notices import ASSOCIATION, page_header
from sitesense.components.sidebar import current_filters

ALL_AREAS = "__all__"


@dataclass(frozen=True)
class Event:
    label: str
    levels: dict[str, str]  # threshold_level -> label
    default: str


# Proposed definitions (decision #4, pending team confirmation).
EVENTS = {
    "heavy_rain": Event(
        "Heavy rain",
        {"moderate": "≥ 10 mm/day", "heavy": "≥ 20 mm/day", "very_heavy": "> wet-day p95"},
        "heavy",
    ),
    "hot_day": Event(
        "Hot day",
        {"local_p90": "max temp > local p90", "fixed_35": "max temp ≥ 35 °C"},
        "local_p90",
    ),  # fmt: skip
    "cold_day": Event(
        "Cold day",
        {"local_p10": "min temp < local p10", "ice_day": "max temp < 0 °C"},
        "local_p10",
    ),  # fmt: skip
    "snowfall": Event(
        "Snowfall", {"light": "≥ 2.5 cm (3.5 mm w.e.)", "heavy": "≥ 5 cm (7 mm w.e.)"}, "light"
    ),
}


def effect_row(
    effects: pd.DataFrame, area_id: str | None, event: str, level: str
) -> pd.Series | None:
    """The effect for one area (None = whole metro), event type and threshold level."""
    scope = effects.area_id.isna() if area_id is None else effects.area_id == area_id
    rows = effects[scope & (effects.event_type == event) & (effects.threshold_level == level)]
    return None if rows.empty else rows.iloc[0]


def _fmt_pct(value: float) -> str:
    return f"{value:+.1f}%".replace("-", "−")


def _p_text(p_value: float) -> str:
    return "p < 0.001" if p_value < 0.001 else f"p = {p_value:.3f}"


def _metrics(row: pd.Series, areas: pd.DataFrame, area_id: str | None) -> None:
    columns = st.columns(4)
    n = int(row.n_events)
    columns[0].metric("Event days found", n, "days, 2015–2021", delta_color="off")
    columns[1].metric(
        "Change in check-ins",
        _fmt_pct(row.effect_pct),
        f"95% CI {_fmt_pct(row.ci_low)} to {_fmt_pct(row.ci_high)}",
        delta_color="off",
    )
    if n < scoring.MIN_EVENTS:
        verdict, note = "Hidden", f"fewer than {scoring.MIN_EVENTS} event days"
    else:
        reliable = scoring.is_reliable(n, row.p_value)
        verdict, note = ("Yes" if reliable else "No"), f"{_p_text(row.p_value)} · n = {n}"
    columns[2].metric("Clear result?", verdict, note, delta_color="off")
    scope = areas if area_id is None else areas[areas.area_id == area_id]
    columns[3].metric(
        "Distance to weather cell",
        f"{scope.weather_cell_km.median():.1f} km",
        f"{scope.weather_cell_id.nunique()} ERA5 cell(s), 0.25° grid",
        delta_color="off",
    )


def _effects_by_type(effects: pd.DataFrame, area_id: str | None, chosen: dict[str, str]) -> None:
    rows, hidden = [], []
    for key, event in EVENTS.items():
        row = effect_row(effects, area_id, key, chosen.get(key, event.default))
        if row is None:
            continue
        label = f"{event.label} ({event.levels[row.threshold_level]})"
        if row.n_events < scoring.MIN_EVENTS:
            hidden.append(f"{label}, n = {row.n_events}")
            continue
        rows.append({**row.to_dict(), "label": f"{label}\nn = {row.n_events}"})
    st.subheader("Effect by event type")
    st.caption("Bar = average change vs normal days · line = 95% confidence interval")
    if rows:
        st.pyplot(charts.effect_bars(pd.DataFrame(rows)))
    if hidden:
        st.caption("Hidden (fewer than 20 event days): " + "; ".join(hidden))


def _sensitivity_table(effects: pd.DataFrame, areas: pd.DataFrame) -> None:
    st.subheader("Weather sensitivity by area")
    records = []
    for area in areas.itertuples():
        record: dict[str, object] = {"Area": f"{area.name} · {area.area_id}"}
        heavy = None
        for key, event in EVENTS.items():
            row = effect_row(effects, str(area.area_id), key, event.default)
            if row is None or row.n_events < scoring.MIN_EVENTS:
                record[event.label] = "—"
                continue
            text = f"{row.effect_pct:+.0f}%".replace("-", "−")
            clear = row.ci_low > 0 or row.ci_high < 0
            record[event.label] = text if clear else f"({text})"
            if key == "heavy_rain":
                heavy = float(row.effect_pct)
        record["Sensitivity"] = "—" if heavy is None else scoring.sensitivity_label(heavy)
        record["Weather cell"] = area.weather_cell_id
        record["Distance"] = f"{area.weather_cell_km:.1f} km"
        records.append(record)
    st.dataframe(pd.DataFrame(records), hide_index=True, width="stretch")
    st.caption(
        "Default thresholds per event. Values in brackets have a 95% CI that includes zero. "
        "Most areas share the same weather grid cell, so differences between areas come from "
        "customer behaviour, not from different weather. Per-area intervals are wide because "
        "each area has few check-ins per day."
    )


def render() -> None:
    filters = current_filters()
    page_header(
        "How does weather change activity?",
        "Check-ins on bad-weather days compared with similar normal days "
        "(same weekday, ± 4 weeks, excluding other event days).",
        ASSOCIATION,
    )
    areas = queries.areas(filters.metro)
    effects = queries.weather_effects(filters.metro, filters.categories)

    controls = st.columns(3)
    event_key = controls[0].selectbox(
        "Weather event", list(EVENTS), format_func=lambda key: EVENTS[key].label
    )
    event = EVENTS[event_key]
    level = controls[1].select_slider(
        "Threshold",
        options=list(event.levels),
        value=event.default,
        format_func=event.levels.__getitem__,
        key=f"level_{event_key}",
    )
    names = {ALL_AREAS: "All candidate areas"} | {
        str(a.area_id): f"{a.name} ({a.area_id})" for a in areas.itertuples()
    }
    choice = controls[2].selectbox("Area", list(names), format_func=names.__getitem__)
    area_id = None if choice == ALL_AREAS else choice
    if event_key == "snowfall":
        st.caption(
            "Snowfall is not in the current weather download yet; the team still has to add "
            "it or drop snow from the MVP."
        )

    row = effect_row(effects, area_id, event_key, level)
    if row is None:
        st.warning("No estimate for this event, threshold and area.")
        st.stop()
    _metrics(row, areas, area_id)

    left, right = st.columns(2, gap="large")
    with left:
        _effects_by_type(effects, area_id, {event_key: level})
    with right:
        st.subheader("Temperature vs activity")
        st.caption("Each dot = one week (whole metro) · fitted quadratic trend")
        st.pyplot(
            charts.anomaly_scatter(queries.anomaly_response(filters.metro, filters.categories))
        )

    where = "across all candidate areas" if area_id is None else f"in {names[choice]}"
    if row.n_events < scoring.MIN_EVENTS:
        meaning = (
            f"There are only {row.n_events} {event.label.lower()} days at this threshold, "
            f"fewer than {scoring.MIN_EVENTS}, so no estimate is shown in the comparison."
        )
    elif scoring.is_reliable(int(row.n_events), row.p_value):
        meaning = (
            f"On {event.label.lower()} days ({event.levels[level]}), check-ins {where} change "
            f"by about {_fmt_pct(row.effect_pct)} compared with similar normal days "
            f"(95% CI {_fmt_pct(row.ci_low)} to {_fmt_pct(row.ci_high)})."
        )
    else:
        meaning = (
            f"On {event.label.lower()} days ({event.levels[level]}), there is no clear change "
            f"in check-ins {where}: the 95% CI runs from {_fmt_pct(row.ci_low)} to "
            f"{_fmt_pct(row.ci_high)}."
        )
    st.info(f"**What this means:** {meaning}", icon=":material/lightbulb:")

    _sensitivity_table(effects, areas)
    with st.expander("How this is calculated"):
        st.markdown(
            "1. Each business is matched to its nearest ERA5 grid cell (0.25°, about 25 km); "
            "areas inherit the cell of their businesses.\n"
            "2. Daily weather is built in local time: precipitation is the sum of the hours, "
            "max/min/mean temperature come from hourly values. Percentile thresholds are per "
            "grid cell, using a ± 2-day calendar window (ETCCDI style).\n"
            "3. A day is an **event day** when it passes the chosen threshold. Thresholds are "
            "proposed and still need team confirmation.\n"
            "4. Event-day check-ins are compared with the same weekday in the ± 4 weeks "
            "around it, excluding other event days. Events with fewer than 20 days are hidden.\n"
            "5. The result is an association with a 95% confidence interval, not a causal "
            "effect."
        )
