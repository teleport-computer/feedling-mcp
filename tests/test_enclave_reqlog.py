"""T637: real enclave ASGI boundary, closed fields and sensitive sentinels."""
from __future__ import annotations

import asyncio
import ast
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import anyio
import httpx
import pytest
from fastapi import Request
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from core import runtime_token
from enclave import auth, backend_client, config, envelope, keys, state
from enclave.routes import _reqlog, build_app
from enclave_health_contract import PURPOSE_LABELS

UID = "usr_12345678SECRETUSERID"
SECRET = "private-payload-sentinel"
KEY = "private-api-key-sentinel"
TOKEN = "private-runtime-token-sentinel"
ENV = {"id": SECRET, "v": 1, "body_ct": SECRET, "K_enclave": SECRET}
EXPECTED_FIELDS = {
    "ts", "pid", "method", "route", "status", "dur_ms", "purpose", "auth_kind",
    "whoami_source", "whoami_ms", "decrypt_queue_ms", "decrypt_ms",
    "user_prefix", "failure_class", "req_bytes", "resp_bytes",
}


@pytest.fixture(autouse=True)
def wired(monkeypatch):
    monkeypatch.delenv("FEEDLING_ENCLAVE_REQLOG", raising=False)
    monkeypatch.delenv("FEEDLING_ENCLAVE_REQLOG_SKIP", raising=False)
    monkeypatch.setitem(state._state, "ready", True)
    monkeypatch.setitem(state._state, "error", SECRET)
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", "")
    auth.reset_cache()

    async def get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": UID}
        return {**ENV, "owner_user_id": UID, "ts": 1}

    async def sk():
        return object()

    monkeypatch.setattr(backend_client, "backend_get", get)
    monkeypatch.setattr(keys, "get_content_sk", sk)
    original_decrypt = envelope.decrypt_envelope
    monkeypatch.setattr(envelope, "decrypt_envelope", lambda *a: SECRET.encode())
    yield original_decrypt
    auth.reset_cache()


def records(capsys):
    raw = capsys.readouterr().out
    rows = [json.loads(line) for line in raw.splitlines()]
    for row in rows:
        assert set(row) == EXPECTED_FIELDS
        assert row["purpose"] in PURPOSE_LABELS
        assert row["failure_class"] in _reqlog.FAILURE_CLASSES
    for forbidden in (SECRET, KEY, TOKEN, UID, base64.b64encode(SECRET.encode()).decode()):
        assert forbidden not in raw
    return rows


def decrypt(client, **kwargs):
    return client.post("/v1/envelope/decrypt", json={"envelope": ENV, **kwargs},
                       headers={"X-API-Key": KEY})


def test_success_closed_schema_and_real_byte_counts(capsys):
    client = TestClient(build_app())
    response = decrypt(client, purpose="model_api_provider_key")
    assert response.status_code == 200
    assert base64.b64decode(response.json()["plaintext_b64"]) == SECRET.encode()
    row, = records(capsys)
    assert set(_reqlog.FIELDS) == EXPECTED_FIELDS
    assert row["route"] == "/v1/envelope/decrypt"
    assert row["status"] == 200 and row["method"] == "POST"
    assert row["purpose"] == "model_api_provider_key"
    assert row["auth_kind"] == "api_key" and row["whoami_source"] == "backend"
    assert row["user_prefix"] == "usr_12345678"
    assert row["req_bytes"] == len(response.request.content)
    assert row["resp_bytes"] == len(response.content)
    assert row["failure_class"] == "none"
    assert row["pid"] > 0 and row["ts"].endswith("Z")
    for field in ("dur_ms", "whoami_ms", "decrypt_ms", "decrypt_queue_ms"):
        assert row[field] >= 0


@pytest.mark.parametrize("purpose", [SECRET, {"x": SECRET}, [SECRET], None, 123])
def test_purpose_never_passes_untrusted_value(purpose, capsys):
    assert decrypt(TestClient(build_app()), purpose=purpose).status_code == 200
    assert records(capsys)[0]["purpose"] == "other"


@pytest.mark.parametrize("purpose", sorted(PURPOSE_LABELS))
def test_all_contract_purposes_survive(purpose, capsys):
    assert decrypt(TestClient(build_app()), purpose=purpose).status_code == 200
    assert records(capsys)[0]["purpose"] == purpose


@pytest.mark.parametrize("status,expected", [(400, "envelope_required"), (401, "missing_api_key"),
                                               (403, "decrypt_failed"), (503, "not_ready")])
