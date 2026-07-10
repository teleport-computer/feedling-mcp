# Hosted Runtime V2 — `screen_watch` lane — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** When the user is sharing their screen, V2 speaks up on its own — but only when the screen content genuinely changed and the user is not mid-conversation.

**Architecture:** `screen_watch` is a **wake producer**, not a memory extractor: it joins `_WAKE_LANES` and reuses `_run_wake`. The resident's 120 s per-user polling loop moves server-side into the scheduler, which already has two identical sweeps (`scheduled`, `extraction`) to copy. The gate itself becomes a pure function. Its one piece of cross-tick state (`last_screen_watch_frame_id`, a process variable in the resident) gets persisted in `v2_wake_schedule`.

**Spec:** `docs/superpowers/specs/2026-07-10-hosted-runtime-v2-screen-watch-design.md`

## Global Constraints

- **NO-COMMIT.** Never `git commit` / `git add` / `git stash` / `git stash pop` / `git checkout --` / `git reset` / `git clean`. The stash stack is SHARED with other live sessions and worktrees; a concurrent session is active in this repo.
- **Worktree:** `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2`, branch `feat/hosted-runtime-v2`. Never the main checkout.
- **Do not modify** `tools/chat_resident_consumer.py`, `proactive/*`, `capabilities/*`, `responder.py`, `provider_client.py`, `extraction.py`, `agent_loop.py`, `planner.py`, `executor.py`, `compaction.py`.
- **`screen_watch.py` is PURE**: stdlib only. No `provider_client`, no DB, no `hosted`, no `agent_runtime`. An AST guard (derived from the v2 directory) enforces the last two; a source-grep test in this plan enforces the rest.
- **Gating must not touch the enclave.** Both inputs are available in plaintext: latest frame id/ts via `db.frame_list_meta(user_id)`, and last-user-message ts via `store.chat_messages` (`role`/`ts` are plaintext columns; only `body_ct` is encrypted). The gate runs every 120 s for every `db_action_v2` user — an enclave round-trip there would hammer the single-threaded bottleneck this subproject exists to protect.
- **no-filler:** only model-authored text writes a bubble. A suppressed tick, or a model that chooses silence, writes nothing and emits no error chip. `_run_wake` already treats `empty_reply` as SUCCESS ("weak wake sleeps") — inherit it, do not special-case.
- **Zero pre-activation burn:** the producer must consult the existing read-only proactive oracle `serve_worker._wake_decision_for_user(user_id)` BEFORE enqueueing. An un-activated / Ambient-off / do-not-disturb user must never produce a job.
- **BYOK-only.** No platform LLM key fallback, ever.
- **Test baseline:** 2961 passed / 8 pre-existing failures (`test_chat_route_debug_trace` ×3, `test_debug_trace_event_route`, `test_memory_capture_trace`, `test_model_api_path` verify-ping ×2, `test_model_api_path::test_model_api_setup_reasoning_effort_off_and_default_disable_gateway_reasoning` — proven upstream).
- **Full-suite command** (a bare `pytest tests/` aborts at collection and tests NOTHING):
  ```bash
  python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
  ```
  Same two flags for any `-k` sweep. Postgres required: `pg_isready -h 127.0.0.1 -p 55432`.

---

## Task 1: the pure gate

**Files:**
- Create: `backend/model_api_runtime/v2/screen_watch.py`
- Test: `tests/test_v2_screen_watch_gate.py` (create)

**Interfaces:**
- `screen_watch.FRESH_SEC = 90`, `CHAT_SUPPRESS_SEC = 180`, `INTERVAL_SEC = 120`
- `screen_watch.should_watch(*, latest_frame_id: str, latest_ts: float, last_frame_id: str, last_user_msg_ts: float | None, now: float) -> tuple[bool, str]` — `(should, reason)`; `reason` is always non-empty.

Ported verbatim from `tools/chat_resident_consumer.py:7830-7860`. Order matters: **fresh**, then **changed**, then **chatting**.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_v2_screen_watch_gate.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import screen_watch as sw

_NOW = 1_000_000.0


def _call(**over):
    kw = dict(latest_frame_id="f2", latest_ts=_NOW - 10, last_frame_id="f1",
              last_user_msg_ts=None, now=_NOW)
    kw.update(over)
    return sw.should_watch(**kw)


def test_fresh_changed_and_quiet_watches():
    should, reason = _call()
    assert should is True and reason == "ok"


