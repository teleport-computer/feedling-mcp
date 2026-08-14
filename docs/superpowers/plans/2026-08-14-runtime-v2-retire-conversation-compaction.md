# Runtime V2 Conversation Compaction Retirement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove Runtime V2 conversation compaction from every active prompt and history-search path so Chat and wake depend only on the latest usable `MEMORY`/`USER` profile plus bounded complete recent turns, while old maintenance jobs drain harmlessly during a rolling deployment.

**Architecture:** Chat reads up to 40 complete recent turns and wake reads up to 16, partitions them into required active-turn messages and optional older turns, and lets the existing model-aware prompt frontier discard only optional groups. Profile selection returns last-known-good decrypted `MEMORY`/`USER` fields or an empty profile without consulting a conversation summary. Explicit history search remains available but scans only the frozen raw encrypted Chat snapshot. Phase one preserves legacy database schema and a maintenance-job tombstone; schema deletion is deferred to a later PR.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, PostgreSQL, pytest, Runtime V2 enclave/provider adapters, Ruff/type checks through the existing repository scripts, MDX documentation.

## Global Constraints

- Work only in `.worktrees/v2-deterministic-only-coverage` on `refactor/v2-deterministic-only-coverage`; keep PR #187 targeted at `test`.
- Follow test-driven development: add or change one focused test, observe the intended failure, make the smallest production change, and rerun the focused test before proceeding.
- Preserve raw encrypted Chat, Capture, Memory Garden, Profile, Dream, `history_fetch`, attachment replay, tool admission, provider routing, and prompt-frontier budget behavior.
- Do not drop or mutate legacy summary/frontier tables in phase one. Do not add a data cleanup migration.
- New workers must never enqueue maintenance jobs. An already queued/running maintenance job must complete without reading Chat, decrypting content, resolving a provider, writing a summary, or scheduling a successor.
- A Profile lag/failure cannot block Chat or wake. Use retained decryptable `MEMORY`/`USER` fields when present; otherwise use an empty profile.
- Automatic prompt replay must contain complete turns only. Chat targets 40 turns; wake targets 16. Older optional groups may be omitted for model budget, but required active-turn messages may not be omitted.
- Keep all metrics content-free. Retain `prompt_frontier` metrics because they describe model context admission; remove only conversation-summary/frontier/coverage metrics.
- Existing summary-aware history cursors may fail with stable `cursor_invalid`; do not introduce a second cursor compatibility parser.
- Use the repository-local test interpreter and PostgreSQL fixture:

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest ...
  ```

---

## Task 1: Make Profile Selection Summary-Free and Last-Known-Good

**Files:**

- Modify: `backend/model_api_runtime/v2/profile_store.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `tests/test_v2_profile_prompt.py`

**Interfaces:**

- Consumes: the current Profile blob row and its encrypted retained `MEMORY`/`USER` envelopes.
- Produces: `ProfilePromptSelection(memory, user, state, memory_chars, user_chars, age_seconds)` with no summary field.
- Invariant: any Profile state or read/decrypt error returns either retained usable fields (`state="last_good"`) or empty strings (`state="empty"`/`"unavailable"`); it never raises into Chat/wake and never substitutes a conversation summary.

- [ ] **Step 1: Replace summary-fallback tests with the new state contract**

  In `tests/test_v2_profile_prompt.py`, delete assertions that pass or expect `summary`. Add focused cases for `ok`, degraded/pending with retained fields, missing blob, read failure, and decrypt failure:

  ```python
  @pytest.mark.asyncio
  async def test_select_profile_uses_retained_fields_when_profile_is_degraded():
      row = profile_row(
          state="degraded",
          memory_envelope=encrypted("remembered memory"),
          user_envelope=encrypted("known user"),
      )

      selected = await select_profile_for_turn(
          "user-1",
          enabled=True,
          decrypt_envelope=fake_decrypt,
          read_blob=AsyncMock(return_value=row),
      )

      assert selected.memory == "remembered memory"
      assert selected.user == "known user"
      assert selected.state == "last_good"


  @pytest.mark.asyncio
  async def test_select_profile_returns_empty_when_no_usable_profile_exists():
      selected = await select_profile_for_turn(
          "user-1",
          enabled=True,
          decrypt_envelope=AsyncMock(side_effect=ValueError("bad envelope")),
          read_blob=AsyncMock(return_value=profile_row(state="ok")),
      )

      assert (selected.memory, selected.user) == ("", "")
      assert selected.state == "unavailable"
  ```

