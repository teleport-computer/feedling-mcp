# Runtime V2 Three-Pool Slot Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Use `superpowers:test-driven-development` for every behavior change and `superpowers:verification-before-completion` before claiming a task complete.

**Goal:** From the current `test` branch, replace Runtime V2's shared multi-slot worker with the complete 4-foreground/2-wake/2-heavy runtime, then validate it in the test environment.

**Architecture:** Runtime V2 has one topology only: a `serve-worker` parent owns eight one-slot child processes grouped into three logical pools. PostgreSQL remains the source of truth for Job ownership and recovery; Chat atomically terminalizes/requeues conflicting same-user background work, while a typed notification asks the owning child to stop promptly. The parent watchdog kills only the affected slot, recovers only that slot's active claim, and then respawns it. Enclave concurrency is enforced by a parent IPC broker, not by process-local semaphores.

**Tech Stack:** Python 3.11, `asyncio`, `multiprocessing` with spawn, psycopg 3 and `psycopg_pool`, PostgreSQL/Alembic, pytest, Docker Compose, and the existing admin JSON metrics endpoint.

## Global Constraints

- Start implementation from the current local `test` tip. The single implementation PR targets `test`; this plan ends after test-environment validation and does not promote to pre or prod.
- Three pools and one-process-per-slot are unconditional Runtime V2 behavior. Do not add `FEEDLING_V2_POOL_MODE`, a legacy supervisor branch, or feature switches for Chat preemption or slot isolation.
- Do not change the public return contract of `backend.db.chat_append_and_enqueue`: it remains `tuple[int, int | None]`.
- PostgreSQL is authoritative. IPC messages accelerate cancellation and admission but must never be required for durable correctness.
- A child process owns exactly one execution slot. Do not recreate several `asyncio` slot tasks inside one child.
- Chat preemption and Chat enqueue happen in the same database transaction. Publish cancellation notifications only after that transaction commits.
- A watchdog must use this order: advertise slot unavailable, snapshot active claim, kill child, recover exactly that claim conditionally, then start the replacement child.
- All claim recovery updates must compare `job_id`, `claimed_by`, and active status so a stale watchdog cannot overwrite a newer owner.
- Preserve terminal-failure outbox/reconciler semantics. A failed Chat still gets at most one durable user-visible reply.
- Parent DB pool maximum is 8 and each child DB pool maximum is 2. Do not apply the old all-slots-in-one-process sizing formula to every child.
- Enclave admission is instance-wide: total 4 permits, with initial reservations of foreground 2, wake 1, heavy 1. Unused reservations may be borrowed without violating a waiting pool's reservation.
- Only one Heavy slot may claim `profile`; the other Heavy slot handles the remaining heavy lanes.
- Non-secret pool and capacity settings belong directly in `deploy/docker-compose.phala.test.yaml`. Credentials and provider keys remain in encrypted environment configuration.
- Any architecture, trust-boundary, deployment-topology, or user-visible behavior change must update the affected public docs and `Unreleased` changelog in the same phase.
- Use one behavior change per commit where practical. Do not mix formatting or unrelated cleanup into these commits.
- All PostgreSQL tests must run against the test DSN and must not silently skip:

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests -q
  ```

## Target Runtime Layout

| Pool | Slots | Lanes | Per-job watchdog |
|---|---:|---|---|
| `foreground` | 4 | `chat`, `manual_wake` | 240s stall / 1500s absolute |
| `wake` | 2 | `heartbeat`, `scheduled`, `screen_watch` | 240s stall / 900s absolute |
| `heavy-0` | 1 | `profile`, `dream`, `capture`, `maintenance`, `trajectory_review` | 240s stall initially, then 120s after batching / 1200s absolute |
| `heavy-1` | 1 | `dream`, `capture`, `maintenance`, `trajectory_review` | 240s stall initially, then 120s after batching / 1200s absolute |

The logical pool name stored in worker heartbeat rows is `heavy` for both Heavy slots; the profile allowlist difference is local slot configuration.

## Delivery Phases

- **Checkpoint 1 — correctness primitives:** pool-aware queries, atomic Chat preemption, typed cancellation, exact recovery APIs, and owner fences.
- **Checkpoint 2 — failure-domain isolation:** one process per slot, three pool lane allowlists, pool-aware capacity and admission.
- **Checkpoint 3 — bounded resources and test readiness:** Enclave broker, DB limits, Profile batching/progress, metrics, test YAML, and public docs.

Keep all checkpoints on one feature branch and open one implementation PR targeting `test` only after Tasks 1–16 pass. Merging that PR is the first test deployment, so `test` never receives an incomplete hybrid topology. Pre/prod promotion is a separate decision after this plan's test evidence is reviewed.

## Approved Requirement Overrides

This plan incorporates the user's post-spec decision and overrides the design document wherever it describes a legacy mode, independent rollout switches, phased deployment of partial topology, or pre/prod rollout:

- Runtime V2 always starts all three pools and one process per slot.
- `FEEDLING_V2_POOL_MODE`, `FEEDLING_V2_MAX_WORKERS`, `FEEDLING_V2_CHAT_PREEMPTION_ENABLED`, and `FEEDLING_V2_SLOT_PROCESS_ISOLATION` are retired from the test service.
- Tasks 1–16 land together in one PR to `test`; Task 17 validates only the test environment.
- Operational recovery redeploys the previous known-good image/commit. There is no configuration switch back to the shared child topology.

Before Task 1, verify the branch is based on the local `test` tip:

```bash
git merge-base --is-ancestor test HEAD
git rev-list --left-right --count test...HEAD
```

Expected: the first command exits 0 and the second prints `0 N`, where `N` is the number of feature-branch commits. If the left count is non-zero, rebase before changing implementation files.

---

## Task 1: Add Typed Pool Configuration and Validation

**Files:**

- Create: `backend/model_api_runtime/v2/pool_config.py`
- Test: `tests/test_v2_pool_config.py`

- [ ] **Step 1: Write failing tests for the unconditional 4/2/2 layout, lane allowlists, and invalid values**

  ```python
  def test_three_pool_defaults_build_eight_one_slot_specs(monkeypatch):
      config = RuntimePoolConfig.from_env()

      assert [slot.pool for slot in config.slots].count("foreground") == 4
      assert [slot.pool for slot in config.slots].count("wake") == 2
      assert [slot.pool for slot in config.slots].count("heavy") == 2
      assert config.slots[0].lanes == frozenset({"chat", "manual_wake"})
      assert sum("profile" in slot.lanes for slot in config.slots) == 1


  def test_three_pool_rejects_zero_foreground_slots(monkeypatch):
      monkeypatch.setenv("FEEDLING_V2_FOREGROUND_SLOTS", "0")
      with pytest.raises(ValueError, match="foreground"):
          RuntimePoolConfig.from_env()
  ```

- [ ] **Step 2: Run the focused test and confirm it fails because the module does not exist**

  ```bash
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_pool_config.py -q
  ```

  Expected: FAIL during import of `backend.model_api_runtime.v2.pool_config`.

- [ ] **Step 3: Implement immutable pool and slot specifications**

  ```python
  from dataclasses import dataclass
  from typing import Literal

  PoolName = Literal["foreground", "wake", "heavy"]


  @dataclass(frozen=True)
  class SlotSpec:
      pool: PoolName
      index: int
      lanes: frozenset[str]
      stall_budget_sec: float
      absolute_budget_sec: float

      @property
      def slot_id(self) -> str:
          return f"{self.pool}-{self.index}"


  @dataclass(frozen=True)
  class RuntimePoolConfig:
      slots: tuple[SlotSpec, ...]
      profile_instance_concurrency: int
      enclave_instance_concurrency: int
  ```

  Implement `RuntimePoolConfig.from_env() -> RuntimePoolConfig`. It always constructs all three pools; there is no mode field or legacy fallback. Set initial budgets to Foreground `240/1500` seconds, Wake `240/900`, and Heavy `240/1200`; tighten Heavy stall to 120 only in the later Profile task after batched progress tests pass. Reject negative counts, no foreground slots, profile concurrency other than `1` in the initial implementation, or an Enclave total lower than the three reserved pool minima. Add a test proving `FEEDLING_V2_POOL_MODE`, `FEEDLING_V2_MAX_WORKERS`, `FEEDLING_V2_CHAT_PREEMPTION_ENABLED`, and `FEEDLING_V2_SLOT_PROCESS_ISOLATION` do not affect the parsed topology when present in an operator shell.

- [ ] **Step 4: Run the tests and confirm they pass**

  ```bash
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_pool_config.py -q
  ```

  Expected: PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add backend/model_api_runtime/v2/pool_config.py tests/test_v2_pool_config.py
  git commit -m "feat(v2): define three-pool runtime configuration"
  ```