def test_no_frame_at_all_does_not_watch():
    should, reason = _call(latest_frame_id="")
    assert should is False and reason == "no_frames"


def test_stale_frame_does_not_watch():
    """A frame older than FRESH_SEC means sharing is not live right now."""
    should, reason = _call(latest_ts=_NOW - (sw.FRESH_SEC + 1))
    assert should is False and reason == "not_fresh"


def test_frame_exactly_at_the_freshness_boundary_is_fresh():
    should, _ = _call(latest_ts=_NOW - sw.FRESH_SEC)
    assert should is True


def test_unchanged_frame_does_not_watch():
    """Only act on genuinely new content — otherwise every tick re-wakes on the same frame."""
    should, reason = _call(latest_frame_id="f1", last_frame_id="f1")
    assert should is False and reason == "unchanged"


def test_first_ever_tick_treats_any_frame_as_changed():
    """Persisted frame id is NULL on the first tick; matches the resident's empty-string start."""
    should, _ = _call(last_frame_id="")
    assert should is True


def test_active_chat_suppresses_the_watch():
    should, reason = _call(last_user_msg_ts=_NOW - (sw.CHAT_SUPPRESS_SEC - 1))
    assert should is False and reason == "chatting"


def test_chat_exactly_at_the_suppress_boundary_does_not_suppress():
    should, _ = _call(last_user_msg_ts=_NOW - sw.CHAT_SUPPRESS_SEC)
    assert should is True


def test_old_chat_does_not_suppress():
    should, _ = _call(last_user_msg_ts=_NOW - (sw.CHAT_SUPPRESS_SEC + 1))
    assert should is True


def test_never_chatted_does_not_suppress():
    should, _ = _call(last_user_msg_ts=None)
    assert should is True


def test_freshness_is_checked_before_chatting():
    """A stale frame reports `not_fresh`, not `chatting`, even while the user is typing.
    The reason string is an observability contract — keep the resident's precedence."""
    should, reason = _call(latest_ts=_NOW - 10_000, last_user_msg_ts=_NOW - 1)
    assert should is False and reason == "not_fresh"


def test_screen_watch_gate_is_pure():
    import pathlib
    src = pathlib.Path(sw.__file__).read_text()
    for forbidden in ("provider_client", "jobs_store", "import hosted", "from hosted",
                      "agent_runtime", "core.store", "psycopg", "import db"):
        assert forbidden not in src, f"screen_watch.py must not reference {forbidden}"
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_v2_screen_watch_gate.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'model_api_runtime.v2.screen_watch'`.

- [ ] **Step 3: Implement**

```python
"""V2 screen-watch gate —— **纯**函数，stdlib only。

逐字移植 resident 的 120s 循环判定（tools/chat_resident_consumer.py:7830-7860）：
fresh（共享真的在进行）→ changed（只对新内容动作）→ chatting（用户在打字就让路）。
三条的**顺序**是可观测性契约：reason 告诉运维为什么没醒。

零 I/O：两个输入（最新帧 id/ts、用户上次说话 ts）在服务端都是明文可得，不需要 enclave。
这很重要 —— 这个 gate 每 120s 对每个 db_action_v2 用户跑一次。
"""
from __future__ import annotations

# 一帧比这更新 = 共享此刻确实是活的（iOS 约 30s 一帧）。
FRESH_SEC = 90
# 用户这么久内说过话 = 正在对话，屏幕轮询让路。
CHAT_SUPPRESS_SEC = 180
# 轮询间隔（scheduler 用它推进 next_screen_watch_at）。
INTERVAL_SEC = 120


def should_watch(
    *,
    latest_frame_id: str,
    latest_ts: float,
    last_frame_id: str,
    last_user_msg_ts: float | None,
    now: float,
) -> tuple[bool, str]:
    """(should, reason)。reason 恒非空。"""
    if not latest_frame_id:
        return False, "no_frames"
    if (now - float(latest_ts or 0.0)) > FRESH_SEC:
        return False, "not_fresh"
    if latest_frame_id == (last_frame_id or ""):
        return False, "unchanged"
    if last_user_msg_ts is not None and (now - float(last_user_msg_ts)) < CHAT_SUPPRESS_SEC:
        return False, "chatting"
    return True, "ok"
```

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_v2_screen_watch_gate.py tests/test_v2_dependency_direction.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: 12 new pass; 8 pre-existing failures, 0 new.

- [ ] **Step 5: Do NOT commit.**

---

## Task 2: migration 0019 + `jobs_store` state and due-query

**Files:**
- Create: `backend/alembic/versions/0019_v2_screen_watch.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Test: `tests/test_v2_screen_watch_store.py` (create)