- [ ] **Step 2: Run the Profile tests and confirm the old signature/state fails**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_profile_prompt.py -q
  ```

  Expected failure: `select_profile_for_turn` still requires `summary`, and `ProfilePromptSelection` has `summary`/`fallback_reason` rather than the new state fields.

- [ ] **Step 3: Implement the summary-free selection result**

  In `profile_store.py`, replace the selection shape and signature:

  ```python
  @dataclass(frozen=True)
  class ProfilePromptSelection:
      memory: str = ""
      user: str = ""
      state: str = "empty"
      memory_chars: int = 0
      user_chars: int = 0
      age_seconds: float | None = None


  async def select_profile_for_turn(
      user_id: str,
      *,
      enabled: bool,
      decrypt_envelope: DecryptEnvelope,
      read_blob: ReadProfileBlob = read_profile_blob,
  ) -> ProfilePromptSelection:
      ...
  ```

  Centralize result construction so character counts are derived only from plaintext already selected. Treat `state == "ok"` as `ok`; treat a non-`ok` row with decryptable retained fields as `last_good`; return `empty` when disabled/missing; return `unavailable` on read/decrypt errors. Do not log plaintext or exception payloads that can contain content.

- [ ] **Step 4: Remove summary arguments from the production adapter**

  Change `serve_worker._select_agent_profile_for_turn` and the matching `TurnDeps` callable in `worker.py` so callers pass only user/profile inputs:

  ```python
  selection = await deps.select_agent_profile_for_turn(user_id=user_id)
  agent_memory = selection.memory
  user_profile = selection.user
  ```

  Record only `state`, `memory_chars`, `user_chars`, and `age_seconds` on the existing content-free trajectory/metrics surface.

- [ ] **Step 5: Rerun focused Profile tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_profile_prompt.py tests/test_v2_profile.py tests/test_v2_profile_lane.py -q
  ```

- [ ] **Step 6: Commit the Profile contract change**

  ```bash
  git add backend/model_api_runtime/v2/profile_store.py backend/model_api_runtime/v2/serve_worker.py backend/model_api_runtime/v2/worker.py tests/test_v2_profile_prompt.py
  git commit -m "refactor(v2): make profile prompt selection summary-free"
  ```

---

## Task 2: Add a Bounded Complete-Turn Prompt Context

**Files:**

- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `tests/test_v2_summary_watermark_seq.py`
- Modify: `tests/test_v2_prompt_frontier.py`
- Add: `tests/test_v2_recent_prompt_context.py`

**Interfaces:**

- Consumes: `read_recent_turns(user_id, max_turns, row_cap, through_seq)`, attachment expansion, and Task 1 Profile selection.
- Produces: `RecentPromptContext(required_tail, optional_turns, tail_source_truncated, agent_memory, user_profile, profile_state)`.
- Invariant: only complete genuine-user-seeded turns enter replay. The inclusive required/optional boundary is based on the genuine user seed sequence, never summary coverage; `None` means every replay turn is optional.

- [ ] **Step 1: Add unit tests for complete-turn grouping and boundary partitioning**

  In `tests/test_v2_recent_prompt_context.py`, cover incomplete leading assistant rows, tool rows inside a turn, exactly-on-boundary turns, Chat required tail, wake all-optional behavior, and source truncation:

  ```python
  def test_split_recent_window_marks_only_turns_after_cursor_required():
      window = {
          "rows": [
              msg(10, "user", genuine=True),
              msg(11, "assistant"),
              msg(20, "user", genuine=True),
              msg(21, "assistant"),
          ],
          "source_truncated": False,
      }

      optional, required, truncated = _split_recent_turn_window(
          window, required_from_seq=20
      )

      assert [[m["seq"] for m in group] for group in optional] == [[10, 11]]
      assert [m["seq"] for m in required] == [20, 21]
      assert truncated is False
  ```

  Add an async adapter test proving `_read_recent_prompt_context` calls `read_recent_turns` with `target_turns=40` or `16`, and never invokes a summary/tail reader.

