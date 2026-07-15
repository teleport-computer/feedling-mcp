# Hosted Runtime V2 — PR B: Provider Transport / Telemetry — Implementation Plan

> **STATUS: LANDED / HISTORICAL IMPLEMENTATION RECORD.**

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `backend/provider_client.py` into a normalized, tool-capable, natively-async transport and make per-turn telemetry one idempotent whole-turn metric per job.

**Architecture:** Additive extension of the existing dict-returning `chat_completion` / `chat_completion_async`. The return dict keeps `reply`/`usage`/… and GAINS a `tool_calls` list + an in-place-normalized `usage`. Per-wire codecs encode normalized `tools`/`tool_results` into each of the four wire formats and decode tool-call blocks out. Anthropic/Gemini/Responses become natively async by sharing pure build/parse functions between sync and async paths (the pattern OpenAI-compat already uses). A per-job whole-turn accumulator upserts one idempotent metric row.

**Tech Stack:** Python, httpx (sync `_http_client` + async `_async_http_client`), psycopg, Postgres, alembic.

## Global Constraints

- **Additive safety (spec):** existing text-only callers (`responder`, `planner`, `extraction`, `compaction`, `hosted/turn`, `genesis/llm_client`) must keep working with ZERO changes to how they read `result["reply"]`. `chat_completion*` KEEPS returning a `dict` (NOT a breaking `ProviderResponse` return); it gains a `tool_calls` key (empty list when no tools requested) and normalizes `usage` in place. `ProviderResponse` (Task 1) is a typed adapter PR C builds from the dict — never the wire return type.
- **Delivery boundary = transport-only (spec):** NO live production caller passes `tools` in PR B; do NOT remove `is_official`/`rule_plan`/`official_plan`; do NOT add a real tool-execution loop (all PR C). Acceptance is proven by codec round-trip tests, not a live loop.
- **Dependency direction:** `backend/provider_types.py` is top-level (alongside `provider_client.py`), pure — no `hosted`/`agent_runtime`/`db` imports. v2 core modules (`model_api_runtime/v2/*` except `serve_worker.py`) must not import `hosted`/`agent_runtime` (guard: `tests/test_v2_dependency_direction.py`).
- **No LiteLLM** in `provider_client`'s path (already true — keep it that way).
- **Gemini has no tool-call id:** synthesize `call_{index}_{name}` deterministically; keep a name↔synthetic-id map so tool_result encode (Gemini keys `functionResponse` on name) is reconstructable.
- **Whole-turn metric is idempotent by `job_id`** (one row per job, upsert-replace); covers all model calls, retries, and failed turns.
- **Native-async conversions must preserve** `reliable_chat_completion_async` retry/backoff/timeout semantics — no regression.
- **NO-COMMIT mode:** leave every change in the working tree; never run `git commit`/`git add`/`git stash`/`git checkout --`/`git reset`/`git clean`. The human commits. (The `git add`/`git commit` steps shown in the template below are REPLACED by "leave in working tree" for this plan.)
- **Postgres** at `127.0.0.1:55432`. Full suite: `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py`; baseline = 8 pre-existing failures (all in `tests/test_model_api_path.py`), zero new.

---

## File Structure

- `backend/provider_types.py` (NEW) — pure normalized types + `ProviderResponse.from_result`.
- `backend/provider_client.py` (MODIFY) — `_normalize_usage`; per-wire tool encode/decode; `tools` param threading + `tool_calls` in the return dict; native-async for anthropic/gemini/responses via shared pure build/parse.
- `backend/model_api_runtime/v2/responder.py`, `planner.py`, `extraction.py`, `compaction.py` (MODIFY) — read normalized `usage`; fold usage into the turn accumulator.
- `backend/alembic/versions/0029_v2_turn_metrics_whole_turn.py` (NEW) — columns + `UNIQUE(job_id)` + dedup.
- `backend/model_api_runtime/v2/jobs_store.py` (MODIFY) — `record_whole_turn_metric` upsert (replacing `record_turn_metric`'s role).
- `backend/model_api_runtime/v2/worker.py` + `serve_worker.py` (MODIFY) — per-turn accumulator; terminal flush on success AND mark_failed.
- `scripts/loadtest/collect.py`, `run_loadtest.py` (MODIFY) — new columns; simulated processor writes whole-turn shape.
- `scripts/provider_probe/probe.py` (NEW) — manual live 4-provider probe (not in CI).
- Tests: `tests/test_provider_types.py`, `tests/test_provider_usage_normalize.py`, `tests/test_provider_tools_openai.py`, `tests/test_provider_tools_anthropic.py`, `tests/test_provider_tools_gemini.py`, `tests/test_provider_tools_acceptance.py`, `tests/test_provider_async_native.py`, `tests/test_v2_whole_turn_metric.py`, plus updates to `tests/test_loadtest_*.py`.

---

### Task 1: B1 — Normalized provider types (`backend/provider_types.py`)

**Files:**
- Create: `backend/provider_types.py`
- Test: `tests/test_provider_types.py`

**Interfaces:**
- Produces: `ToolSpec(name,description,parameters)`, `ToolCall(id,name,args,args_raw="",args_ok=True)`, `ToolResult(call_id,content)`, `Usage(prompt_tokens,completion_tokens,total_tokens)`, `ProviderResponse(text,tool_calls,usage,raw)`, and classmethod `ProviderResponse.from_result(result: dict) -> ProviderResponse`.

- [ ] **Step 1: Failing test** `tests/test_provider_types.py`

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from provider_types import ToolSpec, ToolCall, ToolResult, Usage, ProviderResponse


def test_types_construct():
    ts = ToolSpec(name="web_search", description="search", parameters={"type": "object"})
    tc = ToolCall(id="c1", name="web_search", args={"q": "hi"})
    assert tc.args_ok is True and tc.args_raw == ""
    assert ToolResult(call_id="c1", content="ok").call_id == "c1"
    assert Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15).total_tokens == 15


