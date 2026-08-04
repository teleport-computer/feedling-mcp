# Admin Usage P0-A-only Delivery Design

**Date:** 2026-08-04
**Status:** Approved scope decision
**Delivery branch:** `feat/admin-runtime-user-report`
**Pull request:** [#146](https://github.com/teleport-computer/feedling-mcp/pull/146)

PR #146 merged into `test` on 2026-08-03. It remains the only product-code
delivery PR. A later docs-only follow-up may record this scope decision without
adding or changing product behavior.

## Execution status (2026-08-04)

PR #146 has already merged the P0-A product code into `test`. Docs-only PR
#154 was closed unmerged after unrelated current-`test` Dream policy checks
failed. The maintainer explicitly authorized this direct, docs-only update to
`test`; P0-B and P0-C remain deferred.

## Decision

Ship P0-A as the complete scope for this delivery. Defer P0-B provider-attempt
accounting and P0-C resident telemetry because the current product need is
operational usage and reliability visibility, not financial billing or exact
provider-invoice reconciliation.

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

Do not merge or deploy `feat/provider-attempt-accounting`. Preserve the branch
as experimental evidence only. Its current strict 3-million-turn plus
3-million-attempt query gate is not met: the tested query shapes remain above
the three-second budget and spill aggregation work to disk.

If exact attempt accounting becomes a real requirement, start with a fresh
design review using the recorded experiments. Do not resume by merging the
stale branch unchanged.

### P0-C: resident usage upload

Do not start the resident upload API, default-on resident batching, or resident
user report. Reconsider it only when self-host/resident usage visibility has a
confirmed product consumer and an explicit privacy/trust-boundary review.

## Delivery and validation

1. Treat merged PR #146 as the only product-code delivery PR.
2. Publish this final boundary through a docs-only follow-up PR against `test`;
   it must contain no backend, migration, infrastructure, or generated changes.
3. Preserve PR #146's screenshots, current-RDS architecture, fail-open behavior,
   and scale proof, and add the operational-not-billing boundary in the
   follow-up record.
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

## Future reopening triggers

Revisit P0-B or P0-C only if at least one of these becomes an approved
requirement:

- invoice-grade provider reconciliation;
- exact per-HTTP-attempt retry or possibly-billed accounting;
- resident/self-host usage reporting in the product;
- a demonstrated operational question that P0-A cannot answer.