---

## Task 2: Make Worker Heartbeats and Admission Pool-Aware

**Files:**

- Create: `backend/alembic/versions/0085_v2_worker_pool_heartbeats.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `tests/test_v2_jobs_migration.py`
- Modify: `tests/test_v2_worker_heartbeat.py`
- Modify: `tests/test_v2_capacity_health.py`

- [ ] **Step 1: Write failing migration and store tests**

  Add assertions that migration `0085_v2_worker_pool_heartbeats` has `down_revision = "0084_wake_support_indexes"`, adds `pool TEXT NOT NULL DEFAULT 'unassigned'`, adds `runtime_state JSONB NOT NULL DEFAULT '{}'::jsonb`, and creates an index beginning with `(pool, kind, beat_at DESC)`.

  Add behavior tests:

  ```python
  jobs_store.record_worker_heartbeat("worker:foreground", kind="turn", capacity=4, pool="foreground")
  jobs_store.record_worker_heartbeat("worker:heavy", kind="turn", capacity=2, pool="heavy")

  assert jobs_store.live_worker_capacity(pool="foreground") == 4
  assert jobs_store.live_worker_capacity(pool="heavy") == 2
  assert jobs_store.live_worker_capacity() == 6
  ```

  Also cover `workers_alive(pool="foreground")` and `live_worker_count(pool="foreground")`.

- [ ] **Step 2: Run focused tests and confirm the missing arguments/schema fail**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_jobs_migration.py tests/test_v2_worker_heartbeat.py tests/test_v2_capacity_health.py -q
  ```

  Expected: FAIL on absent migration and unsupported `pool` parameters.

- [ ] **Step 3: Add migration `0085`**

  Use Alembic operations to add the non-null column with server default `unassigned`. Existing/stale pre-deploy heartbeat rows remain distinguishable and never count as foreground capacity; all new turn and Genesis heartbeat call sites must write an explicit pool. Create the pool/kind/freshness index and remove both columns/indexes in downgrade.

- [ ] **Step 4: Require explicit pool identity from every new heartbeat caller**

  ```python
  def record_worker_heartbeat(
      worker_id: str,
      *,
      pool: str,
      kind: str = "turn",
      capacity: int = 1,
      runtime_state: dict[str, object] | None = None,
  ) -> None:
      """Upsert the liveness, capacity, pool, and bounded runtime snapshot."""


  def live_worker_capacity(
      *,
      within_sec: int = 30,
      kind: str = "turn",
      pool: str | None = None,
  ) -> int:
      """Return fresh executable capacity, optionally restricted to one pool."""
  ```

  Apply the optional pool predicate consistently to count/alive/capacity queries. Use distinct heartbeat identities such as `f"{worker_id}:{pool}"` when the parent publishes multiple pool rows. Turn callers use `foreground`, `wake`, or `heavy`; the Genesis parent thread uses `control`. Queries that gate Chat explicitly ignore `unassigned` and `control`.

- [ ] **Step 5: Make Runtime V2 health require foreground capacity**

  In `serve_worker.py`, return healthy for Chat admission only when fresh `foreground` capacity is positive. Expose wake/heavy capacity separately for diagnostics; do not let a healthy Heavy pool make Chat appear healthy.

- [ ] **Step 6: Run focused tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_jobs_migration.py tests/test_v2_worker_heartbeat.py tests/test_v2_capacity_health.py -q
  ```

  Expected: PASS.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/alembic/versions/0085_v2_worker_pool_heartbeats.py backend/model_api_runtime/v2/jobs_store.py backend/model_api_runtime/v2/serve_worker.py tests/test_v2_jobs_migration.py tests/test_v2_worker_heartbeat.py tests/test_v2_capacity_health.py
  git commit -m "feat(v2): track runtime capacity by pool"
  ```

---

## Task 3: Make Admission Metrics Lane-Aware

**Files:**

- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `tests/test_v2_jobs_store.py`
- Modify: `tests/test_v2_capacity_health.py`

- [ ] **Step 1: Write failing tests for filtered inflight counts and foreground admission**

  ```python
  assert jobs_store.inflight_job_count(lanes={"chat", "manual_wake"}) == 1
  assert jobs_store.inflight_job_count(lanes={"profile", "dream"}) == 7
  ```

  Seed one Chat and several background Jobs. Assert the Chat admission calculation uses only foreground in-flight work and foreground live capacity. Keep an unfiltered assertion for backwards compatibility.

  Also assert the estimator continues to use completed Chat service time only, and `PENDING_CHAT_TTL_SEC` remains the independent product queue deadline of 120 seconds. Do not derive TTL from mean service time; an operational rollout may explicitly set 180 seconds as temporary buffer, but that is not the fix.

- [ ] **Step 2: Run focused tests and confirm failure**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_jobs_store.py tests/test_v2_capacity_health.py -q
  ```

  Expected: FAIL because `inflight_job_count` has no `lanes` parameter and admission uses global capacity/work.

- [ ] **Step 3: Add lane filtering with parameterized SQL**

  ```python
  def inflight_job_count(*, lanes: set[str] | None = None) -> int:
      lane_clause = ""
      params: list[object] = []
      if lanes:
          lane_clause = " AND lane = ANY(%s)"
          params.append(sorted(lanes))
  ```

  Do not interpolate lane names into SQL. Chat admission always passes `{"chat", "manual_wake"}` and queries `pool="foreground"`. Keep the unfiltered store method only for Admin's all-lane observability, not as a runtime fallback.

- [ ] **Step 4: Run focused tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_jobs_store.py tests/test_v2_capacity_health.py -q
  ```

  Expected: PASS.

- [ ] **Step 5: Commit**

  ```bash
  git add backend/model_api_runtime/v2/jobs_store.py backend/model_api_runtime/v2/serve_worker.py tests/test_v2_jobs_store.py tests/test_v2_capacity_health.py
  git commit -m "fix(v2): isolate foreground admission metrics"
  ```

---

## Task 4: Atomically Preempt Same-User Background Work When Chat Arrives

**Files:**

- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/db.py`
- Modify: `tests/test_v2_send_enqueue_atomic.py`
- Modify: `tests/test_chat_send_v2_enqueue.py`
- Modify: `tests/test_v2_jobs_store.py`

- [ ] **Step 1: Write failing transactional tests**

  Cover all cases below:

  - An active same-user `profile` Job is terminalized as `preempted_by_chat`, its claim fields are cleared, and a Chat Job is inserted in the same transaction.
  - Same-user `scheduled`/`capture` work is requeued according to the existing lane-specific recovery policy rather than duplicated.
  - An active same-user Chat Job is not preempted.
  - Another user's background Job is untouched.
  - If Chat insert fails, preemption rolls back.
  - `chat_append_and_enqueue` still returns the existing two-tuple.

  Use the existing fixtures and status vocabulary from `tests/test_v2_jobs_store.py`; do not invent a new terminal status if the schema constrains it. Store `preempted_by_chat` in the existing error/reason field.

- [ ] **Step 2: Run the focused tests and confirm failure**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_send_enqueue_atomic.py tests/test_chat_send_v2_enqueue.py tests/test_v2_jobs_store.py -q
  ```

  Expected: FAIL because background claims remain active and block the new Chat.

