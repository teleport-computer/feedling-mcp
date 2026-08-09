# Runtime V2 Fable 5 / Opus 4.8 Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Runtime V2 chat and native tool loops compatible with Fable 5 and Opus 4.8 without changing Runtime V1 or other models' behavior.

**Architecture:** Runtime V2 will decide at prompt-build time whether a model may receive the mandatory visible self-thinking instruction; Fable 5 will use plain visible answers. The provider-neutral tool loop will keep memory discovery schemas visible after their first real execution, while the existing completed-tool guard continues to prevent duplicate dispatch.

**Tech Stack:** Python 3.11, pytest, Runtime V2 provider-neutral tool loop, Anthropic Messages native `ToolExchange` encoding, public docs changelog.

## Global Constraints

- Modify Runtime V2 only; do not edit Runtime V1 behavior.
- Fable 5 may omit the `<think>` bubble, but ordinary text and tool calls must remain available.
- Opus 4.8 keeps historical tool schemas visible; a discovery tool still executes at most once per turn.
- Do not add provider calls, database schema, public API fields, or model-specific branches inside the provider transport.
- Preserve existing empty-response recovery and provider-health classification.

---

### Task 1: Disable mandatory self-thinking for Fable 5 in V2 chat

**Files:**
- Modify: `tests/test_v2_worker_tool_loop.py`
- Modify: `backend/model_api_runtime/v2/context.py:211-219`
- Modify: `backend/model_api_runtime/v2/worker.py:11948`

**Interfaces:**
- Consumes: `provider_client.ProviderConfig` and `core.self_thinking.enabled()`.
- Produces: `context.chat_system_prompt(provider_config=None) -> str`, with Fable 5 returning the stable chat prompt without `self_thinking.INSTRUCTION`.

- [ ] **Step 1: Write the failing worker-level regression test**

Add a Fable config beside `_BYOK`:

```python
_FABLE_BYOK = provider_client.ProviderConfig(
    provider="anthropic",
    model="claude-fable-5",
    api_key="sk-user-byok",
    base_url="",
)
```

Import `self_thinking` from `core`, then add this test near the existing self-thinking worker test:

```python
def test_fable_chat_omits_mandatory_self_thinking_prompt(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)
    uid = "u_toolloop_fable_plain_reply"
    conftest.seed_user(uid)
    _reset(uid)
    jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("w-fable-plain")
    _patch_real_write(monkeypatch)
    calls = _script_provider(monkeypatch, [_text_round("Fable plain reply")])
    deps = _deps(messages=[{
        "id": "m1", "ts": 10.0, "role": "user", "content": "hi",
    }])

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_FABLE_BYOK,
        api_key=None, runtime_token="rt",
    ))

    assert status == "completed"
    system_text = "\n".join(
        str(message.get("content") or "")
        for message in calls[0]["messages"]
        if isinstance(message, dict) and message.get("role") == "system"
    )
    assert self_thinking.INSTRUCTION not in system_text
    assert _bubbles(uid)[-1] == "Fable plain reply"
```

Also extend `test_self_thinking_on_suppresses_native_reasoning` with the preserved
non-Fable behavior:

```python
system_text = "\n".join(
    str(message.get("content") or "")
    for message in calls[0]["messages"]
    if isinstance(message, dict) and message.get("role") == "system"
)
assert self_thinking.INSTRUCTION in system_text
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```bash
uv run pytest tests/test_v2_worker_tool_loop.py::test_fable_chat_omits_mandatory_self_thinking_prompt -q
```

Expected: the Fable test FAILS because the current V2 chat builder appends
`self_thinking.INSTRUCTION` for every model; the existing non-Fable test passes
and proves the preserved branch before implementation.

- [ ] **Step 3: Implement the minimal V2 model capability check**

In `backend/model_api_runtime/v2/context.py`, add:

```python
def _supports_mandatory_self_thinking(provider_config: Any) -> bool:
    if provider_config is None:
        return True
    model = str(getattr(provider_config, "model", "") or "").strip().lower()
    return "claude-fable-5" not in model


