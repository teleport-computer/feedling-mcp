# Runtime V2 Plaintext Generated Image Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Runtime V2 save provider-generated images as native Chat image messages when effective content encryption is `off`, without changing encrypted delivery.

**Architecture:** Add explicit text-versus-binary intent to the central shared-envelope builder. Runtime V2's generated-image effect builder declares binary intent; the plaintext branch emits the repository's existing `body_b64` shape, while the encrypted branch remains byte-for-byte on the existing envelope path.

**Tech Stack:** Python 3.11, pytest, PostgreSQL-backed Runtime V2 tests, Pillow-backed generated-image normalization.

## Global Constraints

- Scope is limited to Runtime V2 generated-image delivery.
- Existing callers default to text behavior.
- Effective encryption `on` keeps the existing ciphertext envelope shape.
- Effective encryption `off` binary output uses strict base64 `body_b64` plus exact `body_size_bytes`.
- The call site declares binary intent; invalid UTF-8 text must still fail.
- No iOS, screen sharing, perception-photo, ordinary-file, routing, or stored-row migration changes.

---

### Task 1: Add explicit binary intent to the shared-envelope builder

**Files:**
- Modify: `backend/core/envelope.py:346-380`
- Modify: `tests/test_write_side_format_routing.py:1-170`

**Interfaces:**
- Produces: `_build_shared_envelope_for_store(store, plaintext: bytes, *, item_id: str | None = None, content_kind: str = "text") -> tuple[dict | None, str]`
- Supported `content_kind` values: `text`, `binary`
- Effective-off binary result: `body_b64`, `body_size_bytes`, `id`, `owner_user_id`, `visibility`

- [ ] **Step 1: Write failing plaintext-binary and validation tests**

Add `import base64` and these tests to `tests/test_write_side_format_routing.py`:

```python
def test_plaintext_tier_binary_body_uses_body_b64(store, monkeypatch):
    _prefer(monkeypatch, "off")
    raw = b"\xff\x00generated-image"

    out, err = core_envelope._build_shared_envelope_for_store(
        store,
        raw,
        item_id="generated-image-id",
        content_kind="binary",
    )

    assert err == "", err
    assert out is not None
    assert set(out) == {
        "body_b64",
        "body_size_bytes",
        "id",
        "owner_user_id",
        "visibility",
    }
    assert base64.b64decode(out["body_b64"], validate=True) == raw
    assert out["body_size_bytes"] == len(raw)
    assert out["id"] == "generated-image-id"
    assert out["owner_user_id"] == store.user_id
    assert out["visibility"] == "shared"


def test_shared_envelope_rejects_unknown_content_kind(store, monkeypatch):
    _prefer(monkeypatch, "off")

    with pytest.raises(ValueError, match="invalid content_kind"):
        core_envelope._build_shared_envelope_for_store(
            store,
            b"payload",
            content_kind="guess",
        )
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_write_side_format_routing.py::test_plaintext_tier_binary_body_uses_body_b64 \
  tests/test_write_side_format_routing.py::test_shared_envelope_rejects_unknown_content_kind -q
```

Expected: both fail because `_build_shared_envelope_for_store` does not accept `content_kind`.

- [ ] **Step 3: Implement the minimal builder change**

In `backend/core/envelope.py`, import `base64` if it is not already imported and change the builder to validate intent before resolving the account mode:

```python
def _build_shared_envelope_for_store(
    store,
    plaintext: bytes,
    *,
    item_id: str | None = None,
    content_kind: str = "text",
) -> tuple[dict | None, str]:
    if content_kind not in {"text", "binary"}:
        raise ValueError(f"invalid content_kind: {content_kind!r}")
    if resolve_content_encryption(store.user_id) == "off":
        if content_kind == "binary":
            return {
                "body_b64": base64.b64encode(plaintext).decode("ascii"),
                "body_size_bytes": len(plaintext),
                "id": item_id or content_encryption.random_item_id(),
                "owner_user_id": store.user_id,
                "visibility": "shared",
            }, ""
        try:
            body = plaintext.decode("utf-8")
        except UnicodeDecodeError:
            return None, "plaintext_body_not_utf8"
        return {
            "body": body,
            "id": item_id or content_encryption.random_item_id(),
            "owner_user_id": store.user_id,
            "visibility": "shared",
        }, ""
```

Leave the encrypted branch below this block unchanged.

- [ ] **Step 4: Add and run encrypted-binary compatibility coverage**

Add:

```python
def test_encrypted_tier_binary_intent_keeps_ciphertext_shape(store, monkeypatch):
    _prefer(monkeypatch, "on")
    _wire_assembly(monkeypatch)

    out, err = core_envelope._build_shared_envelope_for_store(
        store,
        b"\xff\x00generated-image",
        content_kind="binary",
    )

    assert err == "", err
    assert out is not None
    assert "body_ct" in out and "K_user" in out and "K_enclave" in out
    assert "body" not in out and "body_b64" not in out
```

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest tests/test_write_side_format_routing.py -q
```

Expected: all tests in the file pass, including the existing invalid UTF-8 text regression.

---

### Task 2: Declare binary intent for Runtime V2 generated images

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py:4929-4934,5256-5278`
- Modify: `tests/test_v2_downloadable_files.py`

