"""Source-category business locations and recorded activity, without area aggregation."""

import csv
import io
import math
from dataclasses import replace
from functools import partial
from typing import Any, cast

import psycopg
import pydeck as pdk  # type: ignore[import-untyped]
import streamlit as st

from sitesense import (
    business_activity,
    business_tooltip,
    business_weather_layer,
    location_businesses,
    repository,
)
from sitesense import business_analysis as analysis
from sitesense.database import ConfigurationError
from sitesense.models import Filters
from sitesense.ui import ScreenContext
from sitesense.weather_map import _fit_view


@st.cache_data(ttl=300, max_entries=32, show_spinner=False)
def _business_activity(filters: Filters, revision: str) -> tuple[analysis.BusinessActivity, ...]:
    return repository.load_business_activity(filters)


def business_rows(
    context: ScreenContext,
    records: tuple[analysis.BusinessActivity, ...] | None,
    projection: business_activity.ProjectionResult | None = None,
) -> list[dict[str, object]]:
    """Keep every matching listing; unavailable activity remains null rather than zero."""
    rows = location_businesses.business_rows(context.catalog, context.filters, records)
    by_id = {business.business_id: business for business in context.catalog.businesses}
    for rank, row in enumerate(rows, 1):
        business = by_id[str(row["Business ID"])]
        row.pop("ZIP", None)
        row.pop("Check-ins / day", None)
        row.update(
            {
                "Rank": rank
                if records is not None and row["Recorded check-ins"] is not None
                else None,
                "Latitude": business.latitude,
                "Longitude": business.longitude,
            }
        )
    if projection is not None and projection.interval is not None:
        predicted = {row.group.business_id: row.period_rate for row in projection.projections}
        recorded_required = location_businesses.recorded_interval(context.catalog, context.filters)
        for row in rows:
            count = row["Recorded check-ins"]
            estimate = predicted.get(str(row["Business ID"]))
            row["Projected check-ins"] = estimate
            row["Recorded + projected check-ins"] = (
                (float(cast(int, count)) if recorded_required else 0.0) + estimate
                if estimate is not None and (not recorded_required or count is not None)
                else None
            )
        rows.sort(
            key=lambda row: (
                row["Recorded + projected check-ins"] is None,
                -float(cast(float, row["Recorded + projected check-ins"]) or 0)
                if row["Recorded + projected check-ins"] is not None
                else 0,
                str(row["Business"]),
                str(row["Business ID"]),
            )
        )
        for rank, row in enumerate(rows, 1):
            row["Rank"] = rank if row["Recorded + projected check-ins"] is not None else None
    return rows


def map_points(rows: list[dict[str, object]], selected_id: str | None) -> list[dict[str, Any]]:
    """Use each listing's own valid coordinates, retaining true zero activity pins."""
    largest = max(
        (
            float(cast(float, row.get("Recorded + projected check-ins", row["Recorded check-ins"])))
            for row in rows
            if row.get("Recorded + projected check-ins", row["Recorded check-ins"]) is not None
        ),
        default=0,
    )
    points = []
    for row in rows:
        latitude, longitude = row["Latitude"], row["Longitude"]
        if (
            not isinstance(latitude, int | float)
            or not isinstance(longitude, int | float)
            or not math.isfinite(latitude)
            or not math.isfinite(longitude)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
        ):
            continue
        count = cast(int | None, row["Recorded check-ins"])
        estimated = cast(float | None, row.get("Projected check-ins"))
        activity = cast(
            float | None, row.get("Recorded + projected check-ins", row["Recorded check-ins"])
        )
        selected = row["Business ID"] == selected_id
        points.append(
            {
                "business_id": row["Business ID"],
                "business_name": row["Business"],
                "latitude": latitude,
                "longitude": longitude,
                "checkins_text": f"{int(count):,}" if count is not None else "Unavailable",
                "projected_text": f"{estimated:,.2f}" if estimated is not None else "Unavailable",
                "radius": max(4.0, 23 * math.sqrt(float(activity) / largest))
                if activity is not None and largest
                else 4.0,
                "fill_color": (
                    [113, 85, 138, 180] if "Projected check-ins" in row else [38, 116, 90, 180]
                )
                if activity is not None
                else [128, 135, 132, 170],
                "line_color": [25, 47, 41, 255] if selected else [255, 255, 255, 220],
                "line_width": 3 if selected else 1,
            }
        )
    return points


def _prepare_focus(context: ScreenContext, ids: tuple[str, ...]) -> None:
    scope = (
        context.filters.city,
        context.filters.state,
        context.filters.category,
        context.filters.source_category,
    )
    if st.session_state.get("business_map_scope") != scope:
        st.session_state["business_map_scope"] = scope
        st.session_state.pop("map_business", None)
        st.session_state.pop("business_map_search", None)
    st.session_state["business_map_eligible_ids"] = ids
    pending = st.session_state.pop("pending_map_business", None)
    if pending in ids:
        st.session_state["map_business"] = pending
    if st.session_state.get("map_business") not in ids:
        st.session_state["map_business"] = ids[0] if ids else None


