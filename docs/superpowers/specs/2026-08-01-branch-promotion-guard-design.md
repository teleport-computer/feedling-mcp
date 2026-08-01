# Branch Promotion Guard Design

## Goal

Prevent ordinary development branches from being merged directly into
`main`, where a push starts the production deployment workflow. Keep the
release path flexible enough that either `test` or `pre` may be promoted to
`main`.

## Policy

- Feature, fix, optimization, and agent-created development branches should
  open pull requests against `test` first and be verified in the test
  environment before promotion.
- Pull requests targeting `main` are allowed only when their source branch is
  `test` or `pre`.
- The repository does not enforce a fixed merge relationship between `test`
  and `pre`.
- Emergency exceptions require an explicit maintainer decision and a recorded
  reason and follow-up validation plan. The normal CI guard remains fail-closed;
  an exception is handled through an authorized ruleset bypass, not a magic
  branch-name or label escape hatch in repository code.

## Documentation

`CONTRIBUTING.md` is the canonical human-facing development and release guide.
`AGENTS.md` repeats the concise mandatory rules that coding agents need before
creating a pull request. This is an internal contribution workflow change, so
the public product documentation under `docs-site/content/docs/` is unchanged.

## CI Guard

Add a small pull-request job to `.github/workflows/ci.yml`. For pull requests
whose base branch is `main`, it succeeds only when `github.head_ref` is exactly
`test` or `pre`. It is a no-op for pull requests targeting other branches and
for push or manual workflow runs.

The check must be configured as a required status check in the GitHub ruleset
for `main`; repository workflow code alone cannot prevent a maintainer from
merging a red pull request or pushing directly to `main`.

## Verification

- Exercise the branch predicate with a local table of allowed and rejected
  base/head combinations.
- Parse the workflow YAML and inspect the documentation diff.
- Run the repository's lightweight workflow/configuration tests if a focused
  test exists; otherwise rely on the predicate test and YAML parser.
