"""Fix A: identity.profile_patch on a fresh-start user (no identity card) creates
the card instead of 409-wedging the V2 conversation. A profile_patch that resolves
to nothing writable still 409s; an existing card takes the normal update path."""
from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from identity import actions as identity_actions_mod  # noqa: E402


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
        json={"public_key": _b64(b"\x22" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _fake_envelope_builder(captured: list):
    def _build(store, plaintext: bytes, item_id: str | None = None):
        try:
            captured.append(json.loads(plaintext.decode("utf-8")))
        except Exception:
            captured.append(plaintext.decode("utf-8"))
        envelope = {
            "id": item_id or "env_boot",
            "body_ct": f"ct_{len(captured)}",
            "nonce": f"nonce_{len(captured)}",
            "K_user": f"k_user_{len(captured)}",
            "K_enclave": f"k_enclave_{len(captured)}",
            "visibility": "shared",
            "owner_user_id": store.user_id,
            "enclave_pk_fpr": "test",
        }
        return envelope, ""
    return _build


def _no_identity_enclave(path, key, params=None):
    # /v1/identity/get with no identity dict → _identity_plain_for_action returns
    # (None, "identity_not_initialized").
    return {}, ""


def _plain_identity() -> dict:
    return {
        "agent_name": "bro",
        "self_introduction": "I keep the real thread with you.",
        "dimensions": [
            {"name": "Signal sensitivity", "value": 92, "description": "x"},
            {"name": "Context retention", "value": 88, "description": "y"},
        ],
    }


def test_identity_action_reads_plaintext_card_without_enclave(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_plain")
    monkeypatch.setattr(
        identity_actions_mod.identity_service,
        "_load_identity",
        lambda _store: {
            "v": 1,
            "body": json.dumps(_plain_identity()),
            "owner_user_id": "usr_plain",
            "visibility": "shared",
        },
    )
    monkeypatch.setattr(
        identity_actions_mod.identity_service,
        "_live_days_with_user",
        lambda *_args, **_kwargs: 3,
    )
    monkeypatch.setattr(
        core_enclave,
        "_enclave_get_json_for_gate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("plaintext identity must not call enclave")
        ),
    )

    identity, error = identity_actions_mod._identity_plain_for_action(store, None)

    assert error == ""
    assert identity["agent_name"] == "bro"
    assert identity["days_with_user"] == 3


def test_profile_patch_bootstraps_card_when_none_exists(client, monkeypatch):
    user_id, api_key = _register(client)
    captured: list = []
    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", _no_identity_enclave)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured))

    assert db.get_blob(user_id, "identity") is None  # fresh-start: no card

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.profile_patch",
                "patch": {"self_introduction": "I'm your co-pilot.", "agent_name": "Ori"},
                "reason": "Agent introduces itself on the first turn.",
            }],
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "ok"
    assert body["effects"][0]["type"] == "identity_updated"
    assert set(body["effects"][0]["fields"]) == {"self_introduction", "agent_name"}
    # The card now exists, encrypts the patch content, and has a stamped anchor.
    saved = db.get_blob(user_id, "identity")
    assert saved is not None
    assert saved["relationship_anchor_source"] == "agent_bootstrap"
    assert saved["relationship_started_at"]      # anchor stamped (today)
    assert saved["replaced_at"]                  # full-card write baseline stamped
    assert captured[-1]["self_introduction"] == "I'm your co-pilot."
    assert captured[-1]["agent_name"] == "Ori"


def test_profile_patch_no_card_nothing_writable_still_409(client, monkeypatch):
    user_id, api_key = _register(client)
    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", _no_identity_enclave)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))

    # A non-empty, valid patch that resolves to zero writable fields against an
    # empty card (an empty list value) → nothing to bootstrap → still 409.
    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.profile_patch",
                "patch": {"do_not_say": []},
            }],
        },
    )

    assert res.status_code == 409, res.get_data(as_text=True)
    assert res.get_json()["error"] == "identity_not_initialized"
    assert db.get_blob(user_id, "identity") is None  # nothing created


def test_profile_patch_existing_card_takes_update_path(client, monkeypatch):
    # An existing card must NOT be bootstrapped/replaced — it takes the normal
    # update path (preserving its relationship anchor).
    user_id, api_key = _register(client)
    db.set_blob(user_id, "identity", {
        "v": 1, "id": "identity_1", "body_ct": "old", "nonce": "old_n",
        "K_user": "old_k", "K_enclave": "old_ke", "visibility": "shared",
        "owner_user_id": user_id,
        "created_at": "2026-05-01T00:00:00", "updated_at": "2026-05-01T00:00:00",
        "relationship_started_at": "2026-04-01",
        "relationship_anchor_source": "test",
        "relationship_anchor_evidence": "seeded",
    })
    captured: list = []
    monkeypatch.setattr(
        core_enclave, "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"identity": _plain_identity()}, "") if path == "/v1/identity/get" else ({}, ""),
    )
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured))

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.profile_patch",
                "patch": {"agent_name": "阿锐"},
                "reason": "rename",
            }],
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    saved = db.get_blob(user_id, "identity")
    assert saved["id"] == "identity_1"                          # same card, updated
    assert saved["relationship_started_at"] == "2026-04-01"    # anchor preserved
    assert saved["relationship_anchor_source"] == "test"       # NOT agent_bootstrap
    assert captured[-1]["agent_name"] == "阿锐"


def test_bootstrap_honors_user_filled_relationship_days(client, monkeypatch):
    # Item 2a: a fresh-start card built via profile_patch that ALSO carries an
    # explicit relationship_days must honor it (source=user_calibrated), not
    # silently drop it while the other fields create the card.
    from datetime import date, timedelta
    from core import store as core_store
    from identity import service as identity_service

    user_id, api_key = _register(client)
    captured: list = []
    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", _no_identity_enclave)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured))

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.profile_patch",
                "patch": {"agent_name": "Ori", "self_introduction": "I'm your co-pilot.",
                          "relationship_days": 200},
                "reason": "User says we've actually known each other 200 days.",
            }],
        },
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert set(body["effects"][0]["fields"]) == {"agent_name", "self_introduction", "days_with_user"}
    saved = db.get_blob(user_id, "identity")
    assert saved["relationship_anchor_source"] == "user_calibrated"  # user-filled beats auto
    # relationship_days is 1-based ("第 N 天", met day = 第 1 天), so N=200
    # stores elapsed N-1=199 → today-199.
    assert saved["relationship_started_at"] == (date.today() - timedelta(days=199)).isoformat()
    assert identity_service._live_days_with_user(
        saved, store=core_store.get_store(user_id)) == 199


def test_bootstrap_relationship_days_only_creates_calibrated_card(client, monkeypatch):
    # A relationship_days-only patch on a fresh user is enough to create the card
    # (days_with_user is a writable field now) with the user_calibrated anchor.
    from datetime import date, timedelta
    user_id, api_key = _register(client)
    captured: list = []
    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", _no_identity_enclave)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured))

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={"actions": [{"type": "identity.profile_patch", "patch": {"relationship_days": 5}}]},
    )

    assert res.status_code == 200, res.get_data(as_text=True)
    saved = db.get_blob(user_id, "identity")
    assert saved is not None
    assert saved["relationship_anchor_source"] == "user_calibrated"
    # 1-based: N=5 stores elapsed N-1=4 → today-4.
    assert saved["relationship_started_at"] == (date.today() - timedelta(days=4)).isoformat()
