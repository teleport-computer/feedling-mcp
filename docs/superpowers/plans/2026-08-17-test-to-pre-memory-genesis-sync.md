# Test to Pre Memory and Genesis Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge `test@8034690e` into `pre@3ebc700f`, preserve PRE content-encryption and TEE boundaries, converge the RDS/TEE migration chains, and deploy the verified result to both PRE CVMs.

**Architecture:** Use a normal two-parent Git merge and resolve the nine known conflicts by composing TEST's Memory Garden, Genesis, identity, profile, wake, and migration-test behavior with PRE's content-shape-aware paths. Add one no-op RDS merge revision and one schema-equivalent TEE revision so both runtime schema modes have a single release head. Verification gates the remote PRE push, TEE migration, main-CVM deployment, runner deployment, and three-entrypoint live acceptance.

**Tech Stack:** Python 3.11, pytest, PostgreSQL 16, Alembic, ASGI, Phala CVM, Docker Compose, GitHub Actions, MDX/OpenAPI, npm.

## Global Constraints

- Frozen base: `origin/pre@3ebc700ff006bca7d54d141028c4f85596ef72fc`.
- Frozen input: `origin/test@8034690eef39104511c31870a33b18eb338d6074`.
- Preserve PRE per-user plaintext/encrypted content routing and strict enclave boundary.
- Preserve PRE TEE-primary startup contract and dual enclave entry topology.
- Update both PRE main and runner CVMs; do not deploy production.
- Do not force-push `pre`.
- The final release commit must descend from both frozen inputs.

---

### Task 1: Merge the frozen TEST input and resolve content conflicts

**Files:**
- Modify: `backend/capabilities/identity.py`
- Modify: `backend/memory/memory_core.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/model_api_runtime/v2/profile_store.py`
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `tests/test_memory_migration.py`
- Modify: `tests/test_v2_jobs_migration.py`
- Modify: `tests/test_v2_profile_storage.py`

**Interfaces:**
- Consumes: frozen PRE content-shape helpers, frozen TEST Memory Garden and Runtime V2 APIs.
- Produces: a two-parent merge with no conflict markers and composed plaintext/encrypted behavior.

- [ ] **Step 1: Revalidate the frozen inputs before mutation**

Run:

```bash
git fetch origin test pre
test "$(git rev-parse origin/pre)" = "3ebc700ff006bca7d54d141028c4f85596ef72fc"
test "$(git rev-parse origin/test)" = "8034690eef39104511c31870a33b18eb338d6074"
git status --short
```

Expected: both assertions succeed and the only branch change is this committed plan/spec history.

- [ ] **Step 2: Start the real merge without committing**

Run:

```bash
git merge --no-ff --no-commit 8034690eef39104511c31870a33b18eb338d6074
git diff --name-only --diff-filter=U
```

Expected conflict list:

```text
backend/capabilities/identity.py
backend/memory/memory_core.py
backend/model_api_runtime/v2/jobs_store.py
backend/model_api_runtime/v2/profile_store.py
backend/model_api_runtime/v2/serve_worker.py
docs-site/content/docs/architecture.mdx
tests/test_memory_migration.py
tests/test_v2_jobs_migration.py
tests/test_v2_profile_storage.py
```

Stop if another file appears.

- [ ] **Step 3: Resolve identity and Memory conflicts by composition**

`backend/capabilities/identity.py` must import both dependencies:

```python
from core import enclave as core_enclave
from core import envelope as core_envelope
from identity import card_policy, card_view, identity_core
```

`backend/memory/memory_core.py::add` must first call `core_envelope.validate_uploaded_envelope`, then normalize the timestamp:

```python
gate_err = core_envelope.validate_uploaded_envelope(
    envelope,
    user_id=store.user_id,
)
if gate_err is not None:
    return gate_err, 400
occurred_at = memory_timestamps.normalize(envelope.get("occurred_at"))
```

Retain the existing owner, visibility, and memory-type checks after normalization.

- [ ] **Step 4: Resolve Runtime V2 capture and profile conflicts**

In `jobs_store._validate_capture_actions`, retain PRE's mutually exclusive ciphertext/plaintext validation, then normalize `occurred_at` before building `clean_action`:

```python
occurred_at = memory_timestamps.normalize(envelope.get("occurred_at"))
if not occurred_at:
    raise ValueError("capture envelope invalid occurred_at")
```

