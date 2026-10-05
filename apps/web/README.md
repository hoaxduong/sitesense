# SiteSense web

Next.js App Router frontend for the SiteSense AI research project. The initial
page describes the project and checks backend connectivity. It deliberately
reports that research data and models are not configured.

## Local development

Run workspace commands from the repository root; see the root README for the
combined frontend and Python API workflow.

Copy `.env.example` to `.env.local` to override the backend origin. `API_BASE_URL`
is read only by the server. If it is unset in development, the frontend connects
to `http://127.0.0.1:8000`. It must be supplied in production.

## API boundary

The browser calls the same-origin `/api/health` route. That route uses the
generated `@sitesense/api-client` to check `/api/v1/health`, with an uncached
request and a five-second timeout. Responses expose service availability without
including the upstream URL or internal error messages. A healthy API does not
imply that data, models, or predictions are ready.

Keep backend calls in server code and regenerate the shared API client whenever
the backend contract changes. Never expose backend secrets using `NEXT_PUBLIC_`
environment variables.

## Checks

The package provides `lint`, `typecheck`, `test`, and `build` scripts. Route tests
cover valid responses, upstream failures, timeouts, and production configuration.
No remote font downloads are needed to build the application.
