"""WBS task directory and destinations in the current app."""

from dataclasses import dataclass

import streamlit as st

WBS_URL = (
    "https://docs.google.com/spreadsheets/d/1KQh724X0Vwdu3_7ywtbaOh7YGfpm0otI/"
    "edit?gid=1112071536#gid=1112071536"
)


@dataclass(frozen=True)
class UseCase:
    code: str
    title: str
    group: str
    note: str
    page: str = ""
    section: str = ""
    anchor: str = ""


USE_CASES = (
    UseCase(
        "T013",
        "Check-in aggregation",
        "US1",
        "Recorded activity, not customer counts.",
        "location_map",
        "Top businesses in the selected niche",
        "business-ranking",
    ),
    UseCase(
        "T014",
        "Activity ranking",
        "US1",
        "Descriptive check-in ranking.",
        "location_map",
        "Top businesses in the selected niche",
        "business-ranking",
    ),
    UseCase(
        "T015",
        "Ranking explanation",
        "US1",
        "Ranking basis and data limitations.",
        "business_analysis",
        "Ranking details",
        "ranking-details",
    ),
    UseCase(
        "T016",
        "Ranking SQL queries",
        "US1",
        "repository.py reads scoped business activity; Python computes descriptive rankings.",
    ),
    UseCase(
        "T017",
        "Ranking and comparison dashboard",
        "US1",
        "Compare up to four businesses.",
        "compare_sites",
        "Business comparison",
        "business-comparison",
    ),
    UseCase(
        "T018",
        "Explanation interface",
        "US1",
        "Ranking basis and data limitations.",
        "business_analysis",
        "Ranking details",
        "ranking-details",
    ),
    UseCase(
        "T019",
        "US1 demo scenario",
        "US1",
        "Planned demo: choose city/category, inspect rankings, compare businesses and explain "
        "coverage. WBS target: complete the US1 flow in five minutes.",
    ),
    UseCase(
        "T020",
        "Weather events and baseline",
        "US2",
        "Partial: descriptive weather bands; event baseline not implemented.",
        "business_analysis",
        "Weather band summary",
        "weather-bands",
    ),
    UseCase(
        "T021",
        "Weather sensitivity",
        "US2",
        "Descriptive daily correlations; no causal claim.",
        "business_analysis",
        "Analysis details",
        "weather-sensitivity",
    ),
    UseCase(
        "T022",
        "Weather SQL queries",
        "US2",
        "repository.py and location_weather.py supply activity and mapped-station observations "
        "for weather analysis. Missing or rejected weather remains unavailable.",
    ),
    UseCase(
        "T023",
        "Weather and activity charts",
        "US2",
        "Observed weather and recorded activity.",
        "location_map",
        "Weather and recorded activity",
        "scope-weather",
    ),
    UseCase(
        "T024",
        "Weather sensitivity in ranking",
        "US2",
        "No current feature: historical sensitivity is not incorporated into ranking.",
    ),
    UseCase(
        "T025",
        "Forecast baseline",
        "US3",
        "Daily-average reference and training method.",
        "about_models",
        "How we train",
        "model-training",
    ),
    UseCase(
        "T026",
        "Forecast model",
        "US3",
        "How HGB estimates daily activity.",
        "about_models",
        "What HGB does",
        "forecast-model",
    ),
    UseCase(
        "T027",
        "Forecast evaluation",
        "US3",
        "Forward-time checks and measured errors.",
        "about_models",
        "How we evaluate",
        "model-evaluation",
    ),
    UseCase(
        "T028",
        "Forecast SQL queries",
        "US3",
        "business_training.py prepares database-backed business/day training inputs; "
        "model training runs explicitly outside page rendering.",
    ),
    UseCase(
        "T029",
        "Forecast interface",
        "US3",
        "Choose Forecast or History → forecast in Date preset. Estimates need a trained "
        "model and complete weather inputs; uncertainty intervals are not provided.",
        "location_map",
        "Weather and recorded activity",
        "scope-weather",
    ),
    UseCase(
        "T007",
        "Database schema",
        "Data foundations",
        "Schema documentation, not live counts.",
        "about_database",
        "What each table stores",
        "database-tables",
    ),
    UseCase(
        "T008",
        "Yelp import",
        "Data foundations",
        "Import pipeline documentation.",
        "about_database",
        "From source files to the app",
        "data-pipeline",
    ),
    UseCase(
        "T009",
        "Weather import",
        "Data foundations",
        "Import pipeline documentation.",
        "about_database",
        "From source files to the app",
        "data-pipeline",
    ),
    UseCase(
        "T010",
        "Business–station matching",
        "Data foundations",
        "Spatial matching documentation.",
        "about_database",
        "Reading activity and weather correctly",
        "reading-data",
    ),
    *(
        UseCase(code, title, "Other WBS tasks", note)
        for code, title, note in (
            (
                "T001",
                "MVP scope, regions and categories",
                "Current app scope: Philadelphia, Nashville and Tampa; Restaurants and Day Spas. "
                "Defined in models.py.",
            ),
            (
                "T002",
                "Project instructions",
                "AGENTS.md and docs/agents/ define development, data interpretation "
                "and tool rules.",
            ),
            (
                "T003",
                "Technology versions",
                "pyproject.toml declares Python and dependency constraints; uv.lock records the "
                "resolved environment.",
            ),
            (
                "T004",
                "Streamlit foundation",
                "app.py starts the app; sitesense/app.py configures navigation, filters and safe "
                "setup states for missing database configuration or data.",
            ),
            (
                "T005",
                "Multipage structure",
                "app_pages/ contains exploration pages, model/database guides and this directory. "
                "Shared filters live in sitesense/app.py.",
            ),
            (
                "T006",
                "uv, Docker, PostgreSQL and CI",
                "Local setup is documented in docs/agents/local-development.md. "
                ".github/workflows/ci.yml runs the project checks.",
            ),
            (
                "T011",
                "Data quality checks",
                "Import adapters and tests validate source records. WBS acceptance includes "
                "business coverage and station-match targets; this page does not measure them.",
            ),
            (
                "T012",
                "Data usage rights",
                "WBS requires written confirmation of dataset usage rights. Authorization evidence "
                "and its status are not verified by this app.",
            ),
            (
                "T030",
                "Code freeze and end-to-end testing",
                "WBS code-freeze date: 21 October. scripts/check.py and scripts/smoke.py verify "
                "code and startup; this directory does not certify a release freeze.",
            ),
            (
                "T031",
                "UI polish",
                "Refine navigation, feature labels, empty states and responsive presentation. "
                "Final acceptance is outside this task directory.",
            ),
            (
                "T032",
                "Performance checks",
                "WBS proposes rankings in under five seconds. Runtime performance must be measured "
                "with the intended dataset and environment; no benchmark is reported here.",
            ),
            (
                "T033",
                "Reporting figures",
                "Collect source coverage, model evaluation and demo evidence for the report. "
                "This directory does not contain a verified reporting snapshot.",
            ),
            (
                "T034",
                "Presentation slides",
                "Prepare slides covering scope, data, US1–US3 results and limitations. "
                "No slide deliverable is linked in the app.",
            ),
            (
                "T035",
                "Demo rehearsal",
                "WBS rehearsal dates: 22–23 October. Practice the demo and Q&A; "
                "rehearsal completion is not tracked here.",
            ),
            (
                "T036",
                "Final report",
                "WBS report date: 24 October. Consolidate results and limitations; "
                "submission status is not tracked in the app.",
            ),
        )
    ),
)