**Interfaces:**
- Consumes: `_build_shared_envelope_for_store(..., content_kind="binary")` from Task 1
- Produces: `_build_encrypted_image_reply_effect_payload(...)` payloads whose envelope is ciphertext for effective-on accounts and `body_b64` for effective-off accounts

- [ ] **Step 1: Write the failing Runtime V2 effect test**

Add this focused test to `tests/test_v2_downloadable_files.py`:

```python
def test_generated_image_effect_uses_plaintext_binary_envelope_when_off(monkeypatch):
    class Store:
        user_id = "u_v2_plaintext_generated_image"

    monkeypatch.setattr(
        worker.core_envelope,
        "resolve_content_encryption",
        lambda _user_id: "off",
    )
    raw = b"\x89PNG\r\n\x1a\n\x00binary-image"

    payload = worker._build_encrypted_image_reply_effect_payload(
        Store(),
        worker.GeneratedImageReply(
            name="result.png",
            mime_type="image/png",
            data=raw,
        ),
        effect_id="generated-image-effect",
    )

    envelope = payload["envelope"]
    assert base64.b64decode(envelope["body_b64"], validate=True) == raw
    assert envelope["body_size_bytes"] == len(raw)
    assert "body" not in envelope and "body_ct" not in envelope
    assert payload["message_extra"] == {
        "content_type": "image",
        "image_mime": "image/png",
        "image_byte_count": len(raw),
    }
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_v2_downloadable_files.py::test_generated_image_effect_uses_plaintext_binary_envelope_when_off -q
```

Expected: FAIL with `RuntimeError: plaintext_body_not_utf8` because the image effect builder still requests the default text shape.

- [ ] **Step 3: Pass binary intent from the generated-image effect builder**

Change the envelope call in `backend/model_api_runtime/v2/worker.py` to:

```python
envelope, error = core_envelope._build_shared_envelope_for_store(
    store,
    bytes(image_reply.data),
    item_id=item_id,
    content_kind="binary",
)
```

Update the `GeneratedImageReply` and helper docstrings so they describe a shared/native Chat image rather than claiming the payload is always encrypted.

- [ ] **Step 4: Update the existing test fake's signature**

In `test_process_job_commits_single_generated_image_without_empty_followups`, change the local fake to accept and assert the new intent:

```python
def build_envelope(
    store,
    plaintext,
    *,
    item_id=None,
    content_kind="text",
):
    assert content_kind == "binary"
    return (
        {
            "v": 1,
            "id": item_id,
            "owner_user_id": store.user_id,
            "body_ct": base64.b64encode(plaintext).decode("ascii"),
            "nonce": "n",
            "K_user": "k",
        },
        "",
    )
```

- [ ] **Step 5: Run focused Runtime V2 image tests and verify GREEN**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_v2_downloadable_files.py::test_generated_image_effect_uses_plaintext_binary_envelope_when_off \
  tests/test_v2_downloadable_files.py::test_process_job_commits_single_generated_image_without_empty_followups \
  tests/test_v2_downloadable_files.py::test_reply_payload_sequence_accepts_image_primary_and_image_followups -q
```

Expected: all selected tests pass.

---

### Task 3: Regression verification and commit

**Files:**
- Verify: `backend/core/envelope.py`
- Verify: `backend/model_api_runtime/v2/worker.py`
- Verify: `tests/test_write_side_format_routing.py`
- Verify: `tests/test_v2_downloadable_files.py`

**Interfaces:**
- Consumes the completed Task 1 and Task 2 behavior.
- Produces one reviewed local `pre` commit containing the runtime fix and regressions.

- [ ] **Step 1: Run the complete focused suites**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_write_side_format_routing.py \
  tests/test_v2_downloadable_files.py \
  tests/test_generated_image.py -q
```

Expected: zero failures and no skipped database module caused by a missing PostgreSQL connection.

- [ ] **Step 2: Run adjacent contract coverage**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  .venv-test/bin/python -m pytest \
  tests/test_uploaded_envelope_gate.py \
  tests/test_envelope_storage_fields.py \
  tests/test_read_side_shape_routing.py -q
```

Expected: zero failures.

- [ ] **Step 3: Inspect the final change**

```bash
git diff --check
git diff -- backend/core/envelope.py backend/model_api_runtime/v2/worker.py \
  tests/test_write_side_format_routing.py tests/test_v2_downloadable_files.py
git status --short --branch
```

Expected: no whitespace errors; only the scoped runtime and test files are modified.

- [ ] **Step 4: Commit the implementation**

```bash
git add backend/core/envelope.py backend/model_api_runtime/v2/worker.py \
  tests/test_write_side_format_routing.py tests/test_v2_downloadable_files.py
git commit -m "fix(v2): publish plaintext generated images"
```

- [ ] **Step 5: Report deployment boundary**

Report the commit id and test evidence. Do not push or deploy unless the user explicitly requests it; note that local `pre` is already divergent from `origin/pre` and must be reconciled before any push.
