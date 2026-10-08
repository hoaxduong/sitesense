"""Offline pipeline: local Yelp and ERA5 files -> serving tables in PostgreSQL.

Run with ``python -m sitesense.pipeline``. Streamlit never runs this code; the app only reads
the tables it publishes.
"""
