# Formal Scale Business-Proof Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the commit-bound business/pool proof only after the full scale fixture has been timed, exactly cleaned, and the dedicated PostgreSQL database is verified empty, while preserving structured evidence on every workflow failure.

**Architecture:** Add a small callback-driven workflow state machine to `admin_usage_scale.py`. Fresh and resume preparation feed the same fixture workload, cleanup, empty-database proof, and business producer phases; callbacks continue to own existing seed/bootstrap/timing logic, while the state machine owns order, failure capture, and producer prohibition. The existing producer and its commit-bound validator remain unchanged.

**Tech Stack:** Python 3.11, psycopg/PostgreSQL, pytest, Ruff.

## Global Constraints

- Do not raise the 15,000ms Usage report total deadline, 3,000ms attempt subsection limit, 3,000ms maintenance probe limit, or 180,000ms resume-integrity statement limit.
- Do not add a database, schema, service, package, or infrastructure dependency.
- Do not run formal 3M resume or cleanup during implementation; preserve `scale_usage_42e02f444a_`.
- Every timing, cleanup, or producer failure after database validation must still write structured failed evidence when `--output` is supplied.
- A producer result is valid only when the existing validator binds it to the current full Git commit and database counts are zero before and after it.

---

### Task 1: Workflow state machine and failure semantics

**Files:**
- Modify: `tests/test_admin_usage.py`
- Modify: `scripts/perf/admin_usage_scale.py`

**Interfaces:**
- Produces: `_execute_scale_workflow(*, prepare_fixture, run_fixture_workload, cleanup_fixture, collect_database_counts, produce_business) -> dict[str, Any]`.
- The result contains `phase_trace`, `terminal_phase`, `failure`, `cleanup`, `post_fixture_empty_counts`, `business_database_counts`, `business_status`, and `business_result`.

- [ ] **Step 1: Write failing ordering and failure tests**

Add literal callback traces proving these behaviors:

```python
def test_scale_workflow_runs_business_only_after_verified_empty_cleanup():
    # prepare calls arm_cleanup before returning; workload succeeds; cleanup
    # changes literal counts from {"users": 1} to {"users": 0}.
    # Assert the trace is prepare, workload, cleanup, count, count, business,
    # count and business_status == "passed".

def test_scale_workflow_timing_failure_cleans_and_never_runs_business():
    # workload raises RuntimeError("timing failed"). Assert cleanup runs,
    # post_fixture_empty_counts == {"users": 0}, producer is absent, and the
    # structured failure identifies fixture_workload/RuntimeError.

def test_scale_workflow_cleanup_failure_never_runs_business():
    # cleanup raises after leaving {"users": 1}. Assert business_status is
    # "not_run", cleanup status is failed, counts are retained, and cleanup
    # failure evidence is present.

def test_scale_workflow_business_failure_collects_post_counts_in_finally():
    # producer observes literal zero pre-counts then raises. Assert fixture
    # cleanup preceded it, post-counts are collected, status is failed, and the
    # terminal phase is business_failed.
```

- [ ] **Step 2: Run the four tests and verify RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_admin_usage.py::test_scale_workflow_runs_business_only_after_verified_empty_cleanup \
  tests/test_admin_usage.py::test_scale_workflow_timing_failure_cleans_and_never_runs_business \
  tests/test_admin_usage.py::test_scale_workflow_cleanup_failure_never_runs_business \
  tests/test_admin_usage.py::test_scale_workflow_business_failure_collects_post_counts_in_finally -q
