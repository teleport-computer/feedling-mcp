# Test → Pre Runtime V2 Second Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge frozen `origin/test@79c130b6` into `origin/pre@19ef79ee`, converge the new RDS migration with pre, carry the same schema change into TEE-primary, and deploy the verified result to pre.

**Architecture:** Keep test's `0088_agent_jobs_available_at` migration byte-for-byte, add RDS `0089_merge_pre_test_agent_jobs` to join it with pre's existing `0088_merge_pre_test_heads`, and add a linear TEE `0021_agent_jobs_available_at` migration with identical schema SQL. Resolve profile conflicts by accepting test's full-card and durable-retry behavior while retaining pre's plaintext shape coverage and deployment trust boundaries.

**Tech Stack:** Git worktrees, Python, pytest, PostgreSQL 16, Alembic, GitHub Actions, Phala Cloud, Next.js/Fumadocs.

## Global Constraints

- Merge exactly `79c130b6163f42bfece0243dda2b91eccd276be8`; later test commits belong to another round.
- Preserve pre TEE-primary startup checks, phase-4 marker validation, plaintext/encrypted routing, both enclave entrypoints, and separate main/runner CVMs.
- Do not modify or push `main`, `prod`, `test`, or iOS.
- RDS and TEE must each expose exactly one code head before any remote push.
- Use local PostgreSQL `postgresql://postgres:test@127.0.0.1:55432/postgres` for DB-backed pytest.
- Keep all public docs and changelog updates associated with accepted test behavior.

---

### Task 1: Merge the frozen test head and resolve conflicts

**Files:**
- Modify: `tests/test_v2_jobs_migration.py`
- Modify: `tests/test_v2_profile_cards.py`
- Modify: `tests/test_v2_profile_storage.py`
- Review: `.github/workflows/ci.yml`
- Review: `backend/model_api_runtime/v2/serve_worker.py`
- Review: `backend/model_api_runtime/v2/worker.py`
- Review: `backend/model_api_runtime/v2/jobs_store.py`
- Review: `docs-site/content/docs/changelog.mdx`
- Review: `docs/CHANGELOG.md`

**Interfaces:**
- Consumes: frozen pre/test commits from the design.
- Produces: a conflict-free merge worktree ready for migration convergence.

- [ ] **Step 1: Reconfirm the frozen refs**

Run:

```bash
git rev-parse HEAD origin/pre origin/test
git status --short
```

Expected: clean worktree; `origin/test` equals the frozen full SHA.

- [ ] **Step 2: Start the merge without committing**

Run:

```bash
git merge --no-ff --no-commit 79c130b6163f42bfece0243dda2b91eccd276be8
git diff --name-only --diff-filter=U
```

Expected conflicts are exactly the three test files listed above. If another file conflicts, stop and add its resolution contract to this plan before editing.

- [ ] **Step 3: Resolve `test_v2_profile_cards.py` for full-card behavior**

Keep test's case beginning with:

```python
def test_profile_card_full_content_reaches_provider_request_trace(monkeypatch):
    tail_sentinel = "T062_PROFILE_CARD_TAIL_REACHES_PROVIDER"
    full_body = (
        "Q" * (profile.PROFILE_MEMORY_MAX_CHARS + profile.PROFILE_USER_MAX_CHARS)
        + tail_sentinel
    )
```

Keep assertions that the provider trace contains the tail sentinel and reports `"truncated": False`. Remove the old pre-only fixed truncation assertion because it contradicts the accepted test behavior.

- [ ] **Step 4: Resolve `test_v2_profile_storage.py` as a union**

Retain both pre's plaintext profile document build/validate tests and test's durable retry fields:

```python
assert normalized["last_attempt"]["retry_disposition"] == ""
assert normalized["last_attempt"]["retry_family"] == ""
assert normalized["last_attempt"]["retry_attempts"] == 0
assert normalized["last_attempt"]["retry_not_before"] == 0.0
```

Keep `test_production_builder_locally_seals_each_field_before_jsonb` after both groups.

- [ ] **Step 5: Resolve `test_v2_jobs_migration.py` for the final topology**

Use:

```python
assert script.get_heads() == ["0089_merge_pre_test_agent_jobs"]
merge = script.get_revision("0089_merge_pre_test_agent_jobs")
assert set(merge.down_revision) == {
    "0088_merge_pre_test_heads",
    "0088_agent_jobs_available_at",
}
agent_jobs = script.get_revision("0088_agent_jobs_available_at")
assert agent_jobs.down_revision == "0087_v2_first_chat_activation"
```

