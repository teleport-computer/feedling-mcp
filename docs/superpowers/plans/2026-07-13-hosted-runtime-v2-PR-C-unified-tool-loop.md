# Hosted Runtime V2 — PR C: Unified Provider-Native Tool Loop — Implementation Plan

> **STATUS: LANDED / CURRENT ARCHITECTURE IMPLEMENTATION RECORD.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the staged `is_official → rule_plan/official_plan → executor → forced responder` V2-foreground pipeline (chat + wake lanes) with ONE provider-native tool loop where every model uses the same tool catalog + loop, `reply{text}` is a special tool writing an immediate bubble, and no-tool-call plain text is the final reply.

**Architecture:** A new `tool_loop.py` drives rounds of `chat_completion_async(tools=CATALOG)`; tool_calls dispatch through a refactored executor (reads inline-parallel, writes as generation-fenced PR A effects, provenance-gated); the `reply` tool + terminal plain text enqueue reply effects drained immediately for visibility. Effects flow through PR A's outbox (this PR wires the producers); provider calls use PR B's `tools=`/`ProviderResponse` + fold into the whole-turn metric.

**Tech Stack:** Python asyncio, httpx (via provider_client), psycopg/Postgres, the existing `capabilities.registry` catalog + `executor` read/write machinery + PR A `effect_outbox`/`effect_id`/`cursor` + PR B `provider_client`/`provider_types`.

## Global Constraints