def _select_map_business() -> None:
    state = st.session_state.get("business_map_deck", {})
    objects = state.get("selection", {}).get("objects", {}).get("business_locations", [])
    if objects:
        identifier = objects[0].get("business_id")
        if identifier in st.session_state.get("business_map_eligible_ids", ()):
            st.session_state["map_business"] = identifier


def _select_table_business(ids: tuple[str, ...]) -> None:
    """Translate the rendered table's checkboxes to stable comparison business IDs."""
    rows = st.session_state.get("business_map_table", {}).get("selection", {}).get("rows", [])
    eligible = st.session_state.get("business_map_eligible_ids", ())
    checked = tuple(
        dict.fromkeys(
            ids[row]
            for row in rows
            if isinstance(row, int)
            and not isinstance(row, bool)
            and 0 <= row < len(ids)
            and ids[row] in eligible
        )
    )
    selected = [
        identifier
        for identifier in st.session_state.get("business_compare_ids", ())
        if identifier in eligible and (identifier not in ids or identifier in checked)
    ]
    added = [identifier for identifier in checked if identifier not in selected]
    combined = list(dict.fromkeys((*selected, *added)))
    selected = combined[:4]
    st.session_state["business_compare_ids"] = selected
    accepted = [identifier for identifier in added if identifier in selected]
    if accepted:
        st.session_state["business_compare_focus"] = accepted[-1]
        st.session_state["map_business"] = accepted[-1]
    elif st.session_state.get("business_compare_focus") not in selected:
        st.session_state.pop("business_compare_focus", None)
    if len(combined) > 4:
        st.toast("Compare up to four businesses. Uncheck one before adding another.")
    _sync_table_selection(ids)


def _sync_table_selection(ids: tuple[str, ...]) -> None:
    """Rebuild visible checks from IDs whenever filtering or activity changes row order."""
    selected = st.session_state.get("business_compare_ids", ())
    rows = [index for index, identifier in enumerate(ids) if identifier in selected]
    current = st.session_state.get("business_map_table", {}).get("selection", {}).get("rows", [])
    if current != rows or "business_map_table" not in st.session_state:
        st.session_state["business_map_table"] = {"selection": {"rows": rows}}


def _open_selected_comparison() -> None:
    from sitesense import business_comparison

    selected = business_comparison.get_comparison_ids()
    if not selected:
        return
    focused = st.session_state.get("business_compare_focus")
    st.session_state["pending_comparison_business"] = (
        focused if focused in selected else selected[-1]
    )
    business_comparison.open_comparison()


def business_csv(
    rows: list[dict[str, object]],
    context: ScreenContext,
    projection: business_activity.ProjectionResult | None = None,
) -> str:
    covered = location_businesses.recorded_interval(context.catalog, context.filters)
    selected_days = (context.filters.end_date - context.filters.start_date).days + 1
    recorded_days = (covered[1] - covered[0]).days + 1 if covered else 0
    scope: dict[str, object] = {
        "city": context.filters.city,
        "state": context.filters.state,
        "category": context.filters.category,
        "source_category": context.filters.source_category or "All source categories",
        "selected_start": context.filters.start_date,
        "selected_end": context.filters.end_date,
        "recorded_start": covered[0] if covered else None,
        "recorded_end": covered[1] if covered else None,
        "covered_calendar_days": recorded_days,
        "unavailable_calendar_days": selected_days - recorded_days,
        "timestamp_policy": context.catalog.coverage.timestamp_policy,
        "import_revision": context.import_revision,
        "data_source": "Imported Yelp archive; snapshot listings; check-ins are an activity proxy",
    }
    if projection is not None and projection.interval is not None:
        scope.update(
            {
                "projected_start": projection.interval[0],
                "projected_end": projection.interval[1],
                "model_revision": projection.model_revision,
                "estimate_kind": "individual_business_calendar_projection",
            }
        )
    exports = [dict(scope, **row) for row in rows]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(exports[0]) if exports else list(scope))
    writer.writeheader()
    writer.writerows(exports)
    return output.getvalue()


