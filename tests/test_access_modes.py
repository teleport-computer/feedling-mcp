from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import access as accounts_access  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


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


def _register(client) -> tuple[str, str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["principal_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def test_whoami_does_not_rewrite_user_table_on_each_call(client, monkeypatch):
    """Regression guard for the prod lock-convoy incident.

    whoami -> _access_modes_payload used to call _save_users() — a full
    `DELETE FROM users` + re-INSERT of every row — under the global _users_lock
    on EVERY request. Under load (and amplified by a larger gunicorn thread
    pool) this serialized into a lock convoy that drove whoami p50 to ~100s and
    max to ~247s. Steady-state re-polls must now touch the users table zero
    times.
    """
    _user_id, _principal_id, api_key = _register(client)

    save_all: list = []
    upserts: list = []
    monkeypatch.setattr(db, "save_all_users", lambda users: save_all.append(1))
    monkeypatch.setattr(db, "upsert_user", lambda entry: upserts.append(entry.get("user_id")))

    # First whoami may legitimately persist once (binding flips to connected).
    assert client.get("/v1/users/whoami", headers=_headers(api_key)).status_code == 200
    save_all.clear()
    upserts.clear()

    # Steady state: repeated whoami must not rewrite the table or the row.
    for _ in range(5):
        assert client.get("/v1/users/whoami", headers=_headers(api_key)).status_code == 200

    assert save_all == [], "whoami must never call save_all_users (full-table DELETE+reINSERT)"
    assert upserts == [], "steady-state whoami must not write the user row at all"


def test_access_modes_payload_rolls_back_binding_on_persist_failure(client, monkeypatch):
    """Codex P2 guard for the conditional one-shot write.

    _access_modes_payload mutates the in-memory binding to "connected" before
    persisting it. If db.upsert_user fails transiently, the binding must be
    rolled back so the NEXT call retries — otherwise was_connected stays True and
    the write is skipped forever. (access_bindings for an api_key mode are
    rebuilt from keys by normalization, so to exercise a genuinely new binding
    we drive the onboarding route to a mode the user has no key for.)
    """
    user_id, _principal_id, api_key = _register(client)
    store = core_store.get_store(user_id)

    # Route to a mode with no api_key → its binding is new and not reconstructed
    # from keys, so _access_modes_payload must persist it. Set the blob directly
    # so the route endpoint doesn't persist the binding for us.
    route = "resident"
    db.set_blob(user_id, "onboarding_route", {"route": route})

    def has_connected(mode: str) -> bool:
        with registry._users_lock:
            e = registry._find_user_entry_locked(user_id)
            return any(
                b.get("access_mode") == mode and b.get("status") == "connected"
                for b in e.get("access_bindings") or []
            )

    real_upsert = db.upsert_user
    fail = {"on": True}
    calls = {"n": 0}

    def flaky_upsert(entry):
        calls["n"] += 1
        if fail["on"]:
            raise RuntimeError("transient DB error")
        return real_upsert(entry)

    monkeypatch.setattr(db, "upsert_user", flaky_upsert)

    # Persist fails → the new binding must be rolled back (not left connected),
    # and the call must not raise.
    accounts_access._access_modes_payload(store)
    assert calls["n"] == 1
    assert not has_connected(route), "failed persist must roll back the new binding"

    # Persist succeeds → the retry writes it; the binding now sticks.
    fail["on"] = False
    accounts_access._access_modes_payload(store)
    assert calls["n"] == 2, "binding persist must be retried after a transient failure"
    assert has_connected(route)


def test_register_creates_principal_and_access_state(client):
    user_id, principal_id, api_key = _register(client)

    who = client.get("/v1/users/whoami", headers=_headers(api_key))
    assert who.status_code == 200
    who_body = who.get_json()
    assert who_body["user_id"] == user_id
    assert who_body["principal_id"] == principal_id
    assert who_body["active_route"] == "resident"

    modes = client.get("/v1/access/modes", headers=_headers(api_key))
    assert modes.status_code == 200
    body = modes.get_json()
    assert body["user_id"] == user_id
    assert body["principal_id"] == principal_id
    assert body["active_route"] == "resident"
    assert body["api_keys_count"] == 1
    by_mode = {m["access_mode"]: m for m in body["access_modes"]}
    assert by_mode["official_import"]["connected"] is True
    assert by_mode["resident"]["connected"] is True
    assert by_mode["resident"]["active"] is True


def test_switch_access_mode_preserves_user_and_marks_binding(client):
    user_id, principal_id, api_key = _register(client)

    res = client.post(
        "/v1/access/modes/switch",
        headers=_headers(api_key),
        json={"access_mode": "api"},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["user_id"] == user_id
    assert body["principal_id"] == principal_id
    assert body["active_route"] == "model_api"
    by_mode = {m["access_mode"]: m for m in body["access_modes"]}
    assert by_mode["model_api"]["active"] is True
    assert by_mode["model_api"]["connected"] is True

    route = client.get("/v1/onboarding/route", headers=_headers(api_key)).get_json()
    assert route["route"] == "model_api"


def test_link_token_claim_issues_new_key_for_same_user(client):
    user_id, principal_id, api_key = _register(client)

    token_res = client.post(
        "/v1/access/link-token",
        headers=_headers(api_key),
        json={"access_mode": "server", "label": "local server"},
    )
    assert token_res.status_code == 201, token_res.get_data(as_text=True)
    token = token_res.get_json()["token"]

    claim = client.post(
        "/v1/access/claim-token",
        json={"token": token, "client_label": "agent runtime"},
    )
    assert claim.status_code == 201, claim.get_data(as_text=True)
    claim_body = claim.get_json()
    assert claim_body["user_id"] == user_id
    assert claim_body["principal_id"] == principal_id
    assert claim_body["api_key"] != api_key
    assert claim_body["active_route"] == "resident"

    new_who = client.get("/v1/users/whoami", headers=_headers(claim_body["api_key"]))
    assert new_who.status_code == 200
    assert new_who.get_json()["user_id"] == user_id

    old_who = client.get("/v1/users/whoami", headers=_headers(api_key))
    assert old_who.status_code == 200
    assert old_who.get_json()["principal_id"] == principal_id

    second_claim = client.post("/v1/access/claim-token", json={"token": token})
    assert second_claim.status_code == 409

    users_json = db.load_all_users()
    assert len(users_json) == 1
    assert len(users_json[0]["api_keys"]) == 2


def test_legacy_user_record_backfills_principal_and_api_keys(client):
    raw_key = "legacy-test-key"
    user_id = "usr_abcdef1234567890"
    registry._users[:] = [{
        "user_id": user_id,
        "api_key_hash": registry._hash_api_key(raw_key),
        "created_at": "2026-06-01T00:00:00",
    }]
    registry._save_users()
    registry._users[:] = []
    registry._key_to_user.clear()
    registry.load_users()

    res = client.get("/v1/access/modes", headers=_headers(raw_key))
    assert res.status_code == 200
    body = res.get_json()
    assert body["user_id"] == user_id
    assert body["principal_id"].startswith("prn_")
    assert body["api_keys_count"] == 1
