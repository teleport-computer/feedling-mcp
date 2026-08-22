# TEE Dirty-Key Sync Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make normal TEE/plaintext-shadow convergence proportional to changed keys and remove recurring full-table SNAPSHOT and random-order verification work.

**Architecture:** Extend the generation-safe dirty-key outbox so SNAPSHOT tables reconcile exact key batches, while CIPHERTEXT and MIRROR retain their key handlers. Keep cursor replication and confirmed full snapshots as recovery paths; use deterministic samples and rolling hash buckets for ongoing verification.

**Tech Stack:** Python 3.11+, PostgreSQL 16, psycopg 3, TEE direct-TLS PostgreSQL, pytest.

**Spec:** `docs/superpowers/specs/2026-08-22-rds-egress-and-tee-incremental-sync-design.md`

## Global Constraints

- Dirty markers contain primary-key material only, never row documents or decrypted content.
- Generation-safe acknowledgement prevents an older apply from deleting a newer marker.
- Normal scheduler ticks never invoke full SNAPSHOT-table COPY.
- Confirmed full snapshot and cursor backfill remain available for bootstrap and recovery.
- Parent-first ordering, FK integrity, retry, lease, and quarantine remain fail-visible.
- Public APIs and TEE trust/encryption boundaries do not change.

## File map

- `backend/tee_shadow/snapshot.py`: batched key-level SNAPSHOT reconciliation.
- `backend/plaintext_shadow/outbox.py`: group and apply dirty keys in dependency order.
- `backend/tee_shadow/table_registry.py`: stable registry-derived apply order.
- `backend/admin/tee_sync_scheduler.py`: bounded cursor recovery without normal snapshots.
- `backend/admin/plaintext_shadow_scheduler.py`: dirty drain, audit, scheduling, and metrics.
- `backend/tee_shadow/verify.py`, `backend/admin/plaintext_shadow.py`: deterministic sample and rolling audit.
- `docs/ops/tee-incremental-sync-runbook.md`: cutover, rollback, bootstrap, and repair.

---

### Task 1: SNAPSHOT key-level reconciliation

**Files:**
- Modify: `backend/tee_shadow/snapshot.py`
- Modify: `tests/test_tee_snapshot.py`
- Modify: `tests/test_plaintext_shadow_target.py`

**Interfaces:**
- Produces `snapshot.reconcile_keys(table: str, keys: list[dict], *, target_policy: TargetPolicy | None = None) -> dict`.
- Returns fixed scalars `table`, `claimed`, `applied`, `deleted`, `missing`, and `ok`.

- [ ] **Step 1: Write real-Postgres RED tests**

Cover single/composite keys, update, missing-source delete, duplicate keys, empty input, unknown table, non-SNAPSHOT rejection, common-column intersection, rollback after mutation, and a mixed update/delete batch.

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_tee_snapshot.py tests/test_plaintext_shadow_target.py -q
```

- [ ] **Step 3: Implement validated key normalization**

Read `entry.key_columns`; reject missing or extra fields; canonicalize and deduplicate tuples; cap at 500. Compose all identifiers with `psycopg.sql.Identifier`.

- [ ] **Step 4: Implement batched source read and atomic target mutation**

Use `= ANY(%s)` for one-column keys and a typed temporary-key table for composite keys. Fetch common columns, stage existing source rows, UPSERT them, and delete only claimed target keys absent from the stage, all in one target transaction.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/tee_shadow/snapshot.py tests/test_tee_snapshot.py tests/test_plaintext_shadow_target.py
git commit -m "feat(tee): reconcile snapshot tables by dirty key"
```

---

### Task 2: Batch and dependency-order dirty-key draining

**Files:**
- Modify: `backend/plaintext_shadow/outbox.py`
- Modify: `backend/tee_shadow/table_registry.py`
- Modify: `tests/test_plaintext_shadow_outbox.py`
- Modify: `tests/test_plaintext_shadow_schema.py`