- **No behavior tiering:** NO `is_official → rule_plan/official_plan` dispatch in V2 production. `_is_official_identity` survives only as an identity-copy tag, never deciding the loop. Same tool catalog + loop for all models.
- **Preamble rule:** a response with BOTH plain text and tool_calls → the text is preamble/thinking, NOT a user bubble. Only `reply{text}` or a no-tool-call terminal response produces a bubble.
- **No forced responder:** empty `tool_calls` → the model's plain text IS the final reply. Never a second responder round-trip; never a filler bubble.
- **reply is immediate:** `reply{text}` writes an intermediate bubble immediately (enqueue reply effect + drain), then the loop continues.
- **Per-round fold, no debounce/restart:** before every provider call, fold in newly-visible user messages via the PR A seq cursor; no time-debounce; no loop restart; completed tool_results preserved.
- **Reads parallel, writes/replies ordered, call_id preserved.** Reads run inline (content fed back); writes become PR A effects (NOT run inline).
- **Provenance write gate:** web/tool observations carry provenance; a write tool_call (`memory_write`/`identity_patch`/`schedule_wake`/`cancel_wake`) is deterministically REFUSED when the turn holds no `user` or `wake_trigger` authorization (a purely-external round). Fixed rule in code, independent of tool content.
- **One-turn fallback, no persisted tier:** broken args / relay 400-422 → exactly one same-turn fallback (retry with `tools` omitted → plain text); nothing persisted.
- **Effects are generation-fenced + effect_id-idempotent (PR A):** a turn retry re-enqueues the SAME effect_ids → no duplicate bubble/write ("retry 不重复").
- **Scope:** chat + wake lanes only. `extraction`/`compaction` keep their current direct paths — do NOT touch them.
- **Dependency direction:** `model_api_runtime/v2/` core modules (incl. new `tool_loop.py`) must not import `hosted`/`agent_runtime` (guard `tests/test_v2_dependency_direction.py`). `tool_loop` takes injected deps.
- **NO-COMMIT:** leave every change in the working tree; never `git commit`/`git add`/`git stash`/`git checkout --`/`git reset`/`git clean`. The human commits. (The template's `git add`/`commit` steps are REPLACED by "leave in working tree.")
- **Postgres** `127.0.0.1:55432`. Full suite `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py`; baseline = 8 pre-existing failures (`test_chat_route_debug_trace`×3, `test_debug_trace_event_route`, `test_memory_capture_trace`, `test_model_api_path`×3).

## Reused interfaces (grounding, verified)

- `capabilities.registry`: `CAPABILITIES` (18 names), `WRITE_ACTIONS={memory_write,identity_patch,schedule_wake,cancel_wake}`, `READ_ACTIONS` (the other 14), `run_capability(action_type, store, *, api_key, runtime_token, params) -> CapabilityResult` (`.to_dict()` → `{"ok","data"/"error",...}`). `chat_image_read` excluded from the model catalog.
- `executor.py`: `_run_one(store, step, *, api_key, runtime_token, enclave_sem) -> (type, data)`; `execute_plan(...)` read-parallel(`read_sem`)/write-serial(`before_write` fence). Steps are `{"type","payload","_action_id"?}`.
- PR A: `db.effect_enqueue(effect_id,user_id,job_id,effect_type,expected_generation,payload)->bool`; `effect_id.derive(*,job_id,effect_type,ordinal)->str`; `effect_outbox.apply_pending_effects(user_id,*,dispatch)`; `serve_worker.build_production_effect_dispatch(user_id)`; `cursor.load_seq(store)`, `db.chat_messages_after_seq(user_id, after_seq)`; `db.get_runtime_generation(user_id)`.
- PR B: `provider_client.chat_completion_async(config, messages, *, tools=None) -> dict` with `result["tool_calls"]=[{id,name,args,args_raw,args_ok}]`; `provider_types.{ToolSpec,ToolCall,ToolResult,ProviderResponse}`; `worker.TurnMetrics.add_call(usage)`.
- `coalesce.coalesce_pending(messages, *, since_ts, decrypt) -> (coalesced, cursor)`; `context.build_turn_messages(...)`.

---

### Task 1: C5 — `effect_outbox.enqueue_effect` producer wrapper

**Files:**
- Modify: `backend/model_api_runtime/v2/effect_outbox.py`
- Test: `tests/test_v2_effect_enqueue.py`

**Interfaces:**
- Consumes: `db.effect_enqueue`, `effect_id.derive`.
- Produces: `effect_outbox.enqueue_effect(*, job_id, user_id, effect_type, ordinal, expected_generation, payload) -> str` (returns the effect_id; idempotent via the underlying ON CONFLICT DO NOTHING).

- [ ] **Step 1: Failing test** `tests/test_v2_effect_enqueue.py`

```python
import os, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db
from model_api_runtime.v2 import effect_outbox
from conftest import seed_user
pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")


@pytest.fixture
def pg_clean():
    with db.get_pool().connection() as c:
        c.execute("TRUNCATE v2_effect_outbox, v2_runtime_state, agent_jobs, user_blobs CASCADE")
    yield


def test_enqueue_effect_derives_id_and_inserts(pg_clean):
    seed_user("u_ee1")
    eid = effect_outbox.enqueue_effect(
        job_id=5, user_id="u_ee1", effect_type="reply", ordinal=0,
        expected_generation=1, payload={"text": "hi"})
    assert eid == "job5:reply:0"
    rows = list(db.effect_pending("u_ee1"))
    assert len(rows) == 1 and rows[0]["effect_id"] == eid


def test_enqueue_effect_idempotent_same_id(pg_clean):
    seed_user("u_ee2")
    a = effect_outbox.enqueue_effect(job_id=5, user_id="u_ee2", effect_type="reply",
                                     ordinal=0, expected_generation=1, payload={"text": "x"})
    b = effect_outbox.enqueue_effect(job_id=5, user_id="u_ee2", effect_type="reply",
                                     ordinal=0, expected_generation=1, payload={"text": "y"})
    assert a == b
    assert len(list(db.effect_pending("u_ee2"))) == 1  # second is a no-op
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: enqueue_effect`).

- [ ] **Step 3: Implement** — add to `effect_outbox.py`:

```python
from model_api_runtime.v2 import effect_id as _effect_id


def enqueue_effect(*, job_id, user_id, effect_type, ordinal, expected_generation, payload) -> str:
    """Producer-side entry to the generation-fenced outbox (spec C5 / PR A A4). Derives the
    deterministic effect_id, enqueues (ON CONFLICT DO NOTHING = retry-idempotent), returns the id.
    PR C's tool loop is the first caller; the already-wired apply_pending_effects drains it."""
    eid = _effect_id.derive(job_id=job_id, effect_type=effect_type, ordinal=ordinal)
    db.effect_enqueue(eid, user_id, job_id, effect_type, expected_generation, payload)
    return eid
```

- [ ] **Step 4: Run — expect PASS.** Leave in working tree.

---

### Task 2: C4 — Provenance types + deterministic write gate

**Files:**
- Create: `backend/model_api_runtime/v2/provenance.py`
- Test: `tests/test_v2_provenance.py`

