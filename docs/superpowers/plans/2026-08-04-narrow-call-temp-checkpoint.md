# Narrow Call Dimensions TEMP Checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove or reject the narrow daily call-dimension design on the retained local 3M fixture without changing persistent schema or production code.

**Architecture:** A diagnostic script builds one session-local eight-identity-plus-32-flags relation directly from corrected/priced source attempts. One candidate statement independently aggregates existing persistent attempt facts and TEMP call flags, compares A1 bounded unions with A2 grouping sets under an interleaved order, and fails closed unless every exactness, storage, no-spill, timing, raw-bound, and non-persistence gate passes.

**Tech Stack:** Python 3.11, PostgreSQL 16, psycopg 3, pytest, Ruff.

## Global Constraints

- Modify only diagnostic scripts, focused tests, and the Task-5 report. Do not modify `backend/`, Alembic 0077, TEE registry, production report SQL, deployment, or public docs.
- Keep one attempt-ledger SQL statement and require its total execution strictly below 3,000ms.
- Keep reference statements at 180,000ms and every day build below 120,000ms.
- Add no service, queue, extension, package, schema, or analytics dependency.
- Create only session-local TEMP relations. Persistent counts, source checksums, and watermarks must remain identical on pass or failure.
- Never clean, downgrade, migrate, or mutate retained prefix `scale_usage_42e02f444a_`.
- Require exactly 731,199 TEMP rows on the retained fixture and never more rows than the corresponding fact dimensions.
- Require TEMP heap plus indexes at most 700,000,000 bytes and strictly below 705,099,776 bytes.
- Require exactly 8,208 raw attempts and 8,208 raw logical calls.
- Interleave shapes: unfiltered A1 then A2; provider/model-filtered A2 then A1.
- Name the first sample `first_execution_after_build_analyze`; never claim physical cold-cache behavior.
- A shape is eligible only when all six first/warm/ordinary samples are exact, strictly below 3,000ms, and have zero Temp Read/Write Blocks.
- If neither A1 nor A2 is eligible, or any non-query hard gate fails, stop without production work.
- Keep both existing evidence JSON files untracked and unstaged.

---

### Task 1: Narrow TEMP schema and storage boundaries

**Files:**
- Create: `scripts/perf/admin_usage_narrow_call_temp_probe.py`
- Modify: `tests/test_admin_usage.py`

**Interfaces:**
- Consumes: `FLAG_COLUMN_NAMES` and `_ranked_rows_and_outputs` from `scripts/perf/admin_usage_ranked_flags_temp_probe.py`.
- Produces: `NARROW_IDENTITY_COLUMNS: tuple[str, ...]`.
- Produces: `_narrow_table_ddl(*, relation: str) -> tuple[str, ...]`.
- Produces: `_narrow_storage_passed(stats: dict[str, int], *, membership_total_bytes: int) -> bool`.

- [ ] **Step 1: Write exact-schema and storage RED tests**

Add `_load_narrow_call_probe()` beside the ranked-probe loader, then add:

```python
def test_narrow_call_probe_schema_contains_only_identity_and_flags():
    probe = _load_narrow_call_probe()
    assert probe.NARROW_IDENTITY_COLUMNS == (
        "local_day", "user_id", "cohort_lane",
        "requested_provider", "requested_model",
        "resolved_provider", "resolved_model", "effective_usage_known",
    )
    statements = probe._narrow_table_ddl(
        relation="admin_usage_daily_call_dimensions"
    )
    sql = " ".join(statements).lower()
    assert sql.count("create temp table") == 1
    assert sql.count("create") == 4
    assert all(name in sql for name in probe.FLAG_COLUMN_NAMES)
    for forbidden in (
        "call_id", "attempts", "input_tokens", "cost_kind", "currency",
        "ttft_samples", "refreshed_at", "llm_usage_daily_call_memberships",
    ):
        assert forbidden not in sql


def test_narrow_call_probe_storage_gate_requires_both_byte_limits():
    probe = _load_narrow_call_probe()
    healthy = {"heap_bytes": 330_000_000, "index_bytes": 320_000_000,
               "total_bytes": 650_000_000}
    assert probe._narrow_storage_passed(
        healthy, membership_total_bytes=2_820_399_104
    ) is True
    assert probe._narrow_storage_passed(
        {**healthy, "total_bytes": 700_000_001},
        membership_total_bytes=2_820_399_104,
    ) is False
    assert probe._narrow_storage_passed(
        healthy, membership_total_bytes=2_400_000_000
    ) is False
```

