"""A compact, source-category competitor list for the location workspace."""

import csv
import io
from dataclasses import replace
from datetime import date

import psycopg
import streamlit as st

from sitesense import business_analysis as analysis
from sitesense import repository
from sitesense.database import ConfigurationError
from sitesense.models import Business, Catalog, Filters
from sitesense.ui import ScreenContext

RANK_METRICS = ("Recorded check-ins", "Check-ins / day")


def recorded_interval(catalog: Catalog, filters: Filters) -> tuple[date, date] | None:
    """Keep recorded activity inside both the selected dates and import coverage."""
    start = max(filters.start_date, catalog.coverage.start_date)
    end = min(filters.end_date, catalog.coverage.end_date)
    return (start, end) if start <= end else None


def scoped_businesses(
    catalog: Catalog, filters: Filters, area_id: int | None = None
) -> tuple[Business, ...]:
    """Use exact co-tags in the metro catalog; source city spelling is not its scope."""
    return tuple(
        business
        for business in catalog.businesses
        if filters.category in business.categories
        and (filters.source_category is None or filters.source_category in business.categories)
        and (area_id is None or business.area_id == area_id)
    )


def business_rows(
    catalog: Catalog,
    filters: Filters,
    records: tuple[analysis.BusinessActivity, ...] | None,
    area_id: int | None = None,
    metric: str = RANK_METRICS[0],
) -> list[dict[str, object]]:
    """Rank only covered recorded activity; missing history has null activity fields."""
    if metric not in RANK_METRICS:
        raise ValueError(f"Unknown location competitor metric: {metric}")
    businesses = scoped_businesses(catalog, filters, area_id)
    covered = recorded_interval(catalog, filters)
    by_id: dict[str, analysis.BusinessSummary] = {}
    if covered is not None and records is not None:
        ids = {business.business_id for business in businesses}
        summaries = analysis.rank_businesses(
            analysis.summarize_businesses(
                businesses,
                (record for record in records if record.business_id in ids),
                *covered,
            ),
            metric,
        )
        businesses = tuple(summary.business for summary in summaries)
        by_id = {summary.business.business_id: summary for summary in summaries}
    else:
        businesses = tuple(
            sorted(businesses, key=lambda business: (business.name, business.business_id))
        )
    return [
        {
            "Business": business.name,
            "ZIP": business.postal_code,
            "Recorded check-ins": by_id[business.business_id].checkin_count
            if business.business_id in by_id
            else None,
            "Check-ins / day": round(by_id[business.business_id].daily_checkins, 3)
            if business.business_id in by_id
            else None,
            "Rating (snapshot)": business.stars,
            "Reviews (snapshot)": business.review_count,
            "Operating status (snapshot)": "Open" if business.is_open else "Closed",
            "Source categories": ", ".join(business.categories),
            "Business ID": business.business_id,
        }
        for business in businesses
    ]


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _business_activity(filters: Filters, revision: str) -> tuple[analysis.BusinessActivity, ...]:
    return repository.load_business_activity(filters)


def _download_csv(
    rows: list[dict[str, object]], context: ScreenContext, area_id: int | None
) -> None:
    covered = recorded_interval(context.catalog, context.filters)
    scope: dict[str, object] = {
        "city": context.filters.city,
        "state": context.filters.state,
        "category": context.filters.category,
        "source_category": context.filters.source_category,
        "selected_start": context.filters.start_date,
        "selected_end": context.filters.end_date,
        "recorded_start": covered[0] if covered else None,
        "recorded_end": covered[1] if covered else None,
        "candidate_area_id": area_id,
        "data_source": "Imported Yelp archive; business snapshot; check-ins are an activity proxy",
    }
    exports = [dict(scope, **row) for row in rows]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(exports[0]))
    writer.writeheader()
    writer.writerows(exports)
    st.download_button(
        "Download businesses CSV",
        output.getvalue(),
        file_name="sitesense_location_businesses.csv",
        mime="text/csv",
        key="download_location_businesses",
        icon=":material/download:",
    )


def render(context: ScreenContext, area_id: int | None = None) -> None:
    """Display same-niche peers in one selected ZIP, or the metro without eligible ZIPs."""
    area = next((area for area in context.catalog.areas if area.id == area_id), None)
    location = f"ZIP {area.postal_code}" if area is not None else context.filters.city
    niche = context.filters.source_category or "All source categories"
    with st.container(border=True, key="location_businesses"):
        st.subheader(f"Top businesses in {location}")
        st.caption(f"{context.filters.category_label} → {niche}. Exact source-category membership.")
        if not scoped_businesses(context.catalog, context.filters, area_id):
            st.info(
                "No listed businesses match this category and source category in this location."
            )
            return
        covered = recorded_interval(context.catalog, context.filters)
        metric = st.selectbox(
            "Rank businesses by",
            RANK_METRICS,
            key="location_business_rank_metric",
            disabled=covered is None,
            persist_state="session",
        )
        records: tuple[analysis.BusinessActivity, ...] | None = None
        if covered is not None:
            try:
                records = _business_activity(
                    replace(context.filters, start_date=covered[0], end_date=covered[1]),
                    context.import_revision,
                )
            except (ConfigurationError, repository.DataUnavailableError, psycopg.Error):
                st.info("Business activity is unavailable. Snapshot listings remain available.")
        if covered is None:
            st.info(
                "No recorded business activity covers the selected dates. "
                "Snapshot listings are shown alphabetically; check-ins are unavailable."
            )
        elif records is not None:
            st.caption(
                f"Recorded activity: {covered[0]} to {covered[1]} · "
                f"{(covered[1] - covered[0]).days + 1:,} calendar days, "
                "including zero-activity days."
            )
            if covered != (context.filters.start_date, context.filters.end_date):
                st.caption("The ranking uses the covered portion of the selected dates only.")
        rows = business_rows(context.catalog, context.filters, records, area_id, str(metric))
        top = rows[:20]
        st.dataframe(
            top,
            hide_index=True,
            placeholder="—",
            key="location_business_table",
            column_config={
                "Recorded check-ins": st.column_config.NumberColumn(format="localized"),
                "Check-ins / day": st.column_config.NumberColumn(format="%.3f"),
                "Rating (snapshot)": st.column_config.NumberColumn(format="%.1f"),
                "Reviews (snapshot)": st.column_config.NumberColumn(format="localized"),
            },
            alt=f"Businesses in {location}, {niche}, with recorded activity and snapshot fields",
        )
        st.caption(
            f"Showing {len(top):,} of {len(rows):,} matching listings. "
            "Check-ins are an activity proxy. Ratings, reviews and operating status are archive "
            "snapshots. Source tags overlap; a listing's activity covers its whole business."
        )
        _download_csv(rows, context, area_id)
