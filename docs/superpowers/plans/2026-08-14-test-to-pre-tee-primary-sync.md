# Test → Pre TEE-primary Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Safely merge the current backend `test` branch into `pre`, converge both Alembic chains for the TEE-primary topology, and deploy the verified result to the pre environment.

**Architecture:** Start from `origin/pre` in the existing isolated worktree. First make the pre baseline understand the test-side RDS revisions and add equivalent sequential TEE revisions, then merge `origin/test` and resolve the eight known conflicts by preserving pre plaintext/TEE-primary behavior while accepting Runtime V2 changes. The existing pre schema preflight intentionally blocks CVM mutation until the owner migration workflow reaches the new TEE head.

**Tech Stack:** Python 3.12, Alembic, PostgreSQL/psycopg, pytest, GitHub Actions, Phala CVMs, Next.js documentation site.

## Global Constraints

- Work only in `.worktrees/sync-test-to-pre-20260814` on `codex/sync-test-to-pre-20260814`.
- Preserve pre TEE-primary, plaintext/encrypted dual-tier behavior, preflight gates, and both CVM identities.
- Accept test Runtime V2 three-pool, slot lifecycle, wake observation, first-chat activation, MCP diagnostics, and tooling changes.
- Maintain exactly one RDS Alembic head and exactly one TEE Alembic head.
- Do not modify `main`, `prod`, the iOS repository, perception permission aggregation, or model tool selection.
- Do not bypass the branch-flow check, TEE migration workflow, pre schema preflight, or GitHub Actions deployment.
- Update and validate public docs because the merged test changes cover architecture, trust boundaries, and deployment topology.

---

### Task 1: Add dual migration-chain convergence to the pre baseline

**Files:**
- Create: `backend/alembic/versions/0085_v2_wake_shadow_decisions.py`
- Create: `backend/alembic/versions/0086_v2_worker_pool_heartbeats.py`
- Create: `backend/alembic/versions/0087_v2_first_chat_activation.py`
- Create: `backend/alembic/versions/0088_merge_pre_test_heads.py`
- Create: `backend/alembic_tee/versions/0018_v2_wake_shadow_decisions.py`
- Create: `backend/alembic_tee/versions/0019_v2_worker_pool_heartbeats.py`
- Create: `backend/alembic_tee/versions/0020_v2_first_chat_activation.py`
- Modify: `tests/test_v2_jobs_migration.py`
- Modify: `tests/test_pre_runtime_preflight.py`
- Create: `tests/test_pre_test_migration_convergence.py`

**Interfaces:**
- Consumes: pre RDS heads `0086_merge_voice_wake` and test chain head `0087_v2_first_chat_activation`; pre TEE head `0017_voice_primary_alignment`.
- Produces: RDS head `0088_merge_pre_test_heads` and TEE head `0020_v2_first_chat_activation`, with equivalent table, columns, index, and bounded activation backfill.

- [ ] **Step 1: Add failing topology and parity tests**

Update `tests/test_v2_jobs_migration.py` so the installed RDS head assertion is `0088_merge_pre_test_heads`, then add the following topology checks to `tests/test_pre_test_migration_convergence.py`:

```python
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).parent.parent


def _scripts(tree: str) -> ScriptDirectory:
    ini = "alembic.ini" if tree == "alembic" else "alembic_tee/alembic.ini"
    cfg = Config(str(ROOT / "backend" / ini))
    cfg.set_main_option("script_location", str(ROOT / "backend" / tree))
    return ScriptDirectory.from_config(cfg)


def test_rds_pre_and_test_heads_converge():
    script = _scripts("alembic")
    assert script.get_heads() == ["0088_merge_pre_test_heads"]
    assert set(script.get_revision("0088_merge_pre_test_heads").down_revision) == {
        "0086_merge_voice_wake",
        "0087_v2_first_chat_activation",
    }


def test_tee_chain_carries_test_runtime_schema():
    script = _scripts("alembic_tee")
    assert script.get_heads() == ["0020_v2_first_chat_activation"]
    assert script.get_revision("0020_v2_first_chat_activation").down_revision == "0019_v2_worker_pool_heartbeats"
    assert script.get_revision("0019_v2_worker_pool_heartbeats").down_revision == "0018_v2_wake_shadow_decisions"
    assert script.get_revision("0018_v2_wake_shadow_decisions").down_revision == "0017_voice_primary_alignment"


def test_tee_migrations_reuse_the_rds_contract_sql():
    rds = _scripts("alembic")
    tee = _scripts("alembic_tee")
    assert tee.get_revision("0018_v2_wake_shadow_decisions").module._SCHEMA_UP == rds.get_revision("0085_v2_wake_shadow_decisions").module._SCHEMA_UP
    assert tee.get_revision("0019_v2_worker_pool_heartbeats").module._UP == rds.get_revision("0086_v2_worker_pool_heartbeats").module._UP
    assert tee.get_revision("0020_v2_first_chat_activation").module._BACKFILL_SQL == rds.get_revision("0087_v2_first_chat_activation").module._BACKFILL_SQL
```

