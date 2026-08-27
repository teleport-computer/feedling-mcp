---
document_lifecycle: historical
canonical_owner: docs/repository-cleanup/storage-tee-history.md
historical_reason: implemented
---
# Storage/TEE History Batch 3 Implementation Plan

> **HISTORICAL / IMPLEMENTED（2026-08-27）**：本批已经完成。当前 Storage/TEE
> 历史归档结论、D1–D4 决策结果与兼容义务由
> `docs/repository-cleanup/storage-tee-history.md` 持有；下述任务、路径和命令仅保留为
> 执行期证据，不应重放。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with review checkpoints.

**Goal:** Remove the superseded 2026-07-04 all-plaintext Postgres migration design from the default agent search surface while preserving its adopted infrastructure decisions and making the rejected content-encryption decisions explicit.

**Architecture:** Treat `docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md` as the current content-shape, mixed-client, promotion, rollback, and plaintext-shadow owner, and `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` as the current PG CVM/TLS/backup/restore owner. Move the old design and the two completed cleanup batch plans to the archive, update historical inbound links, and record a D1–D4 outcome matrix in the storage/TEE audit so agents do not mistake the old “remove all envelopes” destination for current policy.

**Tech Stack:** Markdown lifecycle metadata, Git rename history, deterministic lifecycle inventory, pytest.

**Spec:** `docs/superpowers/plans/2026-08-24-repository-cleanup.zh-CN.md`

## Global Constraints

- Target branch is `test`; after the pre-PR rebase this batch starts from `origin/test` commit `c4976326`.
- Do not change runtime behavior, RDS or TEE schema/Alembic history, compose, workflows, deployment records, public API, `backend/`, `deploy/`, `.github/`, `docs-site/`, or `tools/chat_resident_consumer.py`.
- Preserve current mixed encrypted/plaintext behavior. `local_only` remains encrypted; old encrypted uploads remain accepted for plaintext-tier accounts; reads remain row-shape driven; historical ciphertext is not rewritten merely because a preference changes.
- Do not interpret the archive as authorization to delete envelope readers, rewrap paths, provider/runtime credential protection, enclave routes, compatibility gates, legacy migration components, or R2 object-storage support.
- The archived design must use `document_lifecycle: historical`, `historical_reason: superseded`, and both `canonical_owner` and `superseded_by` must resolve to `docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md`.
- The completed batch 1 and batch 2 execution plans must use `document_lifecycle: historical`, `historical_reason: implemented`, and retain the repository-cleanup plan as their canonical owner. Their implementation-era command bodies remain historical evidence.
- `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` remains the current owner for adopted D2 infrastructure details; the storage/TEE audit records both current owners and D1–D4 outcomes.
- Regenerate `docs/repository-cleanup/document-lifecycle-inventory.md` only through `python3 tools/check_document_lifecycle.py --all --report` and prove byte-for-byte determinism.

---

### Task 1: Archive the superseded migration design and transfer decisions

**Files:**
- Move: `docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md` → `docs/archive/superpowers/specs/2026-07-04-tee-postgres-migration-design.md`
- Move: `docs/superpowers/plans/2026-08-26-storage-tee-history-batch-1.md` → `docs/archive/superpowers/plans/2026-08-26-storage-tee-history-batch-1.md`
- Move: `docs/superpowers/plans/2026-08-27-storage-tee-history-batch-2.md` → `docs/archive/superpowers/plans/2026-08-27-storage-tee-history-batch-2.md`
- Modify: `docs/archive/superpowers/plans/2026-07-07-tee-pg-phase0-1-infra.md`
- Modify: `docs/archive/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md`
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/repository-cleanup/storage-tee-history.md`

**Interfaces:**
- Consumes: current migration runbook, current PG CVM operations guide, archived Phase 0–1 and Phase 2–3 plans, implementation history, and exact inbound references.
- Produces: one archived historical design, valid historical references, and an explicit decision-outcome record.

- [ ] **Step 1: Prove reference ownership before the move**

Run exact full-path and basename searches excluding the generated lifecycle inventory. Expected: inbound references are limited to archived implementation records, historical changelog/audit material, and this batch's own move/search assertions; no other current plan, runtime, workflow, compose, current runbook, or public docs consume the design as current authority.

```bash
rg -n -F 'docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md' . \
  --glob '!docs/repository-cleanup/document-lifecycle-inventory.md'
rg -n -F '2026-07-04-tee-postgres-migration-design.md' . \
  --glob '!docs/repository-cleanup/document-lifecycle-inventory.md'
