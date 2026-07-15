"""Native /v1/genesis/* parity (ASGI-migration plan §5.3 / §9).

Asserts the FastAPI routes (``genesis.routes_asgi``) return the same status/body
as the Flask oracle (``genesis.routes``) for every route (imports list/create/
status, chunk PUT, finalize, outputs, plaintext ingest, persona_backfill), plus
auth-failure (401), scope-failure (403 for the two scoped routes outputs +
persona_backfill), validation paths, and — the load-bearing one — that the
plaintext import route ENQUEUES its background distill job (spawns the daemon
thread) and never runs the heavy ``_run_plaintext_genesis_job`` inline on the
request path (plan §5.7).

Both sides call the same framework-neutral ``genesis.genesis_core``. The
enclave/apply/enqueue seams (``service.apply_reducer_output`` /
``service.put_chunk`` / ``routes._start_plaintext_genesis_job`` /
``persona_backfill.run_persona_backfill`` / ``identity.actions.
_identity_plain_for_action``) are stubbed once on their shared module objects, so
both frameworks hit the identical offline path and the credential forwarding /
enqueue-not-inline invariants are observed.
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
import debug_trace  # noqa: E402
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from core import runtime_token  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from genesis import plaintext as genesis_routes  # noqa: E402
from genesis import routes_asgi as genesis_asgi  # noqa: E402
from genesis import service as genesis_service  # noqa: E402
from identity import actions as identity_actions  # noqa: E402

_SECRET = "test-asgi-genesis-secret"


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    genesis_asgi.register_asgi(app)
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
# state helpers
# --------------------------------------------------------------------------- #

def _reset_genesis(uid: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM genesis_import_chunks WHERE user_id = %s", (uid,))
        conn.execute("DELETE FROM genesis_import_jobs WHERE user_id = %s", (uid,))
    for blob in ("genesis_state", "genesis_persona", "genesis_voice"):
        db.delete_blob(uid, blob)


def _create_job(uid: str, api_key: str, **payload) -> dict:
    body = {"job_id": payload.pop("job_id", "seedjob"), **payload}
    res = make_client().post(
        "/v1/genesis/imports", headers=_headers(api_key), json=body)
    assert res.status_code in (200, 201), res.get_data(as_text=True)
    return res.get_json()["job"]


def _seed_done_plaintext_job(uid: str, input_hash: str) -> None:
    genesis_service.create_import_job(
        core_store.get_store(uid),
        {
            "job_id": "plaindone",
            "source_kind": "history_import",
            "metadata": {"ingest": "plaintext", "input_hash": input_hash, "mode": "onboarding"},
        },
    )
    db.genesis_set_job_status(uid, "plaindone", status=genesis_service.DONE_JOB_STATUS)


# --------------------------------------------------------------------------- #
# normalisation (volatile timestamps / random job ids)
# --------------------------------------------------------------------------- #

def _blank(obj, drop=()):  # blank any *_at / ts / updated field + explicit drop keys
    if isinstance(obj, dict):
        return {
            k: ("<v>" if (k in drop or k in {"updated", "ts"} or str(k).endswith("_at"))
                else _blank(v, drop))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_blank(x, drop) for x in obj]
    return obj


def _norm(resp, drop=()):
    status, body = resp
    return status, _blank(copy.deepcopy(body), drop)


# --------------------------------------------------------------------------- #
# request helpers
# --------------------------------------------------------------------------- #

def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _flask(method: str, path: str, *, headers=None, json_body=None, data=None, extra_headers=None):
    client = make_client()
    hdr = dict(headers or {})
    if extra_headers:
        hdr.update(extra_headers)
    if data is not None:
        res = client.open(path, method=method, headers=hdr, data=data)
    else:
        res = client.open(path, method=method, headers=hdr, json=json_body)
    return res.status_code, res.get_json(silent=True)


def _asgi(method: str, path: str, *, headers=None, json_body=None, content=None, extra_headers=None):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            hdr = dict(headers or {})
            if extra_headers:
                hdr.update(extra_headers)
            kw: dict = {}
            if content is not None:
                kw["content"] = content
            elif json_body is not None:
                kw["json"] = json_body
            resp = await client.request(method, path, headers=hdr, **kw)
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


# --------------------------------------------------------------------------- #
# auth parity (401) — one per route
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/genesis/imports"),
    ("POST", "/v1/genesis/imports/plaintext"),
    ("GET", "/v1/genesis/imports"),
    ("PUT", "/v1/genesis/imports/j1/chunks/0"),
    ("POST", "/v1/genesis/imports/j1/finalize"),
    ("POST", "/v1/genesis/imports/j1/outputs"),
    ("POST", "/v1/genesis/persona_backfill"),
    ("GET", "/v1/genesis/imports/j1"),
])
def test_no_auth_is_401_parity(user, method, path):
    jb = {} if method in ("POST", "PUT") else None
    f = _flask(method, path, json_body=jb)
    a = _asgi(method, path, json_body=jb)
    assert f == a == (401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# create parity
# --------------------------------------------------------------------------- #

def test_create_happy_parity(user):
    uid, api_key = user
    payload = {"job_id": "createjob1", "source_kind": "history_import", "total_chunks": 2, "total_bytes": 10}
    f = _flask("POST", "/v1/genesis/imports", headers=_headers(api_key), json_body=payload)
    _reset_genesis(uid)
    a = _asgi("POST", "/v1/genesis/imports", headers=_headers(api_key), json_body=payload)
    assert f[0] == a[0] == 201
    assert _norm(f) == _norm(a)
    assert f[1]["status"] == "created"
    assert f[1]["job"]["job_id"] == "createjob1"


def test_create_invalid_total_chunks_400_parity(user):
    _uid, api_key = user
    payload = {"job_id": "badjob", "total_chunks": "abc"}
    f = _flask("POST", "/v1/genesis/imports", headers=_headers(api_key), json_body=payload)
    a = _asgi("POST", "/v1/genesis/imports", headers=_headers(api_key), json_body=payload)
    assert f == a == (400, {"error": "total_chunks_total_bytes_must_be_int"})


# --------------------------------------------------------------------------- #
# list parity
# --------------------------------------------------------------------------- #

def test_list_empty_parity(user):
    _uid, api_key = user
    f = _flask("GET", "/v1/genesis/imports", headers=_headers(api_key))
    a = _asgi("GET", "/v1/genesis/imports", headers=_headers(api_key))
    assert f == a == (200, {"jobs": [], "state": None})


def test_list_with_job_parity(user):
    uid, api_key = user
    _create_job(uid, api_key, job_id="listjob1", source_kind="history_import", total_chunks=1, total_bytes=3)
    f = _flask("GET", "/v1/genesis/imports", headers=_headers(api_key))
    a = _asgi("GET", "/v1/genesis/imports", headers=_headers(api_key))
    assert f[0] == a[0] == 200
    assert _norm(f) == _norm(a)
    assert [j["job_id"] for j in f[1]["jobs"]] == ["listjob1"]


# --------------------------------------------------------------------------- #
# status parity
# --------------------------------------------------------------------------- #

def test_status_invalid_job_id_400_parity(user):
    _uid, api_key = user
    f = _flask("GET", "/v1/genesis/imports/bad%20id!", headers=_headers(api_key))
    a = _asgi("GET", "/v1/genesis/imports/bad%20id!", headers=_headers(api_key))
    assert f == a == (400, {"error": "invalid_job_id"})


def test_status_not_found_404_parity(user):
    _uid, api_key = user
    f = _flask("GET", "/v1/genesis/imports/nope", headers=_headers(api_key))
    a = _asgi("GET", "/v1/genesis/imports/nope", headers=_headers(api_key))
    assert f == a == (404, {"error": "genesis_job_not_found"})


def test_status_found_parity(user):
    uid, api_key = user
    _create_job(uid, api_key, job_id="statjob1", source_kind="history_import", total_chunks=1, total_bytes=3)
    f = _flask("GET", "/v1/genesis/imports/statjob1", headers=_headers(api_key))
    a = _asgi("GET", "/v1/genesis/imports/statjob1", headers=_headers(api_key))
    assert f[0] == a[0] == 200
    assert _norm(f) == _norm(a)
    assert f[1]["job"]["job_id"] == "statjob1"
    assert "persona" in f[1]


# --------------------------------------------------------------------------- #
# chunk PUT parity (service.put_chunk stubbed -> offline, deterministic)
# --------------------------------------------------------------------------- #

def test_chunk_invalid_job_id_400_parity(user):
    _uid, api_key = user
    f = _flask("PUT", "/v1/genesis/imports/bad%20id!/chunks/0", headers=_headers(api_key), json_body={})
    a = _asgi("PUT", "/v1/genesis/imports/bad%20id!/chunks/0", headers=_headers(api_key), json_body={})
    assert f == a == (400, {"error": "invalid_job_id"})


def test_chunk_json_body_parity(user, monkeypatch):
    _uid, api_key = user
    calls: list = []

    def fake_put(store, job_id, **kw):
        calls.append({"job_id": job_id, **kw})
        return {"ok": True, "seq": kw["seq"]}

    monkeypatch.setattr(genesis_service, "put_chunk", fake_put)
    body = {"ciphertext_b64": _b64(b"hello"), "byte_start": 0, "byte_end": 5}
    f = _flask("PUT", "/v1/genesis/imports/chunkjob/chunks/3", headers=_headers(api_key), json_body=body)
    a = _asgi("PUT", "/v1/genesis/imports/chunkjob/chunks/3", headers=_headers(api_key), json_body=body)
    assert f == a == (200, {"status": "uploaded", "chunk": {"ok": True, "seq": 3}})
    # Both frameworks decoded the ciphertext + passed the int seq into the store.
    assert len(calls) == 2
    for c in calls:
        assert c["encrypted_body"] == b"hello"
        assert c["seq"] == 3


def test_chunk_binary_body_parity(user, monkeypatch):
    _uid, api_key = user
    calls: list = []

    def fake_put(store, job_id, **kw):
        calls.append({"job_id": job_id, **kw})
        return {"ok": True, "seq": kw["seq"]}

    monkeypatch.setattr(genesis_service, "put_chunk", fake_put)
    hdr = {**_headers(api_key), "Content-Type": "application/octet-stream",
           "X-Byte-Start": "0", "X-Byte-End": "5"}
    f = _flask("PUT", "/v1/genesis/imports/binjob/chunks/7", headers=hdr, data=b"world")
    a = _asgi("PUT", "/v1/genesis/imports/binjob/chunks/7", headers=hdr, content=b"world")
    assert f == a == (200, {"status": "uploaded", "chunk": {"ok": True, "seq": 7}})
    assert len(calls) == 2
    for c in calls:
        assert c["encrypted_body"] == b"world"
        assert c["seq"] == 7
        assert c["byte_start"] == 0
        assert c["byte_end"] == 5


# --------------------------------------------------------------------------- #
# finalize parity
# --------------------------------------------------------------------------- #

def test_finalize_invalid_job_id_400_parity(user):
    _uid, api_key = user
    f = _flask("POST", "/v1/genesis/imports/bad%20id!/finalize", headers=_headers(api_key), json_body={})
    a = _asgi("POST", "/v1/genesis/imports/bad%20id!/finalize", headers=_headers(api_key), json_body={})
    assert f == a == (400, {"error": "invalid_job_id"})


def test_finalize_not_found_404_parity(user):
    _uid, api_key = user
    f = _flask("POST", "/v1/genesis/imports/missingjob/finalize", headers=_headers(api_key), json_body={})
    a = _asgi("POST", "/v1/genesis/imports/missingjob/finalize", headers=_headers(api_key), json_body={})
    assert f == a == (404, {"error": "genesis_job_not_found"})


def test_finalize_uploaded_parity(user):
    uid, api_key = user
    _create_job(uid, api_key, job_id="finjob1", source_kind="history_import", total_chunks=0, total_bytes=0)
    f = _flask("POST", "/v1/genesis/imports/finjob1/finalize", headers=_headers(api_key), json_body={})
    _reset_genesis(uid)
    _create_job(uid, api_key, job_id="finjob1", source_kind="history_import", total_chunks=0, total_bytes=0)
    a = _asgi("POST", "/v1/genesis/imports/finjob1/finalize", headers=_headers(api_key), json_body={})
    assert f[0] == a[0] == 202
    assert _norm(f) == _norm(a)
    assert f[1]["status"] == "uploaded"


def test_finalize_apply_parity(user, monkeypatch):
    uid, api_key = user
    seen: list = []

    def fake_apply(store, key, job_id, output, *, runtime_token=""):
        seen.append({"api_key": key, "job_id": job_id, "runtime_token": runtime_token})
        return {"memory_action_count": 0, "identity_status": "skipped"}

    monkeypatch.setattr(genesis_service, "apply_reducer_output", fake_apply)
    payload = {"reducer_output": {"memories": []}}

    _create_job(uid, api_key, job_id="finapply1", source_kind="history_import", total_chunks=0, total_bytes=0)
    f = _flask("POST", "/v1/genesis/imports/finapply1/finalize", headers=_headers(api_key), json_body=payload)
    _reset_genesis(uid)
    _create_job(uid, api_key, job_id="finapply1", source_kind="history_import", total_chunks=0, total_bytes=0)
    a = _asgi("POST", "/v1/genesis/imports/finapply1/finalize", headers=_headers(api_key), json_body=payload)
    assert f[0] == a[0] == 200
    assert _norm(f) == _norm(a)
    assert f[1]["status"] == "done"
    # E2E: the caller's api key reached the (enclave-owned) apply on BOTH frameworks.
    assert seen and all(s["api_key"] == api_key for s in seen)


# --------------------------------------------------------------------------- #
# outputs parity (scoped route)
# --------------------------------------------------------------------------- #

def test_outputs_scope_denied_403_parity(user, monkeypatch):
    uid, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    tok = _mint(uid, scope=["chat", "memory"])
    hdr = {"X-Feedling-Runtime-Token": tok}
    f = _flask("POST", "/v1/genesis/imports/j1/outputs", headers=hdr, json_body={"reducer_output": {}})
    a = _asgi("POST", "/v1/genesis/imports/j1/outputs", headers=hdr, json_body={"reducer_output": {}})
    assert f == a == (403, {"error": "forbidden"})


def test_outputs_scope_allowed_reaches_validation_parity(user, monkeypatch):
    uid, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    tok = _mint(uid, scope=["genesis"])
    hdr = {"X-Feedling-Runtime-Token": tok}
    # Gate cleared -> reaches the job_id validation -> 400 invalid_job_id (proves 403 passed).
    f = _flask("POST", "/v1/genesis/imports/bad%20id!/outputs", headers=hdr, json_body={"reducer_output": {}})
    a = _asgi("POST", "/v1/genesis/imports/bad%20id!/outputs", headers=hdr, json_body={"reducer_output": {}})
    assert f == a == (400, {"error": "invalid_job_id"})


def test_outputs_happy_parity(user, monkeypatch):
    uid, api_key = user
    seen: list = []

    def fake_apply(store, key, job_id, output, *, runtime_token=""):
        seen.append({"api_key": key, "job_id": job_id, "runtime_token": runtime_token})
        return {"memory_action_count": 1, "identity_status": "initialized", "persona_ref": "ref"}

    monkeypatch.setattr(genesis_service, "apply_reducer_output", fake_apply)
    monkeypatch.setattr(debug_trace, "trace_event", lambda *a, **k: None)
    _create_job(uid, api_key, job_id="outjob1", source_kind="history_import", total_chunks=0, total_bytes=0)
    payload = {"reducer_output": {"memories": []}}
    f = _flask("POST", "/v1/genesis/imports/outjob1/outputs", headers=_headers(api_key), json_body=payload)
    a = _asgi("POST", "/v1/genesis/imports/outjob1/outputs", headers=_headers(api_key), json_body=payload)
    assert f[0] == a[0] == 200
    assert _norm(f) == _norm(a)
    assert f[1]["status"] == "done"
    # api-key path: key forwarded, runtime_token empty string on BOTH frameworks.
    assert seen and all(s["api_key"] == api_key and s["runtime_token"] == "" for s in seen)


# --------------------------------------------------------------------------- #
# persona_backfill parity (scoped route)
# --------------------------------------------------------------------------- #

def test_persona_backfill_scope_denied_403_parity(user, monkeypatch):
    uid, _api_key = user
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", _SECRET)
    tok = _mint(uid, scope=["chat"])
    hdr = {"X-Feedling-Runtime-Token": tok}
    f = _flask("POST", "/v1/genesis/persona_backfill", headers=hdr, json_body={})
    a = _asgi("POST", "/v1/genesis/persona_backfill", headers=hdr, json_body={})
    assert f == a == (403, {"error": "forbidden"})


def test_persona_backfill_identity_unavailable_409_parity(user, monkeypatch):
    _uid, api_key = user
    monkeypatch.setattr(
        identity_actions, "_identity_plain_for_action",
        lambda store, key, *, runtime_token="": (None, "identity_unavailable"))
    f = _flask("POST", "/v1/genesis/persona_backfill", headers=_headers(api_key), json_body={})
    a = _asgi("POST", "/v1/genesis/persona_backfill", headers=_headers(api_key), json_body={})
    assert f == a == (409, {"error": "identity_unavailable"})


def test_persona_backfill_no_signal_200_parity(user, monkeypatch):
    _uid, api_key = user
    monkeypatch.setattr(
        identity_actions, "_identity_plain_for_action",
        lambda store, key, *, runtime_token="": ({"agent_name": "x"}, ""))
    from genesis import persona_backfill as genesis_persona_backfill
    monkeypatch.setattr(genesis_persona_backfill, "run_persona_backfill", lambda store, plain: None)
    f = _flask("POST", "/v1/genesis/persona_backfill", headers=_headers(api_key), json_body={})
    a = _asgi("POST", "/v1/genesis/persona_backfill", headers=_headers(api_key), json_body={})
    assert f == a == (200, {"status": "no_signal"})


def test_persona_backfill_enqueued_202_parity(user, monkeypatch):
    _uid, api_key = user
    seen: list = []

    def fake_plain(store, key, *, runtime_token=""):
        seen.append({"api_key": key, "runtime_token": runtime_token})
        return {"agent_name": "x"}, ""

    monkeypatch.setattr(identity_actions, "_identity_plain_for_action", fake_plain)
    from genesis import persona_backfill as genesis_persona_backfill
    monkeypatch.setattr(
        genesis_persona_backfill, "run_persona_backfill",
        lambda store, plain: {"job_id": "pb1", "status": "created"})
    f = _flask("POST", "/v1/genesis/persona_backfill", headers=_headers(api_key), json_body={})
    a = _asgi("POST", "/v1/genesis/persona_backfill", headers=_headers(api_key), json_body={})
    assert f == a == (202, {"status": "enqueued", "job_id": "pb1", "job_status": "created"})
    # api-key path: key forwarded; runtime_token defaults to "" (header .get default).
    assert seen and all(s["api_key"] == api_key and s["runtime_token"] == "" for s in seen)


# --------------------------------------------------------------------------- #
# plaintext ingest — ENQUEUES the background job, never inline (plan §5.7)
# --------------------------------------------------------------------------- #

def test_plaintext_enqueues_not_inline_parity(user, monkeypatch):
    uid, api_key = user
    started: list = []

    def fake_start(store, key, job, **kwargs):
        started.append({"job_id": job.get("job_id"), "mode": kwargs.get("mode"), "api_key": key})
        return True

    # Enqueue seam: spawn is recorded, NOT executed. The heavy runner must never
    # be called inline on the request path — make it explode if it is.
    monkeypatch.setattr(genesis_routes, "_start_plaintext_genesis_job", fake_start)
    monkeypatch.setattr(
        genesis_routes, "_run_plaintext_genesis_job",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("distill ran inline — must be enqueued")))

    payload = {"format": "plaintext", "content": "User: hello\nAssistant: hi", "client_job_id": "pt1"}

    f = _flask("POST", "/v1/genesis/imports/plaintext", headers=_headers(api_key), json_body=payload)
    flask_job_id = f[1]["job"]["job_id"]
    assert db.genesis_get_job(uid, flask_job_id) is not None  # real job row enqueued
    _reset_genesis(uid)

    a = _asgi("POST", "/v1/genesis/imports/plaintext", headers=_headers(api_key), json_body=payload)
    asgi_job_id = a[1]["job"]["job_id"]
    assert db.genesis_get_job(uid, asgi_job_id) is not None

    assert f[0] == a[0] == 202
    assert f[1]["status"] == a[1]["status"] == "processing"
    # Body parity once the random job id + timestamps are blanked.
    assert _norm(f, drop={"job_id"}) == _norm(a, drop={"job_id"})
    # The enqueue seam fired on both; the inline runner never did (else it raised).
    assert [s["mode"] for s in started] == ["onboarding", "onboarding"]
    assert all(s["api_key"] == api_key for s in started)


def test_plaintext_reuses_done_job_200_parity(user, monkeypatch):
    uid, api_key = user
    payload = {"format": "plaintext", "content": "User: hello"}
    input_hash = genesis_routes.history_import._history_import_payload_hash(payload)
    _seed_done_plaintext_job(uid, input_hash)
    # A done job must be reused (200), never restarted.
    monkeypatch.setattr(
        genesis_routes, "_start_plaintext_genesis_job",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not restart a done job")))
    f = _flask("POST", "/v1/genesis/imports/plaintext", headers=_headers(api_key), json_body=payload)
    a = _asgi("POST", "/v1/genesis/imports/plaintext", headers=_headers(api_key), json_body=payload)
    assert f[0] == a[0] == 200
    assert _norm(f) == _norm(a)
    assert f[1]["status"] == "done"
    assert f[1]["job"]["job_id"] == "plaindone"


def test_plaintext_update_identity_without_identity_enqueues_202_parity(user, monkeypatch):
    _uid, api_key = user
    payload = {"mode": "update_identity", "ai_persona_content": "Name: Joy", "client_job_id": "identity-x"}
    monkeypatch.setattr(genesis_routes, "_start_plaintext_genesis_job", lambda *_args, **_kwargs: True)
    f = _flask("POST", "/v1/genesis/imports/plaintext", headers=_headers(api_key), json_body=payload)
    a = _asgi("POST", "/v1/genesis/imports/plaintext", headers=_headers(api_key), json_body=payload)
    assert f[0] == a[0] == 202
    assert _norm(f) == _norm(a)
    assert f[1]["status"] == "processing"
