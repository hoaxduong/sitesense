"""Caveat banners shown wherever numbers appear."""

import streamlit as st

from sitesense import queries

PROXY = (
    "Check-ins are a **proxy** for customer activity, not real visitor counts or sales. "
    "Use them to compare areas, not to predict revenue."
)
ASSOCIATION = (
    "Weather results are **associations** with confidence intervals, not proof that "
    "weather causes the change."
)
FORECAST = (
    "Forecasts are model estimates with a range. Weather scenarios show what the model "
    "expects under different conditions; they do not prove causal effects."
)
YEARLY_USAGE = (
    "The number of people using Yelp check-ins changes from year to year. Compare areas "
    "using **share** or **index** values, not raw counts across years."
)
SAMPLE = (
    "**Sample data.** Every number on this page is a generated placeholder shaped like the "
    "real data, not a Yelp or weather result."
)


def page_header(title: str, subtitle: str, caveat: str) -> None:
    st.title(title)
    st.caption(subtitle)
    if queries.using_sample_data():
        st.warning(SAMPLE, icon=":material/science:")
    st.info(caveat, icon=":material/info:")
