"""Multipage Streamlit app: shared sidebar filters and one module per page."""

import streamlit as st

from sitesense.components.sidebar import render_sidebar
from sitesense.pages import activity, compare, forecast, ranking, weather


def render_app() -> None:
    st.set_page_config(page_title="SiteSense AI", page_icon="🌦️", layout="wide")
    navigation = st.navigation(
        [
            st.Page(ranking.render, title="Site ranking", icon=":material/leaderboard:",
                    url_path="ranking", default=True),
            st.Page(activity.render, title="Customer activity", icon=":material/schedule:",
                    url_path="activity"),
            st.Page(weather.render, title="Weather impact", icon=":material/rainy:",
                    url_path="weather"),
            st.Page(forecast.render, title="Demand forecast", icon=":material/trending_up:",
                    url_path="forecast"),
            st.Page(compare.render, title="Compare sites", icon=":material/compare_arrows:",
                    url_path="compare"),
        ]
    )  # fmt: skip
    render_sidebar()
    navigation.run()