```

- [ ] **Step 2: Move and classify the design**

Use `git mv`, then prepend:

```yaml
---
document_lifecycle: historical
canonical_owner: docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md
historical_reason: superseded
superseded_by: docs/CONTENT_ENCRYPTION_TEE_MIGRATION_RUNBOOK.md
---
```

Add a concise `SUPERSEDED / DO NOT IMPLEMENT` banner. State that current mixed encrypted/plaintext support and environment topology come from the migration runbook, PG CVM operations guide, exact deployed release, and live configuration.

- [ ] **Step 3: Repair historical inbound links**

Update the top-level `Spec:` references in both archived implementation plans and the historical changelog path to the archive location. Preserve old command snippets and plan bodies as historical evidence rather than rewriting their implementation-era content.

- [ ] **Step 4: Record D1–D4 outcomes and rationale transfer**

Extend the storage/TEE audit with:

- D1 rejected: `local_only` remains encrypted; explicit encrypted tier remains supported.
- D2 adopted with corrections: independent PG CVM, direct TLS, native Postgres wire, LISTEN/NOTIFY, separate roles, WAL-G, and no PG AppAuth under `--kms phala`.
- D3 rejected: compatible old encrypted uploads remain accepted; releases keep mixed-row readers and fail-safe encryption for unknown users instead of a forced all-plaintext cutover.
- D4 partially adopted and redefined: large frame/attachment bodies may stay in R2 with owner/pointer/SHA-256/length validation and mixed-shape readers. Encrypted-tier objects remain ciphertext, but `plaintext_v1` bodies are raw plaintext bytes in R2, so object storage and backups are plaintext recipients; the original KMS storage-encryption / “plaintext never leaves enclave” assumption was not adopted.
- The original terminal goal to delete the entire envelope layer, rewrap paths, and encrypted-account support is superseded and is not a cleanup candidate.
- Current owner split: migration runbook for content/topology gates and provisioning guide for PG CVM operations.

Record source/design commit `051221af`, initial Phase delivery `08afdcc0`, partial-retirement marker `5b297f79`, and test TEE-primary promotion `82c4c019`.

- [ ] **Step 5: Validate Task 1**

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_document_lifecycle.py tests/test_current_state_docs.py \
  tests/test_repository_inventory.py -q
git diff --check
```

Expected: lifecycle validation succeeds, 45 documentation tests pass, and diff check is empty.

### Task 2: Regenerate lifecycle inventory and verify content/storage contracts

**Files:**
- Modify: `docs/repository-cleanup/document-lifecycle-inventory.md`
- Verify: all files changed by Task 1 plus this plan.

**Interfaces:**
- Consumes: archive classification and Git-tracked document paths.
- Produces: deterministic lifecycle inventory and evidence that the documentation-only archive did not alter mixed content or storage behavior.

- [ ] **Step 1: Stage all new and moved documents, regenerate through temporary files, and prove determinism**

```bash
git add \
  docs/CHANGELOG.md \
  docs/archive/superpowers/plans/2026-07-07-tee-pg-phase0-1-infra.md \
  docs/archive/superpowers/plans/2026-07-07-tee-pg-phase2-3-shadow-dualwrite.md \
  docs/archive/superpowers/plans/2026-08-26-storage-tee-history-batch-1.md \
  docs/archive/superpowers/plans/2026-08-27-storage-tee-history-batch-2.md \
  docs/archive/superpowers/specs/2026-07-04-tee-postgres-migration-design.md \
  docs/repository-cleanup/storage-tee-history.md \
  docs/superpowers/plans/2026-08-27-storage-tee-history-batch-3.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/storage-tee-batch3-inventory-a.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/storage-tee-batch3-inventory-b.md
cmp /tmp/storage-tee-batch3-inventory-a.md /tmp/storage-tee-batch3-inventory-b.md
cp /tmp/storage-tee-batch3-inventory-a.md docs/repository-cleanup/document-lifecycle-inventory.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/storage-tee-batch3-inventory-after-write.md
cmp docs/repository-cleanup/document-lifecycle-inventory.md /tmp/storage-tee-batch3-inventory-after-write.md
```

- [ ] **Step 2: Run lifecycle and focused compatibility regressions**

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
NO_PROXY='*' no_proxy='*' \
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_document_lifecycle.py tests/test_current_state_docs.py \
  tests/test_repository_inventory.py tests/test_database_topology_gate.py \
  tests/test_content_encryption_preference.py tests/test_uploaded_envelope_gate.py \
  tests/test_plaintext_enclave_boundary.py tests/test_frame_r2.py \
  tests/test_object_storage.py -q
git diff --check
```

- [ ] **Step 3: Verify protected scope and archive paths**

```bash
git diff --exit-code origin/test -- backend deploy .github docs-site tools/chat_resident_consumer.py
test ! -f docs/superpowers/specs/2026-07-04-tee-postgres-migration-design.md
test -f docs/archive/superpowers/specs/2026-07-04-tee-postgres-migration-design.md
```

Expected changed files are limited to this plan, the archived spec, two completed cleanup-plan archives, two archived-plan link repairs, historical changelog path, storage/TEE audit, and generated inventory.

- [ ] **Step 4: Review checkpoint**

Review decision outcomes, current-owner split, lifecycle metadata, historical links, exact commit evidence, deterministic inventory, tests, and protected scope. Commit and open a PR against `test` only after independent review is clean.