**Interfaces:**
- `LANES` gains `"screen_watch"`; `LANE_PRIORITY["screen_watch"] = 50` (a wake, same as `heartbeat`/`scheduled`).
- `upsert_wake_schedule(user_id, *, next_heartbeat_at=None, next_capture_at=None, payment_cooldown_until=None, next_screen_watch_at=None, last_screen_watch_frame_id=None)` — the two new params keep the existing COALESCE "None = leave unchanged" semantics.
- `get_wake_schedule(user_id)` returns the two new fields.
- `due_screen_watch_users(*, now=None, limit=500) -> list[str]` — mirrors `due_heartbeat_users` exactly, including the `payment_cooldown_until` exclusion.

- [ ] **Step 1: Write the migration**

`backend/alembic/versions/0019_v2_screen_watch.py`, `down_revision = "0018_v2_wake_schedule"`:

```python
"""v2 screen-watch state: persist the resident's in-process last-frame id.

resident 把 `last_screen_watch_frame_id` 放在进程内存里；V2 没有 per-user 常驻进程，
不落库的话每个 scheduler tick 都会把同一帧当成「新内容」，变成 120s 一次的唤醒风暴。
"""
revision = "0019_v2_screen_watch"
down_revision = "0018_v2_wake_schedule"

def upgrade():
    op.execute("ALTER TABLE v2_wake_schedule ADD COLUMN IF NOT EXISTS next_screen_watch_at TIMESTAMPTZ")
    op.execute("ALTER TABLE v2_wake_schedule ADD COLUMN IF NOT EXISTS last_screen_watch_frame_id TEXT")

def downgrade():
    op.execute("ALTER TABLE v2_wake_schedule DROP COLUMN IF EXISTS last_screen_watch_frame_id")
    op.execute("ALTER TABLE v2_wake_schedule DROP COLUMN IF EXISTS next_screen_watch_at")
```

Read `0018_v2_wake_schedule.py` first and match its exact import/style (it may use `op.execute` with a raw `CREATE TABLE`).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_v2_screen_watch_store.py` following `tests/test_v2_wake_schedule.py`'s conventions (real DB, `conftest.seed_user`):

```python
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest
import conftest
import db
from model_api_runtime.v2 import jobs_store


@pytest.fixture(autouse=True)
def _clean():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_wake_schedule")
    yield


def test_screen_watch_is_a_wake_lane():
    assert "screen_watch" in jobs_store.LANES
    assert jobs_store.LANE_PRIORITY["screen_watch"] == jobs_store.LANE_PRIORITY["heartbeat"]


def test_upsert_and_read_back_the_new_columns():
    conftest.seed_user("u_sw_1")
    jobs_store.upsert_wake_schedule("u_sw_1", next_screen_watch_at=500.0,
                                    last_screen_watch_frame_id="f1")
    row = jobs_store.get_wake_schedule("u_sw_1")
    assert row["last_screen_watch_frame_id"] == "f1"
    assert abs(row["next_screen_watch_at"] - 500.0) < 1.0


def test_partial_upsert_leaves_the_other_columns_alone():
    """None = leave unchanged. Advancing the timer must not wipe the frame id."""
    conftest.seed_user("u_sw_2")
    jobs_store.upsert_wake_schedule("u_sw_2", next_screen_watch_at=500.0,
                                    last_screen_watch_frame_id="f1")
    jobs_store.upsert_wake_schedule("u_sw_2", next_screen_watch_at=900.0)
    row = jobs_store.get_wake_schedule("u_sw_2")
    assert row["last_screen_watch_frame_id"] == "f1"
    assert abs(row["next_screen_watch_at"] - 900.0) < 1.0


def test_due_screen_watch_users_returns_only_due_ones():
    for uid, due in (("u_sw_due", 100.0), ("u_sw_later", 9_000.0)):
        conftest.seed_user(uid)
        jobs_store.upsert_wake_schedule(uid, next_screen_watch_at=due)
    assert jobs_store.due_screen_watch_users(now=500.0) == ["u_sw_due"]