- [ ] **Step 2: Run the new tests and confirm missing symbols**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_recent_prompt_context.py -q
  ```

  Expected failure: `RecentPromptContext`, `_split_recent_turn_window`, and `_read_recent_prompt_context` do not exist.

- [ ] **Step 3: Implement the context and split helpers**

  Add to `worker.py` near the existing turn grouping helpers:

  ```python
  @dataclass(frozen=True)
  class RecentPromptContext:
      required_tail: list[dict[str, Any]]
      optional_turns: list[list[dict[str, Any]]]
      tail_source_truncated: bool
      agent_memory: str
      user_profile: str
      profile_state: str


  def _split_recent_turn_window(
      window: dict[str, Any], *, required_from_seq: int | None
  ) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]], bool]:
      rows = [dict(row) for row in window.get("rows", [])]
      groups = _group_complete_turns(rows)
      optional: list[list[dict[str, Any]]] = []
      required_groups: list[list[dict[str, Any]]] = []
      for group in groups:
          seed_seq = int(group[0]["seq"])
          is_required = required_from_seq is not None and seed_seq >= required_from_seq
          (required_groups if is_required else optional).append(group)
      leading_partial = bool(rows) and not _is_genuine_user_seed(rows[0])
      truncated = bool(window.get("source_truncated")) or leading_partial
      return optional, _flatten_turns(required_groups), truncated
  ```

  Implement `_read_recent_prompt_context(...)` to read the bounded window once, expand/scrub it through the existing recent-turn adapter, select Profile, partition turns, and return this dataclass. Do not call `read_summary_with_seq`, `read_tail_after_seq`, or any coverage helper.

- [ ] **Step 4: Preserve model-aware admission for optional whole turns**

  Refactor `_make_build_messages_fn` to accept `agent_memory`, `user_profile`, `required_tail`, and `optional_turns` without `summary` or `coverage_hole_notice`. Keep the existing newest-useful optional-group admission loop and `PromptFrontierExhausted` behavior:

  ```python
  def build_messages(frontier: int) -> list[dict[str, Any]]:
      admitted = optional_turns[-frontier:] if frontier else []
      replay = _flatten_turns(admitted) + required_tail
      return build_turn_messages(
          agent_memory=agent_memory,
          user_profile=user_profile,
          replay_messages=replay,
          ...,
      )
  ```

  The exact helper call must match the current builder API; the semantic requirement is that the budget frontier can remove only complete optional groups and never required rows.

- [ ] **Step 5: Run context, recent-turn, and prompt-frontier tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_recent_prompt_context.py tests/test_v2_summary_watermark_seq.py tests/test_v2_prompt_frontier.py -q
  ```

- [ ] **Step 6: Commit the reusable prompt-context core**

  ```bash
  git add backend/model_api_runtime/v2/worker.py backend/model_api_runtime/v2/serve_worker.py tests/test_v2_recent_prompt_context.py tests/test_v2_summary_watermark_seq.py tests/test_v2_prompt_frontier.py
  git commit -m "refactor(v2): build prompts from bounded complete turns"
  ```

---

## Task 3: Switch Chat to the New Prompt Contract

**Files:**

- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `tests/test_v2_worker.py`
- Modify: `tests/test_v2_prompt_invariant.py`
- Modify: `tests/test_v2_summary_watermark_seq.py`
- Modify: `tests/test_v2_prompt_frontier.py`

**Interfaces:**

- Consumes: `RecentPromptContext` with `target_turns=40`, `through_seq` frozen for the active job, and the active user row's `cursor_seq` as inclusive `required_from_seq`.
- Produces: Chat provider input containing Profile plus optional older complete turns and the required active turn.
- Invariant: no historical coverage check, compaction catch-up, summary read, or coverage-hole degradation can delay Chat.

- [ ] **Step 1: Rewrite Chat contract tests before changing the path**

  Replace summary-watermark expectations with tests that inject forbidden dependencies which raise if touched:

  ```python
  async def forbidden(*args, **kwargs):
      raise AssertionError("conversation compact dependency was touched")


  deps.read_summary_with_seq = forbidden
  deps.read_tail_after_seq = forbidden
  deps.enqueue_maintenance = forbidden
  deps.read_recent_turns = AsyncMock(return_value=recent_window(turns=40))
  ```

  Assert Chat succeeds with `profile_state="unavailable"`, includes the active user turn after `cursor_seq`, caps the automatic replay at 40 complete turns, and does not retry through `_ensure_prompt_coverage`. Add a required-only overflow test that still raises the existing stable prompt-budget error.

