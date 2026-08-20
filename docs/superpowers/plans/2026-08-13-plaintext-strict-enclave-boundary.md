# Plaintext Strict Enclave Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every server-owned plaintext content workflow succeed without sending `body` or `body_b64` to enclave decrypt endpoints, while preserving sealed-row and mixed-history behavior.

**Architecture:** Centralize persisted-envelope classification, local plaintext decoding, and authoritative-shape hashing in `core.envelope`. Convert single-row callers to that contract, then partition batch readside work so plaintext is processed locally and only sealed projections cross the enclave boundary; merge results using the original row order and existing public semantics.

**Tech Stack:** Python 3.11, FastAPI/Starlette, httpx, PostgreSQL/psycopg, pytest, Phala CVM, GitHub Actions.

## Global Constraints

- A non-empty `body_ct` is authoritative when multiple content fields coexist.
- `body` and `body_b64` must never trigger enclave HTTP or crypto calls in normal backend/hosted-runtime workflows.
- Routing is per row, not per account, because encrypted and plaintext history can coexist.
- Unknown shapes, wrong owners, malformed base64, and malformed plaintext JSON fail explicitly.
- Public response schemas, encrypted-tier behavior, authorization, ordering, cursor semantics, and resource caps remain stable.
- No logs, traces, or errors may contain plaintext content.
- Existing unrelated worktree changes must not enter these commits.

---

## File Structure

- `backend/core/envelope.py`: authoritative envelope classification, local plaintext reader, CAS token.
- `backend/genesis/persona_backfill.py`, `backend/genesis/worker.py`, `backend/genesis/plaintext.py`: Genesis shape routing.
- `backend/agent_runtime/spawners.py`, `backend/agent_runtime/supervisor.py`: hosted persona and Identity routing.
- `backend/hosted/history_import.py`, `backend/hosted/turn.py`: plaintext Identity/history use.
- `backend/voice/cleanup.py`: local plaintext transcript reconstruction.
- `backend/model_api_runtime/v2/worker.py`: shape-neutral reasoning sub-envelope flattening.
- `backend/memory/migration.py`, `backend/memory/actions.py`, `backend/memory_readside_core.py`: plaintext-safe CAS and local readside.
- `backend/model_api_runtime/v2/history_readside.py`, `backend/enclave/routes/history.py`: split History scan/leaf/fetch processing and stable internal results.
- `backend/worldbook/worldbook_core.py`, `backend/worldbook_readside_core.py`, `backend/enclave/routes/worldbook.py`: split World Book processing and mergeable projections.
- `backend/capabilities/chat.py`, `backend/identity/actions.py`: shape-aware attachment and Identity capability reads.
- Adjacent `tests/test_*.py`: focused regression tests; do not create a second implementation-only test framework.
- `docs-site/content/docs/**`, `docs-site/content/docs/changelog.mdx`: trust-boundary and release documentation.

---

### Task 1: Shared persisted-envelope primitives

**Files:**
- Modify: `backend/core/envelope.py`
- Test: `tests/test_read_side_shape_routing.py`
- Test: `tests/test_envelope_storage_fields.py`

**Interfaces:**
- Produces: `classify_envelope_shape(envelope: object) -> str` returning `sealed`, `plaintext_text`, `plaintext_binary`, or `invalid`.
- Produces: `read_plaintext_envelope_body(envelope: dict, *, owner_user_id: str = "") -> bytes` that rejects sealed/invalid/wrong-owner rows.
- Produces: `envelope_content_token(envelope: dict) -> str`, a SHA-256 token over the authoritative shape and stored body value.
- Updates: `read_envelope_body(...)` delegates plaintext branches to `read_plaintext_envelope_body` and preserves the sealed enclave call byte-for-byte.

- [ ] **Step 1: Write classification and local-read failing tests**

```python
def test_plaintext_local_reader_never_calls_enclave(spy):
    env = {"body": "hello", "owner_user_id": "u1", "visibility": "shared"}
    assert core_envelope.classify_envelope_shape(env) == "plaintext_text"
    assert core_envelope.read_plaintext_envelope_body(env, owner_user_id="u1") == b"hello"
    assert spy == []

def test_ciphertext_wins_over_stale_plaintext():
    env = {"body_ct": "Y3Q=", "body": "stale"}
    assert core_envelope.classify_envelope_shape(env) == "sealed"
    with pytest.raises(ValueError, match="plaintext_envelope_required"):
        core_envelope.read_plaintext_envelope_body(env)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres ../../.venv-test/bin/python -m pytest tests/test_read_side_shape_routing.py tests/test_envelope_storage_fields.py -q`

