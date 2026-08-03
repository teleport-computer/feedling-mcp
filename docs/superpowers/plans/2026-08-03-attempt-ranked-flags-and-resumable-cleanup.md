# Attempt Ranked Flags and Resumable Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the 3M-row call-membership rollup with exact ranked counters on existing daily dimensions, keep both formal cohorts strictly below 3 seconds, and make scale cleanup bounded and crash-resumable.

**Architecture:** The day builder computes deterministic first-match flags for every resolved-filter/completeness selector and stores 32 additive BIGINT counters on `llm_usage_daily_attempt_dimensions`. Report SQL sums those counters from the same 178k dimension rows used for attempts/tokens/cost; it never scans call IDs or a membership table. The scale harness deletes heavy user-owned rows in independent batches and exposes an ownership-checked recovery mode.

**Tech Stack:** Python 3.11, PostgreSQL 16, psycopg 3, Alembic, pytest, Ruff.

## Global Constraints

- Task 1 is a hard checkpoint. Any exactness, persistent-state, no-spill, build-time, or strict sub-3,000ms failure stops this plan before migration/production edits.
- Keep attempt statements at 3,000ms, the Usage deadline at 15,000ms, maintenance at its existing bounded timeout, and resume semantic statements at 180,000ms.
- Add no service, database, extension, queue, package, schema, or external analytics dependency.
- Do not mutate or clean retained prefix `scale_usage_42e02f444a_` during the TEMP experiment or ordinary implementation tests.
- Final unshipped revision 0077 must never create `llm_usage_daily_call_memberships` or related indexes, registry, query, retention, or cleanup paths.
- Never downgrade retained 0077 until a test database proves exact preservation of 0076 users, turns, attempts, corrections, and rate cards.
- Preserve exact optional user/lane/resolved-provider/resolved-model/completeness filters, requested/resolved breakdowns, and full-call outer/inner gaps.
- Never stage the two retained untracked evidence JSON files.

---

### Task 1: Non-persistent 3M TEMP feasibility checkpoint

**Files:**
- Create: `scripts/perf/admin_usage_ranked_flags_temp_probe.py`
- Modify: `tests/test_admin_usage.py`
- Modify: `.superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md`

**Interfaces:**
- `FLAG_COLUMN_NAMES: tuple[str, ...]`: exact 32-column matrix.
- `_ranked_flag_ctes(*, priced_relation: str, gap_relation: str) -> str`: diagnostic-only 16-rank CTE.
- `_run_probe(database_url: str, *, output: Path) -> dict[str, Any]`: TEMP-only scale probe.

- [ ] **Step 1: Write matrix and SQL-boundary RED tests**

```python
def test_ranked_probe_declares_exact_32_column_matrix():
    expected = tuple(
        f"{metric}_{selector}_{completeness}"
        for selector in ("all", "provider", "model", "provider_model")
        for completeness in ("all", "effective")
        for metric in (
            "logical_calls_cohort", "logical_calls_requested",
            "missing_outer_ordinals", "missing_inner_ordinals",
        )
    )
    assert ranked_probe.FLAG_COLUMN_NAMES == expected
    assert len(set(expected)) == 32

def test_ranked_probe_uses_16_stable_ranks_and_no_persistent_ddl():
    sql = ranked_probe._ranked_flag_ctes(
        priced_relation="priced", gap_relation="call_gaps"
    )
    assert sql.count("row_number() OVER") == 16
    assert sql.count("ORDER BY attempt_id") == 16
    assert "CREATE TABLE" not in sql
    assert "llm_usage_daily_call_memberships" not in sql
```

- [ ] **Step 2: Run RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_admin_usage.py::test_ranked_probe_declares_exact_32_column_matrix \
  tests/test_admin_usage.py::test_ranked_probe_uses_16_stable_ranks_and_no_persistent_ddl -q
