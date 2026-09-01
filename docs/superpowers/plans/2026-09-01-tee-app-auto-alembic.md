---
document_lifecycle: current
canonical_owner: docs/superpowers/specs/2026-09-01-tee-app-auto-alembic-design.md
---
# TEE App-Role Automatic Alembic Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a TEE-primary backend process upgrade `alembic_tee` at startup through the primary `DATABASE_URL` logged in as `app`, before it accepts traffic.

**Architecture:** `app` inherits `feedling_owner`, so the existing Gunicorn-master and standalone-runner calls to `db.init_schema()` can run the existing TEE Alembic migration chain. In exact `FEEDLING_DATABASE_SCHEMA=tee` mode the migration connection comes from the primary app DSN; legacy shadow modes keep their explicit owner migration-DSN precedence. The process asserts the final version and required triggers only after upgrade has completed.

**Tech Stack:** PostgreSQL role membership, Bash provisioning, Python 3, psycopg, Alembic, pytest.

**Spec:** `docs/superpowers/specs/2026-09-01-tee-app-auto-alembic-design.md`

## Global Constraints

- Do not change CI workflows or cause a migration during CI.
- Do not inject or persist the `feedling_owner` DSN in application runtime configuration.
- Preserve the legacy plaintext-shadow migration selector behavior outside exact TEE-primary mode.
- Keep migration execution in the existing pre-ready startup path; a failed migration must fail the new process before it is ready.
- Leave unrelated worktree changes untouched and never log credentials or full DSNs.

## File Structure

- Modify `deploy/postgres/ensure-roles.sh`: grant `feedling_owner` membership to `app` during idempotent role provisioning and correct the privilege comment.
- Modify `backend/alembic_tee/connection.py`: select `DATABASE_URL` for exact TEE-primary migrations and retain legacy owner selectors otherwise.
- Modify `backend/db.py`: upgrade TEE Alembic under the existing schema lock before its version and trigger assertions.
- Modify `tests/test_plaintext_shadow_schema.py`: cover selector branching and preserve the legacy no-fallback contract.
- Modify `tests/test_tee_primary_startup.py`: assert the startup upgrade precedes readiness checks and failures remain blocking.
- Add `tests/test_tee_app_role_ddl_contract.py`: lock the provisioning membership grant in a source-level regression test.
- Modify `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` and `deploy/DEPLOYMENTS.md`: document privilege scope, automatic startup migration, and operational rollback.

---

## Task 1: Grant the app role owner membership idempotently

**Files:**
- Create: `tests/test_tee_app_role_ddl_contract.py`
- Modify: `deploy/postgres/ensure-roles.sh`

- [ ] **Step 1: Write the failing provisioning-contract test.**

```python
from pathlib import Path


def test_tee_role_bootstrap_grants_owner_membership_to_app() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "deploy/postgres/ensure-roles.sh"
    ).read_text(encoding="utf-8")

    assert r'GRANT \"${POSTGRES_USER}\" TO app;' in source
    assert "DDL will be rejected" not in source
```

- [ ] **Step 2: Run the test and confirm it fails.**

Run: `uv run pytest tests/test_tee_app_role_ddl_contract.py -q`

Expected: failure because the role bootstrap does not currently grant owner membership and still describes DDL as rejected.

- [ ] **Step 3: Implement the minimal provisioning change.**

In the SQL supplied to `ensure_role app`, make the first grant:

```bash
GRANT "${POSTGRES_USER}" TO app;
```

Update the adjacent role comment to explain that `app` inherits the database-owner role so startup Alembic can run. Keep the existing schema, table, sequence, and default privilege grants; do not alter `tee_replicator` or monitoring roles.

- [ ] **Step 4: Re-run the focused test.**

Run: `uv run pytest tests/test_tee_app_role_ddl_contract.py -q`

Expected: PASS.

- [ ] **Step 5: Commit the provisioning contract.**

```bash
git add deploy/postgres/ensure-roles.sh tests/test_tee_app_role_ddl_contract.py
git commit -m "feat: allow app role to run TEE migrations"
```

## Task 2: Select the app DSN only in TEE-primary mode

**Files:**
- Modify: `tests/test_plaintext_shadow_schema.py`
- Modify: `backend/alembic_tee/connection.py`

- [ ] **Step 1: Add failing tests for both selector branches.**

Add a test that sets `FEEDLING_DATABASE_SCHEMA=tee`, a primary `DATABASE_URL`, and a different `TEE_MIGRATION_DATABASE_URL`, then expects the normalized primary DSN. Explicitly set `FEEDLING_DATABASE_SCHEMA=rds` in the legacy precedence/no-fallback test so it continues to validate shadow behavior.

```python
monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
monkeypatch.setenv("DATABASE_URL", "postgresql://app/primary")
monkeypatch.setenv("TEE_MIGRATION_DATABASE_URL", "postgresql://owner/legacy")

assert migration_database_url() == "postgresql+psycopg://app/primary"
```

- [ ] **Step 2: Run the selector tests and confirm the new test fails.**

Run: `uv run pytest tests/test_plaintext_shadow_schema.py -q`

Expected: the new exact-TEE test fails because the selector still chooses the owner migration variable.

- [ ] **Step 3: Implement explicit mode selection.**

Refactor `migration_database_url()` so that:

```python
if os.environ.get("FEEDLING_DATABASE_SCHEMA", "rds").strip().lower() == "tee":
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("TEE primary database URL is not set")
    return _normalize_postgres_url(url)
```

For every other mode, retain the exact existing precedence: `PLAINTEXT_SHADOW_MIGRATION_DATABASE_URL`, then `TEE_MIGRATION_DATABASE_URL`, then `TEE_DATABASE_URL`; do not fall back to `DATABASE_URL`. Extract normalization only if necessary to keep the two branches identical in URL handling.

