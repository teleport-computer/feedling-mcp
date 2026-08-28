---
document_lifecycle: historical
canonical_owner: docs/MEMORY.md
historical_reason: implemented
---
# Retire Route-B Readside Island Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Remove the unreachable Route-B selector, feature flag, tests, and stale two-mode documentation while preserving current bucketed recall, trace privacy, and query-parameter compatibility.

**Architecture:** Keep `/v1/chat/history` on its existing real path: host lifecycle filtering, `card_shape.to_garden_card()`, then `select_context_memories_with_trace(..., mode="default")`. Move the old island's useful behavior assertions onto that path before deleting the dead adapter, policy module, test island, and compose flag.

**Tech Stack:** Python 3.11, FastAPI/ASGI, pytest, Docker Compose YAML, Markdown

**Spec:** `docs/repository-cleanup/candidates/route-b-readside-island.md` (introduced by PR #461)

## Implementation Result

- Migrated the current-path recall, trace-privacy, and query-mode convergence guards before deleting the old island.
- Local gates: targeted 180 passed / 8 xfailed; Memory Garden CI lane 262 passed; enclave path plus coverage ratchet 33 passed; first full suite 12082 passed / 3 skipped / 9 xfailed.
- The implementation deleted 540 lines before the post-rebase candidate-record update; excluding this 124-line plan artifact, that verified tree was net -408. PR #461 and the stale-E2E cleanup have since landed, this branch is rebased onto them, and the candidate index now records Route-B as implemented. Test deployment remains gated on integration.

## Global Constraints

- Keep `MEMORY_READSIDE_MODEL_API_LIMIT` and `FEEDLING_MEMORY_READSIDE_HARD_MAX` behavior unchanged.
- Keep accepting `context_mode`, `contextMode`, and `context_strict`; they must not select a different policy.
- Do not change database, envelope, tenant, public API, migration, or resident-consumer behavior.
- Do not reintroduce retired sensitivity fields or classification behavior.
- Remove `MEMORY_READSIDE_FOR_MODEL_API` only from repository-owned compose files; live platform configuration requires separate evidence.

---

### Task 1: Protect the current selector and route

**Files:**
- Modify: `tests/test_context_memories.py`
- Rename: `tests/test_enclave_routeb_readside.py` → `tests/test_enclave_context_recall.py`
- Test: the same two files

**Interfaces:**
- Consumes: `card_shape.to_garden_card(dict) -> dict`, `select_context_memories_with_trace(list[dict], str, mode="default")`
- Produces: regression coverage for canonical content recall, trace privacy, and query-mode convergence

- [x] **Step 1: Add canonical-card recall and trace-privacy tests**

  Add a canonical card whose query term exists only in `content`. Assert the real translated selector recalls it for that term, and assert a rejected card's private content is absent from the serialized trace.

- [x] **Step 2: Replace obsolete flag assertions with a real route convergence test**

  Exercise the existing ASGI fixture with `""`, `"context_mode=model_api"`, and `"context_strict=1"`. Assert every request produces the same ordered ids, includes `mem_cat`, and does not expose strict-only `index_sample` trace state. Do not require `mem_lark` to be absent: the unified bucketed policy intentionally includes recent cards as grounding.

- [x] **Step 3: Prove the convergence guard catches a regression**

  Temporarily change the production selector argument from `mode="default"` to `mode=context_mode or "default"`; run the new convergence test and require the `context_mode=model_api` case to fail. Restore production source, rerun, and require PASS.

- [x] **Step 4: Run the focused green suite**

  Run:

  ```bash
  pytest tests/test_context_memories.py tests/test_enclave_context_recall.py tests/test_garden_card_shape.py -q
  ```

  Expected: PASS with no unexpected failure.

### Task 2: Delete the unreachable island

**Files:**
- Modify: `backend/enclave/readside.py`
- Delete: `backend/memory/selection_policies.py`
- Delete: `tests/test_route_b_card_shape_recall.py`
- Modify: `tests/test_enclave_context_recall.py`
- Modify: `backend/enclave/routes/chat.py`
- Modify: `deploy/docker-compose.phala.test.yaml`
- Modify: `deploy/docker-compose.phala.pre.yaml`
- Modify: `deploy/docker-compose.phala.yaml`

**Interfaces:**
- Preserves: `memory_readside_model_api_limit()`, `memory_readside_effective_limit()`, and the fixed bucketed selector call
- Removes: `memory_readside_for_model_api_enabled()`, `context_moment_to_index_item()`, `select_context_memories_via_readside()`, and the zero-consumer policy module

- [x] **Step 1: Delete old adapter code and dependencies**

  Remove the three dead functions from `readside.py`, plus the now-unused `config` and `select_memory_index_items` imports and stale module description. Delete `selection_policies.py`, whose symbols have no repository consumers.

- [x] **Step 2: Delete old tests without deleting migrated protections**

  Delete `test_route_b_card_shape_recall.py`, rename the remaining route suite to `test_enclave_context_recall.py`, and remove only the old helper/flag tests. Keep candidate-limit, invalid-config, failed-log, route convergence, decrypt, and observability tests.

- [x] **Step 3: Remove the dead deployment flag and stale comments**

  Delete `MEMORY_READSIDE_FOR_MODEL_API` from all three Phala compose files. Rewrite the chat-route comment to state that the query parameters are compatibility inputs and the selector is fixed to default bucketed mode.

- [x] **Step 4: Run deletion-focused verification**

  Run the Task 1 suite plus compose, release-pin, and route tests. Search the non-historical tree for every removed symbol and require zero hits.

### Task 3: Update current documentation and verify the batch

**Files:**
- Modify: `docs/MEMORY.md`
- Modify: `docs/superpowers/plans/2026-08-28-retire-route-b-readside-island.md`
- Modify after PR #461 lands: `docs/repository-cleanup/candidates/route-b-readside-island.md`
- Modify after PR #461 lands: `docs/repository-cleanup/candidates/README.md`

**Interfaces:**
- Produces: one current description of the unified selector and one historical implementation record

- [x] **Step 1: Correct the current memory guide**

  Replace the two-mode section with the real unified bucketed policy. State that `context_mode` and `context_strict` remain accepted compatibility inputs but do not fork selection; keep `context_trace` behavior documented.

- [x] **Step 2: Mark this plan historical after implementation**

  Set `document_lifecycle: historical` and `historical_reason: implemented`, then run `python3 tools/check_document_lifecycle.py --changed-vs HEAD`.

- [x] **Step 3: Run final local gates**

  Run the full targeted baseline, `git diff --check`, document lifecycle validation, and the repository's standard full pytest command with only `tests/test_api.py` ignored.

- [x] **Step 4: Review and integration checkpoint**

  Request independent code review. After findings are closed, commit the batch and use `superpowers:finishing-a-development-branch`; target the PR at `test` and do not merge without the user's integration choice.
