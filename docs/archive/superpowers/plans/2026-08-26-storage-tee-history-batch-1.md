---
document_lifecycle: historical
canonical_owner: docs/superpowers/plans/2026-08-24-repository-cleanup.zh-CN.md
historical_reason: implemented
---
# Storage/TEE History Batch 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Remove the explicitly retired Phase 2–3 RDS-to-TEE shadow implementation checklist from the default agent search surface while preserving the current TEE-primary topology, migration compatibility paths, implementation evidence, and trust-boundary obligations.

**Architecture:** Treat `docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md`, `deploy/DEPLOYMENTS.md`, current workflow/compose wiring, runtime code, and focused tests as current truth. Move only the implementation plan already marked `RETIRED / DO NOT DEPLOY` into `docs/archive/superpowers/plans/`, point its lifecycle metadata at the current migration runbook, and record the evidence/rationale transfer in one storage/TEE audit page.

**Tech Stack:** Markdown lifecycle metadata, Git rename history, deterministic lifecycle inventory, pytest.

**Spec:** `docs/superpowers/plans/2026-08-24-repository-cleanup.zh-CN.md`

## Global Constraints

- Target branch is `test`; this batch starts from `origin/test` commit `830fd32c`.
- Do not change runtime behavior, RDS or TEE schema/Alembic history, compose, workflows, public API, `backend/`, `deploy/`, `.github/`, or `docs-site/`.
- Do not archive `docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md` in this batch. It is only partially retired and needs a separate rationale-transfer review.
- Do not classify the continued presence of `backend/tee_shadow/`, `backend/tee_replicator/`, `backend/admin/tee_sync_scheduler.py`, or `backend/alembic_tee/` as dead code. They retain migration, compatibility, verification, and schema-history obligations.
- Preserve the current direction distinction: the retired plan describes RDS primary to TEE shadow; TEE-primary plaintext shadow is a separate TEE primary to plaintext projection topology.
- The archived plan must use `document_lifecycle: historical`, `historical_reason: superseded`, and both `canonical_owner` and `superseded_by` must resolve to the current migration runbook.
- Regenerate `docs/repository-cleanup/document-lifecycle-inventory.md` only through `python3 tools/check_document_lifecycle.py --all --report` and prove byte-for-byte determinism.

---

### Task 1: Archive the retired Phase 2–3 shadow implementation plan

**Files:**
- Move: `docs/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md` → `docs/archive/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md`
- Create: `docs/repository-cleanup/storage-tee-history.md`
- Modify: `docs/repository-cleanup/README.md`
- Verify: `docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md`

**Interfaces:**
- Consumes: the current migration runbook, test TEE-primary release record, workflow/compose topology gates, retained shadow/replicator code, and focused TEE tests.
- Produces: one archived historical record and a current audit page that tells agents which statements remain compatibility obligations and which topology is retired.

- [ ] **Step 1: Prove reference ownership before the move**

Run exact full-path and basename searches excluding the generated lifecycle inventory. Expected: no production code, deployment config, workflow, or current runbook consumes the plan as executable instructions. The retained 2026-07-04 design may mention the basename solely to mark the plan retired.

```bash
rg -n -F 'docs/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md' . \
  --glob '!docs/repository-cleanup/document-lifecycle-inventory.md'
rg -n -F '2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md' . \
  --glob '!docs/repository-cleanup/document-lifecycle-inventory.md'
```

- [ ] **Step 2: Move and classify the plan**

Use `git mv`, then prepend exactly:

```yaml
---
document_lifecycle: historical
canonical_owner: docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md
historical_reason: superseded
superseded_by: docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md
---
```

Keep the existing `RETIRED / DO NOT DEPLOY` warning immediately below the title.

- [ ] **Step 3: Create the storage/TEE audit page and add its index entry**

Create a current/self audit page and link it from `docs/repository-cleanup/README.md`. Record:

