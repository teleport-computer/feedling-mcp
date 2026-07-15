"""Native /v1/identity/* parity (ASGI-migration plan §5.3 / §9).

Asserts the FastAPI routes (``identity.routes_asgi``) return the same status/body
as the Flask oracle (``identity.routes``) for every route
(get/verify/changes/init/replace/relationship_anchor/actions), plus auth-failure
(401), scope-failure (403 for the scoped ``actions`` route), and validation
paths. Both sides call the same framework-neutral ``identity.identity_core``.

E2E boundary: identity cards are v1 E2E envelopes; the server never decrypts.
The two enclave-touching paths — plaintext ``init`` (server builds an envelope
via ``core.envelope``) and ``actions`` (forwards the caller credential to the
enclave via ``identity.actions``) — are exercised with the enclave/envelope-build
functions stubbed on their shared module objects, so both frameworks hit the
identical (offline) code path and the credential forwarding is observed.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from identity import routes_asgi as identity_asgi  # noqa: E402

_SECRET = "test-asgi-identity-secret"


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    identity_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --------------------------------------------------------------------------- #
# state seed / reset
# --------------------------------------------------------------------------- #

def _seed_identity(uid: str) -> None:
    db.set_blob(uid, "identity", {
        "v": 1,
        "id": "identity_1",
        "body_ct": "seed_ct",
        "nonce": "seed_nonce",
        "K_user": "seed_k_user",
        "K_enclave": "seed_k_enclave",
        "enclave_pk_fpr": "fpr",
        "visibility": "shared",
        "owner_user_id": uid,
        "created_at": "2026-05-31T00:00:00",
        "updated_at": "2026-05-31T00:00:00",
        "relationship_started_at": "2026-04-01",
        "relationship_anchor_source": "test",
        "relationship_anchor_evidence": "seeded identity for test",
    })


def _reset_identity(uid: str) -> None:
    db.delete_blob(uid, "identity")
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM user_logs WHERE user_id = %s AND stream = %s",
            (uid, "identity_changes"),
        )


def _env(uid: str, entry_id: str = "identity_1") -> dict:
    return {
        "id": entry_id,
        "body_ct": "ct-new",
        "nonce": "nonce-new",
        "K_user": "k-user-new",
        "K_enclave": "k-enclave-new",
        "enclave_pk_fpr": "fpr-new",
        "visibility": "shared",
        "owner_user_id": uid,
    }


def _plain_identity() -> dict:
    return {
        "agent_name": "bro",
        "self_introduction": "I keep the real thread with you.",
        "dimensions": [{"name": "Context retention", "value": 88, "description": "keeps context"}],
    }


def _fake_envelope_builder(captured: list):
    def _build(store, plaintext: bytes, item_id: str | None = None):
        captured.append(plaintext)
        return {
            "id": item_id or "built_env",
            "body_ct": "built_ct",
            "nonce": "built_nonce",
            "K_user": "built_k_user",
            "K_enclave": "built_k_enclave",
            "visibility": "shared",
            "owner_user_id": store.user_id,
            "enclave_pk_fpr": "built_fpr",
        }, ""
    return _build


# --------------------------------------------------------------------------- #
# request helpers
# --------------------------------------------------------------------------- #

def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _flask(method: str, path: str, *, headers=None, json_body=None):
    client = make_client()
    res = client.open(path, method=method, headers=headers or {}, json=json_body)
    return res.status_code, res.get_json(silent=True)


def _asgi(method: str, path: str, *, headers=None, json_body=None):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            kw: dict = {}
            if json_body is not None:
                kw["json"] = json_body
            resp = await client.request(method, path, headers=headers or {}, **kw)
            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = None
            return resp.status_code, body

    return asyncio.run(go())


def _mint(uid: str, scope) -> str:
    return runtime_token.mint(
        _SECRET.encode("utf-8"),
        user_id=uid,
        runtime_instance_id="ri_test",
        scope=scope,
        ttl=900.0,
    )


def _norm_ident(body):
    """Blank volatile created_at/updated_at/replaced_at inside an identity payload.
    replaced_at (Task 3 / P5 baseline) is stamped from `datetime.now()` at
    init/replace time, same as created_at/updated_at — volatile across the two
    separately-timed Flask/ASGI calls this helper compares."""
    body = copy.deepcopy(body)
    ident = body.get("identity") if isinstance(body, dict) else None
    if isinstance(ident, dict):
        for k in ("created_at", "updated_at", "replaced_at"):
            if k in ident:
                ident[k] = "<ts>"
    return body


# --------------------------------------------------------------------------- #
# auth parity (401) — one per route
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/identity/get"),
    ("GET", "/v1/identity/verify"),
    ("GET", "/v1/identity/changes"),
    ("POST", "/v1/identity/init"),
    ("POST", "/v1/identity/replace"),
    ("POST", "/v1/identity/relationship_anchor"),
    ("POST", "/v1/identity/actions"),
])
def test_no_auth_is_401_parity(user, method, path):
    jb = {} if method == "POST" else None
    f = _flask(method, path, json_body=jb)
    a = _asgi(method, path, json_body=jb)
    assert f == a == (401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# get parity
# --------------------------------------------------------------------------- #

def test_get_empty_parity(user):
    _uid, api_key = user
    f = _flask("GET", "/v1/identity/get", headers=_headers(api_key))
    a = _asgi("GET", "/v1/identity/get", headers=_headers(api_key))
    assert f == a == (200, {"identity": None})


def test_get_seeded_parity(user):
    uid, api_key = user
    _seed_identity(uid)
    f = _flask("GET", "/v1/identity/get", headers=_headers(api_key))
    a = _asgi("GET", "/v1/identity/get", headers=_headers(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["identity"]["id"] == "identity_1"
    assert "days_with_user" in f[1]["identity"]


# --------------------------------------------------------------------------- #
# verify parity
# --------------------------------------------------------------------------- #

def test_verify_unwritten_parity(user):
    _uid, api_key = user
    f = _flask("GET", "/v1/identity/verify", headers=_headers(api_key))
    a = _asgi("GET", "/v1/identity/verify", headers=_headers(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["written"] is False


def test_verify_written_parity(user):
    uid, api_key = user
    _seed_identity(uid)
    f = _flask("GET", "/v1/identity/verify", headers=_headers(api_key))
    a = _asgi("GET", "/v1/identity/verify", headers=_headers(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["written"] is True
    assert f[1]["passing"] is True


# --------------------------------------------------------------------------- #
# changes parity
# --------------------------------------------------------------------------- #

def test_changes_empty_parity(user):
    _uid, api_key = user
    f = _flask("GET", "/v1/identity/changes", headers=_headers(api_key))
    a = _asgi("GET", "/v1/identity/changes", headers=_headers(api_key))
    assert f == a == (200, {"changes": [], "total": 0})


def test_changes_invalid_limit_400_parity(user):
    _uid, api_key = user
    f = _flask("GET", "/v1/identity/changes?limit=abc", headers=_headers(api_key))
    a = _asgi("GET", "/v1/identity/changes?limit=abc", headers=_headers(api_key))
    assert f == a == (400, {"error": "invalid limit"})


def test_changes_seeded_parity(user):
    uid, api_key = user
    # Deterministic log rows (fixed id + ts) so both frameworks read identical data.
    db.log_append(uid, "identity_changes", {"id": "c1", "ts": "2026-06-01T00:00:00", "action": "init", "reason": "first"})
    db.log_append(uid, "identity_changes", {"id": "c2", "ts": "2026-06-02T00:00:00", "action": "replace", "reason": "second"})
    f = _flask("GET", "/v1/identity/changes", headers=_headers(api_key))
    a = _asgi("GET", "/v1/identity/changes", headers=_headers(api_key))
    assert f == a
    assert f[0] == 200
    assert f[1]["total"] == 2
    assert [c["id"] for c in f[1]["changes"]] == ["c2", "c1"]  # newest-first

    # `since` filter parity
    f2 = _flask("GET", "/v1/identity/changes?since=2026-06-01T12:00:00", headers=_headers(api_key))
    a2 = _asgi("GET", "/v1/identity/changes?since=2026-06-01T12:00:00", headers=_headers(api_key))
    assert f2 == a2
    assert [c["id"] for c in f2[1]["changes"]] == ["c2"]


# --------------------------------------------------------------------------- #
# init parity
# --------------------------------------------------------------------------- #

def test_init_missing_body_400_parity(user):
    _uid, api_key = user
    f = _flask("POST", "/v1/identity/init", headers=_headers(api_key), json_body={})
    a = _asgi("POST", "/v1/identity/init", headers=_headers(api_key), json_body={})
    assert f == a == (400, {"error": "envelope or identity required"})


def test_init_owner_mismatch_403_parity(user):
    uid, api_key = user
    body = {
        "envelope": _env("other-user"),
        "days_with_user": 30,
        "relationship_anchor_evidence": "session transcript pointer",
    }
    f = _flask("POST", "/v1/identity/init", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/identity/init", headers=_headers(api_key), json_body=body)
    assert f == a == (403, {"error": "envelope.owner_user_id does not match caller"})


def test_init_already_initialized_409_parity(user):
    uid, api_key = user
    _seed_identity(uid)
    body = {
        "envelope": _env(uid),
        "days_with_user": 30,
        "relationship_anchor_evidence": "session transcript pointer",
    }
    f = _flask("POST", "/v1/identity/init", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/identity/init", headers=_headers(api_key), json_body=body)
    assert f == a
    assert f[0] == 409
    assert f[1]["error"] == "already_initialized"


def test_init_envelope_happy_parity(user):
    uid, api_key = user
    body = {
        "envelope": _env(uid),
        "days_with_user": 30,
        "relationship_anchor_evidence": "session transcript pointer",
    }
    f = _flask("POST", "/v1/identity/init", headers=_headers(api_key), json_body=body)
    _reset_identity(uid)
    a = _asgi("POST", "/v1/identity/init", headers=_headers(api_key), json_body=body)
    assert _norm_ident(f[1]) == _norm_ident(a[1])
    assert f[0] == a[0] == 201
    assert f[1]["identity"]["owner_user_id"] == uid
    assert f[1]["identity"]["relationship_anchor_source"] == "days_with_user"


def test_init_plaintext_server_build_parity(user, monkeypatch):
    uid, api_key = user
    captured: list = []
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder(captured))
    body = {
        "identity": _plain_identity(),
        "days_with_user": 12,
        "relationship_anchor_evidence": "user confirmed fresh start",
    }
    f = _flask("POST", "/v1/identity/init", headers=_headers(api_key), json_body=body)
    _reset_identity(uid)
    a = _asgi("POST", "/v1/identity/init", headers=_headers(api_key), json_body=body)
    assert _norm_ident(f[1]) == _norm_ident(a[1])
    assert f[0] == a[0] == 201
    # E2E: the server built the envelope from plaintext via core.envelope (the
    # enclave-owned path); the persisted card carries the built ciphertext.
    assert f[1]["identity"]["body_ct"] == "built_ct"
    assert captured, "envelope builder must have been invoked with plaintext"


# --------------------------------------------------------------------------- #
# replace parity
# --------------------------------------------------------------------------- #

def test_replace_missing_envelope_400_parity(user):
    uid, api_key = user
    _seed_identity(uid)
    f = _flask("POST", "/v1/identity/replace", headers=_headers(api_key), json_body={})
    a = _asgi("POST", "/v1/identity/replace", headers=_headers(api_key), json_body={})
    assert f == a
    assert f[0] == 400
    assert "envelope required" in f[1]["error"]


def test_replace_happy_preserves_created_at_parity(user):
    uid, api_key = user
    body = {"envelope": _env(uid)}
    _seed_identity(uid)
    f = _flask("POST", "/v1/identity/replace", headers=_headers(api_key), json_body=body)
    _reset_identity(uid)
    _seed_identity(uid)
    a = _asgi("POST", "/v1/identity/replace", headers=_headers(api_key), json_body=body)
    assert _norm_ident(f[1]) == _norm_ident(a[1])
    assert f[0] == a[0] == 200
    assert f[1]["status"] == "replaced"
    # created_at preserved from the pre-existing seed; anchor preserved too.
    assert f[1]["identity"]["created_at"] == "2026-05-31T00:00:00"
    assert f[1]["identity"]["relationship_started_at"] == "2026-04-01"


def test_replace_no_prior_anchor_400_parity(user):
    uid, api_key = user
    body = {"envelope": _env(uid)}
    f = _flask("POST", "/v1/identity/replace", headers=_headers(api_key), json_body=body)
    a = _asgi("POST", "/v1/identity/replace", headers=_headers(api_key), json_body=body)
    assert f == a
    assert f[0] == 400
    assert "no relationship anchor on file" in f[1]["error"]


# --------------------------------------------------------------------------- #
# relationship_anchor parity
# --------------------------------------------------------------------------- #

def test_anchor_not_initialized_404_parity(user):
    _uid, api_key = user
    f = _flask("POST", "/v1/identity/relationship_anchor", headers=_headers(api_key), json_body={"days_with_user": 5})
    a = _asgi("POST", "/v1/identity/relationship_anchor", headers=_headers(api_key), json_body={"days_with_user": 5})
    assert f == a == (404, {"error": "identity not initialized"})


def test_anchor_invalid_days_400_parity(user):
    uid, api_key = user
    _seed_identity(uid)
    f = _flask("POST", "/v1/identity/relationship_anchor", headers=_headers(api_key), json_body={"days_with_user": -1})
    a = _asgi("POST", "/v1/identity/relationship_anchor", headers=_headers(api_key), json_body={"days_with_user": -1})
    assert f == a == (400, {"error": "days_with_user (non-negative int) required"})


def test_anchor_happy_parity(user):
    uid, api_key = user
    body = {"days_with_user": 7}
    _seed_identity(uid)
    f = _flask("POST", "/v1/identity/relationship_anchor", headers=_headers(api_key), json_body=body)
    _reset_identity(uid)
    _seed_identity(uid)
    a = _asgi("POST", "/v1/identity/relationship_anchor", headers=_headers(api_key), json_body=body)
    # relationship_started_at is a calendar date (today - days) — deterministic.
    assert f == a
    assert f[0] == 200
    assert f[1]["status"] == "updated"


# --------------------------------------------------------------------------- #
# actions parity (scoped route)
# --------------------------------------------------------------------------- #

def test_actions_invalid_body_400_parity(user):
    _uid, api_key = user
    f = _flask("POST", "/v1/identity/actions", headers=_headers(api_key), json_body={})
    a = _asgi("POST", "/v1/identity/actions", headers=_headers(api_key), json_body={})
    assert f == a == (400, {"error": "actions required"})


def test_actions_scope_denied_403_parity(user, monkeypatch):
    uid, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    # Runtime token WITHOUT the "identity" scope must 403 on both frameworks.
    tok = _mint(uid, scope=["chat", "memory"])
    hdr = {"X-Feedling-Runtime-Token": tok}
    f = _flask("POST", "/v1/identity/actions", headers=hdr, json_body={"actions": []})
    a = _asgi("POST", "/v1/identity/actions", headers=hdr, json_body={"actions": []})
    assert f == a == (403, {"error": "forbidden"})


def test_actions_scope_allowed_parity(user, monkeypatch):
    uid, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    # Token WITH the "identity" scope clears the gate; empty actions list then
    # hits the core's actions_required error (200/gate cleared -> 400 body).
    tok = _mint(uid, scope=["identity"])
    hdr = {"X-Feedling-Runtime-Token": tok}
    f = _flask("POST", "/v1/identity/actions", headers=hdr, json_body={"actions": []})
    a = _asgi("POST", "/v1/identity/actions", headers=hdr, json_body={"actions": []})
    assert f == a
    # empty list is a list, so it reaches _execute_identity_actions -> 400 actions_required
    assert f[0] == 400
    assert f[1]["error"] == "actions_required"


def test_actions_profile_patch_forwards_credential_parity(user, monkeypatch):
    uid, api_key = user
    captured_keys: list = []

    def fake_enclave_get(path, key, params=None, runtime_token=""):
        captured_keys.append(key)
        if path == "/v1/identity/get":
            return {"identity": _plain_identity()}, ""
        return {}, ""

    monkeypatch.setattr(core_enclave, "_enclave_get_json_for_gate", fake_enclave_get)
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder([]))

    action_body = {"actions": [{"type": "identity.profile_patch", "patch": {"agent_name": "小秘"}, "reason": "rename"}]}

    _seed_identity(uid)
    f = _flask("POST", "/v1/identity/actions", headers=_headers(api_key), json_body=action_body)
    _reset_identity(uid)
    _seed_identity(uid)
    a = _asgi("POST", "/v1/identity/actions", headers=_headers(api_key), json_body=action_body)

    def _norm_actions(resp):
        status, body = resp
        body = copy.deepcopy(body) or {}
        for r in body.get("results", []):
            if isinstance(r, dict):
                if isinstance(r.get("identity"), dict):
                    r["identity"]["updated_at"] = "<ts>"
                if isinstance(r.get("change"), dict):
                    r["change"]["id"] = "<id>"
                    r["change"]["ts"] = "<ts>"
        for e in body.get("effects", []):
            if isinstance(e, dict):
                e["change_id"] = "<id>"
        return status, body

    assert _norm_actions(f) == _norm_actions(a)
    assert f[0] == 200
    assert f[1]["status"] == "ok"
    assert f[1]["results"][0]["changed_fields"] == ["agent_name"]
    # Credential forwarding: the api key from the caller reached the enclave call
    # on BOTH frameworks (the E2E boundary — server never decrypts locally).
    assert captured_keys and all(k == api_key for k in captured_keys)
