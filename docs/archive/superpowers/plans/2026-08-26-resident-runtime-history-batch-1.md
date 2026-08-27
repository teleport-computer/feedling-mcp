---
document_lifecycle: historical
canonical_owner: docs/repository-cleanup/resident-runtime-history.md
historical_reason: implemented
---
# Resident Runtime History Batch 1 Implementation Plan

> **HISTORICAL / IMPLEMENTED（2026-08-26）**：本批已经完成。当前 Resident runtime
> 历史归档结论与兼容义务由 `docs/repository-cleanup/resident-runtime-history.md` 持有；
> 下述任务、路径和命令仅保留为执行期证据，不应重放。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove implemented resident/V2 runtime-selection plans from the default agent search surface while preserving the live dual-runtime and new-user cohort decisions, their implementation evidence, and every compatibility obligation.

**Architecture:** Treat `docs/CURRENT_STATE.md`, deployed compose, runtime code, and focused tests as current truth. Move only implementation checklists whose goals landed into `docs/archive/superpowers/plans/`; retain accepted designs in place as `decision` documents and record the evidence/rationale transfer in one resident-runtime audit page.

**Tech Stack:** Markdown lifecycle metadata, Git rename history, deterministic lifecycle inventory, pytest.

**Spec:** `docs/superpowers/plans/2026-08-24-repository-cleanup.zh-CN.md`

## Global Constraints

- Target branch is `test`; this batch starts from `origin/test`.
- `tools/chat_resident_consumer.py` remains a single-file VPS distribution boundary and must not be modified.
- Do not change runtime behavior, schema/Alembic, compose, public API, `deploy/`, or `docs-site/`.
- Keep `docs/superpowers/specs/2026-07-21-dual-runtime-v1-v2-coexistence-design.md` as the accepted dual-runtime decision.
- Keep `docs/superpowers/specs/2026-08-10-new-model-api-users-default-v2-design.md` as the accepted cohort decision, but correct its stale “待实现” status using shipped evidence.
- An archived implementation plan uses `document_lifecycle: historical`, `historical_reason: implemented`, and a live current/decision `canonical_owner`.
- Regenerate `docs/repository-cleanup/document-lifecycle-inventory.md` only through `python tools/check_document_lifecycle.py --all --report` and prove byte-for-byte determinism.

---

### Task 1: Archive the implemented dual-runtime coexistence plan

**Files:**
- Create: `docs/repository-cleanup/resident-runtime-history.md`
- Move: `docs/superpowers/plans/2026-07-21-dual-runtime-v1-v2-coexistence.md` → `docs/archive/superpowers/plans/2026-07-21-dual-runtime-v1-v2-coexistence.md`
- Verify: `docs/superpowers/specs/2026-07-21-dual-runtime-v1-v2-coexistence-design.md`

**Interfaces:**
- Consumes: current runtime truth from `docs/CURRENT_STATE.md`, managed compose values, `backend/hosted/runtime_reconciler.py`, migration `0052_dual_runtime_coexistence`, and focused dual-runtime tests.
- Produces: the first batch in `docs/repository-cleanup/resident-runtime-history.md`; Task 2 appends the cohort lineage to the same audit.

- [ ] **Step 1: Prove the plan has no current-path consumers**

Run exact full-path and basename searches excluding the generated inventory. Expected: no callers that use the implementation plan as a runbook; references to the retained design remain allowed.

```bash
rg -n -F 'docs/superpowers/plans/2026-07-21-dual-runtime-v1-v2-coexistence.md' . --glob '!docs/repository-cleanup/document-lifecycle-inventory.md'
rg -n -F '2026-07-21-dual-runtime-v1-v2-coexistence.md' . --glob '!docs/repository-cleanup/document-lifecycle-inventory.md'
```

- [ ] **Step 2: Move and classify the implementation plan**

Use `git mv`, then prepend exactly:

```yaml
---
document_lifecycle: historical
canonical_owner: docs/superpowers/specs/2026-07-21-dual-runtime-v1-v2-coexistence-design.md
historical_reason: implemented
---
```

- [ ] **Step 3: Create the resident-runtime audit page**

Create a current/self document. Record the original document, lifecycle/owner, repository callers, implementation/test evidence, compatibility obligations, archive path, and rationale transfer. The evidence must name commits `bda95682`, `db178b0f`, and `5b8fee70`, plus current compose policy `dual`, default desired `resident`, per-user fence/generation, bidirectional rollback, separate runner CVM, and the protected self-hosted consumer boundary.

- [ ] **Step 4: Validate Task 1**

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_document_lifecycle.py tests/test_current_state_docs.py -q
git diff --check
```

Expected: lifecycle validation succeeds, tests pass, and diff check is empty.

- [ ] **Step 5: Commit Task 1**

```bash
git add docs/archive/superpowers/plans/2026-07-21-dual-runtime-v1-v2-coexistence.md \
  docs/repository-cleanup/resident-runtime-history.md