```

Expected: import/collection failure because the probe module is absent.

- [ ] **Step 3: Implement the minimal diagnostic rank generator**

Use these literal partitions; append requested identity only for requested ranks:

```python
SELECTOR_PARTITIONS = {
    "all": (),
    "provider": ("resolved_provider",),
    "model": ("resolved_model",),
    "provider_model": ("resolved_provider", "resolved_model"),
}
COMPLETENESS_PARTITIONS = {
    "all": (),
    "effective": ("effective_usage_known",),
}
```

Every rank orders by immutable unique `attempt_id`. Gap expressions multiply
the full-call gap by the matching cohort rank-one predicate.

- [ ] **Step 4: Run Step 2 and verify GREEN**

Expected: 2 passed.

- [ ] **Step 5: Write adversarial PostgreSQL RED tests**

Seed `call-a` across p1/shared known and p2/shared unknown with outer ordinals
1,3; seed `call-b` across p1/m1 and p1/m2 with inner ordinals 1,3. Compare
ranked output to canonical raw/membership truth for all combinations of
user absent/present, lane absent/present, provider absent/p1/p2, model
absent/shared/m1, and completeness all/metered/unknown. Compare overview,
user, lane, requested models, resolved models, and both gap fields.

- [ ] **Step 6: Run adversarial RED, implement TEMP builder/candidate, then GREEN**

The builder must set `temp_buffers='8MB'` before creating TEMP objects, build
`pg_temp.admin_usage_ranked_dimensions` one Shanghai day at a time from the
current corrected/priced source, add only TEMP user/day and resolved/day
indexes, ANALYZE, and use the same rank generator for raw partial days. Run the
named parameterized test until every output is exactly equal.

- [ ] **Step 7: Run the retained 3M TEMP experiment**

```bash
SCALE_PROBE_DSN='postgresql://postgres:test@127.0.0.1:55432/feedling_usage_scale_task4d' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  scripts/perf/admin_usage_ranked_flags_temp_probe.py \
  --output /private/tmp/2026-08-03-ranked-flags-temp-probe.json
```

Execute each old cohort once at diagnostic 180,000ms; compare exact metric-row
sets. Execute candidate ordinary SQL at 3,000ms, then `EXPLAIN (ANALYZE,
BUFFERS, FORMAT JSON, TIMING OFF)` twice for cold-local-buffer and warm samples.

- [ ] **Step 8: Enforce the hard gate**

Require TEMP rows=731,199; exact old/new outputs; ordinary/cold/warm each
<3,000ms; plan Temp Read/Write Blocks=0; no membership/call-ID/distinct path;
8,208 bounded raw calls/probes; largest day build below maintenance timeout;
recorded build total/p50/p95/max and TEMP heap/index/total bytes; exact
persistent pre/post counts, source checksums, and watermarks; after closing the
session, no non-temp probe object. On any failure append measurements to the
report, notify the user, and stop before Task 2.

- [ ] **Step 9: Verify and commit PASS checkpoint**

Run focused tests, Ruff, compileall, harness self-test, and `git diff --check`.
Append all measurements and non-persistence proof to the report.

```bash
git add scripts/perf/admin_usage_ranked_flags_temp_probe.py tests/test_admin_usage.py
git add -f .superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md
git commit -m "perf: prove ranked attempt flags at scale"
```

---

### Task 2: Final 0077 schema and source-preserving downgrade proof

**Files:**
- Modify: `backend/alembic/versions/0077_llm_usage_attempt_rollups.py`
- Modify: `tests/test_provider_attempt_rollup_migration.py`
- Modify: `backend/tee_shadow/table_registry.py`
- Modify: `tests/test_tee_table_registry.py`

**Interfaces:**
- `RANKED_FLAG_COLUMNS` equals Task-1 matrix byte-for-byte.
- Upgrade creates dimensions + dirty days, never memberships.
- Downgrade removes only revision-0077 derived objects/columns.

- [ ] **Step 1: Write exact-schema and source-witness RED tests**

Require 32 BIGINT/non-null/default-zero columns, logical-call `<= attempts` and
nonnegative gap checks, membership table/index/registry absence. In an isolated
test DB seed a user, turn, attempt, correction, and rate card; capture ordered
count+MD5 witnesses for `users`, `v2_turn_metrics`, `llm_provider_attempts`,
`llm_provider_attempt_corrections`, and `llm_rate_cards`. Downgrade to exact
0076 and require identical witnesses; upgrade modified 0077 and require them
again.

- [ ] **Step 2: Run RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_provider_attempt_rollup_migration.py tests/test_tee_table_registry.py -q
```

Expected: old 0077 creates memberships and lacks flags.

- [ ] **Step 3: Implement final unshipped migration and registry**

Delete membership DDL, three indexes, index targets, downgrade drop, and
registry entry. Generate exact 32 columns/constraints. Retain existing
dimension user/resolved and attempt/rate maintenance indexes.

- [ ] **Step 4: Run GREEN and commit**

Require schema, wrong-owner protections, downgrade/upgrade source witnesses,
and registry tests all pass.

```bash
git add backend/alembic/versions/0077_llm_usage_attempt_rollups.py \
  tests/test_provider_attempt_rollup_migration.py \
  backend/tee_shadow/table_registry.py tests/test_tee_table_registry.py
git commit -m "feat(accounting): store ranked call flags in daily dimensions"
```

---

### Task 3: Production ranked day builder and lifecycle