- [ ] **Step 2: Run focused Chat tests and observe compact-path calls**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_worker.py tests/test_v2_prompt_invariant.py tests/test_v2_prompt_frontier.py -q -x
  ```

  Expected failure: Chat still calls `_ensure_prompt_coverage_or_degrade`, `_read_seq_adaptive_prompt_context`, or the preflight catch-up retry.

- [ ] **Step 3: Replace the Chat read path**

  In the Chat branch of `worker.py`, retain the frozen `through_seq` and active-turn anchoring, then read the new context:

  ```python
  prompt_context = await _read_recent_prompt_context(
      user_id=user_id,
      deps=deps,
      through_seq=through_seq,
      target_turns=40,
      required_from_seq=cursor_seq,
      enclave_sem=enclave_sem,
      trajectory_recorder=trajectory_recorder,
      active_image_ids=active_image_ids,
  )
  ```

  Feed it into `_make_build_messages_fn`. Delete both the ordinary coverage/catch-up call and the `tail_limit=0` retry after preflight exhaustion. Let optional-turn admission handle model pressure; propagate the existing required-only failure unchanged.

- [ ] **Step 4: Remove Chat post-reply maintenance scheduling**

  Delete the `context.needs_compaction(tail, budget=_TAIL_BUDGET)` branch and its `enqueue_maintenance` call. Do not replace it with another background summary trigger.

- [ ] **Step 5: Update content-free Chat observations**

  Record effective optional turn count, required row count, `tail_source_truncated`, Profile state/character counts/age, and Capture/Profile freshness lag using existing metrics primitives. Remove Chat-emitted summary watermark, coverage hole, compact retry, and compaction-needed fields.

- [ ] **Step 6: Rerun focused Chat tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_worker.py tests/test_v2_prompt_invariant.py tests/test_v2_summary_watermark_seq.py tests/test_v2_prompt_frontier.py -q
  ```

  Delete test cases whose only subject is historical-summary coverage/catch-up; retain and rename cases covering active-tail anchoring and required prompt overflow.

- [ ] **Step 7: Commit the Chat cutover**

  ```bash
  git add backend/model_api_runtime/v2/worker.py tests/test_v2_worker.py tests/test_v2_prompt_invariant.py tests/test_v2_summary_watermark_seq.py tests/test_v2_prompt_frontier.py
  git commit -m "refactor(v2): remove compact coverage from chat"
  ```

---

## Task 4: Switch Wake to the New Prompt Contract

**Files:**

- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `tests/test_v2_wake_worker.py`
- Modify: `tests/test_v2_wake_success.py`
- Modify: `tests/test_v2_prompt_invariant.py`

**Interfaces:**

- Consumes: `RecentPromptContext` with `target_turns=16`, `through_seq=wake_snapshot_seq`, and `required_from_seq=None`.
- Produces: wake provider input with up to 16 optional complete recent turns plus the existing required scheduled/proactive boundary.
- Invariant: Profile/Capture lag and old Chat coverage never block wake; the genuine-user-history eligibility gate remains unchanged.

- [ ] **Step 1: Add wake tests that prohibit compact dependencies**

  Configure summary/frontier/coverage callbacks as `forbidden`, return 17 complete recent turns, and assert the provider sees only the newest 16. Assert all replay groups are optional while the wake instruction/note remains required. Add a missing/degraded Profile case that still completes wake.

- [ ] **Step 2: Run focused wake tests and observe the old coverage call**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_wake_worker.py tests/test_v2_wake_success.py tests/test_v2_prompt_invariant.py -q -x
  ```

- [ ] **Step 3: Replace wake coverage/context reads**

  Delete `_ensure_prompt_coverage` and `_read_seq_adaptive_prompt_context` from the wake path. Use:

  ```python
  prompt_context = await _read_recent_prompt_context(
      user_id=user_id,
      deps=deps,
      through_seq=wake_snapshot_seq,
      target_turns=16,
      required_from_seq=None,
      enclave_sem=enclave_sem,
      trajectory_recorder=trajectory_recorder,
  )
  ```

  Since no recent group can start after the frozen snapshot, all historical turns are optional. Preserve the separate required wake payload and the existing genuine-user-history gate.

- [ ] **Step 4: Rerun focused wake tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_wake_worker.py tests/test_v2_wake_success.py tests/test_v2_prompt_invariant.py -q
  ```

- [ ] **Step 5: Commit the wake cutover**

  ```bash
  git add backend/model_api_runtime/v2/worker.py tests/test_v2_wake_worker.py tests/test_v2_wake_success.py tests/test_v2_prompt_invariant.py
  git commit -m "refactor(v2): remove compact coverage from wake"
  ```

---

## Task 5: Make `history_search` Raw-Chat-Only

**Files:**

- Modify: `backend/model_api_runtime/v2/history_search.py`
- Modify: `backend/model_api_runtime/v2/history_readside.py`
- Modify: `tests/test_v2_history_search_unit.py`
- Modify: `tests/test_v2_history_readside.py`
- Modify: `tests/test_v2_history_search_store.py`

**Interfaces:**

