"""Native ASGI ``POST /v1/model_api/chat/send`` parity (plan §11.1).

Asserts the FastAPI route returns the same body/status as the Flask oracle for
the hosted-agent path (both the ``processing`` and the ``reply_ready`` 202
shapes), preserves the 503 supervisor wedge guard (no orphan append), the 409
unsupported-provider branch, message validation, and fixed-body auth failures.

The legacy inline provider path was removed from this route (host-all cutover /
"Task 3"): every configured send is handed to ``agent_runtime_cutover``. So the
"two paths" exercised here are the two agent-runtime 202 shapes, driven by
stubbing ``wait_for_reply`` (reply landed vs not) — the REAL ``handle_send`` +
``build_*_response`` run, so the 202 contract is exercised, not mocked.

Provider/supervisor/enclave are stubbed exactly as the existing Flask routing
test (``test_model_api_chat_send_routing``) so this stays deterministic + offline.
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import asgi_app  # noqa: E402
import debug_trace  # noqa: E402
import provider_client  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import agent_runtime_cutover  # noqa: E402
from hosted import chat_routes_asgi  # noqa: E402
from hosted import config_store as hosted_config_store  # noqa: E402


# Mount the hosted send router onto the shared ASGI app if a sibling agent's
# registration (asgi_app._ASGI_PACKAGES) has not already added it. Idempotent:
# never double-registers the same path. fastapi 0.139 / starlette 1.3
# include_router is lazy: app.routes holds ``_IncludedRouter`` proxies with no
# ``.path``, so the guard must walk through ``original_router`` — a flat
# ``r.path`` scan never sees the already-assembled route and double-registers.


def _app_has_route(app, path: str) -> bool:
    def walk(routes) -> bool:
        for r in routes:
            original = getattr(r, "original_router", None)
            if original is not None:
                if walk(original.routes):
                    return True
            elif getattr(r, "path", None) == path:
                return True
        return False

    return walk(app.routes)


if not _app_has_route(asgi_app.app, "/v1/model_api/chat/send"):
    chat_routes_asgi.register_asgi(asgi_app.app)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    monkeypatch.setattr(
        core_enclave, "_get_enclave_info",
        lambda: {"content_pk_hex": ("22" * 32), "compose_hash": "test"},
    )
    # Fake envelopes (no real enclave) shared by Flask + ASGI (module attr).
    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", _fake_envelope_builder())
    # _load_runtime_provider_config succeeds without a real enclave decrypt.
    monkeypatch.setattr(
        core_enclave, "_decrypt_envelope_via_enclave",
        lambda envelope, key, purpose: b"sk-or-test",
    )
    # Live supervisor by default; the wedge-guard test overrides this.
    monkeypatch.setattr(agent_runtime_cutover, "check_supervisor_live", lambda **kw: (True, ""))
    return monkeypatch


def _fake_envelope_builder():
    counter = {"n": 0}

    def _build(store, plaintext: bytes, *, item_id: str | None = None):
        counter["n"] += 1
        return {
            "v": 1, "id": item_id or f"env_{counter['n']}",
            "body_ct": f"ct_{counter['n']}", "nonce": f"nonce_{counter['n']}",
            "K_user": f"k_user_{counter['n']}", "K_enclave": f"k_enclave_{counter['n']}",
            "visibility": "shared", "owner_user_id": getattr(store, "user_id", "test"),
            "enclave_pk_fpr": "test",
        }, ""

    return _build


def _register() -> tuple[str, str]:
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _setup_openrouter(api_key: str, monkeypatch) -> None:
    monkeypatch.setattr(
        provider_client, "test_provider_key",
        lambda cfg: {"reply": "ok", "usage": {"total_tokens": 1}},
    )
    res = make_client().post(
        "/v1/model_api/setup",
        json={"provider": "openrouter", "model": "openai/gpt-4o-mini", "api_key": "sk-or-test"},
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)


def _flask_post(path, payload, headers=None, *, raw=None, content_type=None):
    c = make_client()
    if raw is not None:
        res = c.post(path, data=raw, content_type=content_type, headers=headers or {})
    else:
        res = c.post(path, json=payload, headers=headers or {})
    return res.status_code, res.get_json()


def _asgi_post(path, payload, headers=None, *, raw=None, content_type=None):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            if raw is not None:
                h = dict(headers or {})
                if content_type:
                    h["content-type"] = content_type
                resp = await client.post(path, content=raw, headers=h)
            else:
                resp = await client.post(path, json=payload, headers=headers or {})
            try:
                body = resp.json()
            except Exception:
                body = None
            return resp.status_code, body

    return asyncio.run(go())


def _user_msg_count(user_id: str) -> int:
    store = core_store._stores.get(user_id)
    if not store:
        return 0
    return len([m for m in store.chat_messages if m.get("role") == "user"])


def _norm(body):
    """Blank the volatile per-append user_message id/ts (a fresh row per call)."""
    if not isinstance(body, dict):
        return body
    b = dict(body)
    um = b.get("user_message")
    if isinstance(um, dict):
        b["user_message"] = {**um, "id": "<id>", "ts": "<ts>"}
    return b


# --------------------------------------------------------------------------- #
# Hosted-agent path — reply NOT ready within the wait window → 202 processing
# --------------------------------------------------------------------------- #

def test_processing_202_parity(env):
    monkeypatch = env
    uid, api_key = _register()
    _setup_openrouter(api_key, monkeypatch)
    # Real handle_send + build_processing_response; just no reply lands.
    monkeypatch.setattr(agent_runtime_cutover, "wait_for_reply", lambda *a, **k: None)

    traces: list = []
    monkeypatch.setattr(debug_trace, "trace_event", lambda *a, **k: traces.append(k))

    before = _user_msg_count(uid)
    f_status, f_body = _flask_post("/v1/model_api/chat/send", {"message": "hello"}, {"X-API-Key": api_key})
    assert _user_msg_count(uid) == before + 1  # exactly one append, no double
    flask_traces = list(traces)

    traces.clear()
    a_status, a_body = _asgi_post("/v1/model_api/chat/send", {"message": "hello"}, {"X-API-Key": api_key})
    assert _user_msg_count(uid) == before + 2  # ASGI appended exactly one more
    asgi_traces = list(traces)

    assert f_status == a_status == 202
    assert _norm(a_body) == _norm(f_body)
    assert f_body["status"] == "processing" and f_body["reply_ready"] is False
    assert f_body["runtime"]["driver"] == "pi"
    # debug_trace: route.decided / agent_runtime fired on BOTH backends.
    for tr in (flask_traces, asgi_traces):
        decided = [t for t in tr if t.get("type") == "route.decided"]
        assert any(t.get("detail", {}).get("mode") == "agent_runtime" for t in decided), tr


# --------------------------------------------------------------------------- #
# Hosted-agent path — reply landed within the wait window → 202 reply_ready
# --------------------------------------------------------------------------- #

def test_ready_202_parity(env):
    monkeypatch = env
    uid, api_key = _register()
    _setup_openrouter(api_key, monkeypatch)
    fake_reply = {"id": "asst-1", "ts": 999}
    monkeypatch.setattr(agent_runtime_cutover, "wait_for_reply", lambda *a, **k: fake_reply)

    f_status, f_body = _flask_post("/v1/model_api/chat/send", {"message": "hi"}, {"X-API-Key": api_key})
    a_status, a_body = _asgi_post("/v1/model_api/chat/send", {"message": "hi"}, {"X-API-Key": api_key})

    assert f_status == a_status == 202
    assert _norm(a_body) == _norm(f_body)
    assert f_body["reply_ready"] is True
    assert f_body["assistant_message"] == {"id": "asst-1", "ts": 999}
    assert f_body["status"] == "processing"  # still 202/processing, never 200/ok


# --------------------------------------------------------------------------- #
# Supervisor wedge guard — 503, and NO orphan user append (guard precedes append)
# --------------------------------------------------------------------------- #

def test_503_supervisor_down_parity_no_orphan(env):
    monkeypatch = env
    uid, api_key = _register()
    _setup_openrouter(api_key, monkeypatch)
    monkeypatch.setattr(
        agent_runtime_cutover, "check_supervisor_live",
        lambda **kw: (False, "stale_supervisor_heartbeat_120s"),
    )
    called = []
    monkeypatch.setattr(agent_runtime_cutover, "handle_send",
                        lambda *a, **k: (called.append(1), ({}, 202))[1])

    before = _user_msg_count(uid)
    f_status, f_body = _flask_post("/v1/model_api/chat/send", {"message": "x"}, {"X-API-Key": api_key})
    a_status, a_body = _asgi_post("/v1/model_api/chat/send", {"message": "x"}, {"X-API-Key": api_key})

    assert f_status == a_status == 503
    assert f_body == a_body == {"error": "hosting_runtime_unavailable", "reason": "stale_supervisor_heartbeat_120s"}
    assert called == []  # never routed
    assert _user_msg_count(uid) == before  # guard fired before append → no orphan


# --------------------------------------------------------------------------- #
# Unsupported provider — 409 parity, no orphan append (resolve precedes append)
# --------------------------------------------------------------------------- #

def test_409_unsupported_provider_parity(env):
    monkeypatch = env
    uid, api_key = _register()
    fake_runtime = provider_client.ProviderConfig(provider="bogus", model="x", api_key="k")
    monkeypatch.setattr(hosted_config_store, "_load_runtime_provider_config",
                        lambda store, api_key, **kw: fake_runtime)
    monkeypatch.setattr(hosted_config_store, "_ensure_model_api_runtime_profile",
                        lambda store, config=None, **kw: None)
    monkeypatch.setattr(hosted_config_store, "_load_model_api_config",
                        lambda store: {"provider": "bogus", "model": "x", "test_status": "ok"})

    before = _user_msg_count(uid)
    f_status, f_body = _flask_post("/v1/model_api/chat/send", {"message": "x"}, {"X-API-Key": api_key})
    a_status, a_body = _asgi_post("/v1/model_api/chat/send", {"message": "x"}, {"X-API-Key": api_key})

    assert f_status == a_status == 409
    assert f_body == a_body == {"error": "provider_not_configured"}
    assert _user_msg_count(uid) == before  # no orphan append


# --------------------------------------------------------------------------- #
# runtime-load failure — 400 + model_api_action_trace written (parity)
# --------------------------------------------------------------------------- #

def test_400_runtime_load_failure_writes_action_trace(env):
    monkeypatch = env
    uid, api_key = _register()
    monkeypatch.setattr(
        hosted_config_store, "_load_runtime_provider_config",
        lambda store, api_key, **kw: ({}, {"error": "model_api_key_decrypt_failed"}),
    )
    traces: list = []
    monkeypatch.setattr(hosted_config_store, "_append_model_api_action_trace",
                        lambda store, entry: traces.append(entry))

    f_status, f_body = _flask_post("/v1/model_api/chat/send", {"message": "x"}, {"X-API-Key": api_key})
    a_status, a_body = _asgi_post("/v1/model_api/chat/send", {"message": "x"}, {"X-API-Key": api_key})

    assert f_status == a_status == 400
    assert f_body == a_body == {"error": "model_api_key_decrypt_failed"}
    # One action-trace per backend, carrying the failed/load_runtime fields.
    assert len(traces) == 2
    for entry in traces:
        assert entry["status"] == "failed"
        assert entry["error"] == "model_api_key_decrypt_failed"
        assert entry["context"] == {"stage": "load_runtime"}
        assert "duration_ms" in entry


# --------------------------------------------------------------------------- #
# Validation + auth
# --------------------------------------------------------------------------- #

def test_message_required_400_parity(env):
    monkeypatch = env
    uid, api_key = _register()
    _setup_openrouter(api_key, monkeypatch)
    f_status, f_body = _flask_post("/v1/model_api/chat/send", {"message": ""}, {"X-API-Key": api_key})
    a_status, a_body = _asgi_post("/v1/model_api/chat/send", {"message": ""}, {"X-API-Key": api_key})
    assert f_status == a_status == 400
    assert f_body == a_body == {"error": "message required"}


def test_message_too_long_413_parity(env):
    monkeypatch = env
    uid, api_key = _register()
    _setup_openrouter(api_key, monkeypatch)
    big = "a" * 12001
    f_status, f_body = _flask_post("/v1/model_api/chat/send", {"message": big}, {"X-API-Key": api_key})
    a_status, a_body = _asgi_post("/v1/model_api/chat/send", {"message": big}, {"X-API-Key": api_key})
    assert f_status == a_status == 413
    assert f_body == a_body == {"error": "message too long", "max_chars": 12000}


def test_non_json_content_type_dropped_parity(env):
    """A text/plain body carrying JSON is ignored (Flask get_json(silent=True)
    content-type gate) → payload {} → message required. ASGI must match."""
    monkeypatch = env
    uid, api_key = _register()
    _setup_openrouter(api_key, monkeypatch)
    f_status, f_body = _flask_post(
        "/v1/model_api/chat/send", None, {"X-API-Key": api_key},
        raw='{"message": "hello"}', content_type="text/plain",
    )
    a_status, a_body = _asgi_post(
        "/v1/model_api/chat/send", None, {"X-API-Key": api_key},
        raw='{"message": "hello"}', content_type="text/plain",
    )
    assert f_status == a_status == 400
    assert f_body == a_body == {"error": "message required"}


# --------------------------------------------------------------------------- #
# File turns (chat file upload) — file envelope + image-repipe + rejection
# --------------------------------------------------------------------------- #

def test_send_file_stores_file_turn(env):
    monkeypatch = env
    uid, api_key = _register()
    _setup_openrouter(api_key, monkeypatch)
    monkeypatch.setattr(agent_runtime_cutover, "wait_for_reply", lambda *a, **k: None)

    body = {
        "content_type": "file",
        "file_name": "notes.md",
        "file_mime": "text/markdown",
        "file_b64": _b64(b"# Title\nbody\n"),
        "message": "read this",
    }
    status, resp_body = _asgi_post("/v1/model_api/chat/send", body, {"X-API-Key": api_key})
    assert status == 202, resp_body

    store = core_store._stores.get(uid)
    user_rows = [m for m in store.chat_messages if m.get("role") == "user"]
    last = user_rows[-1]
    assert last["content_type"] == "file"
    assert last["file_name"] == "notes.md"
    assert last["file_mime"] == "text/markdown"


def test_send_image_file_repipes_as_image(env):
    monkeypatch = env
    uid, api_key = _register()
    _setup_openrouter(api_key, monkeypatch)
    monkeypatch.setattr(agent_runtime_cutover, "wait_for_reply", lambda *a, **k: None)

    body = {
        "content_type": "file",
        "file_name": "pic.png",
        "file_mime": "image/png",
        "file_b64": _b64(b"\x89PNG\r\n\x1a\n"),
    }
    status, resp_body = _asgi_post("/v1/model_api/chat/send", body, {"X-API-Key": api_key})
    assert status == 202, resp_body

    store = core_store._stores.get(uid)
    user_rows = [m for m in store.chat_messages if m.get("role") == "user"]
    last = user_rows[-1]
    assert last["content_type"] == "image"


def test_send_unsupported_file_400(env):
    monkeypatch = env
    uid, api_key = _register()
    _setup_openrouter(api_key, monkeypatch)
    monkeypatch.setattr(agent_runtime_cutover, "wait_for_reply", lambda *a, **k: None)

    before = _user_msg_count(uid)
    body = {
        "content_type": "file",
        "file_name": "x.bin",
        "file_mime": "",
        "file_b64": _b64(b"\x00\x01\x02bin"),
    }
    status, resp_body = _asgi_post("/v1/model_api/chat/send", body, {"X-API-Key": api_key})
    assert status == 400
    assert resp_body["error"] == "unsupported_file_type"
    assert _user_msg_count(uid) == before  # rejected before append → no orphan


def test_bad_api_key_is_fixed_401(env):
    _register()
    status, body = _asgi_post("/v1/model_api/chat/send", {"message": "x"}, {"X-API-Key": "nope"})
    assert status == 401
    assert body == {"error": "unauthorized"}


def test_no_auth_is_401(env):
    _register()
    status, body = _asgi_post("/v1/model_api/chat/send", {"message": "x"})
    assert status == 401
    assert body == {"error": "unauthorized"}
