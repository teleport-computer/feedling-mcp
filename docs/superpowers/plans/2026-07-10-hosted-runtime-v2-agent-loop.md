# Hosted Runtime V2 — Agent Loop Implementation Plan

> **STATUS: HISTORICAL / SUPERSEDED.** This plan produced an intermediate
> JSON-planner state machine that is no longer part of production V2. Current
> behavior lives in the unified provider-native `tool_loop.py` path.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the V2 hosted runtime a real agent loop (`decide → act → observe → decide`) built on multi-round `plan → execute`, without depending on any provider's native tool-calling wire protocol.

**Architecture:** A new pure state machine `v2/agent_loop.py` sits **above** `executor.py` and **below** `worker.py`. It takes two injected callbacks (`decide`, `run_tools`) and drives up to `_LOOP_MAX_ROUNDS` rounds. The existing message-driven replan loop stays as the OUTER loop; the new model-driven tool loop is the INNER loop; a single `_TURN_MAX_LLM_CALLS` budget is counted across both. `executor.py` and `capabilities/*` are not modified. The existing `final_response` plan sentinel becomes the loop's stop signal, which fixes BUG-4 by construction.

**Tech Stack:** Python 3.11, asyncio, pytest. Existing modules: `backend/model_api_runtime/v2/{planner,executor,responder,worker,invalidation,context}.py`, `scripts/loadtest/{mock_provider,compare_tokens}.py`.

**Spec:** `docs/superpowers/specs/2026-07-10-hosted-runtime-v2-agent-loop-design.md` (§12 decisions, §13 BUG-4)
**Parity matrix:** `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md` (§C turn shape, §E BUG-1/BUG-4)

## Global Constraints

- **NO-COMMIT.** Do not run `git commit` or `git add`. Do not run `git stash` / `git stash pop` (the stash stack is shared across worktrees and other live sessions — two near-misses already). Leave all work in the working tree; the user commits at the end.
- **Worktree:** all work happens in `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2` on branch `feat/hosted-runtime-v2`. Never touch the main checkout.
- **BYOK-only (hard invariant):** every LLM call — every planner round AND the responder — uses the user's own JIT-decrypted provider key. There is NO platform LLM key fallback, ever.
- **Single-decrypt-per-job:** `provider_config` is resolved once per turn by `_run_turn` and passed through. `process_job` must never call `deps.resolve_provider`.
- **no-filler:** only model-authored `final_response` text writes a chat bubble. Hitting the round cap, hitting the LLM-call budget, or detecting no-progress must NOT write a placeholder bubble — it forces a real responder call using whatever results are in hand.
- **Dependency direction** (AST-guarded by `tests/test_v2_dependency_direction.py`): `backend/model_api_runtime/v2/*` and `backend/capabilities/*` must NOT import `hosted` or `agent_runtime`. `agent_loop.py` additionally must not import `provider_client`, `jobs_store`, or any DB module — it is a pure state machine.
- `_LOOP_MAX_ROUNDS = 3`, `_TURN_MAX_LLM_CALLS = 6`. These are intentionally NOT consistent with `replan_budget(2) × 3 + 1 = 7` — the budget is meant to actually bind.
- **ENCLAVE_SEMAPHORE:** capability calls still pass through `executor.execute_plan`. The loop introduces no new enclave concurrency.
- **Test baseline:** 2640 passed / 7 pre-existing failures (`test_chat_route_debug_trace` ×3, `test_debug_trace_event_route`, `test_memory_capture_trace`, `test_model_api_path` verify-ping ×2), measured after Task 1. Postgres must be running on `127.0.0.1:55432` (`postgres`/`test`) or ~2000 DB tests are silently skipped and "all green" is a false signal (`pg_isready -h 127.0.0.1 -p 55432`).
- **The full-suite command is:**
  ```bash
  python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
  ```
  `tests/test_api.py` and `tests/e2e_model_api_test.py` are live-server integration **scripts**, not pytest suites — they issue a real HTTP request at import time and abort collection with `ConnectionError` unless a backend happens to be listening on `:5001`. `conftest.py`'s `collect_ignore` only excludes them on the no-Postgres path, so with Postgres up a bare `pytest tests/` errors out before running anything. The same two `--ignore` flags are required for any `-k`-filtered sweep. A run that ends in `Interrupted: 1 error during collection` has tested nothing — do not read it as a pass.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `scripts/loadtest/mock_provider.py` | Fake OpenAI-wire provider | **Modify** — optional request-derived token estimation + server-side token/request accumulators |
| `scripts/loadtest/compare_tokens.py` | tokens/turn measurement + rollback gate | **Modify** — measure a WHOLE turn (planner rounds + responder), not just responder |
| `backend/model_api_runtime/v2/responder.py` | Pure: (config, summary, tail, action_results) → text | **Modify** — `_fold_action_results` drops blob keys, caps per-action size |
| `backend/model_api_runtime/v2/planner.py` | Pure: turn inputs → action plan | **Modify** — drop `chat_image_read` from vocabulary; accept `prior_action_results` |
| `backend/model_api_runtime/v2/agent_loop.py` | **NEW.** Pure state machine: rounds, stop conditions, result accumulation | **Create** |
| `backend/model_api_runtime/v2/worker.py` | Assembly: wires planner/executor/responder into a turn | **Modify** — inner tool loop, cross-layer LLM budget, forced reply |
| `docs/HOSTED_RUNTIME_V2_TOKEN_BASELINE.md` | **NEW.** Recorded single-round tokens/turn baseline | **Create** (Task 3) |
| `tests/test_v2_agent_loop.py` | **NEW.** Pure loop tests with fake `decide`/`run_tools` | **Create** |

Unchanged, deliberately: `executor.py`, `capabilities/*`, `invalidation.py`, `context.py`. If a task's diff touches those, the task is wrong.

---

## Task 1: Stop the BUG-1 bleeding (planner vocabulary + responder fold)

BUG-1 (parity matrix §E): `chat_image_read` is in the planner vocabulary and returns raw `image_b64`. `_fold_action_results` copies `data` verbatim, `_action_context_str` does `json.dumps(folded)[:8000]` — so the model receives ~8000 chars of truncated base64 *instead of* the memory cards it also fetched. The loop would make this worse (more chances to pick it). Per spec §12 decision 2, we remove the action from the vocabulary now (making the bug unreachable) and harden the fold as defence-in-depth. Multimodal is its own later round, which re-adds the action.

**Files:**
- Modify: `backend/model_api_runtime/v2/planner.py:16-21` (`_READ_ACTIONS`), `:116-130` (`_PLANNER_SYSTEM`)
- Modify: `backend/model_api_runtime/v2/responder.py:48-69` (`_fold_action_results`)
- Test: `tests/test_v2_responder.py`, `tests/test_v2_planner.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `responder._BLOB_KEYS: frozenset[str]`, `responder._PER_ACTION_CHAR_CAP: int`, `responder._strip_blobs(value) -> Any`. `planner._READ_ACTIONS` no longer contains `"chat_image_read"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_v2_responder.py`:

```python
def test_fold_action_results_drops_image_blob():
    from model_api_runtime.v2 import responder
    action_results = {
        "chat_image_read": [{"ok": True, "data": {
            "message_id": "m1", "image_mime": "image/jpeg", "image_b64": "A" * 50000}}],
    }
    folded = responder._fold_action_results(action_results)
    assert folded["chat_image_read"]["message_id"] == "m1"
    assert folded["chat_image_read"]["image_mime"] == "image/jpeg"
    assert "image_b64" not in folded["chat_image_read"]


def test_fold_action_results_caps_a_single_oversized_action():
    from model_api_runtime.v2 import responder
    action_results = {
        "memory_fetch": [{"ok": True, "data": {"body": "B" * 50000}}],
        "perception_snapshot": [{"ok": True, "data": {"mood": "calm"}}],
    }
    folded = responder._fold_action_results(action_results)
    assert folded["memory_fetch"]["_truncated"] is True
    assert len(folded["memory_fetch"]["preview"]) <= responder._PER_ACTION_CHAR_CAP
    # The small action must survive intact — the point of the cap is that one
    # fat capability cannot evict the others from the 8000-char context budget.
    assert folded["perception_snapshot"] == {"mood": "calm"}