- Consumes: query/time bounds, authenticated user/runtime generation, a frozen `snapshot_through_seq`, raw Chat candidate rows, and existing row/byte/deadline/result/lease budgets.
- Produces: newest-to-oldest matching results plus a signed continuation cursor for the same frozen snapshot.
- Invariant: no summary/frontier read, summary-leaf enclave request, leaf-priority phase, summary watermark cursor field, or summary-derived `coverage_gap`. `history_fetch` is unchanged.

- [ ] **Step 1: Rewrite cursor and scan-plan unit tests**

  Define the new cursor payload explicitly:

  ```python
  cursor = HistoryCursor(
      user_id="user-1",
      snapshot_through_seq=900,
      runtime_generation=7,
      query="deployment",
      start_ts=None,
      end_ts=None,
      resume_seq=750,
      expires_at=now + 300,
  )
  ```

  Test HMAC tamper rejection, user/generation/query mismatch, deterministic second-page continuation, empty complete scan, and old summary-aware payload rejection as `cursor_invalid`. Remove tests for leaf ranges, watermark phases, and summary coverage gaps.

- [ ] **Step 2: Add read-side tests with forbidden summary APIs**

  Monkeypatch `jobs_store.get_summary_frontier_state` and `jobs_store.list_level0_summary_leaves` to raise. Assert `run_history_search` still pages newest-to-oldest through raw `chat_history_candidate_rows`, honors budgets, invokes the enclave only for raw candidates, and returns no `coverage_gap` field/value.

- [ ] **Step 3: Run the history tests and confirm summary dependencies fail**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_history_search_unit.py tests/test_v2_history_readside.py tests/test_v2_history_search_store.py -q -x
  ```

- [ ] **Step 4: Collapse the cursor/scan state to one raw phase**

  In `history_search.py`, remove summary watermark, leaf ranges, and phase transitions. Keep a versioned minimal state:

  ```python
  @dataclass(frozen=True)
  class HistoryCursor:
      user_id: str
      snapshot_through_seq: int
      runtime_generation: int
      query: str
      start_ts: float | None
      end_ts: float | None
      resume_seq: int
      expires_at: float
      version: int = CURSOR_VERSION
  ```

  Increment `CURSOR_VERSION`, validate every field before using it, and continue signing/verifying with the existing HMAC secret. Remove `summary_watermark_seq`, `phase`, and `uncompressed_floor`; do not accept their old cursor version by silently dropping fields.

- [ ] **Step 5: Remove summary reads and leaf hinting from the read side**

  In `history_readside.run_history_search`, freeze the snapshot only on page one, then repeatedly request bounded raw candidates before `before_seq`, decrypt/scan them in the enclave, and advance the cursor to the oldest scanned sequence. Preserve attachment-caption matching, result ordering, and all budgets. Return an incomplete page plus cursor when any scan budget is exhausted.

- [ ] **Step 6: Rerun history search and fetch regressions**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_history_search_unit.py tests/test_v2_history_readside.py tests/test_v2_history_search_store.py tests/test_v2_history_tools.py -q
  ```

- [ ] **Step 7: Commit the raw-only history path**

  ```bash
  git add backend/model_api_runtime/v2/history_search.py backend/model_api_runtime/v2/history_readside.py tests/test_v2_history_search_unit.py tests/test_v2_history_readside.py tests/test_v2_history_search_store.py
  git commit -m "refactor(v2): search raw chat without summary hints"
  ```

---

## Task 6: Stop Maintenance Production and Add the Rolling-Deploy Tombstone

**Files:**

- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `tests/test_v2_compaction_cas_requeue.py`
- Add: `tests/test_v2_maintenance_retired.py`

**Interfaces:**

- Consumes: an already claimed legacy `maintenance` job.
- Produces: a normal owned-job completion and content-free `maintenance_retired` status.
- Invariant: the tombstone runs before provider resolution and touches no user content, Chat reader, decryptor, summary writer, or job enqueue callback.

- [ ] **Step 1: Write a dependency-denial tombstone test**

  Construct a claimed maintenance job and set every prohibited dependency to the same raising stub:

  ```python
  async def prohibited(*args, **kwargs):
      raise AssertionError("retired maintenance touched protected dependency")


  deps.resolve_provider = prohibited
  deps.mint_provider_token = prohibited
  deps.read_recent_turns = prohibited
  deps.read_capture_tail_after_seq = prohibited
  deps.append_summary_segment = prohibited
  deps.enqueue_job = prohibited
  ```

  Assert `_run_turn` completes the owned job once, flushes `status="maintenance_retired"`, emits no contentful trajectory fields, and creates no successor. Add an idempotency/lease-loss case using the existing job completion semantics.

