---
document_lifecycle: historical
canonical_owner: docs/repository-cleanup/candidates/user-store-refresh-compatibility.md
historical_reason: implemented
---
# Retire UserStore Refresh Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Remove the test-only targeted-refresh fallback while preserving production `UserStore` section invalidation and wake dispatch isolation.

**Architecture:** Keep `wake_bus._dispatch()` routed through `_refresh_store_channel()`, but require the cache's production `UserStore` section API instead of probing for it and directly calling private loaders. Test the positive path with a real `UserStore`, and test the retired-adapter boundary through wake dispatch so listener isolation remains observable.

**Tech Stack:** Python 3.11, pytest, PostgreSQL test fixture, Markdown

**Spec:** `docs/repository-cleanup/candidates/user-store-refresh-compatibility.md`

## Implementation Result

This record was added after implementation because the bounded in-chat design was incorrectly treated as sufficient; the repository cleanup master plan requires a child plan for every accepted code candidate. Commit `6d3aa396` contains the initial implementation. Review corrections replace the incidental `AttributeError` assertion with dispatch-level behavior, restore multiplicity checking, correct diff accounting, and regenerate lifecycle inventory after every new document is tracked.

## Global Constraints

- Preserve `frames`, `blob`, and `proactive` production behavior.
- Preserve cold-section no-load, stale/fresh, single-flight, failure-retention, and telemetry semantics.
- Preserve wake listener exception isolation.
- Do not modify schema, migration, public API, deployment configuration, or `tools/chat_resident_consumer.py`.
- Merge through a PR targeting `test`; record exact deployed SHA and TEST regression evidence after integration.

---

### Task 1: Retire the fallback behind a failing dispatch contract

**Files:**
- Modify: `backend/core/store.py`
- Modify: `tests/test_wake_bus.py`
- Modify: `tests/test_store_cache.py`

**Interfaces:**
- Consumes: `_refresh_store_channel(user_id: str, channel: str) -> bool`
- Preserves: `UserStore.note_section_change()` and `UserStore.ensure_sections()`
- Removes: direct private-loader refresh for objects without the section API

- [x] **Step 1: Write the failing dispatch test**

  Add a legacy adapter with `_load_frames_meta()` but no section API, dispatch a `frames` event,
  and assert the private loader was not called while the registered extra handler still ran.

- [x] **Step 2: Verify RED on the base**

  Run:

  ```bash
  python -m pytest \
    tests/test_wake_bus.py::test_store_channel_does_not_bypass_sections_or_stop_dispatch -q
  ```

  Expected on `7f639ceb`: FAIL because the legacy fallback sets `adapter.loaded` to `True`.

- [x] **Step 3: Delete the fallback**

  Remove the `hasattr()` fork, direct private-loader calls, and local reload-guard management.
  Keep the existing section candidates and proactive waiter branch unchanged.

- [x] **Step 4: Migrate the positive test to the production contract**

  Use a real `UserStore` with loaded `SectionSlot`s. Assert frames refreshes once, every blob
  component refreshes exactly once, and the real proactive waiter event is set.

- [x] **Step 5: Verify GREEN**

  Run:

  ```bash
  FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
    python -m pytest \
    tests/test_store_cache.py tests/test_blob_wake.py tests/test_wake_bus.py -q
  ```

  Final review-corrected result: 76 passed.

### Task 2: Record and verify the cleanup

**Files:**
- Modify: `docs/CHANGELOG.md`
- Modify: `docs/repository-cleanup/candidates/README.md`
- Modify: `docs/repository-cleanup/candidates/user-store-refresh-compatibility.md`
- Modify: `docs/repository-cleanup/document-lifecycle-inventory.md`
- Create: `docs/superpowers/plans/2026-08-29-retire-user-store-refresh-adapter.md`

**Interfaces:**
- Produces: reproducible candidate evidence and a historical child implementation plan

- [x] **Step 1: Record exact scope and accounting**

  Record the base SHA, production consumers, compatibility decision, rollback, and production
  diff as 47 deletions / 24 additions / net −23 lines.

- [x] **Step 2: Run local quality gates**

  Run focused pytest, `py_compile`, pyflakes, `git diff --check`, changed-document lifecycle
  validation, and deterministic lifecycle inventory generation through a temporary file.

- [ ] **Step 3: Run CI and TEST integration gates**

  Push the branch, open a PR to `test`, require CI green, merge, then verify exact deployed SHA,
  public health, main/runner CVM processes, and canonical P0 cells appropriate to this internal
  cache-invalidation change.

- [ ] **Step 4: Record TEST evidence**

  Update the candidate record with the merged SHA and TEST results. If cross-worker refresh or
  P0 regresses, revert the cleanup commit; no schema or data recovery is required.
