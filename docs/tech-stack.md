# SiteSense AI technology stack

SiteSense AI uses **Python, Streamlit, PostgreSQL, and uv** for a weather-aware retail location research workspace. Jupyter notebooks provide the data exploration environment. Reusable Python modules handle database access; ongoing local weather research uses notebook 02.

This guide describes the repository inspected on **2026-10-05**. Versions come from [pyproject.toml](../pyproject.toml), [uv.lock](../uv.lock), and the runtime configuration. The rationale explains why the current choices fit the project; it is engineering reasoning, rather than a record of the original authors' decisions.

Weather-specific entries describe local research work. Notebook 02 and its omfiles decoder dependency are pending inclusion in the versioned project; the committed application and notebook setup do not yet include them.

## Stack at a glance

| Layer | Technology / current version | Role in SiteSense | Why it fits |
| --- | --- | --- | --- |
| Language | Python 3.13 | Application, data preparation, notebooks, and validation scripts | Research and application code can share functions and data structures. |
| Web application | Streamlit 1.65.0 | Runs the Python page and manages browser interactions | A small team can expose research workflows through a Python UI. |
| Structured storage | PostgreSQL 18 | Dataset provenance registry and foundation for application records | Constraints and transactions protect structured, persistent records. |
| Database access | Psycopg 3.3.6, with binary distribution | Direct SQL connections and explicit migrations | The small schema remains easy to inspect without an ORM. |
| Environment and dependencies | uv 0.12.23 in CI and Docker | Shared lockfile, environment, commands, and package builds | Development, research, and runtime use the same dependency resolution. |
| Research environment | JupyterLab 4.6.4 and IPykernel 7.4.0 | Interactive dataset exploration | Code, parameters, charts, and interpretation can be reviewed together. |
| Tabular analysis | pandas 3.0.6 | Coverage audits, date handling, joins, aggregation, and exports | Fits inspectable tabular experiments and supports bounded CSV reads. |
| Research charts | Matplotlib 3.11.2 | Coverage, activity, baseline, and weather plots | Produces research figures that can be saved and shared. |
| Data acquisition | gdown 6.4.1; omfiles 1.2.0 in local research | Yelp archive download and selected weather-array decoding | Handles the actual source formats used by the research workflows. |
| Packaging and local services | Hatchling, Docker, Docker Compose | Python distributions, app container, local PostgreSQL | Separates the app runtime from build tools and database lifecycle. |
| Quality and automation | Ruff, mypy, pytest, Streamlit AppTest, GitHub Actions | Static checks, behavioral tests, startup checks, and CI | Each checks a different class of failure. |

The project allows Python `>=3.13,<3.14`, Streamlit `>=1.54,<2`, and Psycopg `>=3.2,<4`; the table shows the current resolutions. uv requires at least `0.12.23`. Python and PostgreSQL image tags select version families, rather than exact patches or image digests. Hatchling is configured as `>=1.27,<2`; it has no resolved package entry in `uv.lock`.

## Architecture and current boundaries

The Streamlit page currently displays readiness and the research sequence; it does not query PostgreSQL, load notebook artifacts, or score a location. The database module and research workflows are available independently of the page. Notebook outputs are not automatically imported into PostgreSQL.

The browser connects to Streamlit's server, where Python executes. The entry point calls the rendering module under `src/sitesense`. Streamlit normally reruns a script when widgets change; future expensive data loading must account for this execution model. [Streamlit architecture and execution concepts](https://docs.streamlit.io/develop/concepts/architecture)

See [architecture.md](architecture.md) for application boundaries and research requirements.

## Why Python and Streamlit

**Python keeps the research and application in one language.** The notebooks already perform data inspection and baseline experiments in Python. Database access lives in Python modules, while weather preparation lives directly in notebook 02. Moving a validated calculation into the app can therefore reuse its implementation, rather than translating it into a second language or exposing it through an additional service.

