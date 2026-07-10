# Hosted Runtime V2 — `scheduled` lane + `schedule_wake` capability — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Let the agent schedule its own wake-ups, make those wake-ups actually fire (BUG-3), and stop unhandled lanes from silently writing chat bubbles (BUG-2).

**Architecture:** Not a rewrite — a rewiring. `proactive/scheduled_wake_v2.py` already implements timers, timezone parsing, pending caps, atomic claim/fire, and takes `submit_wake` as an **injected callback**. We redirect that callback from the dead legacy `proactive_jobs` stream to `agent_jobs`, add the one thing that genuinely does not exist (a cross-user due query), and expose `schedule_wake`/`cancel_wake` as a thin capability facade.

**Spec:** `docs/superpowers/specs/2026-07-10-hosted-runtime-v2-scheduled-lane-design.md`

## Global Constraints

- **NO-COMMIT.** Never `git commit` / `git add` / `git stash` / `git stash pop` / `git checkout --` / `git reset` / `git clean`. The stash stack is SHARED with other live sessions and worktrees.
- **Worktree:** `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2`, branch `feat/hosted-runtime-v2`. Never the main checkout.
- **Do not modify** `backend/proactive/scheduled_wake_v2.py`, `responder.py`, `provider_client.py`, `compaction.py`, `context.py`, `agent_loop.py`. Zero drift from the existing wake machinery is the entire point.
- **Dependency direction** (AST-guarded by `tests/test_v2_dependency_direction.py`, now derived from the v2 directory): `backend/capabilities/*` and `backend/model_api_runtime/v2/*` must NOT import `hosted` or `agent_runtime`. `capabilities/wake.py` may import `proactive.*` (unrestricted) but must **NOT** import `model_api_runtime.*` — that would invert the layering and create an import cycle.
- **no-filler:** background lanes never write a chat bubble and never emit a user-visible error chip. An unhandled lane is a silent `mark_failed`.
- **Zero pre-activation burn:** `fire_due_timers` runs the existing settings gate; do not bypass it.
- **`user_logs` is append-only.** One timer has many rows (`created` → `claimed` → `fired`). Any query over it MUST take the latest `seq` per `(user_id, item_key)` or it will re-fire timers that already fired.
- **Test baseline:** 2914 passed / **8** pre-existing failures, measured on the CURRENT rebased base (`feat/hosted-runtime-v2` was rebased onto `origin/test` = `b8d26f9`, which merged PR#65 user-mcp-servers). The 8: `test_chat_route_debug_trace` ×3, `test_debug_trace_event_route`, `test_memory_capture_trace`, `test_model_api_path` verify-ping ×2, and `test_model_api_path::test_model_api_setup_reasoning_effort_off_and_default_disable_gateway_reasoning` (**proven upstream** — reproduced on a detached worktree at pure `b8d26f9` with none of our code).
- `tests/test_v2_status_poll.py` has an uncommitted diff from another session (adapting to PR#65's new `user_mcp` context key). Do not touch it, do not revert it, do not commit it.
- **Full-suite command** (a bare `pytest tests/` aborts at collection and tests NOTHING):
  ```bash
  python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
  ```
  Same two flags for any `-k` sweep. Postgres required: `pg_isready -h 127.0.0.1 -p 55432`.

---

## Task 1: BUG-2 — an unhandled lane must never take the chat path

`jobs_store.LANES` includes `capture`, but `process_job` dispatches only `maintenance` (`worker.py:452`) and `_WAKE_LANES` (`:457`); everything else falls through to the chat turn. The chat path's guard is `if not coalesced and lane == "chat"` (`:469`), and `wants_reply = lane == "chat" or stop_reason == WANTS_REPLY` (`:544`) — so a `capture` job whose planner asks to reply **writes a chat bubble**, and a failure emits a user-visible error chip. Latent only because nothing enqueues `capture` today.

This task is the **safety fix**, not the capture feature. Capture/dream/screen_watch share a shape (background prompt → memory writes → no bubble) and get built together in a later round.

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`
- Test: `tests/test_v2_worker.py`

**Interfaces:**
- Produces: `worker._CHAT_LANE = "chat"`. `process_job` returns `"failed"` for any lane that is not `chat` / `maintenance` / `_WAKE_LANES`, with `last_error` starting `unhandled_lane:`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_v2_worker.py`, following that file's conventions (synchronous, `asyncio.run(...)`, `conftest.seed_user` / `_reset` / `_deps` / `_patch_cheap_boundaries` / `_BYOK`):

```python
def test_capture_lane_never_writes_a_bubble_and_fails_loudly_in_the_db(monkeypatch):
    """BUG-2: `capture` is in LANES but has no handler. Before this fix it fell through to
    the chat path and could write a chat bubble + a user-visible error chip."""
    uid = "u_w_unhandled_lane"
    conftest.seed_user(uid)
    _reset(uid)
    job_id, _ = jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="SHOULD NOT BE WRITTEN")
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(text=text) or {"id": "r"})
    emitted = []
    monkeypatch.setattr(worker, "_emit_status",
                        lambda uid_, jid, status, **kw: emitted.append(status))

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert written == {}                       # no chat bubble
    assert "error" not in emitted              # no user-visible error chip
    row = _job_status(job_id)
    assert row[0] == "failed"
    assert row[1].startswith("unhandled_lane:")


def test_chat_lane_still_takes_the_chat_path(monkeypatch):
    """Guard against the dispatch fix accidentally starving the real chat turn."""
    uid = "u_w_chat_still_works"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="REPLY")
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hi"}])
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(text=text) or {"id": "r"})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))
    assert status == "completed"
    assert written == {"text": "REPLY"}
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_v2_worker.py -q -k "unhandled_lane or chat_still_works"
```

Expected: `test_capture_lane_never_writes_a_bubble...` FAILS (status `"completed"`, a bubble was written). The second test PASSES already — that is intentional; it is the regression guard.

- [ ] **Step 3: Implement the explicit dispatch**

In `backend/model_api_runtime/v2/worker.py`, inside `process_job`'s `try:` block, immediately after the `if lane in _WAKE_LANES:` dispatch, insert:

```python
        if lane != "chat":
            # BUG-2（矩阵 §E）：`capture` 在 LANES 里但没有 handler。以前它会直接掉进下面的
            # chat 回合 —— chat 路径的早退闸是 `lane == "chat"`，而 wants_reply 也是
            # `lane == "chat" or ...`，于是 planner 一旦要求回复，一个后台 job 就会写出用户
            # 可见的聊天气泡，失败还弹 error chip。
            #
            # 显式失败，静默（背景 job 的既有口径：不写气泡、不 _surface_terminal_error）。
            # capture/dream/screen_watch 三条 lane 同形（后台 prompt → 写记忆卡 → 零气泡），
            # 合并成独立一轮实现；在那之前落到这里就是「明确失败」而不是「偷偷写气泡」。
            log.warning("[v2.worker] job %s has unhandled lane=%s", job_id, lane)
            await asyncio.to_thread(jobs_store.mark_failed, job_id, f"unhandled_lane:{lane}")
            return "failed"
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_v2_worker.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: both new tests pass; 7 pre-existing failures, 0 new. If an existing test enqueued a non-chat lane and expected the chat path, that test encoded BUG-2 — report it with old vs new assertion, do not silently weaken it.

- [ ] **Step 5: Do NOT commit.**

---

## Task 2: `jobs_store.due_scheduled_users` — the cross-user due query

`DBScheduledWakeStoreV2.due_candidates` is **per-user** (`scheduled_wake_v2.py:375`). The scheduler cannot afford one query per user per 30 s tick. There is no cross-user query anywhere; this is the one genuinely missing piece.

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Test: `tests/test_v2_scheduled_due.py` (create)

**Interfaces:**
- Produces: `jobs_store.due_scheduled_users(*, now: float | None = None, limit: int = 500) -> list[str]`
- Consumes: the `user_logs` stream `proactive_scheduled_wakes_v2` (append-only; `item_key` = `timer_id`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_v2_scheduled_due.py`:

```python
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
from model_api_runtime.v2 import jobs_store

_STREAM = "proactive_scheduled_wakes_v2"


@pytest.fixture(autouse=True)
def _clean_stream():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM user_logs WHERE stream = %s", (_STREAM,))
    yield


def _append(uid, timer_id, doc, ts=None):
    db.log_append(uid, _STREAM, doc, ts=ts or time.time(), item_key=timer_id)


def test_returns_user_with_a_due_pending_timer():
    conftest.seed_user("u_due_1")
    _append("u_due_1", "t1", {"status": "pending", "due_at": 100.0})
    assert jobs_store.due_scheduled_users(now=200.0) == ["u_due_1"]


def test_ignores_a_timer_that_is_not_due_yet():
    conftest.seed_user("u_due_2")
    _append("u_due_2", "t1", {"status": "pending", "due_at": 999.0})
    assert jobs_store.due_scheduled_users(now=200.0) == []


def test_takes_only_the_latest_row_per_timer():
    """user_logs is append-only: created -> claimed -> fired are THREE rows for one timer.
    Reading anything but the newest row re-fires timers that already fired."""
    conftest.seed_user("u_due_3")
    _append("u_due_3", "t1", {"status": "pending", "due_at": 100.0}, ts=1.0)
    _append("u_due_3", "t1", {"status": "fired", "due_at": 100.0}, ts=2.0)
    assert jobs_store.due_scheduled_users(now=200.0) == []


def test_reclaims_a_claim_whose_lease_expired():
    conftest.seed_user("u_due_4")
    _append("u_due_4", "t1",
            {"status": "claimed", "due_at": 100.0, "claim_expires_at": 150.0})
    assert jobs_store.due_scheduled_users(now=200.0) == ["u_due_4"]


def test_does_not_reclaim_a_live_claim():
    conftest.seed_user("u_due_5")
    _append("u_due_5", "t1",
            {"status": "claimed", "due_at": 100.0, "claim_expires_at": 999.0})
    assert jobs_store.due_scheduled_users(now=200.0) == []


def test_dedupes_a_user_with_two_due_timers_and_respects_limit():
    conftest.seed_user("u_due_6")
    _append("u_due_6", "t1", {"status": "pending", "due_at": 100.0})
    _append("u_due_6", "t2", {"status": "pending", "due_at": 101.0})
    assert jobs_store.due_scheduled_users(now=200.0) == ["u_due_6"]
    assert jobs_store.due_scheduled_users(now=200.0, limit=0) == []
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_v2_scheduled_due.py -q
```

Expected: FAIL — `AttributeError: module ... has no attribute 'due_scheduled_users'`.

- [ ] **Step 3: Implement**

In `backend/model_api_runtime/v2/jobs_store.py`:

```python
SCHEDULED_WAKE_STREAM = "proactive_scheduled_wakes_v2"


def due_scheduled_users(*, now: float | None = None, limit: int = 500) -> list[str]:
    """有到期 self-wake timer 的用户（跨用户）。

    `user_logs` 是 append-only：一个 timer 会有 created→claimed→fired 多行。必须用
    `DISTINCT ON (user_id, item_key) ... ORDER BY seq DESC` 只取每个 timer 的**最新一版**，
    否则早已 fire 的 timer 会被当成 pending 反复唤醒。

    到期 = pending，或 claimed 但 claim 租约已过期（持有者死了）。触发的原子性由
    `ScheduledWakeServiceV2.fire_due_timers` 内部的 claim_due CAS 保证——本函数只是个
    廉价的候选人筛子，允许假阳性（多个 scheduler 抢同一个 timer 是安全的）。
    """
    ts = time.time() if now is None else float(now)
    if limit <= 0:
        return []
    sql = (
        "SELECT DISTINCT user_id FROM ("
        "  SELECT DISTINCT ON (user_id, item_key) user_id, doc"
        "  FROM user_logs WHERE stream = %s"
        "  ORDER BY user_id, item_key, seq DESC"
        ") latest "
        "WHERE COALESCE(NULLIF(doc->>'due_at','')::float8, 0) <= %s "
        "  AND (doc->>'status' = 'pending' OR (doc->>'status' = 'claimed' "
        "       AND COALESCE(NULLIF(doc->>'claim_expires_at','')::float8, 0) <= %s)) "
        "LIMIT %s"
    )
    with db.get_pool().connection() as conn:
        rows = conn.execute(sql, (SCHEDULED_WAKE_STREAM, ts, ts, limit)).fetchall()
    return [str(r[0]) for r in rows]
```

`jobs_store` imports `db` (`:13`) but **does NOT import `time`** — add `import time` to its import block.

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_v2_scheduled_due.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: 6 new pass; 7 pre-existing failures, 0 new.

- [ ] **Step 5: Do NOT commit.**

---

## Task 3: the `scheduled` lane producer

**Files:**
- Modify: `backend/model_api_runtime/v2/scheduler.py`, `backend/model_api_runtime/v2/serve_worker.py`
- Test: `tests/test_v2_scheduler.py` (append), `tests/test_v2_serve_worker.py` (append)

**Interfaces:**
- Consumes: `jobs_store.due_scheduled_users` (Task 2).
- Produces:
  - `scheduler.SchedulerDeps` gains two optional callables: `due_scheduled_users() -> list[str]` and `fire_scheduled(user_id) -> int` (number of timers fired). Both default to `None` → the scheduled sweep is skipped entirely (keeps every existing scheduler test working unchanged).
  - `run_scheduler_tick` return dict gains `"scheduled_fired": int`.
  - `serve_worker._fire_scheduled_for_user(user_id) -> int`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_v2_scheduler.py` (match the file's existing fake-deps style):

```python
class _SchedFakeDeps:
    """The scheduler's deps are duck-typed. Heartbeat side is inert here."""

    def __init__(self, *, due_scheduled=None, fire=None):
        self._due_scheduled = due_scheduled
        self._fire = fire
        if due_scheduled is not None:
            self.due_scheduled_users = lambda: list(due_scheduled)
        if fire is not None:
            self.fire_scheduled = fire

    def due_users(self):
        return []


def test_tick_fires_due_scheduled_users_and_counts_them():
    fired = []
    deps = _SchedFakeDeps(due_scheduled=["u1", "u2"],
                          fire=lambda uid: (fired.append(uid), 1)[1])
    out = scheduler.run_scheduler_tick(deps, now=100.0)
    assert fired == ["u1", "u2"]
    assert out["scheduled_fired"] == 2


def test_tick_isolates_a_failing_scheduled_user():
    """One user's broken timer must not stop the sweep — mirrors heartbeat isolation."""
    def _fire(uid):
        if uid == "bad":
            raise RuntimeError("boom")
        return 1

    deps = _SchedFakeDeps(due_scheduled=["bad", "good"], fire=_fire)
    out = scheduler.run_scheduler_tick(deps, now=100.0)
    assert out["scheduled_fired"] == 1


def test_tick_skips_the_scheduled_sweep_when_deps_are_absent():
    """Existing FakeDeps have neither attribute; the sweep must no-op, not AttributeError."""
    deps = _SchedFakeDeps()          # no due_scheduled_users / fire_scheduled at all
    out = scheduler.run_scheduler_tick(deps, now=100.0)
    assert out["scheduled_fired"] == 0
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_v2_scheduler.py -q -k "scheduled"
```

Expected: FAIL — unexpected kwarg / `KeyError: 'scheduled_fired'`.

- [ ] **Step 3: Implement in `scheduler.py`**

**There is no `SchedulerDeps` dataclass.** `run_scheduler_tick(deps, *, now)` takes a DUCK-TYPED object — a `FakeDeps` class in tests, a `types.SimpleNamespace` in `serve_worker._build_scheduler_deps`. So you MUST probe with `getattr(..., None)`; writing `deps.due_scheduled_users` would raise `AttributeError` on every existing scheduler test, since their `FakeDeps` has no such attribute.

The module's logger is named `logger` (`scheduler.py:14`), not `log`.

`run_scheduler_tick` currently ends with `return {"considered": ..., "enqueued": ..., "skipped": ...}`. Replace that return with:

```python
    # `deps` 是鸭子类型（测试里的 FakeDeps / 生产的 SimpleNamespace），不是 dataclass。
    # 必须 getattr 探测：老的 FakeDeps 根本没有这两个属性，直接取会 AttributeError。
    # 两者缺一即整个 scheduled 扫描跳过 —— 既有 scheduler 测试因此一行不用改。
    due_scheduled = getattr(deps, "due_scheduled_users", None)
    fire_scheduled = getattr(deps, "fire_scheduled", None)
    scheduled_fired = 0
    if due_scheduled is not None and fire_scheduled is not None:
        for user_id in due_scheduled():
            try:
                scheduled_fired += int(fire_scheduled(user_id) or 0)
            except Exception:  # noqa: BLE001 — 单用户失败不能中断整轮扫描
                logger.exception("scheduler: fire_scheduled failed for user %s", user_id)

    return {"considered": considered, "enqueued": enqueued, "skipped": skipped,
            "scheduled_fired": scheduled_fired}
```

- [ ] **Step 4: Implement `_fire_scheduled_for_user` in `serve_worker.py`**

```python
def _fire_scheduled_for_user(user_id: str) -> int:
    """触发该用户所有到期的 self-wake timer，每个转成一个 `scheduled` lane 的 agent_job。

    复用 `ScheduledWakeServiceV2.fire_due_timers` 的注入缝：把 legacy 的
    `submit_wake`（塞进 proactive_jobs 流，V2 下无人排空 = BUG-3）换成 enqueue_job。
    claim/mark_fired 的原子性由 fire_due_timers 内部的 claim_due CAS 保证，多个
    scheduler 实例并发跑也不会重复触发。gate（未激活/免打扰）原样复用，零漂移。
    """
    from proactive.controls_v2 import WakeControlDecisionV2
    from proactive.scheduled_wake_v2 import DBScheduledWakeStoreV2, ScheduledWakeServiceV2
    from proactive.store_v2 import DBProactiveSettingsStoreV2

    settings = DBProactiveSettingsStoreV2().load(user_id)
    service = ScheduledWakeServiceV2(DBScheduledWakeStoreV2(), owner_id=_V2_WAKE_OWNER_ID)
    fired = 0

    def _submit(event):
        nonlocal fired
        jobs_store.enqueue_job(user_id, "scheduled", reason="scheduled_wake")
        core_wake_bus.notify("v2_jobs", user_id)
        fired += 1
        return WakeControlDecisionV2(True, "queued_v2", settings)

    service.fire_due_timers(user_id, settings=settings, submit_wake=_submit,
                            owner_id=_V2_WAKE_OWNER_ID)
    return fired
```

Add a module constant `_V2_WAKE_OWNER_ID = "hosted_runtime_v2"` (distinct from `proactive_core.RESIDENT_RUNTIME_OWNER_ID_V2`, so a V2 worker's claim can never be confused with a resident's). Read `proactive_core.scheduled_fire` first and mirror its construction. Wire both new deps into `_build_scheduler_deps()`.

- [ ] **Step 5: Test the production wiring**

Append to `tests/test_v2_serve_worker.py`:

```python
def test_fire_scheduled_for_user_enqueues_a_scheduled_agent_job(monkeypatch, backend_env):
    """BUG-3: the timer machinery existed but submitted into the dead legacy proactive_jobs
    stream. It must now produce an agent_jobs `scheduled` job."""
    import conftest
    from model_api_runtime.v2 import jobs_store

    serve_worker.wire_assembly()
    uid = "u_sw_fire_scheduled"
    conftest.seed_user(uid)

    calls = []
    monkeypatch.setattr(jobs_store, "enqueue_job",
                        lambda u, lane, **kw: calls.append((u, lane)) or ("job1", False))

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def fire_due_timers(self, user_id, *, settings, submit_wake, owner_id):
            submit_wake(object())
            return ()

    monkeypatch.setattr("proactive.scheduled_wake_v2.ScheduledWakeServiceV2", _FakeService)
    assert serve_worker._fire_scheduled_for_user(uid) == 1
    assert calls == [(uid, "scheduled")]
```

- [ ] **Step 6: Run tests**

```bash
python -m pytest tests/test_v2_scheduler.py tests/test_v2_serve_worker.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: new tests pass; 7 pre-existing failures, 0 new.

- [ ] **Step 7: Do NOT commit.**

---

## Task 4: `schedule_wake` / `cancel_wake` capability

**Files:**
- Create: `backend/capabilities/wake.py`
- Modify: `backend/capabilities/registry.py`, `backend/model_api_runtime/v2/planner.py`, `backend/model_api_runtime/v2/executor.py`
- Test: `tests/test_capability_wake.py` (create)

**Interfaces:**
- Produces: `wake.schedule(store, *, api_key, runtime_token, params) -> CapabilityResult`, `wake.cancel(...)`; registry keys `schedule_wake` / `cancel_wake`, both in `registry.WRITE_ACTIONS`.
- `planner._WRITE_ACTIONS` gains both; `_PLANNER_SYSTEM` vocabulary gains both; `executor._CONTROL_ACTIONS` **loses** both.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_capability_wake.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from capabilities import registry as cap_registry
from capabilities import wake


class _Store:
    user_id = "u_cap_wake"


def test_schedule_requires_at():
    res = wake.schedule(_Store(), params={}).to_dict()
    assert res["ok"] is False
    assert "at" in res["error"]["message"]


def test_cancel_requires_wake_id():
    res = wake.cancel(_Store(), params={}).to_dict()
    assert res["ok"] is False


def test_schedule_forwards_the_action_shape_apply_turn_actions_expects(monkeypatch):
    seen = {}

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def apply_turn_actions(self, user_id, actions, **kw):
            seen["user_id"] = user_id
            seen["actions"] = list(actions)
            seen["submit_wake"] = kw.get("submit_wake")
            return ()

    monkeypatch.setattr("proactive.scheduled_wake_v2.ScheduledWakeServiceV2", _FakeService)
    res = wake.schedule(_Store(), params={"at": "2026-07-11T18:00", "tz": "Asia/Shanghai",
                                          "reason": "check in"}).to_dict()
    assert res["ok"] is True
    assert seen["user_id"] == "u_cap_wake"
    assert seen["actions"] == [{"type": "schedule_wake", "at": "2026-07-11T18:00",
                                "tz": "Asia/Shanghai", "reason": "check in"}]


def test_submit_wake_does_not_enqueue(monkeypatch):
    """Layering: capabilities/* must not import model_api_runtime.v2.jobs_store (that would
    invert the dependency direction and cycle). The timer is persisted; the scheduler picks
    it up within one tick. So submit_wake must accept WITHOUT enqueueing."""
    captured = {}

    class _FakeService:
        def __init__(self, *a, **kw):
            pass

        def apply_turn_actions(self, user_id, actions, **kw):
            captured["decision"] = kw["submit_wake"](object())
            return ()

    monkeypatch.setattr("proactive.scheduled_wake_v2.ScheduledWakeServiceV2", _FakeService)
    wake.schedule(_Store(), params={"at": "2026-07-11T18:00"})
    assert captured["decision"].accepted is True


def test_registered_as_write_capabilities():
    assert "schedule_wake" in cap_registry.CAPABILITIES
    assert "cancel_wake" in cap_registry.CAPABILITIES
    assert {"schedule_wake", "cancel_wake"} <= cap_registry.WRITE_ACTIONS


def test_planner_can_emit_them_and_executor_no_longer_skips_them():
    from model_api_runtime.v2 import executor, planner
    assert {"schedule_wake", "cancel_wake"} <= planner._WRITE_ACTIONS
    assert "schedule_wake" in planner._PLANNER_SYSTEM
    assert "schedule_wake" not in executor._CONTROL_ACTIONS
    assert "cancel_wake" not in executor._CONTROL_ACTIONS
    steps = planner.validate_plan({"plan": [
        {"type": "schedule_wake", "payload": {"at": "2026-07-11T18:00"}},
        {"type": "final_response", "payload": {}}]})
    assert steps[0]["type"] == "schedule_wake"


def test_wake_capability_does_not_import_model_api_runtime():
    import pathlib
    src = pathlib.Path(wake.__file__).read_text()
    for forbidden in ("model_api_runtime", "import hosted", "from hosted", "agent_runtime"):
        assert forbidden not in src, f"capabilities/wake.py must not reference {forbidden}"
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_capability_wake.py -q
```

Expected: FAIL — `ImportError: cannot import name 'wake' from 'capabilities'`.

- [ ] **Step 3: Implement `backend/capabilities/wake.py`**

```python
"""Self-wake capabilities — facade over proactive/scheduled_wake_v2.ScheduledWakeServiceV2.

分层：本模块只 import `proactive.*`。**绝不** import `model_api_runtime.*` —— v2 是
capabilities 的上层，反向 import 会成环（AST 守卫也盯着这条方向）。

于是 `submit_wake` 在这里**不入队**：timer 已经持久化，下一次 scheduler tick（≤30s）会经
`fire_due_timers` 正常捞起并产出 `scheduled` lane 的 agent_job。让 scheduler 做唯一的入队者，
也顺带保证「只有一个地方产生 scheduled job」。代价是最坏 30s 延迟——对「稍后叫我」无所谓。
"""
from __future__ import annotations

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err

_OWNER_ID = "hosted_runtime_v2"


class _Accepted:
    """submit_wake 的返回契约：只需要一个 `.accepted` 为真的对象。"""
    accepted = True
    reason = "persisted_for_scheduler"


def _apply(user_id: str, action: dict) -> CapabilityResult:
    from proactive import scheduled_wake_v2
    from proactive.store_v2 import DBProactiveSettingsStoreV2

    settings = DBProactiveSettingsStoreV2().load(user_id)
    service = scheduled_wake_v2.ScheduledWakeServiceV2(
        scheduled_wake_v2.DBScheduledWakeStoreV2(), owner_id=_OWNER_ID)
    try:
        results = service.apply_turn_actions(
            user_id, [action], settings=settings, submit_wake=lambda _event: _Accepted())
    except Exception as e:  # noqa: BLE001
        return err(errors.UPSTREAM, f"scheduled wake failed: {type(e).__name__}", retryable=True)
    return ok(data={"results": [r.as_dict() for r in results] if results else []})


def schedule(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    at = str(params.get("at") or "").strip()
    if not at:
        return err(errors.INVALID, "schedule_wake needs an `at` time", retryable=False)
    action = {"type": "schedule_wake", "at": at}
    for key in ("tz", "reason"):
        value = str(params.get(key) or "").strip()
        if value:
            action[key] = value
    return _apply(store.user_id, action)


def cancel(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    wake_id = str(params.get("wake_id") or params.get("id") or "").strip()
    if not wake_id:
        return err(errors.INVALID, "cancel_wake needs a `wake_id`", retryable=False)
    action = {"type": "cancel_wake", "wake_id": wake_id}
    reason = str(params.get("reason") or "").strip()
    if reason:
        action["reason"] = reason
    return _apply(store.user_id, action)
```

Check `ScheduledWakeActionResultV2` really has `.as_dict()` (`scheduled_wake_v2.py:145/171`) before relying on it.

- [ ] **Step 4: Register + vocabulary + un-skip**

`backend/capabilities/registry.py`: add `wake` to the import line, then

```python
    "schedule_wake": lambda store, **kw: wake.schedule(store, **kw),
    "cancel_wake": lambda store, **kw: wake.cancel(store, **kw),
```
and
```python
WRITE_ACTIONS = frozenset({"memory_write", "identity_patch", "schedule_wake", "cancel_wake"})
```

`backend/model_api_runtime/v2/planner.py`: add both to `_WRITE_ACTIONS`, and to `_PLANNER_SYSTEM`'s vocabulary sentence — insert `schedule_wake, cancel_wake, ` right before `final_response.`, and append one sentence before `"Never wrap the JSON in Markdown."`:

```python
    "schedule_wake takes payload.at (ISO time or a relative spec like '2h') and optional "
    "tz/reason; cancel_wake takes payload.wake_id. Use them only when the user actually "
    "asks to be reminded or checked on later. "
```

`backend/model_api_runtime/v2/executor.py`: remove `"schedule_wake", "cancel_wake"` from `_CONTROL_ACTIONS` (they are real capabilities now) and update that comment block so it no longer names them.

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_capability_wake.py tests/test_v2_planner.py tests/test_v2_executor.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: new tests pass; 7 pre-existing failures, 0 new. An existing executor test may assert that `schedule_wake` is skipped — that test encoded BUG-3's second half. Report old vs new prominently.

- [ ] **Step 6: Update the parity matrix**

In `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`: mark the `schedule_wake`/`cancel_wake` §A row aligned, mark §B's `scheduled` row aligned, and mark BUG-2 (safety fix only — capture still has no handler) and BUG-3 resolved in §E.

- [ ] **Step 7: Do NOT commit.**

---

## Traceability

| Test | Row |
|---|---|
| `test_capture_lane_never_writes_a_bubble_and_fails_loudly_in_the_db` | §E BUG-2 |
| `test_takes_only_the_latest_row_per_timer` | spec §2.2 (append-only `user_logs`) |
| `test_fire_scheduled_for_user_enqueues_a_scheduled_agent_job` | §B `scheduled` / §E BUG-3 |
| `test_submit_wake_does_not_enqueue` | spec §2.4 (layering) |
| `test_planner_can_emit_them_and_executor_no_longer_skips_them` | §A `schedule_wake` |

## Out of scope

`capture` / `dream` / `screen_watch` lanes (same shape — one later round); a `user_logs` due index; the resident tokens/turn baseline.