Keep the real-PostgreSQL assertions for `available_at`, its `now()` default, and `ix_agent_jobs_pending_available_at`; update installed-head assertions to `0089_merge_pre_test_agent_jobs`.

- [ ] **Step 6: Review auto-merged high-risk files**

Run `git diff --cached --` for every Review file. Verify CI retains pre startup/TEE/plaintext gates plus test security/profile suites; Runtime V2 retains plaintext routing plus durable retry; changelogs retain both sides.

- [ ] **Step 7: Confirm conflict markers are gone**

Run:

```bash
git diff --name-only --diff-filter=U
rg -n '^(<<<<<<<|=======|>>>>>>>)' tests/test_v2_jobs_migration.py tests/test_v2_profile_cards.py tests/test_v2_profile_storage.py
```

Expected: no output.

---

### Task 2: Converge RDS and TEE migration heads

**Files:**
- Create: `backend/alembic/versions/0089_merge_pre_test_agent_jobs.py`
- Create: `backend/alembic_tee/versions/0021_agent_jobs_available_at.py`
- Modify: `tests/test_pre_test_migration_convergence.py`
- Modify: `tests/test_v2_jobs_migration.py`

**Interfaces:**
- Consumes: `_UP` from RDS `0088_agent_jobs_available_at`.
- Produces: RDS head `0089_merge_pre_test_agent_jobs`, TEE head `0021_agent_jobs_available_at`, exact schema-SQL parity.

- [ ] **Step 1: Add failing convergence assertions**

Add:

```python
assert _scripts("alembic").get_heads() == ["0089_merge_pre_test_agent_jobs"]
assert set(
    _scripts("alembic").get_revision(
        "0089_merge_pre_test_agent_jobs"
    ).down_revision
) == {"0088_merge_pre_test_heads", "0088_agent_jobs_available_at"}
assert _scripts("alembic_tee").get_heads() == ["0021_agent_jobs_available_at"]
assert (
    _scripts("alembic_tee").get_revision(
        "0021_agent_jobs_available_at"
    ).down_revision
    == "0020_v2_first_chat_activation"
)
```

Also assert `0021.module._UP == 0088_agent_jobs.module._UP`.

