"""Page 5 · Compare sites: candidate areas side by side (Feature 5)."""

import pandas as pd
import streamlit as st

from sitesense import analysis, charts, queries, scoring
from sitesense.components.notices import PROXY, page_header
from sitesense.components.sidebar import current_filters
from sitesense.pages.ranking import ranked_areas


def _badges(cards: pd.DataFrame) -> dict[str, str]:
    badges: dict[str, str] = {}
    for column, label in (
        ("score", "Best overall"),
        ("growth_yoy", "Fastest growing"),
        ("seasonality_ratio", "Most seasonal"),
    ):
        winner = str(cards.area_id.iloc[int(cards[column].to_numpy().argmax())])
        badges.setdefault(winner, label)
    return badges


def _review_trend(trends: pd.DataFrame, area_id: str) -> tuple[float, int, float, int, int]:
    rows = trends[trends.area_id == area_id].sort_values("year")
    first, last = rows.iloc[0], rows.iloc[-1]
    change = float(last.review_share / first.review_share - 1)
    return change, int(rows.new_businesses.sum()), float(last.avg_stars), first.year, last.year


def _card(row: pd.Series, badge: str | None, trend: float, peak: str) -> None:
    with st.container(border=True):
        top = st.columns([3, 2])
        top[0].markdown(f"**{row['name']}** · {row.area_id}")
        if badge:
            top[1].badge(badge, color="green")
        st.markdown(f"### {row.score:.0f} <small>score · rank #{row['rank']}</small>",
                    unsafe_allow_html=True)  # fmt: skip
        facts = {
            "Check-ins / week": f"{row.avg_weekly_checkins:.0f}",
            "Share trend (2019)": f"{row.growth_yoy:+.0%}".replace("-", "−"),
            "Peak season": peak,
            "Weather sensitivity": row.sensitivity,
            "Category trend (reviews)": f"{trend:+.0%}".replace("-", "−"),
            "Competitors in 1 km": f"{row.competitor_count}",
        }
        st.markdown("\n".join(f"- {label}: **{value}**" for label, value in facts.items()))


def render() -> None:
    filters = current_filters()
    page_header(
        "Compare candidate sites",
        "Pick 2–4 areas to compare demand, seasonality, weather sensitivity and category "
        "trends side by side. Scores use the weights from the Site ranking page.",
        PROXY,
    )
    table = ranked_areas(filters.metro, filters.categories)
    names = dict(zip(table.area_id, table.name, strict=True))
    top = st.columns([4, 1], vertical_alignment="bottom")
    selected = top[0].multiselect(
        "Sites to compare (max 4)",
        list(names),
        default=list(table.area_id[:3]),
        max_selections=4,
        format_func=names.__getitem__,
    )
    if len(selected) < 2:
        st.info("Pick at least two areas to compare.")
        st.stop()

    cards = table.set_index("area_id").loc[selected].reset_index()
    trends = queries.category_trends(filters.metro, filters.categories)
    activity = queries.activity_hourly(filters.metro, filters.categories)
    activity = activity[
        (activity.obs_date >= pd.Timestamp(filters.start))
        & (activity.obs_date <= pd.Timestamp(filters.end))
        & activity.area_id.isin(selected)
    ]
    index = analysis.monthly_index(activity).reindex(columns=selected)
    review = {a: _review_trend(trends, a) for a in selected}

    export = cards[["area_id", "name", "score", "rank", "avg_weekly_checkins", "growth_yoy",
                    "sensitivity", "competitor_count"]].assign(
        review_share_change=[review[a][0] for a in selected]
    )  # fmt: skip
    top[1].download_button(
        "Download comparison (CSV)",
        export.to_csv(index=False),
        file_name="sitesense_comparison.csv",
        mime="text/csv",
    )

    badges = _badges(cards)
    for column, (_, row) in zip(st.columns(len(selected)), cards.iterrows(), strict=True):
        with column:
            peak = analysis.peak_season(index[row.area_id]) if row.area_id in index else "—"
            _card(row, badges.get(str(row.area_id)), review[row.area_id][0], peak)

    with st.container(border=True):
        st.subheader("Score by dimension (0–100, higher is better)")
        dimensions = pd.DataFrame(
            {names[a]: [float(cards.set_index("area_id").loc[a, f.column]) * 100
                        for f in scoring.FACTORS] for a in selected},
            index=[f.label for f in scoring.FACTORS],
        )  # fmt: skip
        st.pyplot(charts.grouped_bars(dimensions))

    left, right = st.columns([3, 2], gap="large")
    with left, st.container(border=True):
        st.subheader("Seasonality · monthly index (year avg = 100)")
        st.pyplot(charts.index_lines(index, [names[a] for a in selected]))
    with right, st.container(border=True):
        first_year, last_year = review[selected[0]][3], review[selected[0]][4]
        st.subheader(f"{', '.join(filters.categories)} trend, {first_year} → {last_year}")
        st.dataframe(
            pd.DataFrame(
                {
                    "Area": [names[a] for a in selected],
                    "Reviews (share)": [f"{review[a][0]:+.0%}".replace("-", "−") for a in selected],
                    "New shops": [review[a][1] for a in selected],
                    "Avg ★": [review[a][2] for a in selected],
                }
            ),
            hide_index=True,
            width="stretch",
            column_config={"Avg ★": st.column_config.NumberColumn(format="%.1f")},
        )
        st.caption(
            "Review counts per year, normalised by the metro total. New shops = businesses "
            "whose first review or check-in falls in the period (Yelp has no opening dates)."
        )

    with st.container(border=True):
        st.caption("TAKEAWAY")
        st.write(scoring.compare_takeaway(cards))
