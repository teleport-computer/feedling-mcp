---
document_lifecycle: current
canonical_owner: docs/superpowers/specs/2026-09-15-tee-ciphertext-repair-design.md
---

# TEE Ciphertext Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent standalone TEE replication and verification from preserving ciphertext because of an empty process-local account registry, then add a guarded fleet repair path for existing effective-off users.

**Architecture:** A small `tee_replicator.policy` module reads the three-state content-encryption preference directly from the authoritative source PostgreSQL pool. Replication and verification consume that resolver through separate call paths; the existing per-user plaintext migration is widened from explicit-off to effective-off and wrapped by a deterministic, dry-run-first fleet coordinator.

**Tech Stack:** Python 3, psycopg 3, PostgreSQL JSONB, pytest, existing enclave decrypt client and plaintext migration transforms.

**Spec:** `docs/superpowers/specs/2026-09-15-tee-ciphertext-repair-design.md`

## Global Constraints

- Preserve ciphertext for explicit `content_encryption=on` users and unknown users.
- Treat an existing user with an unset preference as effective-off without modifying the user document.
- Never decrypt or rewrite `local_only`, missing-`K_enclave`, or terminal-preserved rows.
- Every mutation remains exact-old-shape compare-and-swap protected and produces content-free reports.
- Dry-run is the default; fleet apply requires the existing environment gate and a separate literal fleet confirmation.
- Keep mixed plaintext/ciphertext readers intact; this plan does not change the public API contract.

---

### Task 1: Authoritative Content-Encryption Policy Resolver

**Files:**
- Create: `backend/tee_replicator/policy.py`
- Modify: `backend/tee_replicator/worker.py`
- Modify: `tests/test_tee_carry_verbatim.py`
- Create: `tests/test_tee_replicator_policy.py`

**Interfaces:**
- Produces: `resolve_content_encryption(user_id: str, *, pool=None) -> str | None`, where existing explicit-on returns `"on"`, existing off or unset returns `"off"`, and an absent user returns `None`.
- Produces: `probe_policy_source(*, pool=None) -> dict[str, int]`, returning only `users`, `explicit_on`, `explicit_off`, and `default_off` counts and propagating database failures.
- Consumes: `db.get_pool()` when no pool is injected.

- [ ] **Step 1: Write failing resolver tests**

  Seed explicit-on, explicit-off, unset, and missing users in `tests/test_tee_replicator_policy.py`; assert the exact three-state results. Add a connection-failure test proving `probe_policy_source()` raises rather than returning an empty count.

- [ ] **Step 2: Run the resolver tests and confirm the module is missing**

  Run: `FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_tee_replicator_policy.py -q`

  Expected: collection fails because `tee_replicator.policy` does not exist.

- [ ] **Step 3: Implement the database-backed resolver**

  Use one exact-user query:

  ```python
  row = conn.execute(
      "SELECT doc->>'content_encryption' FROM users WHERE user_id=%s",
      (str(user_id),),
  ).fetchone()
  if row is None:
      return None
  return "on" if str(row[0] or "").strip().lower() == "on" else "off"
  ```

  Implement the source probe as one aggregate query. Do not catch database exceptions or log document values.

- [ ] **Step 4: Replace the worker's registry lookup**

  Keep `_carries_verbatim(user_id, target_policy)` and its bounded TTL cache stable for callers, but populate it from `policy.resolve_content_encryption(user_id)` and use `resolved != "off"` as the fail-safe predicate. Remove the lazy `accounts.registry` import and update its docstring.

- [ ] **Step 5: Prove fresh-process behavior and carry semantics**

  Update `tests/test_tee_carry_verbatim.py` so it mutates the seeded database preference rather than the registry cache. Assert explicit-on carries without enclave access, off/unset decrypt, and unknown carries without enclave access.

- [ ] **Step 6: Run focused tests**

  Run: `FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_tee_replicator_policy.py tests/test_tee_carry_verbatim.py tests/test_tee_replicator_worker.py -q`

  Expected: all pass.

- [ ] **Step 7: Commit**

  ```bash
  git add backend/tee_replicator/policy.py backend/tee_replicator/worker.py tests/test_tee_replicator_policy.py tests/test_tee_carry_verbatim.py
  git commit -m "fix: resolve TEE replication policy from PostgreSQL"
  ```