**Streamlit fits the current research workflow.** The page uses native titles, status messages, containers, and captions. This keeps the UI close to the analysis and reduces the amount of application infrastructure needed to share experiments. [app.py](../app.py) delegates rendering to [src/sitesense/app.py](../src/sitesense/app.py).

The tradeoff is Streamlit's server execution and rerun model. Large downloads, migrations, and long computations need explicit boundaries as interactive features grow. The current acquisition notebook and migration CLI already operate separately from UI rendering. Requirements for a highly customized interface, independent API clients, or long-running jobs would justify revisiting the application boundary.

Python 3.13 is the common target in `.python-version`, package metadata, lint/type settings, Docker, and CI. This alignment reduces environment differences. The repository does not establish a performance benchmark or a research requirement that makes 3.13 uniquely necessary.

## Why uv and separate dependency groups

[pyproject.toml](../pyproject.toml) defines the dependencies; [uv.lock](../uv.lock) records their resolved versions. uv manages the root `.venv` and runs project commands within that environment.

| Dependency boundary | Contents | How it is used |
| --- | --- | --- |
| Application dependencies | Streamlit and `psycopg[binary]` | Direct dependencies installed for the app runtime. |
| `dev` group | Ruff, mypy, pytest | Included by default for development and validation. |
| `notebooks` group | Jupyter tools, pandas, Matplotlib, gdown | Selected explicitly for exploration; local weather work also adds omfiles. |
| Build system | Hatchling | Builds the wheel and source distribution. |

**One lockfile reduces dependency drift between research and application work.** Groups let the repository share compatible versions while installing tools according to the task. The Docker build uses `--no-dev` and does not select the notebook group. A library such as pandas can still appear in the runtime as a Streamlit dependency; group membership is not a promise that every listed library is absent from other installations.

