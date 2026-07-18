"""Envelope ``content_pk_fpr`` labeling + chat-ingest guard (usr_f13f 2026-07-16).

Incident shape: a writer (resident consumer with a stale whoami key cache)
sealed every new chat envelope to a retired user content key. The device could
never open them; each one had to be repaired after the fact by an
iOS-triggered ``/v1/content/rewrap-to-current-key`` storm.

Two-part fix under test here:

1. ``build_envelope`` labels every envelope with the fingerprint of the user
   pk it sealed to (``content_pk_fpr``) — previously only the rewrap endpoint
   wrote that label.
2. The chat ingest routes (``/v1/chat/message``, ``/v1/chat/response``) reject
   a LABELED envelope whose fingerprint doesn't match the user's currently
   registered content key with a structured 409, so a stale-key writer bounces
   (and can refresh + retry) instead of storing ciphertext the device can
   never open. Unlabeled envelopes still pass (older clients).
"""

from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402,F401  (assembles injections chat routes need)
from accounts import registry as accounts_registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from bootstrap import gates as boot_gates  # noqa: E402
from content_encryption import build_envelope  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


USER_PK = b"\x11" * 32
USER_PK_FPR = hashlib.sha256(USER_PK).hexdigest()[:16]


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _hk(api_key: str) -> dict:
    return {"X-API-Key": api_key}


def _env(user_id: str, marker: str, *, visibility: str = "shared") -> dict:
    env = {
        "v": 1,
        "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 32),
        "visibility": visibility,
        "owner_user_id": user_id,
    }
    if visibility == "shared":
        env["K_enclave"] = _b64(b"\x02" * 32)
    return env


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    accounts_registry._users[:] = []
    accounts_registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(USER_PK), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


@pytest.fixture()
def keyless_user(tmp_path, monkeypatch):
    """A user registered WITHOUT a content pubkey (pre-v1 registration)."""
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    accounts_registry._users[:] = []
    accounts_registry._key_to_user.clear()
    core_store._stores.clear()
    accounts_registry._save_users()
    res = make_client().post("/v1/users/register", json={"archive_language": "en"})
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --------------------------------------------------------------------------- #
# 1. build_envelope labels the sealed-to key
# --------------------------------------------------------------------------- #

def test_build_envelope_labels_content_pk_fpr():
    env = build_envelope(
        plaintext=b"hello",
        owner_user_id="usr_x",
        user_pk_bytes=USER_PK,
        enclave_pk_bytes=b"\x02" * 32,
        visibility="shared",
    )
    assert env["content_pk_fpr"] == USER_PK_FPR


def test_build_envelope_labels_local_only_too():
    env = build_envelope(
        plaintext=b"hello",
        owner_user_id="usr_x",
        user_pk_bytes=USER_PK,
        enclave_pk_bytes=None,
        visibility="local_only",
    )
    assert env["content_pk_fpr"] == USER_PK_FPR


# --------------------------------------------------------------------------- #
# 2. /v1/chat/message guard
# --------------------------------------------------------------------------- #

def test_chat_message_rejects_mismatched_fpr(user):
    uid, api_key = user
    env = _env(uid, "m1")
    env["content_pk_fpr"] = "deadbeefdeadbeef"
    res = make_client().post(
        "/v1/chat/message", headers=_hk(api_key), json={"envelope": env})
    assert res.status_code == 409, res.get_data(as_text=True)
    body = res.get_json()
    assert body["error"] == "content_pk_fpr_mismatch"
    assert body["current_public_key_fpr"] == USER_PK_FPR
    assert body["envelope_content_pk_fpr"] == "deadbeefdeadbeef"
    # nothing stored
    store = core_store.get_store(uid)
    with store.chat_lock:
        assert store.chat_messages == []


def test_chat_message_accepts_matching_fpr(user):
    uid, api_key = user
    env = _env(uid, "m2")
    env["content_pk_fpr"] = USER_PK_FPR
    res = make_client().post(
        "/v1/chat/message", headers=_hk(api_key), json={"envelope": env})
    assert res.status_code == 200, res.get_data(as_text=True)


