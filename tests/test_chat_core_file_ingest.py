"""VPS user ingest (chat_core.write_message) accepts content_type=file.

spec: .superpowers/sdd/task-4-brief.md
Run:  python -m pytest tests/test_chat_core_file_ingest.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi_test_client import make_client  # noqa: E402
from core import store as core_store  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from chat import chat_core  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _env(user_id: str, marker: str) -> dict:
    return {
        "v": 1, "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x00" * 12), "K_user": _b64(b"\x01" * 32),
        "K_enclave": _b64(b"\x02" * 32),
        "visibility": "shared", "owner_user_id": user_id,
    }


@pytest.fixture()
def store(backend_env):
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return core_store.get_store(res.get_json()["user_id"])


def test_write_message_accepts_file(store):
    payload = {
        "envelope": _env(store.user_id, "envf1"),
        "content_type": "file",
        "file_name": "plan.md",
        "file_mime": "text/markdown",
    }
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envf1")
    assert m["content_type"] == "file"
    assert m["file_name"] == "plan.md"
    assert m["file_mime"] == "text/markdown"


def test_write_message_accepts_binary_plaintext_file_when_effective_off(
    store, monkeypatch,
):
    raw = b"\x00plaintext-file\xff"
    monkeypatch.setattr(
        core_envelope,
        "resolve_content_encryption",
        lambda _uid: "off",
    )
    payload = {
        "envelope": {
            "v": 1,
            "id": "env-plain-file",
            "body_b64": _b64(raw),
            "body_size_bytes": len(raw),
            "visibility": "shared",
            "owner_user_id": store.user_id,
        },
        "content_type": "file",
        "file_name": "plain.bin",
        "file_mime": "application/octet-stream",
    }

    body, status = chat_core.write_message(store, payload)

    assert status == 200, body
    message = next(
        x for x in store.chat_messages if x["id"] == "env-plain-file"
    )
    assert message["body_b64"] == _b64(raw)
    assert message["body_size_bytes"] == len(raw)
    assert "body_ct" not in message and "K_enclave" not in message


def test_write_message_rejects_binary_plaintext_file_when_effective_on(
    store, monkeypatch,
):
    monkeypatch.setattr(
        core_envelope,
        "resolve_content_encryption",
        lambda _uid: "on",
    )
    body, status = chat_core.write_message(
        store,
        {
            "envelope": {
                "id": "env-rejected-plain-file",
                "body_b64": _b64(b"plaintext"),
                "visibility": "shared",
                "owner_user_id": store.user_id,
            },
            "content_type": "file",
        },
    )

    assert status == 400
    assert body["error"] == "plaintext_envelope_not_enabled_for_this_account"


def test_write_message_rejects_unknown_content_type(store):
    payload = {"envelope": _env(store.user_id, "envv1"), "content_type": "video"}
    body, status = chat_core.write_message(store, payload)
    assert status == 400
    assert body["error"].startswith("content_type")


def test_write_message_text_still_works(store):
    payload = {"envelope": _env(store.user_id, "envt1"), "content_type": "text"}
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envt1")
    assert m["content_type"] == "text"


def test_write_message_image_persists_caption(store):
    # image + text: the user's caption rides a separate client-built envelope and
    # MUST be persisted (caption_body_ct) so the enclave decrypts it into content
    # and the agent sees the text. Regression for "image+text → text swallowed".
    cap = _env(store.user_id, "capimg1")
    payload = {
        "envelope": _env(store.user_id, "envimg1"),
        "content_type": "image",
        "caption_envelope": cap,
    }
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envimg1")
    assert m["content_type"] == "image"
    assert m.get("caption_body_ct") == cap["body_ct"]   # caption persisted
    assert m.get("caption_K_enclave") == cap["K_enclave"]  # enclave can decrypt it


def test_write_message_image_without_caption_ok(store):
    # image with no accompanying text → no caption fields, no crash.
    payload = {"envelope": _env(store.user_id, "envimg2"), "content_type": "image"}
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envimg2")
    assert m["content_type"] == "image"
    assert not m.get("caption_body_ct")


def test_write_message_file_persists_caption_alongside_name(store):
    cap = _env(store.user_id, "capfile1")
    payload = {
        "envelope": _env(store.user_id, "envfile2"),
        "content_type": "file",
        "file_name": "plan.md",
        "caption_envelope": cap,
    }
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envfile2")
    assert m["file_name"] == "plan.md"
    assert m.get("caption_body_ct") == cap["body_ct"]


# --------------------------------------------------------------------------- #
# context_refs → quoted_memory_ids (Garden「talk in chat」on the resident line).
# Same plaintext-routing contract as the hosted path in chat_send_core: only
# memory ids are persisted; the enclave expands them into cards on read.
# --------------------------------------------------------------------------- #

def test_write_message_persists_quoted_memory_ids(store):
    payload = {
        "envelope": _env(store.user_id, "envq1"),
        "context_refs": [{"type": "memory", "id": "mom_dog", "title": "养了蛋子"}],
    }
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envq1")
    assert m.get("quoted_memory_ids") == "mom_dog"


def test_write_message_quoted_ids_filter_cap_and_junk(store):
    refs = [
        {"type": "memory", "id": f"m{i}"} for i in range(10)  # sliced to 8
    ] + [
        {"type": "screen", "id": "not_memory"},  # non-memory type ignored
        {"type": "memory", "id": "   "},         # blank id ignored
        "junk", 42, None,                        # malformed entries ignored
    ]
    payload = {"envelope": _env(store.user_id, "envq2"), "context_refs": refs}
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envq2")
    assert m.get("quoted_memory_ids") == ",".join(f"m{i}" for i in range(8))


def test_write_message_quoted_ids_hosted_parity(store):
    # Normalization must stay in lockstep with hosted _context_refs_from_payload:
    # slice-to-8 happens BEFORE filtering (a memory ref hiding behind 8 junk
    # entries is dropped, as on hosted), type is stripped, ids truncate to 160,
    # and the camelCase contextRefs alias is accepted.
    behind_junk = [{"type": "screen", "id": f"s{i}"} for i in range(8)]
    behind_junk.append({"type": "memory", "id": "mom_ninth"})
    payload = {"envelope": _env(store.user_id, "envq5"), "context_refs": behind_junk}
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envq5")
    assert not m.get("quoted_memory_ids")

    long_id = "x" * 161
    payload = {
        "envelope": _env(store.user_id, "envq6"),
        "contextRefs": [{"type": " memory ", "id": long_id}],
    }
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envq6")
    assert m.get("quoted_memory_ids") == "x" * 160

    # Truncation exposing trailing whitespace must strip again, as hosted does
    # at its filter stage ("x"*159 + " y" → cut at 160 → "x"*159 + " " → strip).
    payload = {
        "envelope": _env(store.user_id, "envq7"),
        "context_refs": [{"type": "memory", "id": "x" * 159 + " y"}],
    }
    body, status = chat_core.write_message(store, payload)
    assert status == 200, body
    m = next(x for x in store.chat_messages if x["id"] == "envq7")
    assert m.get("quoted_memory_ids") == "x" * 159


def test_quoted_ids_normalization_matches_hosted_pipeline():
    # Anti-drift guard: run the exact hosted pipeline (normalize → memory
    # filter, hosted/context.py + chat_send_core) against the chat_core mirror
    # over a corpus of tricky inputs. Tests may import both layers even though
    # chat itself must not import hosted.
    from hosted import context as hosted_context

    corpus = [
        [],
        "not_a_list",
        [{"type": "memory", "id": "m1"}],
        [{"type": " memory ", "id": "  m2  "}],
        [{"type": "screen", "id": "s"}] * 8 + [{"type": "memory", "id": "m9"}],
        [{"type": "memory", "id": f"m{i}"} for i in range(12)],
        [{"type": "memory", "id": "x" * 161}],
        [{"type": "memory", "id": "x" * 159 + " y"}],
        [{"type": "memory", "id": "   "}, "junk", 42, None, {"id": "no_type"}],
    ]
    for refs in corpus:
        for key in ("context_refs", "contextRefs"):
            payload = {key: refs}
            hosted_refs = hosted_context._context_refs_from_payload(payload)
            hosted_ids = [
                str(r.get("id") or "").strip()
                for r in hosted_refs
                if r.get("type") == "memory" and str(r.get("id") or "").strip()
            ]
            expected = ",".join(hosted_ids[:8])
            got = chat_core._quoted_memory_ids_from_context_refs(payload)
            assert got == expected, f"drift for {key}={refs!r}: {got!r} != {expected!r}"


def test_write_message_without_context_refs_unchanged(store):
    # The normal path must stay byte-identical: no refs (or malformed
    # container) → the quoted_memory_ids key is never written.
    for marker, extra in (("envq3", {}), ("envq4", {"context_refs": "oops"})):
        payload = {"envelope": _env(store.user_id, marker), **extra}
        body, status = chat_core.write_message(store, payload)
        assert status == 200, body
        m = next(x for x in store.chat_messages if x["id"] == marker)
        assert "quoted_memory_ids" not in m or not m["quoted_memory_ids"]
