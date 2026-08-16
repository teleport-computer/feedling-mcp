# Runtime V2 Profile Durable Retry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Runtime V2 profile generation recover durably from retryable failures without user activity, while keeping Dream/post-chat semantics, single-flight ownership, watchdog safety, and the three-pool isolation contract intact.

**Architecture:** Add an `available_at` claim fence to `agent_jobs`, then reschedule the exact owned profile Job as delayed `pending` after a classified retryable failure. Keep retry policy in a pure module, persist its content-free disposition in `v2_agent_profile.last_attempt`, and make queue/watchdog metrics distinguish ready work from delayed work. PR #187 must be merged into `test` before implementation begins so profile is tested as the sole long-term semantic layer beside deterministic history coverage.

**Tech Stack:** Python 3.12, asyncio, PostgreSQL 16, psycopg 3, Alembic, pytest, Runtime V2 worker/jobs-store, Phala Compose, Next.js docs-site.

## Global Constraints

- Merge [PR #187](https://github.com/teleport-computer/feedling-mcp/pull/187) into `test`, fetch, and rebase the implementation branch onto that exact `origin/test` before Task 1.
- Do not change Dream's night window, 23-hour minimum interval, minimum-new-card threshold, user consent gate, or `force=True` behavior.
- Keep the post-chat `_enqueue_profile_if_due` hook and the 7-day successful-profile freshness rule.
- Profile remains in the heavy pool; delayed profile Jobs must not occupy a slot or appear claimable to watchdog.
- Never put Memory Garden text, provider raw responses, API keys, or secrets into `last_error`, trajectory retry payloads, metrics, or logs.
- Preserve runtime-generation, lease, CAS, and per-user/per-lane single-flight fences.
- Use TDD for every behavior change: write one failing test, run and verify the expected failure, then write the minimum implementation.
- Deploy only to test during this plan. Do not modify pre/prod configuration or deploy pre/prod.

---

### Task 1: Add the durable `available_at` claim fence

**Files:**
- Create: `backend/alembic/versions/0088_agent_jobs_available_at.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `tests/test_v2_jobs_migration.py`
- Modify: `tests/test_v2_jobs_store.py`

**Interfaces:**
- Produces: `agent_jobs.available_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
- Produces: every `claim_next_job()` candidate/revalidation path requires `available_at <= clock_timestamp()`.
- Consumes: existing `_WORKER_CLAIM_PROTOCOL`, runtime-state-first lock order, `LANE_PRIORITY`, and partial single-flight index.

- [ ] **Step 1: Write the failing migration contract**

Add a helper for revision `0088_agent_jobs_available_at` and extend the installed-head test:

```python
def _migration_0088_module():
    backend = Path(__file__).parent.parent / "backend"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    return (
        ScriptDirectory.from_config(cfg)
        .get_revision("0088_agent_jobs_available_at")
        .module
    )


def test_agent_jobs_available_at_is_the_single_installed_head():
    backend = Path(__file__).parent.parent / "backend"
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    assert script.get_heads() == ["0088_agent_jobs_available_at"]
    assert script.get_revision("0088_agent_jobs_available_at").down_revision == (
        "0087_v2_first_chat_activation"
    )

    with db.get_pool().connection() as conn:
        column = conn.execute(
            "SELECT data_type,is_nullable,column_default "
            "FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='agent_jobs' "
            "AND column_name='available_at'"
        ).fetchone()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT indexname FROM pg_indexes "
                "WHERE schemaname='public' AND tablename='agent_jobs'"
            ).fetchall()
        }
    assert column[0:2] == ("timestamp with time zone", "NO")
    assert "now()" in str(column[2])
    assert "ix_agent_jobs_pending_available_at" in indexes
```

- [ ] **Step 2: Run the migration test and verify RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  ../../.venv-test/bin/python -m pytest \
  tests/test_v2_jobs_migration.py::test_agent_jobs_available_at_is_the_single_installed_head -q
```

Expected: FAIL because Alembic cannot resolve `0088_agent_jobs_available_at`.

- [ ] **Step 3: Implement migration 0088**

Create the revision with this upgrade contract:

```python
from alembic import op

revision = "0088_agent_jobs_available_at"
down_revision = "0087_v2_first_chat_activation"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE agent_jobs
  ADD COLUMN available_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX ix_agent_jobs_pending_available_at
  ON agent_jobs (available_at, priority DESC, created_at)
  WHERE status='pending';
"""

_DOWN = """
DROP INDEX IF EXISTS ix_agent_jobs_pending_available_at;
ALTER TABLE agent_jobs DROP COLUMN IF EXISTS available_at;
"""

def upgrade() -> None:
    op.execute(_UP)

def downgrade() -> None:
    op.execute(_DOWN)
```

Update the old head assertion to chain `0088 -> 0087 -> 0086`; do not delete the assertions for older revisions.

- [ ] **Step 4: Run migration tests and verify GREEN**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  ../../.venv-test/bin/python -m pytest tests/test_v2_jobs_migration.py -q
```

Expected: PASS with one Alembic head and the new column/index installed.

- [ ] **Step 5: Write failing delayed-claim tests**

In `tests/test_v2_jobs_store.py`, seed a V2 user and enqueue a profile Job, then set its availability into the future:

```python
def test_claim_skips_profile_job_until_available_at():
    uid = "u_delayed_claim"
    seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "profile", reason="retry")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET available_at=now()+interval '1 hour' WHERE id=%s",
            (job_id,),
        )
    assert jobs_store.claim_next_job("heavy-1", lanes={"profile"}) is None

    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE agent_jobs SET available_at=now()-interval '1 second' WHERE id=%s",
            (job_id,),
        )
    claimed = jobs_store.claim_next_job("heavy-1", lanes={"profile"})
    assert claimed["id"] == job_id
```

Add a second regression that deletes `v2_runtime_state` while a future Job exists and asserts the orphan probe does not supersede it before `available_at`.

- [ ] **Step 6: Run delayed-claim tests and verify RED**

Run the two new tests. Expected: the future Job is claimed or orphan-retired because the SQL does not yet filter `available_at`.

- [ ] **Step 7: Add the claim predicate in all three SQL paths**

Modify candidate SQL, orphan SQL, and locked-row SQL to include:

```python
"AND j.available_at <= clock_timestamp() "
```

Use the correct alias in each query. Keep the final `ORDER BY j.priority DESC, j.created_at` and the two-statement runtime-state-first locking protocol unchanged.

- [ ] **Step 8: Run Task 1 tests and commit**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  ../../.venv-test/bin/python -m pytest \
  tests/test_v2_jobs_migration.py tests/test_v2_jobs_store.py -q
```

Then commit:

```bash
git add backend/alembic/versions/0088_agent_jobs_available_at.py \
  backend/model_api_runtime/v2/jobs_store.py \
  tests/test_v2_jobs_migration.py tests/test_v2_jobs_store.py
git commit -m "feat(v2): add delayed job claim fence"
```

---

### Task 2: Add owner-safe rescheduling, force-ready, and queue observability

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `tests/test_v2_jobs_store.py`
- Modify: `tests/test_v2_watchdog.py`

**Interfaces:**
- Produces: `reschedule_owned_job(job_id, *, claimed_by, error, available_at) -> bool`.
- Produces: `make_pending_job_ready(user_id, lane="profile") -> bool`.
- Produces: `pending_job_count()` counts ready pending rows only.
- Produces: pool metrics expose `pending_ready` and `pending_delayed`, while
  retaining `pending` as a backward-compatible alias of `pending_ready`.

- [ ] **Step 1: Write failing ownership and single-flight tests**

Add tests proving:

```python
assert jobs_store.reschedule_owned_job(
    job_id,
    claimed_by="slot-heavy-1-g1",
    error="profile_generation_failed:providererror",
    available_at=now + 300,
) is True
```

Then assert the row is `pending`, `attempt_count == 1`, `available_at` matches, and all owner/lease/start/deadline/finish fields are NULL. Assert a wrong owner returns `False` without mutation. Assert `enqueue_job(user, "profile")` coalesces into the delayed row instead of inserting a second active row.

- [ ] **Step 2: Run the new ownership tests and verify RED**

Expected: `AttributeError` because `reschedule_owned_job` does not exist.

- [ ] **Step 3: Implement `reschedule_owned_job` with existing lock order**

Use one transaction and lock `v2_runtime_state` before `agent_jobs`. Require the Job's expected generation to match the current V2 generation. The UPDATE shape is:

```sql
UPDATE agent_jobs
SET status='pending',
    available_at=to_timestamp(%s),
    last_error=%s,
    attempt_count=attempt_count+1,
    claimed_by=NULL,
    claimed_at=NULL,
    started_at=NULL,
    finished_at=NULL,
    lease_expires_at=NULL,
    deadline_at=NULL
WHERE id=%s
  AND claimed_by=%s
  AND status IN ('claimed','running')
  AND lease_expires_at > clock_timestamp()
  AND expected_runtime_generation=%s
```

Return `rowcount == 1`. Do not enqueue a successor row.

- [ ] **Step 4: Write and implement force-ready tests**

Test that `make_pending_job_ready(user_id, lane="profile")` changes only an existing delayed pending row, sets `available_at=clock_timestamp()`, and returns `False` for claimed/running/terminal rows. Implement it as one bounded UPDATE; it must not bypass runtime ownership or create a Job.

- [ ] **Step 5: Write failing watchdog and metric tests**

Add tests with one future profile Job:

```python
assert jobs_store.pending_job_count() == 0
metrics = jobs_store.pool_queue_metrics()["heavy"]
assert metrics["pending_ready"] == 0
assert metrics["pending_delayed"] == 1
assert metrics["pending"] == metrics["pending_ready"]
assert metrics["oldest_pending_sec"] is None
```

In `tests/test_v2_watchdog.py`, make `_jobs_claimable()` use the real count seam and assert it is false when the only pending Job is delayed.

- [ ] **Step 6: Implement ready/delayed queue semantics**

Change `pending_job_count()` to filter `available_at <= clock_timestamp()`. In `pool_queue_metrics()` and `job_counts_by_lane()`, split pending rows with FILTER clauses:

```sql
count(*) FILTER (
  WHERE status='pending' AND available_at <= clock_timestamp()
) AS pending_ready,
count(*) FILTER (
  WHERE status='pending' AND available_at > clock_timestamp()
) AS pending_delayed
```

Calculate `oldest_pending_sec` only from ready rows. Preserve the existing
`pending` key in both `pool_queue_metrics()` and `job_counts_by_lane()` as an
alias of `pending_ready`; update internal consumers to prefer the explicit
field. Keep aggregate inflight metrics backward compatible unless a caller
explicitly needs claimability.

- [ ] **Step 7: Run Task 2 tests and commit**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  ../../.venv-test/bin/python -m pytest \
  tests/test_v2_jobs_store.py tests/test_v2_watchdog.py \
  tests/test_v2_pool_config.py -q
```

Commit:

```bash
git add backend/model_api_runtime/v2/jobs_store.py \
  backend/model_api_runtime/v2/serve_worker.py \
  tests/test_v2_jobs_store.py tests/test_v2_watchdog.py
git commit -m "feat(v2): reschedule owned jobs durably"
```

---

### Task 3: Define pure profile retry policy and storage contract

**Files:**
- Create: `backend/model_api_runtime/v2/profile_retry.py`
- Create: `tests/test_v2_profile_retry.py`
- Modify: `backend/model_api_runtime/v2/profile_store.py`
- Modify: `tests/test_v2_profile_storage.py`

**Interfaces:**
- Produces: `ProfileRetryDecision(disposition, retry_family, retry_attempts, retry_not_before, reason)`.
- Produces: `decide_profile_retry(error_class, reject_code, previous_retry_family, previous_retry_attempts, now)`.
- Produces: `last_attempt.retry_disposition` validated as `"" | "scheduled" | "provider_config" | "source_change" | "terminal"`.
- Produces: `last_attempt.retry_family` and `retry_attempts` track only the current consecutive failure family; existing `attempts` remains cumulative.

- [ ] **Step 1: Write the retry policy matrix as failing tests**

Create parametrized tests for:

```python
@pytest.mark.parametrize(
    ("error_class", "code", "previous_family", "previous_attempts", "disposition", "attempts"),
    [
        ("transient_exhausted", "profile_generation_failed:providererror", "", 0, "scheduled", 1),
        ("provider_config", "profile_generation_failed:providererror", "transient", 8, "provider_config", 1),
        ("", "reply_not_json", "", 0, "scheduled", 1),
        ("", "reply_not_json", "shape", 3, "terminal", 4),
        ("", "field_empty:memory", "shape", 1, "scheduled", 2),
        ("", "profile_source_exceeds_budget:120001", "shape", 2, "source_change", 1),
        ("", "profile_cards_count_invalid", "", 0, "source_change", 1),
        ("", "profile_generation_failed:runtimeerror", "", 0, "terminal", 1),
    ],
)
def test_retry_policy_matrix(
    error_class, code, previous_family, previous_attempts, disposition, attempts
):
    decision = profile_retry.decide_profile_retry(
        error_class=error_class,
        reject_code=code,
        previous_retry_family=previous_family,
        previous_retry_attempts=previous_attempts,
        now=1000.0,
    )
    assert decision.disposition == disposition
    assert decision.retry_attempts == attempts
```

Also assert transient delays are 300, 600, 1200 seconds and cap at 21600 seconds. Shape errors allow exactly three delayed retries after the initial failed execution; retry attempt 4 is terminal. Add a regression where cumulative `attempts=20` but `retry_attempts=1`, and assert the first shape failure still schedules.

- [ ] **Step 2: Run policy tests and verify RED**

Expected: import failure because `profile_retry.py` does not exist.

- [ ] **Step 3: Implement the pure policy module**

Define frozen dataclass and fixed code sets. The function must not import DB, worker, provider client, hosted config, or envelope modules:

```python
@dataclass(frozen=True)
class ProfileRetryDecision:
    disposition: str
    retry_family: str
    retry_attempts: int
    retry_not_before: float
    reason: str


def decide_profile_retry(*, error_class: str, reject_code: str,
                         previous_retry_family: str,
                         previous_retry_attempts: int,
                         now: float) -> ProfileRetryDecision:
    family = classify_retry_family(error_class=error_class, reject_code=reject_code)
    retry_attempts = (
        max(0, int(previous_retry_attempts)) + 1
        if previous_retry_family == family
        else 1
    )
    if family == "transient":
        delay = min(21600.0, 300.0 * (2 ** max(0, retry_attempts - 1)))
        return ProfileRetryDecision(
            "scheduled", family, retry_attempts, now + delay, reject_code
        )
    if family == "provider_config":
        return ProfileRetryDecision(
            "provider_config", family, retry_attempts, 0.0, reject_code
        )
    if family == "shape":
        if retry_attempts <= 3:
            delay = min(21600.0, 300.0 * (2 ** max(0, retry_attempts - 1)))
            return ProfileRetryDecision(
                "scheduled", family, retry_attempts, now + delay, reject_code
            )
        return ProfileRetryDecision(
            "terminal", family, retry_attempts, 0.0, reject_code
        )
    if family == "source":
        return ProfileRetryDecision(
            "source_change", family, retry_attempts, 0.0, reject_code
        )
    return ProfileRetryDecision(
        "terminal", "terminal", retry_attempts, 0.0, reject_code
    )
```

Reject-code matching must use fixed full matches/prefixes and never include original content.

- [ ] **Step 4: Write failing storage compatibility tests**

Test that `validate_profile_document()` accepts every allowed disposition/family, normalizes missing old documents to blank family and zero `retry_attempts`, and rejects unknown strings with `profile_retry_disposition_invalid` or `profile_retry_family_invalid`. Test that success/empty documents with blank reject code store blank disposition/family, zero retry attempts, and zero retry time.

- [ ] **Step 5: Extend `profile_store` minimally**

Validate and return:

```python
retry_disposition = str(attempt.get("retry_disposition") or "")
if retry_disposition not in {
    "", "scheduled", "provider_config", "source_change", "terminal"
}:
    raise ProfileStorageError("profile_retry_disposition_invalid")

retry_family = str(attempt.get("retry_family") or "")
if retry_family not in {
    "", "transient", "shape", "provider_config", "source", "terminal"
}:
    raise ProfileStorageError("profile_retry_family_invalid")
retry_attempts = _nonnegative_int(
    attempt.get("retry_attempts", 0), "last_attempt_retry_attempts"
)
```

Include it inside normalized `last_attempt`. Do not change envelope or CAS ordering.

- [ ] **Step 6: Run Task 3 tests and commit**

Run:

```bash
../../.venv-test/bin/python -m pytest \
  tests/test_v2_profile_retry.py tests/test_v2_profile_storage.py -q
```

Commit:

```bash
git add backend/model_api_runtime/v2/profile_retry.py \
  backend/model_api_runtime/v2/profile_store.py \
  tests/test_v2_profile_retry.py tests/test_v2_profile_storage.py
git commit -m "feat(v2): classify profile retry outcomes"
```

---

### Task 4: Integrate durable retry into `_run_profile`

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `tests/test_v2_profile_lane.py`
- Modify: `tests/test_v2_profile_refresh.py`

**Interfaces:**
- Consumes: `profile_retry.decide_profile_retry`.
- Consumes: `jobs_store.reschedule_owned_job`.
- Produces: `_run_profile()` returns `"rescheduled"` for delayed retry and never marks that Job terminal.
- Produces: `_profile_refresh_due()` understands `scheduled`, `provider_config`, `source_change`, and `terminal`.

- [ ] **Step 1: Write failing worker tests for transient and shape retries**

Add tests where the provider raises a `ProviderError` carrying `feedling_error_class="transient_exhausted"`. Assert:

```python
assert status == "rescheduled"
assert rescheduled == [(job_id, claimed_by, retry_not_before)]
assert failed == []
assert document["last_attempt"]["retry_disposition"] == "scheduled"
```

Add shape tests proving attempts 1–3 reschedule and attempt 4 calls `mark_failed` without rescheduling.

- [ ] **Step 2: Run the new worker tests and verify RED**

Expected: current `_run_profile` always calls `mark_failed` and returns `"failed"`.

- [ ] **Step 3: Thread retry decisions through failure metadata**

Change `_metadata_failure(previous, code, decision)` so `last_attempt` contains the decision's disposition/family/retry-attempt count/time. Derive provider classification from `getattr(exc, "feedling_error_class", "")`; do not parse raw provider messages. Pass the previous family/count into `decide_profile_retry`, which increments within one family or resets to 1 when the family changes. Continue incrementing existing cumulative `attempts` independently. Success and empty writes set blank family and `retry_attempts=0`.

For `generated.fields is None`, derive a decision from the generated reject code. For exceptions, derive it from both safe error class and `_profile_failure_code(exc)`.

- [ ] **Step 4: Reschedule instead of terminalizing scheduled failures**

After profile metadata CAS wins, branch on the persisted `last_attempt.retry_disposition`:

```python
if disposition == "scheduled":
    moved = await asyncio.to_thread(
        jobs_store.reschedule_owned_job,
        job_id,
        claimed_by=claimed_by,
        error=terminal_reject,
        available_at=retry_not_before,
    )
    if not moved:
        raise LostJobLease("profile retry ownership lost")
    tm.flush(failed=True, status="profile_retry_scheduled")
    return "rescheduled"
```

Only terminal/provider-config/source-change decisions call `mark_failed`.

- [ ] **Step 5: Add the crash-gap preflight test and implementation**

Seed a profile document with future `scheduled` retry metadata, then run a reaper-recovered Job immediately. Assert Garden/card reader and provider are never called; the Job is moved back to the exact recorded retry time.

Implement this check before provider resolution/Garden reads. It must still renew/fence ownership before rescheduling.

- [ ] **Step 6: Update `_profile_refresh_due` tests**

Assert:

- `scheduled` before its time returns false; after its time returns true as a repair backstop.
- `provider_config` and `terminal` return false.
- `source_change` returns true only when count or `max_updated_at` differs.
- old documents without disposition retain their current retry-not-before behavior.
- successful `ok` and `empty` behavior remains byte-for-byte compatible.

- [ ] **Step 7: Run Task 4 tests and commit**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  ../../.venv-test/bin/python -m pytest \
  tests/test_v2_profile_lane.py tests/test_v2_profile_refresh.py \
  tests/test_v2_profile_storage.py tests/test_v2_profile_retry.py -q
```

Commit:

```bash
git add backend/model_api_runtime/v2/worker.py \
  tests/test_v2_profile_lane.py tests/test_v2_profile_refresh.py
git commit -m "fix(v2): retry profile jobs durably"
```

---

### Task 5: Preserve Dream force semantics and wake profile after provider repair

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `backend/hosted/config_store.py`
- Modify: `backend/hosted/setup_core.py`
- Modify: `tests/test_v2_profile_refresh.py`
- Modify: `tests/test_v2_extraction_lanes.py`
- Modify: `tests/test_model_api_route_activation_unit.py`
- Modify: `tests/test_model_api_profiles_routes.py`

**Interfaces:**
- Produces: force profile enqueue makes an existing delayed profile Job ready now.
- Produces: successful provider setup/activation best-effort readies or enqueues profile.
- Preserves: non-force post-chat enqueue never accelerates delayed retry.

- [ ] **Step 1: Write failing force/coalesce tests**

In profile refresh tests, make `enqueue_job` return `(job_id, True)` and assert:

- `force=False` never calls `make_pending_job_ready`.
- `force=True` calls `make_pending_job_ready(user_id, lane="profile")` and notifies `v2_jobs` when it changed the delayed row.

- [ ] **Step 2: Implement force-ready inside `_enqueue_profile_if_due`**

Keep the due check unchanged for non-force calls. After enqueue/coalesce:

```python
made_ready = False
if force and coalesced:
    made_ready = await asyncio.to_thread(
        jobs_store.make_pending_job_ready, user_id, lane="profile"
    )
if not coalesced or made_ready:
    await asyncio.to_thread(core_wake_bus.notify, "v2_jobs", user_id)
return (not coalesced) or made_ready
```

This changes no Dream gate or frequency; it only preserves the existing immediate force meaning when a delayed Job already exists.

- [ ] **Step 3: Add a hosted best-effort profile wake helper**

In `hosted/config_store.py`, factor the existing post-cutover enqueue into:

```python
def enqueue_profile_best_effort(user_id: str, *, reason: str,
                                force_ready: bool = False) -> bool:
    try:
        if not _profile_generation_enabled():
            return False
        _mode, state, _generation = db.get_hosted_runtime_control_strict(user_id)
        if state != "v2":
            return False
        from core import wake_bus
        from model_api_runtime.v2 import jobs_store
        _job_id, coalesced = jobs_store.enqueue_job(
            user_id, "profile", reason=reason
        )
        made_ready = (
            force_ready
            and coalesced
            and jobs_store.make_pending_job_ready(user_id, lane="profile")
        )
        if not coalesced or made_ready:
            wake_bus.notify("v2_jobs", user_id)
        return (not coalesced) or made_ready
    except Exception as exc:
        log.warning(
            "profile_wake_failed user_id=%s error_type=%s",
            user_id,
            type(exc).__name__,
        )
        return False
```

The helper catches/logs internally because provider setup or runtime cutover is already durable and must not be rolled back by an advisory profile wake. It also verifies the user's current hosted runtime state is `v2`, so successful provider changes for resident users do not create unusable V2 Jobs. The warning is content-free: log only the user id and exception type, never the exception string.

- [ ] **Step 4: Write failing provider-repair trigger tests**

Extend setup and route-activation tests to assert that a successful tested/active provider calls:

```python
enqueue_profile_best_effort(
    store.user_id,
    reason="provider_config_changed",
    force_ready=True,
)
```

Assert failed provider tests, failed writes, and inactive/untested routes do not call it. Assert helper exceptions do not change the successful HTTP response.

- [ ] **Step 5: Wire successful setup and activation**

Call the helper only after route test status is `ok`, activation succeeded, runtime restore/policy succeeded, and before returning the success response. Reuse it from both `model_api_setup` and `model_api_route_activate`; do not enqueue on mere catalog listing or failed route test.

- [ ] **Step 6: Run Task 5 tests and commit**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  ../../.venv-test/bin/python -m pytest \
  tests/test_v2_profile_refresh.py tests/test_v2_extraction_lanes.py \
  tests/test_model_api_route_activation_unit.py \
  tests/test_model_api_profiles_routes.py -q
```

Commit:

```bash
git add backend/model_api_runtime/v2/worker.py \
  backend/hosted/config_store.py backend/hosted/setup_core.py \
  tests/test_v2_profile_refresh.py tests/test_v2_extraction_lanes.py \
  tests/test_model_api_route_activation_unit.py \
  tests/test_model_api_profiles_routes.py
git commit -m "fix(v2): wake profile after explicit repair"
```

---

### Task 6: Combined verification, public docs, and test-only rollout

**Files:**
- Modify: `docs/RUNTIME_V2_FLOWS.md`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify if the current text describes profile failure behavior: `docs-site/content/docs/self-hosting.mdx`
- Modify: `docs/CHANGELOG.md`
- Create: `docs/superpowers/reports/2026-08-14-v2-profile-durable-retry-verification.md`

**Interfaces:**
- Consumes: Tasks 1–5 and merged PR #187.
- Produces: reproducible local/CI/test evidence; no pre/prod mutation.

- [ ] **Step 1: Run the focused Runtime V2 matrix**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  ../../.venv-test/bin/python -m pytest \
  tests/test_v2_jobs_migration.py \
  tests/test_v2_jobs_store.py \
  tests/test_v2_watchdog.py \
  tests/test_v2_profile_retry.py \
  tests/test_v2_profile_lane.py \
  tests/test_v2_profile_refresh.py \
  tests/test_v2_profile_storage.py \
  tests/test_v2_profile_prompt.py \
  tests/test_v2_deterministic_compaction.py \
  tests/test_v2_compaction_integration.py \
  tests/test_v2_serve_worker.py -q
```

Expected: all selected tests PASS with a real local PostgreSQL instance; no DB-backed modules skipped.

- [ ] **Step 2: Run backend full regression and static checks**

Run the repository-standard full command from `docs/testing/TESTING.md`, including real Postgres, and:

```bash
../../.venv-test/bin/python -m compileall -q \
  backend/model_api_runtime/v2 backend/hosted backend/alembic/versions tests
../../.venv-test/bin/python -m pyflakes \
  backend/model_api_runtime/v2/profile_retry.py \
  backend/model_api_runtime/v2/jobs_store.py \
  backend/model_api_runtime/v2/worker.py
git diff --check
```

Record exact pass/skip/xfail counts and any pre-existing failures in the verification report.

- [ ] **Step 3: Update internal and public documentation**

Document these exact points:

- profile transient failures retry durably without user activity;
- shape retries are bounded; provider config/source/internal failures do not loop;
- delayed Jobs do not occupy a slot or count as watchdog-claimable;
- Dream and post-chat behavior remain unchanged;
- PR #187 leaves MEMORY/USER as the only long-term semantic layer.

Do not document `available_at` as a public API field. Add the behavior change under public `Unreleased` because self-hosted users' providers can receive bounded background retry calls.

- [ ] **Step 4: Run documentation and contract gates**

Run:

```bash
../../.venv-test/bin/python -m pytest tests/openapi -q
cd docs-site
npm run types:check
npm run lint
npm run build
```

OpenAPI generation is not required because no public route/schema changes.

- [ ] **Step 5: Commit docs and verification evidence**

```bash
git add docs/RUNTIME_V2_FLOWS.md docs/CHANGELOG.md \
  docs-site/content/docs/changelog.mdx \
  docs-site/content/docs/self-hosting.mdx \
  docs/superpowers/reports/2026-08-14-v2-profile-durable-retry-verification.md
git commit -m "docs(v2): document durable profile recovery"
```

If `self-hosting.mdx` required no edit after inspection, omit it from `git add` and explicitly record that review result in the verification report.

- [ ] **Step 6: Push feature branch and require green PR checks targeting `test`**

Create a PR from the feature branch to `test`, never directly to `main`. Require migration, Runtime V2 rollout matrices, API multi-tenant tests, docs checks, and branch-flow check to pass.

- [ ] **Step 7: Deploy only to test and run live recovery proof**

After merge to `test` and successful `deploy-test-cvm`:

1. Verify `/healthz`, attestation, migration head `0088_agent_jobs_available_at`, and fixed three-pool child counts.
2. Create a dedicated V2 test user and obtain a valid baseline Chat reply.
3. Use a test-only provider route that returns a recoverable 5xx/timeout; verify Chat remains durable and the profile Job becomes delayed `pending` with content-free error metadata.
4. Restore the provider without sending another Chat or forcing Dream; wait for `available_at` and verify the same profile Job is claimed and reaches profile state `ok`.
5. Run several Chat turns and one Dream trigger; verify at most one active profile Job, no fifth child, no `progress pipe closed`, no unconfirmed termination, and no foreground latency regression.
6. Delete the synthetic account and record aggregate/content-free evidence only.

- [ ] **Step 8: Final verification commit if test evidence changes the report**

```bash
git add docs/superpowers/reports/2026-08-14-v2-profile-durable-retry-verification.md
git commit -m "docs(v2): record test profile retry evidence"
```

Do not propose production promotion until the test report contains the automatic recovery timestamps, queue ready/delayed metrics, worker child counts, and clean watchdog logs.
