import streamlit as st

from sitesense.business_map import render

render(st.session_state["screen_context"])