- [ ] **Step 2: Run the new test and confirm old compaction executes**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_maintenance_retired.py -q
  ```

- [ ] **Step 3: Add an early maintenance tombstone in `_run_turn`**

  Place the branch after claim/ownership setup and before provider resolution:

  ```python
  if job.lane == "maintenance":
      completed = await deps.complete_owned_job(job.id, owner=worker_id)
      await tm.flush(
          failed=not completed,
          status="maintenance_retired" if completed else "lease_lost",
      )
      return
  ```

  Adapt names to the existing owned completion API. Do not route through `process_job` or `_run_compaction`.

- [ ] **Step 4: Remove every new-job producer**

  Delete the compaction self-chain/CAS requeue, coverage-triggered enqueue, post-Chat `needs_compaction` enqueue, `_backlog_scan_loop`, backlog scan startup task, `_BACKLOG_SCAN_*` constants, and `jobs_store.due_compaction_users`. Keep `maintenance` in lane validation, priority, slot protocol, and heavy-lane routing for phase-one drain compatibility.

- [ ] **Step 5: Remove maintenance dispatch from `process_job`**

  Delete the branch that calls `_run_compaction`; the only accepted maintenance behavior is the `_run_turn` tombstone. Rewrite `tests/test_v2_compaction_cas_requeue.py` to assert no successor, or delete it after its sole old behavior is covered by `test_v2_maintenance_retired.py`.

- [ ] **Step 6: Run lifecycle, lane, and tombstone tests**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_maintenance_retired.py tests/test_v2_jobs_store.py tests/test_v2_job_lease_fencing.py tests/test_v2_extraction_lanes.py tests/test_v2_worker.py -q
  ```

- [ ] **Step 7: Commit the rolling-deploy behavior**

  ```bash
  git add backend/model_api_runtime/v2/worker.py backend/model_api_runtime/v2/serve_worker.py backend/model_api_runtime/v2/jobs_store.py tests/test_v2_maintenance_retired.py tests/test_v2_compaction_cas_requeue.py
  git commit -m "refactor(v2): retire legacy maintenance jobs safely"
  ```

---

## Task 7: Delete Conversation-Compact Runtime and Storage Dead Code

**Files:**

- Delete: `backend/model_api_runtime/v2/compaction.py`
- Delete: `backend/model_api_runtime/v2/summary_frontier.py`
- Delete: `scripts/repair_v2_bricked_summary_frontier.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/model_api_runtime/v2/db.py`
- Delete: `tests/test_repair_v2_bricked_frontier.py`
- Delete: `tests/test_v2_compaction.py`
- Delete: `tests/test_v2_compaction_cas_requeue.py`
- Delete: `tests/test_v2_compaction_integration.py`
- Delete: `tests/test_v2_deterministic_compaction.py`
- Delete: `tests/test_v2_summary_frontier_store.py`
- Delete: `tests/test_v2_summary_frontier_unit.py`
- Delete: `tests/test_v2_summary_store.py`
- Modify or delete summary-only cases in: `tests/test_v2_summary_watermark_seq.py`
- Modify: tests that instantiate `TurnDeps` directly.

**Interfaces:**

- Removes: summary/frontier reads and writes, compact/catch-up code, coverage sentinels, summary CAS/checkpoint repair, and summary-only DB helpers.
- Preserves: legacy schema/migration files and data, maintenance lane acceptance, raw Chat readers, Capture tail reading, recent-turn reading, and prompt-frontier budget code.
- Invariant: Capture must retain its oldest-contiguous raw Chat reader under a truthful name.

- [ ] **Step 1: Rename the Capture-only tail dependency before deleting compact APIs**

  Rename `read_compaction_tail_after_seq` to `read_capture_tail_after_seq` in `TurnDeps`, `serve_worker`, Capture call sites, and tests:

  ```python
  read_capture_tail_after_seq: Callable[..., Awaitable[dict[str, Any]]]
  ```

  Run Capture tests to prove behavior is unchanged:

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_capture_lifecycle.py tests/test_v2_capture_batch_protocol.py -q
  ```

- [ ] **Step 2: Add a static no-reintroduction test**

  Add a source scan to the existing Runtime V2 static-policy test module (or a new `tests/test_v2_no_conversation_compaction.py`) that rejects active Python/config references:

  ```python
  FORBIDDEN = {
      "read_summary_with_seq",
      "read_summary_frontier_metadata",
      "append_summary_segment",
      "append_summary_checkpoint",
      "_ensure_prompt_coverage",
      "_run_compaction",
      "needs_compaction",
      "PROFILE_COVERAGE_DETERMINISTIC",
  }
  ```

  Exclude migrations, the approved phase-one tombstone string, design/plan docs, and historical changelogs. Also assert no production enqueue call uses lane/job type `maintenance`.

- [ ] **Step 3: Run the guard and observe remaining references**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_no_conversation_compaction.py -q
  ```

