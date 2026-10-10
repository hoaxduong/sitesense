# Python structure

- `app.py` is the Streamlit entry point.
- Put reusable Python code under `src/sitesense`.
- Keep data preparation and modeling logic separate from UI rendering as those features are added.
- Keep downloads, imports, migrations, and model training outside Streamlit rendering. Run them explicitly.
- Keep Yelp and climate source adapters under `src/sitesense/import_data/`. Each adapter owns its source parsing and database writes; `pipeline.py` coordinates them in one transaction, and `cli.py` handles command-line options. Keep their source configurations independent.
- Use `repository.py` for database queries and `analytics.py` for historical summaries; keep page rendering under `app_pages/`.