Expected: new tests fail because the three public helpers do not exist.

- [ ] **Step 3: Implement minimal shape helpers**

```python
def classify_envelope_shape(envelope) -> str:
    if not isinstance(envelope, dict):
        return "invalid"
    if envelope.get("body_ct"):
        return "sealed"
    if envelope.get("body_b64") is not None:
        return "plaintext_binary"
    if isinstance(envelope.get("body"), str):
        return "plaintext_text"
    return "invalid"

def envelope_content_token(envelope: dict) -> str:
    shape = classify_envelope_shape(envelope)
    if shape == "invalid":
        raise ValueError("envelope_shape_unrecognized")
    field = {"sealed": "body_ct", "plaintext_binary": "body_b64", "plaintext_text": "body"}[shape]
    return hashlib.sha256(f"{shape}\0{envelope[field]}".encode("utf-8")).hexdigest()
```

Implement owner validation and strict base64 decoding in the local reader; make `read_envelope_body` call it only for non-sealed shapes.

- [ ] **Step 4: Run focused tests and verify GREEN**

- [ ] **Step 5: Commit**

Run: `git add backend/core/envelope.py tests/test_read_side_shape_routing.py tests/test_envelope_storage_fields.py && git commit -m 'fix(envelope): centralize plaintext shape routing'`

---

### Task 2: Fix direct single-row plaintext failures

**Files:**
- Modify: `backend/genesis/persona_backfill.py`
- Modify: `backend/genesis/worker.py`
- Modify: `backend/genesis/plaintext.py`
- Modify: `backend/agent_runtime/spawners.py`
- Modify: `backend/hosted/history_import.py`
- Modify: `backend/voice/cleanup.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `backend/memory/migration.py`
- Modify: `backend/memory/actions.py`
- Test: `tests/test_genesis_worker.py`
- Test: `tests/test_genesis_plaintext_routes.py`
- Test: `tests/test_agent_runtime_spawners.py`
- Test: `tests/test_history_import_identity.py`
- Test: `tests/test_voice_cleanup.py`
- Test: `tests/test_chat_resident_consumer.py`
- Test: `tests/test_memory_migration.py`

**Interfaces:**
- Consumes: Task 1 helpers.
- Produces: no new public API; removes sealed-only preconditions and hard-coded field extraction.

- [ ] **Step 1: Add one failing regression per reproduced bug**

Tests must assert real outputs, with enclave spies used only to prove a forbidden side effect:

```python
def test_persona_from_plaintext_blob_loads_without_decrypt():
    blob = {"content_envelope": {"body": "You are Kai."}}
    assert spawners._persona_from_blob(blob, forbidden_decrypt) == "You are Kai."

def test_voice_reconstruction_reads_plaintext_chat_row(monkeypatch):
    # seed one body row and make enclave decrypt raise AssertionError
    assert cleanup.transcript_turns_from_rows("u", "call") == [
        {"role": "user", "text": "hello"}
    ]

def test_plaintext_memory_cas_token_changes_with_body():
    assert migration.body_hash({"body": "a"}) != migration.body_hash({"body": "b"})