Update `tests/test_pre_runtime_preflight.py::test_tee_migrate_has_one_head_after_voice_primary_alignment` to assert the new `0020` head, its `0019 → 0018 → 0017` ancestry, and the literal marker `\'["0020_v2_first_chat_activation"]\'::jsonb` in `_UPDATE_PREPARED_HEAD`.

- [ ] **Step 2: Run tests and verify the missing revisions fail**

Run:

```bash
PYTHONPATH=backend .venv-test/bin/python -m pytest \
  tests/test_pre_test_migration_convergence.py \
  tests/test_pre_runtime_preflight.py::test_tee_migrate_has_one_head_after_voice_primary_alignment \
  -q
```

Expected: FAIL because `0088_merge_pre_test_heads` and TEE revisions `0018`–`0020` do not exist.

- [ ] **Step 3: Add the three test-side RDS revisions and merge head**

Use `apply_patch` to create `0085`, `0086`, and `0087` with the exact revision metadata and SQL from `origin/test`. Create the no-op merge revision:

```python
"""Merge the PRE voice/wake chain with the Runtime V2 activation chain."""

revision = "0088_merge_pre_test_heads"
down_revision = ("0086_merge_voice_wake", "0087_v2_first_chat_activation")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
```

- [ ] **Step 4: Add equivalent sequential TEE migrations**

Create `0018` with the byte-identical `_SCHEMA_UP` from RDS `0085` and this metadata:

```python
revision = "0018_v2_wake_shadow_decisions"
down_revision = "0017_voice_primary_alignment"
```

Create `0019` with the byte-identical `_UP` from RDS `0086` and this metadata:

```python
revision = "0019_v2_worker_pool_heartbeats"
down_revision = "0018_v2_wake_shadow_decisions"
```

Create `0020` with the byte-identical `_BACKFILL_SQL` from RDS `0087`, run it before updating the prepared marker, and do not erase activation data during downgrade:

```python
revision = "0020_v2_first_chat_activation"
down_revision = "0019_v2_worker_pool_heartbeats"

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0020_v2_first_chat_activation"]'::jsonb)::text,
  'UTF8'
)
WHERE key = 'phase4_primary_prepared'
  AND COALESCE(convert_from(value, 'UTF8')::jsonb->>'prepared', 'false') = 'true';
"""


def upgrade() -> None:
    op.execute(_BACKFILL_SQL)
    op.execute(_UPDATE_PREPARED_HEAD)


def downgrade() -> None:
    raise NotImplementedError(
        "alembic_tee downgrade is not supported; restore from backup"
    )
```

- [ ] **Step 5: Run migration contract tests**

Run:

```bash
PYTHONPATH=backend .venv-test/bin/python -m pytest \
  tests/test_pre_test_migration_convergence.py \
  tests/test_pre_runtime_preflight.py \
  -q
```

Expected: topology and static workflow tests PASS. The DB-backed activation test arrives with `origin/test` in Task 2 and runs in Task 3.

- [ ] **Step 6: Commit the migration convergence layer**

```bash
git add backend/alembic/versions backend/alembic_tee/versions \
  tests/test_v2_jobs_migration.py tests/test_pre_runtime_preflight.py \
  tests/test_pre_test_migration_convergence.py
git commit -m "fix(db): converge test and pre migration heads"
```

---

### Task 2: Merge `origin/test` and resolve pre-specific conflicts

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `backend/tee_shadow/table_registry.py`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: `docs/CHANGELOG.md`
- Modify: `tests/test_v2_jobs_migration.py`
- Modify: `tests/test_v2_profile.py`
- Modify: `tests/test_v2_profile_cards.py`
- Modify: `tools/e2e/client.py`
- Merge: all non-conflicting files changed by `origin/test`

**Interfaces:**
- Consumes: Task 1’s unique RDS and TEE heads.
- Produces: one merge commit containing Runtime V2 changes without regressing pre plaintext or TEE-primary behavior.

- [ ] **Step 1: Merge without committing and confirm the known conflict set**

Run:

```bash
git merge --no-commit --no-ff origin/test
git diff --name-only --diff-filter=U
```

