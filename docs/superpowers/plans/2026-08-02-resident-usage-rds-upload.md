# Resident Usage RDS Upload P0-C Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upload self-host/resident provider-attempt telemetry to the current business RDS by default, expose a user-scoped report, and preserve business behavior under every telemetry failure.

**Architecture:** The resident captures content-free attempt facts into a bounded in-memory queue and posts batches of at most 64 to a user-authenticated endpoint on the existing ASGI backend. The endpoint validates a strict allowlist and idempotently upserts the P0-B ledger; a user-scoped report API reads only the authenticated user's rows, and a local tool renders temporary HTML from that API without local persistence.

**Tech Stack:** Python 3, existing resident consumer, FastAPI/ASGI, current business RDS, public OpenAPI generator, docs-site, pytest.

## Global Constraints

- Create `feat/resident-usage-rds-upload` from the final P0-B head. Open a stacked PR whose base is `feat/provider-attempt-accounting`; retarget it to `test` after its parent merges.
- Telemetry defaults on. `FEEDLING_USAGE_TELEMETRY_ENABLED=false` is the operator opt-out. Missing `CONSUMER_ID` disables telemetry with a bounded warning; it never disables the resident.
- No disk queue, SQLite, local PostgreSQL, local report history, new RDS, Redis, Kafka, service, container, CVM, or deployment unit.
- Offline/restart/queue-full data loss is accepted and surfaced as a coverage gap. Never compensate by blocking chat, provider calls, replies, retries, heartbeat, maintenance, or shutdown.
- Server authentication—not request fields—selects `user_id`. Reject unknown/content-bearing fields and batches over 64.
- This changes the public API and trust boundary; OpenAPI, public docs, self-hosting privacy/trust documentation, and `Unreleased` changelog updates are mandatory in the same PR.

---

## Task 1: Add authenticated ingestion API

**Files:**
- Create: `backend/usage/__init__.py`
- Create: `backend/usage/usage_core.py`
- Create: `backend/usage/routes_asgi.py`
- Modify: `backend/asgi_app.py`
- Modify: `backend/provider_attempt_accounting.py`
- Create: `tests/test_asgi_usage.py`
- Create: `tests/test_usage_core.py`

- [ ] Write failing tests for `POST /v1/usage/provider-attempts`: authentication, JSON object shape, maximum 64 events, strict event allowlist, rejection of prompt/message/content/body/header/credential fields at any nesting level, numeric/string bounds, server-forced user identity, and idempotent replay.
- [ ] Add tests showing a client-supplied `user_id` cannot write another user's rows and that a valid batch returns accepted/deduplicated counts without echoing sensitive data.
- [ ] Implement framework-neutral validation in `usage_core.py`; route blocking RDS work through `asgi.threadpool.run_db`; register the package in `_NATIVE_PACKAGES`.
- [ ] Reuse P0-B full-row upsert semantics. API validation/storage errors may return 4xx/5xx to the telemetry client, but must not mutate unrelated business state.
- [ ] Run `pytest -q tests/test_usage_core.py tests/test_asgi_usage.py` and commit.

## Task 2: Replace resident diagnostic upload with default-on telemetry batching

**Files:**
- Modify: `tools/chat_resident_consumer.py`
- Test: `tests/test_chat_resident_consumer.py`
- Test: `tests/test_provider_attempt_ledger.py`

- [ ] Add failing tests for default enabled, explicit false opt-out, missing `CONSUMER_ID`, batch size 64, bounded queue, daemon/lazy worker, request timeout, retry/backoff bounds, process restart boot ID, monotonic `attempt_seq`, and deterministic attempt identity.
- [ ] Add failure-injection tests for queue full, serialization failure, HTTP timeout/401/429/500, worker crash, and shutdown with queued data; assert provider subprocess result, reply emission, retry decision, heartbeat, and main loop remain unchanged.
- [ ] Refactor the existing `_PROVIDER_ATTEMPT_QUEUE` path to post the new allowlisted batch contract to `/v1/usage/provider-attempts`. Do not write a fallback file or local database. Use boot ID/attempt sequence only to expose upload gaps, never as the idempotency key.
- [ ] Preserve or remove the legacy `/v1/debug/trace/event` provider-attempt route only according to compatibility tests; stop treating `user_logs` diagnostic entries as Usage data.
- [ ] Emit rate-limited operational counters/logs for enqueued, uploaded, dropped, validation-rejected, and last-success age without including content.
- [ ] Run focused resident tests and commit.