```

Also cover persona backfill chunk bytes, Genesis worker persona/voice, plaintext Identity merge, History Import preferred name, and V2 `thinking_body` persistence.

- [ ] **Step 2: Run all named focused tests and verify RED for the expected reasons**

- [ ] **Step 3: Implement minimal routing changes**

- Persona backfill obtains bytes through `read_envelope_body`/local reader instead of indexing `body_ct`.
- Genesis worker `_decrypt_blob_text` routes `body` locally and calls `_decrypt_envelope` only for sealed rows.
- `_persona_from_blob` accepts any recognized content envelope and delegates to its reader.
- Remove `not blob.get("body_ct")` guards before shape-aware readers.
- Voice cleanup calls `read_envelope_body` with the runtime token; plaintext does not reach enclave.
- V2 `_thinking_extra` delegates to `core_envelope.envelope_prefixed_fields` like `chat.service`.
- Both memory CAS functions delegate to `envelope_content_token`.

- [ ] **Step 4: Run focused tests and verify GREEN**

- [ ] **Step 5: Run adjacent Genesis/Voice/Identity/V2 suites**

- [ ] **Step 6: Commit**

Run: `git add backend tests && git commit -m 'fix(plaintext): remove sealed-only single-row assumptions'`

---

### Task 3: Partition Memory readside locally

**Files:**
- Modify: `backend/memory_readside_core.py`
- Modify: `backend/enclave/readside.py` only if a pure builder must be shared without importing route code
- Test: `tests/test_memory_readside_core.py`
- Test: `tests/test_memory_readside_sandbox.py`

**Interfaces:**
- Produces: `_partition_memory_candidates(candidates, owner_user_id) -> (plaintext_items, sealed_candidates, invalid_ids)`.
- Keeps: `memory_index_core` and `memory_fetch_core` public return schemas.

- [ ] **Step 1: Write failing all-plaintext and mixed-shape tests**

```python
def test_plaintext_memory_index_never_posts_enclave(store):
    store.moments = [plaintext_memory("m1", "needle")]
    result = memory_index_core(store, None, {"query": "needle"}, post_enclave=forbidden)
    assert [item["id"] for item in result["items"]] == ["m1"]

def test_mixed_memory_fetch_posts_only_ciphertext(store):
    seen = []
    result = memory_fetch_core(store, None, {"ids": ["p", "c"]}, post_enclave=capture(seen))
    assert [row["id"] for row in seen[0]] == ["c"]
    assert [row["id"] for row in result["items"]] == ["p", "c"]