- [ ] **Step 4: Re-run the selector tests.**

Run: `uv run pytest tests/test_plaintext_shadow_schema.py -q`

Expected: PASS, including legacy no-fallback behavior.

- [ ] **Step 5: Commit the connection-selection change.**

```bash
git add backend/alembic_tee/connection.py tests/test_plaintext_shadow_schema.py
git commit -m "feat: use app DSN for TEE primary migrations"
```

## Task 3: Upgrade TEE schema before its readiness assertions

**Files:**
- Modify: `tests/test_tee_primary_startup.py`
- Modify: `backend/db.py`

- [ ] **Step 1: Extend the startup fake to record events and add a failing ordering test.**

Teach `_configure_startup(...)` to append `"upgrade"`, `"head"`, and `"triggers"` events. Monkeypatch `alembic_tee.upgrade_head` to append `"upgrade"`. Add a test asserting `db.init_schema()` produces:

```python
["upgrade", "head", "triggers"]
```

Also add a test that makes `upgrade_head()` raise and asserts the exception escapes; this demonstrates the process cannot become ready after a migration failure.

- [ ] **Step 2: Run the TEE startup tests and confirm the new ordering test fails.**

Run: `uv run pytest tests/test_tee_primary_startup.py -q`

Expected: failure because TEE startup currently checks existing state without invoking `upgrade_head()`.

- [ ] **Step 3: Implement the locked upgrade path.**

Inside the existing exact-TEE branch of `db.init_schema()`, import `alembic_tee` and run:

```python
with _schema_lock:
    alembic_tee.upgrade_head()
    # existing version and trigger queries/assertions
```

Keep the current connection and trigger checks after the upgrade, still under `_schema_lock`. Preserve the version mismatch and missing-trigger failures, but revise their wording so they describe failed automatic convergence rather than directing operators to a separate owner-only migration workflow. Use the module's existing logging style and do not catch migration exceptions.

- [ ] **Step 4: Re-run focused startup tests.**

Run: `uv run pytest tests/test_tee_primary_startup.py tests/test_tee_schema.py -q`

Expected: PASS. Existing trigger-missing coverage still proves the post-upgrade safety gate remains active.

- [ ] **Step 5: Run the related startup/preflight suite.**

Run: `uv run pytest tests/test_plaintext_shadow_schema.py tests/test_pre_runtime_preflight.py tests/test_tee_primary_startup.py tests/test_tee_schema.py tests/test_tee_app_role_ddl_contract.py -q`

Expected: PASS. This confirms no CI workflow contract was changed and both TEE selector modes remain covered.

- [ ] **Step 6: Commit the runtime migration behavior.**

```bash
git add backend/db.py tests/test_tee_primary_startup.py
git commit -m "feat: auto-upgrade TEE schema at app startup"
```

## Task 4: Document deployment behavior and operational rollback

**Files:**
- Modify: `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md`
- Modify: `deploy/DEPLOYMENTS.md`

- [ ] **Step 1: Document the runtime contract.**

State that in exact TEE-primary mode the app's primary `DATABASE_URL` is used for Alembic before readiness, because `app` inherits `feedling_owner`. Note that this is intentionally wider than CRUD-only access, no owner DSN is injected into the service, and CI remains a non-migrating verifier.

- [ ] **Step 2: Document sequencing and rollback.**

Specify: first apply the idempotent role-provisioning grant with owner credentials; then deploy normally. If startup migration fails, the new process must remain unready and the previous healthy release stays serving. Rollback privilege using owner credentials only after new code no longer expects automatic migration:

```sql
REVOKE feedling_owner FROM app;
```

Do not describe Alembic downgrade as an automatic rollback.

- [ ] **Step 3: Validate documentation references.**

Run: `rg -n "owner-only|DDL will be rejected|TEE_MIGRATION_DATABASE_URL|DATABASE_URL|feedling_owner FROM app" deploy/postgres/ensure-roles.sh docs/TEE_POSTGRES_SHADOW_PROVISIONING.md deploy/DEPLOYMENTS.md`

Expected: docs and provisioning script agree on the app-role migration model; no stale owner-only instructions remain for exact TEE-primary startup.

- [ ] **Step 4: Commit documentation.**

```bash
git add docs/TEE_POSTGRES_SHADOW_PROVISIONING.md deploy/DEPLOYMENTS.md
git commit -m "docs: describe TEE app startup migrations"
```

## Task 5: Final verification and handoff

**Files:**
- Verify only: all files changed above

- [ ] **Step 1: Run the complete backend test suite.**

Run: `uv run pytest tests -q`

Expected: PASS. If the environment requires the established local PostgreSQL test DSN, use the repository's documented test invocation instead; do not suppress failures or skip the new coverage.

- [ ] **Step 2: Inspect the final diff and working tree.**

Run:

```bash
git diff --check
git status --short
git log --oneline -3
```

Expected: no whitespace errors; only the intended commits/changes on `codex/app-auto-alembic-tee`.

- [ ] **Step 3: Perform one live role verification only with explicit operational authorization.**

After the owner provisioning command is applied to the target TEE database, use the app DSN to verify only:

```sql
SELECT current_user, pg_has_role(current_user, 'feedling_owner', 'member');
```

Expected: `app | true`. Do not execute production role changes, deploy, or migrations as part of this code implementation plan.

- [ ] **Step 4: Handoff.**

Report the commit IDs, focused and full test results, the privilege expansion, and the required operator order: provision role membership first, then perform a normal backend deploy when authorized.
