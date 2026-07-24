# Fix: identity_patch on a fresh-start user wedges the V2 conversation

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans or subagent-driven-development. Steps use `- [ ]`.

**Goal:** A fresh-start V2 user (no imported persona → no identity card) whose model
calls `identity_patch` must not have their conversation wedged. Two layers: (A)
make `identity_patch` create the card when none exists so the agent can establish
its identity; (B) make effect sinks honor `retryable` so a deterministic (4xx)
capability failure is terminally discarded, never retried forever.

## Root cause (verified on pre, usr_025fc32384fcf8a2)

- Genesis onboarding with no persona → `identity_status='not_provided'`, NO identity
  card written.
- First chat message → model calls `identity_patch` (WRITE_ACTION) → enqueued as an
  `identity_encrypted_v1` effect.
- `_sink_identity` (serve_worker.py:1257) runs the `identity_patch` capability →
  `_identity_plain_for_action` returns `identity_not_initialized` → capability
  `ok=False` (409, **non-retryable**) → sink `raise RuntimeError("identity_patch_failed")`.
- The outbox records the error and retries forever (after 8 attempts →
  `needs_reconciliation`, still swept). `CapabilityResult.retryable` is IGNORED by
  the sink. The reconcile sweeper loops on the dead effect; the job shows failed.

## Global constraints

- Postgres 127.0.0.1:55432. Full suite:
  `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py`.
  Baseline **4469 passed**. No regressions.
- Do not weaken identity crypto invariants (card_policy validation, shared-envelope
  build, relationship anchor, owner_user_id == caller).
- Effect exactly-once/generation-fence semantics (PR A) must be preserved.

---

## Fix B — effect sinks honor `retryable` (contained, do FIRST)

Terminal-discard a deterministic capability failure instead of retrying forever.

### Task B1: `EffectTerminalError` + outbox discard-on-terminal

**Files:** `backend/db.py` (near `EffectDeliveryUncertainError`, db.py:7172),
`backend/model_api_runtime/v2/effect_outbox.py` (dispatch-failure handling,
~131-150 and outer except ~180-193).
**Test:** `tests/test_v2_effect_terminal_discard.py`.

- Add `class EffectTerminalError(RuntimeError)` in db.py: raised by a sink when a
  capability failed non-retryably; the outbox marks the effect `status='discarded'`
  (terminal, no retry, no wedge) and counts it as `discarded`.
- In effect_outbox dispatch-failure path: if
  `isinstance(deferred_dispatch_error, db.EffectTerminalError)` → mark the effect
  `discarded` on the cursor (not `_effect_record_error_on_cursor`), increment
  `discarded`, and do NOT re-raise (a terminal discard is a handled outcome, not a
  delivery failure). Otherwise unchanged.
- [ ] Test: a sink raising `EffectTerminalError` → effect row `status='discarded'`,
  not retried; `discarded` count bumped; `apply_pending_effects` returns normally
  (does not raise). A sink raising a plain `RuntimeError` still → retry
  (attempt_count++, pending).

### Task B2: capability sinks raise terminal on non-retryable failure

**Files:** `backend/model_api_runtime/v2/serve_worker.py` — `_sink_identity`
(1257), and the capability branch of `_sink_schedule` (1296). Factor a shared
helper `_raise_capability_result(result, *, terminal_code)` that raises
`EffectTerminalError(terminal_code)` when `not result.retryable`, else
`RuntimeError(terminal_code)`.
**Test:** `tests/test_v2_sink_retryable.py`.

- [ ] Test (`_sink_identity` with a fake `run_capability`): capability returns
  `ok=False, retryable=False` → sink raises `EffectTerminalError` (effect will be
  discarded). Capability returns `ok=False, retryable=True` → sink raises plain
  `RuntimeError` (effect retried). `effect_sink_release` still called on both (undo
  the claim) before raising.

**Outcome after B:** the fresh-start user's identity effect is discarded after one
attempt; the reply flows; no infinite reconcile. The identity is still not set
(that's Fix A).

---

## Fix A — identity_patch creates the card when none exists

When `_identity_profile_patch` finds the identity uninitialized, synthesize a
minimal VALID identity card from the patch + defaults and init it, instead of 409.

### Task A1: card bootstrap from a profile patch

**Files:** `backend/identity/actions.py` — `_identity_profile_patch` (115); a new
helper `_bootstrap_identity_from_patch(store, patch, api_key, runtime_token)`.
Reuse `identity_core.init_identity` / `_build_shared_envelope_for_store` +
`card_policy.validate_full_identity_card`.
**Test:** `tests/test_identity_patch_bootstrap.py`.

Design:
- In `_identity_profile_patch`, when `_identity_plain_for_action` returns None with
  err `identity_not_initialized`: build a minimal full card = `{agent_name: <from
  patch or a default>, self_introduction: <from patch or "">, dimensions: [], ...}`
  + the init-required fields with defaults: `days_with_user = 0`, a default
  `relationship_started_at` (now). Validate via `card_policy.validate_full_identity_card`;
  if invalid (e.g. patch had neither name nor intro), fall back to the current 409
  (nothing to create). Otherwise init the card (shared-envelope path, same as
  `init_identity`'s plaintext branch), THEN apply the patch (now a no-op/refinement).
- Confirm `card_policy.validate_full_identity_card` minimal requirements first
  (read it) — the default card must pass it. Adjust defaults to the minimum valid.
- [ ] Test: profile_patch with `self_introduction` on a user with NO identity →
  card created (identity now initialized, self_introduction set, relationship
  anchor stamped). profile_patch with an EMPTY/invalid patch on no card → still 409
  (nothing to bootstrap). profile_patch on an EXISTING card → unchanged (update
  path, no bootstrap).

### Task A2: integration — the wedge scenario end to end

**Test:** `tests/test_identity_patch_no_wedge_integration.py` (or extend
test_v2_worker*). Simulate: no identity card + a turn whose model calls
identity_patch → the effect applies (card created) OR (if bootstrap declined)
discards terminally (Fix B) → the reply is delivered and the job completes, NOT
wedged. Assert no `needs_reconciliation` effect remains.

---

## Verify + deploy

- Full suite green (4469 + new).
- On pre: the wedged user (usr_025fc32384fcf8a2) — after deploy, the stuck
  identity effect is discarded (B) on the next sweep; a fresh message with
  identity_patch creates the card (A). Confirm the conversation replies again.

## Notes / open checks (resolve during implementation)

- `_save_identity_action_payload` is UPDATE-ONLY (requires existing card +
  relationship anchor) — Fix A must go through the init/create path, not this
  helper. Confirmed actions.py:63.
- Read `card_policy.validate_full_identity_card` for the minimal valid card before
  choosing defaults.
- Check whether any OTHER capability-write sink (`_sink_memory`, etc.) should also
  honor `retryable` (Fix B pattern) — apply consistently.