def test_chat_message_accepts_unlabeled_envelope(user):
    uid, api_key = user
    res = make_client().post(
        "/v1/chat/message", headers=_hk(api_key), json={"envelope": _env(uid, "m3")})
    assert res.status_code == 200, res.get_data(as_text=True)


def test_chat_message_skips_guard_without_registered_key(keyless_user):
    uid, api_key = keyless_user
    env = _env(uid, "m4")
    env["content_pk_fpr"] = "deadbeefdeadbeef"
    res = make_client().post(
        "/v1/chat/message", headers=_hk(api_key), json={"envelope": env})
    assert res.status_code == 200, res.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# 3. /v1/chat/response guard (main + thinking envelopes)
# --------------------------------------------------------------------------- #

def test_chat_response_rejects_mismatched_fpr(user, monkeypatch):
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    uid, api_key = user
    env = _env(uid, "r1")
    env["content_pk_fpr"] = "deadbeefdeadbeef"
    res = make_client().post(
        "/v1/chat/response", headers=_hk(api_key),
        json={"envelope": env, "source": "chat"})
    assert res.status_code == 409, res.get_data(as_text=True)
    assert res.get_json()["error"] == "content_pk_fpr_mismatch"


def test_chat_response_rejects_mismatched_thinking_fpr(user, monkeypatch):
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    uid, api_key = user
    thinking = _env(uid, "r2think")
    thinking["content_pk_fpr"] = "deadbeefdeadbeef"
    res = make_client().post(
        "/v1/chat/response", headers=_hk(api_key),
        json={"envelope": _env(uid, "r2"), "thinking_envelope": thinking,
              "source": "chat"})
    assert res.status_code == 409, res.get_data(as_text=True)
    assert res.get_json()["error"] == "content_pk_fpr_mismatch"


def test_chat_response_accepts_matching_fpr(user, monkeypatch):
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    uid, api_key = user
    env = _env(uid, "r3")
    env["content_pk_fpr"] = USER_PK_FPR
    res = make_client().post(
        "/v1/chat/response", headers=_hk(api_key),
        json={"envelope": env, "source": "chat"})
    assert res.status_code == 200, res.get_data(as_text=True)


# --------------------------------------------------------------------------- #
# 4. the label survives storage (rewrap's skip logic reads it off the row)
# --------------------------------------------------------------------------- #

def test_chat_message_stores_content_pk_fpr(user):
    uid, api_key = user
    env = _env(uid, "s1")
    env["content_pk_fpr"] = USER_PK_FPR
    res = make_client().post(
        "/v1/chat/message", headers=_hk(api_key), json={"envelope": env})
    assert res.status_code == 200, res.get_data(as_text=True)
    store = core_store.get_store(uid)
    with store.chat_lock:
        stored = [m for m in store.chat_messages if m["id"] == "s1"][0]
    assert stored["content_pk_fpr"] == USER_PK_FPR


def test_chat_response_stores_thinking_content_pk_fpr(user, monkeypatch):
    monkeypatch.setattr(boot_gates, "_gate_bootstrap_for_chat", lambda store, **_: None)
    uid, api_key = user
    env = _env(uid, "s2")
    env["content_pk_fpr"] = USER_PK_FPR
    thinking = _env(uid, "s2think")
    thinking["content_pk_fpr"] = USER_PK_FPR
    res = make_client().post(
        "/v1/chat/response", headers=_hk(api_key),
        json={"envelope": env, "thinking_envelope": thinking, "source": "chat"})
    assert res.status_code == 200, res.get_data(as_text=True)
    store = core_store.get_store(uid)
    with store.chat_lock:
        stored = [m for m in store.chat_messages if m["id"] == "s2"][0]
    assert stored["content_pk_fpr"] == USER_PK_FPR
    assert stored["thinking_content_pk_fpr"] == USER_PK_FPR
