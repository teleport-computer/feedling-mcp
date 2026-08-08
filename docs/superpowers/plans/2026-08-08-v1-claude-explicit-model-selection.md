# Runtime V1 Claude Explicit Model Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make managed Runtime V1 Claude turns explicitly select the active route model, rotate sessions on model provenance changes, and reject replies produced by a different model.

**Architecture:** Keep the Claude Code driver. Thread the route model into both managed Claude command templates as `--model <id>`, export provider/model as non-secret runtime metadata, and use the existing entry-signature mechanism to rotate sessions. Before accepting a successful Claude result or persisting its session ID, compare structured assistant/modelUsage metadata with the configured model and raise `model_mismatch` when they differ.

**Tech Stack:** Python 3, pytest, Claude Code JSON/stream-json protocol, resident consumer.

## Global Constraints

- Do not modify pi routing/configuration or Runtime V2 execution.
- Keep `ANTHROPIC_MODEL` for compatibility; do not configure `--fallback-model`.
- Preserve operator-provided `cli_cmd`; inject `--model` only into managed defaults.
- Never infer actual model identity from natural-language text.
- A structured configured/actual mismatch is non-retryable, clears the resident session, and publishes no reply.
- Register a new public error class in both consumer and backend catalogs.

---

### Task 1: Explicit Model Argument and Session Provenance

**Files:**
- Modify: `backend/agent_runtime/spawners.py:640-815`
- Modify: `backend/agent_runtime/spawners.py:1058-1160`
- Test: `tests/test_agent_runtime_spawners.py`

**Interfaces:**
- Consumes: roster `driver`, `provider`, `model`, and optional `cli_cmd`.
- Produces: managed Claude argv containing one `--model`; env keys `FEEDLING_AGENT_PROVIDER` and `FEEDLING_AGENT_MODEL_ID` consumed by existing `AGENT_RUNTIME_METADATA` and `_agent_entry_signature()`.

- [ ] **Step 1: Write failing argv tests**

```python
def test_claude_default_cli_cmd_selects_exact_route_model():
    argv = shlex.split(
        spawners._default_cli_cmd("claude", "/h", model="claude-fable-5")
    )
    assert argv[argv.index("--model") + 1] == "claude-fable-5"
    assert "--fallback-model" not in argv


def test_claude_thinking_cli_cmd_selects_exact_route_model():
    argv = shlex.split(
        spawners._default_thinking_claude_cmd(
            "/h", model="claude-opus-4-8"
        )
    )
    assert argv[argv.index("--model") + 1] == "claude-opus-4-8"


def test_claude_default_cli_cmd_quotes_model_as_one_token():
    model = "custom model alias"
    argv = shlex.split(spawners._default_cli_cmd("claude", "/h", model=model))
    assert argv[argv.index("--model") + 1] == model
```

Add a `consumer_env()` test asserting the final command, `ANTHROPIC_MODEL`, `FEEDLING_AGENT_PROVIDER`, and `FEEDLING_AGENT_MODEL_ID` all carry the literal `claude-fable-5` route fact.

- [ ] **Step 2: Verify RED**

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_agent_runtime_spawners.py \
  -k 'claude_default_cli_cmd_selects_exact_route_model or claude_thinking_cli_cmd_selects_exact_route_model or claude_default_cli_cmd_quotes_model_as_one_token or consumer_env_pins_claude_route_model' -q
```

Expected: failures because Claude templates omit `--model`, the thinking builder rejects `model`, and metadata keys are absent.

- [ ] **Step 3: Implement the minimum spawner change**

For both Claude builders normalize and quote the model:

```python
model_id = (model or "").strip()
model_part = f"--model {shlex.quote(model_id)} " if model_id else ""
```

Insert `model_part` before `{mcp}`. Change `_default_thinking_claude_cmd()` to accept `model: str = ""`. In `consumer_env()`, compute normalized `provider`/`model` once, pass model to both managed builders, then export:

```python
if provider:
    env["FEEDLING_AGENT_PROVIDER"] = provider
if model:
    env["FEEDLING_AGENT_MODEL_ID"] = model
```

Do not rewrite an operator `cli_cmd`. Continue exporting `ANTHROPIC_MODEL` for Claude.

- [ ] **Step 4: Verify GREEN and regression suite**

Run the Step 2 command, then:

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_agent_runtime_spawners.py -q
```

Expected: zero failures.

- [ ] **Step 5: Mutation check**

Temporarily remove `model_part` from each Claude builder in turn and confirm its corresponding test fails. Restore production code and rerun the focused tests green.

- [ ] **Step 6: Commit**

```bash
git add backend/agent_runtime/spawners.py tests/test_agent_runtime_spawners.py
git commit -m "fix: pin managed Claude route model in CLI"
```

Task 1 also activates existing session rotation: `_agent_entry_signature()` already includes `AGENT_RUNTIME_METADATA`; the missing producer was `consumer_env()`.

---

### Task 2: Structured Actual-Model Guard

**Files:**
- Modify: `tools/chat_resident_consumer.py:4882-4935`
- Modify: `tools/chat_resident_consumer.py:7035-7505`
- Modify: `tools/chat_resident_consumer.py:734-930`
- Modify: `backend/notices/catalog.py`
- Modify: `docs/API_ERRORS.md`
- Test: `tests/test_chat_resident_consumer.py`
- Test: `tests/test_catalog_consumer_parity.py`

