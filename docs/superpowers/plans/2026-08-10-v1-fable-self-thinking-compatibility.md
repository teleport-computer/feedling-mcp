# V1 Fable 5 Self-Thinking Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Runtime V1 Fable 5 foreground turns from receiving the mandatory visible `<think>` instruction while preserving pi native reasoning and every non-Fable behavior.

**Architecture:** Add one exact-basename model capability predicate in the Runtime V1 resident consumer and use it at the existing foreground self-thinking injection seam. Exercise the real foreground message assembly path so the regression proves the dispatched prompt changes, not merely a helper return value.

**Tech Stack:** Python 3.11, pytest, Runtime V1 resident consumer

## Global Constraints

- Match only basename `claude-fable-5`, including `anthropic/claude-fable-5`.
- Do not match `claude-fable-50` or `foo-claude-fable-5-bar`.
- Preserve pi `--thinking`, provider tests, Runtime V2, background lanes, and non-Fable behavior.
- Do not modify or commit unrelated shared-worktree changes.

---

### Task 1: Gate V1 mandatory self-thinking by exact Fable model identity

**Files:**
- Modify: `tools/chat_resident_consumer.py` at the foreground self-thinking injection
- Test: `tests/test_chat_resident_consumer.py`

**Interfaces:**
- Consumes: `AGENT_RUNTIME_METADATA: dict[str, Any]` and `core.self_thinking.enabled() -> bool`
- Produces: `_supports_mandatory_self_thinking_v1() -> bool`

- [ ] **Step 1: Write the failing foreground-path regression test**

Add a parametrized test near existing `_process_messages` foreground tests. Patch `AGENT_RUNTIME_METADATA`, enable self-thinking, capture the argument passed to `call_agent`, and assert these boundaries:

```python
@pytest.mark.parametrize(
    ("model", "expects_instruction"),
    [
        ("claude-fable-5", False),
        ("anthropic/claude-fable-5", False),
        ("claude-fable-50", True),
        ("foo-claude-fable-5-bar", True),
    ],
)
def test_v1_foreground_self_thinking_skips_only_exact_fable(
    monkeypatch, model, expects_instruction
):
    monkeypatch.setattr(
        crc, "AGENT_RUNTIME_METADATA", {"model": model, "provider": "openrouter"}
    )
    monkeypatch.setenv("SELF_THINKING_ENABLED", "true")
    captured = {}

    def _call_agent(content, **kwargs):
        captured["content"] = content
        return "ok"

    monkeypatch.setattr(crc, "call_agent", _call_agent)
    monkeypatch.setattr(crc, "post_reply", lambda *args, **kwargs: {})
    crc._process_messages([_make_msg(role="user", content="hello", ts=2000.0)])

    from core import self_thinking

    present = self_thinking.INSTRUCTION.strip() in captured["content"]
    assert present is expects_instruction
```

Adapt only fixture details needed by the repository's existing helpers; keep the assertion on real content passed to `call_agent`.

- [ ] **Step 2: Run the new test and verify RED**

Run:

```bash
uv run pytest tests/test_chat_resident_consumer.py::test_v1_foreground_self_thinking_skips_only_exact_fable -q
```

Expected: the Fable cases fail because V1 currently injects the instruction for every model; the near-match cases pass.

- [ ] **Step 3: Implement the minimal exact-basename predicate**

```python
def _supports_mandatory_self_thinking_v1() -> bool:
    model = str(AGENT_RUNTIME_METADATA.get("model") or "").strip().lower()
    return model.rsplit("/", 1)[-1] != "claude-fable-5"
```

Change only the injection condition:

```python
if _self_thinking_v1.enabled() and _supports_mandatory_self_thinking_v1():
    content = f"{_self_thinking_v1.INSTRUCTION.strip()}\n\n{content}"
```

- [ ] **Step 4: Run the focused test and verify GREEN**

Run the Step 2 command again. Expected: all four parameter cases pass.

- [ ] **Step 5: Run focused V1/V2 compatibility regressions**

```bash
uv run pytest \
  tests/test_chat_resident_consumer.py::test_v1_foreground_self_thinking_skips_only_exact_fable \
  tests/test_v2_context.py::test_chat_system_prompt_omits_self_thinking_for_namespaced_fable \
  tests/test_v2_context.py::test_chat_system_prompt_keeps_self_thinking_for_non_fable_boundaries \
  tests/test_agent_runtime_spawners.py::test_pi_default_cli_cmd_threads_thinking_level \
  -q
```

Expected: all pass, proving V1/V2 boundary parity and unchanged pi native reasoning.

- [ ] **Step 6: Run owning modules and diff checks**

```bash
uv run pytest tests/test_chat_resident_consumer.py tests/test_v2_context.py -q
git diff --check -- tools/chat_resident_consumer.py tests/test_chat_resident_consumer.py
```

Expected: both modules pass and `git diff --check` emits no output. Record any pre-existing environmental failure separately.

- [ ] **Step 7: Commit only after the repository merge is resolved**

Stage only the implementation, tests, spec, and plan, then commit `fix(v1): skip forced thinking for Fable 5`. Until the merge is resolved, leave these changes uncommitted rather than including unrelated staged work.
