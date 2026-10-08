"""Page 3 · Weather impact: how does weather change activity? (Feature 3, US2)"""

from typing import Literal

import pandas as pd
import streamlit as st

from sitesense import charts, queries, scoring
from sitesense.components.notices import ASSOCIATION, page_header
from sitesense.components.sidebar import current_filters

ALL_AREAS = "__all__"
EVENT_NAMES = {
    "heavy_rain": "Heavy rain/snow",
    "heat": "Heat",
    "cold": "Cold",
    "ice_day": "Ice day",
    "snow": "Snow",
}
BadgeColor = Literal["green", "orange", "gray"]
STATUS: dict[str, tuple[str, BadgeColor]] = {
    "supported": ("Supported", "green"),
    "partial": ("Partly supported", "orange"),
    "not_supported": ("No clear effect", "gray"),
    "insufficient": ("Too few days", "gray"),
    "not_available": ("No official threshold", "gray"),
}
SHOWN = ("supported", "partial", "not_supported")


def effect_row(
    effects: pd.DataFrame, area_id: str | None, event: str, level: str
) -> pd.Series | None:
    """The effect for one area (None = whole metro), event type and threshold level."""
    scope = effects.area_id.isna() if area_id is None else effects.area_id == area_id
    rows = effects[scope & (effects.event_type == event) & (effects.threshold_level == level)]
    return None if rows.empty else rows.iloc[0]


def default_level(thresholds: pd.DataFrame, effects: pd.DataFrame, event: str) -> str | None:
    """The level to show by default: best-supported metro estimate, else the first available."""
    levels = thresholds[(thresholds.event_type == event) & thresholds.available]
    if levels.empty:
        return None
    metro = effects[effects.area_id.isna() & (effects.event_type == event)]
    rank = {"supported": 0, "partial": 1, "not_supported": 2, "insufficient": 3}
    records = metro.to_dict("records")
    best = sorted(records, key=lambda r: (rank.get(str(r["status"]), 9), -int(r["n_events"])))
    return str(best[0]["threshold_level"]) if best else str(levels.threshold_level.iloc[0])


def _fmt_pct(value: float) -> str:
    return f"{value:+.1f}%".replace("-", "−")


def _p_text(p_value: float) -> str:
    return "p < 0.001" if p_value < 0.002 else f"p = {p_value:.3f}"


def _metrics(
    row: pd.Series | None, threshold: pd.Series, areas: pd.DataFrame, area_id: str | None
) -> None:
    columns = st.columns(4)
    status = "not_available" if row is None else str(row.status)
    text, color = STATUS[status]
    if row is None or pd.isna(row.effect_pct):
        columns[0].metric("Event days found", "—", "no estimate", delta_color="off")
        columns[1].metric("Change in check-ins", "—", delta_color="off")
    else:
        columns[0].metric("Event days found", int(row.n_events), "usable days, 2015 – Feb 2020",
                          delta_color="off")  # fmt: skip
        ci = (f"95% CI {_fmt_pct(row.ci_low)} to {_fmt_pct(row.ci_high)}"
              if pd.notna(row.ci_low) else "no interval (too few days)")  # fmt: skip
        columns[1].metric("Change in check-ins", _fmt_pct(row.effect_pct), ci, delta_color="off")
    with columns[2]:
        st.caption("Verdict")
        st.badge(text, color=color)
        if row is not None and pd.notna(row.p_value):
            st.caption(f"{_p_text(row.p_value)} · strict rule {_fmt_pct(row.effect_strict)}")
    scope = areas if area_id is None else areas[areas.area_id == area_id]
    columns[3].metric(
        "Distance to weather cell",
        f"{scope.weather_cell_km.median():.1f} km",
        f"{scope.weather_cell_id.nunique()} ERA5 cell(s), 0.25° grid",
        delta_color="off",
    )
    if threshold.days_per_year == threshold.days_per_year:
        st.caption(
            f"**Threshold:** {threshold.label} · source: "
            f"[{threshold.office}]({threshold.source_url}) ({threshold.source_status})"
            f" · about {threshold.days_per_year:.1f} days a year "
            f"({threshold.share_of_days:.1%} of days, 2010–2021)."
        )


def _effects_chart(effects: pd.DataFrame, thresholds: pd.DataFrame, area_id: str | None) -> None:
    rows, hidden = [], []
    for item in thresholds.itertuples():
        row = effect_row(effects, area_id, str(item.event_type), str(item.threshold_level))
        status = "not_available" if row is None else str(row.status)
        if row is None or status not in SHOWN:
            hidden.append(f"{item.label}: {STATUS[status][0].lower()}")
            continue
        rows.append(
            {
                **row.to_dict(),
                "label": f"{item.label}\nn = {int(row.n_events)} · {STATUS[status][0]}",
            }
        )
    st.subheader("Effect by event type")
    st.caption("Bar = average change vs normal days · line = 95% confidence interval")
    if rows:
        st.pyplot(charts.effect_bars(pd.DataFrame(rows)))
    if hidden:
        with st.expander(f"Not shown ({len(hidden)})"):
            st.markdown("\n".join(f"- {h}" for h in hidden))