```

Append to `tests/test_v2_planner.py`:

```python
def test_chat_image_read_is_not_emittable():
    from model_api_runtime.v2 import planner
    assert "chat_image_read" not in planner._READ_ACTIONS
    assert "chat_image_read" not in planner._WRITE_ACTIONS
    assert "chat_image_read" not in planner._PLANNER_SYSTEM
    # A model that names it anyway gets it silently dropped (BUG-1 unreachable).
    steps = planner.validate_plan({"plan": [
        {"type": "chat_image_read", "payload": {"message_id": "m1"}},
        {"type": "final_response", "payload": {}},
    ]})
    assert steps == [{"type": "final_response", "payload": {}}]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python -m pytest tests/test_v2_responder.py::test_fold_action_results_drops_image_blob \
  tests/test_v2_responder.py::test_fold_action_results_caps_a_single_oversized_action \
  tests/test_v2_planner.py::test_chat_image_read_is_not_emittable -v
```

Expected: 3 FAIL — `AttributeError: module ... has no attribute '_PER_ACTION_CHAR_CAP'` and `assert 'chat_image_read' not in frozenset(...)`.

- [ ] **Step 3: Remove `chat_image_read` from the planner vocabulary**

In `backend/model_api_runtime/v2/planner.py`, change `_READ_ACTIONS` (currently lines 16-21) to:

```python
# 封闭动作词表（§4.3，NO recent_chat_digest——它不是 capability，digest 在 worker 确定性构建）。
# 词表外一律丢弃。final_response 是唯一可见/作者 action。
#
# chat_image_read 被**故意移出**词表（BUG-1，见 docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md §E）：
# 它返回原始 image_b64，会经 responder 的文本 grounding context 挤掉记忆卡/感知。多模态那一轮
# 会给 provider_client 真正的 image content block，届时把它加回来。
_READ_ACTIONS = frozenset({
    "identity_get", "memory_index", "memory_fetch", "memory_search",
    "perception_snapshot", "perception_trend", "perception_history",
    "screen_recent", "screen_read", "photo_recent", "photo_read",
    "web_search", "web_fetch",
})
```

In the same file, `_PLANNER_SYSTEM` (currently lines 116-130) — delete `chat_image_read, ` from the vocabulary sentence so it reads:

```python
    "photo_read, web_search, web_fetch, memory_write, identity_patch, "
```

- [ ] **Step 4: Harden `_fold_action_results`**

In `backend/model_api_runtime/v2/responder.py`, add after `_ACTION_CONTEXT_CHAR_CAP = 8000` (line 33):

```python
# 单个 action 的 grounding 上限。没有它，一个返回大 blob 的 capability 能把整个
# _ACTION_CONTEXT_CHAR_CAP 吃光，把同回合 fetch 到的记忆卡/感知全挤出 context（BUG-1 的
# 一般形式）。BLOB 键则直接丢——它们对文本 responder 永远没有意义。
_PER_ACTION_CHAR_CAP = 2000
_BLOB_KEYS = frozenset({"image_b64"})


def _strip_blobs(value: Any) -> Any:
    """递归剥掉 `_BLOB_KEYS`。dict/list 之外的值原样返回。"""
    if isinstance(value, dict):
        return {k: _strip_blobs(v) for k, v in value.items() if k not in _BLOB_KEYS}
    if isinstance(value, list):
        return [_strip_blobs(v) for v in value]
    return value
```

Then replace the body of `_fold_action_results` (lines 56-69) with:

```python
    ctx: dict[str, Any] = {}
    if not action_results:
        return ctx
    for action_type, runs in action_results.items():
        if not isinstance(runs, list):
            continue
        payloads = [
            _strip_blobs(r.get("data"))
            for r in runs
            if isinstance(r, dict) and r.get("ok") and r.get("data")
        ]
        if not payloads:
            continue
        folded = payloads if len(payloads) > 1 else payloads[0]
        rendered = json.dumps(folded, ensure_ascii=False)
        if len(rendered) > _PER_ACTION_CHAR_CAP:
            folded = {"_truncated": True, "preview": rendered[:_PER_ACTION_CHAR_CAP]}
        ctx[action_type] = folded
    return ctx
```

Extend the docstring's second paragraph to say: blob keys are dropped and any single action whose serialized form exceeds `_PER_ACTION_CHAR_CAP` is replaced by a truncated preview, so no one capability can evict the others.

- [ ] **Step 5: Run the tests to verify they pass**

```bash
python -m pytest tests/test_v2_responder.py tests/test_v2_planner.py -q
```

Expected: PASS, no regressions in those two files.

- [ ] **Step 6: Run the V2 + capabilities regression sweep**

```bash
python -m pytest tests/ -q -k "v2 or capabilit" --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: no new failures versus the baseline.

- [ ] **Step 7: Do NOT commit.** Leave the changes in the working tree (Global Constraints).

---

## Task 2: Make the token gate measure something

`MockProvider` reports a **fixed** `usage.prompt_tokens` no matter what request body it receives (`mock_provider.py:52-53`), so `compare_tokens.measure_v2_tokens_per_turn` returns a constant — it cannot detect prompt growth. It also drives only `responder.respond`, never the planner. Both defects make the D4 rollback gate (`compare_tokens.py --resident-baseline`) meaningless for a change whose entire risk is "the loop calls the planner more times with a bigger prompt". Fix the instrument before taking the measurement.

**Files:**
- Modify: `scripts/loadtest/mock_provider.py`
- Test: `tests/test_loadtest_harness_smoke.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `MockProvider(..., estimate_tokens: bool = False)` — when True, `usage.prompt_tokens` is derived from the actual request body and `usage.completion_tokens` from the actual reply, instead of the fixed values.
  - `MockProvider.total_prompt_tokens: int`, `.total_completion_tokens: int`, `.request_count: int` — server-side accumulators over every request served, thread-safe. These let a caller measure a whole turn's token cost across an arbitrary number of LLM calls made by arbitrary code.
  - `mock_provider.estimate_tokens_from_text(text: str) -> int`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_loadtest_harness_smoke.py`:

```python
def test_mock_provider_estimates_tokens_from_request_and_accumulates():
    import json
    import urllib.request
    from scripts.loadtest.mock_provider import MockProvider, estimate_tokens_from_text

    long_prompt = "x" * 4000
    with MockProvider(reply="ok", estimate_tokens=True) as p:
        def _post(content: str) -> dict:
            req = urllib.request.Request(
                f"{p.base_url}/chat/completions",
                data=json.dumps({"messages": [{"role": "user", "content": content}]}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())

        small = _post("hi")
        large = _post(long_prompt)

        # A bigger prompt MUST cost more prompt_tokens — the whole point of the gate.
        assert large["usage"]["prompt_tokens"] > small["usage"]["prompt_tokens"]
        assert large["usage"]["prompt_tokens"] == estimate_tokens_from_text(long_prompt)
        assert small["usage"]["completion_tokens"] == estimate_tokens_from_text("ok")

        # Server-side accumulators see EVERY call, whoever made it.
        assert p.request_count == 2
        assert p.total_prompt_tokens == (
            small["usage"]["prompt_tokens"] + large["usage"]["prompt_tokens"])


def test_mock_provider_fixed_tokens_remains_the_default():
    import json
    import urllib.request
    from scripts.loadtest.mock_provider import MockProvider

    with MockProvider(reply="ok", prompt_tokens=100, completion_tokens=20) as p:
        req = urllib.request.Request(
            f"{p.base_url}/chat/completions",
            data=json.dumps({"messages": [{"role": "user", "content": "x" * 4000}]}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as r:
            body = json.loads(r.read())
    assert body["usage"]["prompt_tokens"] == 100
    assert body["usage"]["completion_tokens"] == 20
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python -m pytest tests/test_loadtest_harness_smoke.py -q -k "estimates_tokens or fixed_tokens"
```

Expected: FAIL — `ImportError: cannot import name 'estimate_tokens_from_text'`.

- [ ] **Step 3: Implement estimation + accumulators**

In `scripts/loadtest/mock_provider.py`, add near the top (after the existing `DEFAULT_*` constants):

```python
import threading

# 4 chars/token 是所有主流 BPE 分词器在混合中英文本上的常用粗估。我们只需要一个对
# prompt 长度**单调**的量：token 门比较的是"循环前 vs 循环后"的比值，不是绝对 token 数。
_CHARS_PER_TOKEN = 4


def estimate_tokens_from_text(text: str) -> int:
    """粗估 token 数。对长度单调、恒 >= 1（空串也算一个 token 的开销）。"""
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _estimate_prompt_tokens(payload: dict) -> int:
    parts = [
        str(m.get("content") or "")
        for m in (payload.get("messages") or [])
        if isinstance(m, dict)
    ]
    return estimate_tokens_from_text("".join(parts))
```

