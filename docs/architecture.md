# Architecture

## Application boundaries

`apps/web` owns the user interface and Next.js server routes. `apps/api` owns domain operations and their FastAPI contract. Browser code does not need the backend's private deployment address: the frontend uses `API_BASE_URL` on the server when contacting the API.

`packages/api-client` provides the shared client and generated TypeScript types. FastAPI models define the contract; `pnpm generate` exports `apps/api/openapi.json` and generates `packages/api-client/src/schema.d.ts`. Keep generated artifacts in Git so frontend builds do not depend on a running API. CI detects stale artifacts by regenerating them.

The API is a standalone uv project with a packaged `src` layout. Its `package.json` exposes Python commands to Turborepo; it does not replace `pyproject.toml` or create a second Python dependency graph. A root uv workspace is unnecessary while the repository contains one Python project.

## Dependency and task model

pnpm installs JavaScript dependencies from `pnpm-lock.yaml`. uv installs Python dependencies from `apps/api/uv.lock`. CI validates the Python lockfile before using frozen installs because `uv sync --frozen` does not check whether the manifest changed.

Turborepo schedules lint, type checks, tests, and builds through package scripts. The frontend depends on the shared API client, so dependency builds precede its production build. Development servers are persistent, uncached tasks. Generated contracts are refreshed explicitly with `pnpm generate` and verified in CI.

Environment settings that affect cached tasks must appear in `turbo.json`. Keep secrets in local environment files or the eventual hosting platform's secret store. `API_BASE_URL` is server-side configuration and must not receive a `NEXT_PUBLIC_` prefix.

## Current service behavior

The API's versioned health endpoint returns `{"status":"ok","service":"sitesense-api"}`; `/healthz` provides the same service check. Interactive API documentation is available in development and disabled in production. The frontend health route reports backend connectivity. These are operational checks, not evidence that a dataset or model is ready.

## Research and data boundaries

The linked product conversation frames SiteSense as weather-aware retail location assessment. The scaffold deliberately starts with application infrastructure; it does not contain a trained model, a populated dataset, or a scoring service.

The first analytical increment should audit source data and define the target before adding prediction endpoints. Check-ins measure recorded activity and may contain repeated visitors, sampling bias, or source changes. Do not reinterpret them as unique customers or revenue.

Build a seasonal baseline with calendar and location features, then test whether weather improves out-of-sample performance. Use forward temporal holdouts, unseen-location spatial holdouts, and event holdouts. Fit preprocessing only on training data and compare both models on the same held-out observations.

Ranking and explanations follow validated prediction performance. Record dataset versions, feature definitions, evaluation splits, and uncertainty alongside model versions. Weather scenarios describe model estimates under alternative inputs; they do not establish that weather caused a change in activity.