def test_real_route_failures(status, expected, monkeypatch, capsys):
    client = TestClient(build_app())
    if status == 400:
        response = client.post("/v1/envelope/decrypt", json={}, headers={"X-API-Key": KEY})
    elif status == 401:
        response = client.post("/v1/envelope/decrypt", json={"envelope": ENV})
    elif status == 503:
        monkeypatch.setitem(state._state, "ready", False)
        response = decrypt(client)
    else:
        def fail(*a):
            raise envelope.DecryptFailure(SECRET)
        monkeypatch.setattr(envelope, "decrypt_envelope", fail)
        response = decrypt(client)
    assert response.status_code == status
    row, = records(capsys)
    assert (row["status"], row["failure_class"]) == (status, expected)
    assert row["resp_bytes"] == len(response.content)
    if status in (401, 503):
        assert row["req_bytes"] == 0 and row["decrypt_ms"] is None
    if status == 403:
        assert row["decrypt_ms"] is not None


@pytest.mark.parametrize("error", [SECRET, {"secret": SECRET}, "none", "decrypt_failed " + SECRET])
def test_free_text_error_is_other(error, capsys):
    app = build_app()
    @app.get("/test-error")
    async def fail():
        return JSONResponse({"error": error, "detail": SECRET}, status_code=400)
    response = TestClient(app).get("/test-error")
    assert response.status_code == 400
    assert records(capsys)[0]["failure_class"] == "other"


def test_unmatched_and_dynamic_paths_do_not_leak(capsys):
    client = TestClient(build_app())
    assert client.get(f"/{SECRET}?key={KEY}").status_code == 404
    row, = records(capsys)
    assert row["route"] == "<unmatched>" and row["failure_class"] == "not_found"
    assert client.get(f"/v1/screen/frames/{SECRET}/decrypt?key={KEY}").status_code == 400
    assert records(capsys)[0]["route"] == "/v1/screen/frames/{frame_id}/decrypt"


def test_local_verified_token_and_invalid_token_fallback(monkeypatch, capsys):
    monkeypatch.setattr(config, "RUNTIME_TOKEN_SECRET", b"secret-signing-key")
    token = runtime_token.mint(b"secret-signing-key", user_id=UID,
                              runtime_instance_id="ri", scope=["enclave:decrypt"], ttl=300)
    client = TestClient(build_app())
    for value, source in [(token, "local_token"), (TOKEN, "backend")]:
        response = client.post("/v1/envelope/decrypt", json={"envelope": ENV},
                               headers={"X-Feedling-Runtime-Token": value})
        assert response.status_code == 200
        row, = records(capsys)
        assert value not in json.dumps(row)
        assert row["auth_kind"] == "runtime_token" and row["whoami_source"] == source
        assert (row["whoami_ms"] is None) == (source == "local_token")


def test_cache_and_backend_failure_timing(monkeypatch, capsys):
    client = TestClient(build_app())
    for source in ("backend", "cache"):
        response = client.post("/v1/memory/fetch", json={"moments": []}, headers={"X-API-Key": KEY})
        assert response.status_code == 200
        row, = records(capsys)
        assert row["whoami_source"] == source and row["user_prefix"] == "usr_12345678"
        assert (row["whoami_ms"] is None) == (source == "cache")
    async def fail(*args, **kwargs):
        raise httpx.ConnectError(SECRET)
    monkeypatch.setattr(backend_client, "backend_get", fail)
    # Live route must not inherit the read-side cache hit.
    assert decrypt(client).status_code == 502
    row, = records(capsys)
    assert row["whoami_source"] == "backend" and row["whoami_ms"] is not None
    assert row["failure_class"] == "backend_error"


@pytest.mark.parametrize("uid", ["usr_a", "usr_12345678", "usr_a-bad-full-secret", SECRET])
def test_short_or_irregular_ids_are_not_emitted(uid, monkeypatch, capsys):
    async def get(*a, **kw):
        return {"user_id": uid}
    monkeypatch.setattr(backend_client, "backend_get", get)
    assert decrypt(TestClient(build_app())).status_code == 200
    assert records(capsys)[0]["user_prefix"] is None


@pytest.mark.parametrize("path,body", [
    ("/v1/memory/index", {"moments": []}),
    ("/v1/memory/fetch", {"moments": []}),
    ("/v1/worldbook/match", {"world_books": [], "messages": []}),
    ("/v1/history/scan", {"rows": []}),
    ("/v1/history/fetch", {"anchor": {}}),
    ("/v1/history/leaf-hints", {"leaves": [], "query": SECRET}),
])
def test_batch_routes_have_job_timing_and_purpose(path, body, capsys):
    response = TestClient(build_app()).post(path, json={**body, "purpose": "v2_chat_read"},
                                          headers={"X-API-Key": KEY})
    assert response.status_code == 200, response.text
    row, = records(capsys)
    assert row["route"] == path and row["purpose"] == "v2_chat_read"
    assert row["decrypt_ms"] is not None and row["decrypt_queue_ms"] is not None