def chat_system_prompt(provider_config: Any = None) -> str:
    from core import self_thinking

    if self_thinking.enabled() and _supports_mandatory_self_thinking(provider_config):
        return CHAT_SYSTEM_PROMPT + self_thinking.INSTRUCTION
    return CHAT_SYSTEM_PROMPT
```

Update the production chat builder in `worker.py`:

```python
system_prompt=context.chat_system_prompt(provider_config),
```

This remains V2-only. `split_thinking()` already preserves an ordinary Fable answer through its `ABSENT` branch.

- [ ] **Step 4: Run the Fable regression and existing self-thinking test**

Run:

```bash
uv run pytest \
  tests/test_v2_worker_tool_loop.py::test_fable_chat_omits_mandatory_self_thinking_prompt \
  tests/test_v2_worker_tool_loop.py::test_self_thinking_on_suppresses_native_reasoning -q
```

Expected: 2 passed. The first proves Fable receives no mandatory visible-CoT instruction; the second proves existing models still suppress native reasoning when self-thinking is enabled.

- [ ] **Step 5: Commit Task 1**

```bash
git add backend/model_api_runtime/v2/context.py \
  backend/model_api_runtime/v2/worker.py \
  tests/test_v2_worker_tool_loop.py
git commit -m "fix(v2): allow Fable plain replies"
```

---

### Task 2: Keep memory discovery schemas visible across native tool rounds

**Files:**
- Modify: `tests/test_v2_tool_loop.py:984-1010`
- Modify: `backend/model_api_runtime/v2/tool_loop.py:794-802`

**Interfaces:**
- Consumes: `completed_memory_discovery_tools`, the provider-facing `tools` list, and existing `repeated_memory_calls` dispatch classification.
- Produces: stable provider-facing schemas across rounds while retaining one-real-dispatch-per-discovery-mode semantics.

- [ ] **Step 1: Change the existing test into the failing compatibility contract**

Rename `test_each_memory_discovery_mode_is_offered_only_until_its_first_result` to `test_memory_discovery_schema_remains_visible_after_first_result` and change its assertions to:

```python
first_names = {spec.name for spec in provider.calls[0]["tools"]}
second_names = {spec.name for spec in provider.calls[1]["tools"]}
assert {"memory_index", "memory_search"}.issubset(first_names)
assert {"memory_index", "memory_search"}.issubset(second_names)
assert outcome.final_text == "direct answer"
```

This catches the production bug: a native `ToolExchange` referencing `memory_index` must not be followed by a request whose current schema directory denies that tool exists.

- [ ] **Step 2: Run the renamed test and verify RED**

Run:

```bash
uv run pytest tests/test_v2_tool_loop.py::test_memory_discovery_schema_remains_visible_after_first_result -q
```

Expected: FAIL because `memory_index` is absent from the second provider request.

- [ ] **Step 3: Remove only the provider-facing schema filter**

Delete the `if tools is not None and completed_memory_discovery_tools:` block that rebuilds `tools` without completed discovery names.

Do not remove or change:

```python
completed_memory_discovery_tools.update(...)
```

or the `repeated_discovery` / `repeated_memory_calls` handling. Those paths are the execution fence that prevents duplicate reads.

- [ ] **Step 4: Run schema continuity and duplicate-dispatch tests**

Run:

```bash
uv run pytest \
  tests/test_v2_tool_loop.py::test_memory_discovery_schema_remains_visible_after_first_result \
  tests/test_v2_tool_loop.py::test_repeated_memory_discovery_reuses_prior_result_without_dispatch \
  tests/test_v2_tool_loop.py::test_same_batch_duplicate_memory_discovery_dispatches_only_once -q