- [ ] **Step 3: Add a typed preemption result and cursor-scoped operation**

  ```python
  @dataclass(frozen=True)
  class PreemptedJob:
      job_id: int
      user_id: int
      lane: str
      claimed_by: str
      recovery: Literal["terminal", "requeued"]


  def preempt_active_for_chat_on_cursor(
      cur: psycopg.Cursor,
      *,
      user_id: int,
  ) -> list[PreemptedJob]:
      """Preempt active same-user non-Chat Jobs inside the caller transaction."""
  ```

  Lock matching active non-Chat rows with `FOR UPDATE` and apply this exact mapping:

  - `manual_wake`, `heartbeat`, `screen_watch`: `superseded:foreground_chat_preempted`; preserve unconsumed context for the normal successor.
  - `profile`, `dream`, `maintenance`, `trajectory_review`: `superseded:foreground_chat_preempted`; allow normal due/backoff logic to recreate later work.
  - `scheduled`: cancel this execution and create or retain exactly one successor for the same durable reminder.
  - `capture`: invoke the existing prepared-batch cancellation/recovery protocol, preserve uncommitted input, and enqueue exactly one successor.

  Every update must include the row's current `claimed_by` and active status in its predicate. Reuse existing scheduling/coalescing helpers so preemption cannot create duplicate Jobs.

- [ ] **Step 4: Call preemption inside `chat_append_and_enqueue`'s transaction**

  Perform it after the user/runtime-state lock and before Chat coalescing/insertion. Keep the public return tuple unchanged. Carry the `PreemptedJob` list through private attempt state and invoke a post-commit callback hook; never send NOTIFY before commit.

- [ ] **Step 5: Run focused tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_send_enqueue_atomic.py tests/test_chat_send_v2_enqueue.py tests/test_v2_jobs_store.py -q
  ```

  Expected: PASS, including transaction rollback behavior.

- [ ] **Step 6: Commit**

  ```bash
  git add backend/model_api_runtime/v2/jobs_store.py backend/db.py tests/test_v2_send_enqueue_atomic.py tests/test_chat_send_v2_enqueue.py tests/test_v2_jobs_store.py
  git commit -m "fix(v2): atomically preempt background work for chat"
  ```

---

## Task 5: Extend the Existing Wake Bus with Typed Job Cancellation

**Files:**

- Modify: `backend/core/wake_bus.py`
- Modify: `backend/db.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `tests/test_wake_bus.py`
- Modify: `tests/test_v2_send_enqueue_atomic.py`

- [ ] **Step 1: Write failing codec, post-commit, and targeting tests**

  ```python
  event = JobCancellation(job_id=3694, claimed_by="worker:heavy:0:g7", reason="preempted_by_chat")
  assert JobCancellation.from_payload(event.to_payload()) == event
  ```

  Assert invalid/oversized payloads are rejected, rollback publishes nothing, commit publishes one notification per preempted Job, and a parent ignores an event whose `claimed_by` no longer matches its slot registry.

- [ ] **Step 2: Run tests and confirm the typed API is absent**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_wake_bus.py tests/test_v2_send_enqueue_atomic.py -q
  ```

  Expected: FAIL because `wake_bus` has no typed Job-cancellation API.

- [ ] **Step 3: Add typed cancellation to the existing wake channel**

  ```python
  @dataclass(frozen=True)
  class JobCancellation:
      job_id: int
      claimed_by: str
      reason: str


  def notify_job_cancel(event: JobCancellation) -> None:
      """Publish one compact job_cancel event on feedling_wake after commit."""

  def register_job_cancel_handler(fn: Callable[[JobCancellation], None]) -> None:
      """Register an idempotent typed handler on the existing wake listener."""
  ```

  Reuse `PG_CHANNEL = "feedling_wake"` and the existing listener/reconnect lifecycle. Add logical channel `job_cancel` and compact keys for `job_id`, `claimed_by`, and `reason`; keep existing per-user handler behavior unchanged. PostgreSQL NOTIFY payloads are bounded, so reject unexpected keys/types and oversized values before dispatch.

- [ ] **Step 4: Publish only after Chat transaction commit and route only to the matching slot**

  The Chat DB function collects preemptions in the successful attempt and calls `notify_job_cancel` after the transaction exits. The parent listener looks up `claimed_by -> SlotKey` in its current registry and terminates/restarts only that child after the database commit. Durable preemption already invalidates the lease; the child kill prevents uninstrumented in-flight work from continuing. This behavior is unconditional and may be merged before the slot-fleet task, but the combined PR set must not deploy to test until one-process-per-slot is present.

- [ ] **Step 5: Run focused tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_wake_bus.py tests/test_v2_send_enqueue_atomic.py -q
  ```

  Expected: PASS.

- [ ] **Step 6: Commit**

  ```bash
  git add backend/core/wake_bus.py backend/db.py backend/model_api_runtime/v2/serve_worker.py tests/test_wake_bus.py tests/test_v2_send_enqueue_atomic.py
  git commit -m "feat(v2): cancel preempted jobs through typed notifications"
  ```

---

## Task 6: Carry Exact Active-Job Identity Through the Child Protocol

**Files:**

- Create: `backend/model_api_runtime/v2/slot_protocol.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `backend/model_api_runtime/v2/turn_child.py`
- Modify: `backend/model_api_runtime/v2/child_supervisor.py`
- Modify: `tests/test_v2_child_supervisor.py`
- Modify: `tests/test_v2_worker_heartbeat.py`

- [ ] **Step 1: Write failing protocol and supervisor tests**

  Test strict decoding for messages such as:

  ```python
  SlotProgress(
      slot_id="heavy-0",
      slot_generation="g7",
      monotonic_at=123.4,
      turn_start=120.0,
      stage="profile.cards.batch",
      active_job=ActiveJobIdentity(job_id=3694, lane="profile", claimed_by="worker:heavy:0:g7"),
  )
  ```

  Assert a claim sends progress with active identity before Job execution, progress stages preserve identity, and the idle message clears it. Reject malformed messages rather than indexing arbitrary tuples.

- [ ] **Step 2: Run tests and confirm failure**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_child_supervisor.py tests/test_v2_worker_heartbeat.py -q
  ```

  Expected: FAIL because current IPC contains only tuple timestamps and slot IDs.

- [ ] **Step 3: Implement typed protocol dataclasses and compact wire encoding**

  ```python
  @dataclass(frozen=True)
  class ActiveJobIdentity:
      job_id: int
      lane: str
      claimed_by: str


  @dataclass(frozen=True)
  class SlotProgress:
      slot_id: str
      slot_generation: str
      monotonic_at: float
      turn_start: float | None
      stage: str
      active_job: ActiveJobIdentity | None
  ```

  Put encode/decode in `slot_protocol.py` so child and parent share one schema. Include generation on every message to discard late messages from a killed process.

- [ ] **Step 4: Extend `_slot_loop` progress callbacks**

  Replace `(slot_id, turn_start)` callbacks with a callback accepting `SlotProgress` or the exact fields needed to build one. Emit stages at `idle`, `claimed`, lane dispatch, external-model request boundaries, durable completion, and failure cleanup.

- [ ] **Step 5: Make `ChildSupervisor` expose an immutable current snapshot**

  Add `snapshot() -> SlotProgress | None` because every child owns exactly one slot. Delete parsing/state branches for the old tuple protocol; there is no multi-slot child compatibility path.