- [ ] **Step 4: Remove dead `TurnDeps` and production adapters**

  Delete `read_summary_with_seq`, `read_summary_frontier_metadata`, `append_summary_segment`, and `append_summary_checkpoint` from `TurnDeps` and production dependency assembly. Delete `serve_worker` helpers `_decrypt_summary_text`, `_read_summary_frontier_metadata`, `_read_summary_with_seq`, `_append_summary_segment`, and `_append_summary_checkpoint`.

- [ ] **Step 5: Remove summary/frontier store and DB helpers with no remaining callers**

  Delete from `jobs_store.py`: `get_summary_row`, `upsert_summary_row_cas`, `get_summary_frontier_state`, `append_summary_leaf_cas`, `seed_legacy_summary_segment`, `insert_summary_checkpoint`, and `list_level0_summary_leaves`. Delete `chat_coverage_bounds_after_seq` and `seq_for_watermark_ts` from `db.py` only after `rg` proves they have no non-test callers. Preserve `chat_messages_after_seq`, `chat_recent_turn_rows`, `count_messages_after_seq`, raw history queries, all tables, and all migrations.

- [ ] **Step 6: Delete compact/frontier modules, repair script, and obsolete tests**

  Remove the files listed above using `apply_patch`. Remove imports, fixtures, config knobs, environment templates, deployment manifests, and comments that imply conversation compact is active. Do not delete prompt-frontier code or tests; it is the model-budget mechanism.

- [ ] **Step 7: Prove the forbidden surface is gone**

  ```bash
  rg -n "read_summary_with_seq|read_summary_frontier_metadata|append_summary_segment|append_summary_checkpoint|_ensure_prompt_coverage|_run_compaction|needs_compaction|PROFILE_COVERAGE_DETERMINISTIC" backend scripts tests .github docker-compose.yml pyproject.toml
  ```

  Expected result: only deliberate assertions in the static guard, if its source contains the forbidden literals. Separately inspect all remaining `maintenance` references and confirm each is lane compatibility or tombstone code, not a producer.

- [ ] **Step 8: Run the affected Runtime V2 suite**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_no_conversation_compaction.py tests/test_v2_capture_lifecycle.py tests/test_v2_capture_batch_protocol.py tests/test_v2_profile_prompt.py tests/test_v2_recent_prompt_context.py tests/test_v2_prompt_frontier.py tests/test_v2_worker.py tests/test_v2_wake_worker.py tests/test_v2_history_readside.py -q
  ```

- [ ] **Step 9: Commit dead-code removal**

  ```bash
  git add -A
  git commit -m "refactor(v2): delete conversation compaction runtime"
  ```

---

## Task 8: Update Architecture Documentation and Release Notes

**Files:**

- Modify: `docs/RUNTIME_V2_FLOWS.md`
- Modify: `docs/RUNTIME_V2_PARITY.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/workflows/chat.mdx`
- Modify: `docs-site/content/docs/workflows/memory.mdx`
- Modify: `docs-site/content/docs/self-hosting.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`

**Interfaces:**

- Documents: `Chat -> bounded recent turns + MEMORY/USER`, asynchronous `Chat -> Capture -> Memory Garden -> Profile`, raw-only explicit history tools, and the phase-one maintenance tombstone/schema retention.
- Does not change: public HTTP/OpenAPI request or response contracts.

- [ ] **Step 1: Add a documentation assertion before editing prose**

  Extend the static guard to require the canonical limits and paths in internal docs:

  ```python
  assert "40 complete recent turns" in flows
  assert "16 complete recent turns" in flows
  assert "history_search" in flows and "raw encrypted Chat" in flows
  assert "conversation compaction" not in active_architecture_section.lower()
  ```

  Keep the check scoped to current architecture sections so historical changelog text remains legal.

- [ ] **Step 2: Run the guard and observe stale documentation**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_no_conversation_compaction.py -q
  ```

