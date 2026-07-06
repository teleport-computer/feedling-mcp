from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import debug_trace  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    debug_trace._flag_cache.clear()
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c
    debug_trace._flag_cache.clear()


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


def _enable_trace(client, api_key: str) -> None:
    res = client.post("/v1/debug/trace/enable", headers=_headers(api_key), json={"enabled": True})
    assert res.status_code == 200, res.get_data(as_text=True)


def test_emit_event_records(client):
    _user_id, api_key = _register(client)
    _enable_trace(client, api_key)

    r = client.post(
        "/v1/debug/trace/event",
        headers=_headers(api_key),
        json={
            "event": {
                "subsystem": "agent",
                "type": "agent.model.call.done",
                "explain": "模型返回",
                "dur_ms": 2300,
            }
        },
    )
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json() == {"status": "ok"}

    g = client.get("/v1/debug/trace?limit=10", headers=_headers(api_key))
    assert g.status_code == 200, g.get_data(as_text=True)
    body = g.get_json()
    assert "verbose" in body
    assert any(e["type"] == "agent.model.call.done" for e in body["events"])
