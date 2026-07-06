"""Native /v1/history_import/* + /v1/onboarding/validate parity (ASGI plan §5.3 / §9).

Asserts the FastAPI routes (``hosted.history_import_asgi`` /
``hosted.onboarding_validation_asgi``) return the same status/body as the Flask
oracle (``hosted.history_import`` / ``hosted.onboarding_validation``) for every
route, plus auth-failure (401), and — the load-bearing one — that the history-import
upload ENQUEUES its background distill job (records the ``_start_history_import_job``
daemon-thread seam) and never runs the heavy ``_run_history_import_job`` inline on
the request path (plan §5.7).

Both sides call the same framework-neutral cores (``history_import_core`` /
``onboarding_validation_core``); the enqueue seam is stubbed once on the shared
``hosted.history_import`` module so both frameworks hit the identical offline path
and the credential-forwarding / enqueue-not-inline invariants are observed.
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
from core import store as core_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from hosted import history_import as hi  # noqa: E402
from hosted import history_import_asgi as hi_asgi  # noqa: E402
from hosted import onboarding_validation_asgi as ov_asgi  # noqa: E402


def _build_asgi_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    hi_asgi.register_asgi(app)
    ov_asgi.register_asgi(app)
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
        json={"public_key": _b64(b"\x22" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


# --------------------------------------------------------------------------- #
# state helpers
# --------------------------------------------------------------------------- #

def _reset_history(uid: str) -> None:
    for job in db.list_blobs(uid, "history_import_job:"):
        db.delete_blob(uid, hi._history_job_kind(job["job_id"]))


# --------------------------------------------------------------------------- #
# normalisation (volatile timestamps / random job ids)
# --------------------------------------------------------------------------- #

def _blank(obj, drop=()):
    if isinstance(obj, dict):
        return {
            k: ("<v>" if (k in drop or k in {"updated", "ts", "age_sec", "last_poll_at",
                                             "days_with_user"} or str(k).endswith("_at"))
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


def _flask(method: str, path: str, *, headers=None, json_body=None):
    client = make_client()
    res = client.open(path, method=method, headers=dict(headers or {}), json=json_body)
    return res.status_code, res.get_json(silent=True)


def _asgi(method: str, path: str, *, headers=None, json_body=None):
    async def go():
        transport = httpx.ASGITransport(app=_ASGI)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            kw: dict = {}
            if json_body is not None:
                kw["json"] = json_body
            resp = await client.request(method, path, headers=dict(headers or {}), **kw)
            body = None
            if resp.content:
                try:
                    body = resp.json()
                except Exception:
                    body = None
            return resp.status_code, body

    return asyncio.run(go())


# --------------------------------------------------------------------------- #
# auth parity (401) — one per route
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/history_import/upload"),
    ("GET", "/v1/history_import/status/hi_abc"),
    ("GET", "/v1/onboarding/validate"),
])
def test_no_auth_is_401_parity(user, method, path):
    jb = {} if method == "POST" else None
    f = _flask(method, path, json_body=jb)
    a = _asgi(method, path, json_body=jb)
    assert f == a == (401, {"error": "unauthorized"})


# --------------------------------------------------------------------------- #
# upload — ENQUEUES the background job, never inline (plan §5.7)
# --------------------------------------------------------------------------- #

def test_upload_enqueues_not_inline_parity(user, monkeypatch):
    uid, api_key = user
    started: list = []

    def fake_start(store, key, job, payload):
        started.append({"job_id": job.get("job_id"), "api_key": key,
                        "status": job.get("status")})
        return True

    # Enqueue seam: spawn is recorded, NOT executed. The heavy runner must never
    # be called inline on the request path — make it explode if it is.
    monkeypatch.setattr(hi, "_start_history_import_job", fake_start)
    monkeypatch.setattr(
        hi, "_run_history_import_job",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("distill ran inline — must be enqueued")))

    payload = {"format": "plaintext", "content": "User: hello\nAssistant: hi",
               "client_job_id": "hup1"}

    f = _flask("POST", "/v1/history_import/upload", headers=_headers(api_key), json_body=payload)
    flask_job_id = f[1]["job"]["job_id"]
    # Real job blob row enqueued (not run inline).
    assert db.get_blob(uid, hi._history_job_kind(flask_job_id)) is not None
    _reset_history(uid)

    a = _asgi("POST", "/v1/history_import/upload", headers=_headers(api_key), json_body=payload)
    asgi_job_id = a[1]["job"]["job_id"]
    assert db.get_blob(uid, hi._history_job_kind(asgi_job_id)) is not None

    assert f[0] == a[0] == 202
    assert f[1]["job"]["status"] == a[1]["job"]["status"] == "queued"
    # Body parity once the random job id + timestamps are blanked.
    assert _norm(f, drop={"job_id"}) == _norm(a, drop={"job_id"})
    # The enqueue seam fired on both; the inline runner never did (else it raised).
    assert len(started) == 2
    # E2E credential forwarding: the caller's api key reached the enqueue on BOTH.
    assert all(s["api_key"] == api_key and s["status"] == "queued" for s in started)


def test_upload_reuses_done_job_200_parity(user, monkeypatch):
    uid, api_key = user
    # A terminal (done) job with a matching input_hash must be reused (200),
    # never restarted — the enqueue seam must NOT fire.
    monkeypatch.setattr(
        hi, "_start_history_import_job",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not restart a done job")))
    payload = {"format": "plaintext", "content": "User: hi", "client_job_id": "done1"}
    input_hash = hi._history_import_payload_hash(payload)
    store = core_store.get_store(uid)
    hi._save_history_job(store, {
        "job_id": "hi_donefixed",
        "status": "done",
        "client_job_id": "done1",
        "input_hash": input_hash,
        **hi._history_import_phase_fields("completed"),
    })

    f = _flask("POST", "/v1/history_import/upload", headers=_headers(api_key), json_body=payload)
    a = _asgi("POST", "/v1/history_import/upload", headers=_headers(api_key), json_body=payload)
    assert f[0] == a[0] == 200
    assert _norm(f) == _norm(a)
    assert f[1]["job"]["job_id"] == "hi_donefixed"


def test_upload_restarts_queued_job_202_parity(user, monkeypatch):
    uid, api_key = user
    started: list = []
    monkeypatch.setattr(
        hi, "_start_history_import_job",
        lambda store, key, job, payload: started.append(job.get("job_id")) or True)
    payload = {"format": "plaintext", "content": "User: yo", "client_job_id": "q1"}
    input_hash = hi._history_import_payload_hash(payload)
    store = core_store.get_store(uid)
    hi._save_history_job(store, {
        "job_id": "hi_queuedfixed",
        "status": "queued",
        "client_job_id": "q1",
        "input_hash": input_hash,
        **hi._history_import_phase_fields("upload_received"),
    })

    f = _flask("POST", "/v1/history_import/upload", headers=_headers(api_key), json_body=payload)
    a = _asgi("POST", "/v1/history_import/upload", headers=_headers(api_key), json_body=payload)
    assert f[0] == a[0] == 202
    assert _norm(f) == _norm(a)
    assert f[1]["job"]["job_id"] == "hi_queuedfixed"
    # Restarted on both frameworks (a queued/processing reuse re-enqueues).
    assert started == ["hi_queuedfixed", "hi_queuedfixed"]


# --------------------------------------------------------------------------- #
# status parity
# --------------------------------------------------------------------------- #

def test_status_not_found_404_parity(user):
    _uid, api_key = user
    f = _flask("GET", "/v1/history_import/status/hi_nope", headers=_headers(api_key))
    a = _asgi("GET", "/v1/history_import/status/hi_nope", headers=_headers(api_key))
    assert f == a == (404, {"error": "job_not_found"})


def test_status_found_parity(user):
    uid, api_key = user
    store = core_store.get_store(uid)
    hi._save_history_job(store, {
        "job_id": "hi_statfixed",
        "status": "processing",
        "client_job_id": "s1",
        **hi._history_import_phase_fields("candidate_extracting"),
    })
    f = _flask("GET", "/v1/history_import/status/hi_statfixed", headers=_headers(api_key))
    a = _asgi("GET", "/v1/history_import/status/hi_statfixed", headers=_headers(api_key))
    assert f[0] == a[0] == 200
    assert _norm(f) == _norm(a)
    assert f[1]["job"]["job_id"] == "hi_statfixed"
    assert f[1]["job"]["status"] == "processing"


# --------------------------------------------------------------------------- #
# onboarding/validate parity (server-side artifact-based read)
# --------------------------------------------------------------------------- #

def test_onboarding_validate_parity(user):
    _uid, api_key = user
    f = _flask("GET", "/v1/onboarding/validate", headers=_headers(api_key))
    a = _asgi("GET", "/v1/onboarding/validate", headers=_headers(api_key))
    assert f[0] == a[0] == 200
    assert _norm(f) == _norm(a)
    # Fresh user has not completed onboarding — the validator is a live gate.
    assert f[1]["passing"] is False
    assert isinstance(f[1]["steps"], list) and f[1]["steps"]
    assert [s["id"] for s in f[1]["steps"]] == [s["id"] for s in a[1]["steps"]]
