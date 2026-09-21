---
document_lifecycle: current
canonical_owner: docs/superpowers/specs/2026-09-15-tee-ciphertext-repair-design.md
---

# Plaintext Migration Failure Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make historical plaintext migration failures diagnosable, safely retryable, and resumable without repeatedly hammering deterministic poison rows.

**Architecture:** Preserve content-free JSONL logs, but attach a bounded stable failure class/detail derived from the existing enclave/R2/CAS exception paths. Treat CAS conflicts and transient enclave/auth failures as retryable, while classifying deterministic decrypt/data-shape/storage failures for quarantine and reporting. Keep the existing per-user checkpoint and continue-on-failure behavior, adding aggregate retry/report data without changing normal write paths.

**Tech Stack:** Python 3, psycopg/Postgres, pytest, existing `plaintext_migration` and `plaintext_repair` modules.

**Spec:** `docs/superpowers/plans/2026-09-15-tee-ciphertext-repair-design.md`

## Global Constraints

- Never log message bodies, keys, DSNs, plaintext, or full exception messages.
- Keep `workers` bounded to 1–4 and preserve the existing non-destructive CAS behavior.
- A failed item must not stop later users when `--continue-on-failure` is enabled.
- Retry logic must not convert a deterministic decrypt/data-shape failure into an infinite loop.
- Existing checkpoint and failure-log paths remain backward compatible with old JSONL records.
- Changes target the `test` branch; no production deployment or data mutation is part of this plan.

## Review Focus

- Enclave 401/403/token expiry: classify as transient and refresh the runtime token before retrying.
- Enclave `decrypt_failed`/bad nonce/AEAD verification: classify as deterministic and quarantine rather than repeatedly retrying.
- R2 missing/not-hydrated: distinguish missing object from temporary fetch failure without logging the object key or body.
- CAS conflict: keep it separately retryable and count it independently from decrypt failures.
- Legacy failure-log records with only `exception_type`: parse safely and preserve compatibility.

### Task 1: Stable content-free failure classification

**Files:**
- Modify: `backend/core/enclave.py` (expose bounded failure metadata already produced by decrypt errors)
- Modify: `backend/content/plaintext_migration.py` (map exception/status to stable class/detail)
- Modify: `backend/content/plaintext_repair.py` (persist `failure_class` and `failure_detail`)
- Test: `tests/test_effective_off_content_repair.py`
- Test: `tests/test_user_content_plaintext_migration.py`

- [ ] Write failing tests for decrypt HTTP 403, token expiry, R2 missing, CAS conflict, and generic RuntimeError classification.
- [ ] Run the focused tests and verify they fail because the log record lacks stable classification.
- [ ] Implement bounded classification using exception attributes/message prefixes only; cap detail length and never include dynamic content.
- [ ] Persist the new fields while accepting old records without them.
- [ ] Run the focused tests and verify they pass.

### Task 2: Separate retryable and deterministic failure lanes

**Files:**
- Modify: `backend/content/plaintext_migration.py`
- Modify: `backend/content/plaintext_repair.py`
- Modify: `backend/migrate_effective_off_content_to_plaintext.py`
- Test: `tests/test_effective_off_content_repair.py`
- Test: `tests/test_user_content_plaintext_migration.py`

- [ ] Write failing tests proving CAS/transient failures are marked retryable while decrypt/data-shape failures are not.
- [ ] Run the tests and verify the retryability assertions fail.
- [ ] Add a stable retryable flag/class to result counts and failure records; keep `--continue-on-failure` semantics unchanged.
- [ ] Make `--retry-failures` select only retryable records by default, with an explicit opt-in for deterministic records.
- [ ] Run the focused tests and verify they pass.

### Task 3: Operator summary and retry report

**Files:**
- Modify: `backend/content/plaintext_repair.py`
- Modify: `backend/migrate_effective_off_content_to_plaintext.py`
- Test: `tests/test_effective_off_content_repair.py`

- [ ] Write failing tests for content-free aggregate summaries by status/class/surface and for retry selection counts.
- [ ] Run the tests and verify they fail because reports contain only flat item counts.
- [ ] Add deterministic aggregate fields to JSON output and a report-only mode for the failure log; do not expose bodies, keys, or DSNs.
- [ ] Add operator documentation for retrying transient/CAS failures first and quarantining deterministic failures.
- [ ] Run focused migration tests, `git diff --check`, and the relevant full test subset.

### Task 4: Verification and handoff

**Files:**
- Verify: `backend/content/plaintext_migration.py`
- Verify: `backend/content/plaintext_repair.py`
- Verify: `backend/migrate_effective_off_content_to_plaintext.py`
- Verify: `tests/test_effective_off_content_repair.py`
- Verify: `tests/test_user_content_plaintext_migration.py`

- [ ] Run the focused migration test files with the repository Postgres test environment.
- [ ] Run the relevant plaintext/enclave boundary tests.
- [ ] Confirm the JSONL schema remains content-free and old records remain readable.
- [ ] Report exact test results and leave deployment/data execution to the maintainer.
