# Repository instructions

<!-- context7 -->

Use Context7 MCP to fetch current documentation whenever the user asks about a library, framework, SDK, API, CLI tool, or cloud service. This includes API syntax, configuration, version migration, library-specific debugging, setup instructions, and CLI tool usage. Prefer it over web search for library documentation.

Do not use Context7 for refactoring, writing scripts from scratch, debugging business logic, code review, or general programming concepts.

1. Start with `resolve-library-id` using the library name and the documentation question, unless the user supplies an exact `/org/project` ID.
2. Select by exact name, relevance, snippet coverage, source reputation, and benchmark score. Use version-specific IDs when a version is specified; retry alternate names if results are unsuitable.
3. Call `query-docs` with a focused documentation question. Query distinct concepts separately unless the question concerns their interaction.
4. Answer using the retrieved documentation.

<!-- context7 -->

When interacting with Cloudflare, use the `cf` CLI unless the project has a Wrangler configuration file.

## Project conventions

- This is one Python project. uv owns dependencies in the root `pyproject.toml` and `uv.lock`. Use `uv lock --check` and `uv sync --frozen` for reproducible validation. Notebook dependencies are optional: `uv sync --frozen --group notebooks`.
- `app.py` is the Streamlit entry point; put reusable Python code under `src/sitesense`. Keep data preparation and modeling logic separate from UI rendering as those features are added.
- Run `uv run --frozen python scripts/check.py` before handing off changes. Run `uv run --frozen python scripts/smoke.py` to verify server startup and app execution. Start development with `uv run --frozen streamlit run app.py`.
- Preserve the analytical meaning of the data: check-ins are an activity proxy, and weather scenarios do not prove causal effects. Keep secrets out of source control and Streamlit-rendered content; local `.streamlit/secrets.toml` is ignored.