def _sensitivity_table(
    effects: pd.DataFrame, thresholds: pd.DataFrame, areas: pd.DataFrame
) -> None:
    st.subheader("Weather sensitivity by area")
    events = [
        (e, lvl) for e in thresholds.event_type.drop_duplicates()
        if (lvl := default_level(thresholds, effects, str(e))) is not None
    ]  # fmt: skip
    records = []
    for area in areas.itertuples():
        record: dict[str, object] = {"Area": f"{area.name} · {area.area_id}"}
        heavy = None
        for event, level in events:
            row = effect_row(effects, str(area.area_id), str(event), level)
            name = EVENT_NAMES.get(str(event), str(event))
            if (
                row is None
                or row.status in ("insufficient", "not_available")
                or pd.isna(row.effect_pct)
            ):
                record[name] = "—"
                continue
            text = f"{row.effect_pct:+.0f}%".replace("-", "−")
            record[name] = text if row.status in ("supported", "partial") else f"({text})"
            if event == "heavy_rain":
                heavy = float(row.effect_pct)
        record["Sensitivity"] = "—" if heavy is None else scoring.sensitivity_label(heavy)
        record["Weather cell"] = area.weather_cell_id
        record["Distance"] = f"{area.weather_cell_km:.1f} km"
        records.append(record)
    st.dataframe(pd.DataFrame(records), hide_index=True, width="stretch")
    st.caption(
        "Each column uses the best-supported threshold of that event type. Values in brackets "
        "have a 95% CI that includes zero; “—” means too few event days. Most areas share one "
        "weather grid cell, so differences between areas reflect customer behaviour, not "
        "different weather."
    )


def render() -> None:
    filters = current_filters()
    page_header(
        "How does weather change activity?",
        "Check-ins on days that meet official weather thresholds, compared with the same "
        "weekday in the surrounding weeks.",
        ASSOCIATION,
    )
    areas = queries.areas(filters.metro, filters.category)
    effects = queries.weather_effects(filters.metro, filters.category)
    thresholds = queries.weather_thresholds(filters.metro)
    available = thresholds[thresholds.available]
    if available.empty:
        st.warning("No weather thresholds are published for this city.")
        st.stop()

    controls = st.columns(3)
    event_types = list(available.event_type.drop_duplicates())
    event = controls[0].selectbox(
        "Weather event", event_types, format_func=lambda e: EVENT_NAMES.get(e, e)
    )
    levels = available[available.event_type == event]
    labels = dict(zip(levels.threshold_level, levels.label, strict=True))
    preferred = default_level(thresholds, effects, event) or list(labels)[0]
    level = controls[1].selectbox(
        "Threshold",
        list(labels),
        index=list(labels).index(preferred),
        format_func=labels.__getitem__,
        key=f"level_{event}",
    )
    names = {ALL_AREAS: "All candidate areas"} | {
        str(a.area_id): f"{a.name} ({a.area_id})" for a in areas.itertuples()
    }
    choice = controls[2].selectbox("Area", list(names), format_func=names.__getitem__)
    area_id = None if choice == ALL_AREAS else choice

    threshold = levels[levels.threshold_level == level].iloc[0]
    row = effect_row(effects, area_id, event, level)
    _metrics(row, threshold, areas, area_id)

    left, right = st.columns(2, gap="large")
    with left:
        _effects_chart(effects, thresholds, area_id)
    with right:
        st.subheader("Temperature vs activity")
        st.caption("Each dot = one week (whole metro) · fitted quadratic trend")
        st.pyplot(charts.anomaly_scatter(queries.anomaly_response(filters.metro, filters.category)))

    where = "across all candidate areas" if area_id is None else f"in {names[choice]}"
    status = "not_available" if row is None else str(row.status)
    if status in ("supported", "partial") and row is not None:
        meaning = (
            f"On days that meet this threshold, {filters.category} check-ins {where} change by "
            f"about {_fmt_pct(row.effect_pct)} compared with similar normal days (95% CI "
            f"{_fmt_pct(row.ci_low)} to {_fmt_pct(row.ci_high)})."
        )
        if status == "partial":
            meaning += " The stricter baseline rule disagrees, so treat the size as uncertain."
    elif status == "not_supported" and row is not None:
        meaning = f"No clear change in check-ins {where} on these days (the 95% CI includes zero)."
    elif status == "insufficient" and row is not None:
        meaning = (
            f"Only {int(row.n_events)} usable event days in 2015 – Feb 2020, fewer than "
            f"{scoring.MIN_EVENTS}: the threshold is defined, but too rare to measure here."
        )
    else:
        meaning = "There is no official threshold for this event in this city."
    st.info(f"**What this means:** {meaning}", icon=":material/lightbulb:")

    _sensitivity_table(effects, thresholds, areas)
    with st.expander("How this is calculated"):
        st.markdown(
            "1. Thresholds come from the city's NWS forecast office or city government "
            "(see the source link above) and the WMO/ETCCDI rain indices; file "
            "`config/weather_thresholds.csv`.\n"
            "2. Daily weather is built in local time from hourly ERA5 data for the area's grid "
            "cell (0.25°, about 25 km). Heat index and wind chill follow the NWS formulas. "
            "Snowfall uses ERA5 snowfall water equivalent × 0.7 (cm).\n"
            "3. Each event day is compared with the same weekday in the ± 4 surrounding weeks. "
            "US federal holidays are excluded from both. Baseline days exclude other days of the "
            "same event type; the strict rule excludes days of any event type.\n"
            "4. Verdicts: *supported* = at least 20 days and the 95% CI excludes zero under both "
            "rules; *partly supported* = only the first rule; *too few days* = under 20.\n"
            "5. Results are associations, not causal effects. Check-in times are treated as UTC "
            "(pending team decision D7)."
        )