def _render_map(
    rows: list[dict[str, object]],
    context: ScreenContext | None = None,
    projection: business_activity.ProjectionResult | None = None,
) -> None:
    points = map_points(rows, st.session_state.get("map_business"))
    missing_coordinates = len(rows) - len(points)
    st.subheader("Business locations")
    projected = projection is not None and projection.interval is not None
    if points:
        with st.container(horizontal=True, gap="small", vertical_alignment="top"):
            limit = st.selectbox(
                "Top businesses on map",
                (10, 20, 50, 100),
                index=1,
                key="business_map_limit",
                help=(
                    "Ranked by recorded + projected check-ins when both portions are available. "
                    "The table and download retain all listings."
                    if projected
                    else "Ranked by Recorded check-ins. The table and download retain all listings."
                ),
                persist_state="session",
                width=180,
            )
            with st.container(width=320, gap="xsmall"):
                weather = (
                    business_weather_layer.prepare(
                        context,
                        tuple(str(row["Business ID"]) for row in rows),
                        context.filters.start_date,
                        context.filters.end_date,
                        "business_map_weather",
                    )
                    if context is not None
                    else None
                )
        mapped_count = len(points)
        points = points[:limit]
        show_weather = weather is not None and weather.has_values
        layers = []
        if show_weather:
            points = business_weather_layer.annotate(points, weather)
        covered = (
            location_businesses.recorded_interval(context.catalog, context.filters)
            if context is not None
            else None
        )
        for point in points:
            point.update(
                business_tooltip.fields(
                    str(point["business_name"]),
                    str(point["projected_text"])
                    if projected and covered is None
                    else str(point["checkins_text"]),
                    period=business_tooltip.period_label(*covered)
                    if covered
                    else business_tooltip.period_label(*projection.interval)
                    if projection is not None and projection.interval is not None
                    else None,
                    projected=projected and covered is None,
                    projected_count=str(point["projected_text"])
                    if projected and covered is not None
                    else None,
                    projected_period=business_tooltip.period_label(*projection.interval)
                    if projection is not None and projection.interval is not None
                    else None,
                    weather=point if show_weather else None,
                    weather_period=business_tooltip.period_label(
                        weather.start_date, weather.end_date
                    )
                    if show_weather and weather is not None
                    else None,
                )
            )
        if weather is not None and weather.features:
            layers.append(
                pdk.Layer(
                    "GeoJsonLayer",
                    id="business_weather",
                    data={"type": "FeatureCollection", "features": weather.features},
                    get_fill_color="properties.weather_color",
                    filled=True,
                    stroked=True,
                    get_line_color=[255, 255, 255, 90],
                    get_line_width=1,
                    line_width_units="'pixels'",
                    pickable=False,
                )
            )
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                id="business_locations",
                data=points,
                get_position="[longitude, latitude]",
                get_radius="radius",
                # Pydeck treats unquoted strings as data accessors.
                radius_units="'pixels'",
                get_fill_color="fill_color",
                get_line_color="line_color",
                get_line_width="line_width",
                line_width_units="'pixels'",
                stroked=True,
                pickable=True,
                auto_highlight=True,
            )
        )
        deck = pdk.Deck(
            map_provider="carto",
            map_style=pdk.map_styles.CARTO_LIGHT,
            initial_view_state=_fit_view([[row["longitude"], row["latitude"]] for row in points]),
            layers=layers,
            tooltip=business_tooltip.TOOLTIP,
        )
        # Streamlit omits the deck.gl widget-root positioning stylesheet.
        st.html(
            "<style>.st-key-business_map_view .deck-widgets-root "
            "{position:absolute;inset:0;pointer-events:none}</style>"
        )
        with st.container(key="business_map_view"):
            st.pydeck_chart(
                deck,
                height=470,
                key="business_map_deck",
                on_select=_select_map_business,
                selection_mode="single-object",
                alt="Individual businesses sized by activity above mapped-station weather",
            )
        if show_weather and weather is not None:
            business_weather_layer.render_legend(weather)
        st.caption(
            f"Top {len(points):,} of {mapped_count:,} mapped businesses · "
            + (
                "Ranked by recorded + projected check-ins. "
                if projected
                else "Ranked by Recorded check-ins. "
            )
            + "Check table rows to compare businesses."
        )
        if not any(
            point["business_id"] == st.session_state.get("map_business") for point in points
        ):
            st.caption(
                "The selected business is outside the current top businesses on the map. "
                "Use Compare businesses to view its details."
            )
    else:
        st.info("No matching businesses have usable coordinates. All listings remain in the table.")
    if missing_coordinates:
        st.caption(
            f"{missing_coordinates:,} matching listings have no usable coordinates; "
            "they remain available in the rankings and download."
        )