- original document, lifecycle/current owner, inbound references, archive path, and rationale transfer;
- commit `08afdcc0` as the original Phase 2–3 implementation delivery and `8f3c4603` as the explicit retirement marker;
- test TEE-primary promotion commit `82c4c019` and the current `deploy/DEPLOYMENTS.md` statement that `TEST_DATABASE_URL` is the TEE app-role DSN, `FEEDLING_DATABASE_SCHEMA=tee`, and the old dual-write secret is absent;
- current owners: `docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md`, workflow/compose topology checks, `backend/db.py`, `backend/tee_shadow/`, `backend/tee_replicator/`, `backend/admin/tee_sync_scheduler.py`, and `backend/alembic_tee/`;
- compatibility obligations: fail-open legacy mirror only under its explicit gate; no synchronous decrypt on hot paths; owner/app credential separation; encrypted-account and mixed-row support; Alembic history retention; plaintext shadow as a separate reverse-direction projection;
- an explicit statement that archiving the plan does not authorize deleting runtime, migration, recovery, verification, deployment, or trust-boundary code.

- [ ] **Step 4: Validate Task 1**

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_document_lifecycle.py tests/test_current_state_docs.py \
  tests/test_repository_inventory.py -q
git diff --check
```

Expected: lifecycle validation succeeds, 45 focused documentation tests pass, and diff check is empty.

- [ ] **Step 5: Review checkpoint**

Review the diff for factual accuracy and scope containment before generating the inventory. In particular, confirm the retained partially retired design remains in place and no protected runtime/deployment surface changed.

### Task 2: Regenerate lifecycle inventory and run storage/TEE regression

**Files:**
- Modify: `docs/repository-cleanup/document-lifecycle-inventory.md`
- Verify: all files changed by Task 1 plus this plan.

**Interfaces:**
- Consumes: the archive classification and audit entry from Task 1.
- Produces: deterministic lifecycle inventory plus evidence that the documentation-only move did not change TEE topology or behavior.

- [ ] **Step 1: Stage the plan, regenerate through temporary files, and prove deterministic output**

```bash
git add \
  docs/archive/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md \
  docs/repository-cleanup/README.md \
  docs/repository-cleanup/storage-tee-history.md \
  docs/superpowers/plans/2026-08-26-storage-tee-history-batch-1.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/storage-tee-lifecycle-inventory-a.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/storage-tee-lifecycle-inventory-b.md
cmp /tmp/storage-tee-lifecycle-inventory-a.md /tmp/storage-tee-lifecycle-inventory-b.md
cp /tmp/storage-tee-lifecycle-inventory-a.md docs/repository-cleanup/document-lifecycle-inventory.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/storage-tee-lifecycle-inventory-after-write.md
cmp docs/repository-cleanup/document-lifecycle-inventory.md /tmp/storage-tee-lifecycle-inventory-after-write.md
```

Expected: both `cmp` commands exit 0. Generate through temporary files because redirecting directly to the tracked inventory truncates the checker input before it scans the corpus.

- [ ] **Step 2: Run lifecycle and storage/TEE regressions**

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
NO_PROXY='*' no_proxy='*' \
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_document_lifecycle.py tests/test_current_state_docs.py \
  tests/test_repository_inventory.py tests/test_database_topology_gate.py \
  tests/test_tee_primary_startup.py tests/test_tee_table_registry.py \
  tests/test_tee_replicator_main.py tests/test_tee_sync_scheduler.py \
  tests/test_plaintext_shadow_config.py -q
git diff --check
```

Expected: all focused tests pass with local PostgreSQL enabled and no whitespace errors.

- [ ] **Step 3: Verify protected and public surfaces are untouched**

```bash
git diff --name-only origin/test
git diff --exit-code origin/test -- backend deploy .github docs-site tools/chat_resident_consumer.py
test -f docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md
test ! -f docs/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md
test -f docs/archive/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md
```

Expected: changed files are limited to this plan, the archived plan, the storage/TEE audit, the cleanup index, and the generated inventory; protected-surface diff and file assertions succeed.

- [ ] **Step 4: Final review checkpoint**

Review lifecycle metadata, archive links, evidence claims, deterministic inventory, test output, and scope containment. Do not commit or push until the review is clean and the user has authorized integration steps.
