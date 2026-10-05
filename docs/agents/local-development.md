# Local development

Run commands from the repository root.

## Dependency setup

Validate the lockfile and install locked dependencies:

```sh
uv lock --check
uv sync --frozen
```

Notebook dependencies are optional. Install them when working with notebooks:

```sh
uv sync --frozen --group notebooks
```

## App startup

```sh
uv run --frozen streamlit run app.py
```

## Local secrets

Local `.streamlit/secrets.toml` is ignored by Git.
