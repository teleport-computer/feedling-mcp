# Plaintext Binary Media Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make hosted chat images/files, iOS screen sharing, and iOS perception photos work when the server reports effective content encryption `off`, without changing the encrypted verification chain.

**Architecture:** The backend shared-envelope builder receives an explicit binary/text storage intent and emits the existing `body_b64` plaintext shape only for effective-off users. iOS mirrors the server-authoritative effective mode into its App Group and uses small shape builders to choose encrypted envelopes or authenticated plaintext binary envelopes; unknown state remains fail-closed. The photo endpoint and screen ingest reuse the backend's central uploaded-envelope gate.

**Tech Stack:** Python 3.11, FastAPI, pytest, PostgreSQL; Swift, CryptoKit, XCTest/Xcode; MDX documentation and OpenAPI generation.

## Global Constraints

- Encrypted mode must retain the existing `body_ct`, `nonce`, `K_user`, `K_enclave`, attestation, pinning, and measurement behavior.
- Plaintext writes require both `FEEDLING_PLAINTEXT_WRITES_ACCEPTED=1` and effective user preference `off`.
- Unknown or stale client mode must never emit plaintext.
- Plaintext envelopes require authenticated owner equality and `visibility=shared`; `local_only` is invalid.
- Existing rows are not migrated.
- Validation must not inspect or replay the reported user's private content.

---

### Task 1: Backend binary shared-envelope construction

**Files:**
- Modify: `backend/core/envelope.py`
- Modify: `backend/hosted/chat_send_core.py`
- Test: `tests/test_write_side_format_routing.py`
- Test: `tests/test_asgi_hosted_chat_send.py`

**Interfaces:**
- Produces: `_build_shared_envelope_for_store(store, plaintext, *, item_id=None, content_kind="text") -> tuple[dict | None, str]`, where `content_kind` is `"text"` or `"binary"`.
- Binary effective-off output contains `body_b64` and `body_size_bytes`; encrypted output is unchanged.

- [ ] Add a unit test asserting effective-off binary bytes `b"\xff\x00pdf"` produce strict base64, exact decoded size, owner, and shared visibility, while the default text call still returns `plaintext_body_not_utf8`.
- [ ] Run `DATABASE_URL=... FEEDLING_TEST_PG=... .venv-test/bin/python -m pytest tests/test_write_side_format_routing.py -q` and confirm the new binary test fails because `content_kind` is unsupported.
- [ ] Add hosted-route tests for effective-off image and PDF requests that assert 202/accepted persistence, `body_b64`, absence of crypto fields, and a normal queued chat job; keep an effective-on assertion for crypto fields.
- [ ] Run the hosted tests and confirm the effective-off binary cases fail with 409 `plaintext_body_not_utf8`.
- [ ] Implement explicit content-kind validation and binary plaintext construction in `core/envelope.py`; pass `content_kind="binary"` only for image/file bodies in both hosted V1 and V2 paths. Captions and text keep the default.
- [ ] Run the two focused test files and confirm green.
- [ ] Commit as `fix(chat): store plaintext hosted binary media`.

### Task 2: Backend plaintext perception-photo contract

**Files:**
- Modify: `backend/perception/perception_read_core.py`
- Modify: `backend/perception/service.py` only if persistence currently strips binary fields
- Test: `tests/test_asgi_perception.py`
- Test: `tests/test_ios_perception_contract_v2.py`

**Interfaces:**
- Consumes: `core.envelope.validate_uploaded_envelope(envelope, user_id=...)` and existing image binary size validation.
- Produces: `/v1/perception/photo/evaluate` accepts the existing `content_envelope` key with either encrypted v1 fields or valid effective-off `body_b64` fields.

- [ ] Add API tests proving an effective-off owned/shared `body_b64` photo is stored and retrievable as an opaque envelope.
- [ ] Add rejection tests for effective-on plaintext, wrong owner, `local_only`, invalid base64, and decoded size overflow; assert no photo row is created.
- [ ] Run the focused perception tests and confirm failures occur at the current ciphertext-only contract.
- [ ] Route photo content envelopes through the central upload validator before `service.evaluate_photo`; preserve the accepted envelope verbatim and keep encrypted behavior unchanged.
- [ ] Run the focused perception tests and confirm green.
- [ ] Commit as `fix(perception): accept gated plaintext photo envelopes`.

