"""Source-category rankings, competitor research and business weather evidence."""

from dataclasses import replace
from statistics import median
from typing import cast

import pandas as pd  # type: ignore[import-untyped]
import psycopg
import streamlit as st

from sitesense import business_analysis as analysis
from sitesense import repository
from sitesense.database import ConfigurationError
from sitesense.models import Filters
from sitesense.ui import ScreenContext, render_setup


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _business_activity(filters: Filters, revision: str) -> tuple[analysis.BusinessActivity, ...]:
    return repository.load_business_activity(filters)


@st.cache_data(ttl=300, max_entries=64, show_spinner=False)
def _business_monthly(
    filters: Filters, business_ids: tuple[str, ...], revision: str
) -> tuple[analysis.BusinessMonth, ...]:
    return repository.load_business_monthly(filters, business_ids)


def _valid_selection(key: str, options: list[str], default: list[str]) -> None:
    if key not in st.session_state:
        st.session_state[key] = default
    else:
        st.session_state[key] = [value for value in st.session_state[key] if value in options]


def _comparison_csv(table: pd.DataFrame, filters: Filters, source: str = "") -> str:
    return str(
        table.assign(
            **{
                "City": filters.city,
                "State": filters.state,
                "Broad category": filters.category,
                "Selected source category": source,
                "Period start": filters.start_date,
                "Period end": filters.end_date,
            }
        ).to_csv(index=False)
    )


def _category_comparison(summaries: tuple[analysis.BusinessSummary, ...], filters: Filters) -> None:
    st.subheader("Top source categories")
    st.caption("**WBS: US1 · T013 · Check-in totals · T014 · Activity ranking**")
    st.caption(f"Across {filters.category_label}, including alternatives to the selected niche.")
    tags = analysis.subcategory_groups(filters.category, tuple(row.business for row in summaries))[
        "Source categories"
    ]
    if not tags:
        st.info("No additional source categories are available in this snapshot.")
        return
    with st.container(horizontal=True, vertical_alignment="bottom", gap="small"):
        minimum = st.number_input(
            "Minimum businesses",
            min_value=1,
            value=1,
            step=1,
            key="business_category_minimum",
            persist_state="session",
            help=(
                "Minimum listed businesses per source category. Raise this to inspect "
                "rankings beyond very small groups; this is not a significance test."
            ),
            width=200,
        )
        limit = st.selectbox(
            "Categories shown",
            (10, 20, 50),
            key="business_category_limit",
            persist_state="session",
            width=160,
        )
    all_ranked = analysis.rank_categories(summaries, tags)
    ranked = tuple(row for row in all_ranked if row.business_count >= minimum)
    if not ranked:
        st.info(
            f"No source categories have at least {minimum:,} listed businesses in this scope. "
            "Lower the minimum to inspect smaller groups."
        )
        return
    table = pd.DataFrame(
        [
            {
                "Rank": index,
                "Source category": row.category,
                "Recorded check-ins": row.checkin_count,
                "Businesses": row.business_count,
                "With check-ins": row.active_business_count,
                "Largest business share (%)": round(row.top_business_share * 100, 1)
                if row.top_business_share is not None
                else None,
                "Data note": "Few businesses (<5)"
                if row.business_count < 5
                else "Few check-ins (<20)"
                if row.checkin_count < 20
                else "Descriptive comparison",
            }
            for index, row in enumerate(ranked, 1)
        ]
    )
    displayed = table.head(limit)
    st.dataframe(
        displayed, hide_index=True, alt="Ranked source categories with activity and sample sizes"
    )
    st.bar_chart(
        displayed,
        x="Source category",
        y="Recorded check-ins",
        horizontal=True,
        sort=False,
        alt="Top source categories ranked by recorded check-ins",
    )
    st.caption(
        f"Showing {len(displayed):,} of {len(ranked):,} eligible source categories "
        f"({len(all_ranked):,} before the minimum-business filter)."
    )
    st.download_button(
        "Download source category rankings",
        _comparison_csv(table.assign(**{"Minimum businesses": minimum}), filters),
        file_name="source_category_rankings.csv",
        mime="text/csv",
    )