**Interfaces:**
- Produces `table_registry.apply_order(tables: Iterable[str]) -> tuple[str, ...]`.
- `outbox.drain_once(limit=500)` calls Task 1 once per SNAPSHOT table batch.
- CIPHERTEXT continues through `worker.run_keys`; MIRROR through `reconciler.reconcile_keys`.

- [ ] **Step 1: Write RED batching/order tests**

Assert 100 same-table markers make one SNAPSHOT call, parents precede children, one batch success acks every represented generation, a changed generation blocks only its stale ack, FK failure retries, unrelated tables proceed, and quarantine remains per key.

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_plaintext_shadow_outbox.py tests/test_plaintext_shadow_schema.py -q
```

- [ ] **Step 3: Add stable parent-first ordering**

Derive dependencies from the registry/FK information already used by snapshot ordering. Reject dependency cycles at startup and sort independent tables alphabetically.

- [ ] **Step 4: Refactor drain into table batches**

Claim as today, group by table, process in `apply_order`, fold one batch report, then generation-safe ack each marker. Preserve five-minute lease, exponential delay, 20-attempt quarantine, and fixed error slugs.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/plaintext_shadow/outbox.py backend/tee_shadow/table_registry.py tests/test_plaintext_shadow_outbox.py tests/test_plaintext_shadow_schema.py
git commit -m "perf(tee): batch dirty keys in dependency order"
```

---

### Task 3: Remove full snapshots from normal scheduling

**Files:**
- Modify: `backend/admin/tee_sync_scheduler.py`
- Modify: `backend/admin/plaintext_shadow_scheduler.py`
- Modify: `tests/test_tee_sync_scheduler.py`
- Modify: `tests/test_plaintext_shadow_scheduler.py`
- Modify: `tests/test_tee_sync_metrics.py`

**Interfaces:**
- Produces validated `FEEDLING_TEE_SYNC_MODE=legacy|observe|incremental`.
- Incremental mode drains dirty keys every 30 seconds and runs bounded CIPHERTEXT cursor recovery every 300 seconds.
- Full snapshot remains available only through the existing confirmed admin action.

- [ ] **Step 1: Write scheduler RED tests**

Assert incremental first/normal ticks never call `run_action(action="snapshot")`, cursor replication still runs, manual snapshot works, invalid mode fails startup, observe mode does not mutate target, and two schedulers cannot overlap target mutation.

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_tee_sync_scheduler.py tests/test_plaintext_shadow_scheduler.py tests/test_tee_sync_metrics.py -q
```

- [ ] **Step 3: Make plaintext scheduler the normal orchestrator**

Let the elected plaintext scheduler drain keys and invoke a bounded cursor-recovery helper at five-minute cadence. Reuse the existing global replication run lock for every target mutation. Legacy mode retains current behavior for rollback.

- [ ] **Step 4: Remove snapshot calls in incremental mode**

Remove pre-replicate full snapshot and post-replicate targeted snapshot from incremental `_sync_tick`. Keep compatible report fields at zero. Do not alter the confirmed admin snapshot implementation.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/admin/tee_sync_scheduler.py backend/admin/plaintext_shadow_scheduler.py tests/test_tee_sync_scheduler.py tests/test_plaintext_shadow_scheduler.py tests/test_tee_sync_metrics.py
git commit -m "perf(tee): remove full snapshots from normal sync ticks"
```

---

### Task 4: Deterministic verification and rolling audit

**Files:**
- Modify: `backend/tee_shadow/verify.py`
- Modify: `backend/admin/plaintext_shadow.py`
- Modify: `backend/admin/plaintext_shadow_scheduler.py`
- Modify: `tests/test_tee_verify.py`
- Modify: `tests/test_admin_plaintext_shadow.py`
- Modify: `tests/test_plaintext_shadow_scheduler.py`

**Interfaces:**
- Produces `verify.deterministic_keys(table: str, *, seed: int, bucket: int, bucket_count: int, limit: int) -> list[dict]`.
- Produces `verify.compare_keys(table: str, keys: list[dict], *, target_policy=None) -> dict`.
- Runs a bounded hourly sample and one of seven full-keyspace buckets daily.