- [ ] **Step 2: Run RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_admin_usage.py::test_narrow_call_probe_schema_contains_only_identity_and_flags \
  tests/test_admin_usage.py::test_narrow_call_probe_storage_gate_requires_both_byte_limits -q
```

Expected: both fail because the narrow probe module is absent.

- [ ] **Step 3: Implement the minimal schema generator and storage gate**

Create the script with these literal contracts:

```python
from scripts.perf.admin_usage_ranked_flags_temp_probe import (
    FLAG_COLUMN_NAMES,
    _ranked_rows_and_outputs,
)

NARROW_IDENTITY_COLUMNS = (
    "local_day", "user_id", "cohort_lane",
    "requested_provider", "requested_model",
    "resolved_provider", "resolved_model", "effective_usage_known",
)
MAX_NARROW_TOTAL_BYTES = 700_000_000
MAX_MEMBERSHIP_RATIO = Decimal("0.25")

def _narrow_storage_passed(stats, *, membership_total_bytes):
    total = int(stats["total_bytes"])
    return (
        total <= MAX_NARROW_TOTAL_BYTES
        and Decimal(total) < Decimal(membership_total_bytes) * MAX_MEMBERSHIP_RATIO
    )
```

`_narrow_table_ddl` validates relation names with `^[a-z_][a-z0-9_]*$`, emits
all flags as `BIGINT NOT NULL DEFAULT 0 CHECK (flag >= 0)`, and emits exactly
the approved unique, user/day, and resolved/day TEMP indexes. No index includes
any flag.

- [ ] **Step 4: Run GREEN and PostgreSQL schema checks**

Add a test that executes all four DDL statements inside a test transaction,
queries `pg_attribute` for exact columns/types, queries `pg_index` for exact
keys/includes, and requires `relpersistence='t'`. Run Step 2 plus this test.

- [ ] **Step 5: Commit**

```bash
git add scripts/perf/admin_usage_narrow_call_temp_probe.py tests/test_admin_usage.py
git commit -m "test(perf): define narrow call TEMP schema"
```

---

### Task 2: Exact narrow day builder and adversarial truth

**Files:**
- Modify: `scripts/perf/admin_usage_narrow_call_temp_probe.py`
- Modify: `tests/test_admin_usage.py`

**Interfaces:**
- Consumes: Task-1 identities, flags, and TEMP relation.
- Produces: `_narrow_dimension_select(*, priced_relation: str, gap_relation: str, include_local_day: bool = False) -> str`.
- Produces: `_narrow_day_insert_sql(effective_ctes: str) -> str`.
- Produces: `_narrow_raw_insert_sql(effective_ctes: str) -> str`.

- [ ] **Step 1: Write narrow-grain RED tests**

Reuse the existing five-attempt adversarial rows and add two rows sharing all
narrow identities while differing in `cost_kind` and `currency`. Require the
narrow aggregation to merge the cost split while preserving canonical
overview/requested/resolved/gap totals. Retain the complete
user/lane/provider/model/completeness Cartesian loop.

```python
assert len(narrow_rows) < len(wide_cost_rows)
assert sum(row["logical_calls_cohort_all_all"] for row in narrow_rows) == 3
assert sum(row["logical_calls_requested_all_all"] for row in narrow_rows) == 4
assert sum(row["missing_outer_ordinals_all_all"] for row in narrow_rows) == 1
assert sum(row["missing_inner_ordinals_all_all"] for row in narrow_rows) == 1
```

- [ ] **Step 2: Run RED**

Run the new narrow exactness test. Expected: `_narrow_dimension_select` is
absent.

- [ ] **Step 3: Implement minimal narrow aggregation**

Call `_ranked_rows_and_outputs`, select only the eight identities plus 32 flag
expressions, and aggregate flags with `sum(... )::bigint`. The final SELECT may
not project attempt facts, cost, currency, TTFT, or `call_id`.

The day insert prepends the existing corrected/priced CTEs, computes full-call
gaps, and inserts an explicit identity-plus-flags column list. The raw insert
derives Shanghai local day from matched turn metrics but uses identical ranks.

- [ ] **Step 4: Execute empty-PostgreSQL smoke and GREEN tests**

Create the TEMP tables, execute an empty-day insert, execute raw insert with
`cohort_where='false'`, and require zero rows from both. Run the adversarial
matrix and require exact equality.

- [ ] **Step 5: Commit**

```bash
git add scripts/perf/admin_usage_narrow_call_temp_probe.py tests/test_admin_usage.py
git commit -m "test(perf): aggregate exact narrow call flags"
```

---

### Task 3: A1/A2 single-statement candidates and selection

**Files:**
- Modify: `scripts/perf/admin_usage_narrow_call_temp_probe.py`
- Modify: `tests/test_admin_usage.py`

**Interfaces:**
- Produces: `_candidate_query(*, shape: str, selector: str, completeness: str) -> str` for `bounded_unions` and `grouping_sets`.
- Produces: `_shape_order(cohort: str) -> tuple[str, str]`.
- Produces: `_shape_eligible(shape_evidence: dict[str, Any]) -> bool`.
- Produces: `_select_shape(cohorts: dict[str, Any]) -> str | None`.

- [ ] **Step 1: Write query-boundary, ordering, and selection RED tests**

```python
@pytest.mark.parametrize("shape", ("bounded_unions", "grouping_sets"))
def test_narrow_candidate_is_one_statement_without_forbidden_paths(shape):
    probe = _load_narrow_call_probe()
    sql = " ".join(probe._candidate_query(
        shape=shape, selector="provider_model", completeness="all"
    ).split()).lower()
    assert sql.startswith("with ")
    assert "llm_usage_daily_attempt_dimensions" in sql
    assert "admin_usage_daily_call_dimensions" in sql
    for forbidden in (
        "llm_usage_daily_call_memberships", "count(distinct", "cross join lateral",
    ):
        assert forbidden not in sql
    assert not re.search(
        r"selected_attempt_dimensions\\s+[^)]*join\\s+selected_call_dimensions", sql
    )