def test_health_skip_override_and_off_switch(monkeypatch, capsys):
    TestClient(build_app()).get("/healthz")
    assert records(capsys) == []
    monkeypatch.setenv("FEEDLING_ENCLAVE_REQLOG_SKIP", "")
    TestClient(build_app()).get("/healthz")
    assert records(capsys)[0]["route"] == "/healthz"
    monkeypatch.setenv("FEEDLING_ENCLAVE_REQLOG", "0")
    decrypt(TestClient(build_app()))
    assert records(capsys) == []


def test_gzip_head_and_unhandled_500(capsys):
    app = build_app()
    @app.api_route("/test-large", methods=["GET", "HEAD"])
    async def large():
        return JSONResponse({"data": SECRET * 1000})
    @app.get("/test-raises")
    async def raises():
        raise ValueError(SECRET)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/test-large", headers={"Accept-Encoding": "gzip"})
    assert response.headers["content-encoding"] == "gzip"
    assert records(capsys)[0]["resp_bytes"] == int(response.headers["content-length"]) < len(response.content)
    assert client.head("/test-large").content == b""
    assert records(capsys)[0]["resp_bytes"] == 0
    response = client.get("/test-raises")
    assert response.status_code == 500
    row, = records(capsys)
    assert row["status"] == 500 and row["failure_class"] == "internal_error"
    assert row["resp_bytes"] == len(response.content)


def test_thread_queue_and_execution_are_separate(capsys):
    app = build_app()
    @app.get("/test-queue")
    async def queued(request: Request):
        limiter = anyio.to_thread.current_default_thread_limiter()
        old = limiter.total_tokens
        limiter.total_tokens = 1
        entered = asyncio.Event()
        async def hold():
            async with limiter:
                entered.set()
                await asyncio.sleep(0.08)
        holder = asyncio.create_task(hold())
        await entered.wait()
        try:
            await _reqlog.decrypt_job(request, time.sleep, 0.01)
            await holder
        finally:
            limiter.total_tokens = old
        return JSONResponse({"ok": True})
    assert TestClient(app).get("/test-queue").status_code == 200
    row, = records(capsys)
    assert row["decrypt_queue_ms"] >= 50
    assert 5 <= row["decrypt_ms"] < row["decrypt_queue_ms"]


def test_sink_provenance_has_one_closed_record_output():
    tree = ast.parse(Path(_reqlog.__file__).read_text())
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    outputs = [n for n in calls if isinstance(n.func, ast.Name) and n.func.id == "print"]
    assert len(outputs) == 1
    assert ast.unparse(outputs[0].args[0]) == "json.dumps({key: record[key] for key in FIELDS}, separators=(',', ':')) + '\\n'"
    assert not any(isinstance(n.func, ast.Name) and n.func.id in {"str", "repr"} for n in calls)
    assert not any(isinstance(n, ast.Attribute) and n.attr in {"headers", "query_params", "json", "body"}
                   for n in ast.walk(tree) if not isinstance(n, ast.Call))


@pytest.mark.parametrize("endpoint", ["decrypt", "caption", "image"])
def test_frame_routes_have_template_and_timing(endpoint, monkeypatch, capsys):
    import provider_client
    monkeypatch.setenv("FEEDLING_SCREEN_VLM_API_KEY", KEY)
    async def caption(*a, **kw):
        return {"reply": SECRET}
    monkeypatch.setattr(provider_client, "chat_completion_async", caption)
    inner = json.dumps({"image": base64.b64encode(b"jpeg-sentinel").decode(),
                        "image_mime": "image/jpeg", "ocr_text": SECRET}).encode()
    monkeypatch.setattr(envelope, "decrypt_envelope", lambda *a: inner)
    response = TestClient(build_app()).get(f"/v1/screen/frames/{'ab' * 8}/{endpoint}",
                                          headers={"X-API-Key": KEY})
    assert response.status_code == 200, response.text
    row, = records(capsys)
    assert row["route"] == f"/v1/screen/frames/{{frame_id}}/{endpoint}"
    assert row["decrypt_ms"] is not None and row["decrypt_queue_ms"] is not None