### Task 2: Standalone CLI Probe and Independent Verification

**Files:**
- Modify: `backend/tee_replicator/__main__.py`
- Modify: `backend/tee_shadow/verify.py`
- Modify: `tests/test_tee_replicator_main.py`
- Modify: `tests/test_tee_verify.py`

**Interfaces:**
- Consumes: `tee_replicator.policy.probe_policy_source()` before `worker.run_table()`.
- Consumes: `tee_replicator.policy.resolve_content_encryption()` directly from verification; verification must not call `worker._carries_verbatim()` or read its cache.

- [ ] **Step 1: Write failing CLI and verifier regression tests**

  Add an in-process CLI test that stubs `probe_policy_source()` to raise and asserts `worker.run_table()` is never called and no secret exception message is emitted. Add a successful probe test that asserts only aggregate counts precede the JSON report. In `tests/test_tee_verify.py`, poison `worker._carry_verbatim_cache` to `True` for an effective-off user and assert `_expected_doc()` still expects plaintext through the authoritative resolver.

- [ ] **Step 2: Run the new tests and confirm the old paths fail**

  Run: `FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_tee_replicator_main.py tests/test_tee_verify.py -q`

  Expected: the new assertions fail because the CLI has no source probe and verify delegates to `_carries_verbatim()`.

- [ ] **Step 3: Add the CLI startup probe**

  Before invoking `run_table`, call `policy.probe_policy_source()`. Emit a content-free JSON event to stderr such as:

  ```python
  {"event": "policy_source_ready", "users": 1008,
   "explicit_on": 6, "explicit_off": 3, "default_off": 999}
  ```

  Catch probe failure at the CLI boundary, print only `policy source probe failed: <exception-class>`, and exit `1`.

- [ ] **Step 4: Make verification resolve policy independently**

  In `_expected_doc()`, call `policy.resolve_content_encryption(user_id)` directly. Carry verbatim for `"on"` and unknown (`None`); transform for `"off"`. Preserve `target_policy.mode == "plaintext_all"` as the explicit override without calling the resolver.

- [ ] **Step 5: Run focused CLI and verification tests**

  Run: `FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_tee_replicator_main.py tests/test_tee_verify.py tests/test_admin_tee_replication.py -q`

  Expected: all pass and the verifier test remains green even with a poisoned worker cache.

- [ ] **Step 6: Commit**

  ```bash
  git add backend/tee_replicator/__main__.py backend/tee_shadow/verify.py tests/test_tee_replicator_main.py tests/test_tee_verify.py
  git commit -m "fix: independently verify TEE plaintext policy"
  ```

### Task 3: Effective-Off Single-User Migration Safety

**Files:**
- Modify: `backend/content/plaintext_migration.py`
- Modify: `tests/test_user_content_plaintext_migration.py`

**Interfaces:**
- Consumes: `tee_replicator.policy.resolve_content_encryption(user_id) -> str | None`.
- Produces: `run(..., require_explicit_off: bool = False) -> Result`; default admits existing effective-off users, including unset, while still rejecting explicit-on and unknown users.
- Produces: `cas_inline_doc()` and pointer/frame CAS paths recheck effective-off under the user-row lock immediately before mutation.

- [ ] **Step 1: Write failing unset-user and tier-race tests**

  Add tests proving apply succeeds for an existing unset user, rejects explicit-on and unknown users, and stops subsequent mutations when the preference changes to on between inventory and CAS. Cover inline documents and the existing R2 chat/frame promotion entry points.

- [ ] **Step 2: Run the focused migration tests and observe unset rejection**

  Run: `FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_user_content_plaintext_migration.py -q`

  Expected: new unset-user and race tests fail against the explicit-off-only gate.

- [ ] **Step 3: Centralize the effective-off check**

  Replace duplicated preference parsing with the authoritative resolver. Under `SELECT ... FOR UPDATE`, interpret only normalized explicit `on` as encrypted; a present unset/off row is effective-off, and a missing row rejects. Keep the optional `require_explicit_off=True` mode only for compatibility with callers that deliberately demand the older stricter gate.

- [ ] **Step 4: Thread the locked tier check through every writer**

  Ensure inline CAS, chat R2 promotion, frame promotion, and cleanup retry all verify the user remains effective-off in the same transaction or database mutation boundary used for the row change. Return `tier_changed` rather than classifying it as a decrypt/storage failure.

