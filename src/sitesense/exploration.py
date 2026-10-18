"""Shared ZIP selection and native navigation for the two exploration pages."""

from typing import cast

import streamlit as st

from sitesense.models import AreaSummary

MAX_COMPARISON_AREAS = 4


def get_comparison_ids() -> list[int]:
    return cast(list[int], st.session_state.get("compare_areas", []))


def prepare_comparison(summaries: tuple[AreaSummary, ...]) -> None:
    """Keep selections valid after a new catalog or source import."""
    eligible = [summary.area.id for summary in summaries]
    st.session_state["comparison_eligible_ids"] = eligible
    previous = get_comparison_ids()
    valid = list(dict.fromkeys(area_id for area_id in previous if area_id in eligible))[
        :MAX_COMPARISON_AREAS
    ]
    if valid != previous:
        st.session_state["exploration_notice"] = (
            "Comparison updated because some selected ZIP areas are no longer eligible."
        )
    st.session_state["compare_areas"] = valid


def toggle_comparison(area_id: int) -> None:
    selected = get_comparison_ids().copy()
    if area_id in selected:
        selected.remove(area_id)
    elif area_id not in st.session_state.get("comparison_eligible_ids", []):
        st.session_state["exploration_notice"] = "This ZIP is unavailable in the selected scope."
        return
    elif len(selected) >= MAX_COMPARISON_AREAS:
        st.session_state["exploration_notice"] = (
            "Compare up to four areas. Remove one before adding another."
        )
        return
    else:
        selected.append(area_id)
    st.session_state["compare_areas"] = selected


def clear_comparison() -> None:
    st.session_state["compare_areas"] = []


def open_comparison() -> None:
    if len(get_comparison_ids()) >= 2:
        st.session_state["requested_exploration_page"] = "compare_sites"
        st.rerun()


def open_map(area_id: int | None = None) -> None:
    if area_id is not None and area_id in st.session_state.get("comparison_eligible_ids", []):
        st.session_state["pending_map_area"] = area_id
        st.session_state["location_series_scope"] = "Selected ZIP"
    st.session_state["requested_exploration_page"] = "location_map"


def render_comparison_selection(summaries: tuple[AreaSummary, ...]) -> None:
    """Keep candidates and the comparison action visible while exploring the map."""
    selected = get_comparison_ids()
    if not selected:
        # Reserve the outside container before the first fragment-only Add action.
        st.bottom.empty()
        return
    areas = {summary.area.id: summary.area for summary in summaries}
    with st.bottom, st.container(border=True, key="map_comparison_bar", gap="xsmall"):
        with st.container(
            horizontal=True,
            wrap=False,
            vertical_alignment="center",
            gap="small",
        ):
            with st.container(
                horizontal=True,
                wrap=False,
                gap="xsmall",
                width="stretch",
                key="map_comparison_chips",
            ):
                for area_id in selected:
                    st.button(
                        areas[area_id].label,
                        icon=":material/close:",
                        key=f"map_comparison_remove_{area_id}",
                        on_click=toggle_comparison,
                        args=(area_id,),
                        help=f"Remove {areas[area_id].label} from comparison.",
                    )
                st.button("Clear", key="map_clear_comparison", on_click=clear_comparison)
            st.button(
                f"Compare ({len(selected)})",
                key="map_open_comparison",
                type="primary",
                disabled=len(selected) < 2,
                on_click=open_comparison,
                icon=":material/arrow_forward:",
                help="Add one more ZIP to compare."
                if len(selected) == 1
                else "Compare these ZIPs.",
            )