Expected: conflicts are limited to the eight files listed in this task. Stop and inspect before proceeding if any additional file conflicts.

- [ ] **Step 2: Resolve CI, registry, profile, and E2E behavior**

Apply these exact union rules:

- `.github/workflows/ci.yml`: keep every pre plaintext boundary test and also include `tests/test_data_track_debug.py` in the explicit list.
- `backend/tee_shadow/table_registry.py`: retain pre’s `SNAPSHOT` entries for all three `v2_usage_*` tables; add `v2_wake_shadow_decisions` as `SKIP` because it is content-free RDS reporting data; do not create a duplicate `v2_usage_rollup_watermarks` key.
- `tests/test_v2_profile.py`: expect events in runtime order: `provider_request`, `profile_provider_response_observed`, then `profile_overlap_observed`.
- `tests/test_v2_profile_cards.py`: keep the test-side profile-card truncation trace coverage.
- `tools/e2e/client.py`: keep the pre conditional plaintext/sealed envelope and call `self.record_failure_locator("trace_id", envelope.get("id"))` immediately after constructing either envelope.

- [ ] **Step 3: Resolve migrations and changelogs**

Keep Task 1’s `0088` single-head assertion in `tests/test_v2_jobs_migration.py`, while retaining all test-side checks for `0085`–`0087`. In both changelogs, keep the pre plaintext boundary entries and the test Runtime V2 entries in reverse chronological order; remove conflict markers without rewriting unrelated history.

- [ ] **Step 4: Verify conflict resolution before committing**

Run:

```bash
git diff --name-only --diff-filter=U
git diff --check
PYTHONPATH=backend .venv-test/bin/python -m pytest \
  tests/test_pre_test_migration_convergence.py \
  tests/test_pre_runtime_preflight.py \
  tests/test_v2_profile.py \
  tests/test_v2_profile_cards.py \
  tests/test_e2e_tools.py \
  tests/test_tee_table_registry.py \
  -q
```

Expected: no unresolved paths, no newly introduced whitespace errors, and all selected tests PASS with DB-backed cases using the local PostgreSQL fixture.

- [ ] **Step 5: Commit the merge**

```bash
git add -A
git commit -m "merge(test): sync Runtime V2 changes into pre"
```

---

### Task 3: Run release-grade code and documentation verification

**Files:**
- Verify: `backend/**`, `tests/**`, `.github/workflows/**`, `deploy/**`
- Verify: `docs-site/content/docs/**`, `docs-site/openapi/public.json`

**Interfaces:**
- Consumes: resolved merge commit from Task 2.
- Produces: recorded local evidence that the commit is eligible to advance to `pre`.

- [ ] **Step 1: Confirm Git and Alembic state**

```bash
git status --short --branch
PYTHONPATH=backend .venv-test/bin/python - <<'PY'
from alembic.config import Config
from alembic.script import ScriptDirectory
for tree in ("alembic", "alembic_tee"):
    cfg = Config(f"backend/{tree}/alembic.ini")
    cfg.set_main_option("script_location", f"backend/{tree}")
    print(tree, ScriptDirectory.from_config(cfg).get_heads())
PY
```

Expected: clean worktree; heads are `0088_merge_pre_test_heads` and `0020_v2_first_chat_activation`.

- [ ] **Step 2: Run the focused release suite with local PostgreSQL**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_v2_jobs_migration.py \
  tests/test_v2_first_chat_activation_backfill.py \
  tests/test_pre_test_migration_convergence.py \
  tests/test_pre_runtime_preflight.py \
  tests/test_deploy_yaml_strict.py \
  tests/test_tee_schema.py \
  tests/test_tee_table_registry.py \
  tests/test_chat_core_file_ingest.py \
  tests/test_chat_resident_consumer_file.py \
  tests/test_enclave_visual_plaintext.py \
  tests/test_asgi_screen.py \
  tests/test_v2_pool_config.py \
  tests/test_v2_pool_supervisor.py \
  tests/test_v2_worker_files.py \
  tests/test_v2_worker_heartbeat.py \
  -q
```

Expected: PASS. These paths exist across the two input branches and cover plaintext media, screen ingestion, Runtime V2 pools, files, and heartbeats.

- [ ] **Step 3: Run the repository test suite**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests -q
```

Expected: PASS. Investigate every failure; do not classify missing database setup as a pass.

- [ ] **Step 4: Validate public API and docs**

```bash
PYTHONPATH=backend .venv-test/bin/python -m pytest tests/openapi/test_public_openapi.py -q
cd docs-site
npm run openapi:generate
npm run types:check
npm run lint
npm run build
```

