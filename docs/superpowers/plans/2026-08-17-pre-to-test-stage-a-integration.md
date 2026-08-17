# PRE to TEST Stage A Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge PRE's optional encrypted/plaintext content implementation and TEE-primary capability into `test` while keeping TEST on RDS primary with TEE shadow dual-write and the plaintext-write gate closed.

**Architecture:** Work from an integration branch rooted at the latest `origin/test`, merge `origin/pre`, and preserve TEST's later Runtime V2 safety fixes together with PRE's row-shape routing. Treat code convergence and database promotion as separate releases: this plan ends with a Stage A pull request and RDS-primary TEST evidence; a later plan owns the write freeze and TEE-primary cutover.

**Tech Stack:** Python 3.12, PostgreSQL 16, Alembic, pytest, GitHub Actions, Phala CVMs, Next.js documentation site.

## Global Constraints

- Work only in `.worktrees/pre-to-test-tee-primary-20260817` on `codex/integrate-pre-to-test-tee-primary-20260817`.
- Preserve TEST CVM IDs, domains, buckets, secrets, branch triggers, and image-pin behavior.
- Keep `TEST_DATABASE_URL` as `DATABASE_URL`, keep `TEST_TEE_DATABASE_URL` as the shadow database, and keep `TEST_FEEDLING_TEE_DUAL_WRITE` wired during Stage A.
- Keep `FEEDLING_PLAINTEXT_WRITES_ACCEPTED` closed in the deployed TEST configuration until Stage B explicitly opens it.
- Preserve PRE's optional encryption, mixed-shape reads, plaintext binary media, strict enclave boundary, and TEE schema capability.
- Preserve TEST's Runtime V2 scheduled-delivery retry, watchdog replay fence, and later source-guide changes.
- Maintain exactly one RDS Alembic head and exactly one TEE Alembic head.
- Do not change GitHub Secrets, freeze writes, run Phase 4 apply, switch a live DSN, push `main`, or touch production.
- Update and validate public documentation because the merged code changes public behavior, trust boundaries, and deployment topology.

---

### Task 1: Record the integration baseline and safety assertions

**Files:**
- Modify: `tests/test_pre_runtime_preflight.py`
- Verify: `.github/workflows/ci.yml`
- Verify: `deploy/docker-compose.phala.test.yaml`

**Interfaces:**
- Consumes: TEST deployment jobs `validate-test-runtime-prerequisites`, `deploy-test-cvm`, and `deploy-test-runner-cvm`.
- Produces: static regression checks that reject PRE credentials or a premature TEE-primary switch in Stage A.

- [ ] **Step 1: Record exact input SHAs and divergence**

Run:

```bash
git fetch origin test pre
git rev-parse origin/test origin/pre
git rev-list --left-right --count origin/test...origin/pre
git status --short --branch
```

Expected: the worktree is clean before test edits and remains rooted at the reviewed `origin/test`. If either remote moved, repeat the read-only merge-tree and changed-file review before continuing.

- [ ] **Step 2: Add Stage A deployment topology assertions**

Append this test to `tests/test_pre_runtime_preflight.py` after the existing TEST preflight tests:

```python
def test_test_stage_a_keeps_rds_primary_and_tee_shadow_wiring():
    source = WORKFLOW.read_text()
    main = _job(source, "deploy-test-cvm", "deploy-test-runner-cvm")
    runner = _job(source, "deploy-test-runner-cvm", "deploy-pre-cvm")

    assert "${{ secrets.TEST_DATABASE_URL }}" in main
    assert "${{ secrets.TEST_TEE_DATABASE_URL }}" in main
    assert "${{ secrets.TEST_FEEDLING_TEE_DUAL_WRITE }}" in main
    assert "${{ secrets.TEST_DATABASE_URL }}" in runner
    assert "PRE_DATABASE_URL" not in main
    assert "PRE_DATABASE_URL" not in runner
```

- [ ] **Step 3: Run the topology assertion against the TEST baseline**

Run:

```bash
PYTHONPATH=backend /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_pre_runtime_preflight.py::test_test_stage_a_keeps_rds_primary_and_tee_shadow_wiring \
  -q
```

Expected: PASS. This is a characterization/safety test, not new production behavior; it must remain green across the merge.

- [ ] **Step 4: Commit the safety assertion**

```bash
git add tests/test_pre_runtime_preflight.py
git commit -m "test(test): freeze stage a database topology"
```

---

### Task 2: Merge PRE and resolve Runtime V2 conflict without losing either behavior

**Files:**
- Merge: all non-conflicting paths from `origin/pre`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Verify: `backend/model_api_runtime/v2/worker.py`
- Verify: `tests/test_v2_jobs_store.py`
- Verify: `docs-site/content/docs/changelog.mdx`
- Verify: `docs-site/content/docs/workflows/chat.mdx`

**Interfaces:**
- Consumes: TEST's watchdog replay fence and PRE's encrypted/plaintext trajectory and capture validation.
- Produces: a single merge commit where scheduled turns replay only when pristine and within budget, while trajectories and captures accept only the account-authorized stored shape.