- [ ] **Step 3: Update internal and public architecture docs**

  Describe the automatic prompt exactly:

  ```text
  Chat: latest usable MEMORY + USER + up to 40 complete recent turns
  Wake: latest usable MEMORY + USER + up to 16 complete recent turns
  Long-term semantic path: Chat -> Capture -> Memory Garden -> Profile
  Explicit archive path: history_search/history_fetch -> bounded raw encrypted Chat
  ```

  Explain that Profile lag/failure is non-blocking and last-known-good fields are used when decryptable. Remove active compact/frontier/catch-up diagrams and trust claims. In self-hosting docs, retain the encrypted raw Chat and enclave boundary description. Add an `Unreleased` changelog entry. State that legacy summary/frontier schema remains temporarily for rollback and rolling-deploy compatibility.

- [ ] **Step 4: Run docs checks**

  ```bash
  cd docs-site && npm run types:check
  cd docs-site && npm run lint
  cd docs-site && npm run build
  ```

  No OpenAPI regeneration is required because the public API schema is unchanged. If documentation review reveals a response-contract change, stop and add OpenAPI source/regeneration/tests in the same task.

- [ ] **Step 5: Commit documentation**

  ```bash
  git add docs/RUNTIME_V2_FLOWS.md docs/RUNTIME_V2_PARITY.md docs/CHANGELOG.md docs-site/content/docs/architecture.mdx docs-site/content/docs/workflows/chat.mdx docs-site/content/docs/workflows/memory.mdx docs-site/content/docs/self-hosting.mdx docs-site/content/docs/changelog.mdx tests/test_v2_no_conversation_compaction.py
  git commit -m "docs(v2): document summary-free prompt contract"
  ```

---

## Task 9: Full Regression, Diff Audit, and PR Update

**Files:**

- Inspect: every file changed against `origin/test`
- Modify only if verification exposes a defect; use a focused regression test and a separate fix commit.

**Interfaces:**

- Consumes: the complete phase-one branch, current `origin/test`, local PostgreSQL, and docs toolchain.
- Produces: reproducible local test counts, a clean diff audit, updated PR #187 evidence, and remote check results.
- Invariant: verification does not silently weaken tests, expand PR scope, merge the PR, retarget it, or push production branches.

- [ ] **Step 1: Run the full local suite excluding only the established external API/E2E modules**

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py -x
  ```

  Record the exact passed/skipped/xfailed counts. Any new failure must be explained and fixed; do not classify it as unrelated without reproducing it against `origin/test`.

- [ ] **Step 2: Rerun docs verification from a clean command invocation**

  ```bash
  cd docs-site && npm run types:check && npm run lint && npm run build
  ```

- [ ] **Step 3: Audit the final diff and forbidden dependencies**

  ```bash
  git diff --check origin/test...HEAD
  git status --short
  git log --oneline --decorate origin/test..HEAD
  rg -n "PROFILE_COVERAGE_DETERMINISTIC|_run_compaction|_ensure_prompt_coverage|read_summary_with_seq|append_summary_segment" backend scripts tests .github
  rg -n "maintenance" backend/model_api_runtime/v2 tests
  ```

  Confirm:

  - no compact/summary/frontier automatic prompt path remains;
  - no maintenance producer remains;
  - maintenance lane validation and the early tombstone remain;
  - Profile absence is non-blocking;
  - Chat/Wake limits are 40/16 complete turns;
  - history search is raw-only and `history_fetch` is unchanged;
  - no legacy schema or migration was removed;
  - no user-owned unrelated work entered the diff.

- [ ] **Step 4: Review the branch against the current `test` target**

  Fetch and inspect before pushing. If `test` advanced, merge `origin/test` into the feature branch, resolve conflicts by preserving both the new prompt contract and unrelated target-branch changes, then rerun the focused and full verification above. Do not rebase a shared PR branch unless explicitly requested.

- [ ] **Step 5: Push and update PR #187 evidence**

  Push `refactor/v2-deterministic-only-coverage`, confirm PR #187 still targets `test`, and add the exact local test/doc results plus the phase-one compatibility note to the PR description or comment. Do not merge or retarget the PR without explicit user authorization.

- [ ] **Step 6: Verify remote checks**

  Use `gh pr checks 187 --watch` or inspect each completed check. Report failures with their actual logs; fix only failures caused by this branch and rerun local verification before pushing follow-ups.

---

## Phase-Two Follow-Up Boundary

Do not implement this section in PR #187. After at least one stable release shows zero legacy maintenance arrivals and rollback no longer requires the schema:

1. Create a separate migration/PR to drop summary/frontier/checkpoint tables and indexes.
2. Remove the `maintenance` lane/job type, priority, slot, heavy-lane routing, and tombstone.
3. Remove temporary `maintenance_retired` telemetry after the observation window.
4. Run migration rollback/forward validation in the test environment and record deployment evidence before promoting from `test`/`pre` to `main`.