- [ ] **Step 6: Run focused tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_child_supervisor.py tests/test_v2_worker_heartbeat.py -q
  ```

  Expected: PASS.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/model_api_runtime/v2/slot_protocol.py backend/model_api_runtime/v2/worker.py backend/model_api_runtime/v2/turn_child.py backend/model_api_runtime/v2/child_supervisor.py tests/test_v2_child_supervisor.py tests/test_v2_worker_heartbeat.py
  git commit -m "refactor(v2): report exact active job per slot"
  ```

---

## Task 7: Recover Only the Claim Owned by a Killed Slot

**Files:**

- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Create: `backend/model_api_runtime/v2/claim_recovery.py`
- Modify: `backend/model_api_runtime/v2/child_supervisor.py`
- Modify: `backend/model_api_runtime/v2/watchdog.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `tests/test_v2_jobs_store.py`
- Modify: `tests/test_v2_child_supervisor.py`
- Modify: `tests/test_v2_watchdog.py`
- Modify: `tests/test_v2_p0_pool_safety.py`

- [ ] **Step 1: Write failing exact-recovery and ordering tests**

  Cover:

  - `recover_killed_job(job_id, claimed_by)` changes one matching active row immediately.
  - A mismatched or already-finished claim is a no-op.
  - Chat enters the existing terminal failure/outbox path with reason `slot_watchdog_timeout`.
  - Retryable background lanes use their existing safe requeue policy.
  - Another Job for the same user is untouched.
  - Watchdog call order is `capacity=0 -> snapshot -> kill -> recover -> start`.
  - If immediate recovery raises, the replacement still starts and the exact request enters a bounded retry queue; the normal lease reaper remains the final fallback.

- [ ] **Step 2: Run focused tests and confirm failure**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_jobs_store.py tests/test_v2_child_supervisor.py tests/test_v2_watchdog.py tests/test_v2_p0_pool_safety.py -q
  ```

  Expected: FAIL because `kill_and_respawn` is indivisible and recovery waits for lease reaping.

- [ ] **Step 3: Add conditional exact recovery**

  ```python
  def recover_killed_job(
      *,
      job_id: int,
      claimed_by: str,
      reason: str = "slot_watchdog_timeout",
  ) -> dict[str, object] | None:
      """Conditionally recover the one active claim still owned by claimed_by."""
  ```

  Reuse the same terminal/requeue helper used by `reap_stuck_job_rows`; do not fork failure semantics. The SQL predicate must include `id = %s`, `claimed_by = %s`, and `status IN ('claimed', 'running')`. Return the affected row or `None`.

- [ ] **Step 4: Split supervisor lifecycle operations**

  Replace `kill_and_respawn()` with explicit `kill() -> ActiveJobIdentity | None` and `start()`. `kill()` snapshots identity before terminating, joins the process, closes old IPC, and advances generation. `start()` creates fresh IPC/process state.

- [ ] **Step 5: Implement the watchdog sequence with `try/finally` around restart**

  Ensure the replacement process is started even if notification or DB recovery fails. Never call a user-wide recovery query. Log pool, slot, generation, job ID, lane, owner, watchdog reason, and recovery result.

  Implement `claim_recovery.py` as a parent-owned queue keyed by `(job_id, claimed_by)`, with at most 256 entries, exponential delays of 1/2/4/8/16 seconds, five attempts, and duplicate-key coalescing. After attempts are exhausted, log a bounded error and leave the row to the existing lease reaper. Stop/drain the queue with the parent lifecycle.

- [ ] **Step 6: Run focused tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_jobs_store.py tests/test_v2_child_supervisor.py tests/test_v2_watchdog.py tests/test_v2_p0_pool_safety.py -q
  ```

  Expected: PASS.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/model_api_runtime/v2/jobs_store.py backend/model_api_runtime/v2/claim_recovery.py backend/model_api_runtime/v2/child_supervisor.py backend/model_api_runtime/v2/watchdog.py backend/model_api_runtime/v2/serve_worker.py tests/test_v2_jobs_store.py tests/test_v2_child_supervisor.py tests/test_v2_watchdog.py tests/test_v2_p0_pool_safety.py
  git commit -m "fix(v2): recover only a killed slot claim"
  ```

---

## Task 8: Fence Stale Owners and Separate Failure Notices by Cause

**Files:**

- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `backend/notices/catalog.py`
- Modify: `tools/chat_resident_consumer.py`
- Create: `tests/test_v2_job_lease_fencing.py`
- Modify: `tests/test_v2_effect_sinks.py`
- Modify: `tests/test_v2_effect_outbox.py`
- Modify: `tests/test_catalog_consumer_parity.py`

- [ ] **Step 1: Write failing stale-owner tests at every irreversible boundary**

  Create a claimed Job, replace or invalidate its owner, then assert the stale owner cannot:

  - transition `claimed -> running` or renew the lease;
  - call `finish_chat_job` or `finish_wake_job`;
  - commit a prepared Capture batch;
  - enqueue or adopt reply/tool effects;
  - update Profile state/CAS output;
  - write a successful terminal metric that would make the preempted Job look completed.

  Each rejection must return a bounded false/stale result and increment a content-free `stale_owner_write_rejection` aggregate; it must not raise after an external effect was already issued. Add one positive control for the current owner at each boundary.

- [ ] **Step 2: Write failing failure-classification and user-text tests**

  Add three stable classes and exact Chinese messages to the shared catalog/consumer parity table:

  - `platform_queue_timeout`: `这条消息没有及时开始处理，也没有生成回复。请稍后再试，不要连续发送。`
  - `platform_execution_timeout`: `这轮回复因系统执行异常没有完成，也不会重复生成回复。请稍后再试，不要连续发送。`
  - `provider_timeout`: `你配置的模型服务这次没有及时响应。请先检查模型渠道稳定性，不要连续重发。`

  Assert `queue_timeout -> platform_queue_timeout`, `slot_watchdog_timeout -> platform_execution_timeout`, and provider transport timeouts -> `provider_timeout`. Do not promise automatic retry for a terminal Chat Job.

- [ ] **Step 3: Run focused tests and confirm failure**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_job_lease_fencing.py tests/test_v2_effect_sinks.py tests/test_v2_effect_outbox.py tests/test_catalog_consumer_parity.py -q
  ```

  Expected: FAIL on missing fence coverage and missing error classes.

- [ ] **Step 4: Centralize active-owner validation**

  Add one cursor-scoped helper that verifies `job_id`, `claimed_by`, active status, and unexpired lease. Make each irreversible DB boundary call it in the same transaction as its write. For Enclave/Profile writes performed outside PostgreSQL, re-check ownership immediately before dispatch and again before adopting the result. Preserve existing effect IDs so already-issued external calls remain idempotent.

- [ ] **Step 5: Map stable error codes without copying exception text**

  Extend `_terminal_error_class` and the worker's provider exception classifier. Only the three fixed codes/classes cross into the terminal-failure outbox. Keep provider URL, HTTP body, exception message, and credentials out of the stored error fields and notice text.

- [ ] **Step 6: Run focused tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_job_lease_fencing.py tests/test_v2_effect_sinks.py tests/test_v2_effect_outbox.py tests/test_catalog_consumer_parity.py -q
  ```

  Expected: PASS.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/model_api_runtime/v2/jobs_store.py backend/model_api_runtime/v2/worker.py backend/model_api_runtime/v2/serve_worker.py backend/notices/catalog.py tools/chat_resident_consumer.py tests/test_v2_job_lease_fencing.py tests/test_v2_effect_sinks.py tests/test_v2_effect_outbox.py tests/test_catalog_consumer_parity.py
  git commit -m "fix(v2): fence stale jobs and classify timeout notices"
  ```

---

## Task 9: Introduce One-Process-Per-Slot Fleet and Three Lane Pools

**Files:**

- Create: `backend/model_api_runtime/v2/pool_supervisor.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/model_api_runtime/v2/turn_child.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Create: `tests/test_v2_pool_supervisor.py`
- Modify: `tests/test_v2_claim_reservation.py`
- Modify: `tests/test_v2_serve_worker.py`
- Modify: `tests/test_v2_capacity_health.py`