In `MockProvider.__init__`, add the parameter and the accumulator state:

```python
        estimate_tokens: bool = False,
```
```python
        self.estimate_tokens = estimate_tokens
        self._lock = threading.Lock()
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.request_count = 0
```

In the request handler, replace the two lines that currently read
`prompt_tokens = provider.prompt_tokens` / `completion_tokens = provider.completion_tokens`
(lines 52-53) with:

```python
            if provider.estimate_tokens:
                prompt_tokens = _estimate_prompt_tokens(payload)
                completion_tokens = estimate_tokens_from_text(provider.reply)
            else:
                prompt_tokens = provider.prompt_tokens
                completion_tokens = provider.completion_tokens
            # 服务端累加器：无论调用方是 planner 的第 N 轮还是 responder，每一次
            # provider 调用都被计入。这是"整回合 token"唯一可靠的观测点——它不依赖
            # 调用方自报 usage，也不需要知道一个回合内到底发生了几次 LLM 调用。
            with provider._lock:
                provider.total_prompt_tokens += prompt_tokens
                provider.total_completion_tokens += completion_tokens
                provider.request_count += 1
```

(`payload` is the already-parsed request JSON in that handler. If it is bound under another name, use that name.)

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest tests/test_loadtest_harness_smoke.py -q
```

Expected: PASS, including the 9 pre-existing mock-provider tests (the fixed-token default is unchanged).

- [ ] **Step 5: Do NOT commit.**

---

## Task 3: Measure and record the single-round tokens/turn baseline

Per spec §12 decision 3: the loop necessarily raises mean tokens/turn, and D4's rollback gate needs a reference point taken **before** the loop exists. Measure a whole turn (planner + responder) through the real modules against the now-honest mock, and record the number.

**Files:**
- Modify: `scripts/loadtest/compare_tokens.py`
- Create: `docs/HOSTED_RUNTIME_V2_TOKEN_BASELINE.md`
- Test: `tests/test_loadtest_compare.py`

**Interfaces:**
- Consumes: `mock_provider.MockProvider(estimate_tokens=True)` and its `.total_prompt_tokens` / `.total_completion_tokens` / `.request_count` accumulators (Task 2). `planner._READ_ACTIONS` without `chat_image_read` (Task 1).
- Produces: `compare_tokens.measure_turn_tokens(fixtures, *, provider) -> dict` returning `{"tokens_per_turn": float, "llm_calls_per_turn": float}`. Later tasks re-run this and compare against the recorded baseline.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_loadtest_compare.py`:

```python
def test_measure_turn_tokens_counts_planner_and_responder():
    from scripts.loadtest.compare_tokens import measure_turn_tokens
    from scripts.loadtest.mock_provider import MockProvider

    # The mock must answer the planner with parseable JSON; responder gets the
    # same string back, which is fine — we are counting tokens, not reading prose.
    plan_json = '{"plan":[{"type":"final_response","payload":{}}],"reason":"t"}'
    fixtures = [{"summary": "", "tail": [{"role": "user", "content": "hello"}]}]

    with MockProvider(reply=plan_json, estimate_tokens=True) as p:
        report = measure_turn_tokens(fixtures, provider=p)

    # One planner call + one responder call per turn, today (pre-loop).
    assert report["llm_calls_per_turn"] == 2.0
    assert report["tokens_per_turn"] > 0
    assert report["tokens_per_turn"] == (
        p.total_prompt_tokens + p.total_completion_tokens) / len(fixtures)


def test_measure_turn_tokens_grows_with_prompt_size():
    from scripts.loadtest.compare_tokens import measure_turn_tokens
    from scripts.loadtest.mock_provider import MockProvider

    plan_json = '{"plan":[{"type":"final_response","payload":{}}],"reason":"t"}'
    small = [{"summary": "", "tail": [{"role": "user", "content": "hi"}]}]
    large = [{"summary": "S" * 8000, "tail": [{"role": "user", "content": "hi"}]}]

    with MockProvider(reply=plan_json, estimate_tokens=True) as p:
        small_report = measure_turn_tokens(small, provider=p)
    with MockProvider(reply=plan_json, estimate_tokens=True) as p:
        large_report = measure_turn_tokens(large, provider=p)

    assert large_report["tokens_per_turn"] > small_report["tokens_per_turn"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python -m pytest tests/test_loadtest_compare.py -q -k "measure_turn_tokens"
```

Expected: FAIL — `ImportError: cannot import name 'measure_turn_tokens'`.

- [ ] **Step 3: Implement `measure_turn_tokens`**

Add to `scripts/loadtest/compare_tokens.py` (keep the existing `measure_v2_tokens_per_turn` — `run_loadtest.py` and the CLI still call it):

```python
from model_api_runtime.v2 import planner as v2_planner


async def _drive_turn_async(provider_config, fixture: dict[str, Any]) -> None:
    """跑一个**完整回合**的 LLM 调用序列：planner（可能多轮）+ responder。

    这是 token 门唯一正确的观测口径。老的 `measure_v2_tokens_per_turn` 只跑 responder，
    因此对"循环让 planner 多跑几轮"这件事完全失明——而那恰恰是本次改动的全部风险。
    token 计数不在这里做：由 MockProvider 的服务端累加器统计，它看得见每一次调用，
    无论调用方是谁、调了几次。
    """
    tail = list(fixture.get("tail") or [])
    coalesced = [m for m in tail if m.get("role") == "user"]
    await v2_planner.plan(
        None,
        provider_config=provider_config, is_official=True,
        coalesced_messages=coalesced,
        digest={"messages": [{"content": str(m.get("content") or "")[:400]} for m in coalesced[-6:]]},
        memory_index={}, perception_summary={}, runtime_state={},
        lane="chat", reason="loadtest",
    )
    await v2_responder.respond(
        provider_config=provider_config,
        summary=str(fixture.get("summary") or ""),
        tail=tail,
    )


def measure_turn_tokens(fixtures: list[dict[str, Any]], *, provider) -> dict[str, Any]:
    """把每个 fixture 当成一个完整回合跑过 planner+responder，返回每回合的 token 与
    LLM 调用次数均值。`provider` 是一个已启动的 `MockProvider(estimate_tokens=True)`。

    fixtures 为空 → 抛（均值无定义）。
    """
    if not fixtures:
        raise ValueError("measure_turn_tokens requires at least one fixture")
    provider_config = provider_client.ProviderConfig(
        provider="openai_compatible", model="loadtest-mock",
        api_key="mock-key", base_url=provider.base_url,
    )

    async def _run() -> None:
        for fixture in fixtures:
            await _drive_turn_async(provider_config, fixture)

    asyncio.run(_run())
    turns = float(len(fixtures))
    total = provider.total_prompt_tokens + provider.total_completion_tokens
    return {
        "tokens_per_turn": total / turns,
        "llm_calls_per_turn": provider.request_count / turns,
    }
```

`v2_planner.plan` takes `store` positionally but only uses it for the signature — `official_plan` never touches it. Passing `None` is correct and keeps the harness DB-free.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest tests/test_loadtest_compare.py -q
```

Expected: PASS, including the 9 pre-existing compare tests.

- [ ] **Step 5: Take the baseline measurement**

```bash
python - <<'PY'
import json, sys
sys.path.insert(0, "backend"); sys.path.insert(0, ".")
from scripts.loadtest.compare_tokens import measure_turn_tokens
from scripts.loadtest.mock_provider import MockProvider

plan_json = '{"plan":[{"type":"memory_fetch","payload":{"ids":["a"]}},{"type":"final_response","payload":{}}],"reason":"baseline"}'
fixtures = [
    {"summary": "", "tail": [{"role": "user", "content": "今天过得怎么样"}]},
    {"summary": "早前聊到他在换工作。" * 20,
     "tail": [{"role": "user", "content": "我还是有点焦虑"},
              {"role": "assistant", "content": "嗯，说说看"},
              {"role": "user", "content": "面试没过"}]},
    {"summary": "S" * 2000,
     "tail": [{"role": "user", "content": "帮我回忆一下上周说的那个计划"}]},
]
with MockProvider(reply=plan_json, estimate_tokens=True) as p:
    print(json.dumps(measure_turn_tokens(fixtures, provider=p), indent=2))
