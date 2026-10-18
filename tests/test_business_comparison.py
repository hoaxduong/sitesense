"""Comparison selection delegates exact businesses and dates to one weather/activity chart."""

from dataclasses import replace
from datetime import date

import psycopg
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from sitesense import app as application
from sitesense import business_comparison as comparison
from sitesense import repository
from sitesense.business_activity import ProjectionResult
from sitesense.business_analysis import BusinessActivity
from sitesense.business_forecast import BusinessGroup, BusinessProjection
from sitesense.models import Business, Catalog, DateCoverage, Filters
from sitesense.ui import ScreenContext

ChartCall = tuple[ScreenContext, tuple[Business, ...], ProjectionResult]


def _business(business_id: str, tags: tuple[str, ...] = ("Restaurants", "Pizza")) -> Business:
    return Business(
        business_id,
        None,
        "Same name",
        "Philadelphia",
        "PA",
        "19001",
        40.0,
        -75.0,
        tags,
        4.5,
        20,
        True,
        None,
    )


@pytest.fixture
def context() -> ScreenContext:
    businesses = (
        *(_business(value) for value in ("a", "b", "c", "d", "e")),
        _business("pizza-delivery", ("Restaurants", "Pizza Delivery")),
        _business("spa", ("Day Spas", "Pizza")),
    )
    return ScreenContext(
        Filters(
            "Philadelphia",
            "PA",
            "Restaurants",
            date(2020, 1, 29),
            date(2020, 2, 4),
            source_category="Pizza",
        ),
        Catalog((), businesses, (), DateCoverage(date(2020, 1, 31), date(2020, 2, 2))),
        (),
        (),
        "comparison-import",
    )


@pytest.fixture
def chart_calls() -> list[ChartCall]:
    return []


@pytest.fixture(autouse=True)
def repository_stubs(monkeypatch: pytest.MonkeyPatch, chart_calls: list[ChartCall]) -> None:
    comparison._activity.clear()
    monkeypatch.setattr(application, "_import_is_unchanged", lambda revision: True)
    monkeypatch.setattr(comparison, "load_projection", lambda *args: ProjectionResult(None))

    def weather(
        context: ScreenContext, businesses: tuple[Business, ...], projection: ProjectionResult
    ) -> None:
        chart_calls.append((context, businesses, projection))
        st.subheader("Weather and recorded activity")

    monkeypatch.setattr(comparison, "render_comparison_weather", weather)
    monkeypatch.setattr(
        repository, "load_business_activity", lambda filters: (BusinessActivity("a", 10, 2, 3),)
    )


def _page(context: ScreenContext, **state: object) -> AppTest:
    page = AppTest.from_string(
        "from sitesense.business_comparison import render_business_comparison\n"
        "render_business_comparison()",
        default_timeout=15,
    )
    page.session_state["screen_context"] = context
    for key, value in state.items():
        page.session_state[key] = value
    return page.run()


def _chosen(calls: list[ChartCall]) -> tuple[str, ...]:
    return tuple(business.business_id for business in calls[-1][1])


def test_exact_source_selection_delegates_once_with_one_comparison_matrix(
    context: ScreenContext, chart_calls: list[ChartCall]
) -> None:
    page = _page(context)
    assert not page.exception
    assert page.multiselect[0].options == [f"Same name · {value}" for value in "abcde"]
    assert page.multiselect[0].value == ["a", "b"]
    assert len(chart_calls) == 1
    assert _chosen(chart_calls) == ("a", "b")
    assert chart_calls[0][0] == context
    assert [heading.value for heading in page.subheader] == [
        "Weather and recorded activity",
        "Business comparison",
    ]
    assert len(page.dataframe) == 1
    matrix = page.dataframe[0].value.set_index("Metric")
    assert matrix.columns.tolist() == ["Same name · a", "Same name · b"]
    assert matrix.loc["Recorded check-ins"].tolist() == ["10", "0"]
    assert matrix.loc["Days with check-ins"].tolist() == ["2", "0"]
    assert matrix.loc["Weekend share (%)"].tolist() == ["30.0", "Unavailable"]
    assert matrix.loc["Rating (snapshot)"].tolist() == ["4.5", "4.5"]
    assert matrix.loc["Reviews (snapshot)"].tolist() == ["20", "20"]
    assert matrix.loc["Operating status (snapshot)"].tolist() == ["Open", "Open"]
    assert matrix.loc["Source categories"].tolist() == ["Restaurants, Pizza", "Restaurants, Pizza"]
    assert not page.metric
    assert not page.selectbox
    assert not page.get("download_button")
    assert not page.get("vega_lite_chart")