```

Expected: 3 passed. The first proves schema continuity; the latter two prove the real executor still runs once.

- [ ] **Step 5: Run Anthropic provider-native wire tests**

Run:

```bash
uv run pytest tests/test_provider_tools_anthropic.py tests/test_provider_tools_wire.py -q
```

Expected: all pass, proving the unchanged `ToolExchange` encoding remains assistant `tool_use` immediately followed by user `tool_result`.

- [ ] **Step 6: Commit Task 2**

```bash
git add backend/model_api_runtime/v2/tool_loop.py tests/test_v2_tool_loop.py
git commit -m "fix(v2): preserve native tool schemas across rounds"
```

- [ ] **Step 7: Preserve referenced schemas through prompt-frontier degradation**

Code review identified a second removal path: the prompt frontier may omit the
entire optional catalog after a native exchange increases the next round's
prompt size. Split completed memory-discovery schemas into a required
`required_tool_schemas` component and keep the remaining catalog optional. When
the optional component is omitted, send only those required schemas. Add tests
covering same-request `ToolExchange` + schema coexistence, required-schema
budgeting, and fail-closed admission when required content cannot fit.

Also tighten Fable detection to an exact model basename match and add negative
boundary cases so similarly named models retain the existing self-thinking
behavior.

- [ ] **Step 8: Preserve schemas on the bounded terminal round**

Deployed E2E showed Anthropic Opus 4.8 could consume several valid tool rounds
and then return empty on the final `tools=None` request, recreating the same
native-history/schema mismatch. For wires with a documented no-tool choice,
retain only schemas referenced by the transcript, count them as required, and
send `tool_choice=none`. Encode Anthropic's native `{"type":"none"}` form and
reject any terminal tool call locally without dispatch as defense in depth.

---

### Task 3: Document and verify the complete V2 compatibility change

**Files:**
- Modify: `docs-site/content/docs/changelog.mdx`
- Verify: `backend/model_api_runtime/v2/context.py`
- Verify: `backend/model_api_runtime/v2/tool_loop.py`
- Verify: `backend/model_api_runtime/v2/worker.py`

**Interfaces:**
- Consumes: the two independently green fixes from Tasks 1 and 2.
- Produces: user-visible changelog evidence and a clean V2 regression result.

- [ ] **Step 1: Add the Unreleased changelog entry**

Add this bullet at the top of `## Unreleased`:

```markdown
- **Runtime V2 now supports stricter Anthropic-compatible models during chat and
  multi-step tool use.** Fable 5 is allowed to answer without the optional
  self-thinking bubble instead of being forced into a visible chain-of-thought
  format that it refuses. Native tool definitions also remain visible while
  their matching `tool_use` / `tool_result` history is in the turn, so models
  such as Opus 4.8 can continue a real tool workflow without treating earlier
  results as fabricated. Repeated memory discovery is still executed at most
  once per mode.
```

- [ ] **Step 2: Run the focused V2 suite**

Run:

```bash
uv run pytest \
  tests/test_v2_context.py \
  tests/test_v2_tool_loop.py \
  tests/test_v2_worker_tool_loop.py \
  tests/test_provider_tools_anthropic.py \
  tests/test_provider_tools_wire.py -q
```

Expected: all tests pass with no failures.

- [ ] **Step 3: Run static diff checks**

Run:

```bash
git diff --check
git status --short
```

Expected: no whitespace errors; only the intended changelog change is uncommitted after Tasks 1 and 2 commits.

- [ ] **Step 4: Verify the public documentation site**

Run:

```bash
cd docs-site
npm run types:check
npm run lint
npm run build
```

Expected: all three commands exit successfully. OpenAPI regeneration is not
required because this change does not alter an HTTP contract.

- [ ] **Step 5: Commit documentation**

```bash
git add docs-site/content/docs/changelog.mdx
git commit -m "docs: note V2 model compatibility fixes"
```

- [ ] **Step 6: Run final branch verification**

Run:

```bash
uv run pytest \
  tests/test_v2_context.py \
  tests/test_v2_tool_loop.py \
  tests/test_v2_worker_tool_loop.py \
  tests/test_provider_tools_anthropic.py \
  tests/test_provider_tools_wire.py -q
git diff --check origin/test...HEAD
git status --short --branch
```

Expected: all tests pass, the range diff has no whitespace errors, and the worktree is clean on `fix/runtime-v2-fable-opus-compat`.