- [ ] **Step 1: Merge PRE without committing**

Run:

```bash
git merge --no-commit --no-ff origin/pre
git diff --name-only --diff-filter=U
```

Expected: `backend/model_api_runtime/v2/jobs_store.py` is the only unresolved path. Stop if the conflict set differs.

- [ ] **Step 2: Resolve the known comment conflict minimally**

In the watchdog SQL comment near `_reclaim_stalled_executions`, retain this exact text and remove all conflict markers:

```python
# Watchdog recovery is an eager form of the lease reaper.
# It must use the same replay-safety contract: only a
# pristine scheduled turn with retry budget remaining may
# run again. Any MCP attempt or durable platform effect
# makes whole-turn replay unsafe, even when the recorded
# MCP outcome is known-success.
```

Do not alter PRE's plaintext additions elsewhere in the file.

- [ ] **Step 3: Verify the merge result has both feature families**

Run:

```bash
rg -n "_TRAJECTORY_PLAINTEXT_REQUIRED|_validate_capture_actions|pristine scheduled turn|durable platform effect" \
  backend/model_api_runtime/v2/jobs_store.py
rg -n "content_encryption_effective|plaintext_v1|body_b64" \
  backend tests/test_content_encryption_preference.py tests/test_plaintext_enclave_boundary.py
git diff --name-only --diff-filter=U
git diff --check
```

Expected: all four Runtime V2 markers and all three content-shape markers are present; no unresolved path or whitespace error remains.

- [ ] **Step 4: Run the conflict-focused tests**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_v2_jobs_store.py \
  tests/test_v2_serve_worker.py \
  tests/test_content_encryption_preference.py \
  tests/test_write_side_format_routing.py \
  tests/test_read_side_shape_routing.py \
  tests/test_plaintext_enclave_boundary.py \
  -q
