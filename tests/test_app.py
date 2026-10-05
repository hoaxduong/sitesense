from pathlib import Path

from streamlit.testing.v1 import AppTest

APP_FILE = Path(__file__).resolve().parents[1] / "app.py"


def test_workspace_displays_readiness_without_fabricated_predictions() -> None:
    app = AppTest.from_file(str(APP_FILE)).run()

    assert not app.exception
    assert app.title[0].value == "SiteSense AI"
    assert app.info[0].value == "Not configured"
    assert any(
        "No check-in dataset, weather source, or trained model is connected yet." in item.value
        for item in app.markdown
    )
    assert not app.metric
    assert not app.dataframe


def test_workspace_preserves_research_sequence_and_analytical_caveats() -> None:
    app = AppTest.from_file(str(APP_FILE)).run()

    assert not app.exception
    text = "\n".join(item.value for item in app.markdown)
    assert text.index("Prepare the evidence") < text.index("Evaluate the models")
    assert text.index("Evaluate the models") < text.index("Explore the locations")
    assert "held-out data" in text
    assert "assumptions and uncertainty" in text
    assert any(
        "Check-ins are an activity proxy. Weather scenarios do not prove causal effects."
        == item.value
        for item in app.caption
    )
