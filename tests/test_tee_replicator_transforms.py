"""Pure-function tests for tee_replicator.transforms — ciphertext doc → plaintext doc.

No network: the enclave decrypt is a predictable stub (``b"PT:" + body_ct``).
Guards the one invariant that matters most: no envelope/crypto field survives
into the plaintext doc the TEE will store.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from tee_replicator import transforms  # noqa: E402


def _decrypt_stub(envelope, purpose):
    return b"PT:" + envelope["body_ct"].encode()


_CRYPTO_SUBSTRINGS = ("body_ct", "nonce", "K_user", "K_enclave", "enclave_pk_fpr")


def _assert_no_crypto_leak(d: dict):
    for k in d:
        for bad in _CRYPTO_SUBSTRINGS:
            assert bad not in k, f"crypto field {k!r} leaked into plaintext doc"


def test_chat_doc_all_three_envelopes():
    doc = {"id": "m1", "role": "assistant", "ts": 1.0, "source": "chat",
           "content_type": "text", "visibility": "shared", "owner_user_id": "u",
           "v": 1, "body_ct": "AAA", "nonce": "n", "K_user": "k", "K_enclave": "ke",
           "enclave_pk_fpr": "f",
           "thinking_v": 1, "thinking_body_ct": "BBB", "thinking_nonce": "n",
           "thinking_K_user": "k", "thinking_K_enclave": "ke", "thinking_enclave_pk_fpr": "f",
           "thinking_kind": "reasoning", "thinking_source": "codex", "thinking_model": "m",
           "thinking_native": True,
           "caption_v": 1, "caption_body_ct": "CCC", "caption_nonce": "n",
           "caption_K_user": "k", "caption_K_enclave": "ke", "caption_enclave_pk_fpr": "f",
           "caption_visibility": "shared"}
    out = transforms.plaintext_chat_doc(doc, _decrypt_stub)
    assert out["body"] == "PT:AAA"
    assert out["thinking"]["body"] == "PT:BBB" and out["thinking"]["kind"] == "reasoning"
    assert out["thinking"]["native"] is True
    assert out["caption"]["body"] == "PT:CCC"
    _assert_no_crypto_leak(out)
    _assert_no_crypto_leak(out["thinking"])
    _assert_no_crypto_leak(out["caption"])
    assert out["visibility"] == "shared" and out["role"] == "assistant"


def test_chat_doc_plain_main_only():
    doc = {"id": "m2", "role": "user", "ts": 2.0, "visibility": "shared",
           "owner_user_id": "u", "v": 1, "body_ct": "DDD", "nonce": "n",
           "K_user": "k", "K_enclave": "ke", "enclave_pk_fpr": "f"}
    out = transforms.plaintext_chat_doc(doc, _decrypt_stub)
    assert out["body"] == "PT:DDD" and "thinking" not in out and "caption" not in out
    _assert_no_crypto_leak(out)


def test_chat_doc_plain_main_and_plain_subcontent():
    doc = {
        "id": "m-plain", "role": "assistant", "ts": 2.5,
        "visibility": "shared", "owner_user_id": "u", "body": "answer",
        "thinking_body": "reason", "thinking_kind": "reasoning",
        "thinking_visibility": "shared",
    }
    out = transforms.plaintext_chat_doc(
        doc, lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("plaintext row must not decrypt")))
    assert out["body"] == "answer"
    assert out["thinking"] == {
        "body": "reason", "kind": "reasoning", "visibility": "shared",
    }
    assert "thinking_body" not in out


def test_chat_doc_plain_main_with_encrypted_subcontent_still_decrypts():
    doc = {
        "id": "m-mixed", "role": "assistant", "ts": 2.6,
        "visibility": "shared", "owner_user_id": "u", "body": "answer",
        "thinking_body_ct": "BBB", "thinking_nonce": "n",
        "thinking_K_user": "k", "thinking_K_enclave": "ke",
        "thinking_visibility": "shared",
    }
    assert transforms.needs_decrypt(doc) is True
    out = transforms.plaintext_chat_doc(doc, _decrypt_stub)
    assert out["body"] == "answer"
    assert out["thinking"]["body"] == "PT:BBB"


def test_local_only_raises_pending():
    doc = {"id": "m3", "visibility": "local_only", "body_ct": "X", "nonce": "n",
           "K_user": "k", "v": 1, "owner_user_id": "u", "ts": 3.0, "role": "user"}
    try:
        transforms.plaintext_chat_doc(doc, _decrypt_stub)
        assert False, "expected PendingDeviceMigration"
    except transforms.PendingDeviceMigration:
        pass


def test_no_k_enclave_raises_pending():
    # shared visibility but no K_enclave key at all → enclave can't decrypt → pending
    doc = {"id": "m4", "visibility": "shared", "body_ct": "X", "nonce": "n",
           "K_user": "k", "v": 1, "owner_user_id": "u", "ts": 4.0, "role": "user"}
    try:
        transforms.plaintext_chat_doc(doc, _decrypt_stub)
        assert False, "expected PendingDeviceMigration"
    except transforms.PendingDeviceMigration:
        pass


def test_memory_and_world_book_single_envelope():
    doc = {"id": "mem1", "occurred_at": "2026-01-01", "visibility": "shared",
           "owner_user_id": "u", "v": 1, "body_ct": "EEE", "nonce": "n",
           "K_user": "k", "K_enclave": "ke", "enclave_pk_fpr": "f", "importance": 0.5}
    out = transforms.plaintext_memory_doc(doc, _decrypt_stub)
    assert out["body"] == "PT:EEE" and out["importance"] == 0.5
    _assert_no_crypto_leak(out)

    wb = dict(doc, id="wb1", body_ct="FFF")
    out2 = transforms.plaintext_world_book_doc(wb, _decrypt_stub)
    assert out2["body"] == "PT:FFF"
    _assert_no_crypto_leak(out2)


def test_nul_stripped_from_body_and_carried_metadata():
    # PostgreSQL text/JSONB can't store NUL (0x00). It appears BOTH in the
    # decrypted body AND in client-carried metadata fields (which _strip_envelope
    # passes through). The transform must scrub the whole output doc, not just body.
    def decrypt_with_nul(envelope, purpose):
        return "hello\x00world".encode()

    doc = {"id": "m1", "role": "user", "ts": 1.0, "source": "chat",
           "content_type": "text", "visibility": "shared", "owner_user_id": "u",
           # a plaintext metadata field carrying a NUL (not the encrypted body)
           "client_tag": "ta\x00g", "nested": {"note": "a\x00b"},
           "v": 1, "body_ct": "x", "nonce": "n", "K_user": "k", "K_enclave": "ke",
           "enclave_pk_fpr": "f"}
    out = transforms.plaintext_chat_doc(doc, decrypt_with_nul)
    assert out["body"] == "helloworld"
    assert out["client_tag"] == "tag"          # metadata scrubbed
    assert out["nested"]["note"] == "ab"       # nested scrubbed
    import json
    assert "\x00" not in json.dumps(out)       # nothing NUL anywhere in the doc
