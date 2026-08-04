# Runtime V2 P0 Feedback Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development for every task, and superpowers:verification-before-completion before claiming success.

**Goal:** Eliminate false perception arrivals across backend workers and remove synthetic user messages from Runtime V2 proactive turns while restoring V1-equivalent speak/silence semantics.

**Architecture:** Replace the process-local wake-capable perception baseline with a PostgreSQL row keyed by `(user_id, signal)` and decide every observation under a row lock. Build proactive prompts from real conversation rows plus explicitly labeled application-data blocks, never from a fabricated user turn; ordinary automatic heartbeats with no genuine conversation history complete silently without calling a provider.

**Tech Stack:** Python 3.12, psycopg 3, PostgreSQL, Alembic (RDS and TEE chains), pytest, MDX documentation.

**Approved design:** `docs/superpowers/specs/2026-08-04-runtime-v2-user-feedback-remediation-design.md`

---

## Scope and fixed decisions

- This plan implements P0-A and P0-B only. Profile readiness and new metrics remain P1 follow-up work.
- The RDS migration revision is `0077_perception_signal_state_v2`, based on the current single head `0076_plaintext_job_exclusivity`.
- The TEE migration revision is `0011_perception_signal_state_v2`, based on the current single head `0010_v2_chat_tail_anchor`.
- `perception_signal_state_v2` uses the TEE `SNAPSHOT` lane: the table is plaintext operational state, mutable, small, and contains only keyed fingerprints/timestamps.
- Canonical values use `json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)`; values that cannot be canonicalized fail closed.
- The fingerprint is lowercase HMAC-SHA-256 hex over `b"perception-signal-v2\0" + user_id + b"\0" + signal + b"\0" + canonical_json`, keyed by stripped `FEEDLING_RUNTIME_TOKEN_SECRET`. A missing/blank secret produces an error decision and no wake.
- Event times are converted from epoch seconds to UTC `datetime`; non-finite timestamps fail closed.
- The durable decision enum is exactly: `baseline_created`, `duplicate`, `stale`, `conflict_same_ts`, `unchanged`, `changed`, `error`.
- For equal timestamps: equal event ID is `duplicate`; equal fingerprint is `unchanged`; different fingerprint is `conflict_same_ts`. No equal-time path emits a wake.
- `source_event_id` is passed explicitly. Device events use their stable `event_id`; location snapshot observations have no event ID and rely on timestamp/fingerprint ordering.
- Explicit one-shot signals may pass `allow_first_event=True`; anchor signals use the default `False` and therefore never wake on first observation.
- For proactive prompt assembly, generated summary/profile/working-memory/temporal/runtime-data blocks use `role=assistant`; only real chat-tail rows may serialize as `role=user`. The labels remain explicitly untrusted and the system policy states that assistant-role application blocks are data, not prior assistant claims or instructions.
- Scheduled reminder text moves into untrusted runtime data. Manual wake carries only its authorization/control metadata. Neither is represented as a just-spoken user request.
- Automatic `heartbeat` (including perception-triggered heartbeat) with no genuine user chat row completes before the provider/tool loop. `scheduled` and `manual_wake` may run with an empty real history because they have explicit authorization; the existing tool loop already supports an empty internal transcript, so no serialized seed is added.

## Task 1: Install and register the durable signal-state table

**Files:**

- Create: `backend/alembic/versions/0077_perception_signal_state_v2.py`
- Create: `backend/alembic_tee/versions/0011_perception_signal_state_v2.py`
- Modify: `backend/tee_shadow/table_registry.py`
- Modify: `backend/db.py`
- Modify: `tests/test_v2_jobs_migration.py`
- Modify: `tests/test_tee_table_registry.py`
- Modify: `tests/test_delete_user_cascades_all_tables.py`
- Modify: `tests/test_account_reset_purges_all_tables.py`

### 1.1 RED — describe the schema and deletion contract

- [ ] Extend the migration test to load revision `0077_perception_signal_state_v2` and assert:
  - `down_revision == "0076_plaintext_job_exclusivity"`;
  - columns and types match the approved design;
  - primary key is `(user_id, signal)`;
  - `user_id` references `users(user_id) ON DELETE CASCADE`;
  - `source_event_id` is nullable;
  - the installed DB reports `0077_perception_signal_state_v2` as its only head.