```

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Implement local plaintext parsing and deterministic merge**

Reuse the existing pure memory item builders/filter functions. Parse only the
recognized plaintext body, apply query/bucket/thread/sensitive filters locally,
post sealed candidates only, and reorder merged items by candidate/request rank.
Do not mint or require a token when the sealed partition is empty.

- [ ] **Step 4: Verify GREEN and run all Memory readside tests**

- [ ] **Step 5: Commit**

Run: `git add backend/memory_readside_core.py backend/enclave/readside.py tests/test_memory_readside_core.py tests/test_memory_readside_sandbox.py && git commit -m 'fix(memory): keep plaintext readside outside enclave'`

---

### Task 4: Partition History Search/Fetch and leaf hints

**Files:**
- Modify: `backend/model_api_runtime/v2/history_readside.py`
- Modify: `backend/enclave/routes/history.py`
- Modify: `backend/model_api_runtime/v2/history_search.py` if pure match helpers belong there
- Test: `tests/test_v2_history_readside.py`
- Test: `tests/test_v2_history_search.py`
- Test: `tests/test_enclave_history_routes.py`

**Interfaces:**
- Produces: a pure local plaintext scan function returning the enclave scan response keys: `matches`, `checked_count`, `last_checked_seq`, `truncated`.
- Internal enclave responses continue carrying row `seq`; no public schema changes.

- [ ] **Step 1: Write failing plaintext projection/search/fetch/leaf tests**

Cover:

- all-plaintext search finds a query with zero `post` calls;
- all-plaintext fetch returns anchor and neighbors with zero calls;
- plaintext summary leaf hint prioritizes its exact segment locally;
- mixed search sends no `body`/`body_b64` to `post`, preserves descending sequence order, and advances the cursor through the proven checked prefix;
- encrypted sub-scan failure does not advance a mixed batch unsafely.

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Extract/share pure normalization, match, and snippet helpers**

Keep the enclave sealed decrypt function unchanged. Local scan must use the same
normalization and snippet cap constants, not a second approximate implementation.

- [ ] **Step 4: Implement per-batch partition and safe merge**

For each bounded original batch:

1. classify rows and retain their sequence rank;
2. locally scan plaintext rows;
3. project and post sealed rows only;
4. merge matches by original sequence rank;
5. calculate the greatest contiguous checked prefix from local completion plus
   the encrypted response's `last_checked_seq`/`checked_count`;
6. preserve the existing limit, deadline, byte cap, and cursor signing behavior.

- [ ] **Step 5: Verify GREEN and run History suites**

- [ ] **Step 6: Commit**

Run: `git add backend/model_api_runtime/v2/history_readside.py backend/model_api_runtime/v2/history_search.py backend/enclave/routes/history.py tests/test_v2_history_readside.py tests/test_v2_history_search.py tests/test_enclave_history_routes.py && git commit -m 'fix(history): scan plaintext outside enclave'`

---

### Task 5: Partition World Book validation and matching

**Files:**
- Modify: `backend/worldbook/worldbook_core.py`
- Modify: `backend/worldbook_readside_core.py`
- Modify: `backend/enclave/routes/worldbook.py`
- Test: `tests/test_worldbook_core.py`
- Test: `tests/test_worldbook_readside.py`
- Test: `tests/test_enclave_worldbook_routes.py`

**Interfaces:**
- Produces: mergeable internal matched-entry projections containing only the text already included in the existing aggregate block plus id/name/order metadata.
- Keeps: public `block`, `matched_names`, `rejected_over_cap`, and `unavailable_ids` fields.

- [ ] **Step 1: Write failing plaintext upsert/match and mixed-order tests**

Assert plaintext upsert enforces `WORLD_BOOK_CONTENT_CAP` locally without a
network call; all-plaintext match performs zero calls; mixed match posts only
sealed entries and returns canonical ordering.

- [ ] **Step 2: Verify RED**

- [ ] **Step 3: Implement local validation/matching and mergeable encrypted projection**

Do not return arbitrary decrypted envelope data. The internal projection may
carry only the already-rendered per-entry contribution and stable ordering
metadata needed to reconstruct the existing block.

- [ ] **Step 4: Verify GREEN and run World Book suites**

- [ ] **Step 5: Commit**

Run: `git add backend/worldbook backend/enclave/routes/worldbook.py tests/test_worldbook_core.py tests/test_worldbook_readside.py tests/test_enclave_worldbook_routes.py && git commit -m 'fix(worldbook): keep plaintext matching outside enclave'`

---

### Task 6: Route hosted Chat, attachment, and Identity reads by shape

**Files:**
- Modify: `backend/identity/actions.py`
- Modify: `backend/agent_runtime/supervisor.py`
- Modify: `backend/capabilities/chat.py`
- Modify: `backend/hosted/turn.py`
- Modify: `tools/chat_resident_consumer.py`; do not alter unrelated polling state
- Test: `tests/test_identity_actions.py`
- Test: `tests/test_agent_runtime_supervisor.py`
- Test: `tests/test_capabilities_chat.py`
- Test: `tests/test_hosted_turn.py`
- Test: `tests/test_chat_resident_consumer.py`
- Test: `tests/test_chat_resident_consumer_image.py`

**Interfaces:**
- Produces: one internal shape-aware history/body reader that fetches raw authenticated backend rows, decodes plaintext locally, and calls enclave decrypt only for sealed main/caption bodies.
- Keeps: existing capability return values and resident cursor/decrypt-health behavior for encrypted rows.

- [ ] **Step 1: Finish the call graph and pin every unconditional decrypt-and-serve caller in a test**

The test inventory must include `_identity_plain_for_action`, supervisor intro
Identity, hosted recap history, chat image/file reads, and resident history. Each
plaintext fixture installs a forbidden enclave HTTP spy.

- [ ] **Step 2: Run tests and verify RED**

- [ ] **Step 3: Implement raw-fetch then per-row routing**

- Fetch raw documents only through authenticated backend endpoints/store APIs.
- Validate owner id before local decode.
- Use single-envelope enclave decrypt only for `sealed` rows.
- Reconstruct attachment bytes, captions, ordered history, context, and existing
  error categories without sending plaintext fields to enclave.
- Keep encrypted decrypt-health reporting; plaintext-only success does not
  require enclave health.

- [ ] **Step 4: Verify GREEN and run hosted Chat/Identity suites**

- [ ] **Step 5: Commit**

Run: `git add backend/identity/actions.py backend/agent_runtime/supervisor.py backend/capabilities/chat.py backend/hosted/turn.py backend/chat tests && git commit -m 'fix(hosted): bypass enclave for plaintext reads'`

---

### Task 7: Add a repository-wide plaintext boundary regression gate

**Files:**
- Create: `tests/test_plaintext_enclave_boundary.py`
- Modify: production files only if the gate reveals an unhandled real caller, following a new red-green cycle

**Interfaces:**
- Produces: a parameterized test inventory of every server-owned content reader and its plaintext no-enclave invariant.

- [ ] **Step 1: Write the boundary inventory test**

The parameter table names each workflow, invokes its public/internal seam with a
valid plaintext row, asserts the expected content result, and uses one shared
spy that fails on `_decrypt_envelope_via_enclave`, `/v1/envelope/decrypt`, and
batch enclave HTTP transports.

- [ ] **Step 2: Run the gate and verify any failure is a real uncovered caller**

- [ ] **Step 3: Fix uncovered callers one at a time with dedicated adjacent tests**

- [ ] **Step 4: Run the gate and the complete plaintext selection**

Run: `FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres ../../.venv-test/bin/python -m pytest tests -q -k plaintext --ignore=tests/test_api.py`

- [ ] **Step 5: Commit**

Run: `git add backend tests/test_plaintext_enclave_boundary.py tests && git commit -m 'test(plaintext): enforce strict enclave boundary'`

---

### Task 8: Public trust-boundary documentation

**Files:**
- Modify: relevant architecture, workflow, and self-hosting trust pages under `docs-site/content/docs/`
- Modify: `docs-site/content/docs/changelog.mdx`
- Modify: `docs-site/openapi/public.json` only if endpoint schemas changed

**Interfaces:**
- Documents: plaintext rows are processed locally; only sealed rows cross decrypt endpoints; mixed histories route per row.

- [ ] **Step 1: Locate and update every affected trust-boundary statement**

Run: `rg -n 'plaintext|enclave|decrypt|content encryption' docs-site/content/docs`

- [ ] **Step 2: Add an `Unreleased` changelog entry**

- [ ] **Step 3: Run documentation verification**

Run: `cd docs-site && npm run types:check && npm run lint && npm run build`

- [ ] **Step 4: Run OpenAPI contract tests; regenerate only if required**

Run: `../../.venv-test/bin/python -m pytest tests/openapi -q`

- [ ] **Step 5: Commit**

Run: `git add docs-site && git commit -m 'docs: clarify plaintext enclave boundary'`

---

### Task 9: Full verification, review, and PRE deployment

**Files:**
- No planned production edits; any review finding returns to the owning task's TDD cycle.

**Interfaces:**
- Produces: verified commits on `pre`, CI/deploy evidence, and PRE functional evidence.

- [ ] **Step 1: Run focused subsystem suites from Tasks 1-8**

- [ ] **Step 2: Run pyflakes on every changed backend package**

Run: `../../.venv-test/bin/python -m pyflakes backend/core backend/genesis backend/agent_runtime backend/hosted backend/voice backend/memory backend/model_api_runtime/v2 backend/worldbook backend/capabilities backend/identity`

- [ ] **Step 3: Run the full backend suite against local Postgres**

Run: `FEEDLING_TEST_PG=postgresql://postgres:test@127.0.0.1:55432/postgres ../../.venv-test/bin/python -m pytest tests -q --ignore=tests/test_api.py`

