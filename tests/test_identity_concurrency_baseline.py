"""P5 concurrency baseline (Task 3): the outer identity field ``replaced_at``
must be stamped ONLY by full-card writes — identity init and full replace
(both the envelope route and the plaintext ``identity.replace`` action) — and
must NEVER move on partial mutations (``identity.profile_patch``,
``identity.dimension_nudge``, ``identity.relationship_days_set``, the
``/v1/identity/relationship_anchor`` route). Later tasks compare a job's
start time against ``replaced_at`` to detect "a full replace happened after
this job started" conflicts; a patch/nudge/anchor-recalibration must never
look like a conflict.

Fixtures mirror tests/test_identity_actions.py (client/register/seed pattern)
and the resident-distill job gate from tests/test_identity_replace_action.py.
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
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from genesis import service as genesis_service  # noqa: E402
from hosted import history_import  # noqa: E402


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


def _plain_identity(agent_name: str = "bro") -> dict:
    return {
        "agent_name": agent_name,
        "self_introduction": "I keep the real thread with you.",
        "dimensions": [
            {"name": "Signal sensitivity", "value": 92, "description": "Notices subtle shifts."},
            {"name": "Context retention", "value": 88, "description": "Keeps prior context."},
        ],
    }


_SEEDED_REPLACED_AT = "2026-05-31T00:00:00"


def _seed_identity(user_id: str, *, replaced_at: str = _SEEDED_REPLACED_AT) -> None:
    """Mirrors test_identity_actions._seed_identity. Passing replaced_at=""
    seeds a legacy identity (pre-Task-3) with no stamp at all, for the
    back-compat check."""
    doc = {
        "v": 1,
        "id": "identity_1",
        "body_ct": "old",
        "nonce": "old_nonce",
        "K_user": "old_k_user",
        "K_enclave": "old_k_enclave",
        "visibility": "shared",
        "owner_user_id": user_id,
        "created_at": "2026-05-31T00:00:00",
        "updated_at": "2026-05-31T00:00:00",
        "relationship_started_at": "2026-04-01",
        "relationship_anchor_source": "test",
        "relationship_anchor_evidence": "seeded identity for test",
    }
    if replaced_at:
        doc["replaced_at"] = replaced_at
    db.set_blob(user_id, "identity", doc)


def _env(user_id: str, entry_id: str = "identity_1") -> dict:
    return {
        "id": entry_id,
        "body_ct": "ct-new",
        "nonce": "nonce-new",
        "K_user": "k-user-new",
        "K_enclave": "k-enclave-new",
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def _fake_envelope_builder(captured: list):
    def _build(store, plaintext: bytes, item_id: str | None = None):
        try:
            captured.append(json.loads(plaintext.decode("utf-8")))
        except Exception:
            captured.append(plaintext.decode("utf-8"))
        envelope = {
            "id": item_id or "env_1",
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


# --------------------------------------------------------------------------- #
# full-card writes: replaced_at IS stamped / DOES move
# --------------------------------------------------------------------------- #

def test_init_stamps_replaced_at(client):
    user_id, api_key = _register(client)
    res = client.post(
        "/v1/identity/init",
        headers=_headers(api_key),
        json={
            "envelope": _env(user_id),
            "days_with_user": 10,
            "relationship_anchor_evidence": "transcript: earliest chat dated 2026-05-08",
        },
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    replaced_at = body["identity"].get("replaced_at")
    assert replaced_at
    # At init, replaced_at is stamped from the same `now` as created_at/updated_at.
    assert replaced_at == body["identity"]["created_at"] == body["identity"]["updated_at"]
    saved = db.get_blob(user_id, "identity")
    assert saved["replaced_at"] == replaced_at


def test_envelope_replace_route_changes_replaced_at(client):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    res = client.post(
        "/v1/identity/replace",
        headers=_headers(api_key),
        json={"envelope": _env(user_id)},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    new_replaced_at = body["identity"].get("replaced_at")
    assert new_replaced_at
    assert new_replaced_at != _SEEDED_REPLACED_AT
    saved = db.get_blob(user_id, "identity")
    assert saved["replaced_at"] == new_replaced_at


def test_identity_replace_action_changes_replaced_at(client, monkeypatch):
    # The plaintext identity.replace action (consumer path: VPS resident-distill
    # locally re-derives identity and posts the plaintext card). It delegates to
    # genesis_service.replace_identity_preserving_anchor, which builds its OWN
    # identity dict independent of identity_core.replace_identity — verified by
    # trace, and stamped separately (see task-3-report.md).
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    captured: list = []
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured))

    job_id = "job_replace_1"
    db.genesis_create_job(user_id, {"job_id": job_id, "status": "awaiting_resident"})
    db.genesis_claim_resident_jobs(user_id, consumer_id="cons-test")

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.replace",
                "source": "genesis_resident_distill",
                "job_id": job_id,
                "reason": "resident redefined persona from local distill",
                "identity": _plain_identity(agent_name="Nyx"),
            }],
        },
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["status"] == "ok"
    saved = db.get_blob(user_id, "identity")
    assert saved.get("replaced_at")
    assert saved["replaced_at"] != _SEEDED_REPLACED_AT


def test_init_identity_if_absent_stamps_replaced_at_on_init(client, monkeypatch):
    # Genesis import path (no prior identity): genesis_service.init_identity_if_absent
    # builds its own explicit-field identity dict and raw-saves via
    # identity_service._save_identity, independent of identity_core.init_identity.
    # It must stamp replaced_at from the same `now` as created_at/updated_at too.
    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)
    monkeypatch.setattr(genesis_service.core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))

    status = genesis_service.init_identity_if_absent(
        store,
        {
            "identity": {
                "agent_name": "Mira",
                "dimensions": [{"name": "Steady", "value": 84, "description": "Persona says steady."}],
            },
            "relationship_started_at": "2026-06-01",
            "relationship_anchor_evidence": "persona card named Mira",
        },
    )
    assert status == "initialized"
    saved = db.get_blob(user_id, "identity")
    assert saved.get("replaced_at")
    assert saved["replaced_at"] == saved["created_at"] == saved["updated_at"]


def test_init_identity_if_absent_stamps_replaced_at_on_update(client, monkeypatch):
    # Genesis "update" branch (existing identity, still genesis-owned): must move
    # replaced_at just like the envelope /v1/identity/replace route does.
    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)
    _seed_identity(user_id)
    db.set_blob(user_id, "identity", {**db.get_blob(user_id, "identity"), "relationship_anchor_source": genesis_service.GENESIS_SOURCE})
    monkeypatch.setattr(genesis_service.core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))
    monkeypatch.setattr(
        genesis_service,
        "_existing_identity_plain_for_update",
        lambda *_a, **_k: (None, "identity_plain_not_available"),
    )

    status = genesis_service.init_identity_if_absent(
        store,
        {
            "identity": {
                "agent_name": "Mira",
                "dimensions": [{"name": "Steady", "value": 84, "description": "Persona says steady."}],
            },
            "relationship_started_at": "2026-06-01",
            "relationship_anchor_evidence": "persona card named Mira",
        },
    )
    assert status == "updated"
    saved = db.get_blob(user_id, "identity")
    assert saved.get("replaced_at")
    assert saved["replaced_at"] != _SEEDED_REPLACED_AT


def test_store_identity_payload_stamps_replaced_at(client, monkeypatch):
    # hosted/history_import.py::_store_identity_payload (Model API history import):
    # same shape as the two genesis paths above - explicit-field dict, raw-save via
    # identity_service._save_identity. Must also stamp replaced_at.
    user_id, api_key = _register(client)
    store = core_store.get_store(user_id)
    monkeypatch.setattr(history_import.core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))

    identity = history_import._store_identity_payload(
        store,
        _plain_identity(),
        days_with_user=10,
        evidence="history_import:job_1 relationship_started_at=2026-06-01",
        language="en",
        relationship_started_at="2026-06-01",
    )
    assert identity.get("replaced_at")
    assert identity["replaced_at"] == identity["created_at"] == identity["updated_at"]
    saved = db.get_blob(user_id, "identity")
    assert saved["replaced_at"] == identity["replaced_at"]


# --------------------------------------------------------------------------- #
# partial mutations: replaced_at is left UNTOUCHED
# --------------------------------------------------------------------------- #

def test_profile_patch_does_not_change_replaced_at(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    monkeypatch.setattr(
        core_enclave,
        "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"identity": _plain_identity()}, "") if path == "/v1/identity/get" else ({}, ""),
    )
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.profile_patch",
                "patch": {"agent_name": "小秘"},
                "reason": "User asked for a displayed name change.",
            }],
        },
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    saved = db.get_blob(user_id, "identity")
    assert saved["replaced_at"] == _SEEDED_REPLACED_AT


def test_dimension_nudge_does_not_change_replaced_at(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    monkeypatch.setattr(
        core_enclave,
        "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"identity": _plain_identity()}, "") if path == "/v1/identity/get" else ({}, ""),
    )
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.dimension_nudge",
                "dimension": "Signal sensitivity",  # value 92 in _plain_identity()
                "delta": 2,
                "reason": "test",
            }],
        },
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    saved = db.get_blob(user_id, "identity")
    assert saved["replaced_at"] == _SEEDED_REPLACED_AT


def test_relationship_days_set_does_not_change_replaced_at(client):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={"actions": [{"type": "identity.relationship_days_set", "days_with_user": 5}]},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    saved = db.get_blob(user_id, "identity")
    assert saved["replaced_at"] == _SEEDED_REPLACED_AT


def test_relationship_anchor_route_does_not_change_replaced_at(client):
    # /v1/identity/relationship_anchor -> identity_core.update_relationship_anchor,
    # a third partial-mutation path (not routed through identity/actions.py at all).
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    res = client.post(
        "/v1/identity/relationship_anchor",
        headers=_headers(api_key),
        json={"days_with_user": 7},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    saved = db.get_blob(user_id, "identity")
    assert saved["replaced_at"] == _SEEDED_REPLACED_AT


# --------------------------------------------------------------------------- #
# Task 5: identity.replace optimistic concurrency (base_identity_replaced_at)
# --------------------------------------------------------------------------- #

def _replace_action(job_id: str, *, agent_name: str = "Nyx", base_identity_replaced_at=None) -> dict:
    action = {
        "type": "identity.replace",
        "source": "genesis_resident_distill",
        "job_id": job_id,
        "reason": "resident redefined persona from local distill",
        "identity": _plain_identity(agent_name=agent_name),
    }
    if base_identity_replaced_at is not None:
        action["base_identity_replaced_at"] = base_identity_replaced_at
    return action


def test_replace_action_with_matching_baseline_succeeds(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))

    job_id = "job_baseline_match"
    db.genesis_create_job(user_id, {"job_id": job_id, "status": "awaiting_resident"})
    db.genesis_claim_resident_jobs(user_id, consumer_id="cons-test")

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={"actions": [_replace_action(job_id, base_identity_replaced_at=_SEEDED_REPLACED_AT)]},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    saved = db.get_blob(user_id, "identity")
    assert saved["replaced_at"] != _SEEDED_REPLACED_AT


def test_replace_action_with_stale_baseline_returns_409(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))

    job_id = "job_baseline_stale"
    db.genesis_create_job(user_id, {"job_id": job_id, "status": "awaiting_resident"})
    db.genesis_claim_resident_jobs(user_id, consumer_id="cons-test")

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={"actions": [_replace_action(job_id, base_identity_replaced_at="2020-01-01T00:00:00")]},
    )
    assert res.status_code == 409, res.get_data(as_text=True)
    body = res.get_json()
    assert body["results"][0]["error"] == "identity_base_stale"
    # rejected — the card must NOT have been touched.
    saved = db.get_blob(user_id, "identity")
    assert saved["replaced_at"] == _SEEDED_REPLACED_AT


def test_replace_action_without_baseline_skips_check(client, monkeypatch):
    # Back-compat: every existing caller (and this action itself, when it omits the field)
    # must not be gated by a check it never opted into.
    user_id, api_key = _register(client)
    _seed_identity(user_id)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))

    job_id = "job_baseline_absent"
    db.genesis_create_job(user_id, {"job_id": job_id, "status": "awaiting_resident"})
    db.genesis_claim_resident_jobs(user_id, consumer_id="cons-test")

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={"actions": [_replace_action(job_id)]},  # no base_identity_replaced_at key at all
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    saved = db.get_blob(user_id, "identity")
    assert saved["replaced_at"] != _SEEDED_REPLACED_AT


# --------------------------------------------------------------------------- #
# back-compat: a legacy identity with no replaced_at at all must not KeyError
# --------------------------------------------------------------------------- #

def test_profile_patch_on_legacy_identity_without_replaced_at_does_not_crash(client, monkeypatch):
    user_id, api_key = _register(client)
    _seed_identity(user_id, replaced_at="")  # pre-Task-3 identity: field absent
    monkeypatch.setattr(
        core_enclave,
        "_enclave_get_json_for_gate",
        lambda path, key, params=None: ({"identity": _plain_identity()}, "") if path == "/v1/identity/get" else ({}, ""),
    )
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))

    res = client.post(
        "/v1/identity/actions",
        headers=_headers(api_key),
        json={
            "actions": [{
                "type": "identity.profile_patch",
                "patch": {"agent_name": "小秘"},
                "reason": "rename",
            }],
        },
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    saved = db.get_blob(user_id, "identity")
    # Missing stays missing/empty — never a KeyError, never silently invented.
    assert saved.get("replaced_at", "") == ""