- [ ] Add `perception_signal_state_v2` to the real cascade/reset fixtures, seed one row, and assert both `db.delete_user()` and `/v1/account/reset` remove it.
- [ ] Add a DB-belt assertion proving `db.delete_user_data()` removes the new row while preserving the parent `users` row.
- [ ] Add/extend TEE registry coverage so the new table must be registered as `SNAPSHOT` and must physically exist in the TEE schema.
- [ ] Run the smallest schema/reset set and confirm it fails because the table/revisions do not exist:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_v2_jobs_migration.py \
  tests/test_tee_table_registry.py \
  tests/test_delete_user_cascades_all_tables.py \
  tests/test_account_reset_purges_all_tables.py -q
```

Expected RED: missing migration/table/registry entry assertions, not fixture or connection errors.

### 1.2 GREEN — add both DDLs and cleanup registration

- [ ] Create the RDS table with `TIMESTAMPTZ` time columns, a composite primary key, and the cascade FK. Add a non-unique `(user_id, last_seen_at)` index only if the implementation query introduced in Task 2 uses it; do not add speculative indexes.
- [ ] Create the same table and constraints in the TEE migration.
- [ ] Register it in `tee_shadow.table_registry.REGISTRY` as `SNAPSHOT` with a reason explaining that it is mutable plaintext operational state containing only HMAC fingerprints/timestamps.
- [ ] Add it to `db.delete_user_data()`'s RDS belt and to the `_no_tee_tables` set because SNAPSHOT convergence/users cascade owns the TEE copy; do not issue an ad-hoc mirror write for a SNAPSHOT table.
- [ ] Re-run the tests above until GREEN.
- [ ] Run the Alembic single-head tests:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_genesis_worker_claim_migration.py::test_alembic_single_head \
  tests/test_v2_summary_watermark_seq.py -q
```

- [ ] Commit:

```bash
git add backend/alembic/versions/0077_perception_signal_state_v2.py \
  backend/alembic_tee/versions/0011_perception_signal_state_v2.py \
  backend/tee_shadow/table_registry.py backend/db.py \
  tests/test_v2_jobs_migration.py tests/test_tee_table_registry.py \
  tests/test_delete_user_cascades_all_tables.py \
  tests/test_account_reset_purges_all_tables.py
git commit -m "fix(perception): add durable signal baseline table"
```

## Task 2: Implement the atomic perception decision primitive

**Files:**

- Create: `backend/perception/signal_state_v2.py`
- Create: `tests/test_perception_signal_state_v2.py`

### 2.1 RED — prove deterministic single- and multi-worker decisions

- [ ] Write PostgreSQL-backed tests against the real table. Each test names the production break it catches and derives expected outcomes literally.
- [ ] Cover this table of observations:

| Existing row | New input | Expected outcome | Row mutation | Wake eligible |
| --- | --- | --- | --- | --- |
| none | any anchor | `baseline_created` | insert seen/changed | no |
| same event ID | any | `duplicate` | none | no |
| newer row | older timestamp | `stale` | none | no |
| same timestamp/fingerprint | different/no event ID | `unchanged` | event ID may advance | no |
| same timestamp/different fingerprint | any | `conflict_same_ts` | none | no |
| later timestamp/same fingerprint | any | `unchanged` | advance seen/event ID | no |
| later timestamp/different fingerprint | any | `changed` | advance seen/changed/event ID | yes |