PY
```

Record the exact printed numbers. Do not invent them.

- [ ] **Step 6: Write `docs/HOSTED_RUNTIME_V2_TOKEN_BASELINE.md`**

```markdown
# Hosted Runtime V2 — tokens/turn baseline (pre-agent-loop)

Taken on <YYYY-MM-DD>, at commit <git rev-parse --short HEAD>, BEFORE `agent_loop.py` existed.
Purpose: give D4's rollback gate (`scripts/loadtest/compare_tokens.py`) a reference point,
per the agent-loop spec §12 decision 3.

## Method

`scripts/loadtest/compare_tokens.measure_turn_tokens` drives the REAL
`planner.plan` + `responder.respond` for each fixture against
`MockProvider(estimate_tokens=True)`, and reads the mock's server-side
accumulators. Token counts are a 4-chars-per-token estimate — **relative**,
not absolute. The gate compares ratios, so this is sufficient and provider-independent.

Three fixtures: a bare one-liner, a mid-length turn with a real summary + 3-message tail,
and a long-summary turn. Exact fixture bodies are in this plan, Task 3 Step 5.

## Result (single-round `plan → execute → reply`)

| metric | value |
|---|---|
| `tokens_per_turn` | <paste> |
| `llm_calls_per_turn` | <paste — expect 2.0> |

## How to re-measure after the loop lands

Run the same snippet (Task 3 Step 5). `llm_calls_per_turn` will exceed 2.0 whenever the
planner asks for a second round. The rollback gate is `tokens_per_turn` growth > +10%
versus a *resident* baseline — this file is the *V2 single-round* reference, which is what
tells you whether a regression came from the loop or from somewhere else.
```

Fill in the date, the short SHA (`git rev-parse --short HEAD`), and the two measured numbers.

- [ ] **Step 7: Do NOT commit.**

---

## Task 4: The pure loop — `agent_loop.py`

**Files:**
- Create: `backend/model_api_runtime/v2/agent_loop.py`
- Test: `tests/test_v2_agent_loop.py`

**Interfaces:**
- Consumes: nothing (pure; stdlib only).
- Produces:
  - `agent_loop.DEFAULT_MAX_ROUNDS: int = 3`
  - `agent_loop.Decision(actions: list[dict], wants_reply: bool = False, final_text: str | None = None)`
  - `agent_loop.LoopResult(action_results: dict, action_digest: dict, final_text: str | None, rounds: int, stop_reason: str)`
  - `async agent_loop.run_turn(*, decide, run_tools, max_rounds=DEFAULT_MAX_ROUNDS) -> LoopResult`
    - `decide(round_idx: int, prior_results: dict) -> Awaitable[Decision]`
    - `run_tools(actions: list[dict]) -> Awaitable[dict]` returning `{"action_results": {type: [result,...]}, "action_digest": {type: {...}}}` (same shape as `executor.execute_plan`)
  - `stop_reason ∈ {"wants_reply", "no_actions", "no_progress", "max_rounds"}`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_v2_agent_loop.py`:

```python
"""Pure agent-loop tests: fake `decide`/`run_tools`, no DB, no provider, no store."""
import pytest

from model_api_runtime.v2 import agent_loop


def _ok(action_type: str, data):
    return {"action_results": {action_type: [{"ok": True, "data": data}]},
            "action_digest": {action_type: {"ok": True, "count": 1}}}


@pytest.mark.asyncio
async def test_single_round_when_planner_wants_reply_immediately():
    calls = []

    async def decide(round_idx, prior):
        calls.append((round_idx, dict(prior)))
        return agent_loop.Decision(actions=[{"type": "memory_fetch", "payload": {"ids": ["a"]}}],
                                   wants_reply=True)

    async def run_tools(actions):
        return _ok("memory_fetch", {"body": "card"})

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools)
    assert res.stop_reason == "wants_reply"
    assert res.rounds == 1
    # wants_reply does NOT skip this round's tools — the plan was [fetch, final_response].
    assert res.action_results["memory_fetch"][0]["data"] == {"body": "card"}
    assert res.action_digest["memory_fetch"]["count"] == 1
    assert calls[0][1] == {}  # first round sees no prior results


@pytest.mark.asyncio
async def test_second_round_sees_first_round_results_and_accumulates():
    seen = []

    async def decide(round_idx, prior):
        seen.append(dict(prior))
        if round_idx == 0:
            return agent_loop.Decision(actions=[{"type": "memory_index", "payload": {}}])
        return agent_loop.Decision(actions=[{"type": "memory_fetch", "payload": {"ids": ["a"]}}],
                                   wants_reply=True)

    async def run_tools(actions):
        t = actions[0]["type"]
        return _ok(t, {"t": t})

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools)
    assert res.rounds == 2
    assert res.stop_reason == "wants_reply"
    assert seen[1]["memory_index"][0]["data"] == {"t": "memory_index"}   # observation fed back
    assert set(res.action_results) == {"memory_index", "memory_fetch"}   # accumulated, not replaced


@pytest.mark.asyncio
async def test_hits_max_rounds_and_stops_without_final_text():
    rounds = []

    async def decide(round_idx, prior):
        rounds.append(round_idx)
        return agent_loop.Decision(actions=[{"type": "web_search", "payload": {"q": str(round_idx)}}])

    async def run_tools(actions):
        return _ok("web_search", {"hit": actions[0]["payload"]["q"]})

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools, max_rounds=3)
    assert rounds == [0, 1, 2]
    assert res.rounds == 3
    assert res.stop_reason == "max_rounds"
    assert res.final_text is None   # caller must force a responder call — never a filler bubble


@pytest.mark.asyncio
async def test_identical_plan_twice_stops_as_no_progress():
    async def decide(round_idx, prior):
        return agent_loop.Decision(actions=[{"type": "memory_index", "payload": {"k": 1}}])

    async def run_tools(actions):
        return _ok("memory_index", {"items": []})

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools, max_rounds=5)
    assert res.stop_reason == "no_progress"
    assert res.rounds == 2   # round 0 ran; round 1 repeated the same signature and stopped


@pytest.mark.asyncio
async def test_all_actions_failing_stops_as_no_progress():
    """A planner that keeps asking for a tool that keeps erroring must not burn the BYOK key."""
    async def decide(round_idx, prior):
        return agent_loop.Decision(actions=[{"type": "web_fetch", "payload": {"url": str(round_idx)}}])

    async def run_tools(actions):
        return {"action_results": {"web_fetch": [{"ok": False, "error": "boom"}]},
                "action_digest": {"web_fetch": {"ok": False, "count": 1}}}

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools, max_rounds=5)
    assert res.stop_reason == "no_progress"
    assert res.rounds == 1
    assert res.action_results["web_fetch"][0]["ok"] is False   # failures still visible to responder


@pytest.mark.asyncio
async def test_empty_plan_stops_immediately_and_never_calls_run_tools():
    ran = False

    async def decide(round_idx, prior):
        return agent_loop.Decision(actions=[])

    async def run_tools(actions):
        nonlocal ran
        ran = True
        return _ok("x", {})

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools)
    assert res.stop_reason == "no_actions"
    assert res.rounds == 1
    assert res.action_results == {}
    assert ran is False


@pytest.mark.asyncio
async def test_final_text_from_decide_is_returned_verbatim():
    """The native-tools seam: a backend that authors the reply while stopping."""
    async def decide(round_idx, prior):
        return agent_loop.Decision(actions=[], wants_reply=True, final_text="hi there")

    async def run_tools(actions):
        raise AssertionError("must not run tools for an empty plan")

    res = await agent_loop.run_turn(decide=decide, run_tools=run_tools)
    assert res.final_text == "hi there"
    assert res.stop_reason == "wants_reply"


def test_agent_loop_is_pure():
    """Dependency direction: the loop must not reach for DB, provider, or hosted."""
    import pathlib
    src = pathlib.Path(agent_loop.__file__).read_text()
    for forbidden in ("provider_client", "jobs_store", "import hosted", "from hosted",
                      "agent_runtime", "core.store", "psycopg"):
        assert forbidden not in src, f"agent_loop.py must not reference {forbidden}"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python -m pytest tests/test_v2_agent_loop.py -q
```

