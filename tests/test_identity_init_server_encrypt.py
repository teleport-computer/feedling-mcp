"""Identity init plaintext branch: a route-A agent (no crypto) submits a
plaintext `identity`; the server builds the envelope via the same path
memory.add / identity.profile_patch use. The pre-built `envelope` branch
(iOS / official client) must keep working unchanged.
"""
from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402


ANCHOR = "transcript: earliest chat dated 2026-05-08"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _plain_identity() -> dict:
    return {
        "agent_name": "bro",
        "self_introduction": "I keep the real thread with you.",
        "dimensions": [
            {"name": "Signal sensitivity", "value": 92, "description": "Notices subtle shifts."},
        ],
    }


def _prebuilt_envelope(user_id: str) -> dict:
    return {
        "id": "client_env_1",
        "body_ct": "client_ct",
        "nonce": "client_nonce",
        "K_user": "client_k_user",
        "K_enclave": "client_k_enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
        "enclave_pk_fpr": "client",
    }


def _fake_envelope_builder(captured: list):
    def _build(store, plaintext: bytes, item_id: str | None = None):
        captured.append(json.loads(plaintext.decode("utf-8")))
        envelope = {
            "id": item_id or "env_1",
            "body_ct": "ct_1",
            "nonce": "nonce_1",
            "K_user": "k_user_1",
            "K_enclave": "k_enclave_1",
            "visibility": "shared",
            "owner_user_id": store.user_id,
            "enclave_pk_fpr": "test",
        }
        return envelope, ""
    return _build


def test_init_plaintext_server_builds_envelope(client, monkeypatch):
    user_id, api_key = _register(client)
    captured: list = []
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured))

    res = client.post(
        "/v1/identity/init",
        headers=_headers(api_key),
        json={"identity": _plain_identity(), "days_with_user": 30, "relationship_anchor_evidence": ANCHOR},
    )

    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "created"
    assert body["identity"]["body_ct"] == "ct_1"
    assert body["identity"]["owner_user_id"] == user_id
    # the server built the envelope from OUR plaintext identity
    assert captured[-1]["agent_name"] == "bro"
    assert captured[-1]["self_introduction"] == _plain_identity()["self_introduction"]
    saved = db.get_blob(user_id, "identity")
    assert saved["body_ct"] == "ct_1"
    assert saved["relationship_anchor_evidence"] == ANCHOR


def test_init_prebuilt_envelope_still_works(client):
    user_id, api_key = _register(client)
    res = client.post(
        "/v1/identity/init",
        headers=_headers(api_key),
        json={"envelope": _prebuilt_envelope(user_id), "days_with_user": 10, "relationship_anchor_evidence": ANCHOR},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    saved = db.get_blob(user_id, "identity")
    assert saved["body_ct"] == "client_ct"


def test_init_rejects_both_envelope_and_identity(client):
    user_id, api_key = _register(client)
    res = client.post(
        "/v1/identity/init",
        headers=_headers(api_key),
        json={
            "envelope": _prebuilt_envelope(user_id),
            "identity": _plain_identity(),
            "days_with_user": 1,
            "relationship_anchor_evidence": ANCHOR,
        },
    )
    assert res.status_code == 400
    assert "either" in res.get_json()["error"]


def test_init_rejects_neither(client):
    _user_id, api_key = _register(client)
    res = client.post(
        "/v1/identity/init",
        headers=_headers(api_key),
        json={"days_with_user": 1, "relationship_anchor_evidence": ANCHOR},
    )
    assert res.status_code == 400
    assert "required" in res.get_json()["error"]


def test_init_identity_must_be_object(client):
    _user_id, api_key = _register(client)
    res = client.post(
        "/v1/identity/init",
        headers=_headers(api_key),
        json={"identity": "not a dict", "days_with_user": 1, "relationship_anchor_evidence": ANCHOR},
    )
    assert res.status_code == 400
    assert res.get_json()["error"] == "identity must be object"


def test_init_build_failure_returns_409(client, monkeypatch):
    _user_id, api_key = _register(client)
    monkeypatch.setattr(
        core_envelope,
        "_build_shared_envelope_for_store",
        lambda store, plaintext, item_id=None: (None, "user_content_public_key_missing"),
    )
    res = client.post(
        "/v1/identity/init",
        headers=_headers(api_key),
        json={"identity": _plain_identity(), "days_with_user": 1, "relationship_anchor_evidence": ANCHOR},
    )
    assert res.status_code == 409, res.get_data(as_text=True)
    assert res.get_json()["error"] == "user_content_public_key_missing"


def test_init_plaintext_still_validates_anchor_evidence(client, monkeypatch):
    _user_id, api_key = _register(client)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))
    res = client.post(
        "/v1/identity/init",
        headers=_headers(api_key),
        json={"identity": _plain_identity(), "days_with_user": 1, "relationship_anchor_evidence": "short"},
    )
    assert res.status_code == 400
    assert "relationship_anchor_evidence" in res.get_json()["error"]


def test_init_plaintext_requires_days_with_user(client, monkeypatch):
    _user_id, api_key = _register(client)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))
    res = client.post(
        "/v1/identity/init",
        headers=_headers(api_key),
        json={"identity": _plain_identity(), "relationship_anchor_evidence": ANCHOR},
    )
    assert res.status_code == 400
    assert "days_with_user" in res.get_json()["error"]