- [ ] **Step 2: Verify RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_pre_test_migration_convergence.py tests/test_v2_jobs_migration.py -q
```

Expected: FAIL because the RDS `0089` and TEE `0021` revisions do not exist.

- [ ] **Step 3: Create the RDS merge revision**

```python
revision = "0089_merge_pre_test_agent_jobs"
down_revision = (
    "0088_merge_pre_test_heads",
    "0088_agent_jobs_available_at",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
```

- [ ] **Step 4: Create the TEE-primary migration**

Copy `_UP` exactly from the RDS 0088 migration. Add:

```python
revision = "0021_agent_jobs_available_at"
down_revision = "0020_v2_first_chat_activation"

_UPDATE_PREPARED_HEAD = """
UPDATE server_config
SET value = convert_to(
  jsonb_set(convert_from(value, 'UTF8')::jsonb, '{tee_heads}',
            '["0021_agent_jobs_available_at"]'::jsonb)::text,
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

- [ ] **Step 5: Verify GREEN and commit the merge**

Rerun Step 2; require migration topology, real PostgreSQL schema/default/index, and parity assertions to pass. Then run:

```bash
git add .
git diff --cached --check
git commit -m "merge(test): sync durable Runtime V2 changes into pre"
```

---

### Task 3: Verify Runtime V2 and pre trust boundaries

**Files:**
- Verify: `backend/model_api_runtime/v2/`
- Verify: `backend/hosted/`
- Verify: `.github/workflows/ci.yml`
- Verify: `deploy/docker-compose.phala.pre.yaml`
- Verify: `deploy/docker-compose.phala.pre.runner.yaml`

**Interfaces:**
- Consumes: merged Runtime V2 and pre manifests.
- Produces: focused evidence covering durable retry, plaintext routing, TEE startup, and enclave topology.

- [ ] **Step 1: Run profile/durable-retry tests**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_profile_retry.py tests/test_v2_profile_refresh.py tests/test_v2_profile_lane.py tests/test_v2_profile_storage.py tests/test_v2_profile_cards.py tests/test_v2_jobs_store.py tests/test_v2_watchdog.py tests/test_model_api_profiles_routes.py tests/test_model_api_route_activation_unit.py -q
```

Expected: PASS with no unexpected skips.

- [ ] **Step 2: Run pre TEE/deployment tests**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_pre_test_migration_convergence.py tests/test_pre_runtime_preflight.py tests/test_phase4_cutover.py tests/test_tee_schema.py tests/test_deploy_yaml_strict.py tests/test_verify_enclave_domain.py -q
```

Expected: RDS head `0089`, TEE head `0021`, and both enclave paths represented.

- [ ] **Step 3: Run plaintext/encrypted boundary tests**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_content_encryption_preference.py tests/test_encryption_surface_frozen.py tests/test_plaintext_memory_quality.py tests/test_read_side_shape_routing.py tests/test_write_side_format_routing.py tests/test_uploaded_envelope_gate.py tests/test_v2_multimodal_e2e.py tests/test_v2_profile_storage.py -q
```

- [ ] **Step 4: Run CI discovery/security contracts**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_pytest_coverage_ratchet.py tests/test_admin_tee_replication.py tests/test_capabilities_tool_schema.py -q
```

Then reproduce the workflow's `Guard top-level pytest discovery coverage` shell block verbatim and require empty `feedling-new-uncovered-tests.txt` and `feedling-now-covered-tests.txt`.

- [ ] **Step 5: Correct only evidence-backed defects**

For any failure, add or preserve the failing assertion first, verify RED, implement the minimal fix, verify GREEN, and commit with a scoped `fix(pre): ...` subject.

---

### Task 4: Run full and documentation verification

**Files:**
- Verify: complete integration branch and generated public docs.

**Interfaces:**
- Consumes: focused-green integration.
- Produces: exact-head evidence suitable for remote pre promotion.

- [ ] **Step 1: Run the full PostgreSQL-backed suite**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests -q --ignore=tests/test_api.py
```

Require zero failures and explain any skip/xfail delta from the 9703-pass baseline.

- [ ] **Step 2: Run OpenAPI and docs verification**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/openapi/test_public_openapi.py -q
```

Then from `docs-site` run:

```bash
npm run openapi:generate
npm run types:check
npm run lint
npm run build
```

Require exit 0 and review generated diffs.

- [ ] **Step 3: Request and receive code review**

Use `superpowers:requesting-code-review`, then verify every finding with `superpowers:receiving-code-review` before changing code. Review migration topology/marker, conflict semantics, pre trust boundaries, and deployment order.

- [ ] **Step 4: Verify final tree**

```bash
git status --short
git diff --check
git merge-base --is-ancestor 19ef79ee HEAD
git merge-base --is-ancestor 79c130b6 HEAD
```

Expected: clean tree and both frozen inputs are ancestors.

---

### Task 5: Promote, migrate, deploy, and verify pre

**Files:**
- Remote: `pre`
- Workflow: `.github/workflows/tee-migrate.yml`
- Workflow: `.github/workflows/ci.yml`
- IDs: `deploy/pre-cvm-id.txt`, `deploy/pre-runner-cvm-id.txt`

**Interfaces:**
- Consumes: reviewed integration head; live pre TEE DB at `0020`.
- Produces: remote pre, TEE DB, both CVMs, and public endpoints aligned to one release SHA.

- [ ] **Step 1: Push pre by fast-forward only**

```bash
git fetch origin pre
git merge-base --is-ancestor origin/pre HEAD
git push origin HEAD:pre
```

Stop on ancestry failure; never force-push.

- [ ] **Step 2: Confirm schema gate prevents CVM mutation**

Monitor CI/image workflows. The first preflight may reject live DB `0020` versus code `0021`; require both Phala deploy steps to remain untouched.

- [ ] **Step 3: Run the pre TEE migration**

Dispatch `TEE migrate` for `pre`. Require:

```text
code head=0021_agent_jobs_available_at db=0021_agent_jobs_available_at
PRE application startup contract: ok
```

- [ ] **Step 4: Redeploy the same release head**

Rerun gated CI jobs. Require tests, preflight, main-CVM deploy, canary, runner deploy, and compose-hash publication to succeed.

- [ ] **Step 5: Verify public and CVM state**

Require API and both enclave health endpoints to report the final commit; custom enclave mode `attested_ingress`; direct enclave mode `direct_tls`. Use read-only Phala checks to require final image tags, running containers, and restart count `0` for backend, both enclave containers, serve-worker, and agent-runner.

- [ ] **Step 6: Record evidence and preserve the worktree**

Report SHA, workflow URLs, DB heads, health releases, transport modes, and container states. Keep the worktree until the user selects cleanup through `superpowers:finishing-a-development-branch`.