**Interfaces:**
- Produces: constants `USER`, `WAKE_TRIGGER`, `EXTERNAL`, `INTERNAL`; `provenance_for_read(tool_name: str) -> str` (external for web_search/web_fetch, else internal); `turn_has_write_authorization(seed: str) -> bool` (True for USER/WAKE_TRIGGER); `write_gate(tool_name: str, *, turn_authorization: bool) -> tuple[bool, str]` → `(allowed, refusal_reason)`.

- [ ] **Step 1: Failing test** `tests/test_v2_provenance.py`

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import provenance as prov


def test_read_provenance_tags_web_external():
    assert prov.provenance_for_read("web_search") == prov.EXTERNAL
    assert prov.provenance_for_read("web_fetch") == prov.EXTERNAL
    assert prov.provenance_for_read("memory_index") == prov.INTERNAL


def test_turn_authorization():
    assert prov.turn_has_write_authorization(prov.USER) is True
    assert prov.turn_has_write_authorization(prov.WAKE_TRIGGER) is True
    assert prov.turn_has_write_authorization(prov.EXTERNAL) is False


def test_write_gate_allows_writes_with_authorization():
    allowed, reason = prov.write_gate("memory_write", turn_authorization=True)
    assert allowed is True and reason == ""


def test_write_gate_refuses_writes_without_authorization():
    for w in ("memory_write", "identity_patch", "schedule_wake", "cancel_wake"):
        allowed, reason = prov.write_gate(w, turn_authorization=False)
        assert allowed is False and "authorization" in reason


def test_write_gate_ignores_reads():
    allowed, reason = prov.write_gate("web_search", turn_authorization=False)
    assert allowed is True and reason == ""   # reads are never write-gated
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** `backend/model_api_runtime/v2/provenance.py`:

```python
"""Provenance tagging + deterministic write gate (spec C4).

Every tool observation is tagged with where its authorization comes from. A durable-write
tool_call is refused when the turn holds no user/wake authorization — i.e. a purely web-driven
round cannot self-authorize a memory_write/identity_patch/schedule. Fixed rule in code, independent
of what the tool content says, so a prompt-injecting page can never grant itself write access.
"""
from __future__ import annotations
from capabilities import registry as cap_registry

USER = "user"
WAKE_TRIGGER = "wake_trigger"
EXTERNAL = "external"
INTERNAL = "internal"

_EXTERNAL_READS = frozenset({"web_search", "web_fetch"})


def provenance_for_read(tool_name: str) -> str:
    return EXTERNAL if tool_name in _EXTERNAL_READS else INTERNAL


def turn_has_write_authorization(seed: str) -> bool:
    return seed in (USER, WAKE_TRIGGER)


def write_gate(tool_name: str, *, turn_authorization: bool) -> tuple[bool, str]:
    """Reads are never gated. A WRITE_ACTIONS tool is allowed only when the turn holds a
    user/wake authorization; otherwise deterministically refused."""
    if tool_name not in cap_registry.WRITE_ACTIONS:
        return True, ""
    if turn_authorization:
        return True, ""
    return False, f"write refused: no user/wake authorization in this turn for {tool_name}"
```

- [ ] **Step 4: Run — expect PASS.** Leave in working tree.

---

### Task 3: C1 — Tool-schema catalog (`capabilities/tool_schema.py`)

**Files:**
- Create: `backend/capabilities/tool_schema.py`
- Test: `tests/test_capabilities_tool_schema.py`

**Interfaces:**
- Consumes: `provider_types.ToolSpec`, `capabilities.registry.CAPABILITIES`.
- Produces: `build_tool_specs() -> list[ToolSpec]` (one per model-facing capability + the synthetic `reply` tool); `REPLY_TOOL = "reply"`; `PARAMS: dict[str, dict]` (per-tool JSON-Schema). Excludes `chat_image_read`.

- [ ] **Step 1: Failing test** `tests/test_capabilities_tool_schema.py`

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from capabilities import tool_schema, registry
from provider_types import ToolSpec


def test_catalog_covers_capabilities_plus_reply_minus_chat_image():
    specs = tool_schema.build_tool_specs()
    names = {s.name for s in specs}
    assert "reply" in names
    assert "chat_image_read" not in names   # BUG-1 mitigation
    for cap in registry.CAPABILITIES:
        if cap == "chat_image_read":
            continue
        assert cap in names, f"missing tool: {cap}"
    assert all(isinstance(s, ToolSpec) for s in specs)