- [ ] Instantiate two independent state-store objects for the same user/signal and prove the second process-equivalent observer sees `unchanged`, not a new baseline.
- [ ] Run two concurrent transactions observing the same first value and assert exactly one `baseline_created`, one non-waking result, and one stored row.
- [ ] Run two concurrent transactions observing the same real transition and assert exactly one `changed`, one non-waking result, and the final fingerprint/timestamps are stable.
- [ ] Verify HMAC determinism with a hand-computed literal `hmac.new(...)` fixture, domain separation across users/signals, and absence of plaintext `home`/`work` in the row.
- [ ] Verify missing secret, NaN/non-canonical value, non-finite timestamp, and injected DB failure all return `error` without inserting/updating a row.
- [ ] Verify `allow_first_event=True` returns `changed` for a first explicit event while default first observation returns `baseline_created`.
- [ ] Run and observe RED:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests/test_perception_signal_state_v2.py -q
```

### 2.2 GREEN — implement one transaction/row-lock decision

- [ ] Define `DecisionOutcome` as the fixed literal union above and define immutable
  `SignalObservationDecision(outcome, changed, fingerprint, last_seen_at,
  last_changed_at, error_code="")`.
- [ ] Expose `observe_signal_state(user_id: str, signal: str, value: Any, *,
  observed_at: float, source_event_id: str | None = None,
  allow_first_event: bool = False) -> SignalObservationDecision`.

- [ ] Keep canonicalization/fingerprinting as small pure helpers in the same module. Read the environment secret per call so tests and secret rotation do not get a stale import-time value.
- [ ] Start a DB transaction, `SELECT ... FOR UPDATE` by composite key, execute the decision table in the fixed order, and perform only the mutation allowed by that outcome.
- [ ] Handle the first-row insert race using the primary key: after a unique violation, retry the locked read/decision in a fresh transaction rather than treating it as a second baseline.
- [ ] Catch/log storage and validation failures at this boundary and return `error`; never instantiate or call the in-memory differ as fallback.
- [ ] Re-run the new test file until GREEN, then run a mutation check by temporarily reasoning through these breaks: `prev is None -> changed`, using plain SHA-256, accepting stale timestamps, updating on equal-time conflicts, or swallowing the insert race. Confirm at least one named test would fail for each.
- [ ] Commit:

```bash
git add backend/perception/signal_state_v2.py tests/test_perception_signal_state_v2.py
git commit -m "fix(perception): decide signal changes atomically"
```

## Task 3: Route wake-capable perception through durable state

**Files:**

- Modify: `backend/perception/differ_v2.py`
- Modify: `backend/perception/ingress_v2.py`
- Modify: `backend/perception/service.py`
- Modify: `tests/test_proactive_runtime_v2.py`
- Modify: `tests/test_perception_ingress_v2.py`

### 3.1 RED — lock the ingress behavior users observe

- [ ] Change the existing anchor tests so the first observation creates no wake, a repeat through a fresh `PerceptionDifferV2`/ingress instance creates no wake, and only `home -> work` creates one `arrived_at_anchor` wake.
- [ ] Add a service-level test that sends the same encrypted location snapshot through two independent ingress instances (process-equivalent workers), then a real changed anchor; assert emitted triggers are exactly `['arrived_at_anchor']`.
- [ ] Add stale and equal-timestamp-conflict location snapshot tests and assert neither submits a wake or overwrites the durable baseline.
- [ ] Add a device-event test proving `event_id` reaches `source_event_id` and a repeated event ID creates no second wake.
- [ ] Add a fail-closed test by replacing the durable observer with one returning `error`; assert snapshot storage still reports `accepted`, but `submit_wake` is never called.
- [ ] Run and observe RED:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_proactive_runtime_v2.py \
  tests/test_perception_ingress_v2.py -q
```

### 3.2 GREEN — separate event rendering from durable state ownership

- [ ] Make `PerceptionDifferV2` consume a `SignalObservationDecision` rather than own correctness state for wake-capable signals. It may retain pure event/presence-hint rendering, but remove the production `_state` map and the `prev is None -> changed` rule.
- [ ] Extend `IngressObservationV2` and `observe_signal_v2()` with `source_event_id` and `allow_first_event`; default the latter to `False`.
- [ ] In `observe_signal_v2()`, call `signal_state_v2.observe_signal_state()` first. Render/submit events only for `outcome == 'changed'`; return zero wakes for baseline, duplicate, stale, conflict, unchanged, or error.
- [ ] Carry the stable `event_id` from `device_event_observations_v2()`. Keep location snapshot `source_event_id=None` because `ios_report:location_signal` is an origin label, not an idempotency key.
- [ ] Explicitly set `allow_first_event=True` only for `unlock_after_absence`, `photo_added`, and other first-class event IDs whose first occurrence is itself meaningful. Keep all anchor signals at `False`.
- [ ] Ensure `change_digest` contains only the signal/category transition representation already approved for wake context; never include the stored HMAC fingerprint.
- [ ] Re-run the focused tests until GREEN, then run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_perception_ingress_v2.py \
  tests/test_proactive_runtime_v2.py \
  tests/test_runtime_reconciler.py -q
