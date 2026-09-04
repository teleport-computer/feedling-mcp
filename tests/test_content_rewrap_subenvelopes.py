"""Rewrap must also re-seal the ``thinking_*`` / ``caption_*`` sub-envelopes.

A chat row stores up to three independent envelopes under one record: the main
body, the agent's thinking summary, and an image caption. Only the main one used
to be rewrapped, so after a key rotation the device could read the message but
not its thinking summary (iOS silently logs the unseal failure and renders an
empty thinking block — see ChatMessage.decryptThinkingSummaryIfNeeded).

The sub-envelopes carry their own ``id`` / ``v`` / ``owner_user_id``, and iOS
rebuilds the AAD from exactly those three (falling back to the record's ``id``
when ``thinking_id`` is absent). Rewrap must preserve that triple or the device
can no longer derive a matching AAD.
"""

from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from accounts import registry  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


OLD_PK = _b64(b"\x11" * 32)
NEW_PK = _b64(b"\x33" * 32)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    monkeypatch.setattr(
        core_enclave,
        "_get_enclave_info",
        lambda: {"content_pk_hex": ("22" * 32), "compose_hash": "test"},
    )
    with make_client() as c:
        yield c


def _register(client) -> tuple[str, str]:
    res = client.post("/v1/users/register", json={"public_key": OLD_PK, "archive_language": "en"})
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _seed_chat(user_id: str, msg: dict) -> None:
    store = core_store.get_store(user_id)
    with store.chat_lock:
        store.chat_messages = [msg]
        db.chat_append(user_id, msg["id"], msg["ts"], msg, core_store.MAX_CHAT_MESSAGES)


def _base_msg(user_id: str, item_id: str = "chat1") -> dict:
    return {
        "v": 1,
        "id": item_id,
        "body_ct": _b64(f"old-body:{item_id}".encode()),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 48),
        "K_enclave": _b64(b"\x02" * 48),
        "visibility": "shared",
        "owner_user_id": user_id,
        "enclave_pk_fpr": "old",
        "role": "openclaw",
        "source": "test",
        "ts": time.time(),
        "content_type": "text",
    }


def _with_thinking(msg: dict, *, thinking_id: str | None = "think1", shared: bool = True) -> dict:
    msg = dict(msg)
    msg["thinking_v"] = 1
    if thinking_id is not None:
        msg["thinking_id"] = thinking_id
    msg["thinking_owner_user_id"] = msg["owner_user_id"]
    msg["thinking_body_ct"] = _b64(b"old-thinking-body")
    msg["thinking_nonce"] = _b64(b"\x09" * 12)
    msg["thinking_K_user"] = _b64(b"\x03" * 48)
    msg["thinking_visibility"] = "shared" if shared else "local_only"
    if shared:
        msg["thinking_K_enclave"] = _b64(b"\x04" * 48)
    return msg


def _fake_decrypt(envelope, key, purpose, caller_user_id):
    return f"plaintext:{envelope.get('id')}:{envelope.get('body_ct')}".encode()


def _rewrap(client, api_key: str, public_key: str = NEW_PK) -> dict:
    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": public_key},
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def _stored(user_id: str) -> dict:
    return core_store.get_store(user_id).chat_messages[0]


def test_rewrap_reseals_thinking_sub_envelope(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_chat(user_id, _with_thinking(_base_msg(user_id)))
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _fake_decrypt)

    _rewrap(client, api_key)

    msg = _stored(user_id)
    assert msg["thinking_K_user"] != _b64(b"\x03" * 48), "thinking K_user must be re-sealed"
    assert msg["thinking_body_ct"] != _b64(b"old-thinking-body")
    assert msg["thinking_content_pk_fpr"] == core_envelope._content_public_key_fingerprint(
        base64.b64decode(NEW_PK)
    )


def test_rewrap_preserves_thinking_aad_triple(client, monkeypatch):
    """iOS derives the AAD from thinking_owner_user_id|thinking_v|thinking_id."""
    user_id, api_key = _register(client)
    _seed_chat(user_id, _with_thinking(_base_msg(user_id)))
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _fake_decrypt)

    _rewrap(client, api_key)

    msg = _stored(user_id)
    assert msg["thinking_id"] == "think1"
    assert msg["thinking_owner_user_id"] == user_id
    assert msg["thinking_v"] == 1