`uv lock --check` verifies that the lock matches the manifest. Frozen commands use the existing lock without updating it; `--frozen` alone does not verify that it reflects manifest changes. CI and the check script therefore validate the lock explicitly. [uv locking and syncing](https://docs.astral.sh/uv/concepts/projects/sync/)

Use the existing commands:

```sh
uv sync --frozen
uv sync --frozen --group notebooks
uv run --frozen --env-file .env streamlit run app.py
```

The shared resolution also means dependency upgrades must remain compatible across the app and notebooks. Updating a group changes the common lockfile; it should be reviewed and validated as a project change.

## Research and data tooling

Notebook dependencies are optional. Their current versions and uses are:

| Tool | Version | Current use |
| --- | --- | --- |
| JupyterLab | 4.6.4 | Interactive notebook workspace. |
| IPykernel | 7.4.0 | Python kernel for notebook execution. |
| pandas | 3.0.6 | Tabular exploration, joins, calendar aggregation, baseline comparisons, and exports. |
| Matplotlib | 3.11.2 | Research plots in both notebooks. |
| gdown | 6.4.1 | Resumable download of the supplied Google Drive archive in notebook 01. |
| omfiles | 1.2.0 in local research | Decodes selected slices from compressed Open-Meteo `.om` weather arrays. |
| nbclient | 0.11.0 | Notebook execution support. |
| nbconvert | 7.17.1 | Headless execution and notebook conversion. |
| nbformat | 5.11.1 | Notebook document-format support. |

Notebook 02 also imports **NumPy 2.5.3** for numeric and sequence checks. NumPy is currently resolved transitively and is not declared independently in the notebook group. A resolved package version and a directly chosen dependency are different facts.

**Jupyter makes the research inspectable.** Notebook 01 downloads and stages the Yelp business/check-in files, audits coverage, and compares simple daily-activity baselines on a forward holdout. Notebook 02 downloads or reuses weather files, audits their coverage, and produces daily/monthly UTC summaries. Its analysis uses local files; acquisition needs network access when cached partitions are missing or corrupt. Parameters, checks, plots, and interpretation are visible together.

**pandas fits the tabular questions.** The weather notebook reads hourly CSV files in 100,000-row chunks and aggregates them, rather than retaining the whole hourly dataset in one dataframe. pandas also supports join-key validation to catch unexpected duplication. [Chunked CSV reading](https://pandas.pydata.org/docs/reference/api/pandas.read_csv.html), [merge validation](https://pandas.pydata.org/docs/reference/api/pandas.DataFrame.merge.html)

The tradeoff is memory and notebook state. Chunking bounds input batches, but intermediates and joins can still grow. Weather is stored once per grid cell; attaching every weather hour to every business can multiply rows substantially. Acquisition logic lives in notebook 02, and notebook execution remains a separate research check. The standard CI workflow does not execute these notebooks.

## Packaging, services, and deployment

**Hatchling packages reusable code.** The wheel includes `src/sitesense`, including SQL migrations. The source distribution also includes the entry point and Streamlit configuration. `uv build` creates Python distributions; Streamlit runs the Python entry point directly.

**Docker separates build and runtime.** [Dockerfile](../Dockerfile) installs locked app dependencies in a builder stage, then copies the installed virtual environment into a Python 3.13 slim runtime. The final stage runs as a non-root user on port 8501 and checks Streamlit's health endpoint. Multi-stage builds allow selected artifacts to be copied while leaving build tools behind. [Docker multi-stage builds](https://docs.docker.com/build/building/multi-stage/)

[compose.yaml](../compose.yaml) runs PostgreSQL locally with a named persistent volume and a port bound to `127.0.0.1`. Streamlit normally runs on the host for hot reload. The app container expects PostgreSQL to be managed separately, with migrations applied explicitly before application instances start.

The repository provides a container recipe and CI validation. It does not configure a production hosting platform, image publication, or an automatic deployment workflow. A health response verifies server availability; it does not establish database readiness, dataset correctness, or model quality.

Local database credentials belong in ignored `.env`. Other local credentials may use ignored `.streamlit/secrets.toml`; deployed credentials belong in the hosting platform's secret store. The database module reads `DATABASE_URL` from the environment. Secrets and local research artifacts are excluded from source control and the Docker build context.

## Quality checks and their purpose

| Tool / check | Current version | What it checks |
| --- | --- | --- |
| Ruff | 0.16.10 | Lint, imports, selected bug patterns, and formatting. |
| mypy | 1.20.2 | Strict type checking for source, entry point, and scripts. |
| pytest | 9.1.1 | Python behavior and database logic. |
| Streamlit AppTest | Included with Streamlit | Executes the actual page and checks readiness messages and interpretation caveats. |
| Smoke script | Project code | Starts a temporary server, checks health/homepage responses, executes the page with AppTest, and stops the server. |
| Package build | uv / Hatchling | Builds the source distribution and wheel. |

The required handoff commands are:

```sh
uv run --frozen python scripts/check.py
uv run --frozen python scripts/smoke.py
```

[GitHub Actions](../.github/workflows/ci.yml) runs validation on pushes and pull requests, using Python 3.13 and PostgreSQL 18. It also runs isolated database integration tests. Locally, those integration tests require `TEST_DATABASE_URL` and a role able to create disposable databases; they skip when the variable is absent.

These checks give complementary evidence about code and startup behavior. They do not cover browser WebSocket behavior, notebook execution, production deployment, or prediction accuracy. Dependabot currently updates GitHub Actions dependencies; it does not automate the Python dependency upgrades.

## Plain-language explanation for a presentation

> SiteSense uses Python so the research notebooks and web application can share the same logic. Streamlit provides the web interface, PostgreSQL stores structured application records, and uv keeps dependency versions consistent. Jupyter, pandas, and Matplotlib support data exploration, while notebook 02 acquires and audits historical weather. Docker and automated checks make the application easier to package and validate. The current work establishes data quality and seasonal baselines; weather-aware prediction and location scoring come after validation.