def test_default_seed_clips_summary_query_but_chart_retains_requested_dates(
    context: ScreenContext, chart_calls: list[ChartCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Filters] = []

    def activity(filters: Filters) -> tuple[BusinessActivity, ...]:
        calls.append(filters)
        return (BusinessActivity("c", 100, 2, 3), BusinessActivity("a", 10, 2, 3))

    monkeypatch.setattr(repository, "load_business_activity", activity)
    page = _page(context)
    assert not page.exception
    assert calls == [
        replace(context.filters, start_date=date(2020, 1, 31), end_date=date(2020, 2, 2))
    ]
    assert _chosen(chart_calls) == ("c", "a")
    assert chart_calls[-1][0].filters == context.filters


def test_existing_picks_use_cached_summary_without_reseeding_and_resolve_duplicate_names(
    context: ScreenContext, chart_calls: list[ChartCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    queries: list[Filters] = []

    def activity(filters: Filters) -> tuple[BusinessActivity, ...]:
        queries.append(filters)
        return (BusinessActivity("a", 100, 2, 3),)

    monkeypatch.setattr(repository, "load_business_activity", activity)
    page = _page(
        context,
        business_compare_scope=("Philadelphia", "PA", "Restaurants", "Pizza"),
        business_compare_ids=["e", "c"],
    )
    assert not page.exception
    assert _chosen(chart_calls) == ("e", "c")
    assert queries == [
        replace(context.filters, start_date=date(2020, 1, 31), end_date=date(2020, 2, 2))
    ]
    assert chart_calls[-1][1] == (context.catalog.businesses[4], context.catalog.businesses[2])
    page.text_input(key="business_compare_search").set_value("no-such-business").run()
    assert not page.exception
    assert page.multiselect[0].value == ["e", "c"]
    assert page.multiselect[0].options == ["Same name · e", "Same name · c"]
    assert _chosen(chart_calls) == ("e", "c")
    assert len(queries) == 1


def test_scope_change_resets_ids_and_date_change_preserves_them(
    context: ScreenContext, chart_calls: list[ChartCall]
) -> None:
    page = _page(context)
    page.multiselect[0].set_value(["e", "c"]).run()
    changed = replace(context, filters=replace(context.filters, start_date=date(2020, 1, 31)))
    page.session_state["screen_context"] = changed
    page.run()
    assert page.multiselect[0].value == ["e", "c"]
    assert chart_calls[-1][0] == changed
    page.session_state["screen_context"] = replace(
        context, filters=replace(context.filters, source_category="Pizza Delivery")
    )
    page.run()
    assert not page.exception
    assert page.multiselect[0].value == ["pizza-delivery"]
    assert _chosen(chart_calls) == ("pizza-delivery",)


def test_shared_helpers_prune_stale_ids_and_enforce_four(context: ScreenContext) -> None:
    page = AppTest.from_string(
        "import streamlit as st\nfrom sitesense import business_comparison as c\n"
        "c.prepare(st.session_state['screen_context'])\n"
        "for value in st.session_state.get('toggle_ids', []): c.toggle_comparison(value)\n"
        "st.text(','.join(c.get_comparison_ids()))"
    )
    page.session_state["screen_context"] = context
    page.session_state["business_compare_scope"] = ("Philadelphia", "PA", "Restaurants", "Pizza")
    page.session_state["business_compare_ids"] = ["a", "stale-id", "a"]
    page.session_state["toggle_ids"] = ["b", "c", "d", "e", "a"]
    page.run()
    assert not page.exception
    assert page.text[0].value == "b,c,d"


def test_failed_default_query_is_unavailable_not_zero_chart_data(
    context: ScreenContext, chart_calls: list[ChartCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failure(filters: Filters) -> tuple[BusinessActivity, ...]:
        raise psycopg.OperationalError("do not expose credentials")

    monkeypatch.setattr(repository, "load_business_activity", failure)
    page = _page(context)
    assert not page.exception
    assert not chart_calls
    assert any("Database temporarily unavailable" in value.value for value in page.info)
    assert not any("credentials" in value.value for value in page.info)


def test_import_change_suppresses_stale_default_selection(
    context: ScreenContext, chart_calls: list[ChartCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(application, "_import_is_unchanged", lambda revision: False)
    page = _page(context)
    assert not page.exception
    assert not chart_calls


def test_empty_explicit_selection_does_not_reseed_or_render_chart(
    context: ScreenContext, chart_calls: list[ChartCall]
) -> None:
    page = _page(context)
    calls = len(chart_calls)
    page.multiselect[0].set_value([]).run()
    page.run()
    assert not page.exception
    assert page.multiselect[0].value == []
    assert len(chart_calls) == calls
    assert page.info
    assert not page.dataframe


def test_single_selected_business_uses_same_chart_without_detail_selector(
    context: ScreenContext, chart_calls: list[ChartCall]
) -> None:
    page = _page(context)
    page.multiselect[0].set_value(["e"]).run()
    assert not page.exception
    assert _chosen(chart_calls) == ("e",)
    assert not page.selectbox
    assert not page.metric
    assert len(page.dataframe) == 1
    assert page.dataframe[0].value.columns.tolist() == ["Metric", "Same name"]


def test_map_handoff_adds_one_eligible_business_to_chart(
    context: ScreenContext, chart_calls: list[ChartCall]
) -> None:
    page = _page(context, pending_comparison_business="e")
    assert not page.exception
    assert page.multiselect[0].value == ["e"]
    assert _chosen(chart_calls) == ("e",)
    assert "pending_comparison_business" not in page.session_state


def test_full_map_handoff_preserves_four_series_without_adding_fifth(
    context: ScreenContext, chart_calls: list[ChartCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    projections: list[tuple[str, ...]] = []

    def forecast(ctx: ScreenContext, ids: tuple[str, ...]) -> ProjectionResult:
        projections.append(ids)
        return ProjectionResult(None)

    monkeypatch.setattr(comparison, "load_projection", forecast)
    page = _page(
        context,
        business_compare_scope=("Philadelphia", "PA", "Restaurants", "Pizza"),
        business_compare_ids=["a", "b", "c", "d"],
        pending_comparison_business="e",
    )
    assert not page.exception
    assert page.multiselect[0].value == ["a", "b", "c", "d"]
    assert projections == [("a", "b", "c", "d")]
    assert _chosen(chart_calls) == ("a", "b", "c", "d")


def test_scope_change_discards_ineligible_pending_handoff(
    context: ScreenContext, chart_calls: list[ChartCall]
) -> None:
    changed = replace(context, filters=replace(context.filters, source_category="Pizza Delivery"))
    page = _page(
        changed,
        business_compare_scope=("Philadelphia", "PA", "Restaurants", "Pizza"),
        business_compare_ids=["a", "b"],
        pending_comparison_business="spa",
    )
    assert not page.exception
    assert _chosen(chart_calls) == ("pizza-delivery",)
    assert chart_calls[-1][0] == changed
    assert "pending_comparison_business" not in page.session_state


def _projection(start: date, end: date) -> ProjectionResult:
    group = BusinessGroup(
        "a",
        "Same name",
        "Philadelphia",
        "PA",
        "Restaurants",
        ("Pizza",),
        40.0,
        -75.0,
        100,
        20,
        2,
        (2.0,) * 84,
    )
    days = (end - start).days + 1
    return ProjectionResult(
        (start, end),
        (BusinessProjection(group, days, 2, days * 2),),
        model_revision="verified-business-model",
    )


@pytest.mark.parametrize("future_only", [False, True])
def test_chart_receives_requested_dates_and_exact_projection_provenance(
    context: ScreenContext,
    chart_calls: list[ChartCall],
    monkeypatch: pytest.MonkeyPatch,
    future_only: bool,
) -> None:
    projection = _projection(date(2020, 2, 3), date(2020, 2, 4))
    forecasts: list[tuple[ScreenContext, tuple[str, ...]]] = []
    if future_only:
        context = replace(context, filters=replace(context.filters, start_date=date(2020, 2, 3)))

        def unexpected(filters: Filters) -> tuple[BusinessActivity, ...]:
            pytest.fail("Future-only selection must not query recorded activity for seeds")

        monkeypatch.setattr(repository, "load_business_activity", unexpected)

    def forecast(ctx: ScreenContext, ids: tuple[str, ...]) -> ProjectionResult:
        forecasts.append((ctx, ids))
        return projection

    monkeypatch.setattr(comparison, "load_projection", forecast)
    page = _page(context)
    assert not page.exception
    assert forecasts == [(context, ("a", "b"))]
    assert chart_calls == [(context, context.catalog.businesses[:2], projection)]
    assert projection.interval == (date(2020, 2, 3), date(2020, 2, 4))
    assert projection.model_revision == "verified-business-model"
    assert tuple(row.group.business_id for row in projection.projections) == ("a",)
    matrix = page.dataframe[0].value.set_index("Metric")
    assert matrix.loc["Projected check-ins"].tolist() == ["4.00", "Unavailable"]
    assert matrix.loc["Recorded check-ins"].tolist() == (
        ["Unavailable", "Unavailable"] if future_only else ["10", "0"]
    )
    if future_only:
        assert any("Recorded activity unavailable" in caption.value for caption in page.caption)
    else:
        assert any(
            "Selected:" in caption.value
            and "Recorded:" in caption.value
            and "31 Jan 2020 – 02 Feb 2020" in caption.value
            for caption in page.caption
        )
    assert not page.segmented_control
    assert not page.date_input


def test_future_date_max_delegates_without_timestamp_conversion(
    context: ScreenContext, chart_calls: list[ChartCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    projection = _projection(date.max, date.max)
    monkeypatch.setattr(comparison, "load_projection", lambda *args: projection)
    future = replace(
        context, filters=replace(context.filters, start_date=date.max, end_date=date.max)
    )
    page = _page(future)
    assert not page.exception
    assert chart_calls == [(future, future.catalog.businesses[:2], projection)]


def test_summary_failure_keeps_shared_chart_and_snapshot_profile_without_false_zeros(
    context: ScreenContext, chart_calls: list[ChartCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    def failure(filters: Filters) -> tuple[BusinessActivity, ...]:
        raise psycopg.OperationalError("private database credentials")

    monkeypatch.setattr(repository, "load_business_activity", failure)
    page = _page(
        context,
        business_compare_scope=("Philadelphia", "PA", "Restaurants", "Pizza"),
        business_compare_ids=["a", "b"],
    )
    assert not page.exception
    assert _chosen(chart_calls) == ("a", "b")
    matrix = page.dataframe[0].value.set_index("Metric")
    for metric in ("Recorded check-ins", "Days with check-ins", "Weekend share (%)"):
        assert matrix.loc[metric].tolist() == ["Unavailable", "Unavailable"]
    assert matrix.loc["Rating (snapshot)"].tolist() == ["4.5", "4.5"]
    assert matrix.loc["Operating status (snapshot)"].tolist() == ["Open", "Open"]
    assert any("summary is unavailable" in warning.value for warning in page.warning)
    assert not any("credentials" in warning.value for warning in page.warning)


@pytest.mark.parametrize("name", ["Coffee A", "Metric"])
def test_profile_matrix_preserves_snapshot_missing_values_and_reserved_business_names(
    context: ScreenContext,
    name: str,
) -> None:
    first = replace(
        context.catalog.businesses[0], name=name, stars=None, review_count=1_000, is_open=False
    )
    context = replace(
        context,
        catalog=replace(context.catalog, businesses=(first, *context.catalog.businesses[1:])),
    )
    page = _page(context)
    assert not page.exception
    matrix = page.dataframe[0].value.set_index("Metric")
    label = "Metric · a" if name == "Metric" else name
    assert matrix.columns.tolist() == [label, "Same name"]
    assert matrix.loc["Rating (snapshot)", label] == "Unavailable"
    assert matrix.loc["Reviews (snapshot)", label] == "1,000"
    assert matrix.loc["Operating status (snapshot)", label] == "Closed"
    assert matrix.loc["Recorded check-ins", label] == "10"
    assert "Projected check-ins" not in matrix.index


def test_summary_revision_change_discards_matrix_with_existing_selection(
    context: ScreenContext, chart_calls: list[ChartCall], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(application, "_import_is_unchanged", lambda revision: False)
    page = _page(
        context,
        business_compare_scope=("Philadelphia", "PA", "Restaurants", "Pizza"),
        business_compare_ids=["a", "b"],
    )
    assert not page.exception
    assert _chosen(chart_calls) == ("a", "b")
    assert not page.dataframe
