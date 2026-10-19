"""Evidence summaries keep three candidates on identical held-out business-days."""

from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from sitesense import business_research, model_guide, model_guide_views


@pytest.fixture
def research_run(tmp_path: Path) -> business_research.RunResult:
    metrics, predictions = [], []
    # Include an extra candidate and an unmatched population: neither may be pooled.
    for stage, year in (("validation", 2020), ("test", 2021)):
        for population in ("accepted_weather", "all_covered"):
            for model in ("overall_mean", "poisson", "hgb", "gam"):
                error = {"overall_mean": 0.2, "poisson": 0.4, "hgb": 0.3, "gam": 0.1}[model]
                if stage == "test" and model == "poisson":
                    error = 0.25  # The assessment must not choose the weather model.
                metrics.append(
                    dict(
                        stage=stage,
                        population=population,
                        model=model,
                        observations=2,
                        businesses=1,
                        recorded_checkins=3,
                        predicted_checkins=4,
                        poisson_deviance=error,
                        mae=0.5,
                    )
                )
                for month, recorded in ((1, 1), (2, 2)):
                    predictions.append(
                        dict(
                            stage=stage,
                            population=population,
                            model=model,
                            business_id="business-a",
                            day=f"{year}-{month:02d}-01",
                            recorded_checkins=recorded,
                            predicted_checkins=2.0,
                        )
                    )
    return business_research.RunResult(
        tmp_path,
        pd.DataFrame(metrics),
        pd.DataFrame(predictions),
        pd.DataFrame(),
        pd.DataFrame(),
        {
            "import_revision": "import-a",
            "created_at": "2026-10-19",
            "config": {
                "city": "Philadelphia",
                "state": "PA",
                "category": "Restaurants",
                "source_category": "Pizza",
            },
        },
    )


def test_three_models_match_days_and_choose_on_validation(
    research_run: business_research.RunResult,
) -> None:
    evidence = model_guide.summarize_research(research_run)
    assert len(evidence["scores"]) == 6
    assert {r["Model"] for r in evidence["scores"]} == {"Daily average", "Poisson", "HGB"}
    assert evidence["weather_choice"] == "HGB"
    assert len(evidence["monthly"]) == 8
    actual = [r for r in evidence["monthly"] if r["Series"] == "Recorded check-ins"]
    assert sum(r["Check-ins"] for r in actual) == 3  # Not repeated for each candidate.
    for name in ("Daily average", "Poisson", "HGB"):
        assert sum(r["Check-ins"] for r in evidence["monthly"] if r["Series"] == name) == 4


@pytest.mark.parametrize("problem", ("labels", "duplicate", "missing_model", "counts", "nan"))
def test_mismatched_or_invalid_research_is_rejected(
    research_run: business_research.RunResult, problem: str
) -> None:
    predictions, metrics = research_run.predictions, research_run.metrics
    prediction_index = predictions.index[
        predictions.model.eq("hgb") & predictions.population.eq("accepted_weather")
    ][0]
    metric_index = metrics.index[
        metrics.model.eq("hgb") & metrics.population.eq("accepted_weather")
    ][0]
    if problem == "labels":
        predictions.loc[prediction_index, "recorded_checkins"] = 9
    elif problem == "duplicate":
        predictions.loc[prediction_index, "day"] = predictions.loc[prediction_index + 1, "day"]
    elif problem == "missing_model":
        metrics.drop(index=metric_index, inplace=True)
    elif problem == "counts":
        metrics.loc[metric_index, "businesses"] = 2
    else:
        metrics.loc[metric_index, "poisson_deviance"] = float("nan")
    with pytest.raises(ValueError):
        model_guide.summarize_research(research_run)


def test_offline_loader_requires_same_import_revision(
    research_run: business_research.RunResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(business_research, "latest_run", lambda root: research_run.directory)

    def load(directory: Path, *, verify_source: bool) -> business_research.RunResult:
        assert directory == research_run.directory
        assert verify_source is False
        return research_run

    monkeypatch.setattr(business_research, "load_run", load)
    assert len(model_guide.load_research(Path("unused"), "import-a")["scores"]) == 6
    with pytest.raises(ValueError, match="same imported dataset"):
        model_guide.load_research(Path("unused"), "changed-import")


def test_error_reductions_are_not_accuracy_percentages() -> None:
    assert model_guide.error_reduction(0.4, 0.3) == pytest.approx(25)
    assert model_guide.error_reduction(0.2, 0.3) == pytest.approx(-50)
    assert model_guide.error_reduction(0, 0.3) is None


def test_three_model_evidence_renders_with_caveats(
    research_run: business_research.RunResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = model_guide.summarize_research(research_run)
    monkeypatch.setattr(model_guide, "research_revision", lambda root: "verified-revision")
    monkeypatch.setattr(model_guide_views, "_research", lambda *args: evidence)
    app = AppTest.from_string(
        "from sitesense.model_guide_views import render_three_model_research\n"
        "render_three_model_research('import-a')"
    ).run()
    assert not app.exception
    assert app.dataframe[0].value.Model.tolist() == ["Daily average", "Poisson", "HGB"]
    assert len(app.get("vega_lite_chart")) == 2
    assert "25.00% lower" in app.success[0].value
    text = "\n".join(item.value for item in (*app.caption, *app.markdown))
    assert "3 check-ins were recorded and HGB estimated 4.0" in text
    assert "does not establish performance in other cities" in text
    assert "not query the live database" in text


def test_missing_research_still_renders_safe_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(root: Path) -> str:
        raise ValueError("private artifact details")

    monkeypatch.setattr(model_guide, "research_revision", missing)
    app = AppTest.from_string(
        "from sitesense.model_guide_views import render_three_model_research\n"
        "render_three_model_research('import-a')"
    ).run()
    assert not app.exception
    assert "not available" in app.info[0].value
    assert "private" not in app.info[0].value