def test_singleflight_waiters_are_cache_not_backend(monkeypatch, capsys):
    calls = 0
    async def get(*a, **kw):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.025)
        return {"user_id": UID}
    monkeypatch.setattr(backend_client, "backend_get", get)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app()),
                                     base_url="http://test") as client:
            responses = await asyncio.gather(*[
                client.post("/v1/memory/fetch", json={"moments": []}, headers={"X-API-Key": KEY})
                for _ in range(5)])
            assert all(r.status_code == 200 for r in responses)
    asyncio.run(run())
    rows = records(capsys)
    assert calls == 1
    assert [r["whoami_source"] for r in rows].count("backend") == 1
    assert [r["whoami_source"] for r in rows].count("cache") == 4
    assert all(r["whoami_ms"] is None for r in rows if r["whoami_source"] == "cache")


def test_real_crypto_round_trip_and_cross_user_rejection(wired, monkeypatch, capsys):
    import nacl.public
    from test_enclave_envelope_core import _make_envelope
    sk = nacl.public.PrivateKey.generate()
    monkeypatch.setattr(envelope, "decrypt_envelope", wired)
    async def real_key():
        return sk
    monkeypatch.setattr(keys, "get_content_sk", real_key)
    client = TestClient(build_app())
    for owner, status in [(UID, 200), ("usr_differentperson", 403)]:
        env = _make_envelope(owner, SECRET, SECRET.encode(), bytes(sk.public_key))
        response = client.post("/v1/envelope/decrypt", json={"envelope": env}, headers={"X-API-Key": KEY})
        assert response.status_code == status
        if status == 200:
            assert base64.b64decode(response.json()["plaintext_b64"]) == SECRET.encode()
        row, = records(capsys)
        assert row["failure_class"] == ("none" if status == 200 else "decrypt_failed")
        assert row["decrypt_ms"] is not None


def test_production_gunicorn_worker_flushes_json_stdout(tmp_path):
    """No logging configuration assumption: observe the actual worker's pipe."""
    import os
    import socket
    import subprocess
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    stdout_path, stderr_path = tmp_path / "stdout", tmp_path / "stderr"
    env = {**os.environ, "FEEDLING_ENCLAVE_PORT": str(port), "FEEDLING_ENCLAVE_WORKERS": "1",
           "FEEDLING_ENCLAVE_REQLOG": "1", "FEEDLING_ENCLAVE_REQLOG_SKIP": "/healthz",
           "NO_PROXY": "*", "no_proxy": "*"}
    with stdout_path.open("w") as out, stderr_path.open("w") as err:
        proc = subprocess.Popen(
            [sys.executable, "-c", "from enclave.serving import run_enclave_server; run_enclave_server(None)"],
            cwd=Path(__file__).resolve().parents[1] / "backend", env=env, stdout=out, stderr=err)
        try:
            deadline = time.monotonic() + 15
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=1) as client:
                while True:
                    assert proc.poll() is None, stderr_path.read_text()
                    try:
                        response = client.get("/healthz")
                        if response.status_code in (200, 503):
                            break
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline, stderr_path.read_text()
                    time.sleep(0.05)
                assert client.get(f"/{SECRET}?key={KEY}").status_code == 404
                while not stdout_path.read_text().strip():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                raw = stdout_path.read_text()
                row, = [json.loads(line) for line in raw.splitlines()]
                assert set(row) == EXPECTED_FIELDS
                assert row["status"] == 404 and row["route"] == "<unmatched>"
                assert row["pid"] != os.getpid()
                assert SECRET not in raw and KEY not in raw
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.parametrize("body", [b'x' * 9000, b'[' * 1500 + b']' * 1500,
                                   b'{"error":"private-payload-sentinel"}'])
def test_malformed_large_and_streamed_error_body_is_bounded(body, capsys):
    from starlette.responses import StreamingResponse
    app = build_app()
    @app.get("/test-error-stream")
    async def error():
        async def chunks():
            for offset in range(0, len(body), 100):
                yield body[offset:offset + 100]
        return StreamingResponse(chunks(), status_code=400, media_type="application/json")
    response = TestClient(app).get("/test-error-stream")
    assert response.content == body
    row, = records(capsys)
    assert row["failure_class"] == "other" and row["resp_bytes"] == len(body)


@pytest.mark.parametrize("encoding,status,failure", [("identity", 200, "response_incomplete"),
                                                     ("gzip", 500, "internal_error")])
def test_aborted_response_does_not_claim_success(encoding, status, failure, capsys):
    from starlette.responses import StreamingResponse
    app = build_app()
    @app.get("/test-abort")
    async def abort():
        async def chunks():
            yield SECRET.encode()
            raise ValueError(SECRET)
        return StreamingResponse(chunks())
    assert TestClient(app, raise_server_exceptions=False).get(
        "/test-abort", headers={"Accept-Encoding": encoding}).status_code == status
    row, = records(capsys)
    assert row["status"] == status and row["failure_class"] == failure
    assert row["resp_bytes"] == (len(SECRET) if status == 200 else len(b"Internal Server Error"))