def render_destination(page: str) -> None:
    """Identify a WBS destination and offer a matching in-page jump."""
    code = st.query_params.get("wbs", "")
    case = next((case for case in USE_CASES if case.code == code and case.page == page), None)
    if case is not None:
        st.markdown(f"**{case.code} · {case.title}** · [Jump to {case.section}](#{case.anchor})")
        st.caption(case.note)


def render_index() -> None:
    st.markdown(
        "All **36 WBS tasks**, including the US1–US3 use cases. "
        "Use the **Open feature** column to visit the matching page and section."
    )
    st.caption(
        "Destinations identify existing features or documentation, not completed acceptance "
        "criteria. Exploration features require imported data and may depend on your selection."
    )
    st.markdown(f"[Source WBS]({WBS_URL}) · Read on 19 October 2026")
    cases = sorted(USE_CASES, key=lambda case: case.code)
    page_titles = {
        "location_map": "Location map",
        "compare_sites": "Compare businesses",
        "business_analysis": "Business analysis",
        "about_models": "About our models",
        "about_database": "About our database",
    }
    st.dataframe(
        [
            {
                "Code": case.code,
                "Use case / task": case.title,
                "Group": case.group,
                "Page": page_titles.get(case.page, "No app screen"),
                "Section": case.section or "—",
                "Details": case.note,
                "Open feature": (
                    f"./{'' if case.page == 'location_map' else case.page}"
                    f"?wbs={case.code}#{case.anchor}"
                    if case.page
                    else None
                ),
            }
            for case in cases
        ],
        hide_index=True,
        height=600,
        key="use_case_table",
        row_height=100,
        placeholder="—",
        column_config={
            "Code": st.column_config.TextColumn(pinned=True, width="small"),
            "Use case / task": st.column_config.TextColumn(width="medium"),
            "Details": st.column_config.TextColumn(width="large"),
            "Open feature": st.column_config.LinkColumn(display_text="Open feature ↗"),
        },
        alt="All 36 WBS tasks with user story, destination page, section and scope notes",
    )
