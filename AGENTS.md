# Repository guidance for coding agents

## Branch and pull-request flow

- Ordinary development branches, including `feature/*`, `fix/*`, `opt/*`,
  and agent-created branches, must not open pull requests directly against
  `main`. When the target is not explicitly specified, use `test`.
- Before proposing production promotion, merge the work into `test` and record
  test-environment evidence appropriate to the change's risk.
- Pull requests targeting `main` must originate from the repository's `test`
  or `pre` branch. The repository does not enforce a fixed merge direction
  between `test` and `pre`.
- Do not bypass the `branch flow` check. An emergency exception requires an
  explicit maintainer decision recorded in the pull request, including the
  reason, risk assessment, and follow-up validation plan.
- Local merges are not restricted, but pushing `main` starts the production
  deployment path. Never push a locally assembled `main` to bypass review.

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
