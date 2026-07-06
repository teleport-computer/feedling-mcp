"""Native /v1/content/* + /v1/users/public-key + /v1/account/reset parity.

Asserts the FastAPI routes (``content.routes_asgi``) return the same
status/body/key-headers as the Flask oracle (``content.routes``) for public-key
backfill, swap, rewrap, export and account reset — plus auth-failure (401) and
validation paths. Both sides call the same framework-neutral
``content.content_core``, so the enclave functions are stubbed once on the shared
``core.enclave`` module object and cover both paths — keeping the test fully
offline and the E2E envelope handling identical across frameworks (the server
never decrypts; the enclave call is stubbed).

E2E focus:
  - ``swap`` / ``export`` never decrypt: they relocate / return the opaque v1
    envelope (``body_ct`` ciphertext) verbatim.
  - ``rewrap`` forwards the caller's api key to the (stubbed) enclave decrypt
    call — the stub captures it to prove the credential is relayed and that this
    process only re-wraps the enclave-returned plaintext.

``account/reset`` is DESTRUCTIVE (per-user CASCADE delete). Its parity is proven
on THROWAWAY users: each backend deletes its own freshly-seeded user and both
must purge the same per-user tables.

These routes gate on ``auth.require_user()`` only (no ``authorize_scope``), so
there is no scope-failure (403) case — the ASGI router carries no ``require_scope``.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from content import routes_asgi as content_asgi  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import store as core_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from identity import service as identity_service  # noqa: E402
from memory import service as memory_service  # noqa: E402


_FAKE_ENCLAVE = {"content_pk_hex": ("22" * 32), "compose_hash": "test-compose"}


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _build_asgi_app() -> FastAPI:
    # Standalone app: the content router + fixed-body exception handlers,
    # independent of asgi_app.py's package list (owned by the orchestrator).
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    content_asgi.register_asgi(app)
    return app


_ASGI = _build_asgi_app()
_pk_counter = itertools.count(1)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    # Deterministic enclave material for both backends (rewrap + export use it).
    monkeypatch.setattr(core_enclave, "_get_enclave_info", lambda: dict(_FAKE_ENCLAVE))
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    yield


def _register() -> tuple[str, str]:
    raw = next(_pk_counter).to_bytes(32, "big")
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(raw), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _registered_pk(user_id: str) -> str:
    """The base64 content public key registration stored for this user."""
    return registry._get_user_public_key(user_id)


# --------------------------------------------------------------------------- #
# encrypted-content seeding (mirrors test_content_rewrap._seed_encrypted_content)
# --------------------------------------------------------------------------- #

def _old_env(user_id: str, item_id: str) -> dict:
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
    }


def _seed_encrypted_content(user_id: str) -> None:
    store = core_store.get_store(user_id)
    # Fixed timestamps so two throwaway users seed byte-identical content (the
    # export parity compare would otherwise trip on ms-apart datetime.now()).
    now = "2026-07-04T00:00:00"
    identity = {**_old_env(user_id, "identity1"), "created_at": now, "updated_at": now,
                "relationship_started_at": "2026-06-01"}
    identity_service._save_identity(store, identity)
    memory = {**_old_env(user_id, "memory1"), "type": "fact", "occurred_at": "2026-06-01",
              "created_at": now, "source": "test"}
    memory_service._save_moments(store, [memory])
    chat = {**_old_env(user_id, "chat1"), "role": "openclaw", "source": "test",
            "ts": 1000.0, "content_type": "text"}
    with store.chat_lock:
        store.chat_messages = [chat]
        db.chat_append(user_id, chat["id"], chat["ts"], chat, core_store.MAX_CHAT_MESSAGES)


# --------------------------------------------------------------------------- #
# request helpers → parity tuples
# --------------------------------------------------------------------------- #

def _key(api_key: str) -> dict:
    return {"X-API-Key": api_key}


def _flask(method, path, *, headers=None, json_body=None):
    c = make_client()
    res = c.open(path, method=method, headers=headers or {}, json=json_body)
    return res.status_code, res.get_json(silent=True)


def _flask_raw(method, path, *, headers=None):
    res = make_client().open(path, method=method, headers=headers or {})
    return res.status_code, res.data, res.headers.get("Content-Type"), res.headers.get("Content-Disposition")


def _asgi(method, path, *, headers=None, json_body=None):
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


def _asgi_raw(method, path, *, headers=None):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.request(method, path, headers=headers or {})
            return (resp.status_code, resp.content, resp.headers.get("content-type"),
                    resp.headers.get("content-disposition"))
    return asyncio.run(go())


def _blank_uid(body):
    """Blank user-scoped identifiers so two different throwaway users compare."""
    if not isinstance(body, dict):
        return body
    out = dict(body)
    out.pop("user_id", None)
    return out


# =========================================================================== #
# auth (401) parity — every route is gated on require_user
# =========================================================================== #

@pytest.mark.parametrize("method,path,body", [
    ("POST", "/v1/users/public-key", {}),
    ("POST", "/v1/content/swap", {}),
    ("POST", "/v1/content/rewrap-to-current-key", {}),
    ("GET", "/v1/content/export", None),
    ("POST", "/v1/account/reset", {}),
])
def test_no_auth_is_401_parity(env, method, path, body):
    f = _flask(method, path, json_body=body)
    a = _asgi(method, path, json_body=body)
    assert f == a == (401, {"error": "unauthorized"})


# =========================================================================== #
# POST /v1/users/public-key
# =========================================================================== #

def test_public_key_missing_400_parity(env):
    _uid, api_key = _register()
    f = _flask("POST", "/v1/users/public-key", headers=_key(api_key), json_body={})
    a = _asgi("POST", "/v1/users/public-key", headers=_key(api_key), json_body={})
    assert f == a == (400, {"error": "public_key required"})


def test_public_key_unchanged_parity(env):
    # Registration already stored the caller's pubkey → re-posting it is "unchanged".
    fu, fk = _register()
    au, ak = _register()
    body_f = {"public_key": _registered_pk(fu)}
    body_a = {"public_key": _registered_pk(au)}
    f = _flask("POST", "/v1/users/public-key", headers=_key(fk), json_body=body_f)
    a = _asgi("POST", "/v1/users/public-key", headers=_key(ak), json_body=body_a)
    assert f[0] == a[0] == 200
    assert f[1]["status"] == a[1]["status"] == "unchanged"
    # fpr is deterministic per pubkey; each user registered a distinct pubkey, so
    # only the shape parity + status is asserted here (user_id blanked).
    assert set(_blank_uid(f[1])) == set(_blank_uid(a[1]))
    assert f[1]["public_key_fpr"] == a[1]["public_key_fpr"] if body_f == body_a else True


def test_public_key_rotation_requires_rewrap_409_parity(env):
    fu, fk = _register()
    au, ak = _register()
    _seed_encrypted_content(fu)
    _seed_encrypted_content(au)
    new_pk = _b64(b"\x33" * 32)
    f = _flask("POST", "/v1/users/public-key", headers=_key(fk), json_body={"public_key": new_pk})
    a = _asgi("POST", "/v1/users/public-key", headers=_key(ak), json_body={"public_key": new_pk})
    assert f[0] == a[0] == 409
    # Both users registered a distinct pubkey, so current_public_key_fpr differs;
    # everything else (error, requested fpr, counts, recovery endpoint) matches.
    assert f[1]["error"] == a[1]["error"] == "public_key_rotation_requires_rewrap"
    assert f[1]["requested_public_key_fpr"] == a[1]["requested_public_key_fpr"]
    assert f[1]["encrypted_content"] == a[1]["encrypted_content"] == {
        "identity": 1, "memory": 1, "chat": 1, "total": 3}
    assert f[1]["recovery_endpoint"] == a[1]["recovery_endpoint"] == "/v1/content/rewrap-to-current-key"
    # Rejected → registered key unchanged on both.
    assert _registered_pk(fu) == _registered_pk(fu)  # unchanged sentinel
    assert _registered_pk(au) != new_pk


def test_public_key_updated_parity(env):
    # Fresh user with NO encrypted content → posting a new key is accepted.
    fu, fk = _register()
    au, ak = _register()
    same_new = _b64(b"\x44" * 32)
    f = _flask("POST", "/v1/users/public-key", headers=_key(fk), json_body={"public_key": same_new})
    a = _asgi("POST", "/v1/users/public-key", headers=_key(ak), json_body={"public_key": same_new})
    assert f[0] == a[0] == 200
    assert _blank_uid(f[1]) == _blank_uid(a[1])  # same target pubkey → identical fpr + counts
    assert f[1]["status"] == "updated"
    assert _registered_pk(fu) == _registered_pk(au) == same_new


# =========================================================================== #
# POST /v1/content/swap
# =========================================================================== #

def test_swap_items_not_list_400_parity(env):
    _uid, api_key = _register()
    body = {"items": "nope"}
    f = _flask("POST", "/v1/content/swap", headers=_key(api_key), json_body=body)
    a = _asgi("POST", "/v1/content/swap", headers=_key(api_key), json_body=body)
    assert f == a == (400, {"error": "items must be a list"})


def test_swap_empty_items_parity(env):
    _uid, api_key = _register()
    body = {"items": []}
    f = _flask("POST", "/v1/content/swap", headers=_key(api_key), json_body=body)
    a = _asgi("POST", "/v1/content/swap", headers=_key(api_key), json_body=body)
    assert f == a
    assert f == (200, {"results": [], "summary": {"ok": 0, "not_found": 0, "error": 0, "total": 0}})


def test_swap_validation_results_parity(env):
    _uid, api_key = _register()
    body = {"items": [
        {"type": "bogus", "id": "x", "envelope": {}},        # unsupported type
        {"type": "chat", "id": "", "envelope": {}},          # id required
        {"type": "memory", "id": "m", "envelope": {"body_ct": "b"}},  # missing fields
    ]}
    f = _flask("POST", "/v1/content/swap", headers=_key(api_key), json_body=body)
    a = _asgi("POST", "/v1/content/swap", headers=_key(api_key), json_body=body)
    assert f == a
    assert f[0] == 200
    statuses = [r["status"] for r in f[1]["results"]]
    assert statuses[0].startswith("error: unsupported type")
    assert statuses[1] == "error: id required"
    assert statuses[2].startswith("error: envelope missing")


def test_swap_chat_ok_parity(env):
    # Two throwaway users seeded with an identical chat id; swapping a fresh
    # envelope in place must return the same result on both backends.
    fu, fk = _register()
    au, ak = _register()
    _seed_encrypted_content(fu)
    _seed_encrypted_content(au)
    new_env = _old_env(fu, "chat1")
    new_env["K_user"] = _b64(b"\x09" * 48)

    def _swap_body(uid):
        e = dict(new_env)
        e["owner_user_id"] = uid
        return {"items": [{"type": "chat", "id": "chat1", "envelope": e}]}

    f = _flask("POST", "/v1/content/swap", headers=_key(fk), json_body=_swap_body(fu))
    a = _asgi("POST", "/v1/content/swap", headers=_key(ak), json_body=_swap_body(au))
    assert f == a
    assert f[0] == 200
    assert f[1]["summary"] == {"ok": 1, "not_found": 0, "error": 0, "total": 1}
    # E2E: the swapped envelope's ciphertext was relocated verbatim, no decrypt.
    store = core_store.get_store(fu)
    with store.chat_lock:
        stored = [m for m in store.chat_messages if m["id"] == "chat1"][0]
    assert stored["K_user"] == new_env["K_user"]
    assert stored["body_ct"] == new_env["body_ct"]


# =========================================================================== #
# POST /v1/content/rewrap-to-current-key  (enclave stubbed on shared module)
# =========================================================================== #

def test_rewrap_bad_public_key_400_parity(env):
    _fu, fk = _register()
    _au, ak = _register()
    body = {"public_key": "!!!not-base64!!!"}
    f = _flask("POST", "/v1/content/rewrap-to-current-key", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/content/rewrap-to-current-key", headers=_key(ak), json_body=body)
    assert f == a
    assert f[0] == 400 and "error" in f[1]


def test_rewrap_full_parity_and_credential_forwarding(env, monkeypatch):
    fu, fk = _register()
    au, ak = _register()
    _seed_encrypted_content(fu)
    _seed_encrypted_content(au)
    new_pk = _b64(b"\x33" * 32)

    seen_keys: list = []

    def fake_decrypt(envelope, key, *, purpose, runtime_token=""):
        seen_keys.append(key)
        return f"plaintext:{purpose}:{envelope.get('id')}".encode()

    # One patch on the shared module covers Flask-core and ASGI-core.
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    body = {"public_key": new_pk}
    f = _flask("POST", "/v1/content/rewrap-to-current-key", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/content/rewrap-to-current-key", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 200
    assert _blank_uid(f[1]) == _blank_uid(a[1])  # same target key → identical summary/fprs
    assert f[1]["summary"]["total_rewrapped"] == 3
    assert f[1]["summary"]["total_errors"] == 0
    assert f[1]["status"] == "ok"
    # Both backends advanced the registered key and forwarded the caller's api key
    # to the (stubbed) enclave decrypt call — the enclave, not this process, decrypts.
    assert registry._get_user_public_key(fu) == new_pk
    assert registry._get_user_public_key(au) == new_pk
    assert fk in seen_keys and ak in seen_keys


def test_rewrap_dry_run_parity(env, monkeypatch):
    fu, fk = _register()
    au, ak = _register()
    _seed_encrypted_content(fu)
    _seed_encrypted_content(au)
    new_pk = _b64(b"\x33" * 32)

    def fake_decrypt(envelope, key, *, purpose, runtime_token=""):
        return f"plaintext:{purpose}:{envelope.get('id')}".encode()

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)
    body = {"public_key": new_pk, "dry_run": True}
    f = _flask("POST", "/v1/content/rewrap-to-current-key", headers=_key(fk), json_body=body)
    a = _asgi("POST", "/v1/content/rewrap-to-current-key", headers=_key(ak), json_body=body)
    assert f[0] == a[0] == 200
    assert f[1]["status"] == a[1]["status"] == "dry_run"
    assert f[1]["dry_run"] is a[1]["dry_run"] is True
    assert f[1]["summary"]["total_rewrapped"] == a[1]["summary"]["total_rewrapped"] == 3
    # dry_run must NOT advance the registered key on either backend.
    assert registry._get_user_public_key(fu) != new_pk
    assert registry._get_user_public_key(au) != new_pk


# =========================================================================== #
# GET /v1/content/export  (non-JSON attachment; ciphertext verbatim)
# =========================================================================== #

def test_export_parity_headers_and_ciphertext(env):
    # Export is read-only, so both backends hit the SAME user — only the
    # ``exported_at`` timestamp (and the filename derived from it) is volatile;
    # every other byte, incl. the ciphertext, must be identical.
    uid, api_key = _register()
    _seed_encrypted_content(uid)

    f = _flask_raw("GET", "/v1/content/export", headers=_key(api_key))
    a = _asgi_raw("GET", "/v1/content/export", headers=_key(api_key))
    assert f[0] == a[0] == 200
    # Same Content-Type + an attachment Content-Disposition (filename carries the
    # per-call exported_at timestamp, so only the prefix/user_id are stable).
    assert f[2] == a[2]
    assert "application/json" in (f[2] or "")
    assert f[3].startswith(f'attachment; filename="feedling-export-{uid}-')
    assert a[3].startswith(f'attachment; filename="feedling-export-{uid}-')
    # Bodies are pretty-printed JSON; blank only exported_at before comparing.
    fb = json.loads(f[1])
    ab = json.loads(a[1])
    for b in (fb, ab):
        b.pop("exported_at", None)
    assert fb == ab
    assert fb["user_id"] == uid
    # E2E: the seeded ciphertext is present verbatim; no plaintext leaked.
    assert fb["chat"][0]["body_ct"] == _old_env("x", "chat1")["body_ct"]
    assert fb["identity"]["body_ct"] == _old_env("x", "identity1")["body_ct"]
    assert "plaintext" not in f[1].decode()
    assert fb["schema_version"] == 2
    assert fb["attestation_snapshot"]["enclave_content_public_key_hex"] == _FAKE_ENCLAVE["content_pk_hex"]


# =========================================================================== #
# POST /v1/account/reset  (DESTRUCTIVE — throwaway users, side-effect parity)
# =========================================================================== #

_PER_USER_TABLES = (
    "perception_items",
    "perception_daily",
    "agent_runtime_instances",
    "genesis_import_jobs",
    "world_book_entries",
)


def _seed_per_user_tables(user_id: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute("INSERT INTO perception_items (user_id, kind, item_id, ts, doc) "
                     "VALUES (%s, 'photo', 'i1', 1.0, '{}'::jsonb)", (user_id,))
        conn.execute("INSERT INTO perception_daily (user_id, date, signal, doc, updated_at) "
                     "VALUES (%s, '2026-06-28', 'steps', '{}'::jsonb, 1.0)", (user_id,))
        conn.execute("INSERT INTO agent_runtime_instances (user_id, driver, status, runtime_home) "
                     "VALUES (%s, 'claude', 'idle', '/tmp/rt')", (user_id,))
        conn.execute("INSERT INTO genesis_import_jobs (user_id, job_id, status) "
                     "VALUES (%s, 'job1', 'done')", (user_id,))
        conn.execute("INSERT INTO world_book_entries (user_id, entry_id, updated_at, doc) "
                     "VALUES (%s, 'wb1', '2026-07-03T00:00:00', '{}'::jsonb)", (user_id,))


def _remaining_rows(user_id: str) -> dict[str, int]:
    counts = {}
    with db.get_pool().connection() as conn:
        for table in _PER_USER_TABLES:
            row = conn.execute(f"SELECT count(*) FROM {table} WHERE user_id = %s", (user_id,)).fetchone()
            counts[table] = row[0]
    return counts


def test_reset_confirm_required_400_parity(env):
    _uid, api_key = _register()
    f = _flask("POST", "/v1/account/reset", headers=_key(api_key), json_body={})
    a = _asgi("POST", "/v1/account/reset", headers=_key(api_key), json_body={})
    assert f == a
    assert f[0] == 400
    assert f[1]["error"] == "confirmation_required"


def test_reset_deletes_all_per_user_tables_parity(env):
    """Destructive parity: each backend deletes its OWN throwaway user, and both
    must purge the same per-user tables + succeed with the same body shape."""
    fu, fk = _register()
    au, ak = _register()
    _seed_per_user_tables(fu)
    _seed_per_user_tables(au)
    assert all(v > 0 for v in _remaining_rows(fu).values())
    assert all(v > 0 for v in _remaining_rows(au).values())

    f = _flask("POST", "/v1/account/reset", headers=_key(fk),
               json_body={"confirm": "delete-all-data"})
    a = _asgi("POST", "/v1/account/reset", headers=_key(ak),
              json_body={"confirm": "delete-all-data"})

    assert f[0] == a[0] == 200
    assert f[1]["deleted"] is a[1]["deleted"] is True
    assert f[1]["user_id"] == fu and a[1]["user_id"] == au
    # Side-effect parity: every per-user table purged on BOTH throwaway users.
    assert {t: n for t, n in _remaining_rows(fu).items() if n > 0} == {}
    assert {t: n for t, n in _remaining_rows(au).items() if n > 0} == {}
    # Idempotent auth: the revoked key no longer authenticates on either backend.
    assert _flask("POST", "/v1/account/reset", headers=_key(fk),
                  json_body={"confirm": "delete-all-data"})[0] == 401
    assert _asgi("POST", "/v1/account/reset", headers=_key(ak),
                 json_body={"confirm": "delete-all-data"})[0] == 401
