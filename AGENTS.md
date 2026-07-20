# Repository guidance for coding agents

## Public documentation synchronization

The public documentation is maintained in this repository; it is not
automatically derived from every code change. When a change modifies the public
API contract or behavior, system architecture, trust boundaries,
security/isolation assumptions, or deployment topology, update the affected
files under `docs-site/content/docs/` in the same commit or pull request.

For a public API change, also update the OpenAPI source or overrides as needed,
regenerate `docs-site/openapi/public.json` with
`cd docs-site && npm run openapi:generate`, and review the generated diff. For
an architecture change, review the architecture page, its diagram, related
workflow pages, and the self-hosting trust model. Record user-visible contract
or documentation changes under `Unreleased` in
`docs-site/content/docs/changelog.mdx`.

Before completing a relevant change, run the OpenAPI contract tests and, from
`docs-site`, run `npm run types:check`, `npm run lint`, and `npm run build`.
Changes that do not affect documented behavior do not require documentation
edits.

## IO E2E qualification

For requests to plan, start, monitor, inspect, or cancel an agent-driven IO E2E
qualification, read and follow `.agents/skills/io-e2e/SKILL.md`. Use the
checked-in `tools.io_e2e` CLI; do not dispatch the protected workflow directly.