- [ ] **Step 1: Write failing fleet topology and lane-isolation tests**

  Assert:

  - Default three-pool configuration starts eight `ChildSupervisor` instances.
  - Every child receives one slot ID, one generation, and one explicit lane set.
  - Foreground children cannot claim Profile; Heavy children cannot claim Chat.
  - Exactly one Heavy child receives `profile` in its lane set.
  - Within one pool, higher numeric priority claims before lower priority; equal priority remains FIFO by `created_at`.
  - Killing one child does not change the PIDs/generations of the other seven.
  - Pool heartbeat capacity equals the number of healthy children in that pool.
  - If a cancellation NOTIFY is dropped, parent reconciliation detects the no-longer-owned active snapshot and restarts only that slot within 5 seconds.
  - No startup path reads `FEEDLING_V2_MAX_WORKERS` or starts the old multi-slot child.

- [ ] **Step 2: Run focused tests and confirm failure**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_pool_supervisor.py tests/test_v2_claim_reservation.py tests/test_v2_serve_worker.py tests/test_v2_capacity_health.py -q
  ```

  Expected: FAIL because the current parent starts one multi-slot `turn_child`.

- [ ] **Step 3: Implement the fleet abstraction**

  ```python
  @dataclass(frozen=True)
  class SlotKey:
      pool: PoolName
      index: int


  class SlotFleet:
      def start_all(self) -> None:
          """Start every configured one-slot child."""

      def stop_all(self) -> None:
          """Stop every child and close its IPC endpoints."""

      def snapshots(self) -> dict[SlotKey, SlotProgress | None]:
          """Return immutable current progress by slot."""

      def supervisor(self, key: SlotKey) -> ChildSupervisor:
          """Return the supervisor for one exact slot."""

      def find_claim(self, claimed_by: str) -> SlotKey | None:
          """Locate the current slot whose active claim has this owner."""
  ```

  Keep mutation on the parent event-loop thread or protect the registry with one lock. A cancellation listener should enqueue work onto the loop rather than mutate the fleet from its listener thread.

- [ ] **Step 4: Change the child entry point to run one slot**

  ```python
  def main(
      conn,
      worker_id: str,
      poll_interval: float,
      pool: str,
      slot_id: str,
      slot_generation: str,
      lanes: tuple[str, ...],
      db_pool_max: int = 2,
  ) -> None:
      """Initialize one child process and run exactly one configured slot."""
  ```

  Call one `_slot_loop` directly with `lanes=set(lanes)`. Delete the old child-level `MAX_WORKERS` fan-out and `_reserved_lane_slots`; no Runtime V2 path reads `FEEDLING_V2_MAX_WORKERS`.

- [ ] **Step 5: Replace the single supervisor/watchdog in `_serve` with one pair per slot**

  Build `SlotFleet` from `RuntimePoolConfig`. Start one watchdog task per child. Aggregate health by pool and write pool heartbeat rows. Route job-cancellation events through `find_claim`.

  Add one parent reconciliation loop with a 2-second interval. In one batched store query, validate every snapshot's `(job_id, claimed_by)` against active status and lease; if an owner has been invalidated by committed preemption, terminate/restart only that snapshot's slot. Make the query/kill generation-fenced and idempotent so it is safe when NOTIFY and reconciliation race.

- [ ] **Step 6: Run focused tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_pool_supervisor.py tests/test_v2_claim_reservation.py tests/test_v2_serve_worker.py tests/test_v2_capacity_health.py -q
  ```

  Expected: PASS.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/model_api_runtime/v2/pool_supervisor.py backend/model_api_runtime/v2/jobs_store.py backend/model_api_runtime/v2/turn_child.py backend/model_api_runtime/v2/worker.py backend/model_api_runtime/v2/serve_worker.py tests/test_v2_pool_supervisor.py tests/test_v2_claim_reservation.py tests/test_v2_serve_worker.py tests/test_v2_capacity_health.py
  git commit -m "feat(v2): isolate runtime work into one-slot processes"
  ```

---

## Task 10: Enforce Instance-Wide Enclave Admission

**Files:**

- Create: `backend/model_api_runtime/v2/enclave_broker.py`
- Modify: `backend/model_api_runtime/v2/pool_supervisor.py`
- Modify: `backend/model_api_runtime/v2/turn_child.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Create: `tests/test_v2_enclave_broker.py`
- Modify: `tests/test_v2_serve_worker.py`

- [ ] **Step 1: Write failing deterministic broker tests**

  Use a fake clock/queue rather than sleeps. Cover:

  - At most four grants across all children.
  - Reservations foreground 2, wake 1, heavy 1 are available when those pools wait.
  - Idle reservations can be borrowed.
  - Borrowing priority is Foreground, then Wake, then Heavy; Heavy cannot consume either of Foreground's final two reserved permits while Foreground waits.
  - Release wakes the oldest eligible waiter.
  - Killing generation `g7` releases all of that generation's permits and ignores late releases.
  - Cancelling a waiting request removes it without leaking capacity.

- [ ] **Step 2: Run focused tests and confirm failure**

  ```bash
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_enclave_broker.py tests/test_v2_serve_worker.py -q
  ```

  Expected: FAIL because only process-local semaphores exist.

- [ ] **Step 3: Implement parent-side broker state**

  ```python
  @dataclass(frozen=True)
  class EnclaveRequest:
      request_id: str
      pool: PoolName
      slot_id: str
      slot_generation: str


  class EnclaveBroker:
      def request(self, request: EnclaveRequest) -> bool:
          """Grant immediately or enqueue the request and return False."""

      def release(self, request_id: str, slot_generation: str) -> None:
          """Release a matching grant and schedule eligible waiters."""

      def drop_generation(self, slot_generation: str) -> None:
          """Release grants and remove waiters belonging to a dead child."""
  ```

  Maintain granted requests and FIFO waiters explicitly. The eligibility rule is: grant when total capacity exists and doing so does not reduce free capacity below reservations needed by other pools that currently have waiters. Document this rule in the class docstring and tests.

- [ ] **Step 4: Add duplex child IPC for acquire/release**

  The child sends `acquire`, waits for a matching `granted`, performs the Enclave operation, and sends `release` in `finally`. Add cancellation handling so a preempted/killed Job does not leave a waiter. The parent calls `drop_generation` immediately after child death.

- [ ] **Step 5: Replace process-local `ENCLAVE_CONCURRENCY`**

  Delete the Runtime V2 process-local semaphore. Route every existing Enclave read/write call through one broker-IPC helper so Profile, Chat, wake, and heavy calls are all counted.