def test_due_screen_watch_users_excludes_payment_cooldown():
    """A dead BYOK key must not keep getting hammered by the screen poller."""
    conftest.seed_user("u_sw_cool")
    jobs_store.upsert_wake_schedule("u_sw_cool", next_screen_watch_at=100.0,
                                    payment_cooldown_until=9_000.0)
    assert jobs_store.due_screen_watch_users(now=500.0) == []


def test_null_next_screen_watch_at_is_not_due():
    conftest.seed_user("u_sw_null")
    jobs_store.upsert_wake_schedule("u_sw_null", next_heartbeat_at=1.0)
    assert jobs_store.due_screen_watch_users(now=500.0) == []
```

- [ ] **Step 3: Run to verify failure, then implement**

```bash
python -m pytest tests/test_v2_screen_watch_store.py -q
```

Then: apply the migration to the test DB the same way the suite does (check `tests/conftest.py` — it runs alembic upgrade head), add `"screen_watch"` to `LANES` and `LANE_PRIORITY` (value `50`), extend `get_wake_schedule` / `upsert_wake_schedule`, and write `due_screen_watch_users` by copying `due_heartbeat_users` and swapping the column.

- [ ] **Step 4: Verify the migration actually applies**

```bash
python -m pytest tests/test_v2_jobs_migration.py tests/test_v2_screen_watch_store.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: new tests pass; 8 pre-existing failures, 0 new.

- [ ] **Step 5: Do NOT commit.**

---

## Task 3: the handler — a light wake with screen context

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`
- Test: `tests/test_v2_screen_watch_lane.py` (create), `tests/test_v2_worker.py` (one existing test)

**Interfaces:**
- `worker._WAKE_LANES` gains `"screen_watch"`.
- `worker._SCREEN_WATCH_SYSTEM_PROMPT: str`
- `_run_wake` gains a screen-watch branch: for that lane it fetches `screen_recent` and passes it to `v2_responder.respond(..., action_results=...)`, and uses `_SCREEN_WATCH_SYSTEM_PROMPT`.

**Behaviour:**
- Silence is success. `empty_reply` → silent `mark_completed`, no bubble. Most ticks take this path. `_run_wake` already does this — do not add a second code path.
- **No perception snapshot.** The resident sets `perception_digest = None` for screen-watch jobs (`chat_resident_consumer.py:6611`). Do not fetch `perception_snapshot`.
- `_run_wake` has no `api_key`/`runtime_token` parameters — mint one with `deps.mint_enclave_token(user_id)`, exactly as the rest of the wake path does.
- The `screen_recent` result flows through `responder._fold_action_results`, which caps each action at `_PER_ACTION_CHAR_CAP = 2000` chars. That is the multimodal round's anti-poisoning cap; captions fit. Do not raise it.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_v2_screen_watch_lane.py` using `tests/test_v2_worker.py`'s conventions (sync, `asyncio.run`, `conftest.seed_user`, `_patch_cheap_boundaries`-style stubs — import them or re-declare minimal local equivalents).

```python
def test_screen_watch_is_dispatched_to_the_wake_path():
    from model_api_runtime.v2 import worker
    assert "screen_watch" in worker._WAKE_LANES


def test_screen_watch_turn_passes_screen_context_and_its_own_prompt(monkeypatch):
    """It must ground on recent frames and must NOT fetch a perception snapshot."""
    seen = {}

    async def _fake_respond(**kw):
        seen["system_prompt"] = kw.get("system_prompt")
        seen["action_results"] = kw.get("action_results")
        return "你在看这个报错？"

    caps = []

    async def _fake_cap_data(store, action_type, **kw):
        caps.append(action_type)
        return {"frames": [{"frame_id": "f1", "caption": "a stack trace"}]}

    # ... seed user, enqueue "screen_watch", claim, patch worker._cap_data + v2_responder.respond,
    #     patch worker._write_encrypted_reply, run process_job ...

    assert seen["system_prompt"] is worker._SCREEN_WATCH_SYSTEM_PROMPT
    assert "screen_recent" in (seen["action_results"] or {})
    assert "perception_snapshot" not in caps          # resident sets perception_digest=None
    assert caps == ["screen_recent"]


def test_screen_watch_silence_completes_without_a_bubble(monkeypatch):
    """Most ticks produce nothing. empty_reply is SUCCESS (weak wake sleeps), never a chip."""
    # patch v2_responder.respond to raise ResponderError("empty_reply")
    # assert status == "completed", no _write_encrypted_reply call, no error status emitted
```

Fill these in completely against the real helpers; the assertions above are the contract.

- [ ] **Step 2: Run to verify failure**, then implement:

Add to `worker.py`:

```python
_WAKE_LANES = frozenset({"heartbeat", "scheduled", "manual_wake", "screen_watch"})

_SCREEN_WATCH_SYSTEM_PROMPT = (
    "You are the user's personal companion, quietly watching the screen they are sharing. "
    "Recent frames (with captions) are provided as grounding context. "
    "Speak ONLY if you have something genuinely useful or warm to say about what changed on "
    "screen right now. If nothing is worth saying, reply with an empty message — silence is "
    "the correct answer most of the time. Never narrate that you are watching or that you "
    "looked at frames."
)
```

In `_run_wake`, **AFTER the `async with enclave_sem:` block closes** (not inside it), add:

```python
        screen_results = None
        if lane == "screen_watch":
            # 只取近期帧；**不取** perception_snapshot —— resident 对 screen_watch job
            # 显式设 perception_digest=None（chat_resident_consumer.py:6611）。
            #
            # 注意这行在 `async with enclave_sem` 块**之外**：`_cap_data` 自己会抢
            # enclave_sem（worker.py:219），而 asyncio.Semaphore **不可重入**。嵌在里面
            # 会在 FEEDLING_V2_ENCLAVE_CONCURRENCY=1 时死锁。默认值是 2，所以单测会过、
            # 生产会挂 —— 闸门仍然生效，只是由 _cap_data 自己持有。
            token = deps.mint_enclave_token(user_id)
            data = await _cap_data(store, "screen_recent", api_key=None,
                                   runtime_token=token, enclave_sem=enclave_sem)
            screen_results = {"screen_recent": [{"ok": True, "data": data}]}
```

**This placement is verified, not a suggestion.** `_cap_data` does `async with enclave_sem` internally (`worker.py:219`), and `asyncio.Semaphore` is not reentrant. Nesting it inside another `async with enclave_sem` deadlocks whenever the semaphore value is 1. `FEEDLING_V2_ENCLAVE_CONCURRENCY` defaults to 2, so a naive test passes and production wedges. Do not "tidy" this call into the block above it.

Then pass through to the responder:

```python
            reply = await v2_responder.respond(
                provider_config=provider_config, summary=summary, tail=wake_tail,
                action_results=screen_results,
                system_prompt=(_SCREEN_WATCH_SYSTEM_PROMPT if lane == "screen_watch"
                               else _WAKE_SYSTEM_PROMPT))
```

- [ ] **Step 3: Fix the now-broken unhandled-lane test**

`tests/test_v2_worker.py::test_unhandled_lane_never_writes_a_bubble_and_fails_loudly_in_the_db` uses `screen_watch` as its "genuinely unregistered lane". It is registered now. Change it to `"bogus_lane"`. The test's INTENT (an unhandled lane must not take the chat path, must not write a bubble, must fail loudly in the DB) is unchanged. Report old vs new.

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_v2_screen_watch_lane.py tests/test_v2_worker.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Additionally, prove no deadlock at concurrency 1:

```bash
FEEDLING_V2_ENCLAVE_CONCURRENCY=1 python -m pytest tests/test_v2_screen_watch_lane.py -q
```

Expected: passes, does not hang. If it hangs, your `_cap_data` call is nested inside `async with enclave_sem`.

- [ ] **Step 5: Do NOT commit.**

---

## Task 4: producer + wiring + docs

**Files:**
- Modify: `backend/model_api_runtime/v2/scheduler.py`, `backend/model_api_runtime/v2/serve_worker.py`, `tests/test_v2_scheduler.py`
- Create: `tests/test_v2_serve_worker_screen_watch.py`
- Modify: `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`

**Interfaces:**
- `scheduler.run_scheduler_tick` gains an always-present `"screen_watch_enqueued": int`, driven by two **getattr-probed** optional deps `screen_watch_users()` and `tick_screen_watch(user_id)`. Copy the `scheduled_fired` / `extraction_enqueued` sweeps exactly, including `logger.exception` per-user isolation.
- `serve_worker._tick_screen_watch_for_user(user_id) -> int`