**Files:**
- Modify: `backend/model_api_runtime/v2/provider_attempt_rollup.py`
- Modify: `tests/test_provider_attempt_rollup.py`
- Modify: `tests/test_provider_attempt_rollup_reconciler.py`

**Interfaces:**
- `_ranked_attempt_ctes(priced_relation="priced", gap_relation="call_gaps") -> str`.
- `recompute_local_day(...) -> {"status": "ok", "dimensions": int}`.
- `_assert_call_cohort_invariant(...)` rejects cross-user/lane/day calls.

- [ ] **Step 1: Write RED facts/invariant/lifecycle tests**

Assert all 32 literal values for failover, mixed completeness, requested change,
gap, correction, and cost split; exactly one dimension owns each representative;
repeat rebuild is identical. Add cross-user/lane/day violations and require old
dimensions preserved, dirty work retained, watermark unchanged. Add correction,
rate replay, CAS race, turn-day move, and retention tests with no memberships.

- [ ] **Step 2: Run RED**

Run both provider rollup modules. Expected: columns/ranks/invariant absent and
membership lifecycle remains.

- [ ] **Step 3: Implement minimal production builder/lifecycle**

Port the proven 16 ranks after corrected/priced/full-call gaps; aggregate 32
values into the existing dimension insert. Validate one user/lane/Shanghai day
per call before publication. Remove membership insert/delete/count/retention.
Keep atomic day replacement and dirty-generation CAS order unchanged.

- [ ] **Step 4: Run GREEN and commit**

```bash
git add backend/model_api_runtime/v2/provider_attempt_rollup.py \
  tests/test_provider_attempt_rollup.py tests/test_provider_attempt_rollup_reconciler.py
git commit -m "feat(accounting): rank logical calls during daily rebuild"
```

---

### Task 4: Production report reads ranked dimensions only

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `tests/test_admin_usage.py`

**Interfaces:**
- `_usage_attempt_selector_suffix(query) -> tuple[str, str]` returns selector and all/effective.
- `_usage_attempt_query` has no membership/call-ID/distinct rollup path.

- [ ] **Step 1: Write exhaustive production RED tests**

Reuse Task-1 adversarial rows for every filter/completeness combination and all
outputs. Assert SQL chooses cohort flags for overview/user/lane/gaps, requested
flags for requested identity, and pair-cohort flags for resolved identity.
Assert membership, call ID, and `count(DISTINCT` are absent.

- [ ] **Step 2: Run RED, implement ranked sums/raw ranks, then GREEN**

Keep the existing selected-dimension WHERE. Map filter shape to all/provider/
model/pair and completeness to all/effective. Build raw-edge dimensions with
the shared rank helper. Preserve denominator/retention reasons. If Task-1 plan
shows the existing additive five-scope expansion spills, replace only that
expansion with measured grouping sets or bounded scope UNIONs.

- [ ] **Step 3: Run all admin Usage tests and commit**

```bash
git add backend/model_api_runtime/v2/jobs_store.py tests/test_admin_usage.py
git commit -m "perf(admin): read logical calls from ranked dimensions"
```

---

### Task 5: Scale exact proof, plan guards, and twelve counters

**Files:**
- Modify: `scripts/perf/admin_usage_scale.py`
- Modify: `tests/test_admin_usage.py`
- Modify: `.superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md`

**Interfaces:**
- Resume proof compares all 32 source-derived values bidirectionally.
- Removed membership count is replaced by virtual `ranked_attempt_dimension_rows`, preserving twelve evidence keys.

- [ ] **Step 1: Write RED schema/semantic/plan tests**

Mutate one flag without changing row counts and require resume rejection.
Require 32 columns, membership absence, dimensions + bounded raw probes, no
call-ID/distinct/temp spill/timeout increase. Assert the virtual ranked-row
counter is nonzero with ranked dimensions and zero after their deletion.

- [ ] **Step 2: Run RED, implement exact proof/gates, then GREEN**

Replace membership integrity with a source-derived 32-column comparison under
180s. Update expected formal counts, relation stats, cleanup maps, and plan
guards. Keep twelve keys with `ranked_attempt_dimension_rows`.

- [ ] **Step 3: Verify and commit**

Append ranked semantic evidence and unchanged budgets to the report; run focused
tests/Ruff/compileall/self-test/diff-check before commit.

---

### Task 6: Bounded idempotent cleanup and explicit recovery

**Files:**
- Modify: `scripts/perf/admin_usage_scale.py`
- Modify: `tests/test_admin_usage.py`
- Modify: `.superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md`

**Interfaces:**
- `_delete_fixture_batches(conn_factory, *, prefix, batch_size=10, cleanup_statement_timeout_ms=120_000, progress) -> dict`.
- CLI `--recover-cleanup`; normal `--resume` remains exact-complete-only.

