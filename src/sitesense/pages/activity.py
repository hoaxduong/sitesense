"""Page 2 · Customer activity: when are customers active? (Feature 2)"""

import pandas as pd
import streamlit as st

from sitesense import analysis, charts, queries
from sitesense.components.notices import PROXY, YEARLY_USAGE, page_header
from sitesense.components.sidebar import current_filters

VIEWS = ("Hour × weekday", "Month", "Year")


def _pct(value: float) -> str:
    return f"{value:+.0%}".replace("-", "−")


def _main_chart(view: str, activity: pd.DataFrame, focus: str, selected: list[str],
                names: dict[str, str]) -> None:  # fmt: skip
    if view == "Month":
        days = analysis.daily(activity)
        days["month"] = days.obs_date.dt.month
        table = days.pivot_table(index="month", columns="area_id", values="checkins")
        table = (table * 7).reindex(index=range(1, 13), columns=selected)
        st.subheader("Average weekly check-ins by month")
        st.pyplot(
            charts.area_lines(
                table,
                [names[a] for a in selected],
                [m[0] for m in analysis.MONTHS],
                "Check-ins per week",
            )
        )
    elif view == "Year":
        days = analysis.daily(activity)
        days["year"] = days.obs_date.dt.year
        table = days.pivot_table(index="year", columns="area_id", values="checkins")
        table = (table * 7).reindex(columns=selected)
        st.subheader("Average weekly check-ins by year")
        st.pyplot(
            charts.area_lines(
                table,
                [names[a] for a in selected],
                [str(y) for y in table.index],
                "Check-ins per week",
            )
        )
        st.caption("Raw counts fall with Yelp check-in use; see the share chart below.")
    else:
        st.subheader(f"Check-ins by weekday and hour · {names[focus]}")
        st.caption("Hours 06–23, local time · darker = busier")
        st.pyplot(charts.weekday_hour_heatmap(analysis.weekday_hour(activity, focus)))


def render() -> None:
    filters = current_filters()
    page_header(
        "When are customers active?",
        "Yelp check-ins by hour, weekday, month and year, to compare demand across areas, "
        "days and seasons.",
        PROXY,
    )
    areas = queries.areas(filters.metro)
    names = {str(a.area_id): str(a.name) for a in areas.itertuples()}
    activity = queries.activity_hourly(filters.metro, filters.categories)
    activity = activity[
        (activity.obs_date >= pd.Timestamp(filters.start))
        & (activity.obs_date <= pd.Timestamp(filters.end))
    ]

    controls = st.columns([3, 1, 2])
    selected = controls[0].multiselect(
        "Areas to compare",
        list(names),
        default=list(names)[:3],
        max_selections=4,
        format_func=names.__getitem__,
    )
    if not selected:
        st.info("Pick at least one area to compare.")
        st.stop()
    focus = controls[1].selectbox("Focus area", selected, format_func=names.__getitem__)
    view = controls[2].segmented_control("View", VIEWS, default=VIEWS[0]) or VIEWS[0]

    chosen = activity[activity.area_id.isin(selected)]
    summary = analysis.summary(chosen, selected).set_index("area_id")
    if focus not in summary.index:
        st.warning("No check-ins for this area in the selected date range.")
        st.stop()
    row = summary[summary.index == focus].iloc[0]
    change = analysis.share_change(activity, focus)
    columns = st.columns(4)
    columns[0].metric(
        "Avg weekly check-ins",
        f"{row.avg_week:.0f}",
        None if change is None else f"{_pct(change)} metro share vs previous year",
    )
    columns[1].metric(
        "Busiest day", str(row["busiest_day"]), f"{row.busiest_day_share:.0%} of the week",
        delta_color="off",
    )  # fmt: skip
    columns[2].metric("Peak hour", str(row["peak_hour"]), "local time", delta_color="off")
    columns[3].metric(
        "High / low season", f"{row.season_ratio:.2f}×", f"{row.peak_month} vs {row.low_month}",
        delta_color="off",
    )  # fmt: skip

    with st.container(border=True):
        _main_chart(view, chosen, focus, selected, names)

    left, right = st.columns([3, 2], gap="large")
    with left, st.container(border=True):
        st.subheader("Seasonality · monthly index (year avg = 100)")
        index = analysis.monthly_index(chosen).reindex(columns=selected)
        st.pyplot(charts.index_lines(index, [names[a] for a in selected]))
    with right, st.container(border=True):
        st.subheader("Area share of metro check-ins")
        st.pyplot(charts.share_bars(analysis.share_by_year(activity, focus)))
        st.caption(f"{names[focus]}, % of all {filters.metro} check-ins in the selected "
                   "categories. Orange = 2020, COVID-19 lockdowns.")  # fmt: skip
    st.warning(YEARLY_USAGE, icon=":material/warning:")

    st.subheader("Activity summary by area")
    view_table = pd.DataFrame(
        {
            "Area": [names[a] for a in summary.index],
            "Avg / week": summary.avg_week.round(0).to_numpy(),
            "Weekday share": (summary.weekday_share * 100).to_numpy(),
            "Weekend share": (summary.weekend_share * 100).to_numpy(),
            "Busiest day": summary.busiest_day.to_numpy(),
            "Peak hour": summary.peak_hour.to_numpy(),
            "Peak month": summary.peak_month.to_numpy(),
            "High / low season": summary.season_ratio.to_numpy(),
        }
    )
    st.dataframe(
        view_table,
        hide_index=True,
        width="stretch",
        column_config={
            "Avg / week": st.column_config.NumberColumn(format="%d"),
            "Weekday share": st.column_config.NumberColumn(format="%.0f%%"),
            "Weekend share": st.column_config.NumberColumn(format="%.0f%%"),
            "High / low season": st.column_config.NumberColumn(format="%.2f×"),
        },
    )
    st.caption(
        "Hours are Philadelphia local time, converted on the assumption that Yelp check-in "
        "timestamps are UTC (strong evidence, pending team confirmation)."
    )