def _business_label(row: analysis.BusinessSummary) -> str:
    business = row.business
    return f"{business.name} · {business.business_id[-6:]}"


def _business_row(row: analysis.BusinessSummary) -> dict[str, object]:
    business = row.business
    return {
        "Business": business.name,
        "Recorded check-ins": row.checkin_count,
        "Days with check-ins": row.active_days,
        "Weekend share (%)": round(row.weekend_share * 100, 1)
        if row.weekend_share is not None
        else None,
        "Rating (snapshot)": business.stars,
        "Reviews (snapshot)": business.review_count,
        "Operating status (snapshot)": "Open" if business.is_open else "Closed",
        "Source categories": ", ".join(business.categories),
        "Business ID": business.business_id,
    }


def _selected_business_comparison(
    peers: tuple[analysis.BusinessSummary, ...],
    matches: list[analysis.BusinessSummary],
    filters: Filters,
    revision: str,
    source: str,
) -> None:
    by_id = {row.business.business_id: row for row in peers}
    scope = (filters.city, filters.category, source)
    if st.session_state.get("business_peer_scope") != scope:
        st.session_state["business_picks"] = [row.business.business_id for row in peers[:2]]
        st.session_state["business_peer_scope"] = scope
    _valid_selection("business_picks", list(by_id), [])
    options = list(
        dict.fromkeys(
            [
                *st.session_state["business_picks"],
                *(row.business.business_id for row in matches[:100]),
            ]
        )
    )
    selected = st.multiselect(
        "Businesses to compare (up to 4)",
        options,
        key="business_picks",
        max_selections=4,
        persist_state="session",
        format_func=lambda value: _business_label(by_id[value]),
    )
    if not selected:
        st.info("Choose businesses for a comparison, or investigate one business below.")
        return
    comparison = pd.DataFrame([_business_row(by_id[value]) for value in selected])
    st.dataframe(comparison, hide_index=True, alt="Side-by-side competitor comparison")
    monthly = _business_monthly(filters, tuple(sorted(selected)), revision)
    from sitesense.app import _import_is_unchanged

    if not _import_is_unchanged(revision):
        return
    rates = analysis.monthly_rates(monthly, selected, filters.start_date, filters.end_date)
    chart = pd.DataFrame(
        [
            {
                "Month": row.month_start,
                "Business": _business_label(by_id[row.business_id]),
                "Recorded check-ins": row.checkin_count,
            }
            for row in rates
        ]
    )
    st.line_chart(
        chart,
        x="Month",
        y="Recorded check-ins",
        color="Business",
        alt="Monthly recorded check-ins for the selected competitors",
    )
    st.download_button(
        "Download business comparison",
        _comparison_csv(comparison, filters, source),
        file_name="business_comparison.csv",
        mime="text/csv",
    )


def _business_comparison(
    context: ScreenContext,
    summaries: tuple[analysis.BusinessSummary, ...],
    filters: Filters,
    source: str,
) -> None:
    st.subheader("Top businesses and competitors")
    st.caption("**WBS: US1 · T014 · Activity ranking · T017 · Competitor comparison**")
    if not summaries:
        st.info("No businesses match this source category.")
        return
    peers = analysis.rank_businesses(summaries)
    if not peers:
        st.info("No businesses match this source category.")
        return
    st.caption(
        f"{len(peers):,} businesses · median {median(row.checkin_count for row in peers):g} "
        "recorded check-ins per business."
    )
    if len(peers) < 5:
        st.warning("This group has fewer than five businesses. Comparisons are descriptive.")
    query = (
        st.text_input(
            "Find a business by name",
            key="business_search",
            persist_state="session",
            help="Search narrows the list without changing the industry benchmark.",
            width=360,
        )
        .strip()
        .casefold()
    )
    matches = [row for row in peers if not query or query in row.business.name.casefold()]
    ranks = {row.business.business_id: index for index, row in enumerate(peers, 1)}
    st.caption(
        f"Showing {min(len(matches), 100):,} of {len(matches):,} matches, "
        "ordered by Recorded check-ins."
    )
    table = pd.DataFrame(
        [{"Rank": ranks[row.business.business_id], **_business_row(row)} for row in matches[:100]]
    )
    st.dataframe(
        table,
        hide_index=True,
        alt="Top businesses in the selected industry ranked by recorded check-ins",
    )
    _selected_business_comparison(peers, matches, filters, context.import_revision, source)
    by_id = {row.business.business_id: row for row in peers}
    focus = st.session_state.get("business_focus")
    if focus not in by_id:
        st.session_state["business_focus"] = peers[0].business.business_id
    focus_options = list(
        dict.fromkeys(
            [
                st.session_state["business_focus"],
                *st.session_state.get("business_picks", []),
                *(row.business.business_id for row in matches[:100]),
            ]
        )
    )
    focused_id = st.selectbox(
        "Business to investigate",
        focus_options,
        key="business_focus",
        persist_state="session",
        format_func=lambda value: _business_label(by_id[value]),
        width=500,
    )
    business = by_id[focused_id].business
    from sitesense.business_weather_views import render_business_weather

    render_business_weather(context, business, filters)


