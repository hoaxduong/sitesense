# SiteSense AI

SiteSense AI is a single Python/Streamlit research app for weather-aware retail location assessment.

Use **uv** to manage dependencies in the root `pyproject.toml` and `uv.lock`.
Keep secrets out of source control and Streamlit-rendered content.

Before handing off changes, run:

```sh
uv run --frozen python scripts/check.py
uv run --frozen python scripts/smoke.py
```

## Task guides

Read the relevant guides before starting:

- Dependency setup, local startup, notebooks, or secret configuration: [Local development](docs/agents/local-development.md).
- Python modules, data preparation, modeling, or UI rendering: [Python structure](docs/agents/python-structure.md).
- Data analysis, weather scenarios, or research claims: [Data interpretation](docs/agents/data-interpretation.md).
- Questions about libraries, frameworks, SDKs, APIs, CLIs, or cloud services; Cloudflare operations: [External tools](docs/agents/external-tools.md).