In `profile_store.py`:

- retain `_validate_content_field` rather than encrypted-only validation;
- canonicalize output to `memory` and `style`;
- accept legacy `user` only as a read fallback when `style` is absent;
- reject simultaneous `style` and `user`;
- use the resolved `style_key`/`style_field` in prompt selection;
- preserve untouched prior fields via validated deep copies.

The normalized field block must have this shape:

```python
style_key = "style" if style is not None else "user"
style_value = style if style is not None else legacy_user
if (memory is None) != (style_value is None):
    raise ProfileStorageError("profile_fields_torn")
if memory is not None:
    normalized["memory"] = _validate_content_field(memory, "memory")
    normalized[style_key] = _validate_content_field(style_value, style_key)
```

- [ ] **Step 5: Resolve prompt, documentation, and test conflicts**

Use TEST's `_load_identity_card_block` and `identity_card_or_persona` fallback in `serve_worker.py`; keep identity reads through `cap_identity.get`, whose merged implementation handles both PRE content shapes. Use TEST's trusted-prefix rendering behavior.

Keep both architecture sections: Genesis/profile publication immediately before the staged profile-refresh description, and managed PostgreSQL promotion as its own following subsection.

In `tests/test_memory_migration.py`, retain `actions` and import the migrated prompt module:

```python
from memory import actions, migration
from memory_garden.prompts import migrate as mp
```

In `tests/test_v2_jobs_migration.py`, retain TEST's derived-head database-install checks during the merge. Task 2 will then add exact assertions for the new release head before creating the convergence revisions.

In `tests/test_v2_profile_storage.py`, retain both plaintext/mixed-shape cases and TEST's untouched-side/MEMORY-STYLE/retry cases.

- [ ] **Step 6: Prove conflict cleanup and run the focused merge suite**

Run:

```bash
git diff --check
test -z "$(git diff --name-only --diff-filter=U)"
! rg -n '^(<<<<<<<|=======|>>>>>>>)' backend tests docs-site
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_capabilities_identity.py \
  tests/test_memory_migration.py \
  tests/test_memory_actions_guard.py \
  tests/test_v2_jobs_store.py \
  tests/test_v2_profile_storage.py \
  tests/test_v2_profile_cards.py \
  tests/test_v2_serve_worker.py \
  tests/test_v2_workspace_unit.py -q
```

Expected: no conflict markers and all selected tests pass.

- [ ] **Step 7: Commit the merge**

Run:

```bash
git add backend tests docs-site .github deploy tools CONTRIBUTING.md docs
git commit -m "merge(test): sync Memory Garden and Runtime V2 changes into pre"
```

Expected: one two-parent merge commit.

---

### Task 2: Converge RDS and TEE wake-outcome migrations with TDD

**Files:**
- Create: `backend/alembic/versions/0090_merge_wake_outcomes.py`
- Create: `backend/alembic_tee/versions/0022_v2_wake_outcomes.py`
- Modify: `tests/test_v2_jobs_migration.py`
- Modify: `tests/test_pre_runtime_preflight.py`
- Modify: `tests/test_pre_test_migration_convergence.py`

**Interfaces:**
- Consumes: RDS heads `0089_merge_pre_test_agent_jobs` and `0089_v2_wake_outcomes`; TEE head `0021_agent_jobs_available_at`.
- Produces: sole RDS head `0090_merge_wake_outcomes`; sole TEE head `0022_v2_wake_outcomes` with equivalent columns and updated frozen marker.

- [ ] **Step 1: Update tests first to state the new heads and parity contract**

Change graph assertions to:

```python
assert script.get_heads() == ["0090_merge_wake_outcomes"]
assert set(script.get_revision("0090_merge_wake_outcomes").down_revision) == {
    "0089_merge_pre_test_agent_jobs",
    "0089_v2_wake_outcomes",
}
```

Change PRE TEE assertions to:

```python
assert script.get_heads() == ["0022_v2_wake_outcomes"]
assert script.get_revision("0022_v2_wake_outcomes").down_revision == (
    "0021_agent_jobs_available_at"
)
```

Add parity assertions that both RDS and TEE upgrade SQL contain `wake_result TEXT` and `wake_result_reason TEXT`, and that the TEE marker contains `0022_v2_wake_outcomes`.