Record exact passed/skipped/xfailed/failure counts. Investigate every new failure;
do not classify it as baseline without reproducing it on `origin/pre`.

- [ ] **Step 4: Perform a manual diff review**

Check requirements line by line, `git diff origin/pre...HEAD --check`, plaintext
leakage in logs/errors, encrypted-path preservation, and unrelated changes. This
session cannot dispatch subagents, so record the manual review explicitly and do
not claim an independent review.

- [ ] **Step 5: Push only verified task commits to `pre`**

Fetch current `origin/pre`; if it moved, rebase/merge in the isolated branch,
rerun affected tests, then push the task commits without force.

- [ ] **Step 6: Monitor image publication and PRE deployment workflows**

Use `gh run list/view` until build, deploy, attestation, and canary are terminal.

- [ ] **Step 7: Verify PRE runtime**

- `/healthz` release commit matches the deployed commit.
- backend/enclave/worker images use the expected tag.
- plaintext Genesis memory and chat imports finish `done`.
- Memory read, History search/fetch, World Book match, Identity, hosted persona,
  voice reconstruction, and reasoning fixtures succeed.
- logs contain no plaintext-purpose `/v1/envelope/decrypt`, `missing body_ct`, or
  new stable error.

- [ ] **Step 8: Report exact verification evidence and remaining limitations**