def _render_recorded(context: ScreenContext) -> None:
    from sitesense import business_comparison

    filters = context.filters
    businesses = business_comparison.prepare(context)
    st.session_state.setdefault("business_compare_ids", [])
    if not businesses:
        st.info("No listed businesses match this category and source category in this city.")
        return
    covered = location_businesses.recorded_interval(context.catalog, filters)
    records: tuple[analysis.BusinessActivity, ...] | None = None
    if covered is not None:
        try:
            effective = replace(filters, start_date=covered[0], end_date=covered[1])
            records = _business_activity(
                replace(effective, source_category=None), context.import_revision
            )
            from sitesense.app import _import_is_unchanged

            if not _import_is_unchanged(context.import_revision):
                return
        except (ConfigurationError, repository.DataUnavailableError, psycopg.Error):
            st.info("Business activity is unavailable. Snapshot listings remain available.")
    else:
        st.info(
            "No recorded business activity covers the selected dates. Snapshot listings are "
            "shown with unavailable recorded check-ins."
        )
    projection = business_activity.load_projection(context)
    if projection.issue:
        st.info(projection.issue)
    rows = business_rows(context, records, projection)
    ids = tuple(str(row["Business ID"]) for row in rows)
    _prepare_focus(context, ids)
    total = (
        sum(cast(int, row["Recorded check-ins"]) for row in rows) if records is not None else None
    )
    with st.container(horizontal=True):
        st.metric("Matching businesses", f"{len(rows):,}", border=True)
        st.metric(
            "Recorded check-ins", f"{total:,}" if total is not None else "Unavailable", border=True
        )
        if projection.interval is not None:
            st.metric(
                "Projected check-ins",
                f"{sum(row.period_rate for row in projection.projections):,.2f}"
                if projection.projections
                else "Unavailable",
                border=True,
            )
    if projection.interval is not None:
        intervals = (
            f"Recorded: {covered[0]:%d %b %Y}–{covered[1]:%d %b %Y} · "
            if covered is not None
            else ""
        )
        st.caption(
            intervals + f"Projected: {projection.interval[0]:%d %b %Y}–"
            f"{projection.interval[1]:%d %b %Y} · "
            f"{len(projection.projections):,}/{len(rows):,} businesses modeled; others unavailable."
        )
    elif covered is not None and covered != (filters.start_date, filters.end_date):
        st.caption(
            f"Recorded: {covered[0]:%d %b %Y}–{covered[1]:%d %b %Y}; other dates unavailable."
        )
    _render_map(rows, context, projection)
    st.subheader("Top businesses in the selected niche")
    with st.container(horizontal=True, gap="small", vertical_alignment="bottom"):
        search = st.text_input(
            "Search business names", key="business_map_search", persist_state="session", width=320
        )
        limit = st.selectbox(
            "Businesses shown",
            (20, 50, 100),
            key="business_table_limit",
            persist_state="session",
            width=150,
        )
        comparison_ids = business_comparison.get_comparison_ids()
        st.button(
            f"Compare businesses ({len(comparison_ids)})",
            key="business_map_open_comparison",
            on_click=_open_selected_comparison,
            disabled=not comparison_ids,
            type="primary",
            icon=":material/arrow_forward:",
            help="Check up to four rows to compare. Search keeps your selected businesses.",
        )
    matches = [row for row in rows if search.casefold() in str(row["Business"]).casefold()]
    shown = matches[:limit]
    shown_ids = tuple(str(row["Business ID"]) for row in shown)
    _sync_table_selection(shown_ids)
    st.dataframe(
        shown,
        hide_index=True,
        key="business_map_table",
        on_select=partial(_select_table_business, shown_ids),
        selection_mode="multi-row",
        selection_default={"selection": {"rows": []}},
        column_order=(
            "Rank",
            "Business",
            "Recorded check-ins",
            *(
                ("Projected check-ins", "Recorded + projected check-ins")
                if projection.interval is not None
                else ()
            ),
            "Rating (snapshot)",
            "Reviews (snapshot)",
            "Operating status (snapshot)",
            "Source categories",
            "Business ID",
        ),
        column_config={
            "Recorded check-ins": st.column_config.NumberColumn(format="localized"),
            "Projected check-ins": st.column_config.NumberColumn(format="%.2f"),
            "Recorded + projected check-ins": st.column_config.NumberColumn(format="%.2f"),
            "Rating (snapshot)": st.column_config.NumberColumn(format="%.1f"),
            "Reviews (snapshot)": st.column_config.NumberColumn(format="localized"),
        },
        alt="Businesses ranked by recorded check-ins with archive snapshot fields",
    )
    hidden = len(set(comparison_ids).difference(shown_ids))
    st.caption(
        f"Showing {len(shown):,}/{len(matches):,} matches · "
        f"CSV includes all {len(rows):,} businesses."
        + (f" {hidden} selected outside this table." if hidden else "")
    )
    st.download_button(
        "Download businesses CSV",
        business_csv(rows, context, projection),
        file_name="sitesense_business_locations.csv",
        mime="text/csv",
        key="download_business_locations",
        icon=":material/download:",
    )
    from sitesense.scope_weather_views import render_scope_weather

    render_scope_weather(context, projection=projection)


def render(context: ScreenContext) -> None:
    _render_recorded(context)