- [ ] **Step 2: Run the migration tests and confirm RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_v2_jobs_migration.py \
  tests/test_pre_runtime_preflight.py \
  tests/test_pre_test_migration_convergence.py -q
```

Expected: failure because `0090_merge_wake_outcomes` and `0022_v2_wake_outcomes` do not exist and the graph still has two RDS heads.

- [ ] **Step 3: Add the RDS merge revision**

Create:

```python
"""Merge PRE history with auditable Runtime V2 wake outcomes."""

revision = "0090_merge_wake_outcomes"
down_revision = (
    "0089_merge_pre_test_agent_jobs",
    "0089_v2_wake_outcomes",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
```

- [ ] **Step 4: Add the TEE schema revision**

Create an Alembic revision with:

```python
from alembic import op

revision = "0022_v2_wake_outcomes"
down_revision = "0021_agent_jobs_available_at"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE agent_jobs
  ADD COLUMN IF NOT EXISTS wake_result TEXT,
  ADD COLUMN IF NOT EXISTS wake_result_reason TEXT;
"""

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0022_v2_wake_outcomes"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""

def upgrade() -> None:
    op.execute(_UP)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
```

- [ ] **Step 5: Run migration tests and confirm GREEN**

Run the command from Step 2.

Expected: all selected tests pass, including real PostgreSQL upgrade/downgrade coverage.

- [ ] **Step 6: Commit migration convergence**

```bash
git add backend/alembic backend/alembic_tee \
  tests/test_v2_jobs_migration.py \
  tests/test_pre_runtime_preflight.py \
  tests/test_pre_test_migration_convergence.py
git commit -m "fix(pre): converge wake outcome migration heads"
```

---

### Task 3: Verify functional and security boundaries

**Files:**
- Verify: merged backend and test tree
- Modify only if a focused regression exposes a defect within the approved specification.

**Interfaces:**
- Consumes: Tasks 1-2 merged behavior.
- Produces: evidence that encrypted/plaintext, Genesis, Memory Garden, identity, profile, and wake paths coexist on PRE.

- [ ] **Step 1: Run migration and PRE deploy gates**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_v2_jobs_migration.py \
  tests/test_pre_runtime_preflight.py \
  tests/test_pre_test_migration_convergence.py \
  tests/test_phase4_cutover.py \
  tests/test_tee_schema.py -q
```

- [ ] **Step 2: Run plaintext/encrypted boundary suites**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_content_encryption_preference.py \
  tests/test_plaintext_enclave_boundary.py \
  tests/test_plaintext_memory_quality.py \
  tests/test_read_side_shape_routing.py \
  tests/test_write_side_format_routing.py \
  tests/test_encryption_surface_frozen.py \
  tests/test_chat_core_file_ingest.py \
  tests/test_v2_downloadable_files.py -q
```

- [ ] **Step 3: Run Memory Garden, Genesis, profile, identity, and wake suites**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_memory_garden_purity.py \
  tests/test_memory_garden_storage_port.py \
  tests/test_memory_garden_dreaming.py \
  tests/test_genesis_profile_dual_write.py \
  tests/test_genesis_identity_field_lock.py \
  tests/test_identity_actions.py \
  tests/test_v2_profile_storage.py \
  tests/test_v2_profile.py \
  tests/test_v2_jobs_store.py \
  tests/test_v2_wake_worker.py \
  tests/test_v2_tool_loop.py -q
```

Expected for every step: zero failures. If a regression fails, reproduce it in isolation, identify its cause, add or strengthen the narrow regression test, make the minimum in-scope correction, and commit that correction separately.

---

### Task 4: Run full repository and documentation verification

**Files:**
- Verify: entire repository
- Regenerate: `docs-site/openapi/public.json`

**Interfaces:**
- Consumes: exact candidate release commit.
- Produces: complete local release evidence and a clean worktree.

- [ ] **Step 1: Run the full PostgreSQL-backed pytest suite**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests -q --ignore=tests/test_api.py
```

Expected: zero failures; report exact pass/skip/xfail/warning counts.

- [ ] **Step 2: Run OpenAPI contracts**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/openapi -q
```

- [ ] **Step 3: Regenerate and verify public documentation**

```bash
cd docs-site
npm ci
npm run openapi:generate
npm run types:check
npm run lint
npm run build
cd ..
git diff --check
git status --short
```

If OpenAPI regeneration changes `docs-site/openapi/public.json`, review and commit only an intentional contract update. Otherwise the worktree must remain clean.

- [ ] **Step 4: Verify ancestry, migration heads, and release cleanliness**

```bash
git merge-base --is-ancestor 3ebc700ff006bca7d54d141028c4f85596ef72fc HEAD
git merge-base --is-ancestor 8034690eef39104511c31870a33b18eb338d6074 HEAD
PYTHONPATH=backend /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python - <<'PY'
from alembic.config import Config
from alembic.script import ScriptDirectory
for ini, loc, expected in (
    ("backend/alembic.ini", "backend/alembic", ["0090_merge_wake_outcomes"]),
    ("backend/alembic_tee.ini", "backend/alembic_tee", ["0022_v2_wake_outcomes"]),
):
    cfg = Config(ini)
    cfg.set_main_option("script_location", loc)
    assert ScriptDirectory.from_config(cfg).get_heads() == expected
PY
test -z "$(git status --porcelain)"
```

- [ ] **Step 5: Request independent code review**

Review the exact `origin/pre..HEAD` diff for:

- loss of PRE plaintext/encrypted routing;
- encrypted-only assumptions reintroduced by TEST;
- broken MEMORY/STYLE legacy compatibility;
- incomplete RDS/TEE schema parity;
- PRE deploy/preflight head drift;
- documentation or OpenAPI contract gaps.

Resolve every Critical or Important finding, rerun the affected focused suite, and obtain a final “ready” review.

---

### Task 5: Push, migrate, deploy, and verify PRE

**Files:**
- Remote branch: `pre`
- GitHub Actions workflows: image publication, CI, TEE migration
- Deployments: PRE main CVM and PRE runner CVM

**Interfaces:**
- Consumes: clean reviewed release commit from Task 4.
- Produces: remote PRE and both CVMs running that exact release.

- [ ] **Step 1: Confirm remote PRE has not moved**

```bash
git fetch origin pre
test "$(git rev-parse origin/pre)" = "3ebc700ff006bca7d54d141028c4f85596ef72fc"
```

If it moved, stop and re-audit instead of force-pushing.

- [ ] **Step 2: Push the candidate to PRE**

```bash
git push origin HEAD:pre
```

- [ ] **Step 3: Wait for image publication and CI preflight**

Use `gh run list` and `gh run view` to identify workflows whose `headSha` equals the release commit. The first CI attempt may stop at the TEE schema preflight while the live database is still at `0021`; all other unexpected failures are blockers.

- [ ] **Step 4: Run the PRE TEE migration**

Dispatch the repository's PRE TEE migration workflow for the exact release ref. Verify its logs report:

```text
code head=0022_v2_wake_outcomes db=0022_v2_wake_outcomes
PRE application startup contract: ok
```

- [ ] **Step 5: Rerun CI and deploy both CVMs**

Rerun the failed CI attempt after migration. Require successful main-CVM deploy, main canary, compose-hash publication, runner deploy, and runner compose-hash publication.

- [ ] **Step 6: Verify all three public entrypoints**

```bash
curl -sS -i https://pre-api.feedling.app/healthz
curl -sS -i https://pre-enclave.feedling.app/healthz
curl -ksS -i https://7d18a1f234a0d90e5f643cac8283b6048451b8f7-5003s.dstack-pha-prod9.phala.network/healthz
```

Expected: HTTP 200 and exact release SHA on all three; custom enclave is `attested_ingress` with TLS disabled behind ingress, direct enclave is `direct_tls` with TLS enabled.

- [ ] **Step 7: Verify both CVMs and remote branch**

Read `deploy/pre-cvm-id.txt` and `deploy/pre-runner-cvm-id.txt`, then inspect Docker images, health, state, and restart counts through the `amiller-users-projects` Phala profile. Require the exact short release tag, running state, healthy enclave containers, and zero restarts for backend, enclave, enclave-domain, serve-worker, and agent-runner.

Finally run:

```bash
git fetch origin pre
test "$(git rev-parse origin/pre)" = "$(git rev-parse HEAD)"
git status --short
```

Preserve the isolated worktree for follow-up.