```

- [ ] Commit:

```bash
git add backend/perception/differ_v2.py backend/perception/ingress_v2.py \
  backend/perception/service.py tests/test_proactive_runtime_v2.py \
  tests/test_perception_ingress_v2.py
git commit -m "fix(perception): suppress baseline and replay wakes"
```

## Task 4: Remove synthetic user turns and restore V1 proactive semantics

**Files:**

- Modify: `backend/model_api_runtime/v2/context.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `tests/test_v2_context.py`
- Modify: `tests/test_v2_wake_worker.py`
- Modify: `tests/test_v2_wake_tool_loop.py`
- Modify: `tests/test_v2_screen_watch_lane.py`

### 4.1 RED — prove provider `user` messages are genuine

- [ ] Replace tests that expect `_WAKE_NUDGE` with behavior tests that capture the actual provider message list and assert every `role=user` content equals a real row from the supplied chat tail.
- [ ] Include real-history fixtures with both user and assistant rows and generated summary/profile/temporal/runtime data. Assert generated application data is not serialized as `role=user` and the real user's language remains the latest user-language signal.
- [ ] Add an ordinary `heartbeat` test with empty genuine history. Assert provider call count is zero, job status is `completed`, no reply/effect is written, and no user-visible error event is emitted.
- [ ] Add a perception-triggered empty-history heartbeat test with the same zero-provider result.
- [ ] Add a non-empty heartbeat test proving the model is still called once and may either speak or return empty text.
- [ ] Add a context test for an `application_data_role='assistant'` (or equivalently named) build mode: summary/profile/working memory/coverage/temporal/runtime blocks use assistant role, real tail roles remain unchanged, and labels/payloads remain byte-for-byte data rather than being merged into system authority.
- [ ] Add behavioral prompt assertions that the proactive policy communicates all four V1-equivalent rules and does not contain the one-sided gates `only if`, `genuinely worth saying`, or `silence is correct`. Keep this assertion scoped to the user-visible decision behavior, not the private constant name.
- [ ] Run and observe RED:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_v2_context.py \
  tests/test_v2_wake_worker.py \
  tests/test_v2_wake_tool_loop.py \
  tests/test_v2_screen_watch_lane.py -q
```

### 4.2 GREEN — build proactive turns from history plus application data

- [ ] Remove `_WAKE_NUDGE` and every `wake_tail = tail + synthetic user` path, including the prompt-frontier retry rebuild.
- [ ] Add an explicit application-data role option to `context.build_turn_messages()` and `_make_build_messages_fn()`. Default it to the current chat behavior; pass `assistant` only from wake lanes so foreground chat cache/security behavior does not change in this P0.
- [ ] Update `_RUNTIME_CONTEXT_POLICY` to say that proactive application-data blocks may use assistant role, are untrusted application data, are not prior assistant claims, and never constitute a new user request.
- [ ] Before workspace/provider/tool work that can produce external effects, detect whether an ordinary heartbeat's frozen tail contains at least one `_genuine_user` row. If not, finish the wake job with the existing generation/context-consumption-safe finalizer and return `completed` without calling the provider. Do not use tail non-emptiness because assistant-only history is not genuine user history.
- [ ] Use the tool loop's existing empty internal transcript for authorized manual/scheduled wakes. Do not add any marker object to the provider message list.
- [ ] Replace `_WAKE_SYSTEM_PROMPT` with V1-equivalent semantics:
  - platform presence moment, not a user request;
  - speaking and silence are equally valid;
  - decide from personality, real conversation, and current moment without a strong-reason threshold;
  - perception glance is a hint, not a report;
  - never mention wake/timer/prompt/system fields.
- [ ] Apply the same no-one-sided-threshold rule to screen-watch wording while preserving its screen-specific grounding and outbound safety fence.
- [ ] Re-run the focused tests until GREEN.
- [ ] Run prompt-cache/frontier regressions because role placement changes can alter provider prefixes:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_provider_prompt_cache.py \
  tests/test_v2_prompt_frontier.py \
  tests/test_v2_optional_anchor.py \
  tests/test_v2_multimodal_e2e.py -q
```