Expected: FAIL — `ModuleNotFoundError: No module named 'model_api_runtime.v2.agent_loop'`.

- [ ] **Step 3: Implement `agent_loop.py`**

Create `backend/model_api_runtime/v2/agent_loop.py`:

```python
"""V2 agent loop：`decide → act → observe → decide` 的**纯**状态机。

设计见 docs/superpowers/specs/2026-07-10-hosted-runtime-v2-agent-loop-design.md。

为什么在这里而不在 executor 里：executor 是无状态批量调度器，不认识模型、不持有 BYOK
key、不拼 wire。把循环塞进去等于把 V2 花力气拆开的「决定」与「执行」重新焊死。为什么不在
responder 里：responder 必须保持纯（无副作用），而工具里有写操作（memory_write /
identity_patch）——让"写回复的模块"顺手改用户的记忆是错的。

本模块只依赖 stdlib。两个注入回调：
  decide(round_idx, prior_results) -> Decision
  run_tools(actions)               -> {"action_results": {...}, "action_digest": {...}}

停止条件（四种，全部交给调用方决定要不要强制回复——本模块**绝不**产出占位文本）：
  wants_reply  planner 发了 final_response 哨兵：收手去回复
  no_actions   planner 什么也不要：同样收手
  no_progress  本轮 plan 与上轮逐字相同，或本轮工具**全部**失败：别再烧用户的 key
  max_rounds   撞轮数上限
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

DEFAULT_MAX_ROUNDS = 3

WANTS_REPLY = "wants_reply"
NO_ACTIONS = "no_actions"
NO_PROGRESS = "no_progress"
MAX_ROUNDS = "max_rounds"


@dataclass
class Decision:
    """一轮 decide 的产出。

    `final_text` 是给**原生 tool-calling 后端**留的缝：那种后端里，停止发工具的那个模型
    顺手就把回复写了；不接住它就得丢掉这次生成再让 responder 重写一遍，白烧一次 token。
    默认的 json_planner 后端恒为 None（散文由 responder 写，见 spec §4）。
    """

    actions: list[dict[str, Any]] = field(default_factory=list)
    wants_reply: bool = False
    final_text: str | None = None


@dataclass
class LoopResult:
    action_results: dict[str, list[dict[str, Any]]]
    action_digest: dict[str, Any]
    final_text: str | None
    rounds: int
    stop_reason: str


def _signature(actions: list[dict[str, Any]]) -> frozenset:
    """plan 的顺序无关指纹。payload 用 sort_keys 序列化，键序不同不算"变了"。"""
    return frozenset(
        (str(a.get("type") or ""), json.dumps(a.get("payload") or {}, sort_keys=True, ensure_ascii=False))
        for a in actions
    )


def _merge_results(acc: dict, new: dict) -> None:
    for action_type, runs in (new or {}).items():
        acc.setdefault(action_type, []).extend(runs or [])


def _any_ok(results: dict) -> bool:
    return any(
        isinstance(r, dict) and r.get("ok")
        for runs in (results or {}).values()
        for r in (runs or [])
    )


async def run_turn(
    *,
    decide: Callable[[int, dict], Awaitable[Decision]],
    run_tools: Callable[[list[dict]], Awaitable[dict]],
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> LoopResult:
    """驱动至多 `max_rounds` 轮 decide→act。返回累积结果 + 停止原因。

    **绝不**因为撞上限或无进展而产出文本——调用方负责用手上的结果强制一次真正的 responder
    调用（no-filler 铁律）。
    """
    acc_results: dict[str, list[dict[str, Any]]] = {}
    acc_digest: dict[str, Any] = {}
    prev_sig: frozenset | None = None

    for round_idx in range(max_rounds):
        decision = await decide(round_idx, acc_results)

        # 无进展检测在**跑工具之前**：同一个 plan 再跑一遍不会有新观测，只会白烧一轮。
        # wants_reply 的那一轮豁免——它带的 action 是收手前最后一批，不是空转。
        if decision.actions and not decision.wants_reply:
            sig = _signature(decision.actions)
            if sig == prev_sig:
                return LoopResult(acc_results, acc_digest, None, round_idx + 1, NO_PROGRESS)
            prev_sig = sig

        round_results: dict = {}
        if decision.actions:
            executed = await run_tools(decision.actions)
            round_results = (executed or {}).get("action_results") or {}
            _merge_results(acc_results, round_results)
            acc_digest.update((executed or {}).get("action_digest") or {})

        if decision.wants_reply:
            return LoopResult(acc_results, acc_digest, decision.final_text, round_idx + 1, WANTS_REPLY)
        if not decision.actions:
            return LoopResult(acc_results, acc_digest, decision.final_text, round_idx + 1, NO_ACTIONS)
        if not _any_ok(round_results):
            # 本轮工具全挂：再规划一轮也是拿着同样的空手，停。失败结果照样留给 responder，
            # 让它知道"查过了但没查到"，而不是凭空回答。
            return LoopResult(acc_results, acc_digest, None, round_idx + 1, NO_PROGRESS)

    return LoopResult(acc_results, acc_digest, None, max_rounds, MAX_ROUNDS)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest tests/test_v2_agent_loop.py -q
```

Expected: 8 passed.

- [ ] **Step 5: Run the dependency-direction guard**

```bash
python -m pytest tests/test_v2_dependency_direction.py -q
```

Expected: PASS — `agent_loop.py` imports nothing outside stdlib.

- [ ] **Step 6: Do NOT commit.**

---

## Task 5: Feed prior round results back into the planner

Without this, round 2's planner is identical to round 1's and the `no_progress` guard fires immediately — the loop would be structurally present but semantically dead.

**Files:**
- Modify: `backend/model_api_runtime/v2/planner.py` (`plan`, `official_plan`, `_planner_user_payload`, `_PLANNER_SYSTEM`)
- Test: `tests/test_v2_planner.py`

**Interfaces:**
- Consumes: `agent_loop`'s `prior_results` shape — `{action_type: [{"ok": bool, "data": ...}, ...]}`.
- Produces:
  - `planner.plan(store, *, ..., prior_action_results: dict | None = None)` — keyword-only, defaults to `None` so every existing call site is unchanged.
  - `planner.official_plan(*, ..., prior_action_results: dict | None = None)`
  - `planner._compact_prior(prior_action_results) -> dict` — `{action_type: {"ok_count": int, "fail_count": int, "preview": str}}`, each preview ≤ `_PRIOR_PREVIEW_CHARS`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_v2_planner.py`:

```python
def test_compact_prior_summarises_and_truncates():
    from model_api_runtime.v2 import planner
    prior = {
        "memory_index": [{"ok": True, "data": {"items": [{"id": "a"}]}}],
        "web_fetch": [{"ok": False, "error": "timeout"},
                      {"ok": True, "data": {"text": "Z" * 5000}}],
    }
    out = planner._compact_prior(prior)
    assert out["memory_index"] == {"ok_count": 1, "fail_count": 0,
                                   "preview": '{"items": [{"id": "a"}]}'}
    assert out["web_fetch"]["ok_count"] == 1
    assert out["web_fetch"]["fail_count"] == 1
    assert len(out["web_fetch"]["preview"]) <= planner._PRIOR_PREVIEW_CHARS


def test_compact_prior_of_none_is_empty():
    from model_api_runtime.v2 import planner
    assert planner._compact_prior(None) == {}
    assert planner._compact_prior({}) == {}


def test_planner_user_payload_carries_prior_results():
    from model_api_runtime.v2 import planner
    payload = planner._planner_user_payload(
        coalesced_messages=[{"content": "hi"}], digest={}, memory_index={},
        perception_summary={}, runtime_state={}, lane="chat", reason="r",
        prior_action_results={"memory_index": [{"ok": True, "data": {"items": []}}]},
    )
    assert payload["prior_action_results"]["memory_index"]["ok_count"] == 1