Expected: all commands PASS. Review `git diff -- docs-site/openapi/public.json`; commit only a deterministic contract change caused by the merge.

- [ ] **Step 5: Commit deterministic generated-doc changes if present**

```bash
git add docs-site/openapi/public.json
git commit -m "docs(api): refresh merged pre contract"
```

Skip this commit when generation leaves the worktree unchanged.

---

### Task 4: Advance `pre`, migrate TEE, and deploy through GitHub Actions

**Files:**
- Remote branch: `pre`
- Workflow: `.github/workflows/tee-migrate.yml`
- Workflow: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: verified integration tip from Task 3.
- Produces: remote `pre` at that tip, pre TEE DB at `0020_v2_first_chat_activation`, and healthy main/runner CVMs on the same release SHA.

- [ ] **Step 1: Re-fetch and prove `pre` has not moved unexpectedly**

```bash
git fetch origin pre test
git rev-parse origin/pre
git merge-base --is-ancestor origin/test HEAD
git log --oneline --left-right origin/pre...HEAD
```

Expected: `origin/test` is an ancestor of `HEAD`; any new `origin/pre` commit must be merged and reverified before push.

- [ ] **Step 2: Push the verified integration tip to `pre`**

```bash
git push origin HEAD:pre
```

Expected: fast-forward push succeeds and starts CI. The pre TEE schema gate fails before `phala deploy` because the DB is still at `0017`; existing CVMs remain unchanged.

- [ ] **Step 3: Verify the expected schema-gate stop**

```bash
gh run list --branch pre --limit 10
CI_RUN_ID="$(gh run list --workflow ci.yml --branch pre --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run view "$CI_RUN_ID" --log-failed
```

Expected log evidence: `PRE TEE schema migration required`, expected head `0020_v2_first_chat_activation`, actual head `0017_voice_primary_alignment`, and `No PRE CVM was changed`.

- [ ] **Step 4: Run the owner migration workflow on pre**

```bash
gh workflow run tee-migrate.yml --ref pre \
  -f environment=pre \
  -f confirm=MIGRATE-TEE
gh run list --workflow tee-migrate.yml --branch pre --limit 5
TEE_RUN_ID="$(gh run list --workflow tee-migrate.yml --branch pre --limit 1 --json databaseId --jq '.[0].databaseId')"
gh run watch "$TEE_RUN_ID" --exit-status
```

Expected: workflow PASS and final log reports `code head=0020_v2_first_chat_activation db=0020_v2_first_chat_activation`.

- [ ] **Step 5: Rerun the blocked deployment jobs**

```bash
gh run rerun "$CI_RUN_ID" --failed
gh run watch "$CI_RUN_ID" --exit-status
```

Expected: CI and both pre deployment jobs PASS without bypassing preflight.

---

### Task 5: Verify the live pre release

**Files:**
- Inspect: `deploy/pre-cvm-id.txt`
- Inspect: `deploy/pre-runner-cvm-id.txt`
- Inspect: live pre containers and health endpoints

**Interfaces:**
- Consumes: successful GitHub Actions release from Task 4.
- Produces: deployment evidence for backend, enclave, serve-worker, runner, schema, and Runtime V2 health.

- [ ] **Step 1: Verify public health and deployed release identifiers**

```bash
curl -sS -i https://pre-api.feedling.app/healthz
gh run view "$CI_RUN_ID"
```

Expected: HTTP 200 and the deployment jobs reference the pushed seven-character commit tag.

- [ ] **Step 2: Inspect both pre CVMs**

Use the configured Phala profile and the exact IDs from the two inventory files:

```bash
phala cvms get "$(tr -d '[:space:]' < deploy/pre-cvm-id.txt)" --json
phala cvms get "$(tr -d '[:space:]' < deploy/pre-runner-cvm-id.txt)" --json
```

Then use `phala ssh` to inspect `docker ps`, backend/enclave/serve-worker logs, runner logs, and container image tags. Expected: all required containers healthy and image tags aligned to the deployed release.

- [ ] **Step 3: Verify schema and content-free Runtime V2 signals**

Confirm the TEE workflow’s head assertion, then inspect only aggregate/content-free signals: wake bus readiness, three worker-pool heartbeats, and a Runtime V2 health probe. Do not print user messages, perception content, envelopes, keys, or credentials.

- [ ] **Step 4: Record the final state**

Report the integration commit, remote `pre` SHA, CI run, TEE migration run, HTTP health, container/image alignment, and any residual non-blocking warnings. If any required check fails, stop and leave `prod` untouched.