- [ ] Commit:

```bash
git add backend/model_api_runtime/v2/context.py \
  backend/model_api_runtime/v2/worker.py \
  tests/test_v2_context.py tests/test_v2_wake_worker.py \
  tests/test_v2_wake_tool_loop.py tests/test_v2_screen_watch_lane.py
git commit -m "fix(runtime-v2): remove synthetic proactive user turns"
```

## Task 5: Preserve scheduled/manual authorization without a fake request

**Files:**

- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `tests/test_v2_wake_worker.py`
- Modify: `tests/test_v2_wake_tool_loop.py`
- Modify: `tests/test_v2_wake_schedule.py`

### 5.1 RED — prove reminders are data and authorization still works

- [ ] Rewrite the scheduled-wake test so due reminder notes appear only inside the labeled untrusted runtime-data projection, never in a `role=user` message. Assert the scheduled system policy still requires delivery and the activity-event payload still excludes note plaintext.
- [ ] Add a scheduled wake with no real chat history and assert it calls the provider, delivers all reminders, and writes exactly one reply.
- [ ] Add a manual wake with no real chat history and assert it can run with system/application data only, preserves `turn_authorization=True`, and can execute one authorized write tool through the real outbox path.
- [ ] Assert ordinary heartbeat with no history remains different: no provider call and no write authorization is exercised.
- [ ] Run and observe RED:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_v2_wake_worker.py \
  tests/test_v2_wake_tool_loop.py \
  tests/test_v2_wake_schedule.py -q
```

### 5.2 GREEN — move wake payloads into runtime data

- [ ] Add scheduled reminder notes to a dedicated `runtime_data.scheduled_wakes` projection built in `_run_wake()`. Keep identifiers/status/timing structured and label note text as previously user-authorized reminder content.
- [ ] Add non-content authorization metadata for manual wakes (for example `runtime_control.manual_wake=true`) without inventing prose from the user.
- [ ] Keep `_SCHEDULED_WAKE_SYSTEM_PROMPT` as policy only; remove reminder-note interpolation into any conversational message.
- [ ] Preserve scheduled activity events, reply push lane metadata, generation fences, late-user yield, and write-tool authorization.
- [ ] Re-run the focused tests until GREEN, then run the whole wake regression cluster:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_proactive_runtime_v2.py \
  tests/test_v2_wake_worker.py \
  tests/test_v2_wake_tool_loop.py \
  tests/test_v2_wake_schedule.py \
  tests/test_v2_screen_watch_lane.py \
  tests/test_runtime_reconciler.py -q
```

- [ ] Commit:

```bash
git add backend/model_api_runtime/v2/worker.py \
  tests/test_v2_wake_worker.py tests/test_v2_wake_tool_loop.py \
  tests/test_v2_wake_schedule.py
git commit -m "fix(runtime-v2): preserve authorized wake context as data"
```

## Task 6: Update public architecture/reliability documentation

**Files:**

- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/reliability.mdx`
- Modify: `docs-site/content/docs/workflows/perception.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`

### 6.1 Document only the shipped P0 behavior

- [ ] Explain that wake-capable perception baselines are durable, fingerprint-only, ordered, and fail closed; the first anchor reading establishes baseline rather than arrival.
- [ ] Update the trust-boundary diagram/text so proactive application data is distinct from real user messages and does not become a synthetic user request.
- [ ] Document ordinary empty-history heartbeat sleep, scheduled/manual authorization preservation, and V1-equivalent speak/silence semantics.
- [ ] Add an `Unreleased` changelog entry describing the user-visible false-arrival and robotic-timer fixes without claiming P1 profile gates or TTFT metrics are shipped.
- [ ] Confirm no public request/response schema changed. If an implementation step did add a public field, stop and return to design review before touching OpenAPI.
- [ ] Validate docs:

```bash
cd docs-site
npm run types:check
npm run lint
npm run build
```

- [ ] Commit:

```bash
git add docs-site/content/docs/architecture.mdx \
  docs-site/content/docs/reliability.mdx \
  docs-site/content/docs/workflows/perception.mdx \
  docs-site/content/docs/changelog.mdx
