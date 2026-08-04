# Admin Usage P0-A-only Delivery Design

**Date:** 2026-08-04
**Status:** Approved scope decision
**Delivery branch:** `feat/admin-runtime-user-report`
**Product-code pull request:** [#146](https://github.com/teleport-computer/feedling-mcp/pull/146)
**P0-B archive/review pull request:** [Draft #155](https://github.com/teleport-computer/feedling-mcp/pull/155)

[PR #146](https://github.com/teleport-computer/feedling-mcp/pull/146) merged
P0-A into `test` on 2026-08-03 and remains the only product-code delivery.
Only P0-A is shipped on `test`; P0-B, P0-C, and billing-grade accounting remain
unshipped, and production is untouched.

## Execution status (2026-08-04)

[PR #146](https://github.com/teleport-computer/feedling-mcp/pull/146) merged the
P0-A product code into `test` and remains the only product-code delivery.
Docs-only [PR #154](https://github.com/teleport-computer/feedling-mcp/pull/154)
was closed unmerged after unrelated current-`test` Dream policy checks failed.
The maintainer-authorized direct two-document update landed on `test` through
the normal fast-forward sequence culminating in the status correction at
[`953c074d45309448360125753fb231006344eeee`](https://github.com/teleport-computer/feedling-mcp/commit/953c074d45309448360125753fb231006344eeee).
The former follow-up-PR workflow is historical and superseded: it **MUST NOT**
be executed or used to create another PR.

User-authorized [Draft PR #155](https://github.com/teleport-computer/feedling-mcp/pull/155)
is OPEN against `test` from `feat/provider-attempt-accounting` at
`dbfdeed3b4f19a774870fa0e5c1a7cb4f160d1eb`, solely for archival and review.
GitHub reports it as `DIRTY` / `CONFLICTING`; it is **NOT READY FOR MERGE OR
DEPLOYMENT**. The strict 3-million-turn plus 3-million-attempt performance gate
failed, so P0-B requires redesign, rebase, and renewed validation. The Draft
authorizes no `test` or production deployment.

## Decision

Ship P0-A as the complete scope for this delivery. Keep P0-B provider-attempt
accounting and P0-C resident telemetry deferred and unshipped because the
current product need is operational usage and reliability visibility, not
financial billing or exact provider-invoice reconciliation. The open Draft for
P0-B does not change this boundary.

## Product outcome

The Admin surfaces provide:

- per-user (`user_id`) usage visibility;
- token and provider/model breakdowns;
- delivery reliability and failure visibility;
- time, lane, provider, model, completeness, and user filtering;
- explicit unknown/unavailable states rather than presenting missing telemetry
  as zero.

Runtime Health remains the delivery-reliability surface. The independent Usage
view remains the token/model and usage-analysis surface. Together they satisfy
the requested operational report without coupling delivery diagnosis to a more
expensive provider-attempt ledger.

## Data semantics

P0-A uses Hosted V2 whole-turn metrics and current-business-RDS rollups. Its
numbers are suitable for operational trends, user support, capacity analysis,
and anomaly investigation.

P0-A is not a billing ledger. It does not promise exact reconstruction of every
provider HTTP attempt, exact possibly-billed retries, or reconciliation against
a provider invoice. Product and operator copy must not describe it as billing
or financial truth.

## Architecture and infrastructure boundary

- Continue using the existing business RDS and existing backend/worker
  deployment units.
- Do not add SQLite, local PostgreSQL, Redis, Kafka, another RDS, service,
  container, CVM, or client telemetry channel.
- Keep rollup work bounded, off the provider/reply hot path, and fail-open.
- A report or maintenance failure may make a section unavailable or stale; it
  must not change provider calls, replies, retries, heartbeat, or job handling.

## Deferred work

### P0-B: provider-attempt accounting

Do not merge or deploy `feat/provider-attempt-accounting`. The user-authorized
[Draft PR #155](https://github.com/teleport-computer/feedling-mcp/pull/155)
preserves the branch solely as experimental archival/review evidence; it is
OPEN against `test` at `dbfdeed3b4f19a774870fa0e5c1a7cb4f160d1eb`, and GitHub
reports it as `DIRTY` / `CONFLICTING`. It is **NOT READY FOR MERGE OR
DEPLOYMENT** and authorizes no test or production deployment.

Its strict 3-million-turn plus 3-million-attempt query gate failed: the tested
query shapes exceeded the three-second budget and spilled aggregation work to
disk. Redesign, rebase, and renewed validation are required before any new
delivery decision.

If exact attempt accounting becomes a real requirement, start with a fresh
design review using the recorded experiments. Do not resume by merging the
stale branch unchanged.

### P0-C: resident usage upload

Do not start the resident upload API, default-on resident batching, or resident
user report. Reconsider it only when self-host/resident usage visibility has a
confirmed product consumer and an explicit privacy/trust-boundary review.

## Delivery and validation

1. Treat merged [PR #146](https://github.com/teleport-computer/feedling-mcp/pull/146)
   as the only product-code delivery.
2. Historical only — the docs-only follow-up-PR route was superseded by the
   authorized direct two-document fast-forward sequence culminating in the
   status correction at
   [`953c074d45309448360125753fb231006344eeee`](https://github.com/teleport-computer/feedling-mcp/commit/953c074d45309448360125753fb231006344eeee);
   **do not execute it or create another docs-only PR**. PR #154 closed
   unmerged, and the completed direct update contains no backend, migration,
   infrastructure, or generated changes.
3. Preserve PR #146's screenshots, current-RDS architecture, fail-open behavior,
   and scale proof, and add the operational-not-billing boundary in the
   closeout record.
4. Require the focused Admin/Usage/Runtime/migration suites and repository CI to
   pass on the final P0-A head.
5. Retain the recorded 3-million-row, rolling-90-day proof with both unfiltered
   and provider/model-filtered p95 below three seconds.
6. Smoke-test the already-merged `test` delivery: the Usage page, Runtime
   delivery section,
   filters, unknown/unavailable rendering, rollup freshness, and ordinary chat
   behavior before considering production promotion.
7. Observe test-environment RDS pool pressure, query latency, and maintenance
   lag. Any material business-path degradation blocks promotion even if the
   report itself works.

## Acceptance criteria

- An admin can inspect per-user token/model usage and delivery reliability.
- Missing or incomplete telemetry is visibly distinguished from zero usage.
- The 90-day report remains within the established three-second p95 gate.
- Telemetry/report failures do not alter user-facing provider or reply behavior.
- No new infrastructure or local persistent telemetry store is introduced.
- Documentation and PR text state that P0-A is operational telemetry, not a
  financial billing source.
- P0-A remains the only scope shipped on `test`; P0-B, P0-C, and billing-grade
  accounting remain unshipped.
- Draft PR #155 remains archival/review-only and must not be merged or deployed
  without redesign, rebase, renewed validation, and a new delivery decision.
- Production remains untouched.

## Future reopening triggers

Revisit P0-B or P0-C only if at least one of these becomes an approved
requirement:

- invoice-grade provider reconciliation;
- exact per-HTTP-attempt retry or possibly-billed accounting;
- resident/self-host usage reporting in the product;
- a demonstrated operational question that P0-A cannot answer.