## Task 3: Add the authenticated user report and temporary HTML tool

**Files:**
- Modify: `backend/usage/usage_core.py`
- Modify: `backend/usage/routes_asgi.py`
- Create: `tools/resident_usage_report.py`
- Modify: `tests/test_asgi_usage.py`
- Create: `tests/test_resident_usage_report.py`

- [ ] Write failing tests for `GET /v1/usage/report`: authenticated-user scoping, UTC/custom range validation, provider/model filters, totals, attempts/retries/tokens/cache/reasoning/cost, possibly-billed counts, upload/sequence coverage, and no cross-user leakage.
- [ ] Implement a content-free JSON report from canonical ledger aggregates. Force `user_id` from auth; do not accept admin-style arbitrary user selection.
- [ ] Write CLI tests for fetching report JSON, handling auth/API/network errors, and generating a temporary standalone HTML file. Assert no persistent cache/database/history and safe HTML escaping.
- [ ] Implement `tools/resident_usage_report.py` with explicit API base/key inputs or existing resident environment conventions, restrictive temp-file permissions, optional browser opening only when explicitly requested, and cleanup guidance.
- [ ] Run report API/CLI tests and commit.

## Task 4: Integrate self-host visibility, deletion, and retention

**Files:**
- Modify: `backend/admin/usage.py`
- Modify: `backend/admin/data_track.py`
- Modify: `backend/provider_attempt_accounting.py`
- Modify: `backend/db.py`
- Modify: `tests/test_admin_usage.py`
- Modify: `tests/test_account_reset_purges_all_tables.py`

- [ ] Add failing Admin tests for source=`hosted_v2|resident`, per-source coverage, resident upload last-success/gap indicators, and combined provider/model/user rows without double counting Hosted attempts.
- [ ] Add account deletion and retention tests proving all resident attempt rows/corrections/watermarks disappear for the user while rate-card reference data remains.
- [ ] Extend Usage filters/rendering and ledger queries with source/consumer identity. Keep Hosted and resident coverage denominators distinct; never infer zero usage from missing uploads.
- [ ] Run Admin/account lifecycle tests and commit.

## Task 5: Publish the API contract and verify deployment safety

**Files:**
- Modify: `tools/public_openapi_contracts.py`
- Review and modify when needed: `tools/export_public_openapi.py`; the generated contract test must prove whether the existing route discovery already includes the new package
- Regenerate: `docs-site/openapi/public.json`
- Modify: `docs-site/content/docs/self-hosting.mdx`
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: `tests/openapi/test_public_openapi.py`

- [ ] Add OpenAPI contract tests for both usage endpoints, auth, request/response schemas, size limits, filters, errors, and content-free guarantees; run them RED before implementation metadata changes.
- [ ] Document default-on behavior, opt-out, current business RDS destination, data fields, accepted loss/coverage semantics, retention/deletion, and the explicit statement that no new local database or infrastructure is installed.
- [ ] From `docs-site`, run `npm run openapi:generate`, review the generated diff, then run `npm run types:check`, `npm run lint`, and `npm run build`.
- [ ] Run `pytest -q tests/openapi/test_public_openapi.py tests/test_usage_core.py tests/test_asgi_usage.py tests/test_resident_usage_report.py` plus resident/Admin/account tests and the full non-API suite.
- [ ] Simulate the ingestion API down and RDS writes failing while a resident turn, retry, reply, heartbeat, and maintenance tick complete normally. Record this evidence and the absence of new infrastructure/config requirements in the stacked PR.
- [ ] Record the final P0-B commit SHA before branching, inspect `git diff <recorded-p0-b-sha>...HEAD`, push `feat/resident-usage-rds-upload`, and open the stacked PR against `feat/provider-attempt-accounting`.