def test_planner_user_payload_omits_prior_key_on_first_round():
    from model_api_runtime.v2 import planner
    payload = planner._planner_user_payload(
        coalesced_messages=[{"content": "hi"}], digest={}, memory_index={},
        perception_summary={}, runtime_state={}, lane="chat", reason="r",
        prior_action_results=None,
    )
    # First round must not carry a dead key — it costs tokens on every single turn.
    assert "prior_action_results" not in payload
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python -m pytest tests/test_v2_planner.py -q -k "prior"
```

Expected: FAIL — `AttributeError: module ... has no attribute '_compact_prior'`.

- [ ] **Step 3: Implement `_compact_prior` and thread the parameter**

In `backend/model_api_runtime/v2/planner.py`, add after `MAX_PLAN_ACTIONS = 5`:

```python
# 喂回 planner 的上轮结果预览上限。别喂原始 data——那会把 planner 的 prompt 撑成
# responder 的 grounding context，两轮就爆。planner 只需要知道「查到了什么量级的东西」
# 来决定还要不要再查。
_PRIOR_PREVIEW_CHARS = 600


def _compact_prior(prior_action_results: dict[str, Any] | None) -> dict[str, Any]:
    """把上一轮的 action 结果压成 planner 可读的极简摘要：成败计数 + 截断预览。"""
    out: dict[str, Any] = {}
    for action_type, runs in (prior_action_results or {}).items():
        if not isinstance(runs, list):
            continue
        ok = [r for r in runs if isinstance(r, dict) and r.get("ok")]
        fail = [r for r in runs if isinstance(r, dict) and not r.get("ok")]
        first_data = ok[0].get("data") if ok else None
        preview = json.dumps(first_data, ensure_ascii=False)[:_PRIOR_PREVIEW_CHARS] if first_data else ""
        out[action_type] = {"ok_count": len(ok), "fail_count": len(fail), "preview": preview}
    return out
```

Change `_planner_user_payload` to accept and conditionally include the key:

```python
def _planner_user_payload(
    *, coalesced_messages, digest, memory_index, perception_summary, runtime_state, lane, reason,
    prior_action_results=None,
) -> dict:
    payload = {
        "lane": lane,
        "reason": reason,
        "messages": [{"content": str(m.get("content") or "")[:2000]} for m in coalesced_messages[-8:]],
        "recent_chat_digest": digest,
        "memory_index": memory_index,
        "perception_summary": perception_summary,
        "runtime_state": runtime_state or {},   # 只含非敏感 digest（无 provider 三元组、无 key）
    }
    compact = _compact_prior(prior_action_results)
    if compact:
        payload["prior_action_results"] = compact
    return payload
```

Add `prior_action_results: dict | None = None` as a keyword-only parameter to both `plan(...)` and `official_plan(...)`; `plan` forwards it to `official_plan`, and `official_plan` forwards it to `_planner_user_payload`. `rule_plan` ignores it (the deterministic planner does not learn from results — this is spec §8 loss 3, and it means weak-model users effectively still run one round).

Extend `_PLANNER_SYSTEM` — append to the Rules sentence, before `"Never wrap the JSON in Markdown."`:

```python
    "If `prior_action_results` is present, it holds what THIS turn's earlier tool rounds "
    "already returned: request more actions only if they are still missing something, "
    "otherwise include final_response now. You get at most 3 rounds. "
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
python -m pytest tests/test_v2_planner.py -q
```

Expected: PASS, including the pre-existing planner tests (every existing call site omits the new kwarg).

- [ ] **Step 5: Do NOT commit.**

---

## Task 6: Wire the loop into the worker (and fix BUG-4)

This is the integration task. The existing `while True:` replan loop (`worker.py:412-456`) becomes the OUTER loop; `agent_loop.run_turn` becomes the INNER loop; one `_TURN_MAX_LLM_CALLS` counter spans both. The `before_final_response` safe point still evaluates once per outer iteration, after the inner loop settles — preserving today's replan semantics exactly.

**BUG-4** (parity matrix §E): today `wants_reply = any(s["type"] == "final_response" for s in steps)` (`worker.py:458`), and when the trusted model's plan omits `final_response`, the responder is skipped and the job completes with **no chat bubble** — the user's message is silently swallowed. Under the loop, "no `final_response`" means "loop again", and a chat-lane turn always ends in a real responder call. There is no separate patch; the invariant is `lane == "chat"` ⇒ a bubble or a `mark_failed`.

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py` (imports, module constants, `process_job` lines ~409-459)
- Test: `tests/test_v2_worker.py`

**Interfaces:**
- Consumes: `agent_loop.run_turn` / `agent_loop.Decision` / `agent_loop.LoopResult` (Task 4); `planner.plan(..., prior_action_results=...)` (Task 5); `responder.respond(..., action_results=..., usage_out=...)` (unchanged).
- Produces: `worker._LOOP_MAX_ROUNDS: int`, `worker._TURN_MAX_LLM_CALLS: int`. No change to `process_job`'s signature or its `"completed"` / `"failed"` return contract.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_v2_worker.py`, following that file's existing conventions exactly:
- There are **no** `chat_job` / `deps` pytest fixtures. Each test seeds a user, enqueues + claims a
  real job, and builds `deps` via the module-local `_deps(...)` helper.
- Tests are **synchronous** and call `asyncio.run(worker.process_job(...))`. Do not add
  `@pytest.mark.asyncio` here.
- `_patch_cheap_boundaries(monkeypatch, reply=...)` stubs `cap_registry.run_capability` (returning
  `ok=True`) and `v2_responder.respond`. **This means the REAL `executor.execute_plan` runs and every
  action succeeds** — so the loop's `_any_ok` guard is satisfied and no executor stub is needed.
  Add `from model_api_runtime.v2 import agent_loop as v2_agent_loop` to the file's imports.