- [ ] **Step 6: Run focused tests**

  ```bash
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_enclave_broker.py tests/test_v2_serve_worker.py -q
  ```

  Expected: PASS.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/model_api_runtime/v2/enclave_broker.py backend/model_api_runtime/v2/pool_supervisor.py backend/model_api_runtime/v2/turn_child.py backend/model_api_runtime/v2/worker.py tests/test_v2_enclave_broker.py tests/test_v2_serve_worker.py
  git commit -m "feat(v2): broker enclave concurrency across slots"
  ```

---

## Task 11: Bound Parent and Child Database Pools

**Files:**

- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `backend/model_api_runtime/v2/turn_child.py`
- Modify: `backend/db.py`
- Modify: `tests/test_v2_serve_worker.py`
- Modify: `tests/test_v2_p0_pool_safety.py`

- [ ] **Step 1: Write failing sizing tests**

  Assert startup configures the parent with `FEEDLING_DB_POOL_MAX_SIZE=8`, passes `db_pool_max=2` to every child, and never invokes `_configure_db_pool_capacity(max_workers=8)` inside a child. Delete the old `MAX_WORKERS`-derived Runtime V2 sizing formula.

- [ ] **Step 2: Run focused tests and confirm failure**

  ```bash
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_serve_worker.py tests/test_v2_p0_pool_safety.py -q
  ```

  Expected: FAIL because DB sizing assumes all slots share one process.

- [ ] **Step 3: Configure DB limits before each process initializes `backend.db`**

  In the parent, set an explicit parent size of 8 before migrations/assembly. In each spawned child, set size 2 before calling DB initialization. Add a small pure helper returning `(parent_max, child_max)` from `RuntimePoolConfig` so tests do not depend on process spawning.

  Do not lower `_pool_max_size()`'s global minimum if unrelated processes depend on it; instead pass a supported explicit override into V2 child initialization or make the minimum configurable only for this process before pool creation. Add a test proving the effective child maximum is 2.

- [ ] **Step 4: Run focused tests**

  ```bash
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_serve_worker.py tests/test_v2_p0_pool_safety.py -q
  ```

  Expected: PASS and estimated maximum connections for the default layout is `8 + (8 × 2) = 24`, excluding migrations/short-lived administrative connections already documented by the service.

- [ ] **Step 5: Commit**

  ```bash
  git add backend/model_api_runtime/v2/serve_worker.py backend/model_api_runtime/v2/turn_child.py backend/db.py tests/test_v2_serve_worker.py tests/test_v2_p0_pool_safety.py
  git commit -m "fix(v2): bound database pools per runtime process"
  ```

---

## Task 12: Batch Profile Card Reads and Emit Real Progress

**Files:**

- Modify: `backend/model_api_runtime/v2/pool_config.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `tests/test_v2_pool_config.py`
- Modify: `tests/test_v2_profile_cards.py`
- Modify: `tests/test_v2_profile_lane.py`
- Modify: `tests/test_v2_profile_refresh.py`
- Modify: `tests/test_v2_watchdog.py`

- [ ] **Step 1: Write failing batching, concurrency, and progress tests**

  Seed/fake 554 cards and assert reads are split into nine deterministic batches at the default size of 64 IDs, preserve stable ordering, and produce the same assembled result as the old full fetch. Assert progress stages advance after index read, after every batch, before/after model work, and before durable write. Assert only one slot spec contains `profile`, so instance concurrency is 1.

  Add a watchdog test showing periodic Profile progress avoids a false stall kill while the absolute runtime deadline still terminates a genuinely overlong Job.

  Lock existing Profile frequency semantics in `test_v2_profile_refresh.py`: generate on first V2 activation; generate after successful Chat when missing; refresh an `ok` Profile only after at least 7 days and a Garden count/max-updated change; force refresh after Dream; retry failures with exponential backoff starting at 5 minutes and capped at 6 hours. Assert an unchanged healthy user does not enqueue Profile after every Chat.

- [ ] **Step 2: Run focused tests and confirm failure**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_profile_cards.py tests/test_v2_profile_lane.py tests/test_v2_profile_refresh.py tests/test_v2_watchdog.py -q
  ```

  Expected: FAIL because `_read_profile_cards` performs one all-ID fetch and Profile has coarse progress.

- [ ] **Step 3: Add deterministic batched card loading**

  ```python
  PROFILE_CARD_BATCH_SIZE = 64


  def _read_profile_cards(
      user_id: str,
      progress: Callable[[str], None] | None = None,
  ) -> tuple[str, int]:
      index_items = read_profile_index(user_id)
      card_ids = [str(item["id"]) for item in index_items]
      cards_by_id: dict[str, dict] = {}
      for offset in range(0, len(card_ids), PROFILE_CARD_BATCH_SIZE):
          batch_ids = card_ids[offset : offset + PROFILE_CARD_BATCH_SIZE]
          for item in read_profile_card_batch(user_id, batch_ids):
              cards_by_id[str(item["id"])] = item
          if progress:
              progress(f"profile.cards.{min(offset + len(batch_ids), len(card_ids))}.{len(card_ids)}")
      rendered = [_render_profile_card(cards_by_id[card_id]) for card_id in card_ids]
      return "\n".join(rendered), len(card_ids)
  ```

  Extract `read_profile_index` and `read_profile_card_batch` from the current synchronous `memory_core.index`/`memory_core.fetch` logic and keep `_read_profile_cards` synchronous. Do not log card content or user data.

- [ ] **Step 4: Add progress around uninstrumented Profile model work**

  Emit `profile_index_started/completed`, `profile_fetch_batch_started/completed`, `profile_provider_request/response`, and `profile_write_started/completed`. Include only card count, batch index/count, character count, elapsed time, provider call ordinal, and fixed error class. If a provider call has no streaming callback, progress must not reset during the call; the 90-second provider timeout and 1200-second Profile absolute budget still protect it. Keep the Enclave transport timeout at 20 seconds and maximum Profile provider calls at 8.

  After the 554-card equivalence and stall tests pass, change Heavy's default stall budget from 240 to 120 seconds while retaining its 1200-second absolute budget.

- [ ] **Step 5: Run focused tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_profile_cards.py tests/test_v2_profile_lane.py tests/test_v2_profile_refresh.py tests/test_v2_watchdog.py -q
  ```

  Expected: PASS.

- [ ] **Step 6: Commit**

  ```bash
  git add backend/model_api_runtime/v2/pool_config.py backend/model_api_runtime/v2/serve_worker.py backend/model_api_runtime/v2/worker.py tests/test_v2_pool_config.py tests/test_v2_profile_cards.py tests/test_v2_profile_lane.py tests/test_v2_profile_refresh.py tests/test_v2_watchdog.py
  git commit -m "fix(v2): batch profile reads and report progress"
  ```

---

## Task 13: Extend the Existing Admin V2 Metrics Snapshot

**Files:**

- Modify: `backend/admin/admin_core.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `tests/test_v2_metrics_endpoint.py`

- [ ] **Step 1: Write failing endpoint tests for the JSON fields**

  The repository's existing metrics surface is the admin-token-gated JSON endpoint `GET /v1/admin/v2-metrics`, composed by `backend/admin/admin_core.py`; do not introduce a separate Prometheus registry in this change. Extend the exact response assertion with:

  ```python
  "pools": {
      "foreground": {"configured": 4, "healthy": 4, "busy": 2, "restarting": 0, "pending": 1, "oldest_pending_sec": 3.5, "claim_p95_ms": 80.0},
      "wake": {"configured": 2, "healthy": 2, "busy": 1, "restarting": 0, "pending": 0, "oldest_pending_sec": None, "claim_p95_ms": 120.0},
      "heavy": {"configured": 2, "healthy": 2, "busy": 1, "restarting": 0, "pending": 2, "oldest_pending_sec": 40.0, "claim_p95_ms": 900.0},
  },
  "jobs_by_lane": {
      "chat": {"pending": 1, "active": 2},
      "profile": {"pending": 0, "active": 1},
  },
  "preemptions_24h": {"profile:terminal": 1, "scheduled:requeued": 2},
  "watchdog_recoveries_24h": {"chat:terminal": 1, "profile:requeued": 1},
  "enclave": {
      "limit": 4,
      "granted": {"foreground": 2, "wake": 1, "heavy": 1},
      "waiting": {"foreground": 0, "wake": 0, "heavy": 2},
      "wait_p95_ms": {"foreground": 5.0, "wake": 12.0, "heavy": 300.0},
  },
  "db_pools": {
      "parent": {"max": 8, "used": 3, "waiting": 0, "timeouts": 0},
      "slot": {"processes": 8, "max_each": 2, "used": 7, "waiting": 0, "timeouts": 0},
  },
  "isolation_events": {
      "watchdog_kills": {"heavy:profile:stall": 1},
      "stale_owner_rejections": 2,
      "preemption_exit_p95_ms": 140.0,
      "watchdog_release_p95_ms": 250.0,
      "admission_rejects": {"no_foreground_capacity": 0, "over_sla": 1, "control_halted": 0},
  },
  "profile_runtime": {"card_count_max": 554, "batch_count": 9, "provider_calls_max": 3, "stage_p95_ms": {"fetch_batch": 410.0, "provider": 42000.0}},
  ```

  Monkeypatch the new store helpers just as the existing test patches queue and service-time helpers. Assert the heartbeat `runtime_state` contains only bounded operational values: slot counts, stages, and Enclave counts. It must not contain `user_id`, `job_id`, `claimed_by`, generation, provider URL, message text, memory content, or exceptions.

- [ ] **Step 2: Run the metric test and confirm failure**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_metrics_endpoint.py -q
  ```

  Expected: FAIL because the new JSON blocks are absent.

