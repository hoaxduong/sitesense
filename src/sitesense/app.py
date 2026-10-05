"""Render the local research workspace using native Streamlit components."""

import streamlit as st


def render_app() -> None:
    st.set_page_config(page_title="SiteSense AI", page_icon="🌦️", layout="wide")
    st.title("SiteSense AI")
    st.caption("Research workspace · Location. Weather. Activity.")
    st.header("Weather-aware retail location intelligence.")
    st.write(
        "Explore how local weather relates to recorded check-in activity, "
        "then use that evidence to evaluate potential retail locations."
    )

    with st.container(border=True):
        st.subheader("Research readiness")
        st.info("Not configured")
        st.markdown("**Start with trustworthy data.**")
        st.write(
            "No check-in dataset, weather source, or trained model is connected yet. "
            "Activity forecasts and location scores will appear after data is prepared "
            "and models are evaluated."
        )

    st.divider()
    st.subheader("The research path")
    for column, number, title, description in zip(
        st.columns(3),
        ("01", "02", "03"),
        ("Prepare the evidence", "Evaluate the models", "Explore the locations"),
        (
            "Align recorded check-ins, locations, and historical weather.",
            "Compare baselines and measure performance on held-out data.",
            "Present predictions with their assumptions and uncertainty.",
        ),
        strict=True,
    ):
        with column:
            st.caption(number)
            st.markdown(f"**{title}**")
            st.write(description)
    st.caption("Check-ins are an activity proxy. Weather scenarios do not prove causal effects.")
    st.divider()
    st.caption("SiteSense AI · Retail location intelligence research")