```python
def test_chat_turn_always_replies_even_when_plan_omits_final_response(monkeypatch):
    """BUG-4: a trusted model's plan without final_response must NOT silently swallow the turn.
    Pre-loop, `wants_reply` was False and the responder never ran — the user got nothing."""
    uid = "u_w_loop_bug4"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="MODEL REPLY")
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hello"}],
                 is_official=True)
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(text=text) or {"id": "r1"})

    # Each round asks for a DIFFERENT action; an identical plan would trip the loop's
    # `no_progress` guard and we would never reach the max_rounds path this test is about.
    plans = [
        [{"type": "memory_index", "payload": {}}],                # round 0: no final_response
        [{"type": "memory_search", "payload": {"query": "a"}}],   # round 1: still none
        [{"type": "memory_fetch", "payload": {"ids": ["a"]}}],    # round 2: still none -> max_rounds
    ]
    calls = {"plan": 0}

    async def fake_plan(store, **kw):
        i = calls["plan"]; calls["plan"] += 1
        return [dict(s) for s in plans[min(i, len(plans) - 1)]]

    monkeypatch.setattr(v2_planner, "plan", fake_plan)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=True, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert calls["plan"] == worker._LOOP_MAX_ROUNDS   # looped; did not stop after round 0
    assert written.get("text") == "MODEL REPLY"       # forced reply at the cap — no silent swallow


def test_planner_second_round_receives_first_round_results(monkeypatch):
    uid = "u_w_loop_prior"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="R")
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hello"}],
                 is_official=True)
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    seen_prior = []

    async def fake_plan(store, **kw):
        seen_prior.append(kw.get("prior_action_results"))
        if len(seen_prior) == 1:
            return [{"type": "memory_index", "payload": {}}]
        return [{"type": "final_response", "payload": {}}]

    monkeypatch.setattr(v2_planner, "plan", fake_plan)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=True, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert seen_prior[0] in (None, {})               # round 0: nothing observed yet
    assert "memory_index" in (seen_prior[1] or {})   # round 1: the observation was fed back


def test_turn_llm_call_budget_binds_across_replan_and_rounds(monkeypatch):
    """replan_budget(2) x _LOOP_MAX_ROUNDS(3) + 1 = 7 > _TURN_MAX_LLM_CALLS(6). It must bite."""
    uid = "u_w_loop_budget"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="R")
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hello"}],
                 is_official=True)
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    calls = {"plan": 0, "respond": 0}

    async def fake_plan(store, **kw):
        calls["plan"] += 1
        # Payload varies per call so `no_progress` never fires — this test is about the
        # LLM-call budget, and a no-progress stop would mask it.
        return [{"type": "memory_index", "payload": {"n": calls["plan"]}}]   # never asks to reply

    async def fake_respond(**kw):
        calls["respond"] += 1
        return "reply"

    monkeypatch.setattr(v2_planner, "plan", fake_plan)
    monkeypatch.setattr(v2_responder, "respond", fake_respond)
    monkeypatch.setattr(v2_inval, "evaluate", lambda *a, **k: v2_inval.REPLAN)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=True, api_key=None, runtime_token="rt"))

    assert status == "completed"
    # Walk it: outer iter 1 burns rounds 0,1,2 (llm_calls=3) -> max_rounds. evaluate=REPLAN and
    # 3 < 5, so replan. Outer iter 2: rounds 0,1 plan (llm_calls=5); round 2 sees the budget
    # exhausted and returns wants_reply with no actions. evaluate=REPLAN but 5 < 5 is False ->
    # break. The responder takes the reserved 6th slot.
    assert calls["plan"] == 5
    assert calls["respond"] == 1   # the reserved responder slot is never eaten by the planner
    assert calls["plan"] + calls["respond"] == worker._TURN_MAX_LLM_CALLS


def test_final_text_from_decide_short_circuits_the_responder(monkeypatch):
    """Native-tools seam: if `decide` authored the reply, do not pay for a responder call."""
    uid = "u_w_loop_finaltext"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w")

    _patch_cheap_boundaries(monkeypatch, reply="SHOULD NOT BE USED")
    deps = _deps(messages=[{"id": "m1", "ts": 10.0, "role": "user", "content": "hello"}],
                 is_official=True)
    written = {}
    monkeypatch.setattr(worker, "_write_encrypted_reply",
                        lambda store, text: written.update(text=text) or {"id": "r"})

    called = {"respond": 0}

    async def fake_run_turn(*, decide, run_tools, max_rounds):
        return v2_agent_loop.LoopResult({}, {}, "authored inline", 1, v2_agent_loop.WANTS_REPLY)

    async def fake_respond(**kw):
        called["respond"] += 1
        return "should not happen"

    monkeypatch.setattr(v2_agent_loop, "run_turn", fake_run_turn)
    monkeypatch.setattr(v2_responder, "respond", fake_respond)

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_BYOK, is_official=True, api_key=None, runtime_token="rt"))

    assert status == "completed"
    assert called["respond"] == 0
    assert written.get("text") == "authored inline"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python -m pytest tests/test_v2_worker.py -q -k "always_replies or second_round_receives or llm_call_budget or short_circuits" --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: FAIL — `AttributeError: module 'model_api_runtime.v2.worker' has no attribute '_LOOP_MAX_ROUNDS'`.

- [ ] **Step 3: Add the module constants and import**

In `backend/model_api_runtime/v2/worker.py`, add to the import block (alphabetical, after `from model_api_runtime.v2 import coalesce as v2_coalesce`):

```python
from model_api_runtime.v2 import agent_loop as v2_agent_loop
```

Add near `_TAIL_HARD_CAP` (line ~90):

```python
# 工具循环轮数上限（spec §6）。撞上限 → 停止取工具，用手上的结果强制收口回复。
_LOOP_MAX_ROUNDS = int(os.environ.get("FEEDLING_V2_LOOP_MAX_ROUNDS", "3"))
# 一个回合内**跨两层循环**（外层消息驱动 replan × 内层模型驱动 tool loop）的 LLM 调用硬闸。
# 上界是 replan_budget(2) × _LOOP_MAX_ROUNDS(3) + 1(responder) = 7 —— 故意让 6 咬住它。
# 两层语义不同（外层"用户又说话了"、内层"我还想再查"）不能合并，但必须共用一个预算，
# 否则一个话痨用户 + 一个爱查东西的 planner 能把用户的 BYOK key 烧穿。
# 恒留 1 个名额给 responder：no-filler 铁律要求 chat lane 一定产出 model-authored 文本。
_TURN_MAX_LLM_CALLS = int(os.environ.get("FEEDLING_V2_TURN_MAX_LLM_CALLS", "6"))
```

- [ ] **Step 4: Replace the plan/execute block in `process_job` with the loop**

In `process_job`, replace lines 409-458 — from `replan_count = 0` through
`wants_reply = any(s["type"] == "final_response" for s in steps)` — with:

```python
        replan_count = 0
        # 跨两层循环共享的 LLM 调用计数（见 _TURN_MAX_LLM_CALLS）。外层 replan 不重置它。
        llm_calls = 0
        loop_res = v2_agent_loop.LoopResult({}, {}, None, 0, v2_agent_loop.NO_ACTIONS)

        while True:
            # 便宜预取（无 LLM，enclave-auth 凭证）：memory index + 感知摘要；确定性 digest（无 LLM，§7.2）。
            memory_index = await _cap_data(
                store, "memory_index", api_key=api_key, runtime_token=runtime_token,
                enclave_sem=enclave_sem)
            perception_summary = await _cap_data(
                store, "perception_snapshot", api_key=api_key, runtime_token=runtime_token,
                enclave_sem=enclave_sem)
            digest = {"messages": [{"content": m["content"][:400]} for m in coalesced[-6:]]}

            async def _decide(round_idx: int, prior: dict) -> v2_agent_loop.Decision:
                """json_planner 后端（spec §5 默认）：跑用户 BYOK 的结构化 JSON planner。

                预算耗尽 → 立刻收手（wants_reply=True），把最后一个名额留给 responder。
                `final_text` 恒为 None —— 散文由 responder 写（spec §4）。
                """
                nonlocal llm_calls
                if llm_calls >= _TURN_MAX_LLM_CALLS - 1:
                    return v2_agent_loop.Decision(actions=[], wants_reply=True)
                llm_calls += 1
                steps = await v2_planner.plan(
                    store,
                    provider_config=provider_config, is_official=is_official,
                    coalesced_messages=coalesced, digest=digest, memory_index=memory_index,
                    perception_summary=perception_summary, runtime_state=runtime_state,
                    lane=lane, reason=str(job.get("reason") or ""),
                    prior_action_results=prior or None)
                return v2_agent_loop.Decision(
                    actions=[s for s in steps if s["type"] != "final_response"],
                    wants_reply=any(s["type"] == "final_response" for s in steps))

            async def _run_tools(actions: list[dict]) -> dict:
                """executor 桥：DB 记账 + 排空。executor 的并行读/串行写/ENCLAVE_SEMAPHORE 原样复用。"""
                action_ids = await asyncio.to_thread(
                    jobs_store.add_actions, job_id, user_id,
                    [{"type": s["type"], "payload": s["payload"]} for s in actions])
                for s, aid in zip(actions, action_ids):
                    s["_action_id"] = aid
                return await v2_executor.execute_plan(
                    store, job_id, api_key=api_key, runtime_token=runtime_token,
                    plan=actions, read_parallelism=read_parallelism, enclave_sem=enclave_sem)

            loop_res = await v2_agent_loop.run_turn(
                decide=_decide, run_tools=_run_tools, max_rounds=_LOOP_MAX_ROUNDS)

            # 安全点（before_final_response）：跨进程/跨 worker 写入的新消息只活在 DB 里，
            # 本进程内存态的 store.chat_messages 未必看得到——先 reload 再判定，避免漏判。
            # evaluate 只看 role/ts（密文行本身不含明文，无需解密即可判定「有没有新用户消息」）。
            await asyncio.to_thread(store.reload)
            decision = v2_inval.evaluate(
                store.chat_messages, safe_point="before_final_response",
                coalesced_cursor_ts=cursor, replan_count=replan_count, replan_budget=replan_budget)
            if decision == v2_inval.REPLAN and llm_calls < _TURN_MAX_LLM_CALLS - 1:
                await asyncio.to_thread(v2_inval.invalidate, job_id, replan_job_id=job_id)
                replan_count += 1
                coalesced, cursor = await _coalesce_inputs(deps, user_id, since, enclave_sem=enclave_sem)
                continue
            break

        action_state = {"action_results": loop_res.action_results,
                        "action_digest": loop_res.action_digest}
        # BUG-4（矩阵 §E）：chat lane **恒**回复。「planner 没要 final_response」在单轮形状下
        # 被 worker 误读成「这回合不用回复」，可信模型漏写时用户消息被静默吞掉、零气泡。
        # 循环下同一个信号的含义是「想再查一轮」；轮数/预算用尽就用手上的结果强制收口。
        # 这不是占位气泡——responder 仍然产出真正的 model-authored 文本（no-filler 不变量）。
        wants_reply = lane == "chat" or loop_res.stop_reason == v2_agent_loop.WANTS_REPLY