- [ ] **Step 5: Run migration and storage regression suites**

  Run: `FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_user_content_plaintext_migration.py -q`

  Then run: `~/fleet/bus/which_tests.sh backend/content/plaintext_migration.py backend/db.py`

  Execute the selector's emitted command to cover the concrete R2 Chat and Frame database helpers reached by this module.

- [ ] **Step 6: Commit**

  ```bash
  git add backend/content/plaintext_migration.py tests/test_user_content_plaintext_migration.py backend/db.py tests
  git commit -m "fix: migrate existing default-off user content safely"
  ```

### Task 4: Guarded Fleet Repair Coordinator

**Files:**
- Create: `backend/content/plaintext_repair.py`
- Create: `backend/migrate_effective_off_content_to_plaintext.py`
- Create: `tests/test_effective_off_content_repair.py`
- Modify: `deploy/DEPLOYMENTS.md`

**Interfaces:**
- Produces: `eligible_user_ids(*, start_after: str = "", user_limit: int = 0) -> list[str]`, ordered by exact `user_id`, including existing unset/off and excluding on.
- Produces: `run(*, apply=False, start_after="", user_limit=0, row_limit=0, rate=1.0, health_probe=None) -> RepairResult`.
- Consumes: `plaintext_migration.run(user_id, apply=apply, limit=row_limit, rate=rate)` one user at a time.
- CLI apply gates: `FEEDLING_ENABLE_PLAINTEXT_CONTENT_MIGRATION=1`, `--allow-plaintext-rewrite`, and `--confirm-all-effective-off ALL-EFFECTIVE-OFF`.

- [ ] **Step 1: Write failing selection, resume, gating, and health tests**

  Seed unordered on/off/unset users and assert deterministic selection, `start_after`, and `user_limit`. Assert dry-run never constructs a decryptor. Assert apply requires all three gates, stops a user on `tier_changed`, pauses admission after an unhealthy probe, resumes only after the configured consecutive healthy probes, and exits nonzero for unknown users, CAS loss, or transform failures. Reports may contain table/surface, truncated user/item identifiers, and fixed slugs only—never content or DSNs.

- [ ] **Step 2: Run the new suite and confirm imports fail**

  Run: `FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_effective_off_content_repair.py -q`

  Expected: collection fails because the coordinator and CLI do not exist.

- [ ] **Step 3: Implement dry-run-first deterministic coordination**

  Query only user IDs and normalized preference, order in SQL by `user_id`, and apply `start_after` before `LIMIT`. Invoke one user at a time and merge only content-free counters. Do not create a durable cursor: successful stored shape plus `start_after` is the resume contract.

- [ ] **Step 4: Implement health-gated apply and literal confirmation**

  Probe before every user and after any decrypt failure. Stop admitting users while unhealthy; require the configured consecutive healthy results before resuming. The default CLI rate is `1.0`, concurrency is fixed at one, and dry-run performs no health-dependent mutation.

- [ ] **Step 5: Document exact TEST-first operator commands**

  Add inventory, canary, resume, and abort examples to `deploy/DEPLOYMENTS.md`. Require checking `/healthz` release SHA and recording before/after ciphertext class counts. Explicitly state that PROD execution is not part of deployment and must be separately authorized.

- [ ] **Step 6: Run coordinator and combined regression suites**

  Run: `FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_effective_off_content_repair.py tests/test_user_content_plaintext_migration.py tests/test_tee_replicator_policy.py tests/test_tee_carry_verbatim.py tests/test_tee_replicator_main.py tests/test_tee_verify.py -q`

  Expected: all pass.

- [ ] **Step 7: Select and run repository-required tests**

  Run: `~/fleet/bus/which_tests.sh --vs origin/test`

  Then run the emitted command with the documented PostgreSQL environment. Because this changes encryption/enclave paths, also run the repository's TEST encryption-chain E2E before any promotion.

- [ ] **Step 8: Commit**

  ```bash
  git add backend/content/plaintext_repair.py backend/migrate_effective_off_content_to_plaintext.py tests/test_effective_off_content_repair.py deploy/DEPLOYMENTS.md
  git commit -m "feat: add guarded TEE ciphertext repair coordinator"
  ```
