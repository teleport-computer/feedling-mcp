from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, envelope as envmod, keys  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()
    return _AsgiTestClient(build_app())


@pytest.fixture()
def _authed(monkeypatch):
    async def fake_backend_get(path, headers, params=None):
        return {"user_id": "usr_a"}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)


def _v1_inner():
    return json.dumps({"summary": "s", "content": "c", "bucket": "b",
                       "threads": []}).encode()


def test_memory_list_head_supported(client, _authed):
    # Flask 自动挂 HEAD 的 parity（同 chat/history），405 即回归。
    r = client.open("/v1/memory/list", method="HEAD",
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    assert r.data == b""


def test_missing_key_space_spelling(client):
    r = client.post("/v1/memory/index", json={"moments": []})
    assert r.status_code == 401
    assert r.get_json() == {"error": "missing api_key"}  # 空格拼法


def test_index_moments_must_be_list(client, _authed):
    r = client.post("/v1/memory/index", json={"moments": "nope"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "moments must be a list"}


def test_index_decrypts_and_flags_unavailable(client, _authed, monkeypatch):
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: _v1_inner())
    moments = [
        {"id": "m1", "K_enclave": "x"},
        {"id": "m2", "visibility": "local_only"},
    ]
    r = client.post("/v1/memory/index", json={"moments": moments},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["user_id"] == "usr_a"
    assert [i["id"] for i in body["items"]] == ["m1"]
    assert body["unavailable_ids"] == ["m2"]


def test_index_query_matches_private_content_without_exposing_it(client, _authed, monkeypatch):
    private = json.dumps({
        "summary": "ordinary summary",
        "content": "the exact hidden phrase is blue-orchid-47",
        "bucket": "notes",
        "threads": [],
    }).encode()
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: private)

    r = client.post(
        "/v1/memory/index",
        json={"moments": [{"id": "m1", "K_enclave": "x"}], "query": "BLUE-ORCHID-47"},
        headers={"X-API-Key": "k"},
    )

    assert r.status_code == 200
    item = r.get_json()["items"][0]
    assert item["id"] == "m1"
    assert "content" not in item
    assert "_search_content" not in item


def test_index_query_filters_full_candidate_window_before_result_limit(
    client, _authed, monkeypatch
):
    plaintext_by_id = {
        "m1": json.dumps({
            "summary": "high-ranked non-match", "content": "ordinary",
            "bucket": "notes", "threads": [],
        }).encode(),
        "m2": json.dumps({
            "summary": "low-ranked match", "content": "blue-orchid-47",
            "bucket": "notes", "threads": [],
        }).encode(),
    }
    monkeypatch.setattr(
        envmod,
        "decrypt_envelope",
        lambda envelope, _user, _sk: plaintext_by_id[envelope["id"]],
    )

    r = client.post(
        "/v1/memory/index",
        json={
            "moments": [{"id": "m1", "K_enclave": "x"},
                        {"id": "m2", "K_enclave": "x"}],
            "query": "BLUE-ORCHID-47",
            "limit": 1,
        },
        headers={"X-API-Key": "k"},
    )

    assert r.status_code == 200
    assert [item["id"] for item in r.get_json()["items"]] == ["m2"]


def test_fetch_blocks_sensitive_by_default(client, _authed, monkeypatch):
    sensitive = json.dumps({"summary": "s", "content": "c", "bucket": "b",
                            "threads": [], "is_sensitive": True}).encode()
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: sensitive)
    r = client.post("/v1/memory/fetch",
                    json={"moments": [{"id": "m1", "K_enclave": "x"}]},
                    headers={"X-API-Key": "k"})
    body = r.get_json()
    assert body["items"] == []
    assert body["blocked_sensitive_ids"] == ["m1"]


def test_memory_list_decrypt_and_serve(client, _authed, monkeypatch):
    inner = json.dumps({"title": "t", "description": "d", "type": "fact"}).encode()
    async def fake_backend_get(path, headers, params=None):
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        assert path == "/v1/memory/list"
        return {"moments": [{"id": "m1", "v": 1, "K_enclave": "x"}], "total": 1}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: inner)
    r = client.get("/v1/memory/list", headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["moments"][0]["title"] == "t"
    assert body["moments"][0]["decrypt_status"] == "ok"


def test_worldbook_match_shape(client, _authed, monkeypatch):
    inner = json.dumps({"entries": []}).encode()
    monkeypatch.setattr(envmod, "decrypt_envelope", lambda e, u, s: inner)
    r = client.post("/v1/worldbook/match",
                    json={"world_books": [], "messages": []},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["user_id"] == "usr_a"
    assert body["unavailable_ids"] == []


def test_worldbook_messages_must_be_list(client, _authed):
    r = client.post("/v1/worldbook/match",
                    json={"world_books": [], "messages": "x"},
                    headers={"X-API-Key": "k"})
    assert r.status_code == 400
    assert r.get_json() == {"error": "messages must be a list"}


def test_runtime_token_only_forwards_token(client, monkeypatch):
    seen = []
    async def fake_backend_get(path, headers, params=None):
        seen.append(dict(headers or {}))
        if path == "/v1/users/whoami":
            return {"user_id": "usr_a"}
        return {"moments": [], "total": 0}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)
    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    r = client.get("/v1/memory/list",
                   headers={"X-Feedling-Runtime-Token": "tok-1"})
    assert r.status_code == 200
    # spec §7 回归：api_key 为空时所有 backend 调用转发 runtime token，非空 auth
    assert all(h == {"X-Feedling-Runtime-Token": "tok-1"} for h in seen)
    assert len(seen) == 2  # whoami + memory/list