def render_business_analysis() -> None:
    context = cast(ScreenContext, st.session_state["screen_context"])
    filters, catalog = context.filters, context.catalog
    source = str(st.session_state.get("business_source_category", "All source categories"))
    start = max(filters.start_date, catalog.coverage.start_date)
    end = min(filters.end_date, catalog.coverage.end_date)
    if start > end:
        st.info(
            "No recorded activity coverage for these dates. "
            "Choose dates within the available history."
        )
        return
    if (start, end) != (filters.start_date, filters.end_date):
        st.warning("Only dates inside the recorded history are included in business comparisons.")
    st.caption(
        f"Effective comparison dates: {start:%Y-%m-%d} to {end:%Y-%m-%d} · "
        f"{(end - start).days + 1:,} calendar days."
    )
    effective = replace(filters, start_date=start, end_date=end)
    try:
        records = _business_activity(
            replace(effective, source_category=None), context.import_revision
        )
        from sitesense.app import _import_is_unchanged

        if not _import_is_unchanged(context.import_revision):
            return
        summaries = analysis.summarize_businesses(catalog.businesses, records, start, end)
        if not summaries:
            st.info("No businesses are available in this category and city.")
            return
        scope = (filters.city, filters.category, source)
        if st.session_state.get("business_category_scope") != scope:
            for key in ("business_search", "business_zip", "business_picks", "business_focus"):
                st.session_state.pop(key, None)
            st.session_state["business_category_scope"] = scope
        selected = tuple(
            row
            for row in summaries
            if source == "All source categories" or source in row.business.categories
        )
        with st.container(horizontal=True):
            st.metric(
                "Recorded check-ins", f"{sum(row.checkin_count for row in selected):,}", border=True
            )
            st.metric("Listed businesses", f"{len(selected):,}", border=True)
            st.metric(
                "With recorded activity",
                f"{sum(row.checkin_count > 0 for row in selected):,}",
                border=True,
            )
        _category_comparison(summaries, effective)
        _business_comparison(context, selected, effective, source)
        show_ranking = st.query_params.get("wbs") in ("T015", "T018")
        with st.expander("Ranking details", expanded=show_ranking):
            if show_ranking:
                st.subheader("Ranking details", anchor="ranking-details")
            st.caption("**WBS: US1 · T015 · Ranking explanation · T018 · Explanation interface**")
            st.caption(
                "Rankings use recorded Yelp check-ins, not customers or revenue, and cover "
                "the imported city cohort rather than the complete metropolitan market. "
                "Listings with zero recorded activity or missing coordinates remain included."
            )
            st.caption(
                "Source categories use exact archive labels and may share businesses, so "
                "their totals are not additive. Read sample sizes and largest-business "
                "shares when comparing small groups."
            )
            st.caption(
                "Ratings, reviews, status and categories describe the archive snapshot. "
                "Check-ins belong to the whole listing, including listings with several "
                "business types. Historical opening dates are unavailable."
            )
            st.caption(
                "Monthly activity totals use selected covered dates, including partial "
                "months and recorded zeros. Zero check-ins do not establish opening status."
            )
    except ConfigurationError:
        render_setup("configuration")
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn):
        render_setup("migration")
    except repository.DataUnavailableError:
        render_setup("import")
    except psycopg.Error:
        render_setup("connection")