```

Expected: FAIL because `_execute_scale_workflow` is absent.

- [ ] **Step 3: Implement the minimal workflow**

Implement one state machine that passes an `arm_cleanup()` closure to
`prepare_fixture`, captures bounded `{"phase", "type", "message"}` errors,
runs armed cleanup in `finally`, collects global counts even after cleanup
failure, and calls the producer only when cleanup succeeded and all counts are
zero. Collect producer post-counts in its own `finally`. Do not catch
configuration/database identity errors outside this workflow.

- [ ] **Step 4: Run the four tests and verify GREEN**

Run the Step 2 command. Expected: 4 passed.

---

### Task 2: Global count gate and commit-bound final gate

**Files:**
- Modify: `tests/test_admin_usage.py`
- Modify: `scripts/perf/admin_usage_scale.py`

**Interfaces:**
- Produces: `_database_counts(conn) -> dict[str, int]`, covering users, source, corrections, rate cards, daily rollups, memberships, both watermark tables, and dirty days.
- Extends `_formal_gate_passed(..., workflow: dict[str, Any]) -> bool`.

- [ ] **Step 1: Write failing database-count and final-gate tests**

Add a real small PostgreSQL test that inserts one fixture user, observes
`users=1`, deletes it, and observes every literal count as zero. Extend the
synthetic formal gate fixture with a passing workflow, then parameterize three
same-cardinality mutations: nonzero `post_fixture_empty_counts`, nonzero
`business_database_counts.pre`, and nonzero
`business_database_counts.post`. Assert each is rejected. Keep the existing
stale-commit producer validation test as the independent commit-binding proof.

- [ ] **Step 2: Run the new tests and verify RED**

Run the named tests with `FEEDLING_TEST_PG`. Expected: missing collector or
unexpected gate keyword, then false/true mismatch once the interface exists.

- [ ] **Step 3: Implement the minimal count collector and gate**

Refactor `_assert_empty_dedicated_database` to consume `_database_counts`.
Require `workflow.business_status == "passed"`, no workflow failure, successful
cleanup, and present all-zero post-fixture/pre-business/post-business maps in
the formal gate. Keep `_business_path_evidence_passed(... expected_commit=HEAD)`
as the separate sealed-evidence check.

- [ ] **Step 4: Run the new and existing formal-gate tests and verify GREEN**

Run the new count/gate tests plus all tests containing `formal_gate` or
`business_path_evidence`. Expected: pass.

---

### Task 3: Integrate fresh/resume execution and durable failed artifacts

**Files:**
- Modify: `tests/test_admin_usage.py`
- Modify: `scripts/perf/admin_usage_scale.py`
- Modify: `.superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md`

**Interfaces:**
- `_run(args) -> int` returns 1 after rendering failed workflow evidence rather than allowing workflow exceptions to bypass artifact output.
- `--validate-resume-only` remains an early read-only success path.

- [ ] **Step 1: Write failing `_run` orchestration tests**

Use the existing loaded harness and narrowly replace only slow fixture/timing
callbacks. Assert literal phase order for fresh and resume. Add output-path
tests for timing failure, cleanup failure, and business failure; parse the real
JSON file and assert `passed=false`, exact producer status, structured failure,
cleanup/count evidence, and exit code 1. Assert timing/cleanup failures never
invoke the producer and business starts only after a literal zero-count read.

- [ ] **Step 2: Run the orchestration tests and verify RED**

Expected: old order calls producer before fixture work or raises without
writing the output artifact.

- [ ] **Step 3: Route fresh and resume through the unified workflow**

Move producer execution after the existing fixture workload and exact cleanup.
Fresh preparation arms cleanup before seeding; resume preparation exact-
validates then arms cleanup. Remove producer/postvalidation from
`_run_resume_gate`; resume evidence retains exact `pre`, records no impossible
post-fixture snapshot, and uses workflow empty-count proof instead. Initialize
the artifact before workflow execution, merge the returned workflow fields,
render on success or failure, and return 1 for any workflow failure without
inventing `business_path` evidence.

- [ ] **Step 4: Run orchestration tests and verify GREEN**

Expected: all new order/artifact tests pass and old validation-only tests remain
read-only.

- [ ] **Step 5: Run a real small non-formal probe**

Use a new explicit `feedling_usage_scale_*` local database or an already empty
dedicated test database, with 100 turns/100 attempts and at least five timing
runs. Assert exit 1 only because non-formal evidence is permanently ineligible,
while phase order is complete, fixture and producer pre/post counts are zero,
producer evidence is commit-bound, and final cleanup is zero. Do not point the
probe at the retained 3M database.

- [ ] **Step 6: Update the retained load report**

Document the full-fixture producer root cause, failed-run intact-fixture proof,
unified state machine, unchanged timeout budgets, small-probe evidence, and the
explicit fact that no formal resume/cleanup was run.

---

### Task 4: Verification and commit

**Files:**
- Verify: `scripts/perf/admin_usage_scale.py`
- Verify: `scripts/perf/provider_attempt_business_path.py`
- Verify: `tests/test_admin_usage.py`
- Verify: `tests/test_provider_attempt_business_path.py`

- [ ] **Step 1: Run focused tests**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_provider_attempt_business_path.py tests/test_admin_usage.py -q
```

- [ ] **Step 2: Run static and harness checks**

```bash
uv run ruff check scripts/perf/admin_usage_scale.py \
  scripts/perf/provider_attempt_business_path.py tests/test_admin_usage.py \
  tests/test_provider_attempt_business_path.py
.venv-test/bin/python -m compileall -q scripts/perf/admin_usage_scale.py \
  scripts/perf/provider_attempt_business_path.py
.venv-test/bin/python scripts/perf/admin_usage_scale.py --self-test
git diff --check
```

- [ ] **Step 3: Verify safety state**

Confirm no formal resume command was executed and the retained fixture prefix
was never passed to cleanup. Preserve the existing untracked business-path JSON.

- [ ] **Step 4: Commit**

```bash
git add scripts/perf/admin_usage_scale.py tests/test_admin_usage.py \
  docs/superpowers/plans/2026-08-03-formal-scale-business-proof-order.md
git add -f .superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md
git commit -m "fix(perf): run business proof after scale cleanup"
```

---

### Task 5: Make the tested workflow the only production orchestrator

**Files:**
- Modify: `tests/test_admin_usage.py`
- Modify: `scripts/perf/admin_usage_scale.py`

