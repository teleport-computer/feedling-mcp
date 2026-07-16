# CLAUDE.md — repo-level guidance

## Design System

Always read `DESIGN.md` before making any visual or UI decisions.
All font choices, colors, spacing, and aesthetic direction are defined
there. Do not deviate without explicit user approval. If running a
design or QA review, flag any code that does not match `DESIGN.md`.

Concrete rule: no raw hex values, no raw point sizes (except for
display), no raw font strings in Swift view files — use the
`Color.feedling…` / `Font.feedling…` / `Spacing.*` / `Radius.*`
tokens defined at the bottom of `DESIGN.md`.

## Reading order on session start

1. `docs/CHANGELOG.md` — landmark diffs from recent sessions; this is
   the source of truth for "what shipped, when, why."
2. `CONTRIBUTING.md` — backend code-organization rules (where new code
   goes, dependency direction, asgi_app.py is assembly-only). Read before
   writing ANY backend code; PRs are reviewed against it.
3. `DESIGN.md` — if doing any UI work.
4. `deploy/DEPLOYMENTS.md` — if doing any enclave/CVM/on-chain work.
5. `docs/TESTING.md` — the general test standard: a "changed X → run
   which tests" decision matrix (§2) covering every change class
   (backend logic, routes, encryption/account link, gateway/driver,
   consumer, schema, compose, CVM, iOS, docs, contracts). Consult it
   before declaring ANY change done — it defines the Definition of Done.

There is no longer a separate HANDOFF.md — it was a session-relay doc
from the v0→v1 strip era and was deleted 2026-05-12. Recent state lives
in the CHANGELOG and in git log.

## Agent mailbox

When coordinating with another local coding/audit agent in this repo, use
`docs/AGENT_MAILBOX.md` and `scripts/agent-mailbox/`. The durable mailbox lives
under `.agents/mailbox/` and is ignored by git; tmux wakeups only inject a
fixed read command, never the full message body.

## Other repo conventions

- TDX enclave operations — cross-reference the local `dstack-tutorial`
  clone if available when keys/attestation/gateway/TLS details are
  non-obvious.
- Prod user count is intentionally tiny. Clean reinstall migrations are
  acceptable when explicitly scoped by the task; do not publish personal
  tester details in docs.
- `/v1/content/swap` is the ongoing in-place envelope-swap endpoint
  (visibility toggles). There is no v0→v1 migration path anymore;
  plaintext writes now return 400.

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

## Public docs mirror (io-onboarding)

The three public-facing onboarding docs live in a separate public repo
at `github.com/teleport-computer/io-onboarding`:

- `skill.md`            — agent-facing instructions (fetched by user's MCP client)
- `quickstart.md`       — 5-step setup for human testers (zh + en)
- `troubleshooting.md`  — common failure triage (zh + en)

**These are NOT in this repo anymore.** When a user asks to change any
of those three files, edit them in the local clone of the
io-onboarding repo and push there. The iOS app's `ChatEmptyStateView.skillURL`
constant pins to the raw URL of `skill.md` on `main`, so a push to
io-onboarding is immediately visible to all installed apps (no rebuild
needed for doc updates).

When iterating on agent behavior (e.g. tightening the bootstrap rules)
that is also documented in skill.md, update **both**:
1. The relevant code in this repo (asgi_app.py / the relevant *_core.py / etc.)
2. `skill.md` in the io-onboarding repo

If the user says "update the skill" without specifying, they mean the
public skill.md in io-onboarding.