- [ ] **Step 3: Add bounded store aggregations**

  Add `job_counts_by_lane()`, `recent_preemption_counts(within_hours=24)`, and `recent_watchdog_recovery_counts(within_hours=24)` in `jobs_store.py`. Aggregate from authoritative Job status/reason fields; return only lane plus fixed recovery/result categories. Use indexed time/status predicates and add `EXPLAIN` evidence to the commit message if a new query cannot use an existing index.

- [ ] **Step 4: Publish the parent's bounded live snapshot in heartbeat `runtime_state`**

  For each pool heartbeat, publish configured/healthy/busy/restarting slot counts and the parent process's fixed-key rolling aggregates. Publish the Enclave broker, DB pool, preemption/watchdog latency, stale-fence, admission-reject, and Profile stage snapshots once on the `foreground` heartbeat. Use bounded histograms or fixed-size rolling samples in memory; never append unbounded event arrays. The JSON object must stay below 4 KiB and contain no per-Job or per-user identifiers. Clear stale state by replacing the complete JSON value on every heartbeat UPSERT.

- [ ] **Step 5: Compose the fields in `admin_core.v2_metrics`**

  Build `pools` from fresh per-pool heartbeat rows, build durable recent counters from the new store helpers, and select the freshest foreground broker snapshot for `enclave`. If no three-pool worker is fresh, return empty/zero blocks rather than stale data. Preserve every existing response field.

- [ ] **Step 6: Run the metric test**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_metrics_endpoint.py -q
  ```

  Expected: PASS.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/admin/admin_core.py backend/model_api_runtime/v2/serve_worker.py backend/model_api_runtime/v2/jobs_store.py tests/test_v2_metrics_endpoint.py
  git commit -m "feat(v2): expose pool isolation runtime metrics"
  ```

---

## Task 14: Configure the Test Environment with Inline Non-Secret Values

**Files:**

- Modify: `deploy/docker-compose.phala.test.yaml`
- Modify: `tests/test_deploy_yaml_strict.py`

- [ ] **Step 1: Write failing strict-YAML assertions**

  Parse the test Phala Compose file and assert the Runtime V2 `serve-worker` service contains these literal string values:

  ```yaml
  FEEDLING_V2_FOREGROUND_SLOTS: "4"
  FEEDLING_V2_WAKE_SLOTS: "2"
  FEEDLING_V2_HEAVY_SLOTS: "2"
  FEEDLING_V2_PROFILE_INSTANCE_CONCURRENCY: "1"
  FEEDLING_V2_ENCLAVE_INSTANCE_CONCURRENCY: "4"
  ```

  Assert these values do not use `${...}` substitution. Assert the service contains none of `FEEDLING_V2_POOL_MODE`, `FEEDLING_V2_MAX_WORKERS`, `FEEDLING_V2_CHAT_PREEMPTION_ENABLED`, or `FEEDLING_V2_SLOT_PROCESS_ISOLATION`. Keep assertions limited to the V2 service; the separate V1 resident runner is out of scope. Do not modify pre or prod Compose files in this plan.

- [ ] **Step 2: Run the strict test and confirm failure**

  ```bash
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_deploy_yaml_strict.py -q
  ```

  Expected: FAIL because the test service still carries the former max-worker setting with value 4 and lacks the five pool-capacity settings.

- [ ] **Step 3: Write the non-secret settings directly into YAML**

  Apply the exact five-variable block above to test and remove its old V2 max-worker setting. Do not add GitHub Secrets or GitHub Variables. Do not alter `DATABASE_URL`, runtime token secret, provider keys, or other credentials.

  Leave `deploy/docker-compose.agent-runner.yaml`, pre, and prod unchanged; they are outside this test-environment validation plan.

- [ ] **Step 4: Run strict tests and render Compose configs**

  ```bash
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_deploy_yaml_strict.py -q
  docker compose -f deploy/docker-compose.phala.test.yaml config --quiet
  ```

  Expected: both commands exit 0 and no variable-substitution warning mentions the five new settings.

- [ ] **Step 5: Commit**

  ```bash
  git add deploy/docker-compose.phala.test.yaml tests/test_deploy_yaml_strict.py
  git commit -m "ops(v2): configure test runtime with three pools"
  ```

---

## Task 15: Document Architecture, Chat Preemption, Resource Limits, and Deployment Recovery

**Files:**

- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/workflows/chat.mdx`
- Modify: `docs-site/content/docs/workflows/memory.mdx`
- Modify: `docs-site/content/docs/self-hosting.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: `docs/superpowers/specs/2026-08-14-runtime-v2-three-pool-slot-isolation-design.md`

- [ ] **Step 1: Bring the design record forward to the approved no-legacy decision**

  Remove `FEEDLING_V2_POOL_MODE`, the independent partial-rollout switches, shared-child configuration rollback, and pre/prod rollout instructions from the design record. State that the full implementation lands in one PR to `test`, and that recovery uses the previous known-good image/commit while leaving additive migration `0085` installed.

- [ ] **Step 2: Update the architecture page and diagram**

  Show one parent, three logical pools, eight single-slot children, PostgreSQL Job authority, the cancellation bus, and the parent Enclave broker. State that OS-process isolation is per slot, while all pools remain inside one CVM/service instance.

- [ ] **Step 3: Update workflow and trust-boundary text**

  In Chat docs, explain that a newly accepted Chat can preempt same-user non-Chat work atomically. In memory/Profile docs, document batching and instance concurrency 1. In self-hosting, document 4/2/2 slots, Enclave total 4, parent/child DB maxima, resource monitoring, and that Runtime V2 no longer supports the shared multi-slot topology. Document recovery as redeploying the previously known-good image/commit; do not document a mode switch.

- [ ] **Step 4: Add an `Unreleased` changelog entry**

  Describe the user-visible outcome: background maintenance no longer consumes all Chat execution capacity, and a hung Job is isolated to one worker slot. Avoid claiming absolute latency guarantees.

- [ ] **Step 5: Run documentation checks**

  ```bash
  npm run types:check
  npm run lint
  npm run build
  ```

  Run these commands from `docs-site/`.

  Expected: all commands exit 0.