- [ ] **Step 1: Write RED batch/progress/recovery tests**

Inject timeout in batch two: batch one stays committed, batch two rolls back,
size halves only to one, timeout never rises, atomic progress is parseable and
contains no IDs, retry reaches exact zero. Require normal resume to reject
partial state. Recovery must reject invalid prefix/database identity, any
non-prefix user-owned row, foreign watermark/dirty row, or reference data.

- [ ] **Step 2: Run RED, implement child-first batches, then GREEN**

Per transaction set local cleanup timeout and delete corrections, ranked
dimensions, V2 daily dimensions/users, attempts, turns, then users using
indexed `user_id = ANY(%s)`. Checkpoint after commit. Delete named watermarks/
dirty rows only after no prefix users remain; ANALYZE; require twelve zeros.
Never auto-convert normal resume into recovery.

- [ ] **Step 3: Run real `_run` entry scenarios and commit**

Cover timing cleanup, batch failure, process interruption, explicit recovery,
producer prohibition, and successful zero through real atomic output.

```bash
git add scripts/perf/admin_usage_scale.py tests/test_admin_usage.py
git add -f .superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md
git commit -m "fix(perf): batch and resume scale cleanup"
```

---

### Task 7: Full verification and safe retained 0077 replacement

**Files:**
- Verify all implementation files.
- Modify report only with measured retained-database evidence.

- [ ] **Step 1: Run focused PostgreSQL and static suites**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_provider_attempt_rollup_migration.py tests/test_provider_attempt_rollup.py \
  tests/test_provider_attempt_rollup_reconciler.py tests/test_tee_table_registry.py \
  tests/test_admin_usage.py -q
uv run ruff check backend/alembic/versions/0077_llm_usage_attempt_rollups.py \
  backend/model_api_runtime/v2/provider_attempt_rollup.py \
  backend/model_api_runtime/v2/jobs_store.py backend/tee_shadow/table_registry.py \
  scripts/perf/admin_usage_ranked_flags_temp_probe.py scripts/perf/admin_usage_scale.py \
  tests/test_provider_attempt_rollup_migration.py tests/test_provider_attempt_rollup.py \
  tests/test_provider_attempt_rollup_reconciler.py tests/test_tee_table_registry.py \
  tests/test_admin_usage.py
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  scripts/perf/admin_usage_scale.py --self-test
git diff --check
```

- [ ] **Step 2: Run a fresh 100-row temporary-database workflow**

Migrate modified head, seed/bootstrap, run both cohorts and all cleanup/business
phases, require ranked exactness and twelve zero maps, then drop only that exact
temporary database. Never target retained DB.

- [ ] **Step 3: Capture retained source preflight**

In read-only repeatable read capture DB identity, prefix ownership, Alembic
version, counts and ordered MD5 witnesses for users/turns/attempts/corrections/
rate cards. Abort if they differ from formal artifact.

- [ ] **Step 4: Re-run test downgrade proof, then downgrade retained 0077 only**

Freshly pass the Task-2 source-witness test. With explicit local retained DSN,
downgrade to `0076_llm_provider_attempts`; immediately require all five retained
source witnesses unchanged. Only 0077 derived objects may disappear.

- [ ] **Step 5: Upgrade modified 0077 and rebootstrap from source**

Upgrade head; prove 32 columns and membership absence; run bounded normal
attempt-rollup bootstrap. Never seed/delete/clean users. Record ticks, build
p50/p95/max, rows/bytes, watermarks, source witnesses, and all 32 semantic
mismatch counts.

- [ ] **Step 6: Run retained read-only cohort and resume proof**

Require both production cohorts strict sub-3s ordinary/p50/p95 and cold/warm
EXPLAIN, zero spill, 178k dimensions, bounded 8,208 raw edge, no membership/
call-ID distinct, then exact `--validate-resume-only`. The full fixture stays.

---

### Task 8: Final review gate

**Files:**
- Review spec, plan, commits, and load report.

- [ ] **Step 1: Re-run Task-7 tests/static checks fresh**

Every command must exit zero at current HEAD.

- [ ] **Step 2: Verify constraints and branch state**

Confirm current RDS only, no dependency/infrastructure, default-on best-effort
behavior unchanged, provider request path unaffected, timeouts unchanged,
memberships absent, and only the two retained evidence JSON files untracked.

- [ ] **Step 3: Stop before formal cleanup**

Report commits, exactness, timings, storage, downgrade/source witnesses, and
retained fixture status. Do not run another formal workflow or delete the 3M
prefix without separate direction after this gate.