git commit -m "docs: archive dual-runtime implementation plan"
```

### Task 2: Archive the implemented new-user V2 cohort plan

**Files:**
- Move: `docs/superpowers/plans/2026-08-10-new-model-api-users-default-v2.md` → `docs/archive/superpowers/plans/2026-08-10-new-model-api-users-default-v2.md`
- Modify: `docs/superpowers/specs/2026-08-10-new-model-api-users-default-v2-design.md`
- Modify: `docs/repository-cleanup/resident-runtime-history.md`

**Interfaces:**
- Consumes: `backend/hosted/new_user_v2_cohort.py`, setup/resident pin integration, current runbook/public docs, PR #177, and the current `FEEDLING_V2_NEW_USER_CUTOFF` compose wiring.
- Produces: a retained cohort decision with current lifecycle metadata and an archive record that explicitly says the old implementation checklist is not a current test/runbook contract.

- [ ] **Step 1: Prove implementation and reference ownership**

Run exact full-path/basename searches for the plan and implementation searches for `new_user_v2_cohort`, `new-user-cohort`, and `FEEDLING_V2_NEW_USER_CUTOFF`. Expected: no current caller depends on the plan; current behavior is owned by code, compose, `docs/HOSTED_RUNTIME_V2_ADDING_USERS.md`, `deploy/DEPLOYMENTS.md`, and public architecture/workflow docs.

- [ ] **Step 2: Retain and classify the accepted design**

Prepend exactly:

```yaml
---
document_lifecycle: decision
canonical_owner: self
---
```

Change its status line from `状态：已批准，待实现` to `状态：已批准并实现；当前运行状态与后续修订以 docs/CURRENT_STATE.md 和现行 runbook 为准`.

- [ ] **Step 3: Move and classify the implementation plan**

Use `git mv`, then prepend exactly:

```yaml
---
document_lifecycle: historical
canonical_owner: docs/superpowers/specs/2026-08-10-new-model-api-users-default-v2-design.md
historical_reason: implemented
---
```

- [ ] **Step 4: Append the cohort lineage to the audit**

Record PR #177 and commits `e67c3c68`, `155d98cd`, `3acb27c8`, `5347ade4`, `05223b8f`, and `5b8fee70`; current fail-safe resident default; active tested route requirement; manual pin priority; setup/fence convergence; rollback; and the fact that the plan's proposed test-file checklist did not land verbatim and therefore must not be treated as the current test command.

- [ ] **Step 5: Validate Task 2**

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_document_lifecycle.py tests/test_current_state_docs.py \
  tests/test_hosted_runtime_policy.py tests/test_runtime_reconciler.py -q
git diff --check
```

Expected: lifecycle validation succeeds, tests pass with local PostgreSQL enabled, and diff check is empty.

- [ ] **Step 6: Commit Task 2**

```bash
git add docs/archive/superpowers/plans/2026-08-10-new-model-api-users-default-v2.md \
  docs/superpowers/specs/2026-08-10-new-model-api-users-default-v2-design.md \
  docs/repository-cleanup/resident-runtime-history.md
git commit -m "docs: classify resident runtime selection history"
```

### Task 3: Regenerate the lifecycle inventory and run final regression

**Files:**
- Modify: `docs/repository-cleanup/document-lifecycle-inventory.md`
- Verify: all files changed by Tasks 1–2 plus this plan.

**Interfaces:**
- Consumes: the classifications and paths produced by Tasks 1–2.
- Produces: deterministic inventory output and final evidence that the documentation-only batch did not change runtime behavior.

- [ ] **Step 1: Stage the plan, regenerate through temporary files, and prove deterministic inventory**

```bash
git add docs/superpowers/plans/2026-08-26-resident-runtime-history-batch-1.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/resident-runtime-lifecycle-inventory-a.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/resident-runtime-lifecycle-inventory-b.md
cmp /tmp/resident-runtime-lifecycle-inventory-a.md /tmp/resident-runtime-lifecycle-inventory-b.md
cp /tmp/resident-runtime-lifecycle-inventory-a.md docs/repository-cleanup/document-lifecycle-inventory.md
python3 tools/check_document_lifecycle.py --all --report > /tmp/resident-runtime-lifecycle-inventory-after-write.md
cmp docs/repository-cleanup/document-lifecycle-inventory.md /tmp/resident-runtime-lifecycle-inventory-after-write.md
```

Expected: both `cmp` commands exit 0; the generated corpus includes the now-tracked
batch plan and the inventory itself. Generate through temporary files because direct
shell redirection to the tracked inventory truncates that input before the checker
scans it and incorrectly changes the `generated` count.

- [ ] **Step 2: Run lifecycle, repository, and runtime-selection regressions**

```bash
python3 tools/check_document_lifecycle.py --changed-vs origin/test
NO_PROXY='*' no_proxy='*' \
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_document_lifecycle.py tests/test_current_state_docs.py \
  tests/test_repository_inventory.py tests/test_dual_runtime_coexistence.py \
  tests/test_dual_runtime_send_routing.py tests/test_hosted_runtime_policy.py \
  tests/test_runtime_reconciler.py -q
git diff --check
```

Expected: all tests pass with local PostgreSQL enabled and no whitespace errors.

- [ ] **Step 3: Verify protected and public surfaces are untouched**

```bash
git diff --name-only origin/test
git diff --exit-code origin/test -- tools/chat_resident_consumer.py backend deploy docs-site
```

Expected: the changed-file list contains only the plan, audit, archived plans, retained cohort decision, and generated inventory; the protected-surface diff exits 0.

- [ ] **Step 4: Commit the generated inventory and execution plan**

```bash
git add docs/repository-cleanup/document-lifecycle-inventory.md
git commit -m "docs: record resident runtime history batch"
```