- [ ] **Step 6: Run OpenAPI contract tests because deployment behavior changed but public API shape did not**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/openapi/test_public_openapi.py -q
  ```

  Do not regenerate `docs-site/openapi/public.json` unless this contract test or the implementation diff shows the public API schema changed.

- [ ] **Step 7: Commit**

  ```bash
  git add docs/superpowers/specs/2026-08-14-runtime-v2-three-pool-slot-isolation-design.md docs-site/content/docs/architecture.mdx docs-site/content/docs/workflows/chat.mdx docs-site/content/docs/workflows/memory.mdx docs-site/content/docs/self-hosting.mdx docs-site/content/docs/changelog.mdx
  git commit -m "docs(v2): explain three-pool runtime isolation"
  ```

---

## Task 16: Run Fault-Injection and Full Regression Verification

**Files:**

- Create: `tests/test_v2_pool_fault_injection.py`
- Modify: `tests/test_v2_p0_pool_safety.py`
- Modify: `tests/test_v2_watchdog.py`
- Modify: `tests/test_v2_send_enqueue_atomic.py`

- [ ] **Step 1: Write end-to-end process fault tests**

  Use real spawned child processes with fake Job handlers/Enclave calls and bounded synchronization primitives. Do not use arbitrary sleeps. Cover:

  1. A hung Profile occupies Heavy slot 0; four foreground slots can still claim Chat.
  2. A same-user Chat terminalizes/requeues the Profile claim and targets only Heavy slot 0 for cancellation.
  3. Watchdog kills Heavy slot 0; the other seven PIDs/generations remain stable.
  4. Exact claim recovery happens before the replacement Heavy slot claims new work.
  5. A killed child holding an Enclave permit releases it through generation cleanup.
  6. Four simultaneous Enclave operations are granted and a fifth waits.
  7. A dropped NOTIFY still leaves durable DB preemption correct.
  8. Repeated cancellation/watchdog events remain idempotent and do not duplicate terminal Chat replies.

- [ ] **Step 2: Run the new tests and fix only defects in the implemented design**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_pool_fault_injection.py tests/test_v2_p0_pool_safety.py tests/test_v2_watchdog.py tests/test_v2_send_enqueue_atomic.py -q
  ```

  Expected: PASS with no skipped PostgreSQL tests.

- [ ] **Step 3: Run the complete Runtime V2 and deployment suite**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_jobs_store.py tests/test_v2_send_enqueue_atomic.py tests/test_chat_send_v2_enqueue.py tests/test_v2_child_supervisor.py tests/test_v2_watchdog.py tests/test_v2_p0_pool_safety.py tests/test_v2_capacity_health.py tests/test_v2_claim_reservation.py tests/test_v2_profile_cards.py tests/test_v2_profile_lane.py tests/test_v2_profile_refresh.py tests/test_v2_serve_worker.py tests/test_v2_worker_heartbeat.py tests/test_v2_metrics_endpoint.py tests/test_deploy_yaml_strict.py tests/test_v2_pool_config.py tests/test_wake_bus.py tests/test_v2_job_lease_fencing.py tests/test_catalog_consumer_parity.py tests/test_v2_pool_supervisor.py tests/test_v2_enclave_broker.py tests/test_v2_pool_fault_injection.py -q
  ```

  Expected: PASS, zero skips caused by unavailable PostgreSQL.

- [ ] **Step 4: Run the full backend suite**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests -q
  ```

  Expected: PASS. Investigate every new failure; do not waive failures as unrelated without reproducing them on the base branch.

- [ ] **Step 5: Commit fault tests and any narrowly scoped corrections**

  ```bash
  git add tests/test_v2_pool_fault_injection.py tests/test_v2_p0_pool_safety.py tests/test_v2_watchdog.py tests/test_v2_send_enqueue_atomic.py
  git commit -m "test(v2): cover pool isolation failure scenarios"
  ```

---

## Task 17: Deploy and Validate the Complete Runtime in Test

**Files:**

- Create: `docs/ops/2026-08-14-v2-three-pool-test-validation.md`

- [ ] **Step 1: Prepare the rollout evidence template before deployment**

  Include timestamp, commit SHA, CVM profile, effective environment, migration revision, slot PIDs/generations, pool heartbeat capacity, DB connections, Enclave grant/wait counts, queue depth/service time by lane, watchdog/preemption counts, Chat probe IDs, and previous-image recovery outcome. Do not include credentials, message bodies, memory content, or provider payloads.

  Before deploying the new image, record the current test environment's 4-slot baseline for Chat claim P95, Enclave P95, provider 429/timeout rate, DB wait/timeout rate, CPU, and memory. Use those measurements to write explicit allowed-regression numbers for Enclave/provider/DB into this evidence document before evaluating the new runtime; do not invent percentages without a baseline.

- [ ] **Step 2: Merge the implementation PR into `test` and deploy test**

  Follow the repository's normal test deployment workflow. Confirm migration `0085` applies, the effective Runtime V2 environment is exactly 4/2/2 and Enclave 4, and eight distinct slot child PIDs are visible. Assert logs and process arguments contain no `POOL_MODE`, `MAX_WORKERS`, legacy supervisor startup, or disabled-isolation path: the first deployment must already be the complete topology.

- [ ] **Step 3: Execute test-environment acceptance scenarios**

  - Start a controlled long Heavy Job, then send Chat for the same user; Chat must be claimed by foreground capacity without waiting for the Heavy lease timeout.
  - Trigger a controlled slot watchdog; exactly one child PID changes and exactly one claim is recovered.
  - Run at least five concurrent Enclave callers; observed active grants never exceed four.
  - Verify Profile concurrency never exceeds one.
  - Observe DB connections below the documented process-pool ceiling with headroom for Postgres limits.

  Record exact timestamps and metric/log evidence in the rollout document.

- [ ] **Step 4: Soak test for at least one peak-like service window**

  Acceptance thresholds:

  - Chat claim latency P95 is at most 2 seconds;
  - watchdog kill to claim release P95 is at most 5 seconds;
  - foreground healthy capacity remains at 4 except during bounded single-slot restarts;
  - no Chat expires solely because Heavy/Wake queues are non-empty;
  - no instance-wide restart from one slot watchdog;
  - no duplicate terminal Chat failure replies;
  - Enclave active permits never exceed 4;
  - steady-state memory remains below 70% of 16G;
  - steady-state CPU remains below 70% of 8 cores, excluding short peaks;
  - DB connection usage stays within the measured safe envelope;
  - Enclave P95 and provider/DB error or wait rates remain inside the baseline-derived limits recorded in Step 1;
  - watchdog/preemption rates are explainable by injected tests or known failures.

- [ ] **Step 5: Exercise image/commit recovery in test**

  Record the previous known-good test image digest before deployment. Redeploy that image once and confirm the service returns healthy using its own default 4-worker behavior even though the new test YAML no longer defines `FEEDLING_V2_MAX_WORKERS`. Migration `0085` remains installed because its additive `pool='unassigned'` and `runtime_state={}` defaults are backward-compatible. Do not downgrade the database during operational recovery.

- [ ] **Step 6: Redeploy the new image and repeat smoke tests**

  Confirm all eight slot processes return, pool heartbeats report 4/2/2, Chat succeeds, and the controlled Heavy-Job isolation scenario still passes after recovery. This proves recovery does not leave test on the old image.

- [ ] **Step 7: Record the test decision**

  Mark the test result `pass` only if pool heartbeat, cancellation targeting, exact recovery, Enclave/DB ceilings, latency thresholds, and image recovery are all evidenced. Otherwise record `fail`, restore the previous known-good test image, and open follow-up defects. Pre/prod promotion is explicitly outside this plan and requires a separate approval after this evidence is reviewed.

- [ ] **Step 8: Commit the completed evidence document**

  ```bash
  git add docs/ops/2026-08-14-v2-three-pool-test-validation.md
  git commit -m "docs(ops): record v2 three-pool test evidence"
  ```

## Completion Criteria

- A queued/active Heavy or Wake Job cannot consume foreground pool capacity.
- A same-user Chat is durably admitted without waiting for a background Job's lease expiry.
- A hung Job can cause at most one slot process to be killed.
- Only the killed slot's still-owned active claim is recovered immediately.
- Runtime V2 has no legacy topology, pool-mode switch, max-worker fan-out, or isolation/preemption feature flags.
- The test deployment runs 4 foreground, 2 wake, and 2 heavy slots; Profile instance concurrency is 1.
- Instance-wide Enclave concurrency never exceeds 4 and recovers permits after child death.
- Default maximum pooled DB connections are approximately 24 across the parent and eight children.
- Test Compose YAML contains the five non-secret capacity settings directly and does not contain the four retired variables.
- Focused, full Runtime V2, full backend, Compose, OpenAPI contract, and public-doc checks pass.
- Test validation and previous-image recovery evidence is recorded; pre/prod promotion remains a separate decision.
