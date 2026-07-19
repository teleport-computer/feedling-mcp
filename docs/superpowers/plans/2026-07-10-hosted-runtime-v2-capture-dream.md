# Hosted Runtime V2 — `capture` + `dream` lanes — Implementation Plan

> **Correctness update (2026-07-18):** production now validates the complete
> parser → action → Memory Garden executor path. Non-empty Capture and Dream
> results cannot complete unless their memory writes succeed.

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development.

**Goal:** Give V2 its own memory-extraction lanes: `capture` (a chat window → memory cards) and `dream` (existing cards + recent conversation → consolidations).

**Architecture:** The prompts and parsers already exist as PURE functions in `backend/memory/*_prompt_v1.py`, and the persistence entrypoint `memory_core.actions()` is the same one `/v1/memory/actions` and the `memory_write` capability already use. The gates already exist in `proactive/{capture,dream}_scheduler.py` — but unlike `scheduled_wake_v2`, they have **no injectable submitter** and hard-code an append into the legacy `proactive_jobs` stream that nothing drains under V2. We add that seam (defaulting to today's behaviour), put the LLM-call + parse in a pure `v2/extraction.py`, and give `worker` one shared `_run_extraction` skeleton that both lanes inject into.

**Spec:** `docs/superpowers/specs/2026-07-10-hosted-runtime-v2-capture-dream-design.md`

## Global Constraints

- **NO-COMMIT.** Never `git commit` / `git add` / `git stash` / `git stash pop` / `git checkout --` / `git reset` / `git clean`. The stash stack is SHARED with other live sessions and worktrees; a concurrent session is active in this repo.
- **Worktree:** `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2`, branch `feat/hosted-runtime-v2`. Never the main checkout.
- **Do not modify** `tools/chat_resident_consumer.py`, `responder.py`, `provider_client.py`, `compaction.py`, `agent_loop.py`, `planner.py`, `executor.py`, `capabilities/*`, `memory/capture_prompt_v1.py`, `memory/dream_prompt_v1.py`.
- **`extraction.py` is PURE**: it may import `provider_client` and the two `memory.*_prompt_v1` modules (pure prompt/parse). It must NOT import `hosted`, `agent_runtime`, `core.store`, `jobs_store`, or any DB module. Envelope construction and persistence arrive as injected callables.
- **no-filler:** `capture`/`dream` NEVER write a chat bubble and NEVER emit a user-visible error chip. Both have their own self-contained `try/except` (mirror `_run_compaction`/`_run_wake`) and must never fall into `process_job`'s chat-turn `except`.
- **Empty result is SUCCESS, not failure.** 0 cards / 0 consolidations → `mark_completed`. This mirrors the wake lane's "weak wake sleeps".
- **Context fetch failures degrade, they do not fail.** buckets/threads/identity unavailable → pass `""`; both prompt builders already fall back to "（暂无）".
- **BYOK-only:** every LLM call uses the user's own JIT-decrypted key. No platform key fallback, ever.
- **Zero drift for resident:** the new `submit=` parameters default to `None` = exactly today's behaviour. `tools/chat_resident_consumer.py` is not touched.
- **Test baseline:** 2933 passed / 8 pre-existing failures (`test_chat_route_debug_trace` ×3, `test_debug_trace_event_route`, `test_memory_capture_trace`, `test_model_api_path` verify-ping ×2, and `test_model_api_path::test_model_api_setup_reasoning_effort_off_and_default_disable_gateway_reasoning` — proven upstream).
- **Full-suite command** (a bare `pytest tests/` aborts at collection and tests NOTHING):
  ```bash
  python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
  ```
  Same two flags for any `-k` sweep. Postgres required: `pg_isready -h 127.0.0.1 -p 55432`.
- Adding the `dream` lane needs **no migration**: `agent_jobs.lane` is `TEXT NOT NULL` with no CHECK constraint (`alembic/versions/0014_hosted_runtime_v2.py:20`). `LANES` is enforced only in Python (`jobs_store.py:67`).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `backend/model_api_runtime/v2/extraction.py` | **NEW, pure.** BYOK LLM call + parse; cards/consolidations → memory actions | Create |
| `backend/proactive/capture_scheduler.py` | capture gate | **Modify** — optional `submit=` seam |
| `backend/proactive/dream_scheduler.py` | dream gate | **Modify** — optional `submit=` seam |
| `backend/model_api_runtime/v2/jobs_store.py` | lane roster | **Modify** — add `dream` |
| `backend/model_api_runtime/v2/worker.py` | turn assembly | **Modify** — `_run_extraction` + two thin lane handlers + dispatch + 3 `TurnDeps` fields |
| `backend/model_api_runtime/v2/serve_worker.py` | production DI | **Modify** — deps + producers |
| `backend/model_api_runtime/v2/scheduler.py` | tick | **Modify** — capture/dream sweeps |

---

## Task 1: `extraction.py` — the pure core

**Files:**
- Create: `backend/model_api_runtime/v2/extraction.py`
- Test: `tests/test_v2_extraction.py` (create)

**Interfaces (later tasks depend on these exact names):**
- `async extraction.extract(*, provider_config, prompt: str, parse, max_tokens: int = 1500) -> tuple[Any, str | None]`
  Returns `(parsed, None)` on success, `(None, reason)` on provider error / parse error. **Never raises.**
- `extraction.cards_to_actions(cards, *, occurred_at: str, source_ids: list[str], build_envelope) -> tuple[list[dict], int, int]`
  Returns `(actions, cards_added, cards_superseded)`. `build_envelope(inner: dict) -> dict` is injected (the real one encrypts).
  Raises `ValueError("capture_no_memory_actions")` when `cards` is non-empty but produced no actions — ported verbatim from the resident.
- `extraction.consolidations_to_actions(consolidations, *, occurred_at, source_ids, build_envelope) -> tuple[list[dict], int, int]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_v2_extraction.py`:

```python
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

from model_api_runtime.v2 import extraction


def _env(inner):
    return {"body_ct": "CT", "_inner": inner}


# ---------- extract ----------

def test_extract_returns_parsed_on_success(monkeypatch):
    async def _fake(cfg, messages, **kw):
        return {"reply": '{"cards": []}'}

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _fake)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P", parse=lambda raw: (["ok"], None)))
    assert parsed == ["ok"] and err is None


def test_extract_returns_reason_on_provider_error(monkeypatch):
    async def _boom(cfg, messages, **kw):
        raise RuntimeError("402 no credit")

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _boom)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P", parse=lambda raw: (["x"], None)))
    assert parsed is None
    assert err.startswith("provider_call_failed:")


def test_extract_returns_reason_on_parse_error(monkeypatch):
    async def _fake(cfg, messages, **kw):
        return {"reply": "garbage"}

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _fake)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P", parse=lambda raw: (None, "bad_json")))
    assert parsed is None and err == "bad_json"


def test_extract_treats_empty_reply_as_a_reason_not_a_crash(monkeypatch):
    async def _fake(cfg, messages, **kw):
        return {"reply": "   "}

    monkeypatch.setattr(extraction.provider_client, "reliable_chat_completion_async", _fake)
    parsed, err = asyncio.run(extraction.extract(
        provider_config=object(), prompt="P", parse=lambda raw: (["never"], None)))
    assert parsed is None and err == "empty_reply"


# ---------- cards_to_actions ----------

def test_cards_to_actions_add_and_supersede():
    cards = [
        {"action": "add", "summary": "s1", "content": "c1", "bucket": "b", "threads": ["t"]},
        {"action": "supersede", "target_id": "m_old", "summary": "s2", "content": "c2"},
    ]
    actions, added, superseded = extraction.cards_to_actions(
        cards, occurred_at="2026-07-10T10:00:00Z", source_ids=["m1"], build_envelope=_env)
    assert added == 1 and superseded == 1
    assert actions[0]["type"] == "memory.add"
    assert actions[0]["capture_mode"] == "memory_capture"
    assert actions[0]["source_chat_message_ids"] == ["m1"]
    assert actions[1]["type"] == "memory.supersede"
    assert actions[1]["supersedes"] == "m_old"
    # the envelope carries the card's inner fields, built by the injected callable
    assert actions[0]["envelope"]["_inner"]["summary"] == "s1"
    assert actions[0]["envelope"]["_inner"]["threads"] == ["t"]


def test_merge_or_supersede_without_target_degrades_to_add():
    """Ported verbatim from the resident: a merge with no target_id is an add."""
    actions, added, superseded = extraction.cards_to_actions(
        [{"action": "merge", "summary": "s"}],
        occurred_at="T", source_ids=[], build_envelope=_env)
    assert added == 1 and superseded == 0
    assert actions[0]["type"] == "memory.add"


def test_unknown_action_yields_nothing_and_nonempty_cards_raise():
    """Non-empty cards that produce zero actions is a hard error — the model returned
    something we don't understand, and silently writing nothing would hide it."""
    with pytest.raises(ValueError, match="capture_no_memory_actions"):
        extraction.cards_to_actions(
            [{"action": "frobnicate"}], occurred_at="T", source_ids=[], build_envelope=_env)


def test_empty_cards_is_not_an_error():
    actions, added, superseded = extraction.cards_to_actions(
        [], occurred_at="T", source_ids=[], build_envelope=_env)
    assert actions == [] and added == 0 and superseded == 0


# ---------- consolidations_to_actions ----------

def test_consolidations_to_actions_supersedes_when_target_present():
    actions, added, superseded = extraction.consolidations_to_actions(
        [{"action": "supersede", "target_id": "m1", "summary": "merged"}],
        occurred_at="T", source_ids=[], build_envelope=_env)
    assert superseded == 1
    assert actions[0]["type"] == "memory.supersede"
    assert actions[0]["capture_mode"] == "memory_dream"


def test_extraction_is_pure():
    import pathlib
    src = pathlib.Path(extraction.__file__).read_text()
    for forbidden in ("import hosted", "from hosted", "agent_runtime", "jobs_store",
                      "core.store", "psycopg", "memory_core"):
        assert forbidden not in src, f"extraction.py must not reference {forbidden}"
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_v2_extraction.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'model_api_runtime.v2.extraction'`.

- [ ] **Step 3: Implement `backend/model_api_runtime/v2/extraction.py`**

```python
"""V2 记忆抽取（capture / dream）的**纯**核心：BYOK LLM 调用 + 解析 + 卡片→memory action。

依赖方向：只 import provider_client（底层）和 memory/*_prompt_v1（纯 prompt/parse，无 I/O）。
**绝不** import hosted / agent_runtime / core.store / jobs_store / memory_core —— 信封构造与
落库都由调用方（worker）经注入回调提供。

`cards_to_actions` / `consolidations_to_actions` 是从 `tools/chat_resident_consumer.py`
的 `_capture_actions_from_cards` 移植过来的纯映射逻辑（spec §3.3）。resident 保留它自己的
副本直到退役——刻意的重复，换 kill-resident 之前的零风险。
"""
from __future__ import annotations

from typing import Any, Callable

import provider_client

_MAX_TOKENS = 1500
_TEMPERATURE = 0.3
_TIMEOUT_SEC = 90.0


async def extract(
    *,
    provider_config: Any,
    prompt: str,
    parse: Callable[[str], tuple],
    max_tokens: int = _MAX_TOKENS,
) -> tuple[Any, str | None]:
    """跑一次 BYOK 抽取调用并解析。**永不抛**——失败一律返回 (None, reason)。

    `parse` 是 memory/*_prompt_v1 里的纯解析函数。它的返回是 (value, err) 或
    (value, questions, err)；我们只取首项与末项（末项恒为 err）。
    """
    messages = [{"role": "user", "content": prompt}]
    try:
        result = await provider_client.reliable_chat_completion_async(
            provider_config, messages,
            max_tokens=max_tokens, temperature=_TEMPERATURE, timeout=_TIMEOUT_SEC,
        )
    except Exception as e:  # noqa: BLE001 — 背景 job：归一成 reason，绝不抛
        return None, f"provider_call_failed:{type(e).__name__}"
    reply = str((result or {}).get("reply") or "").strip()
    if not reply:
        return None, "empty_reply"
    parsed = parse(reply)
    value, err = parsed[0], parsed[-1]
    if err:
        return None, str(err)
    return value, None


def _inner_from_card(card: dict) -> dict:
    return {
        "summary": str(card.get("summary") or "").strip(),
        "content": str(card.get("content") or "").strip(),
        "bucket": str(card.get("bucket") or "").strip(),
        "threads": list(card.get("threads") or []),
    }


def _to_actions(
    cards: list[dict],
    *,
    occurred_at: str,
    source_ids: list[str],
    build_envelope: Callable[[dict], dict],
    capture_mode: str,
    reason: str,
) -> tuple[list[dict], int, int]:
    actions: list[dict] = []
    added = 0
    superseded = 0
    for card in cards or []:
        action = str(card.get("action") or "").strip().lower()
        target_id = str(card.get("target_id") or "").strip()
        base = {
            "envelope": build_envelope(_inner_from_card(card)),
            "reason": reason,
            "capture_mode": capture_mode,
            "source_chat_message_ids": list(source_ids),
        }
        if action == "add" or (action in {"merge", "supersede"} and not target_id):
            actions.append({"type": "memory.add", **base})
            added += 1
            continue
        if action in {"merge", "supersede"} and target_id:
            actions.append({"type": "memory.supersede", "supersedes": target_id, **base})
            superseded += 1
    if cards and not actions:
        # 模型给了卡但一张都没映射成 action —— 说明它返回了我们不认识的 action 名。
        # 静默写零条会把这件事藏起来，所以硬失败（与 resident 同口径）。
        raise ValueError("capture_no_memory_actions")
    return actions, added, superseded


def cards_to_actions(cards, *, occurred_at, source_ids, build_envelope):
    return _to_actions(cards, occurred_at=occurred_at, source_ids=source_ids,
                       build_envelope=build_envelope, capture_mode="memory_capture",
                       reason="Memory captured from a completed chat window.")


def consolidations_to_actions(consolidations, *, occurred_at, source_ids, build_envelope):
    return _to_actions(consolidations, occurred_at=occurred_at, source_ids=source_ids,
                       build_envelope=build_envelope, capture_mode="memory_dream",
                       reason="Memory consolidated during a dream pass.")
```

Note: `occurred_at` is accepted for signature-compatibility with the resident and for the envelope builder to consume; the injected `build_envelope` is what actually stamps it. Do not drop the parameter.

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_v2_extraction.py tests/test_v2_dependency_direction.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: new tests pass; 8 pre-existing failures, 0 new.

- [ ] **Step 5: Do NOT commit.**

---

## Task 2: the `submit=` seam in the capture and dream gates

`ScheduledWakeServiceV2.fire_due_timers` already takes an injected `submit_wake`; that is how the last round redirected timers into `agent_jobs` with zero drift. `capture_scheduler.tick_quiet_capture` and `dream_scheduler.tick_memory_dream` have no such seam — they hard-call `_enqueue_window` / their dream equivalent, which append to the legacy `proactive_jobs` stream that nothing drains under V2. Give them the same seam. **Default `None` = today's behaviour, byte for byte.**

**Files:**
- Modify: `backend/proactive/capture_scheduler.py`, `backend/proactive/dream_scheduler.py`
- Test: `tests/test_capture_dream_submit_seam.py` (create)

**Interfaces:**
- `capture_scheduler.tick_quiet_capture(store, *, now=None, submit=None) -> dict`
- `dream_scheduler.tick_memory_dream(store, *, now=None, force=False, submit=None) -> dict`
- `submit(store, *, trigger: str, now: float) -> dict` — when provided, it REPLACES the legacy enqueue and its return value is used as the enqueue result (must carry at least `{"enqueued": bool, "reason": str, "job": dict | None}`).

**All gate decisions stay where they are.** The seam is ONLY at the point where the gate has already decided to enqueue.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_capture_dream_submit_seam.py`:

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import inspect

from proactive import capture_scheduler, dream_scheduler


def test_both_ticks_accept_an_optional_submit_kwarg():
    for fn in (capture_scheduler.tick_quiet_capture, dream_scheduler.tick_memory_dream):
        params = inspect.signature(fn).parameters
        assert "submit" in params, f"{fn.__name__} needs a submit seam"
        assert params["submit"].default is None, f"{fn.__name__}: submit must default to None"
        assert params["submit"].kind is inspect.Parameter.KEYWORD_ONLY


def test_capture_submit_replaces_the_legacy_enqueue(monkeypatch):
    """When `submit` is given, `_enqueue_window` must NOT run (it appends to the dead
    legacy proactive_jobs stream)."""
    legacy_called = {"n": 0}
    monkeypatch.setattr(capture_scheduler, "_enqueue_window",
                        lambda *a, **k: legacy_called.update(n=legacy_called["n"] + 1) or {})

    seen = {}

    def _submit(store, *, trigger, now):
        seen["trigger"] = trigger
        return {"enqueued": True, "reason": "v2", "job": {"id": "j1"}}

    # Drive the gate straight to its enqueue point by stubbing the state it reads.
    monkeypatch.setattr(capture_scheduler, "refresh_capture_state_from_chat",
                        lambda store, now=None: {"last_seen_message_id": "m9",
                                                 "message_count": 3, "last_seen_ts": 0.0,
                                                 "last_captured_until_message_id": ""})
    monkeypatch.setattr(capture_scheduler, "_capture_enabled", lambda store: True)
    monkeypatch.setattr(capture_scheduler, "quiet_sec", lambda: 0)

    out = capture_scheduler.tick_quiet_capture(object(), now=1000.0, submit=_submit)

    assert legacy_called["n"] == 0
    assert out["enqueued"] is True and out["reason"] == "v2"
    assert seen["trigger"] == "quiet_timeout"


def test_capture_without_submit_still_uses_the_legacy_enqueue(monkeypatch):
    """Zero drift: the resident path must be byte-for-byte unchanged."""
    called = {"n": 0}
    monkeypatch.setattr(
        capture_scheduler, "_enqueue_window",
        lambda store, *, trigger, now: called.update(n=called["n"] + 1) or
        {"enqueued": True, "reason": "legacy", "job": None})
    monkeypatch.setattr(capture_scheduler, "refresh_capture_state_from_chat",
                        lambda store, now=None: {"last_seen_message_id": "m9",
                                                 "message_count": 3, "last_seen_ts": 0.0,
                                                 "last_captured_until_message_id": ""})
    monkeypatch.setattr(capture_scheduler, "_capture_enabled", lambda store: True)
    monkeypatch.setattr(capture_scheduler, "quiet_sec", lambda: 0)

    out = capture_scheduler.tick_quiet_capture(object(), now=1000.0)
    assert called["n"] == 1
    assert out["reason"] == "legacy"


def test_capture_gate_still_blocks_before_reaching_submit(monkeypatch):
    """A blocked gate must never call submit — zero pre-activation burn."""
    submitted = {"n": 0}
    monkeypatch.setattr(capture_scheduler, "_capture_enabled", lambda store: False)
    monkeypatch.setattr(capture_scheduler, "refresh_capture_state_from_chat",
                        lambda store, now=None: {})

    out = capture_scheduler.tick_quiet_capture(
        object(), now=1000.0, submit=lambda *a, **k: submitted.update(n=1) or {})
    assert submitted["n"] == 0
    assert out["enqueued"] is False and out["reason"] == "capture_disabled"
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_capture_dream_submit_seam.py -q
```

Expected: FAIL — `AssertionError: tick_quiet_capture needs a submit seam`.

- [ ] **Step 3: Implement the seam**

Read `capture_scheduler.tick_quiet_capture` (`:280`) and `dream_scheduler.tick_memory_dream` (`:211`) first.

In `tick_quiet_capture`, add `submit: Callable | None = None` as a keyword-only parameter, and replace the single line

```python
    result = _enqueue_window(store, trigger="quiet_timeout", now=now_ts)
```

with

```python
    # V2 seam（spec §3.1）：默认 None = 今天的行为（append 进 legacy proactive_jobs 流）。
    # V2 的 scheduler 传入一个把 job 塞进 agent_jobs 的 submitter —— 这样 gate 的五道早退
    # （capture_disabled / no_new_messages / already_captured / quiet_not_due / min_interval）
    # 和失败退避全部原样复用，零漂移。镜像 ScheduledWakeServiceV2.fire_due_timers(submit_wake=)。
    _enqueue = submit if submit is not None else _enqueue_window
    result = _enqueue(store, trigger="quiet_timeout", now=now_ts)
```

Do the same for `force_capture` (`trigger="manual_force"`) if it shares `_enqueue_window` — check, and if so give it the same optional `submit=None`.

Apply the identical pattern in `dream_scheduler.tick_memory_dream`. Read it and find its single enqueue call site; the gate logic above it must not move.

**Do not change any gate condition, any early return, or any returned key.**

- [ ] **Step 4: Run tests**

```bash
python -m pytest tests/test_capture_dream_submit_seam.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: new tests pass; 8 pre-existing failures, 0 new. In particular `tests/test_memory_capture_trace.py` (already a known pre-existing failure) must not get WORSE, and any other capture-scheduler test must pass untouched — that is the zero-drift proof.

- [ ] **Step 5: Do NOT commit.**

---

## Task 3: the `dream` lane + the shared `_run_extraction` handler

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py`, `backend/model_api_runtime/v2/worker.py`
- Test: `tests/test_v2_extraction_lanes.py` (create)

**Interfaces:**
- `jobs_store.LANES` gains `"dream"`; `jobs_store.LANE_PRIORITY["dream"] = 10` (same as `capture`/`maintenance`).
- `worker._EXTRACTION_LANES = frozenset({"capture", "dream"})`
- `TurnDeps` gains three optional fields (all default `None`):
  - `read_memory_context: Callable[[str], dict] | None` — `user_id -> {"ai_name","user_name","buckets","threads","identity","cards"}` (all strings; any may be `""`)
  - `apply_memory_actions: Callable[[str, list[dict]], dict] | None` — `(user_id, actions) -> result dict`
  - `build_memory_envelope: Callable[[str, dict], dict] | None` — `(user_id, inner) -> envelope` (encrypts)
- `worker._run_extraction(job_id, user_id, lane, deps, provider_config, enclave_sem) -> str`

**Behaviour contract (all tested):**
- lane `capture`: window = `deps.read_tail(user_id, 0.0, _TAIL_HARD_CAP)`; prompt = `build_capture_prompt`; parse = `parse_capture_cards`; actions = `extraction.cards_to_actions`.
- lane `dream`: prompt = `build_dream_prompt(cards=..., recent_conversations=...)`; parse = `parse_dream_consolidations`; actions = `extraction.consolidations_to_actions`. **`questions` (the parser's 2nd return) is discarded this round** — spec §5.3.
- 0 cards / 0 consolidations → `mark_completed`, no actions applied. **Not a failure.**
- Any failure → **silent** `mark_failed`. No bubble, no `_emit_status(..., "error")`, no `_surface_terminal_error`.
- Missing deps (`read_memory_context is None` etc.) → treat as empty context / skip persistence, still complete cleanly.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_v2_extraction_lanes.py`. Use `test_v2_worker.py`'s conventions (sync tests, `asyncio.run`, `conftest.seed_user`, `_reset`-style cleanup, real `jobs_store`).

```python
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest

import conftest
import db
import provider_client
from model_api_runtime.v2 import extraction, jobs_store, worker

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user", base_url="")


@pytest.fixture(autouse=True)
def _clean():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _job_row(job_id):
    with db.get_pool().connection() as conn:
        return conn.execute(
            "SELECT status, last_error FROM agent_jobs WHERE id=%s", (job_id,)).fetchone()


def _deps(**over):
    base = dict(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (_BYOK, {}),
        is_official=lambda cfg: True,
        mint_enclave_token=lambda uid: "rt",
        read_tail=lambda uid, after, limit: [
            {"id": "m1", "ts": 1.0, "role": "user", "content": "我换工作了"}],
        read_memory_context=lambda uid: {
            "ai_name": "小克", "user_name": "Z", "buckets": "B",
            "threads": "T", "identity": "I", "cards": "C"},
        build_memory_envelope=lambda uid, inner: {"body_ct": "CT", "_inner": inner},
        apply_memory_actions=lambda uid, actions: {"applied": len(actions)},
    )
    base.update(over)
    return worker.TurnDeps(**base)


def test_dream_is_a_lane_with_background_priority():
    assert "dream" in jobs_store.LANES
    assert jobs_store.LANE_PRIORITY["dream"] == jobs_store.LANE_PRIORITY["capture"]


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_extraction_lane_applies_actions_and_completes(monkeypatch, lane):
    uid = f"u_x_{lane}"
    conftest.seed_user(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _fake_extract(*, provider_config, prompt, parse, **kw):
        assert provider_config is _BYOK          # BYOK-only
        return ([{"action": "add", "summary": "s", "content": "c"}], None)

    monkeypatch.setattr(extraction, "extract", _fake_extract)
    applied = {}
    deps = _deps(apply_memory_actions=lambda uid_, actions: applied.update(n=len(actions)) or {})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=True, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert applied == {"n": 1}
    assert _job_row(job_id)[0] == "completed"


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_zero_results_completes_without_applying_anything(monkeypatch, lane):
    """`nothing_worth_keeping` is SUCCESS — mirrors the wake lane's weak-wake-sleeps."""
    uid = f"u_x_empty_{lane}"
    conftest.seed_user(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _empty(*, provider_config, prompt, parse, **kw):
        return ([], None)

    monkeypatch.setattr(extraction, "extract", _empty)
    applied = {"n": 0}
    deps = _deps(apply_memory_actions=lambda uid_, a: applied.update(n=applied["n"] + 1) or {})

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=True, api_key=None, runtime_token="rt"))
    assert status == "completed"
    assert applied["n"] == 0
    assert _job_row(job_id)[0] == "completed"


@pytest.mark.parametrize("lane", ["capture", "dream"])
def test_extraction_failure_is_silent_no_bubble_no_error_chip(monkeypatch, lane):
    uid = f"u_x_fail_{lane}"
    conftest.seed_user(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job("w")

    async def _err(*, provider_config, prompt, parse, **kw):
        return (None, "provider_call_failed:RuntimeError")

    monkeypatch.setattr(extraction, "extract", _err)
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(t=text) or {"id": "r"})
    emitted = []
    monkeypatch.setattr(worker, "_emit_status", lambda *a, **k: emitted.append(a))

    status = asyncio.run(worker.process_job(
        job, _deps(), provider_config=_BYOK, is_official=True, api_key=None, runtime_token="rt"))

    assert status == "failed"
    assert written == {}                       # no chat bubble
    assert emitted == []                       # no user-visible status/error chip
    row = _job_row(job_id)
    assert row[0] == "failed" and "provider_call_failed" in (row[1] or "")


def test_capture_prompt_degrades_when_memory_context_is_missing(monkeypatch):
    """Context fetch failure must degrade, not fail the job (spec §3.5)."""
    uid = "u_x_nocontext"
    conftest.seed_user(uid)
    jobs_store.enqueue_job(uid, "capture")
    job = jobs_store.claim_next_job("w")

    seen = {}

    async def _cap(*, provider_config, prompt, parse, **kw):
        seen["prompt"] = prompt
        return ([], None)

    monkeypatch.setattr(extraction, "extract", _cap)
    status = asyncio.run(worker.process_job(
        job, _deps(read_memory_context=None), provider_config=_BYOK,
        is_official=True, api_key=None, runtime_token="rt"))
    assert status == "completed"
    assert "（暂无）" in seen["prompt"]          # prompt builder's own fallback kicked in
```

- [ ] **Step 2: Run to verify failure**

```bash
python -m pytest tests/test_v2_extraction_lanes.py -q
```

Expected: FAIL — `"dream" not in jobs_store.LANES` / `TurnDeps.__init__() got an unexpected keyword argument 'read_memory_context'`.

- [ ] **Step 3: Add the `dream` lane**

`backend/model_api_runtime/v2/jobs_store.py`:

```python
LANES = {"chat", "manual_wake", "heartbeat", "scheduled", "capture", "maintenance", "dream"}
```
and in `LANE_PRIORITY` add `"dream": 10,` next to `"capture": 10,`.

No migration: `agent_jobs.lane` is `TEXT NOT NULL` with no CHECK constraint; `LANES` is a Python-side guard only (`jobs_store.py:67`).

- [ ] **Step 4: Add the `TurnDeps` fields and the handler**

In `worker.py`, add the three optional `TurnDeps` fields documented in Interfaces above (each with a comment saying why it is injected: `worker.py` must not import `hosted` / `memory_core` / `core.envelope`-for-memory).

Add the import `from model_api_runtime.v2 import extraction as v2_extraction`, plus the two prompt modules — **these are pure and allowed**:

```python
from memory.capture_prompt_v1 import build_capture_prompt, parse_capture_cards
from memory.dream_prompt_v1 import build_dream_prompt, parse_dream_consolidations
```

Add near `_WAKE_LANES`:

```python
# 记忆抽取 lane（capture=一窗对话→记忆卡，dream=现有卡片→合并）。同形：
# build prompt → BYOK 抽取 → parse → memory actions。永不写气泡、永不弹 error chip。
_EXTRACTION_LANES = frozenset({"capture", "dream"})
```

Add the handler (mirror `_run_compaction`'s self-contained shape):

```python
async def _run_extraction(job_id, user_id: str, lane: str, deps: TurnDeps,
                          provider_config: Any, enclave_sem: "asyncio.Semaphore") -> str:
    """capture / dream：后台记忆抽取。自成一体的 try/except —— 绝不落进 process_job 那个
    chat-turn 的 except（那条会 emit 用户可见的 error status + record_terminal_error）。

    空结果（0 张卡 / 0 条合并）是**成功**：mark_completed，不写任何东西。与 wake lane 的
    「弱唤醒睡回去」同口径 —— 模型选择什么都不做，不是失败。
    """
    try:
        ctx = {}
        if deps.read_memory_context is not None:
            try:
                ctx = await asyncio.to_thread(deps.read_memory_context, user_id) or {}
            except Exception as e:  # noqa: BLE001 — 上下文取数失败 → 降级，不失败（spec §3.5）
                log.warning("[v2.worker] memory context unavailable for %s: %s", user_id, e)

        async with enclave_sem:
            tail = await asyncio.to_thread(deps.read_tail, user_id, 0.0, _TAIL_HARD_CAP) \
                if deps.read_tail is not None else []
        window = "\n".join(
            f"- {m.get('role')}: {context.text_of(m.get('content'))}" for m in tail).strip()
        source_ids = [str(m.get("id")) for m in tail if m.get("id")]

        if lane == "capture":
            prompt = build_capture_prompt(
                ai_name=ctx.get("ai_name", ""), user_name=ctx.get("user_name", ""),
                buckets=ctx.get("buckets", ""), threads=ctx.get("threads", ""),
                identity=ctx.get("identity", ""), window=window)
            parse, to_actions = parse_capture_cards, v2_extraction.cards_to_actions
        else:
            prompt = build_dream_prompt(
                ai_name=ctx.get("ai_name", ""), user_name=ctx.get("user_name", ""),
                cards=ctx.get("cards", ""), recent_conversations=window)
            # parse_dream_consolidations 返回 (consolidations, questions, err)。
            # questions 属于「主动提问」= wake 语义，本轮明确丢弃（spec §5.3）。
            parse, to_actions = parse_dream_consolidations, v2_extraction.consolidations_to_actions

        items, reason = await v2_extraction.extract(
            provider_config=provider_config, prompt=prompt, parse=parse)
        if reason:
            raise RuntimeError(reason)
        if not items:
            await asyncio.to_thread(jobs_store.mark_completed, job_id)
            return "completed"

        if deps.build_memory_envelope is None or deps.apply_memory_actions is None:
            await asyncio.to_thread(jobs_store.mark_completed, job_id)
            return "completed"

        actions, _added, _superseded = to_actions(
            items, occurred_at="", source_ids=source_ids,
            build_envelope=lambda inner: deps.build_memory_envelope(user_id, inner))
        await asyncio.to_thread(deps.apply_memory_actions, user_id, actions)
        await asyncio.to_thread(jobs_store.mark_completed, job_id)
        return "completed"
    except Exception as e:  # noqa: BLE001 — 背景 job：静默 mark_failed，绝不 surface/写气泡
        log.warning("[v2.worker] extraction job %s (lane=%s) failed: %s", job_id, lane, e)
        await asyncio.to_thread(
            jobs_store.mark_failed, job_id, f"extraction_failed: {str(e)[:160]}")
        return "failed"
```

Dispatch it in `process_job`, **before** the `if lane != "chat":` unhandled-lane bail-out added last round:

```python
        if lane in _EXTRACTION_LANES:
            return await _run_extraction(job_id, user_id, lane, deps, provider_config, enclave_sem)
```

`capture` therefore stops being "unhandled" and becomes a real lane. Update that bail-out's comment so it no longer claims capture/dream are unimplemented.

- [ ] **Step 5: Run tests**

```bash
python -m pytest tests/test_v2_extraction_lanes.py tests/test_v2_worker.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: new tests pass; 8 pre-existing failures, 0 new. **`tests/test_v2_worker.py::test_unhandled_lane_never_writes_a_bubble_and_fails_loudly_in_the_db` will now break** — it enqueues `capture`, which is no longer unhandled. Change it to use a genuinely unregistered lane. That test's INTENT (an unhandled lane must not take the chat path) must be preserved; report the old vs new assertion prominently. Note `jobs_store.enqueue_job` validates against `LANES`, so to enqueue a bogus lane you must insert the row directly or monkeypatch `LANES` — pick one and say which.

- [ ] **Step 6: Do NOT commit.**

---

## Task 4: production wiring — deps + producers

**Files:**
- Modify: `backend/model_api_runtime/v2/serve_worker.py`, `backend/model_api_runtime/v2/scheduler.py`
- Test: `tests/test_v2_serve_worker_extraction.py` (create), `tests/test_v2_scheduler.py` (append)

**Interfaces:**
- `serve_worker._read_memory_context(user_id) -> dict` — buckets/threads via `memory_core.buckets/threads`, identity via `identity_core.get_identity`, cards via `memory_core.index`-equivalent. **Every sub-fetch is individually try/excepted to `""`.**
- `serve_worker._apply_memory_actions(user_id, actions) -> dict` — `memory_core.actions(store, None, {"actions": actions})`, minted runtime token where required.
- `serve_worker._build_memory_envelope(user_id, inner) -> dict` — `core_envelope._build_shared_envelope_for_store(store, json.dumps(inner).encode())`, raising on failure (a card we cannot encrypt must not be silently dropped).
- `serve_worker._tick_capture_for_user(user_id) -> int` and `_tick_dream_for_user(user_id) -> int` — call `capture_scheduler.tick_quiet_capture(store, submit=...)` / `dream_scheduler.tick_memory_dream(store, submit=...)` where the submitter does `jobs_store.enqueue_job(user_id, "capture"|"dream")` + `core_wake_bus.notify("v2_jobs", user_id)` and returns `{"enqueued": True, "reason": "v2", "job": {"id": job_id}}`.
- `scheduler.run_scheduler_tick` return dict gains `"extraction_enqueued": int`; the sweep is driven by two more **getattr-probed** optional deps: `extraction_users() -> list[str]` and `tick_extraction(user_id) -> int`.

**Which users get swept?** Reuse `db.list_agent_runtime_enabled_users`'s inverse: the users currently in `db_action_v2` mode. Add `jobs_store.v2_mode_users(limit=500) -> list[str]` if no such helper exists — check `admin_core.list_runtime_modes` first, it may already do this.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_v2_serve_worker_extraction.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import serve_worker


def test_memory_context_degrades_each_field_independently(monkeypatch):
    """One failing sub-fetch must not blank the others, and must not raise."""
    serve_worker.wire_assembly()
    monkeypatch.setattr("memory.memory_core.buckets",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("memory.memory_core.threads", lambda *a, **k: ({"threads": ["t1"]}, 200))
    ctx = serve_worker._read_memory_context("u_ctx_degrade")
    assert ctx["buckets"] == ""
    assert isinstance(ctx["threads"], str)


def test_capture_submit_enqueues_a_capture_agent_job(monkeypatch):
    from model_api_runtime.v2 import jobs_store
    serve_worker.wire_assembly()
    calls = []
    monkeypatch.setattr(jobs_store, "enqueue_job",
                        lambda u, lane, **kw: calls.append((u, lane)) or ("j1", False))
    monkeypatch.setattr("proactive.capture_scheduler.tick_quiet_capture",
                        lambda store, *, now=None, submit=None:
                            submit(store, trigger="quiet_timeout", now=0.0))
    assert serve_worker._tick_capture_for_user("u_cap") == 1
    assert calls == [("u_cap", "capture")]


def test_dream_submit_enqueues_a_dream_agent_job(monkeypatch):
    from model_api_runtime.v2 import jobs_store
    serve_worker.wire_assembly()
    calls = []
    monkeypatch.setattr(jobs_store, "enqueue_job",
                        lambda u, lane, **kw: calls.append((u, lane)) or ("j1", False))
    monkeypatch.setattr("proactive.dream_scheduler.tick_memory_dream",
                        lambda store, *, now=None, force=False, submit=None:
                            submit(store, trigger="dream", now=0.0))
    assert serve_worker._tick_dream_for_user("u_dream") == 1
    assert calls == [("u_dream", "dream")]
```

Append to `tests/test_v2_scheduler.py`:

```python
def test_tick_sweeps_extraction_users_and_isolates_failures():
    def _tick(uid):
        if uid == "bad":
            raise RuntimeError("boom")
        return 1

    deps = _SchedFakeDeps()                     # the helper added last round
    deps.extraction_users = lambda: ["bad", "good"]
    deps.tick_extraction = _tick
    out = scheduler.run_scheduler_tick(deps, now=100.0)
    assert out["extraction_enqueued"] == 1


def test_tick_skips_extraction_sweep_when_deps_absent():
    out = scheduler.run_scheduler_tick(_SchedFakeDeps(), now=100.0)
    assert out["extraction_enqueued"] == 0
```

- [ ] **Step 2: Run to verify failure**, then implement, mirroring EXACTLY the `scheduled` sweep added last round: `getattr(deps, "extraction_users", None)` / `getattr(deps, "tick_extraction", None)`, both required, per-user `try/except` with `logger.exception`, and the new key ALWAYS present in the returned dict.

`_tick_extraction_for_user(user_id)` in `serve_worker` calls capture then dream and returns the sum, so one dep drives both.

- [ ] **Step 3: Run tests**

```bash
python -m pytest tests/test_v2_serve_worker_extraction.py tests/test_v2_scheduler.py -q
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Adding a key to `run_scheduler_tick`'s return will break the strict-equality assertions updated last round. Update them again (add `"extraction_enqueued": 0`) and report it — same adjudication as last round, no test deleted.

- [ ] **Step 4: Do NOT commit.**

---

## Task 5: runtime verification + parity matrix

- [ ] **Step 1: Boot check** (the deploy entrypoint is a script; this is exactly how an import bug ships)

```bash
python -c "
import sys, pathlib, importlib.util
src = pathlib.Path('backend/model_api_runtime/v2/serve_worker.py')
sys.path.insert(0, str(src.parent)); sys.argv=['serve_worker.py']
spec = importlib.util.spec_from_file_location('__not_main__', src)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
d = m.build_production_deps()
print('read_memory_context:', d.read_memory_context is not None)
print('apply_memory_actions:', d.apply_memory_actions is not None)
print('build_memory_envelope:', d.build_memory_envelope is not None)
sd = m._build_scheduler_deps()
print('extraction deps:', hasattr(sd,'extraction_users'), hasattr(sd,'tick_extraction'))
"
```

- [ ] **Step 2: Update `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`**

§B: `capture` row → producer `serve_worker._tick_capture_for_user` (via the new `submit=` seam), handler `worker._run_extraction` → **aligned**. Add a `dream` row, same shape → **aligned**. §E: mark BUG-2 fully resolved (capture now has a real handler; the unhandled-lane guard remains as defence). §F bucket 1: strike `capture` and `dream`; leave `screen_watch`.

Record in §G that `dream`'s `questions` output is discarded this round.

- [ ] **Step 3: Full suite**

```bash
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: 8 pre-existing failures, 0 new.

- [ ] **Step 4: Do NOT commit.** Report the final working-tree file list.

---

## Traceability

| Test | Row |
|---|---|
| `test_extraction_lane_applies_actions_and_completes[capture]` | §B capture |
| `test_extraction_lane_applies_actions_and_completes[dream]` | §B dream |
| `test_zero_results_completes_without_applying_anything` | spec §3.4 |
| `test_extraction_failure_is_silent_no_bubble_no_error_chip` | no-filler invariant |
| `test_capture_without_submit_still_uses_the_legacy_enqueue` | spec §3.1 zero-drift |
| `test_capture_gate_still_blocks_before_reaching_submit` | zero pre-activation burn |
| `test_capture_prompt_degrades_when_memory_context_is_missing` | spec §3.5 |
| `test_extraction_is_pure` | dependency direction |

## Out of scope

`screen_watch` (a wake producer, not an extractor — its own round); dream's `questions` → proactive asking; de-duplicating the resident's copy of the card→action mapping (wait for kill-resident); resident tokens/turn baseline; §G Q2 (LiteLLM).
