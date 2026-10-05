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

## Workspace conventions

- pnpm owns JavaScript dependencies; uv owns the Python project in `apps/api`. Use `pnpm install --frozen-lockfile`, `uv lock --check --project apps/api`, and `uv sync --frozen --project apps/api` for reproducible validation.
- Change API models in Python, then run `pnpm generate`. Commit the OpenAPI schema and generated TypeScript types together; do not hand-edit generated contract files.
- Run `pnpm check` before handing off changes. Use `pnpm dev:web` or `pnpm dev:api` for focused development and `pnpm format` for formatting.
- Preserve the analytical meaning of the data: check-ins are an activity proxy, and weather scenarios do not prove causal effects. Keep secrets out of source control and browser-exposed environment variables.

<!-- BEGIN:turborepo-agent-rules -->

# This is NOT the Turborepo you know

Turborepo configuration, task behavior, and CLI commands can vary between installed versions and may differ from your training data. Resolve the `turbo` package from this file's directory or relevant workspace; in monorepos, it may not be visible from the repository root. For example, run `node -p "require.resolve('turbo/package.json')"` from a workspace that depends on `turbo`.

Read `docs/README.md` inside that installed package first, then read the relevant pages from its `docs/` directory before changing Turborepo configuration or commands. Heed deprecation notices. These bundled docs match the installed package version and are available without network access.

This block is written and re-added by `turbo` before repository-scoped commands when an AI agent is detected. In the Turborepo source repository, its template is defined in `crates/turborepo-cli/src/cli/agent_guidance.rs`. Removing the managed block while updates are enabled means a later qualifying invocation will add it again. Set `"agentGuidance": false` in the root `turbo.json` or `turbo.jsonc` to opt out; this does not remove an existing block. Keep the block committed with your work to avoid an uncommitted change on the next agent invocation.
<!-- END:turborepo-agent-rules -->