### Task 3: iOS effective-mode mirror and capture envelope builder

**Files:**
- Modify: `App/FeedlingTest/API/FeedlingAPI.swift`
- Modify: `App/FeedlingBroadcast/FrameEnvelope.swift`
- Modify: `App/FeedlingBroadcast/SampleHandler+WebSocketQueue.swift`
- Test: create or modify the focused XCTest files under `Tests/` and update `App/FeedlingTest.xcodeproj/project.pbxproj` only if target membership requires it

**Interfaces:**
- Produces App Group keys `feedling.contentEncryptionEffective` and `feedling.contentEncryptionEffectiveUpdatedAt`.
- Produces `FrameEnvelope.Mode` with fail-closed parsing and a wrapper that emits encrypted v1 or plaintext binary envelope dictionaries.

- [ ] Add XCTest coverage for `off`, `on`, unknown, and stale effective-mode parsing; unknown/stale must resolve to encrypted/fail-closed behavior.
- [ ] Add wrapper tests asserting `off` emits `body_b64/body_size_bytes` with no crypto fields and `on` emits the existing crypto fields.
- [ ] Run the focused XCTest target and confirm failures because mode-aware wrapping does not exist.
- [ ] Mirror the server's effective mode and timestamp whenever account/enclave state is published; clear it on account reset.
- [ ] Refactor `FrameEnvelope` to build a plaintext context from user id only for explicit fresh `off`, while the encrypted context retains all current verified-attestation checks.
- [ ] Update the WebSocket queue to re-read mode for each frame, emit the selected shape, and retain transition-latched diagnostic logs.
- [ ] Run focused XCTest and confirm green.
- [ ] Commit as `fix(broadcast): support effective plaintext frames`.

### Task 4: iOS mode-aware perception-photo requests

**Files:**
- Modify: `App/FeedlingTest/API/FeedlingAPI.swift`
- Modify: `App/FeedlingTest/Pages/Settings/Perception/PerceptionPermissionsManager.swift`
- Test: focused XCTest files under `Tests/`

**Interfaces:**
- Produces one request builder accepting `metadata`, optional encrypted meta envelope, raw JPEG bytes, and effective mode.
- Effective-off output keeps `content_envelope` as the shared binary plaintext envelope and omits sensitive `meta_envelope`; effective-on output matches today's JSON.

- [ ] Add request-builder tests for effective-on and effective-off payloads, plus unknown-mode fail-closed behavior.
- [ ] Assert the plaintext payload contains no crypto keys and no derived sensitive `meta_envelope`.
- [ ] Run focused XCTest and confirm failure because only encrypted request construction exists.
- [ ] Implement the pure request builder, use it from the API method, and make the photo pipeline re-read effective mode for each asset.
- [ ] Preserve current key renewal/envelope construction only in effective-on mode; explicit fresh `off` must not require enclave keys.
- [ ] Run focused XCTest and confirm green.
- [ ] Commit as `fix(perception): support effective plaintext photos`.

### Task 5: Documentation, full verification, and pre handoff

**Files:**
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: the relevant self-hosting trust page under `docs-site/content/docs/`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: OpenAPI source/override only if the generated schema does not already model both envelope shapes
- Regenerate: `docs-site/openapi/public.json` when its source changes

**Interfaces:**
- Documents the dual encrypted/plaintext binary shapes and server-authoritative effective-mode gate.

- [ ] Update architecture, trust-boundary text, and `Unreleased` changelog without claiming plaintext has enclave confidentiality.
- [ ] Run backend focused suites for hosted chat, envelope routing, screen ingest, uploaded-envelope gates, and perception.
- [ ] Run the repository's required OpenAPI contract tests.
- [ ] From `docs-site`, run `npm run openapi:generate` if needed, then `npm run types:check`, `npm run lint`, and `npm run build`.
- [ ] Run the iOS unit tests and a clean simulator build for the app plus broadcast extension.
- [ ] Inspect `git diff --check`, the complete diffs, and branch status in both worktrees.
- [ ] Commit documentation as `docs: describe plaintext binary capture paths`.
- [ ] Prepare PRs from feature branches to their allowed targets; do not push or deploy pre until explicitly requested.