def test_rewrap_does_not_skip_record_whose_sub_envelope_is_stale(client, monkeypatch):
    """The main envelope is already current; the thinking one is not.

    ``skipped_already_current`` keys off the main envelope's content_pk_fpr, so
    without a sub-envelope-aware check the whole record is skipped and the
    thinking summary stays sealed to the dead key forever.
    """
    user_id, api_key = _register(client)
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _fake_decrypt)

    # First rewrap brings the main envelope (and only it) to NEW_PK.
    _seed_chat(user_id, _base_msg(user_id))
    _rewrap(client, api_key)
    main_after_first = dict(_stored(user_id))

    # Now attach a stale thinking sub-envelope and rewrap again to the same key.
    _seed_chat(user_id, _with_thinking(main_after_first))
    body = _rewrap(client, api_key)

    msg = _stored(user_id)
    assert msg["thinking_K_user"] != _b64(b"\x03" * 48), "stale sub-envelope must be rewrapped"
    # Envelopes are counted individually: the main one is already current (skipped),
    # the thinking one still had to be re-sealed (rewrapped).
    assert body["summary"]["chat"]["rewrapped"] == 1
    assert body["summary"]["chat"]["skipped"] == 1
    # The main envelope was already current — don't burn an enclave call re-sealing it.
    assert msg["body_ct"] == main_after_first["body_ct"]


def test_rewrap_skips_local_only_thinking_without_dropping_it(client, monkeypatch):
    """local_only sub-envelopes have no K_enclave; the enclave cannot open them."""
    user_id, api_key = _register(client)
    _seed_chat(user_id, _with_thinking(_base_msg(user_id), shared=False))
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _fake_decrypt)

    _rewrap(client, api_key)

    msg = _stored(user_id)
    assert msg["thinking_K_user"] == _b64(b"\x03" * 48), "local_only must be left alone"
    assert msg["thinking_body_ct"] == _b64(b"old-thinking-body")
    assert "thinking_content_pk_fpr" not in msg
    assert msg["K_user"] != _b64(b"\x01" * 48), "main envelope still rewrapped"


def test_rewrap_falls_back_to_record_id_when_thinking_id_absent(client, monkeypatch):
    """iOS uses `thinking_id ?? id`; rewrap must derive the same AAD."""
    user_id, api_key = _register(client)
    _seed_chat(user_id, _with_thinking(_base_msg(user_id, "chatX"), thinking_id=None))
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _fake_decrypt)

    _rewrap(client, api_key)

    msg = _stored(user_id)
    assert msg["thinking_id"] == "chatX"


def _decrypt_ok_main_fail_thinking(envelope, key, purpose, caller_user_id):
    if "thinking" in purpose:
        raise RuntimeError("enclave_http_502")
    return f"plaintext:{envelope.get('id')}".encode()


def test_sub_envelope_failure_still_counts_main_rewrap_and_advances_key(client, monkeypatch):
    """A failing sub-envelope must not mask the main envelope's progress.

    _swap_chat persists the main envelope before made_progress is evaluated, so
    reporting "no progress" here would leave stored chat sealed to the new key
    while users.public_key still points at the old one — and every retry would
    skip the (already current) main envelope, so the key could never advance.
    """
    user_id, api_key = _register(client)
    _seed_chat(user_id, _with_thinking(_base_msg(user_id)))
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _decrypt_ok_main_fail_thinking)

    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": NEW_PK},
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()

    assert body["status"] == "partial"
    assert body["summary"]["total_rewrapped"] >= 1, "main envelope progress must be counted"
    assert body["summary"]["total_errors"] >= 1, "the failing sub-envelope must be reported"
    assert body["pending"], "client needs to know what to retry"

    # The main envelope really was persisted…
    assert _stored(user_id)["K_user"] != _b64(b"\x01" * 48)
    # …so the registered key must move with it.
    assert registry._get_user_public_key(user_id) == NEW_PK


def test_sub_envelope_only_failure_makes_no_progress_and_persists_nothing(client, monkeypatch):
    """Main already current + sub-envelope fails → nothing written, no key move."""
    user_id, api_key = _register(client)
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _fake_decrypt)
    _seed_chat(user_id, _base_msg(user_id))
    _rewrap(client, api_key)  # main envelope now current at NEW_PK
    main_after = dict(_stored(user_id))

    _seed_chat(user_id, _with_thinking(main_after))
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _decrypt_ok_main_fail_thinking)
    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": NEW_PK},
        headers={"X-API-Key": api_key},
    )

    assert res.status_code == 409, res.get_data(as_text=True)
    assert res.get_json()["error"] == "rewrap_failed_no_progress"
    assert _stored(user_id)["thinking_K_user"] == _b64(b"\x03" * 48), "nothing should be written"


def test_rewrap_is_idempotent_for_sub_envelopes(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_chat(user_id, _with_thinking(_base_msg(user_id)))
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", _fake_decrypt)

    _rewrap(client, api_key)
    first = dict(_stored(user_id))
    body = _rewrap(client, api_key)

    assert body["summary"]["chat"]["rewrapped"] == 0
    # Both envelopes (main + thinking) are current now, so both skip.
    assert body["summary"]["chat"]["skipped"] == 2
    assert _stored(user_id)["thinking_K_user"] == first["thinking_K_user"]
