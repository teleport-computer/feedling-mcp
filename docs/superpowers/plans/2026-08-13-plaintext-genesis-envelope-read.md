# Plaintext Genesis Envelope Read Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore Genesis staged imports and checkpoint resumes for plaintext accounts while preserving encrypted enclave decryption.

**Architecture:** Route both reads through `core_envelope.read_envelope_body`, the existing storage-shape boundary. Keep digest/JSON validation and all lifecycle behavior unchanged.

**Tech Stack:** Python 3, pytest, PostgreSQL-backed Feedling backend, FastAPI, Phala CVM.

## Global Constraints

- Plaintext `body` and `body_b64` envelopes must be read locally.
- Only `body_ct` envelopes may call `/v1/envelope/decrypt`.
- Do not change the public request or response contract.
- Push directly to `pre` only because the user explicitly authorized it.

---

### Task 1: Protect plaintext staged payload and checkpoint reads

**Files:**
- Modify: `tests/test_genesis_service.py`
- Modify: `backend/genesis/service.py`

**Interfaces:**
- Consumes: `core_envelope.read_envelope_body(envelope, api_key, *, purpose, runtime_token="") -> bytes`
- Produces: shape-aware behavior from `load_genesis_staged_payload` and `load_genesis_checkpoint`

- [ ] **Step 1: Write failing regression tests**

Add one test that persists a staged blob whose `content_envelope` contains a
literal UTF-8 `body`, and one test that persists a checkpoint with the same
shape. Patch `_decrypt_envelope_via_enclave` to raise if called, then assert the
real loaders return the original literal payloads.

- [ ] **Step 2: Verify RED**

Run:

```bash
uv run pytest tests/test_genesis_service.py -q -k 'plaintext and (staged or checkpoint)'
```

Expected: both tests fail because the current loaders call the patched enclave
decrypt function.

- [ ] **Step 3: Implement the minimal routing fix**

Replace the two direct calls to `core_enclave._decrypt_envelope_via_enclave`
with `core_envelope.read_envelope_body`, preserving each existing purpose and
passing `runtime_token` only for checkpoint loading.

- [ ] **Step 4: Verify GREEN and surrounding Genesis behavior**

Run:

```bash
uv run pytest tests/test_genesis_service.py -q
uv run pytest tests/test_genesis_plaintext_routes.py tests/test_genesis_worker.py -q
```

Expected: all selected tests pass.

### Task 2: Document and release to PRE

**Files:**
- Modify: `docs-site/content/docs/changelog.mdx`

**Interfaces:**
- Produces: an Unreleased changelog entry describing restored plaintext import behavior

- [ ] **Step 1: Add the changelog entry**

Record that plaintext-account estimate/commit imports and checkpoint resumes
now read plaintext envelopes locally, while encrypted accounts retain enclave
decryption.

- [ ] **Step 2: Run required verification**

Run the relevant backend test commands and, from `docs-site`, run:

```bash
npm run types:check
npm run lint
npm run build
```

- [ ] **Step 3: Review and commit**

Inspect `git diff --check`, the scoped diff, and `git status`, then create a
single bug-fix commit on `pre`.

- [ ] **Step 4: Push and verify PRE**

Push `pre`, inspect the deployment workflow, confirm the PRE health endpoint,
and verify the deployed backend image contains the fix before reporting
completion.
