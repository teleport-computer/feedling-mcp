# User Content Plaintext Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add and safely run an idempotent, single-user migration that converts enclave-decryptable legacy content envelopes to the plaintext storage shapes required after `content_encryption=off`, while leaving `local_only` content unchanged.

**Architecture:** A reusable migration module owns inventory, classification, decrypt/transform, compare-and-swap persistence, and structured counters. A thin CLI is dry-run by default and requires three independent apply gates. Existing `tee_replicator.transforms`, the runtime-token decrypt callback, chat R2 lifecycle helper, and frame storage protocols remain the canonical shape and cryptographic implementations.

**Tech Stack:** Python 3.11, psycopg 3/PostgreSQL JSONB, pytest, existing Feedling enclave runtime-token and R2 helpers.

**Spec:** `docs/superpowers/specs/2026-09-14-user-content-plaintext-migration-design.md`

**Global Constraints:** Exact `--user` is mandatory; never log plaintext, ciphertext, wrapped keys, or object keys; dry-run performs no decrypt or writes; apply rechecks that the user exists and explicitly has `content_encryption=off`; every inline mutation is CAS-protected; R2 mutations use existing crash-safe helpers; `local_only`/missing-`K_enclave` rows are reported and retained.

---

### Task 1: Define the migration contract and CLI safety gates

**Files:**
- Create: `backend/content/plaintext_migration.py`
- Create: `backend/migrate_user_content_to_plaintext.py`
- Create: `tests/test_user_content_plaintext_migration.py`

1. Write failing tests proving: exact user is required; apply is rejected unless `--allow-plaintext-rewrite` and `FEEDLING_ENABLE_PLAINTEXT_CONTENT_MIGRATION=1` are both present; dry-run does not construct a decrypt callback; missing/on/default-unset preference is rejected for apply; output contains counts only.
2. Run the focused test file and confirm the failures are caused by the missing implementation.
3. Implement immutable item/result data structures, preference gate, argument parsing, and dry-run orchestration with no mutation path.
4. Run the focused tests until green.
5. Commit the task.

### Task 2: Inventory and classify every supported content surface

**Files:**
- Modify: `backend/content/plaintext_migration.py`
- Modify: `tests/test_user_content_plaintext_migration.py`

1. Write failing PostgreSQL-backed tests with mixed plaintext, shared encrypted, `local_only`, malformed, and R2-pointer fixtures across live Chat, archived Chat, Memory, World Book, Identity, and Frames.
2. Implement bounded single-user inventory queries and pure classification. Reuse `tee_replicator.transforms.needs_decrypt` and frame metadata rules; classify encrypted sub-envelopes independently enough that mixed Chat rows remain migratable.
3. Assert dry-run counters are deterministic and no decrypt/R2/write callback runs.
4. Run focused tests and commit.

### Task 3: CAS-migrate inline Chat, Memory, World Book, and Identity rows

**Files:**
- Modify: `backend/content/plaintext_migration.py`
- Modify: `tests/test_user_content_plaintext_migration.py`

1. Write failing tests for successful conversion, crypto-field removal, mixed Chat main/thinking/caption conversion, idempotent rerun, CAS loss, preference flip during the run, malformed UTF-8 handling, and local-only preservation.
2. Transform with the existing `tee_replicator.transforms` functions and a single cached per-user `_make_decrypt` callback.
3. Add exact-old-document CAS writers that lock/recheck the user's explicit-off preference in the same transaction. Preserve ordering columns and primary keys. Mark legacy shadow rows for requeue only after a successful commit where applicable.
4. Run focused and existing transform/content-shape tests; commit.

### Task 4: Migrate R2-backed Chat bodies and Frames without unsafe overwrite windows

**Files:**
- Modify: `backend/content/plaintext_migration.py`
- Modify: `backend/db.py`
- Modify: `tests/test_user_content_plaintext_migration.py`
- Modify or create focused DB tests as appropriate.

1. Write failing tests proving Chat delegates to `db.migrate_chat_r2_pointer_to_plaintext`, sub-envelopes are CAS-migrated after pointer promotion, and a lost CAS is reported without claiming success.
2. Add a frame-specific crash-safe migration helper: write plaintext to a fresh/versioned R2 object (or verified inline shape when storage is disabled), then CAS the row from the exact sealed shape while rechecking explicit-off preference; retain/queue the old object for cleanup according to the existing frame lifecycle policy. Never overwrite the only ciphertext object before DB promotion.
3. Write tests for inline and R2 frame success, R2 upload failure, missing object, preference flip, CAS loss, digest/size metadata, and idempotent rerun.
4. Run all frame/R2 focused tests and commit.

### Task 5: Add rate limiting, canary/resume controls, and operator-safe reporting

**Files:**
- Modify: `backend/content/plaintext_migration.py`
- Modify: `backend/migrate_user_content_to_plaintext.py`
- Modify: `tests/test_user_content_plaintext_migration.py`

1. Write failing tests for `--limit`, `--rate`, stable ordering, retry-safe rerun, nonzero exit on failures, and redacted JSON/text summaries.
2. Implement canary limit and monotonic rate pacing. Treat already-plaintext and skipped-local-only as nonfailures; malformed/decrypt/storage/CAS failures are explicit counters and yield a nonzero exit unless they are benign CAS-to-already-plaintext races.
3. Run focused tests and commit.

### Task 6: Verify, deploy to test, then migrate the named production user

**Files:**
- Modify: `docs/CHANGELOG.md` or operator documentation if the repository convention requires it.

1. Run `~/fleet/bus/which_tests.sh` and execute the required L1/L2 suites, including all existing encryption-shape, TEE replicator, R2, and frame tests.
2. Run compile/lint/static checks selected by repository tooling. Review `git diff` for secrets, user content, and unrelated changes.
3. Deploy the exact branch commit to test using the repository's documented test deployment path. On test, exercise dry-run and an apply fixture/canary; verify plaintext shapes and no enclave decrypt on reread.
4. Promote only through the permitted `test`/`pre` branch flow. Confirm the production runtime reports the exact approved commit before touching data.
5. On production, run dry-run for `usr_453c4b85a306f5d2`; save only aggregate counts. Reconfirm `content_encryption=off` and take a content-free count/shape snapshot.
6. Run `--apply --allow-plaintext-rewrite --limit 20 --rate 1`; verify counts, app history, backend/enclave health, and database shapes.
7. If healthy, resume at no more than `--rate 2`; rerun dry-run until only already-plaintext and skipped-local-only remain. Record final aggregate counts and operational evidence.
8. Use superpowers:verification-before-completion, then superpowers:finishing-a-development-branch before presenting integration choices.