def test_provider_response_from_result():
    result = {
        "reply": "hello",
        "tool_calls": [{"id": "c1", "name": "web_search", "args": {"q": "x"},
                        "args_raw": "", "args_ok": True}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
    }
    pr = ProviderResponse.from_result(result)
    assert pr.text == "hello"
    assert pr.tool_calls == [ToolCall(id="c1", name="web_search", args={"q": "x"})]
    assert pr.usage == Usage(prompt_tokens=3, completion_tokens=4, total_tokens=7)
    assert pr.raw is result


def test_from_result_defaults_missing_tool_calls_and_usage():
    pr = ProviderResponse.from_result({"reply": "hi"})
    assert pr.tool_calls == [] and pr.text == "hi"
    assert pr.usage == Usage(prompt_tokens=None, completion_tokens=None, total_tokens=None)
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: provider_types`).
  `python -m pytest tests/test_provider_types.py -q`

- [ ] **Step 3: Implement** `backend/provider_types.py`

```python
"""Normalized provider transport types (Hosted Runtime V2 PR B / spec B1).

Top-level module (alongside provider_client.py) so both v2 and legacy callers
can import it without a top-level->v2-subpackage dependency. PURE: no
hosted/agent_runtime/db imports. `ProviderResponse` is a typed VIEW over the
dict that provider_client.chat_completion* returns — the dict stays the wire
return type for backward compatibility; PR C uses from_result() for typed access.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema object


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict
    args_raw: str = ""      # provider's raw args string when JSON parse failed
    args_ok: bool = True    # False -> args_raw holds the unparseable original


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    content: str


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True)
class ProviderResponse:
    text: str
    tool_calls: list[ToolCall]
    usage: Usage
    raw: dict

    @classmethod
    def from_result(cls, result: dict) -> "ProviderResponse":
        raw_calls = result.get("tool_calls") or []
        calls = [
            ToolCall(
                id=str(c.get("id") or ""),
                name=str(c.get("name") or ""),
                args=dict(c.get("args") or {}),
                args_raw=str(c.get("args_raw") or ""),
                args_ok=bool(c.get("args_ok", True)),
            )
            for c in raw_calls
        ]
        u = result.get("usage") or {}
        usage = Usage(
            prompt_tokens=u.get("prompt_tokens"),
            completion_tokens=u.get("completion_tokens"),
            total_tokens=u.get("total_tokens"),
        )
        return cls(text=str(result.get("reply") or ""), tool_calls=calls,
                   usage=usage, raw=result)
```

- [ ] **Step 4: Run — expect PASS.** Leave in working tree; do NOT commit.

---

### Task 2: B4 — Usage normalization in place (`provider_client.py`)

**Files:**
- Modify: `backend/provider_client.py` (add `_normalize_usage`; call it in each of the 4 wire parsers so the returned `result["usage"]` always carries `prompt_tokens`/`completion_tokens`/`total_tokens`).
- Test: `tests/test_provider_usage_normalize.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `provider_client._normalize_usage(provider: str, raw: dict | None) -> dict` returning `{"prompt_tokens", "completion_tokens", "total_tokens"}` (values int or None). The four wire parsers set `result["usage"] = _normalize_usage(provider, raw_usage_blob)`.

**Why early:** this is backward-compatible AND fixes the live bug where `responder.py:165` reads `usage["prompt_tokens"]` (populated only for OpenAI today; None for Anthropic/Gemini). After this task those keys are populated for all providers, so `responder.py` needs no read change.

- [ ] **Step 1: Failing test** `tests/test_provider_usage_normalize.py`

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc


def test_openai_usage_passthrough():
    raw = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert pc._normalize_usage("openai", raw) == {
        "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}


def test_anthropic_usage_mapped():
    raw = {"input_tokens": 12, "output_tokens": 8}
    assert pc._normalize_usage("anthropic", raw) == {
        "prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}


def test_gemini_usage_mapped():
    raw = {"promptTokenCount": 30, "candidatesTokenCount": 9, "totalTokenCount": 39}
    assert pc._normalize_usage("gemini", raw) == {
        "prompt_tokens": 30, "completion_tokens": 9, "total_tokens": 39}


def test_empty_usage_yields_nones():
    assert pc._normalize_usage("anthropic", None) == {
        "prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
```

- [ ] **Step 2: Run — expect FAIL** (`AttributeError: _normalize_usage`).

- [ ] **Step 3: Implement** — add to `provider_client.py`:

```python
def _normalize_usage(provider: str, raw: dict | None) -> dict:
    """Normalize a provider's raw usage blob to {prompt_tokens, completion_tokens,
    total_tokens} (spec B4). OpenAI/compat/Responses already use those key names;
    Anthropic uses input/output_tokens; Gemini uses promptTokenCount/
    candidatesTokenCount/totalTokenCount. Missing -> None; total defaults to the
    sum of prompt+completion when the provider omits an explicit total."""
    raw = raw if isinstance(raw, dict) else {}
    if provider == "anthropic":
        pt = raw.get("input_tokens")
        ct = raw.get("output_tokens")
        tt = raw.get("total_tokens")
    elif provider == "gemini":
        pt = raw.get("promptTokenCount")
        ct = raw.get("candidatesTokenCount")
        tt = raw.get("totalTokenCount")
    else:  # openai, openrouter, deepseek, openai_compatible, responses
        pt = raw.get("prompt_tokens")
        ct = raw.get("completion_tokens")
        tt = raw.get("total_tokens")
    if tt is None and (pt is not None or ct is not None):
        tt = (pt or 0) + (ct or 0)
    return {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": tt}
```

Then in each wire parser replace the raw-usage assignment with a normalized one:
- `_parse_openai_compat_body` (`provider_client.py:826`): `"usage": _normalize_usage(provider, body.get("usage"))`.
- `_chat_completion_anthropic` return (`:937`): `"usage": _normalize_usage("anthropic", body.get("usage"))`.
- `_chat_completion_gemini` return: `"usage": _normalize_usage("gemini", body.get("usageMetadata"))`.
- `_chat_completion_openai_responses` return (in `_extract_openai_responses_output`/the responses builder — find where it sets `usage`): `"usage": _normalize_usage("openai", body.get("usage"))`.
  (If the responses path does not currently populate `usage`, add `"usage": _normalize_usage("openai", body.get("usage"))` to its return dict.)

- [ ] **Step 4: Run — expect PASS.**

- [ ] **Step 5: Regression** — `python -m pytest tests/test_model_api_path.py tests/test_v2_responder.py -q`; expect no NEW failures vs the 8-baseline (responder now gets real tokens for anthropic/gemini). Leave in working tree.

---

### Task 3: B2 — OpenAI Chat + Responses tool codec (`provider_client.py`)

**Files:**
- Modify: `backend/provider_client.py` (encode `tools`/`tool_results` + decode `tool_calls` for the OpenAI Chat-completions and OpenAI Responses wires).
- Test: `tests/test_provider_tools_openai.py`

**Interfaces:**
- Consumes: `provider_types.ToolSpec/ToolCall/ToolResult`.
- Produces (module-level pure fns): `_encode_tools_openai_chat(tools) -> list[dict]`, `_decode_tool_calls_openai_chat(body) -> list[dict]`, `_encode_tools_openai_responses(tools) -> list[dict]`, `_decode_tool_calls_openai_responses(body) -> list[dict]`. Each decoded tool-call dict has keys `{id, name, args, args_raw, args_ok}` (matching `ProviderResponse.from_result`).

- [ ] **Step 1: Failing test** `tests/test_provider_tools_openai.py`

```python
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from provider_types import ToolSpec


TOOLS = [
    ToolSpec("web_search", "search the web", {"type": "object",
             "properties": {"q": {"type": "string"}}, "required": ["q"]}),
    ToolSpec("get_time", "get current time", {"type": "object", "properties": {}}),
]


def test_encode_tools_openai_chat():
    enc = pc._encode_tools_openai_chat(TOOLS)
    assert enc[0] == {"type": "function", "function": {
        "name": "web_search", "description": "search the web",
        "parameters": TOOLS[0].parameters}}
    assert enc[1]["function"]["name"] == "get_time"


def test_decode_two_tool_calls_openai_chat():
    body = {"choices": [{"message": {"tool_calls": [
        {"id": "call_a", "function": {"name": "web_search",
         "arguments": json.dumps({"q": "weather"})}},
        {"id": "call_b", "function": {"name": "get_time", "arguments": "{}"}},
    ]}}]}
    calls = pc._decode_tool_calls_openai_chat(body)
    assert [c["id"] for c in calls] == ["call_a", "call_b"]
    assert calls[0]["name"] == "web_search" and calls[0]["args"] == {"q": "weather"}
    assert calls[0]["args_ok"] is True


def test_decode_bad_args_marks_not_ok():
    body = {"choices": [{"message": {"tool_calls": [
        {"id": "call_x", "function": {"name": "web_search", "arguments": "{not json"}}]}}]}
    call = pc._decode_tool_calls_openai_chat(body)[0]
    assert call["args_ok"] is False and call["args_raw"] == "{not json" and call["args"] == {}


def test_encode_tools_openai_responses_and_decode():
    enc = pc._encode_tools_openai_responses(TOOLS)
    assert enc[0] == {"type": "function", "name": "web_search",
                      "description": "search the web", "parameters": TOOLS[0].parameters}
    body = {"output": [
        {"type": "function_call", "call_id": "fc_a", "name": "web_search",
         "arguments": json.dumps({"q": "x"})},
        {"type": "function_call", "call_id": "fc_b", "name": "get_time", "arguments": "{}"},
    ]}
    calls = pc._decode_tool_calls_openai_responses(body)
    assert [c["id"] for c in calls] == ["fc_a", "fc_b"]
    assert calls[0]["args"] == {"q": "x"}
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** in `provider_client.py`:

```python
def _parse_tool_args(raw) -> tuple[dict, str, bool]:
    """Normalize a provider tool-call args payload to (dict, raw_str, ok).
    OpenAI/Responses send a JSON *string*; Anthropic/Gemini send an object."""
    if isinstance(raw, dict):
        return raw, "", True
    s = "" if raw is None else str(raw)
    try:
        parsed = json.loads(s) if s else {}
        return (parsed if isinstance(parsed, dict) else {}), ("" if isinstance(parsed, dict) else s), isinstance(parsed, dict)
    except (ValueError, TypeError):
        return {}, s, False


def _encode_tools_openai_chat(tools) -> list[dict]:
    return [{"type": "function", "function": {
        "name": t.name, "description": t.description, "parameters": t.parameters}} for t in tools]


def _decode_tool_calls_openai_chat(body: dict) -> list[dict]:
    try:
        raw_calls = body["choices"][0]["message"].get("tool_calls") or []
    except (KeyError, IndexError, TypeError):
        return []
    out = []
    for c in raw_calls:
        fn = c.get("function") or {}
        args, args_raw, ok = _parse_tool_args(fn.get("arguments"))
        out.append({"id": str(c.get("id") or ""), "name": str(fn.get("name") or ""),
                    "args": args, "args_raw": args_raw, "args_ok": ok})
    return out


def _encode_tools_openai_responses(tools) -> list[dict]:
    return [{"type": "function", "name": t.name, "description": t.description,
             "parameters": t.parameters} for t in tools]


def _decode_tool_calls_openai_responses(body: dict) -> list[dict]:
    out = []
    for item in (body.get("output") or []):
        if item.get("type") != "function_call":
            continue
        args, args_raw, ok = _parse_tool_args(item.get("arguments"))
        out.append({"id": str(item.get("call_id") or ""), "name": str(item.get("name") or ""),
                    "args": args, "args_raw": args_raw, "args_ok": ok})
    return out


def _encode_tool_results_openai_chat(results) -> list[dict]:
    return [{"role": "tool", "tool_call_id": r.call_id, "content": r.content} for r in results]


def _encode_tool_results_openai_responses(results) -> list[dict]:
    return [{"type": "function_call_output", "call_id": r.call_id, "output": r.content} for r in results]
```

Ensure `import json` is present at the top of `provider_client.py` (it is used elsewhere; confirm).

- [ ] **Step 4: Run — expect PASS.** Leave in working tree.

---

### Task 4: B2 — Anthropic tool codec (`provider_client.py`)

**Files:**
- Modify: `backend/provider_client.py`
- Test: `tests/test_provider_tools_anthropic.py`

**Interfaces:**
- Produces: `_encode_tools_anthropic(tools) -> list[dict]`, `_decode_tool_calls_anthropic(body) -> list[dict]`, `_encode_tool_results_anthropic(results) -> list[dict]`.

- [ ] **Step 1: Failing test** `tests/test_provider_tools_anthropic.py`

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from provider_types import ToolSpec, ToolResult

TOOLS = [ToolSpec("web_search", "search", {"type": "object", "properties": {"q": {"type": "string"}}}),
         ToolSpec("get_time", "time", {"type": "object", "properties": {}})]


def test_encode_tools_anthropic():
    enc = pc._encode_tools_anthropic(TOOLS)
    assert enc[0] == {"name": "web_search", "description": "search",
                      "input_schema": TOOLS[0].parameters}


def test_decode_two_tool_uses_anthropic():
    body = {"content": [
        {"type": "text", "text": "let me look"},
        {"type": "tool_use", "id": "toolu_a", "name": "web_search", "input": {"q": "x"}},
        {"type": "tool_use", "id": "toolu_b", "name": "get_time", "input": {}},
    ]}
    calls = pc._decode_tool_calls_anthropic(body)
    assert [c["id"] for c in calls] == ["toolu_a", "toolu_b"]
    assert calls[0]["name"] == "web_search" and calls[0]["args"] == {"q": "x"}
    assert calls[0]["args_ok"] is True


def test_encode_tool_results_anthropic():
    enc = pc._encode_tool_results_anthropic([ToolResult("toolu_a", "sunny"),
                                             ToolResult("toolu_b", "12:00")])
    assert enc == [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "toolu_a", "content": "sunny"},
        {"type": "tool_result", "tool_use_id": "toolu_b", "content": "12:00"}]}]
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** in `provider_client.py`:

```python
def _encode_tools_anthropic(tools) -> list[dict]:
    return [{"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in tools]


def _decode_tool_calls_anthropic(body: dict) -> list[dict]:
    out = []
    for block in (body.get("content") or []):
        if block.get("type") != "tool_use":
            continue
        args, args_raw, ok = _parse_tool_args(block.get("input"))
        out.append({"id": str(block.get("id") or ""), "name": str(block.get("name") or ""),
                    "args": args, "args_raw": args_raw, "args_ok": ok})
    return out


def _encode_tool_results_anthropic(results) -> list[dict]:
    # Anthropic carries tool results as tool_result content blocks in ONE user turn.
    return [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": r.call_id, "content": r.content} for r in results]}]
```

- [ ] **Step 4: Run — expect PASS.** Leave in working tree.

---

### Task 5: B2 — Gemini tool codec + synthetic call_id (`provider_client.py`)

**Files:**
- Modify: `backend/provider_client.py`
- Test: `tests/test_provider_tools_gemini.py`

**Interfaces:**
- Produces: `_encode_tools_gemini(tools) -> list[dict]`, `_decode_tool_calls_gemini(body) -> list[dict]` (synthesizes `id = f"call_{index}_{name}"`), `_encode_tool_results_gemini(results, id_to_name: dict) -> list[dict]` (rebuilds `functionResponse` keyed by name, resolved from the synthetic-id→name map).

**Gemini wrinkle:** `functionCall` has no id; decode synthesizes `call_{index}_{name}` and the returned tool-call dict also carries the resolved `name`, so the caller can build an `id_to_name` map (`{call["id"]: call["name"]}`) to pass back to `_encode_tool_results_gemini`. Two calls to the same tool differ by `index` in the id.

- [ ] **Step 1: Failing test** `tests/test_provider_tools_gemini.py`

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from provider_types import ToolSpec, ToolResult

TOOLS = [ToolSpec("web_search", "search", {"type": "object", "properties": {"q": {"type": "string"}}}),
         ToolSpec("get_time", "time", {"type": "object", "properties": {}})]


def test_encode_tools_gemini():
    enc = pc._encode_tools_gemini(TOOLS)
    assert enc == [{"functionDeclarations": [
        {"name": "web_search", "description": "search", "parameters": TOOLS[0].parameters},
        {"name": "get_time", "description": "time", "parameters": TOOLS[1].parameters}]}]


def test_decode_two_function_calls_gemini_synthesizes_ids():
    body = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "web_search", "args": {"q": "x"}}},
        {"functionCall": {"name": "get_time", "args": {}}}]}}]}
    calls = pc._decode_tool_calls_gemini(body)
    assert [c["id"] for c in calls] == ["call_0_web_search", "call_1_get_time"]
    assert calls[0]["name"] == "web_search" and calls[0]["args"] == {"q": "x"}


def test_same_tool_twice_disambiguated_by_index():
    body = {"candidates": [{"content": {"parts": [
        {"functionCall": {"name": "web_search", "args": {"q": "a"}}},
        {"functionCall": {"name": "web_search", "args": {"q": "b"}}}]}}]}
    calls = pc._decode_tool_calls_gemini(body)
    assert [c["id"] for c in calls] == ["call_0_web_search", "call_1_web_search"]


def test_encode_tool_results_gemini_by_name_from_map():
    id_to_name = {"call_0_web_search": "web_search", "call_1_get_time": "get_time"}
    enc = pc._encode_tool_results_gemini(
        [ToolResult("call_0_web_search", "sunny"), ToolResult("call_1_get_time", "12:00")], id_to_name)
    assert enc == [{"role": "user", "parts": [
        {"functionResponse": {"name": "web_search", "response": {"content": "sunny"}}},
        {"functionResponse": {"name": "get_time", "response": {"content": "12:00"}}}]}]
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** in `provider_client.py`:

```python
def _encode_tools_gemini(tools) -> list[dict]:
    return [{"functionDeclarations": [
        {"name": t.name, "description": t.description, "parameters": t.parameters} for t in tools]}]


def _decode_tool_calls_gemini(body: dict) -> list[dict]:
    out = []
    try:
        parts = body["candidates"][0]["content"]["parts"] or []
    except (KeyError, IndexError, TypeError):
        return []
    idx = 0
    for part in parts:
        fc = part.get("functionCall")
        if not isinstance(fc, dict):
            continue
        name = str(fc.get("name") or "")
        args, args_raw, ok = _parse_tool_args(fc.get("args"))
        out.append({"id": f"call_{idx}_{name}", "name": name,
                    "args": args, "args_raw": args_raw, "args_ok": ok})
        idx += 1
    return out


def _encode_tool_results_gemini(results, id_to_name: dict) -> list[dict]:
    # Gemini keys functionResponse on the tool NAME (no id); resolve each result's
    # synthetic call_id back to its name via the map the decoder's ids imply.
    parts = []
    for r in results:
        name = id_to_name.get(r.call_id, r.call_id)
        parts.append({"functionResponse": {"name": name, "response": {"content": r.content}}})
    return [{"role": "user", "parts": parts}]
```

- [ ] **Step 4: Run — expect PASS.** Leave in working tree.

---

### Task 6: Thread `tools` through `chat_completion` / `chat_completion_async` + return `tool_calls`

**Files:**
- Modify: `backend/provider_client.py` (add `tools: list[ToolSpec] | None = None` to `chat_completion`, `chat_completion_async`, and the 4 `_chat_completion_*` wire handlers; encode tools into each payload; put decoded `tool_calls` (empty list when none) in every return dict).
- Test: `tests/test_provider_tools_wire.py`

**Interfaces:**
- Consumes: Task 3/4/5 codecs.
- Produces: `chat_completion(..., tools=None)` / `chat_completion_async(..., tools=None)` returning the existing dict PLUS `result["tool_calls"]: list[dict]` (always present). When `tools` is falsy, no `tools` field is sent and `tool_calls == []`.

- [ ] **Step 1: Failing test** `tests/test_provider_tools_wire.py` — monkeypatch each wire's HTTP post to return a canned 2-tool-call body; assert `chat_completion(config, msgs, tools=TOOLS)["tool_calls"]` has 2 entries with ids; assert `tools=None` yields `tool_calls == []` and existing `result["reply"]` unchanged. (Use `monkeypatch.setattr(pc, "_http_client", ...)` returning a fake client whose `.post` returns a fake response with `.json()`/`.status_code=200`.) Cover all 4 providers via `ProviderConfig(provider=…)`.

```python
# sketch — one provider shown; parametrize over the 4
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc
from provider_types import ToolSpec

TOOLS = [ToolSpec("web_search", "s", {"type": "object"}), ToolSpec("get_time", "t", {"type": "object"})]

class _Resp:
    status_code = 200
    def __init__(self, body): self._b = body
    def json(self): return self._b
    def raise_for_status(self): pass

def _fake_client(body):
    class C:
        def post(self, *a, **k): return _Resp(body)
    return C()

def test_anthropic_returns_two_tool_calls(monkeypatch):
    body = {"content": [
        {"type": "tool_use", "id": "t_a", "name": "web_search", "input": {"q": "x"}},
        {"type": "tool_use", "id": "t_b", "name": "get_time", "input": {}}],
        "usage": {"input_tokens": 1, "output_tokens": 2}, "stop_reason": "tool_use"}
    monkeypatch.setattr(pc, "_http_client", lambda: _fake_client(body))
    cfg = pc.ProviderConfig("anthropic", "claude-x", "k", "https://api.anthropic.com/v1")
    res = pc.chat_completion(cfg, [{"role": "user", "content": "hi"}], tools=TOOLS, require_reply=False)
    assert [c["id"] for c in res["tool_calls"]] == ["t_a", "t_b"]

def test_no_tools_empty_list(monkeypatch):
    body = {"content": [{"type": "text", "text": "hello"}], "usage": {"input_tokens": 1, "output_tokens": 1}}
    monkeypatch.setattr(pc, "_http_client", lambda: _fake_client(body))
    cfg = pc.ProviderConfig("anthropic", "claude-x", "k", "https://api.anthropic.com/v1")
    res = pc.chat_completion(cfg, [{"role": "user", "content": "hi"}])
    assert res["tool_calls"] == [] and res["reply"] == "hello"
```

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** — thread `tools` through:
  - Add `tools: "list[ToolSpec] | None" = None` param to `chat_completion` (`:1015`), `chat_completion_async` (`:1180`), and each `_chat_completion_*`.
  - In each wire builder, when `tools`: set the wire's tools field — Anthropic `payload["tools"] = _encode_tools_anthropic(tools)`; Gemini `payload["tools"] = _encode_tools_gemini(tools)`; OpenAI-compat `payload["tools"] = _encode_tools_openai_chat(tools)` (in `_build_openai_compat_payload`, add a `tools` param); Responses `payload["tools"] = _encode_tools_openai_responses(tools)`.
  - In each parser/return, add `"tool_calls": _decode_tool_calls_<wire>(body)` (Anthropic/Gemini/Responses in their return dicts; OpenAI-compat in `_parse_openai_compat_body`, which needs the raw `body` — it already has it).
  - `chat_completion` / `chat_completion_async` pass `tools=tools` down to the selected `_chat_completion_*`.
  - Guarantee `tool_calls` is ALWAYS present: give it a default in each parser (empty list when the wire has no tool blocks — the decoders already return `[]`).

- [ ] **Step 4: Run — expect PASS** (all 4 providers). Then regression `python -m pytest tests/test_v2_responder.py tests/test_model_api_path.py -q` — existing callers unaffected (they pass no `tools`). Leave in working tree.

---

### Task 7: B3 — Native async for Anthropic / Gemini / Responses (`provider_client.py`)

**Files:**
- Modify: `backend/provider_client.py` — factor each of the 3 wires into pure `_build_<wire>_payload(...)` + `_parse_<wire>_body(body, ...) -> dict` (mirroring `_build_openai_compat_payload`/`_parse_openai_compat_body`), keep the sync `_chat_completion_<wire>` using them, and replace the 3 `anyio.to_thread` branches in `chat_completion_async` (`:1199-1210`) with real `_async_http_client()` POSTs that call the shared build/parse.
- Test: `tests/test_provider_async_native.py`

**Interfaces:**
- Consumes: Task 6 (`tools` threading), Task 2 (`_normalize_usage`).
- Produces: `chat_completion_async` dispatches Anthropic/Gemini/Responses through native async (no `anyio.to_thread`), returning the same dict shape (incl. `tool_calls`, normalized `usage`) as the sync path.

- [ ] **Step 1: Failing test** `tests/test_provider_async_native.py` — monkeypatch `pc._async_http_client` to return a fake async client whose `.post` is an async fn returning a canned body; `await chat_completion_async(cfg_anthropic, msgs)` returns the parsed dict; assert `anyio.to_thread` is NOT used (e.g. monkeypatch `anyio.to_thread.run_sync` to raise, proving the async path no longer routes through it for these 3 providers). Parametrize anthropic/gemini/responses.

```python
import sys, asyncio
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import provider_client as pc

class _AResp:
    status_code = 200
    def __init__(self, b): self._b = b
    def json(self): return self._b
    def raise_for_status(self): pass

def _fake_async_client(body):
    class C:
        async def post(self, *a, **k): return _AResp(body)
    return C()

def test_anthropic_native_async_no_thread_bridge(monkeypatch):
    body = {"content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 2, "output_tokens": 3}, "stop_reason": "end_turn"}
    monkeypatch.setattr(pc, "_async_http_client", lambda: _fake_async_client(body))
    import anyio.to_thread
    def _boom(*a, **k): raise AssertionError("must not use thread bridge")
    monkeypatch.setattr(anyio.to_thread, "run_sync", _boom)
    cfg = pc.ProviderConfig("anthropic", "claude-x", "k", "https://api.anthropic.com/v1")
    res = asyncio.get_event_loop().run_until_complete(
        pc.chat_completion_async(cfg, [{"role": "user", "content": "hi"}]))
    assert res["reply"] == "hi" and res["usage"]["prompt_tokens"] == 2
```

- [ ] **Step 2: Run — expect FAIL** (currently routes through `anyio.to_thread`).

- [ ] **Step 3: Implement** — for each of anthropic/gemini/responses:
  - Extract `_build_<wire>_payload(*, model, messages, max_tokens, temperature, response_format, include_reasoning, tools) -> (payload, url, headers)` and `_parse_<wire>_body(body, *, model, require_reply) -> dict` from the current sync handler (moving the payload build + the response parse out of the httpx call; keep the httpx POST + `_raise_for_provider_status` in the sync `_chat_completion_<wire>`).
  - In `chat_completion_async`, replace the `anyio.to_thread` branch with, per provider: build payload via the shared fn, `resp = await _async_http_client().post(url, headers=headers, json=payload, timeout=timeout)`, `_raise_for_provider_status(resp)`, `return _parse_<wire>_body(resp.json(), model=…, require_reply=…)`.
  - Preserve the responses-wire reasoning/response_format handling and the anthropic thinking-budget logic inside the shared build fn.

- [ ] **Step 4: Run — expect PASS.** Regression: `python -m pytest tests/test_v2_worker.py tests/test_v2_responder.py -q`; no new failures. Leave in working tree.

---

### Task 8: B5 — Idempotent whole-turn metric (migration 0029 + accumulator)

**Files:**
- Create: `backend/alembic/versions/0029_v2_turn_metrics_whole_turn.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py` (`record_whole_turn_metric`), `backend/model_api_runtime/v2/worker.py` (per-turn accumulator + terminal flush on success AND every mark_failed path), `backend/model_api_runtime/v2/serve_worker.py` if the accumulator is injected via deps.
- Test: `tests/test_v2_whole_turn_metric.py`, update `tests/test_v2_jobs_store.py` / `tests/test_v2_jobs_migration.py` head-pin.

**Interfaces:**
- Consumes: `db.get_pool`.
- Produces: `jobs_store.record_whole_turn_metric(job_id, user_id, lane, *, prompt_tokens, completion_tokens, latency_ms, model_calls, retries, failed, status) -> None` (upsert on `job_id`); a `TurnMetrics` accumulator (small dataclass in `worker.py` or `jobs_store.py`) with `add_call(usage: dict, *, retried: bool=False)` and `flush(*, failed: bool, status: str)`.

- [ ] **Step 1: Failing test** `tests/test_v2_whole_turn_metric.py`

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db
from model_api_runtime.v2 import jobs_store
from conftest import seed_user
import os, pytest
pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")


def _seed_job(uid):
    seed_user(uid)
    jid, _ = jobs_store.enqueue_job(uid, "chat", reason="t")
    return jid


def test_upsert_is_idempotent_by_job(pg_clean_metrics):
    uid = "u_wtm1"; jid = _seed_job(uid)
    jobs_store.record_whole_turn_metric(jid, uid, "chat", prompt_tokens=10,
        completion_tokens=5, latency_ms=100, model_calls=2, retries=0, failed=False, status="ok")
    jobs_store.record_whole_turn_metric(jid, uid, "chat", prompt_tokens=30,
        completion_tokens=9, latency_ms=200, model_calls=3, retries=1, failed=False, status="ok")
    with db.get_pool().connection() as c:
        rows = c.execute("SELECT prompt_tokens, model_calls FROM v2_turn_metrics WHERE job_id=%s",
                         (jid,)).fetchall()
    assert len(rows) == 1 and rows[0][0] == 30 and rows[0][1] == 3   # latest wins, one row


def test_failed_turn_is_recorded(pg_clean_metrics):
    uid = "u_wtm2"; jid = _seed_job(uid)
    jobs_store.record_whole_turn_metric(jid, uid, "chat", prompt_tokens=7,
        completion_tokens=0, latency_ms=50, model_calls=1, retries=0, failed=True, status="provider_error")
    with db.get_pool().connection() as c:
        row = c.execute("SELECT failed, status FROM v2_turn_metrics WHERE job_id=%s", (jid,)).fetchone()
    assert row[0] is True and row[1] == "provider_error"
```
(Add a `pg_clean_metrics` fixture TRUNCATE-ing `v2_turn_metrics, agent_jobs, v2_runtime_state CASCADE`.)

- [ ] **Step 2: Run — expect FAIL** (`record_whole_turn_metric` missing; new columns missing).

- [ ] **Step 3: Migration `0029_v2_turn_metrics_whole_turn.py`** (down_revision `0028_v2_effect_sink_applied`):

```python
"""0029 whole-turn metric: v2_turn_metrics gains model_calls/retries/failed/status
+ UNIQUE(job_id) so one idempotent row per job (spec B5). Dedups any pre-existing
duplicate job_id rows (keep newest) before adding the constraint — the table is
best-effort instrumentation, so dropping stale dupes is safe."""
from alembic import op

revision = "0029_v2_turn_metrics_whole_turn"
down_revision = "0028_v2_effect_sink_applied"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE v2_turn_metrics ADD COLUMN IF NOT EXISTS model_calls INT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE v2_turn_metrics ADD COLUMN IF NOT EXISTS retries INT NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE v2_turn_metrics ADD COLUMN IF NOT EXISTS failed BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE v2_turn_metrics ADD COLUMN IF NOT EXISTS status TEXT")
    op.execute("ALTER TABLE v2_turn_metrics ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()")
    op.execute("DELETE FROM v2_turn_metrics a USING v2_turn_metrics b "
               "WHERE a.job_id = b.job_id AND a.id < b.id")
    op.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_v2_turn_metrics_job ON v2_turn_metrics(job_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ux_v2_turn_metrics_job")
    for col in ("model_calls", "retries", "failed", "status", "updated_at"):
        op.execute(f"ALTER TABLE v2_turn_metrics DROP COLUMN IF EXISTS {col}")
```

Also update `db.init_schema()` if it builds `v2_turn_metrics` from code (mirror the new columns + unique index there so the conftest fresh-schema path has them). And update `tests/test_v2_jobs_migration.py`'s head-pin assertion to `"0029_v2_turn_metrics_whole_turn"`.

- [ ] **Step 4: Implement** `jobs_store.record_whole_turn_metric` (upsert) + a `TurnMetrics` accumulator; wire it in `worker.process_job`: create `tm = TurnMetrics(job_id, user_id, lane)` at turn start, `tm.add_call(result["usage"])` after each provider call (planner + responder), and `tm.flush(failed=…, status=…)` at the single terminal point — success return AND each `mark_failed` path (worker.py lines 416/422/534/612/712/751/976/997/1014/1108). Remove the old per-call `record_turn_metric` INSERT at `worker.py:904`. `record_whole_turn_metric` SQL:

```python
def record_whole_turn_metric(job_id, user_id, lane, *, prompt_tokens, completion_tokens,
                             latency_ms, model_calls, retries, failed, status) -> None:
    """One idempotent whole-turn metric per job (spec B5): upsert on job_id so a
    re-drive (redelivery/retry of the same job) REPLACES rather than appends. Covers
    all model calls, retries, and failed turns. Best-effort: never raises to the turn."""
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO v2_turn_metrics (job_id, user_id, lane, prompt_tokens, "
                "completion_tokens, latency_ms, model_calls, retries, failed, status) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (job_id) DO UPDATE SET "
                "prompt_tokens=EXCLUDED.prompt_tokens, completion_tokens=EXCLUDED.completion_tokens, "
                "latency_ms=EXCLUDED.latency_ms, model_calls=EXCLUDED.model_calls, "
                "retries=EXCLUDED.retries, failed=EXCLUDED.failed, status=EXCLUDED.status, "
                "updated_at=now()",
                (job_id, user_id, lane, prompt_tokens, completion_tokens, latency_ms,
                 model_calls, retries, failed, status))
    except Exception as e:  # best-effort instrumentation, never fail the turn
        log.error("[jobs_store] record_whole_turn_metric(%s) failed: %s", job_id, e)
```

- [ ] **Step 5: Run** `python -m pytest tests/test_v2_whole_turn_metric.py tests/test_v2_jobs_store.py tests/test_v2_jobs_migration.py -q` — PASS; single alembic head `0029_v2_turn_metrics_whole_turn`. Leave in working tree.

---

### Task 9: B6 — Load-test gate adapts to whole-turn metric (`scripts/loadtest/`)

**Files:**
- Modify: `scripts/loadtest/collect.py` (add reads of `model_calls`/`failed`; existing latency/token queries keep working), `scripts/loadtest/run_loadtest.py` (simulated processor writes via `record_whole_turn_metric` instead of `record_turn_metric`).
- Test: update `tests/test_loadtest_collect.py` / `tests/test_loadtest_harness_smoke.py`.

**Interfaces:**
- Consumes: Task 8 (`record_whole_turn_metric`, new columns).

- [ ] **Step 1: Failing test** — extend `tests/test_loadtest_collect.py` with a case asserting the collector reads `failed`/`model_calls` (e.g. `collect.failed_turn_count()` returns the count of `failed=true` rows). And update the smoke test to expect the simulated processor to have written whole-turn rows (`model_calls >= 1`).

- [ ] **Step 2: Run — expect FAIL.**

- [ ] **Step 3: Implement** — in `collect.py` add `failed_turn_count()` (`SELECT count(*) FROM v2_turn_metrics WHERE failed`) and include it in the report; in `run_loadtest.py`'s `build_simulated_processor`, replace the `jobs_store.record_turn_metric(...)` call with `jobs_store.record_whole_turn_metric(job_id=…, …, model_calls=1, retries=0, failed=False, status="ok")`.

- [ ] **Step 4: Run** `python -m pytest tests/test_loadtest_collect.py tests/test_loadtest_harness_smoke.py -q` — PASS. Leave in working tree.

---

### Task 10: Acceptance codec round-trip (4 providers) + manual live probe

**Files:**
- Create: `tests/test_provider_tools_acceptance.py` (CI — the @sxysun acceptance proof at the codec boundary), `scripts/provider_probe/probe.py` + `scripts/provider_probe/__init__.py` (manual, NOT in CI).
- Test: `tests/test_provider_tools_acceptance.py`.

**Interfaces:** Consumes all Task 3-6 codecs.

- [ ] **Step 1: Acceptance test** `tests/test_provider_tools_acceptance.py` — for EACH of the 4 wires, in ONE parametrized test: (a) encode 2 `ToolSpec`s → assert the wire tools shape; (b) decode a canned 2-tool-call response → 2 `ToolCall`s with distinct ids; (c) build `id_to_name` from the decoded calls; (d) encode 2 `ToolResult`s (one per decoded id) → assert BOTH results are present and keyed by the right id/name in the wire shape. This is the "四类 provider 一次返两个 tool_calls 并按 call_id 收两结果" acceptance.

```python
# parametrize over ("openai_chat","openai_responses","anthropic","gemini")
# each entry supplies: encode_tools, decode_calls, encode_results, a canned 2-call body,
# and the expected wire keys. Assert len(decoded)==2, ids distinct, and both results encoded.
```

- [ ] **Step 2: Run — expect FAIL** then implement the parametrized fixtures → **PASS**.

- [ ] **Step 3: Manual live probe** `scripts/provider_probe/probe.py` — a `python -m scripts.provider_probe.probe` script that, given real BYOK keys via env (`PROBE_ANTHROPIC_KEY`, etc.), calls each configured provider once with 2 `ToolSpec`s and a prompt that forces 2 tool calls, then prints whether it got 2 `tool_calls` and round-trips 2 `tool_results` by id. Module docstring must state: MANUAL, needs real keys, NOT run in CI (mirror `scripts/loadtest/run_loadtest.py`'s manual-caveat docstring). Provide a `tests/test_provider_probe_smoke.py` that only imports the module + asserts `build_probe_tools()` returns 2 ToolSpecs (no network).

- [ ] **Step 4: Run** `python -m pytest tests/test_provider_tools_acceptance.py tests/test_provider_probe_smoke.py -q` — PASS.

- [ ] **Step 5: Full suite** `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py` — 8-baseline, zero new, single alembic head `0029`. Leave in working tree; do NOT commit.

---

## Self-Review

- **Spec coverage:** B1→Task 1; B4→Task 2; B2 (4 wires)→Tasks 3/4/5 + threaded in Task 6; B3 native async→Task 7; B5 whole-turn metric→Task 8; B6 load-test→Task 9; acceptance (4 providers, 2 tool_calls, 2 results by call_id) + manual live probe→Task 10. All six components + acceptance covered.
- **Additive safety:** dict return preserved; `tool_calls` always present; `usage` normalized in place (Task 2 keeps existing responder read working). No caller passes `tools` (transport-only boundary).
- **Type consistency:** decoded tool-call dict shape `{id,name,args,args_raw,args_ok}` is identical across Tasks 3/4/5/6 and matches `ProviderResponse.from_result` (Task 1). `record_whole_turn_metric` signature identical in Tasks 8/9. `_normalize_usage` return keys identical in Tasks 2/6/7/8.
- **Ordering/deps:** 1(types)→2(usage)→3/4/5(codecs)→6(thread tools)→7(async)→8(metric)→9(loadtest)→10(acceptance+probe). Each independently testable.
- **NO-COMMIT:** every task ends "leave in working tree; do not commit."