git commit -m "docs: describe durable proactive wake safeguards"
```

## Task 7: Full verification and test-environment evidence

**Files:**

- Create: `docs/superpowers/reports/2026-08-04-runtime-v2-p0-feedback-remediation-verification.md`

### 7.1 Verify locally from a clean-enough worktree

- [ ] Run formatting/lint commands already defined by the repository for modified Python files; do not introduce a new formatter.
- [ ] Run all focused P0 tests together against real PostgreSQL/TEE PostgreSQL:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_perception_signal_state_v2.py \
  tests/test_perception_ingress_v2.py \
  tests/test_proactive_runtime_v2.py \
  tests/test_v2_context.py \
  tests/test_v2_wake_worker.py \
  tests/test_v2_wake_tool_loop.py \
  tests/test_v2_wake_schedule.py \
  tests/test_v2_screen_watch_lane.py \
  tests/test_v2_jobs_migration.py \
  tests/test_tee_table_registry.py \
  tests/test_delete_user_cascades_all_tables.py \
  tests/test_account_reset_purges_all_tables.py -q
```

- [ ] Run the repository's OpenAPI contract tests even though the contract is expected unchanged.
- [ ] Run the full backend L1 suite with the repository-standard PostgreSQL environment.
- [ ] Re-run docs `types:check`, `lint`, and `build` after all documentation edits.
- [ ] Inspect `git diff --check`, `git status --short`, and the full diff. Confirm no user work outside this isolated worktree was touched.

### 7.2 Verify the multi-worker failure mode in test

- [ ] Deploy only to `test`, following the repository branch flow; do not target `main` and do not promote production.
- [ ] Record the deployed commit SHA and migration heads.
- [ ] With at least two backend workers, send the same anchor observation repeatedly while alternating workers or restarting one worker. Assert zero arrival wakes after the initial baseline.
- [ ] Send one later real anchor transition concurrently/repeatedly. Assert one durable `changed` decision, one queued wake at most, and repeats become duplicate/unchanged.
- [ ] Exercise an ordinary heartbeat for a test account with no chat history and confirm zero provider request; exercise scheduled/manual wakes and confirm their authorization contracts still work.
- [ ] Record content-free evidence only: worker count, timestamps, decision outcomes/counts, job IDs/statuses, and provider-call counts. Do not record location values, fingerprints, prompt text, reminder text, or thinking text.
- [ ] Save commands, outputs, deviations, and rollback notes in the verification report.
- [ ] Commit the evidence report:

```bash
git add docs/superpowers/reports/2026-08-04-runtime-v2-p0-feedback-remediation-verification.md
git commit -m "test(runtime-v2): record P0 remediation evidence"
```

## Task 8: Completion review

- [ ] Use `superpowers:requesting-code-review` and address findings with `superpowers:receiving-code-review` before integration.
- [ ] Use `superpowers:verification-before-completion`; cite fresh command output rather than earlier runs.
- [ ] Confirm every approved P0 acceptance item maps to a passing test:
  - baseline first/no wake;
  - cross-worker/restart stability;
  - one real transition/one wake;
  - duplicate/stale/equal-time conflict/error fail closed;
  - account reset cleanup and TEE alignment;
  - no synthetic provider user messages;
  - empty-history automatic heartbeat sleeps without provider;
  - scheduled/manual authorization retained;
  - V1-equivalent proactive choice semantics;
  - public docs/changelog updated.
- [ ] Confirm P1 items did not leak into this branch as partial schema or half-wired behavior.
- [ ] If opening a PR, target `test`. Do not open a feature/fix PR directly against `main`.

## Plan self-review

- Spec coverage: every P0-A/P0-B requirement in design sections 5, 6, 9, 10.1, 10.2, and 11 has a task/test above.
- Placeholder scan: no `TODO`, `TBD`, “implementation decides”, or unresolved file name remains.
- Type/contract consistency: `SignalObservationDecision`, decision literals, fingerprint domain, timestamp ordering, source event ID semantics, application-data role, and empty-history lane behavior are named once and reused consistently.
- Scope check: profile readiness and new observability fields are explicitly deferred to P1; production deployment is excluded.
- Test quality: real PostgreSQL is used for concurrency/cascade behavior; provider is mocked only at the external network boundary while job state, prompt assembly, tool loop, outbox, and chat persistence remain real.
