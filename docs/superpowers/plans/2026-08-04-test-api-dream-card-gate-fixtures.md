# Test API Dream Card-Gate Fixture Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the `test_api.py (multi-tenant)` CI job by updating stale Dream card-gate fixtures to satisfy the production `rationale` contract while preserving their content-validation coverage.

**Architecture:** This is a test-only compatibility repair. Production continues to reject rationale-free destructive Dream proposals; the affected fixtures gain valid rationales so they reach the placeholder-content gate they are designed to exercise.

**Tech Stack:** Python 3.12, pytest, Ruff, GitHub Actions.

## Global Constraints

- Modify `tests/test_card_text_gate.py` only for the behavior repair.
- Do not change `parse_dream_consolidations()` or weaken the rationale requirement.
- Keep all existing assertions unchanged.
- Do not remove or deselect tests from CI.
- Do not change public APIs or public documentation.

---

## File Map

- Modify `tests/test_card_text_gate.py`: add substantive `rationale` values to Dream fixtures that are intended to test placeholder-content rejection.

### Task 1: Repair Dream Card-Gate Fixtures

**Files:**
- Modify: `tests/test_card_text_gate.py:150-216`
- Test: `tests/test_card_text_gate.py`

**Interfaces:**
- Consumes: `parse_dream_consolidations(raw: str, *, strict: bool = True) -> tuple[list[dict], list[str], str | None]`.
- Produces: CI fixtures that satisfy the Dream proposal structure and still expose invalid summary/content behavior.

- [ ] **Step 1: Verify the existing red state**

Run:

```bash
../../.venv-test/bin/python -m pytest \
  tests/test_card_text_gate.py::test_dream_bounces_the_two_real_garbage_cards \
  tests/test_card_text_gate.py::test_dream_relaxed_pass_drops_dirty_keeps_clean \
  tests/test_card_text_gate.py::test_all_dirty_after_retry_never_looks_like_a_clean_noop \
  -q
```

Expected: three failures because each proposal is dropped for missing `rationale` before `card_text_rejection()` runs, producing `err is None`.

- [ ] **Step 2: Add valid rationale fields to the affected fixtures**

Apply these exact fixture rules in `tests/test_card_text_gate.py`:

```python
# _CARD_A and _CARD_B
"rationale": "同一张卡补充事实"

# both rows in test_dream_relaxed_pass_drops_dirty_keeps_clean
"rationale": "同一张卡补充事实"

# all_dirty in test_all_dirty_after_retry_never_looks_like_a_clean_noop
"rationale": "同一张卡补充事实"
```

Encode the field inside each existing JSON string immediately after `card_ids`. Do not modify assertions or production code.

- [ ] **Step 3: Verify the three failing tests are green**

Run the Step 1 command again.

Expected: `3 passed`.

- [ ] **Step 4: Run the complete card-text gate suite**

Run:

```bash
.venv-test/bin/python -m pytest tests/test_card_text_gate.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run the exact CI Round 3 regression suite**

Run with `FEEDLING_TEST_PG` pointing to the local test Postgres:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  ../../.venv-test/bin/python -m pytest \
  tests/test_proactive_store_v2.py \
  tests/test_proactive_runtime_v2.py \
  tests/test_proactive_scheduled_wake_v2.py \
  tests/test_proactive_dashboard_v2.py \
  tests/test_proactive_observability_v2.py \
  tests/test_proactive_tool_executor_v2.py \
  tests/test_perception_ingress_v2.py \
  tests/test_ios_perception_contract_v2.py \
  tests/test_perception.py \
  tests/test_proactive_jobs.py \
  tests/test_proactive_gate_eval.py \
  tests/test_screen_caption_backend.py \
  tests/test_screen_caption_flag.py \
  tests/test_enclave_frame_caption.py \
  tests/test_enclave_visual_plaintext.py \
  tests/test_proactive_agent_protocol_v2.py \
  tests/test_runtime_v2_default_flag.py \
  tests/test_capture_prompt_v1.py \
  tests/test_dream_prompt_v1.py \
  tests/test_card_text_gate.py \
  tests/test_card_user_referent.py \
  tests/test_pi_mcp_bridge.py \
  -q
```

Expected: all tests pass.

- [ ] **Step 6: Run static checks**

Run:

```bash
ruff check tests/test_card_text_gate.py
git diff --check
git status --short
```

Expected: Ruff and diff checks pass; only the intended test file and this plan are changed after the specification commit.

- [ ] **Step 7: Commit the test-only repair**

```bash
git add tests/test_card_text_gate.py docs/superpowers/plans/2026-08-04-test-api-dream-card-gate-fixtures.md
git commit -m "test(dream): satisfy rationale in card-gate fixtures"
```

- [ ] **Step 8: Integrate and push `test`**

Fetch `origin/test`, verify it is an ancestor of the repair branch or merge it without force, then push the verified result to `origin/test`. Confirm the new CI run's `test_api.py (multi-tenant)` job passes before cleanup.