**Interfaces:**
- Produces: `_build_scale_workflow_callbacks(context: dict[str, Any]) -> dict[str, Callable[..., Any]]` with `prepare_fixture`, `run_fixture_workload`, `cleanup_fixture`, `collect_database_counts`, and `produce_business`.
- Production `_run(args) -> int` calls `_execute_scale_workflow(**callbacks)` exactly once for each non-validation run.
- `_execute_scale_workflow` remains the sole producer of the complete workflow evidence schema, including `business_result`.

- [ ] **Step 1: Add real-entry RED tests**

Create a stateful test runtime around the actual `_run` boundary. Patch only
the validated local-pool/runtime-loading boundary and the five domain callbacks;
do not patch `_execute_scale_workflow` or `_write_evidence_atomic`. Each call
uses a real `tmp_path` output and parses the resulting JSON.

```python
@pytest.mark.parametrize("resumed", [False, True])
def test_run_entry_uses_single_workflow_for_fresh_and_resume_happy_path(...):
    # Assert exit is 1 only because the synthetic/non-formal gate is false,
    # workflow is complete, business_result has literal pool/business fields,
    # and the literal event order ends cleanup,count,count,business,count.

def test_run_entry_timing_failure_writes_atomic_cleanup_zero_without_business(...):
    # workload raises; cleanup changes state to zero; parse failure phase and
    # prove producer status not_run.

def test_run_entry_cleanup_failure_writes_atomic_failure_without_business(...):
    # cleanup raises and leaves literal nonzero state; parse cleanup failure.

def test_run_entry_business_failure_starts_after_zero_and_writes_failed_artifact(...):
    # producer asserts state zero then raises; parse post zero and failed status.
```

- [ ] **Step 2: Run entry tests and verify RED**

Expected: old production `_run` bypasses the patched callback builder and the
real helper, so event/artifact expectations fail.

- [ ] **Step 3: Extract callbacks without changing domain behavior**

Move the existing fresh/resume preparation, shared timing/EXPLAIN workload, and
exact cleanup blocks into the five callback closures returned by
`_build_scale_workflow_callbacks`. Fresh calls `arm_cleanup()` immediately
before `_seed_fixture`; resume exact-validates before calling it. The callbacks
continue mutating the existing top-level evidence dictionaries for source,
bootstrap, cohort, retention, and cleanup data.

- [ ] **Step 4: Route `_run` through the helper and verify GREEN**

Replace the copied workflow block with:

```python
callbacks = _build_scale_workflow_callbacks(context)
workflow = _execute_scale_workflow(**callbacks)
evidence["workflow"] = workflow
business_result = workflow.get("business_result")
evidence["business_path"] = (
    business_result.get("business") if isinstance(business_result, dict) else None
)
```

Evaluate existing gates only after this call and always atomically render the
artifact. Run the Step 1 tests; expected: pass.

- [ ] **Step 5: Add invalid-resume and validation-only mutation tests**

Call `_run` with the same stateful boundary. For invalid resume, make exact
validation fail before arming and assert state unchanged, no cleanup, no
producer, atomic failed JSON, and exit 1. For validation-only, return a healthy
exact snapshot and assert exit 0 with no seed/workload/cleanup/producer events.

- [ ] **Step 6: Run all seven entry scenarios**

Expected: fresh happy, resume happy, timing failure, cleanup failure, business
failure, invalid resume, and validation-only all pass without a large fixture.

---

### Task 6: Probe, verification, report, and review-fix commit

**Files:**
- Modify: `.superpowers/sdd/2026-08-02-provider-attempt-accounting-p0b/task-5-load-report.md`
- Verify: `scripts/perf/admin_usage_scale.py`
- Verify: `scripts/perf/provider_attempt_business_path.py`
- Verify: `tests/test_admin_usage.py`
- Verify: `tests/test_provider_attempt_business_path.py`

- [ ] **Step 1: Run a fresh 100/100 non-formal temporary-database probe**

Create and migrate an explicitly named local `feedling_usage_scale_*` database,
run the unified probe, parse complete workflow/business/zero evidence, then
drop exactly that temporary database. Confirm the retained
`feedling_usage_scale_task4d` database remains the only scale database.

- [ ] **Step 2: Run focused and static verification**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_provider_attempt_business_path.py tests/test_admin_usage.py -q
uv run ruff check scripts/perf/admin_usage_scale.py \
  scripts/perf/provider_attempt_business_path.py tests/test_admin_usage.py \
  tests/test_provider_attempt_business_path.py
.venv-test/bin/python -m compileall -q scripts/perf/admin_usage_scale.py \
  scripts/perf/provider_attempt_business_path.py
.venv-test/bin/python scripts/perf/admin_usage_scale.py --self-test
git diff --check
```

- [ ] **Step 3: Update evidence and commit**

Document the production-helper ownership, seven entry scenarios, 100/100 probe,
temporary database deletion, unchanged budgets, and no retained-fixture
mutation. Commit implementation, tests, plan, and forced-add report while
excluding the existing untracked business-path evidence JSON.