- [ ] **Step 1: Write RED verification tests**

Assert generated SQL contains no `random()`, identical seed/bucket returns identical keys, seven buckets are disjoint and cover all fixture keys, and comparison distinguishes missing source, missing target, and normalized-content mismatch.

- [ ] **Step 2: Run focused tests and confirm RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_tee_verify.py tests/test_admin_plaintext_shadow.py tests/test_plaintext_shadow_scheduler.py -q
```

- [ ] **Step 3: Implement stable sampling and comparison**

Use `hashtextextended` over canonical primary-key text, modulo bucket selection, primary-key ordering, and bounded hourly limits. Read identical keys on both sides and compare canonical hashes without logging bodies.

- [ ] **Step 4: Schedule and persist audit progress**

Derive the daily bucket from UTC date modulo seven. Persist bucket, keys checked, missing/mismatch counts, duration, and pass/fail so failures are reproducible.

- [ ] **Step 5: Run tests and commit**

```bash
git add backend/tee_shadow/verify.py backend/admin/plaintext_shadow.py backend/admin/plaintext_shadow_scheduler.py tests/test_tee_verify.py tests/test_admin_plaintext_shadow.py tests/test_plaintext_shadow_scheduler.py
git commit -m "perf(tee): verify shadow state with deterministic keys"
```

---

### Task 5: Metrics, operations, and rollout

**Files:**
- Modify: `backend/admin/plaintext_shadow_scheduler.py`
- Modify: `tests/test_plaintext_shadow_scheduler.py`
- Create: `docs/ops/tee-incremental-sync-runbook.md`
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`

**Interfaces:**
- Produces bounded pending-age, apply, quarantine, target-probe, audit-progress, mismatch, and full-snapshot-reason metrics.
- Produces exact observe/incremental/legacy, bootstrap, and recovery procedures.

- [ ] **Step 1: Add metric privacy and snapshot-alarm tests**

Assert metrics contain fixed scalars only, never key JSON or row documents. Any snapshot outside `bootstrap`, `manual_recovery`, or `legacy_mode` increments an error counter.

- [ ] **Step 2: Implement metrics and runbook**

Document trigger audit, observe comparison, backlog drain, strict audit, rollback, quarantine repair, explicit full seed with starting generation, post-seed dirty catch-up, and daily/weekly audit interpretation.

- [ ] **Step 3: Update architecture and Unreleased changelog**

Describe key-level normal convergence, retained cursor/full-snapshot recovery, and unchanged trust boundary.

- [ ] **Step 4: Run related and repository verification**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_tee_snapshot.py tests/test_plaintext_shadow_outbox.py tests/test_plaintext_shadow_target.py tests/test_tee_sync_scheduler.py tests/test_plaintext_shadow_scheduler.py tests/test_tee_verify.py tests/test_tee_sync_metrics.py -q
~/fleet/bus/which_tests.sh --vs origin/test
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests -q --ignore=tests/test_api.py
cd docs-site && npm run types:check && npm run lint && npm run build
```

Expected: focused tests and docs pass; full suite adds no failures beyond the verified DB-backed baseline.

- [ ] **Step 5: Commit and deploy through test**

```bash
git add backend/admin/plaintext_shadow_scheduler.py tests/test_plaintext_shadow_scheduler.py docs/ops/tee-incremental-sync-runbook.md docs-site/content/docs/architecture.mdx docs-site/content/docs/changelog.mdx
git commit -m "docs(tee): add incremental shadow operations and gates"
```

Merge into `test` and require trigger audit green, quarantine zero, mismatch zero, and oldest dirty age below five minutes. In production enable observe, then incremental, drain backlog, run strict audit, and only then disable automatic full snapshots. Require normal `snapshot_copied=0`, scheduler duration in seconds, and no RDS NetworkTransmit rebound.