def test_reply_tool_schema_shape():
    reply = next(s for s in tool_schema.build_tool_specs() if s.name == "reply")
    assert reply.parameters["required"] == ["text"]
    assert reply.parameters["properties"]["text"]["type"] == "string"


def test_write_tools_have_object_params():
    specs = {s.name: s for s in tool_schema.build_tool_specs()}
    for w in ("memory_write", "identity_patch", "schedule_wake"):
        assert specs[w].parameters["type"] == "object"
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** `backend/capabilities/tool_schema.py` — a `PARAMS` dict giving each of the 17 model-facing capabilities (all of `CAPABILITIES` except `chat_image_read`) an explicit JSON-Schema `parameters` object (derived from each capability's known params; e.g. `web_search`→`{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}`, `memory_write`→`{"type":"object","properties":{"actions":{"type":"array"}},"required":["actions"]}`, `identity_patch`→`{"type":"object","properties":{"patch":{"type":"object"}},"required":["patch"]}`, `schedule_wake`→`{"type":"object","properties":{"when":{"type":"string"},"reason":{"type":"string"}},"required":["when"]}`, reads with no required args→`{"type":"object","properties":{}}`) plus `DESCRIPTIONS` (a one-line description per tool), and:

```python
from provider_types import ToolSpec
from capabilities import registry

REPLY_TOOL = "reply"
_EXCLUDED = frozenset({"chat_image_read"})
# PARAMS and DESCRIPTIONS: explicit per-tool dicts (see above) covering every name in
# registry.CAPABILITIES minus _EXCLUDED, plus REPLY_TOOL.


def build_tool_specs() -> list[ToolSpec]:
    specs = []
    for name in registry.CAPABILITIES:
        if name in _EXCLUDED:
            continue
        specs.append(ToolSpec(name=name, description=DESCRIPTIONS[name], parameters=PARAMS[name]))
    specs.append(ToolSpec(name=REPLY_TOOL, description=DESCRIPTIONS[REPLY_TOOL], parameters=PARAMS[REPLY_TOOL]))
    return specs
```

(The task's implementer writes the complete `PARAMS`/`DESCRIPTIONS` for all 17 caps + reply, matching each capability's real param handling — read each `capabilities/*.py` module's `params` usage to get the field names right; the test above pins the reply + write-tool shapes.)

- [ ] **Step 4: Run — expect PASS.** Leave in working tree.

---

### Task 4: C3 — Tool-call dispatcher (`executor.dispatch_tool_calls`)

**Files:**
- Modify: `backend/model_api_runtime/v2/executor.py` (ADD `dispatch_tool_calls`, reusing `_run_one`/read-parallel/write-serial; do NOT delete `execute_plan` yet — Task 9 removes its callers).
- Test: `tests/test_v2_dispatch_tool_calls.py`

**Interfaces:**
- Consumes: `provider_types.ToolCall/ToolResult`, `provenance`, `run_capability`, `enqueue_effect` (Task 1), `_run_one`.
- Produces: `executor.dispatch_tool_calls(tool_calls, *, store, api_key, runtime_token, enclave_sem, turn_authorization, enqueue_write_effect, before_write=None) -> list[ToolResult]`. Reads (`READ_ACTIONS`) run inline-parallel via `_run_one`; writes (`WRITE_ACTIONS`) pass the provenance `write_gate` then call `enqueue_write_effect(tool_call)` (queued, NOT run inline) with `before_write` fence; every `call_id` is preserved; unknown name / `args_ok=False` → an error `ToolResult`, no crash.

- [ ] **Step 1: Failing test** `tests/test_v2_dispatch_tool_calls.py` — fakes: a fake `run_capability` (monkeypatch `cap_registry.run_capability` to return a canned `CapabilityResult`), a recording `enqueue_write_effect`. Assert: (a) two READ tool_calls both dispatch and return `ToolResult`s keyed by their `call_id`; (b) a WRITE tool_call with `turn_authorization=True` calls `enqueue_write_effect` once and returns a "queued" `ToolResult`, and is NOT run via run_capability inline; (c) a WRITE with `turn_authorization=False` returns a refusal `ToolResult` and does NOT enqueue; (d) an unknown tool name → error `ToolResult` (no raise); (e) a `ToolCall(args_ok=False)` → error `ToolResult`.

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** `dispatch_tool_calls` in `executor.py`:

```python
from provider_types import ToolResult
from model_api_runtime.v2 import provenance as _prov


async def dispatch_tool_calls(
    tool_calls, *, store, api_key, runtime_token, enclave_sem,
    turn_authorization: bool, enqueue_write_effect, before_write=None,
) -> list:
    """Dispatch provider tool_calls (spec C3). READ_ACTIONS run inline-parallel (their content
    feeds back to the model); WRITE_ACTIONS pass the provenance write_gate then are ENQUEUED as
    generation-fenced effects (not run inline); reply is handled by the caller (tool_loop), not here.
    Every call_id is preserved in the returned ToolResults. Never raises on a bad tool_call."""
    results_by_id: dict[str, ToolResult] = {}
    reads, writes = [], []
    for tc in tool_calls:
        if not tc.args_ok:
            results_by_id[tc.id] = ToolResult(call_id=tc.id, content=f"error: unparseable args for {tc.name}")
        elif tc.name in cap_registry.READ_ACTIONS:
            reads.append(tc)
        elif tc.name in cap_registry.WRITE_ACTIONS:
            writes.append(tc)
        else:
            results_by_id[tc.id] = ToolResult(call_id=tc.id, content=f"error: unknown tool {tc.name}")

    async def _read(tc):
        step = {"type": tc.name, "payload": tc.args}
        _t, data = await _run_one(store, step, api_key=api_key, runtime_token=runtime_token,
                                  enclave_sem=enclave_sem)
        content = _summarize_capability_result(data)   # existing char-cap/blob-strip style (reuse responder's helper)
        return tc.id, ToolResult(call_id=tc.id, content=content)

    read_sem = asyncio.Semaphore(4)

    async def _guarded(tc):
        async with read_sem:
            return await _read(tc)

    for cid, tr in (await asyncio.gather(*[_guarded(t) for t in reads]) if reads else []):
        results_by_id[cid] = tr

    for tc in writes:   # serial + provenance gate + fence
        allowed, reason = _prov.write_gate(tc.name, turn_authorization=turn_authorization)
        if not allowed:
            results_by_id[tc.id] = ToolResult(call_id=tc.id, content=reason)
            continue
        if before_write is not None:
            await before_write()
        enqueue_write_effect(tc)   # producer -> PR A outbox (drained at turn end)
        results_by_id[tc.id] = ToolResult(call_id=tc.id, content=f"queued: {tc.name}")

    return [results_by_id[tc.id] for tc in tool_calls]   # preserve original order + every call_id
```

Add a small `_summarize_capability_result(data) -> str` in executor (or reuse responder's `_action_context_str`-style formatting with the BUG-1 char caps).

- [ ] **Step 4: Run — expect PASS.** `python -m pytest tests/test_v2_dispatch_tool_calls.py tests/test_v2_dependency_direction.py -q`. Leave in working tree.

---

### Task 5: C2 — The unified tool loop (`model_api_runtime/v2/tool_loop.py`)

**Files:**
- Create: `backend/model_api_runtime/v2/tool_loop.py`
- Test: `tests/test_v2_tool_loop.py`

**Interfaces:**
- Consumes: `provider_client.chat_completion_async`, `provider_types.ProviderResponse`, `tool_schema.build_tool_specs`, Task 4 dispatcher (injected), `tool_schema.REPLY_TOOL`.
- Produces: `tool_loop.run_tool_loop(*, provider_config, build_messages, dispatch_tools, on_reply, fold_new_messages, add_usage, max_calls) -> LoopOutcome`. `LoopOutcome{final_text, rounds, stop_reason, replied_intermediate}`. `build_messages(folded_inputs, prior_tool_results) -> list[dict]`; `dispatch_tools(tool_calls) -> list[ToolResult]` (async, injected — the Task-4 dispatcher bound to the turn); `on_reply(text, *, final) -> None` (enqueue+drain reply effect); `fold_new_messages() -> list[dict]` (new user msgs); `add_usage(usage) -> None` (tm.add_call).

- [ ] **Step 1: Failing tests** `tests/test_v2_tool_loop.py` — with a fake `chat_completion_async` (monkeypatch) scripting rounds:
  - **plain text terminal:** one round returns `{"tool_calls":[], "reply":"hello"}` → `on_reply("hello", final=True)` called once, `dispatch_tools` never called, outcome `final_text=="hello"`, `rounds==1`, `replied_intermediate is False`. (P0: weak model 1 call 1 bubble.)
  - **preamble not a bubble:** a round returns text `"let me look"` + one tool_call → `on_reply` is NOT called with "let me look"; `dispatch_tools` IS called; next round terminal text → `on_reply(final=True)` with the terminal text only.
  - **reply special tool intermediate:** a round returns a `reply` tool_call `{text:"我看看哈"}` + continues → `on_reply("我看看哈", final=False)` called, loop continues, a later round terminates.
  - **budget bound:** if every round returns a tool_call, the loop stops at `max_calls` and the final call is made with `tools` omitted (assert the last `chat_completion_async` call got `tools=None`), producing a terminal text (never a filler).
  - **fold before each call:** `fold_new_messages` is invoked before each provider call after the first; its returned messages appear in the next `build_messages(folded_inputs, …)` (assert via a recording `build_messages`).

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** `tool_loop.py` — the loop:

```python
"""Unified provider-native tool loop (spec C2). Dependency-clean: no hosted/agent_runtime/db;
all side effects injected. One loop for every model — no is_official branch."""
from __future__ import annotations
from dataclasses import dataclass
from provider_types import ProviderResponse
from capabilities import tool_schema
import provider_client

_CATALOG = None  # built lazily/once


def _catalog():
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = tool_schema.build_tool_specs()
    return _CATALOG


@dataclass
class LoopOutcome:
    final_text: str
    rounds: int
    stop_reason: str
    replied_intermediate: bool


async def run_tool_loop(*, provider_config, build_messages, dispatch_tools, on_reply,
                        fold_new_messages, add_usage, max_calls: int) -> LoopOutcome:
    folded: list = []
    prior_results: list = []
    replied_intermediate = False
    rounds = 0
    for call_idx in range(max_calls):
        if call_idx > 0:
            folded.extend(fold_new_messages())   # per-round fold, no restart, no debounce
        messages = build_messages(folded, prior_results)
        last_call = call_idx == max_calls - 1
        tools = None if last_call else _catalog()
        result = await provider_client.chat_completion_async(provider_config, messages, tools=tools)
        add_usage(result.get("usage"))
        rounds += 1
        pr = ProviderResponse.from_result(result)
        if not pr.tool_calls:
            on_reply(pr.text, final=True)      # plain text IS the final reply (no responder)
            return LoopOutcome(pr.text, rounds, "final_text", replied_intermediate)
        # text accompanying tool_calls = preamble/thinking, NOT a bubble.
        reply_calls = [tc for tc in pr.tool_calls if tc.name == tool_schema.REPLY_TOOL]
        other_calls = [tc for tc in pr.tool_calls if tc.name != tool_schema.REPLY_TOOL]
        for tc in reply_calls:
            on_reply(str(tc.args.get("text") or ""), final=False)   # immediate intermediate bubble
            replied_intermediate = True
        if other_calls:
            prior_results = prior_results + list(await dispatch_tools(other_calls))
    # budget exhausted without a terminal: the last call had tools=None so pr had no tool_calls
    # and returned above; this line is only reached if max_calls==0.
    return LoopOutcome("", rounds, "budget_exhausted", replied_intermediate)
```

- [ ] **Step 4: Run — expect PASS.** `python -m pytest tests/test_v2_tool_loop.py tests/test_v2_dependency_direction.py -q`. Leave in working tree.

---

### Task 6: C7 — Per-round message fold via seq cursor (`worker` helper)

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py` (a `_make_fold_new_messages(user_id, deps, cursor_box) -> Callable[[], list[dict]]` closure + a `_build_turn_messages_fn` adapting `context.build_turn_messages`/responder-salvaged context to `(folded_inputs, prior_tool_results)`).
- Test: `tests/test_v2_fold_hook.py`

**Interfaces:**
- Consumes: `cursor.load_seq`, `db.chat_messages_after_seq`, `coalesce.coalesce_pending`.
- Produces: a `fold_new_messages()` callable that returns newly-visible user messages after the turn's advancing seq cursor (mutating `cursor_box` so each call only returns messages not seen before), and a `build_messages(folded_inputs, prior_tool_results)` callable for `run_tool_loop`.

- [ ] **Step 1: Failing test** `tests/test_v2_fold_hook.py` — seed a user with 1 message; build the fold closure; first `fold_new_messages()` returns that message and advances the cursor; append a 2nd message; second call returns ONLY the 2nd (no re-fold of the 1st = no dup); a 3rd call with no new messages returns `[]`. (P0 building block: no dup, no restart.)

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** the closures (use `db.chat_messages_after_seq(user_id, cursor_box["seq"])` filtered to user-role rows, advance `cursor_box["seq"]` to the max returned seq). Complete code in the task.

- [ ] **Step 4: Run — expect PASS.** Leave in working tree.

---

### Task 7: C6 + C9a — Wire the chat lane onto `run_tool_loop` (worker surgery)

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py` — replace `process_job`'s chat branch (`:933-1074`: the two-layer `while`/`replan`/`agent_loop.run_turn` + forced `responder.respond`) with: build the turn's `dispatch_tools` (Task-4 dispatcher bound to store/api_key/runtime_token/enclave_sem/`turn_authorization=True`/`enqueue_write_effect`), `on_reply` (enqueue reply effect via Task 1 with an incrementing ordinal + call `apply_pending_effects` immediately — C6), `fold_new_messages`/`build_messages` (Task 6), `add_usage=tm.add_call`; call `await tool_loop.run_tool_loop(...)`; then finalize (finish_chat_job, advance last_replied/cursor as effects, final `apply_pending_effects` drain, `tm.flush`). Pin `expected_generation = db.get_runtime_generation(user_id)` at turn start for all `enqueue_effect` calls.
- Test: `tests/test_v2_worker_tool_loop.py` (+ update/replace the existing chat-turn tests in `tests/test_v2_worker.py` that assert planner/responder call shape).

**Interfaces:**
- Consumes: Tasks 1-6. Produces: the live chat turn on the unified loop.

- [ ] **Step 1: Failing test** `tests/test_v2_worker_tool_loop.py` — a DB-backed chat job with a fake `chat_completion_async` scripting: round-1 plain text → assert exactly ONE reply bubble written (via the effect outbox → chat_messages), no planner/responder calls, `tm` has 1 model call, job completed. Then a two-round script (reply tool + web_search read → terminal text) → assert the intermediate bubble is visible before the terminal bubble, both via effects, and a re-drive re-enqueuing the same effect_ids produces NO duplicate bubbles (exactly-once).

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** the surgery. Show the full replacement block for `process_job`'s chat branch. Bind `enqueue_write_effect(tc)` to `effect_outbox.enqueue_effect(job_id=…, user_id=…, effect_type=<memory|identity|schedule per WRITE tool>, ordinal=next(ordinal), expected_generation=gen, payload=<mapped from tc.args>)`. `on_reply(text, final)` → `enqueue_effect(effect_type="reply", …, payload={"text":text})` then `deps.apply_pending_effects(user_id)`.

- [ ] **Step 4: Run — expect PASS.** Regression `python -m pytest tests/test_v2_worker.py -q`. Leave in working tree.

---

### Task 8: C8 — Migrate the wake lane onto `run_tool_loop`

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py` `_run_wake` (~:497) — replace its `planner.plan`/`responder.respond` calls with `run_tool_loop` using a wake-flavored `build_messages` (proactive open-the-conversation system prompt) and `turn_authorization=True` (wake_trigger authorizes writes; no user message required — remove the no-user-message error path).
- Test: `tests/test_v2_wake_tool_loop.py` (+ update existing wake tests).

- [ ] **Step 1: Failing test** — a wake job with a fake terminal plain-text response → exactly one proactive bubble via the effect outbox; a wake job whose model emits `memory_write` → the write is authorized (turn_authorization True from wake) and enqueued (NOT refused).
- [ ] **Step 2-4:** implement + run `python -m pytest tests/test_v2_wake_tool_loop.py tests/test_v2_worker.py -q`. Leave in working tree.

---

### Task 9: C9b — Delete the dispatch/planner/responder-hot-path/agent_loop machinery

**Files:**
- Modify/Delete: `backend/model_api_runtime/v2/planner.py` (remove `plan/rule_plan/official_plan/validate_plan/_PLANNER_SYSTEM/_parse_plan_json` — the whole is_official dispatch + JSON-plan protocol); `backend/model_api_runtime/v2/responder.py` (remove `respond` from the hot path; keep salvaged context helpers used by Task 6/7 as a small module or move them into `context.py`); `backend/model_api_runtime/v2/agent_loop.py` (delete or reduce to shared stop-reason constants if still referenced); `worker.py` (`TurnDeps.is_official` demoted to telemetry-only tag / removed; drop the `execute_plan`/`v2_inval` REPLAN machinery for chat/wake).
- Test: `tests/test_v2_no_dispatch_tiering.py` (NEW guard).

**Interfaces:** removes the old pipeline; the guard test locks in "no is_official→rule/official dispatch."

- [ ] **Step 1: Failing test** `tests/test_v2_no_dispatch_tiering.py` — AST/import guard (like `test_v2_dependency_direction.py`): assert `planner.plan`/`rule_plan`/`official_plan` no longer exist (or `planner` module gone), and that `worker.py` contains no `is_official`-branching call into a planner (grep the source for `official_plan`/`rule_plan` → zero). Assert `provider_client` tool loop is the sole turn driver.

- [ ] **Step 2-4:** delete, keep the salvaged context helpers wired, run `python -m pytest tests/test_v2_no_dispatch_tiering.py tests/test_v2_worker.py tests/test_v2_responder.py -q` (the responder test file may need trimming to only the salvaged helpers). Full suite check. Leave in working tree.

---

### Task 10: PR C P0 acceptance tests (the loop-behavior subset)

**Files:**
- Create: `tests/test_v2_p0_unified_loop.py`.

**Interfaces:** Consumes the full chat/wake loop (Tasks 1-9).

- [ ] **Step 1: P0 — weak model plain text** (may already be covered by Task 7): 1 provider call, 1 bubble, no dispatch tiering.
- [ ] **Step 2: P0 — `reply(我看看哈)` + web_search:** the intermediate bubble is written BEFORE the web_search round completes (assert ordering via effect drain timing / bubble seq), the next round continues, and a turn RE-DRIVE re-enqueues the same effect_ids → assert exactly one bubble per logical reply (no dup).
- [ ] **Step 3: P0 — mid-turn fold:** user B's message committed during round-1 tool dispatch → round-2's `build_messages` receives A + B + round-1 tool_results; assert no debounce sleep and no loop restart (round counter monotonic, prior_results preserved).
- [ ] **Step 4: P0 — malicious-page write refusal:** a turn seeded with `turn_authorization=False` (simulating a purely-external round) whose model emits `memory_write`/`identity_patch`/`schedule_wake` → the dispatcher returns refusal ToolResults, NO effect enqueued, NO durable write (assert `db.effect_pending` has no write effect + the capability write fn was never called).
- [ ] **Step 5: P0 — 4 providers live loop:** for each of the 4 wires, a scripted 2-tool-call round dispatches 2 reads and feeds back 2 ToolResults by call_id through the real loop.
- [ ] **Step 6: Full suite** `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py` → 8-baseline, zero new, single alembic head 0029 (PR C adds no migration). Leave in working tree; do NOT commit.

---

## Self-Review

- **Spec coverage:** C1→T3; C2→T5; C3→T4; C4→T2; C5→T1; C6→T7(on_reply+drain); C7→T6; C8→T8; C9→T7(surgery)+T9(deletions). P0 subset→T10. All nine components + P0s covered.
- **Ordering/deps:** 1(enqueue_effect)→2(provenance)→3(tool_schema)→4(dispatcher, needs 1/2)→5(loop, needs 3)→6(fold)→7(chat surgery, needs 1-6)→8(wake, needs 7)→9(deletions, after callers gone)→10(P0). Each independently testable; deletions LAST so nothing references removed symbols mid-flight.
- **Type consistency:** `enqueue_effect(*, job_id,user_id,effect_type,ordinal,expected_generation,payload)->str` identical T1/T7/T8; `dispatch_tool_calls(...)->list[ToolResult]` identical T4/T7; `run_tool_loop(*, provider_config,build_messages,dispatch_tools,on_reply,fold_new_messages,add_usage,max_calls)->LoopOutcome` identical T5/T7/T8; `write_gate(name,*,turn_authorization)->(bool,str)` identical T2/T4; ToolCall/ToolResult from provider_types throughout.
- **No behavior tiering:** T9 guard test locks it; T7/T8 pass no is_official into the loop.
- **NO-COMMIT:** every task leaves changes in the working tree.
- **Reused, not rebuilt:** executor `_run_one`/read-parallel/write-fence (T4), registry catalog (T3), PR A outbox (T1/T7), PR B transport (T5), coalesce/context (T6). extraction/compaction untouched (scope).
