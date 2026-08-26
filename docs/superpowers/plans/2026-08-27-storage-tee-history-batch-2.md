---
document_lifecycle: current
canonical_owner: docs/superpowers/plans/2026-08-24-repository-cleanup.zh-CN.md
---
# Storage/TEE History Batch 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Turn the TEE Postgres provisioning guide into an accurate current operations owner, remove the completed Phase 0–1 implementation checklist from the default agent search surface, and preserve the infrastructure, recovery, migration, and trust-boundary obligations that remain active.

**Architecture:** Keep `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` as the current provisioning, backup, restore, and CVM operations owner, while explicitly labeling its RDS-to-TEE shadow and dual-write sections as historical migration-stage material. Make `deploy/DEPLOYMENTS.md` describe environment authority through exact releases and live configuration instead of a historical plan's task numbers. Then move the implemented Phase 0–1 plan into the archive and record the evidence and compatibility obligations in the storage/TEE audit.

**Tech Stack:** Markdown lifecycle metadata, Git rename history, deterministic lifecycle inventory, pytest.

**Spec:** `docs/superpowers/plans/2026-08-24-repository-cleanup.zh-CN.md`

## Global Constraints

- Target branch is `test`; this batch starts from `origin/test` commit `e658740d`.
- Do not change runtime behavior, RDS or TEE schema/Alembic history, compose, workflows, public API, `backend/`, `.github/`, `docs-site/`, or `tools/chat_resident_consumer.py`.
- `deploy/DEPLOYMENTS.md` is the only allowed changed path under `deploy/`; it is a current deployment record, not executable deployment configuration.
- Do not archive `docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md`. Its partially retired rationale needs a separate review.
- Do not infer current prod or pre primary selection from the existence of a TEE Postgres CVM. Environment authority must come from the exact deployed release and live configuration.
- Preserve the direction boundary: historical RDS primary → TEE shadow is not the current TEE primary → plaintext projection topology.
- The archived plan must use `document_lifecycle: historical`, `historical_reason: implemented`, and `canonical_owner: docs/TEE_POSTGRES_SHADOW_PROVISIONING.md`.
- Regenerate `docs/repository-cleanup/document-lifecycle-inventory.md` only through `python3 tools/check_document_lifecycle.py --all --report` and prove byte-for-byte determinism.

---

### Task 1: Establish the current TEE Postgres operations owner

**Files:**
- Modify: `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md`
- Modify: `deploy/DEPLOYMENTS.md`
- Verify: `docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md`

**Interfaces:**
- Consumes: exact deployment records, current migration runbook, provisioning scripts, backup/restore helpers, and environment topology gates.
- Produces: one current provisioning/operations owner and deployment records that no longer depend on historical task numbering.

- [ ] **Step 1: Classify the two current documents**

Add `document_lifecycle: current` and `canonical_owner: self` front matter to the provisioning guide and deployment record.

- [ ] **Step 2: Correct the provisioning guide's authority and time boundaries**

Rename it to a TEE Postgres CVM provisioning, migration, and recovery operations guide. State that it originated during the RDS-to-TEE shadow migration, that test has used TEE primary since release `82c4c019`, and that current environment authority comes from the exact deployed release, `deploy/DEPLOYMENTS.md`, the current migration runbook, and live configuration. Mark the old dual-write/backfill instructions and the 2026-07-29 secret outage as historical; mark fixed schema, table-count, and backup values as point-in-time evidence.

Preserve current infrastructure invariants: isolated CVM/KMS identity, direct TLS, owner/app role separation, checkout-HEAD immutable images, WAL-G backup, external custody for the WAL-G encryption key and TLS CA private key, whole-secret redeploy behavior, cross-worker LISTEN/NOTIFY idle/reconnect acceptance, restore completion at `pg_is_in_recovery() = false`, and release-derived Alembic head.

- [ ] **Step 3: Remove the deployment record's dependency on historical plan tasks**

Replace the stale blanket statement that test/prod are dual-writing with environment-specific authority guidance. Remove the Phase/P1 task-number references and point operators to the current provisioning guide for CVM/TLS/backup/restore and the migration runbook for promotion/topology. Do not assert unverified prod or pre live topology.

- [ ] **Step 4: Validate Task 1**

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_document_lifecycle.py tests/test_current_state_docs.py \
  tests/test_repository_inventory.py -q
