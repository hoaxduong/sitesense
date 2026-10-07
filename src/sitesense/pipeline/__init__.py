"""Offline pipeline: raw Yelp and ERA5 files -> serving tables in PostgreSQL.

Run with ``python -m sitesense.pipeline``. The Streamlit app never runs this code; it only
reads the tables the pipeline publishes.
"""