def test_narrow_shape_order_is_interleaved():
    probe = _load_narrow_call_probe()
    assert probe._shape_order("unfiltered") == ("bounded_unions", "grouping_sets")
    assert probe._shape_order("provider_model_filtered") == (
        "grouping_sets", "bounded_unions"
    )
```

Build pure evidence for six samples per shape: first, warm, and ordinary under
both cohorts. Require every sample `<3000`, every plan temp block count zero,
and both result witnesses exact. Verify selection uses the lower maximum,
selects the sole eligible shape, and returns `None` when neither is eligible.

- [ ] **Step 2: Run RED**

Run the new query/order/selection tests. Expected: missing interfaces fail.

- [ ] **Step 3: Implement A1 and A2**

Both statements create `selected_attempt_dimensions` and
`selected_call_dimensions` independently. A1 emits five bounded aggregate
UNION branches per source. A2 emits the same five output scopes with grouping
sets. Both combine only scope aggregates through null-safe scope keys, then
append cost, gaps, and filter-option rows in the existing output order.

Use these exact flag mappings:

```python
cohort = f"logical_calls_cohort_{selector}_{completeness}"
requested = f"logical_calls_requested_{selector}_{completeness}"
resolved = f"logical_calls_cohort_provider_model_{completeness}"
outer = f"missing_outer_ordinals_{selector}_{completeness}"
inner = f"missing_inner_ordinals_{selector}_{completeness}"
```

Do not add a second statement, lateral scope expansion, or row-grain join.

- [ ] **Step 4: Parse every shape and prove adversarial equality**

For both shapes, four selectors, and both completeness modes, create empty
TEMP relations and run `EXPLAIN (FORMAT JSON)`. Seed adversarial facts/flags
and compare complete A1, A2, and canonical output rows for every filter.

- [ ] **Step 5: Commit**

```bash
git add scripts/perf/admin_usage_narrow_call_temp_probe.py tests/test_admin_usage.py
git commit -m "test(perf): compare narrow call query shapes"
```

---

### Task 4: Fail-closed session orchestration and evidence

**Files:**
- Modify: `scripts/perf/admin_usage_narrow_call_temp_probe.py`
- Modify: `tests/test_admin_usage.py`

**Interfaces:**
- Produces: `_run_probe(database_url: str, *, output: Path, prefix: str) -> dict[str, Any]`.
- Produces: `_probe_passed(evidence: dict[str, Any]) -> bool`.
- CLI consumes `SCALE_PROBE_DSN`, `--prefix`, and required `--output`.

- [ ] **Step 1: Write complete hard-gate RED tests**

Create one healthy synthetic evidence object with exact rows/bytes/build/raw,
adversarial exactness, both interleaved shape matrices, selected shape, plan
guards, persistent equality, and empty post-session objects. Assert it passes.
Independently mutate every gate, including:

```python
failures = (
    ("sample_ms", 3000.0),
    ("total_bytes", 700_000_001),
    ("ratio_boundary_bytes", 705_099_776),
    ("temp_written_blocks", 1),
    ("rows", 731_200),
    ("raw_attempts", 8_209),
    ("persistent_unchanged", False),
    ("persistent_probe_objects", ["public.bad_probe"]),
)
```

Require every mutation to fail.

- [ ] **Step 2: Write failure-finally RED tests**

Use connection fakes that raise at TEMP DDL, day build, A1 timing, A2 timing,
old reference, and post-witness phases. Every failure must record structured
phase/type/message, attempt post-state collection in `finally`, close the TEMP
connection, open a fresh verification connection, write atomic parseable JSON,
and return `passed=false`.

A single shape failure is recoverable only inside approach A: record it as
ineligible and measure the other shape. Once neither can become eligible, skip
the old reference and finish failure evidence.

- [ ] **Step 3: Run RED**

Run the hard-gate and failure-finally tests. Expected: orchestration interfaces
are absent.

- [ ] **Step 4: Implement minimal orchestration**

Execution order is fixed:

1. pre-witness;
2. TEMP DDL and indexes;
3. 366 day inserts and ANALYZE;
4. storage measurement;
5. raw build and adversarial matrix;
6. interleaved first/warm/ordinary samples at 3,000ms;
7. one old-reference fetch per cohort at 180,000ms, compared with every
   otherwise-eligible shape's ordinary result;
8. post-witness in `finally`;
9. close TEMP connection;
10. fresh-connection persistent-object audit;
11. atomic evidence write.

Reuse previous checksum, canonical-row, plan-block, and atomic-write helpers
only where semantics are identical. Correct the previous harness defect by
collecting post-state and session-close evidence on failure as well as success.
Record each shape's six-sample maximum and the exact selection reason.

- [ ] **Step 5: Run focused GREEN and static verification**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_admin_usage.py -k 'narrow_call_probe or ranked_probe' -q
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m ruff check \
  scripts/perf/admin_usage_narrow_call_temp_probe.py tests/test_admin_usage.py
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m compileall -q \
  scripts/perf/admin_usage_narrow_call_temp_probe.py
git diff --check
```

