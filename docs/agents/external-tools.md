# External tools

## Library and service documentation

<!-- context7 -->

Use Context7 MCP to fetch current documentation whenever the user asks about a library, framework, SDK, API, CLI tool, or cloud service, and base the answer on the retrieved documentation. This includes API syntax, configuration, version migration, library-specific debugging, setup instructions, and CLI tool usage. Prefer Context7 over web search for library documentation.

Do not use Context7 for refactoring, writing scripts from scratch, debugging business logic, code review, or general programming concepts.

1. Start with `resolve-library-id` using the library name and the documentation question, unless the user supplies an exact `/org/project` ID.
2. Select by exact name, relevance, snippet coverage, source reputation, and benchmark score. Use version-specific IDs when a version is specified; retry alternate names if results are unsuitable.
3. Call `query-docs` with a focused documentation question. Query distinct concepts separately unless the question concerns their interaction.

<!-- context7 -->

## Cloudflare operations

When interacting with Cloudflare, use the `cf` CLI unless the project has a Wrangler configuration file.