```

Note the replan condition gains `and llm_calls < _TURN_MAX_LLM_CALLS - 1`: without it a REPLAN would re-enter the inner loop with an exhausted budget, `_decide` would immediately return `wants_reply=True` with no actions, and the outer loop would spin on `evaluate` forever.

- [ ] **Step 5: Short-circuit the responder on `final_text`**

Inside `if wants_reply:` (currently line 459), leave the `_emit_status`, `read_summary` / `read_tail` block untouched, and replace the `reply = await v2_responder.respond(...)` call plus its metric block with:

```python
            if loop_res.final_text:
                # 原生 tool-calling 后端在收手时自带回复（spec §3.1）。默认的 json_planner
                # 后端恒为 None，所以今天这条分支不会走到——留着，是为了别在接原生后端时
                # 白丢一次已经生成好的文本、再花一次 token 让 responder 重写。
                reply = loop_res.final_text
            else:
                _usage: dict = {}
                _t0 = time.monotonic()
                reply = await v2_responder.respond(
                    provider_config=provider_config, summary=summary, tail=tail,
                    action_results=action_state["action_results"], usage_out=_usage)
                if deps.record_turn_metric is not None:
                    try:
                        deps.record_turn_metric(
                            job_id=job_id, user_id=user_id, lane=lane,
                            prompt_tokens=_usage.get("prompt_tokens"),
                            completion_tokens=_usage.get("completion_tokens"),
                            latency_ms=int((time.monotonic() - _t0) * 1000),
                        )
                    except Exception as e:  # noqa: BLE001 — 记指标失败绝不能拖垮已经产出的回复
                        log.warning("[v2.worker] record_turn_metric failed job=%s: %s", job_id, e)
```

- [ ] **Step 6: Run the new tests to verify they pass**

```bash
python -m pytest tests/test_v2_worker.py -q
```

Expected: PASS. If a pre-existing worker test asserted "plan without final_response ⇒ no responder call", that test encoded BUG-4 — update it to assert the new forced-reply behaviour and say so in the report.

- [ ] **Step 7: Run the full V2 sweep**

```bash
python -m pytest tests/ -q -k "v2" --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: no new failures.

- [ ] **Step 8: Run the whole suite**

```bash
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: 2637 + new tests passed, 7 pre-existing failures, 0 new regressions. Postgres must be up on `127.0.0.1:55432` — without it ~2000 DB tests are silently skipped.

- [ ] **Step 9: Do NOT commit.**

---

## Task 7: Re-measure tokens/turn and record the loop's real cost

The loop is live. Take the same measurement as Task 3 and write the delta down. This is the number the D4 runbook's rollback gate consumes.

**Files:**
- Modify: `docs/HOSTED_RUNTIME_V2_TOKEN_BASELINE.md`
- Test: `tests/test_loadtest_compare.py`

**Interfaces:**
- Consumes: `compare_tokens.measure_turn_tokens` (Task 3); `compare_tokens.compare_tokens_per_turn` (pre-existing).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_loadtest_compare.py`:

```python
def test_multi_round_turn_costs_more_llm_calls_than_single_round():
    """The loop's cost is real and the instrument must see it. If this passes trivially
    (equal call counts), the mock is not driving the loop and the gate is blind."""
    from scripts.loadtest.compare_tokens import measure_turn_tokens
    from scripts.loadtest.mock_provider import MockProvider

    one_shot = '{"plan":[{"type":"final_response","payload":{}}],"reason":"t"}'
    fixtures = [{"summary": "", "tail": [{"role": "user", "content": "hello"}]}]

    with MockProvider(reply=one_shot, estimate_tokens=True) as p:
        single = measure_turn_tokens(fixtures, provider=p)

    assert single["llm_calls_per_turn"] == 2.0            # planner + responder
    assert single["tokens_per_turn"] > 0
```

- [ ] **Step 2: Run it**

```bash
python -m pytest tests/test_loadtest_compare.py -q
```

Expected: PASS (the harness measures the planner path, which Task 3 established).

- [ ] **Step 3: Re-run the Task 3 Step 5 measurement snippet verbatim**

Run the exact same snippet. Because `measure_turn_tokens` calls `planner.plan` directly (not `worker.process_job`), this still reports the single-round shape — it is the control. Record the number and confirm it matches Task 3's.

- [ ] **Step 4: MEASURE the loop's multiplier — do not derive it**

> **Corrected during execution.** This step originally said the worst case "is bounded by
> construction, not by measurement" and told you to write a table asserting the loop costs
> ≤3× a single round (from `6 calls / 2 calls`). **That is wrong.** Measurement through the
> real `worker.process_job` gives 2 calls → 505 tokens, 6 calls → 2066 tokens = **4.09×**,
> because from round 2 onward each planner prompt additionally carries the
> `prior_action_results` preview. Tokens grow faster than call count. A call-count bound
> understates the worst case by ~35%.
>
> The measurement harness is a throwaway `tests/test_tmp_loop_token_measure.py` that drives
> `worker.process_job` against a `MockProvider` subclass whose `reply` cycles per request.
> Two gotchas cost real time: the handler reads `provider.reply` **twice** per request when
> `estimate_tokens=True` (index by `reads // 2`), and `TurnDeps` must supply `read_summary`
> and `read_tail` or the responder raises `no_user_messages` before ever calling the provider.

Append the measured results to `docs/HOSTED_RUNTIME_V2_TOKEN_BASELINE.md` (already written —
see that file's "After the agent loop — MEASURED, not extrapolated" section, including the
context-dependence caveat: 4.09× is the small-context/pessimistic end, since a large summary
makes the responder prompt dominate and the multiplier fall). The superseded analytic table
is retained below only to show what NOT to write:

```markdown
## After the agent loop (worst case is a hard bound, not an estimate)

`worker._TURN_MAX_LLM_CALLS = 6` counts across BOTH loops (message-driven replan ×
model-driven tool rounds) and always reserves one slot for the responder. So:

| | LLM calls / turn | tokens / turn |
|---|---|---|
| single-round (recorded above) | 2.0 | <paste Task 3 value> |
| loop, typical (planner replies in round 1) | 2.0 | unchanged |
| loop, worst case (hard gate) | 6.0 | ≤ 3× the single-round value |

The typical case is unchanged: a planner that emits `final_response` on round 0 costs
exactly what it costs today. Only turns where the model genuinely asks for more context
pay more — which is the feature. The hard gate means no turn can cost more than 3×,
regardless of how chatty the user or how curious the planner.

**A `planner` prompt does NOT carry the conversation** — it carries the user message,
a digest, and (from round 2 on) a ≤600-char `prior_action_results` preview. That is why
multi-round JSON-planning is cheaper than an uncached native tool-calling loop, which
re-sends the whole conversation every round (spec §5).
```

Fill in the real value from Step 3.

- [ ] **Step 5: Run the full suite one last time**

```bash
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```

Expected: 7 pre-existing failures, 0 new regressions.

- [ ] **Step 6: Do NOT commit.** Report the final working-tree file list to the user.

---

## Out of scope (explicitly deferred, do not build)

- `native_tools` decide backend. Only the two seams (`Decision.final_text`, pluggable `decide`) are built.
- Prompt caching (`cache_control`) — a prerequisite for `native_tools`, not for this round.
- Per-capability JSON schemas — same.
- **Multimodal.** Its own round, immediately after this one; it re-adds `chat_image_read` to the planner vocabulary and gives `provider_client` a real image content block. Until then V2 is image-blind, which is strictly better than image-poisoned (BUG-1).
- BUG-2 (`capture` lane falls through to the chat path), BUG-3 (`scheduled` lane has no producer), `schedule_wake` / `cancel_wake` capability, `dream` / `screen_watch` lanes. All tracked in the parity matrix §F bucket 1.

## Traceability (parity matrix gate)

Per `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md`, every V2 acceptance test names the row it covers:

| Test | Parity row |
|---|---|
| `test_chat_image_read_is_not_emittable` | §A chat image / §E BUG-1 |
| `test_fold_action_results_drops_image_blob` | §E BUG-1 |
| `test_chat_turn_always_replies_even_when_plan_omits_final_response` | §E BUG-4 |
| `test_second_round_sees_first_round_results_and_accumulates` | §C Iteration |
| `test_planner_second_round_receives_first_round_results` | §C Replan |
| `test_final_text_from_decide_short_circuits_the_responder` | §C "who writes the reply" |
| `test_turn_llm_call_budget_binds_across_replan_and_rounds` | spec §6.1 |