- [ ] **Step 6: Commit**

```bash
git add scripts/perf/admin_usage_narrow_call_temp_probe.py tests/test_admin_usage.py
git commit -m "test(perf): gate narrow call TEMP checkpoint"
```

---

### Task 5: Execute the retained 3M hard checkpoint

**Files:**
- Modify: `.superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md`
- Runtime artifact: `/private/tmp/2026-08-04-narrow-call-temp-probe.json`

**Interfaces:**
- Consumes: Task-4 CLI and the unchanged retained fixture.
- Produces: pass/failure artifact, report measurements, and a stop decision or authorization for a later planning turn only.

- [ ] **Step 1: Verify the retained fixture before launch**

Read-only checks require 2,000 users; 3,000,000 turns and attempts; zero
corrections/rate cards; 731,199 rows in each daily relation; 3,000,000
memberships; one row in each watermark; zero dirty days; and zero persistent
`admin_usage_narrow_%` objects.

- [ ] **Step 2: Run the one authorized TEMP checkpoint**

```bash
SCALE_PROBE_DSN='postgresql://postgres:test@127.0.0.1:55432/feedling_usage_scale_task4d' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  scripts/perf/admin_usage_narrow_call_temp_probe.py \
  --prefix scale_usage_42e02f444a_ \
  --output /private/tmp/2026-08-04-narrow-call-temp-probe.json
```

Do not rerun to hide a timeout, spill, storage, exactness, or persistent-state
failure. A syntax defect before TEMP creation may be fixed only after proving
the fixture unchanged and rerunning focused tests; record it in the report.

- [ ] **Step 3: Enforce the stop gate and update the report**

If `passed` is not exactly `true`, append all measurements, failure phase,
post-state, and session-close proof to the Task-5 report and stop. Do not edit
or plan production files and do not clean the fixture.

If `passed` is exactly `true`, append storage/build/raw/adversarial evidence,
both six-sample matrices, selection reason, exact witnesses, plan rows/blocks,
and non-persistence proof. Passing authorizes only a later production plan.

- [ ] **Step 4: Fresh verification and result commit**

Run focused tests, Ruff, compileall, and `git diff --check`. Stage the probe and
tests only if changed after Task 4, force-add the Task-5 report, and confirm no
file under `docs/superpowers/evidence/` is staged.

```bash
git add scripts/perf/admin_usage_narrow_call_temp_probe.py tests/test_admin_usage.py
git add -f .superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md
git diff --cached --name-only
git commit -m "docs(perf): record narrow call TEMP checkpoint"
```
