# SiteSense AI

Weather-aware retail location assessment. SiteSense will compare seasonal activity baselines with weather-aware models before using those models to rank candidate locations.

This repository initializes the application and its development workflow. It includes a Next.js frontend, a Python API, and a generated API contract. Data ingestion, model training, and location scoring are future work.

## Requirements

- Node.js 24, matching `.node-version`.
- pnpm 11.16.0, pinned in the root `package.json`.
- Python 3.13, managed by uv.
- uv 0.12.23 or newer; CI uses 0.12.23.

Install uv using the [official installation instructions](https://docs.astral.sh/uv/getting-started/installation/). Install the pinned pnpm version with:

```sh
npm install --global pnpm@11.16.0
```

Check `uv --version` before starting. For a standalone uv installation, upgrade with `UV_NO_MODIFY_PATH=1 uv self update`; for a package-manager installation, use that manager's upgrade command.

## Start development

From the repository root:

```sh
pnpm install --frozen-lockfile
uv sync --frozen --project apps/api
cp apps/api/.env.example apps/api/.env
cp apps/web/.env.example apps/web/.env.local
pnpm dev
```

Open the web app at <http://localhost:3000>. The API runs at <http://127.0.0.1:8000>, with Swagger documentation at <http://127.0.0.1:8000/docs> in development.

The frontend uses the private, server-side `API_BASE_URL` setting to contact the API. API configuration uses `SITESENSE_` environment variables. Example files document the defaults; keep local environment files out of Git.

## Commands

| Command                              | Purpose                                                                  |
| ------------------------------------ | ------------------------------------------------------------------------ |
| `pnpm dev`                           | Start the frontend and API together.                                     |
| `pnpm dev:web`                       | Start only the frontend.                                                 |
| `pnpm dev:api`                       | Start only the API.                                                      |
| `pnpm generate`                      | Export the Python OpenAPI schema and regenerate TypeScript API types.    |
| `pnpm check`                         | Check formatting, lint, types, tests, and production builds.             |
| `pnpm smoke`                         | Verify the production web app reaches the API and reports an API outage. |
| `pnpm format`                        | Format Python and workspace files.                                       |
| `uv lock --check --project apps/api` | Verify the Python lockfile matches its manifest.                         |

After changing API request or response models, run `pnpm generate` and commit both generated files: `apps/api/openapi.json` and `packages/api-client/src/schema.d.ts`. CI regenerates the contract and rejects drift.

## Workspace

```text
apps/
  api/                  FastAPI service, uv project, Python src layout
  web/                  Next.js App Router frontend
packages/
  api-client/           Shared client and generated OpenAPI types
docs/
  architecture.md       Application boundaries and evaluation principles
```

Turborepo coordinates package tasks and caches completed checks and builds. pnpm owns JavaScript dependencies; uv owns the API's Python environment and `apps/api/uv.lock`. See [architecture.md](docs/architecture.md) for the task graph and research boundaries.

The API's `/api/v1/health` and `/healthz` endpoints report service health. The frontend's `/api/health` checks connectivity to the API. These checks do not establish data quality, model accuracy, or readiness to assess a location. Run `pnpm check` before `pnpm smoke`; the smoke check uses the production frontend build.

## Backend container

Build and run the API with its production dependencies and non-root runtime:

```sh
docker build --tag sitesense-api:local apps/api
docker run --rm --publish 127.0.0.1:8000:8000 sitesense-api:local
```

The image sets `SITESENSE_ENVIRONMENT=production`, which disables interactive API documentation.

## Project principles

- Treat check-ins as an activity proxy, not unique customers or revenue.
- Audit source coverage, missingness, timestamp alignment, and weather joins before modeling.
- Establish seasonal baselines before measuring the additional value of weather.
- Evaluate with temporal, spatial, and event holdouts to expose leakage and poor generalization.
- Present weather scenarios as model estimates, without claiming causal effects.

## Optional Vercel tooling

If you plan to deploy on Vercel, strongly recommended: install the Vercel CLI with `npm i -g vercel`. It enables `vercel env pull`, `vercel deploy`, and `vercel logs` once a Vercel project is connected. This scaffold does not configure a deployment or provision external services.