```

Expected: PASS with PostgreSQL-backed cases collected and executed.

- [ ] **Step 5: Commit the merge**

```bash
git add -A
git commit -m "merge(pre): integrate optional encryption into test"
```

---

### Task 3: Prove migration and deployment convergence

**Files:**
- Verify: `backend/alembic/versions/*.py`
- Verify: `backend/alembic_tee/versions/*.py`
- Verify: `tests/test_pre_test_migration_convergence.py`
- Verify: `tests/test_pre_runtime_preflight.py`
- Verify: `.github/workflows/tee-migrate.yml`
- Verify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: merged RDS head `0090_merge_wake_outcomes` and TEE head `0022_v2_wake_outcomes`.
- Produces: evidence that both migration trees are single-headed and TEST deployment remains RDS-primary during Stage A.

- [ ] **Step 1: Inspect Alembic heads from code**

Run:

```bash
PYTHONPATH=backend /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python - <<'PY'
from alembic.config import Config
from alembic.script import ScriptDirectory

for tree, ini in (("alembic", "backend/alembic.ini"),
                  ("alembic_tee", "backend/alembic_tee/alembic.ini")):
    cfg = Config(ini)
    cfg.set_main_option("script_location", f"backend/{tree}")
    print(tree, ScriptDirectory.from_config(cfg).get_heads())
PY
```

Expected: `alembic ['0090_merge_wake_outcomes']` and `alembic_tee ['0022_v2_wake_outcomes']`.

- [ ] **Step 2: Run migration and workflow contract tests**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_pre_test_migration_convergence.py \
  tests/test_pre_runtime_preflight.py \
  tests/test_v2_jobs_migration.py \
  tests/test_tee_schema.py \
  tests/test_phase4_cutover.py \
  -q
```

Expected: PASS, including the Stage A RDS-primary safety assertion.

- [ ] **Step 3: Verify live TEST schema heads read-only**

Read the RDS DSN from `TEST_DATABASE_URL` and the TEE owner DSN from the local TEST secrets without printing either value. Query only `alembic_version.version_num` and `alembic_tee_version.version_num`.

Expected: record both actual heads. If either differs from the release head, do not deploy; prepare the corresponding migration workflow first.

---

### Task 4: Run release-grade code, media, and documentation verification

**Files:**
- Verify: `backend/**`
- Verify: `tests/**`
- Verify: `docs-site/content/docs/**`
- Verify: `docs-site/openapi/public.json`

**Interfaces:**
- Consumes: the resolved merge and converged migration trees.
- Produces: local evidence that Stage A is eligible for review against `test`.

- [ ] **Step 1: Run the focused encrypted/plaintext surface suite**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_chat_core_file_ingest.py \
  tests/test_chat_file_r2.py \
  tests/test_asgi_perception.py \
  tests/test_perception_ingress_v2.py \
  tests/test_asgi_screen.py \
  tests/test_frame_r2.py \
  tests/test_asgi_memory.py \
  tests/test_memory_readside_core.py \
  tests/test_worldbook_routes.py \
  tests/test_genesis_plaintext_routes.py \
  tests/test_genesis_service.py \
  tests/test_v2_history_readside.py \
  tests/test_v2_jobs_store.py \
  tests/test_v2_serve_worker.py \
  tests/test_encryption_surface_frozen.py \
  tests/test_no_fake_envelope_shapes.py \
  -q
```

Expected: PASS for text, image/PDF attachment, Perception, screen, Memory, World Book, Genesis, Runtime V2, and strict shape checks.

- [ ] **Step 2: Run the full PostgreSQL-backed suite**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests -q --ignore=tests/test_api.py
```

Expected: zero failures. Confirm the collected/pass count is consistent with the merged branch; do not accept a small count caused by skipped DB modules.

- [ ] **Step 3: Validate OpenAPI and public docs**

Run:

```bash
PYTHONPATH=backend /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/openapi/test_public_openapi.py -q
cd docs-site
npm run openapi:generate
npm run types:check
npm run lint
npm run build
```

Expected: all commands exit zero. Review generated `public.json`; commit only deterministic changes caused by the merge.

- [ ] **Step 4: Commit deterministic generated output if needed**

```bash
git add docs-site/openapi/public.json
git commit -m "docs(api): refresh optional encryption contract"
```

Skip this commit when generation leaves the file unchanged.

---

### Task 5: Publish the integration branch and open the TEST pull request

**Files:**
- Remote branch: `codex/integrate-pre-to-test-tee-primary-20260817`
- Pull request target: `test`

**Interfaces:**
- Consumes: clean, fully verified Stage A integration tip.
- Produces: a reviewable PR targeting `test`; it does not itself authorize merge or deployment.

- [ ] **Step 1: Re-fetch and prove the target has not moved**

Run:

```bash
git fetch origin test pre
git log --oneline --left-right origin/test...HEAD
git merge-base --is-ancestor origin/pre HEAD
git status --short --branch
```

Expected: `origin/pre` is an ancestor of `HEAD`; the worktree is clean. If `origin/test` has new commits, merge them and repeat Tasks 2–4 before publishing.

- [ ] **Step 2: Push the integration branch**

```bash
git push -u origin codex/integrate-pre-to-test-tee-primary-20260817
```

- [ ] **Step 3: Open a PR against `test`**

```bash
gh pr create \
  --base test \
  --head codex/integrate-pre-to-test-tee-primary-20260817 \
  --title "merge: bring optional encryption and TEE capability to test" \
  --body-file /tmp/pre-to-test-stage-a-pr.md
```

The PR body must include exact input/output SHAs, conflict resolution, RDS and TEE heads, focused/full/docs results, and the explicit statement: `Stage A keeps TEST on RDS primary; this PR does not authorize TEE-primary cutover.`

- [ ] **Step 4: Review CI and request the merge gate**

Run:

```bash
gh pr checks --watch
gh pr view --json url,headRefOid,mergeStateStatus,statusCheckRollup
```

Expected: required checks pass. Present the evidence and ask for explicit approval before merging the PR into `test`.

---

### Task 6: Verify Stage A after the approved TEST merge

**Files:**
- Inspect: `.github/workflows/ci.yml`
- Inspect: `deploy/test-cvm-id.txt`
- Inspect: `deploy/test-runner-cvm-id.txt`
- Inspect: live TEST main and runner CVMs

**Interfaces:**
- Consumes: an explicitly approved PR merge to `test` and the resulting GitHub Actions release.
- Produces: live RDS-primary compatibility evidence and the input report for the separate Stage B cutover plan.

- [ ] **Step 1: Watch the TEST deployment workflow**

Run `gh run list --branch test`, identify the merge SHA's CI run, then use `gh run watch <run-id> --exit-status` and `gh run view <run-id>`.

Expected: validation, image build, main CVM deployment, attestation/canary, and runner deployment pass.

- [ ] **Step 2: Verify TEST endpoints and release identity**

Check `https://test-api.feedling.app/healthz`, the TEST custom enclave endpoint, and the direct attested `-5003s` endpoint. Expected: HTTP 200 and the deployed release identity matches the TEST merge release.

- [ ] **Step 3: Verify the Stage A database topology in both CVMs**

Use `phala ssh` to print only URL scheme/hostname/port/database path and boolean/schema flags; never print passwords or full DSNs.

Expected: main and runner use TEST RDS as `DATABASE_URL`; main retains TEST TEE shadow DSN and dual-write; schema mode is not TEE primary; plaintext writes remain closed.

- [ ] **Step 4: Run encrypted and plaintext compatibility canaries**

With the plaintext write gate still closed, verify existing encrypted accounts and mixed historical reads. Do not assert new plaintext writes yet; opening that gate belongs to Stage B.

- [ ] **Step 5: Produce the Stage B readiness report**

Record live release SHA, workflow run IDs, database heads, topology, endpoint health, container restart counts, queue health, and any blockers. Use that report to write the separate TEST TEE-primary cutover plan and request the write-freeze/secret-change approval.