**Interfaces:**
- Produces: `_claude_actual_models(raw: str) -> set[str]` and `_assert_claude_model_matches(raw: str) -> None`.
- Consumes: structured `assistant.message.model`, terminal `modelUsage` keys, and `AGENT_RUNTIME_METADATA["model"]`.

- [ ] **Step 1: Write failing structured parser tests**

```python
def test_claude_actual_models_reads_assistant_and_model_usage():
    raw = "\n".join([
        json.dumps({
            "type": "assistant",
            "message": {
                "model": "claude-fable-5",
                "content": [{"type": "text", "text": "ok"}],
            },
        }),
        json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "ok",
            "modelUsage": {
                "claude-fable-5": {"inputTokens": 1, "outputTokens": 1}
            },
        }),
    ])
    assert crc._claude_actual_models(raw) == {"claude-fable-5"}
```

Add a `modelUsage`-only fixture. Add a fixture whose reply text says “I am Fable” but has no structured model field and assert the returned set is empty.

- [ ] **Step 2: Verify parser RED**

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_chat_resident_consumer.py -k 'claude_actual_models' -q
```

Expected: fail because the helper does not exist.

- [ ] **Step 3: Implement the parser and verify GREEN**

Iterate `_json_objects_from_cli_output(raw)`. Add normalized non-empty `assistant.message.model` values and non-empty string keys of result `modelUsage` dictionaries. Return a lowercase set; never inspect reply text.

Run Step 2 again. Expected: all parser tests pass.

- [ ] **Step 4: Write failing call-path behavior tests**

Use real `call_agent_cli()` with only the subprocess boundary replaced. Set:

```python
monkeypatch.setattr(crc, "AGENT_RUNTIME_METADATA", {
    "provider": "anthropic",
    "model": "claude-fable-5",
    "input_modalities": [],
    "input_modalities_source": "explicit",
})
```

Return a successful Claude result with `modelUsage={"claude-opus-4-8": ...}` and assert `model_mismatch` is raised instead of returning its text. A matching Fable fixture must return its reply. Assert mismatch clears the saved session before it can be resumed.

- [ ] **Step 5: Verify behavior RED**

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_chat_resident_consumer.py \
  -k 'claude_model_mismatch or claude_matching_model' -q
```

Expected: mismatch test fails because the Opus reply is currently accepted.

- [ ] **Step 6: Implement fail-closed mismatch validation**

Rules:

- Run only for Claude commands with a configured model.
- Structured evidence absent: log a bounded warning and preserve current behavior.
- Full configured ID: require exact lowercased membership.
- Aliases `fable`, `opus`, `sonnet`, `haiku`: require an actual ID containing the family.
- Mismatch: `_clear_agent_session_id(...)`, then raise:

```python
RuntimeError(
    "model_mismatch: configured=claude-fable-5 actual=claude-opus-4-8"
)
```

Invoke after return code zero and before observed session persistence or reply extraction.

- [ ] **Step 7: Register the public error class**

Consumer and backend catalog values:

```text
error_class: model_mismatch
blame: system
user_text: 当前运行时没有成功加载所选模型，请重新选择模型或稍后重试。
```

Update `CONSUMER_ERROR_CLASSES`, `backend/notices/catalog.py`, the parity test's hard-coded classifier branches, and `docs/API_ERRORS.md`.

- [ ] **Step 8: Verify GREEN, parity, and mutation behavior**

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_chat_resident_consumer.py tests/test_catalog_consumer_parity.py \
  -k 'claude_actual_models or claude_model_mismatch or claude_matching_model or catalog or classify' -q
```

Temporarily disable the validation call and confirm the mismatch test fails by returning the Opus reply. Restore it and rerun green.

- [ ] **Step 9: Run directly affected suites and static checks**

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_agent_runtime_spawners.py tests/test_chat_resident_consumer.py \
  tests/test_catalog_consumer_parity.py -q
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pyflakes \
  backend/agent_runtime/spawners.py backend/notices/catalog.py tools/chat_resident_consumer.py
```

Expected: zero test failures and no new static warnings.

- [ ] **Step 10: Commit**

```bash
git add tools/chat_resident_consumer.py backend/notices/catalog.py docs/API_ERRORS.md \
  tests/test_chat_resident_consumer.py tests/test_catalog_consumer_parity.py
git commit -m "fix: reject Claude model mismatches"
```

---

### Task 3: Integration Verification and Handoff

**Files:**
- Verify only; update incident evidence later only if deployment facts change.

**Interfaces:**
- Consumes: Tasks 1-2.
- Produces: locally verified branch plus test-environment E2E checklist.

- [ ] **Step 1: Run the full consumer-coupled suite required by TESTING.md**

```bash
test_files=$(grep -l -E 'chat_resident_consumer' tests/test_*.py)
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest $test_files -q
```

Expected: zero newly introduced failures.

- [ ] **Step 2: Verify diff scope**

```bash
git diff --check
git status --short
git diff --stat test...HEAD
```

Confirm no pi or Runtime V2 execution file changed.

- [ ] **Step 3: Prepare deployment evidence checklist**

After a reviewed merge into `test` and runner deployment:

1. release commit equals deployed commit;
2. managed Fable argv contains `--model claude-fable-5`;
3. fresh Fable session completes reply plus memory-write/delete;
4. structured actual model is `claude-fable-5` for three turns;
5. Opus 4.8 → Fable 5 → Opus 5 → Sonnet 4.6 rotates session IDs;
6. diagnostic memory is deleted and original user state restored.