git diff --check
```

Expected: lifecycle validation succeeds, 45 documentation tests pass, and diff check is empty.

### Task 2: Archive the implemented Phase 0–1 plan and transfer its obligations

**Files:**
- Move: `docs/superpowers/plans/2026-07-07-tee-pg-phase0-1-infra.md` → `docs/archive/superpowers/plans/2026-07-07-tee-pg-phase0-1-infra.md`
- Modify: `docs/repository-cleanup/storage-tee-history.md`

**Interfaces:**
- Consumes: Phase 0–1 delivery commit `08afdcc0`, provisioning guide history beginning at `64bcc96a`, test TEE-primary promotion `82c4c019`, and current restore/migration operations.
- Produces: one historical implementation record plus an auditable rationale and compatibility transfer.

- [ ] **Step 1: Prove reference ownership before the move**

Run exact full-path and basename searches excluding the generated lifecycle inventory. Expected before editing: only `deploy/DEPLOYMENTS.md` names the full path as a task-number reference; no runtime, workflow, compose, or migration code consumes it.

```bash
rg -n -F 'docs/superpowers/plans/2026-07-07-tee-pg-phase0-1-infra.md' . \
  --glob '!docs/repository-cleanup/document-lifecycle-inventory.md'
rg -n -F '2026-07-07-tee-pg-phase0-1-infra.md' . \
  --glob '!docs/repository-cleanup/document-lifecycle-inventory.md'
```

- [ ] **Step 2: Move and classify the plan**

Use `git mv`, then prepend:

```yaml
---
document_lifecycle: historical
canonical_owner: docs/TEE_POSTGRES_SHADOW_PROVISIONING.md
historical_reason: implemented
---
```

- [ ] **Step 3: Extend the storage/TEE audit**

Record the archive decision, original inbound reference, implementation and promotion evidence, and current owners. Transfer the enduring obligations: isolated identities, direct TLS, separated credentials, WAL-G fail-closed operation before loading data, external custody for the WAL-G encryption key and TLS CA private key, checkout-HEAD immutable image tags, whole-secret redeploy, cross-worker LISTEN/NOTIFY idle/reconnect acceptance, completed recovery rather than mere server readiness, release-derived schema head, and retention of `deploy/postgres/`, `backend/core/wake_bus.py`, plus `backend/alembic_tee/`.

- [ ] **Step 4: Prove the old path is no longer authoritative**

Repeat the exact searches. Expected after editing: the old path may appear only in this implementation plan as audit evidence; current deployment and operations documents do not point agents to it.

### Task 3: Regenerate lifecycle inventory and run focused regressions

**Files:**
- Modify: `docs/repository-cleanup/document-lifecycle-inventory.md`
- Verify: all files changed by Tasks 1–2 plus this plan.

**Interfaces:**
- Consumes: lifecycle classifications and Git-tracked document paths.
- Produces: deterministic inventory and evidence that the documentation-only cleanup did not change storage/TEE behavior.

- [ ] **Step 1: Stage all new and moved documents, regenerate through temporary files, and prove determinism**

```bash
git add \
  deploy/DEPLOYMENTS.md \
  docs/TEE_POSTGRES_SHADOW_PROVISIONING.md \
  docs/archive/superpowers/plans/2026-07-07-tee-pg-phase0-1-infra.md \
  docs/repository-cleanup/storage-tee-history.md \
  docs/superpowers/plans/2026-08-27-storage-tee-history-batch-2.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/storage-tee-batch2-inventory-a.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/storage-tee-batch2-inventory-b.md
cmp /tmp/storage-tee-batch2-inventory-a.md /tmp/storage-tee-batch2-inventory-b.md
cp /tmp/storage-tee-batch2-inventory-a.md docs/repository-cleanup/document-lifecycle-inventory.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/storage-tee-batch2-inventory-after-write.md
cmp docs/repository-cleanup/document-lifecycle-inventory.md /tmp/storage-tee-batch2-inventory-after-write.md
```

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
  tests/test_plaintext_shadow_config.py tests/test_postgres_restore_wait.py \
  tests/test_wake_bus.py -q
git diff --check
```

- [ ] **Step 3: Verify protected scope**

```bash
git diff --exit-code origin/test -- backend .github docs-site tools/chat_resident_consumer.py
test "$(git diff --name-only origin/test -- deploy)" = 'deploy/DEPLOYMENTS.md'
test -f docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md
test ! -f docs/superpowers/plans/2026-07-07-tee-pg-phase0-1-infra.md
test -f docs/archive/superpowers/plans/2026-07-07-tee-pg-phase0-1-infra.md
```

- [ ] **Step 4: Review checkpoint**

Review lifecycle metadata, current/historical wording, exact evidence claims, archive links, deterministic inventory, test output, and scope containment. Commit and open a PR against `test` only after verification and independent review are clean.