`_tick_screen_watch_for_user` must:
1. Read latest frame id/ts via `db.frame_list_meta(user_id)` (newest-last; see `screen/caption.py:134` for how it reverses). **No enclave.**
2. Read the last `role == "user"` message ts from `core_store.get_store(user_id).chat_messages`. **No enclave** — `role`/`ts` are plaintext.
3. Call the pure `screen_watch.should_watch(...)`.
4. If it says yes, ALSO consult `_wake_decision_for_user(user_id)` (the existing read-only proactive oracle) and require `should_wake`. This is the zero-pre-activation-burn + Ambient-off gate.
5. On a real wake: `jobs_store.enqueue_job(uid, "screen_watch", reason="screen_watch")`, `core_wake_bus.notify("v2_jobs", uid)`, and persist `last_screen_watch_frame_id=<latest>`.
6. **ALWAYS** advance `next_screen_watch_at = now + screen_watch.INTERVAL_SEC`, whatever the outcome. Otherwise a blocked user is reconsidered on every single tick.
7. Return `1` if a job was enqueued, else `0`.

**Deliberate divergence from the resident, and it is a fix:** only update `last_screen_watch_frame_id` when we actually wake. The resident writes it as soon as `fresh and changed`, even if the tick is then suppressed by `chatting` — which **permanently loses** that frame. Encode this in a test and mention it in the report.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_v2_serve_worker_screen_watch.py
def test_wakes_and_persists_the_frame_id(monkeypatch): ...
def test_chat_suppression_does_not_consume_the_frame_id(monkeypatch):
    """The deliberate fix vs the resident: a frame suppressed by active chat must remain
    'new' so it can still be seen once the user stops typing."""
    # should_watch -> (False, "chatting"); assert enqueue NOT called AND
    # upsert_wake_schedule called WITHOUT last_screen_watch_frame_id
def test_blocked_proactive_gate_never_enqueues(monkeypatch):
    """Zero pre-activation burn: should_watch says yes, oracle says no -> no job."""
def test_next_screen_watch_at_always_advances(monkeypatch): ...
```

Append to `tests/test_v2_scheduler.py`:

```python
def test_tick_sweeps_screen_watch_users_and_isolates_failures(): ...
def test_tick_skips_screen_watch_sweep_when_deps_absent(): ...
```

- [ ] **Step 2: Implement, run, and update the strict-equality assertions**

Adding `screen_watch_enqueued` breaks the strict-`==` dicts in `tests/test_v2_scheduler.py` that were already updated twice (for `scheduled_fired`, then `extraction_enqueued`). Update them again, delete nothing, report it.

- [ ] **Step 3: Boot check** (the deploy entrypoint is a script)

```bash
python -c "
import sys, pathlib, importlib.util
src = pathlib.Path('backend/model_api_runtime/v2/serve_worker.py')
sys.path.insert(0, str(src.parent)); sys.argv=['serve_worker.py']
spec = importlib.util.spec_from_file_location('__not_main__', src)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
sd = m._build_scheduler_deps()
print('screen_watch deps:', hasattr(sd,'screen_watch_users'), hasattr(sd,'tick_screen_watch'))
from model_api_runtime.v2 import scheduler
import types
print(scheduler.run_scheduler_tick(types.SimpleNamespace(due_users=lambda: []), now=0.0))
"
```

- [ ] **Step 4: Parity matrix**

`docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`: §B `screen-watch` row → producer `serve_worker._tick_screen_watch_for_user` (pure gate `v2/screen_watch.py`, state in `v2_wake_schedule`), handler `_run_wake` → **aligned**. §F bucket 1: strike `screen_watch` — **the bucket is now empty**. Record the deliberate divergence (suppressed frames are not consumed) and the dead `v2_wake_schedule.next_capture_at` column in §G.

- [ ] **Step 5: Full suite + Do NOT commit.**

---

## Traceability

| Test | Row |
|---|---|
| `test_unchanged_frame_does_not_watch` | spec §2(a) — persisted frame id |
| `test_freshness_is_checked_before_chatting` | spec §3.2 — reason precedence |
| `test_due_screen_watch_users_excludes_payment_cooldown` | BYOK cooldown invariant |
| `test_screen_watch_turn_passes_screen_context_and_its_own_prompt` | §B screen-watch |
| `test_screen_watch_silence_completes_without_a_bubble` | no-filler |
| `test_chat_suppression_does_not_consume_the_frame_id` | spec §3.3 deliberate divergence |
| `test_blocked_proactive_gate_never_enqueues` | zero pre-activation burn |

## Out of scope

resident tokens/turn baseline; §G Q2 (in-CVM LiteLLM child); cleaning up the dead `v2_wake_schedule.next_capture_at` column; porting the resident's names-only tool-list trimming (V2's wake turn runs no planner/tool loop at all).
