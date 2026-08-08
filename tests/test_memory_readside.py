from __future__ import annotations

import sys
import types
from pathlib import Path
from urllib.parse import urlparse

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from asgi_test_client import _AsgiTestClient  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, keys, readside  # noqa: E402
from enclave import state as enclave_state  # noqa: E402
from enclave.routes import build_app  # noqa: E402
from memory import memory_core  # noqa: E402
from memory import service as memory_service  # noqa: E402


def _enclave_client(monkeypatch, *, user_id="usr_readside"):
    """Auth-wired ASGI client for the enclave's own memory readside routes
    (/v1/memory/index, /v1/memory/fetch) — distinct from the _CoreClient
    above, which hits the *backend*'s memory_core readside."""
    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    enclave_auth.reset_cache()

    async def fake_backend_get(path, headers, params=None):
        return {"user_id": user_id}
    monkeypatch.setattr(backend_client, "backend_get", fake_backend_get)

    async def fake_sk():
        return object()
    monkeypatch.setattr(keys, "get_content_sk", fake_sk)
    return _AsgiTestClient(build_app())


# The Flask /v1/memory/* readside routes were deleted in the ASGI cutover. This
# module-level hook is what the readside tests monkeypatch (previously
# ``memory.routes._memory_readside_post_enclave``); the _CoreClient below reads it
# at call time and threads it into the framework-neutral memory_core the ASGI
# routes delegate to.
_memory_readside_post_enclave = None


def _readside_post_enclave(api_key, candidates, *, operation, payload=None):
    return _memory_readside_post_enclave(api_key, candidates, operation=operation, payload=payload)


class _Resp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body

    def get_json(self):
        return self._body


class _CoreClient:
    """Mimics the old Flask test-client for the deleted memory readside routes,
    routing straight to memory_core with the current module-level post-enclave hook."""

    def __init__(self, store, api_key="key_readside"):
        self._store = store
        self._api_key = api_key

    def _key(self, headers):
        return (headers or {}).get("X-API-Key", self._api_key)

    def post(self, path, json=None, headers=None):
        p = urlparse(path).path
        api_key = self._key(headers)
        pe = _readside_post_enclave
        if p == "/v1/memory/index":
            return _Resp(*memory_core.index(self._store, api_key, json or {}, post_enclave=pe))
        if p == "/v1/memory/fetch":
            return _Resp(*memory_core.fetch(self._store, api_key, json or {}, post_enclave=pe))
        raise AssertionError(f"unrouted path: {p}")

    def get(self, path, headers=None):
        u = urlparse(path)
        p = u.path
        api_key = self._key(headers)
        pe = _readside_post_enclave
        if p == "/v1/memory/buckets":
            return _Resp(*memory_core.buckets(self._store, api_key, post_enclave=pe))
        if p == "/v1/memory/threads":
            return _Resp(*memory_core.threads(self._store, api_key, post_enclave=pe))
        raise AssertionError(f"unrouted path: {p}")


def _moment(
    mid: str,
    *,
    owner: str = "usr_readside",
    visibility: str = "shared",
    k_enclave: bool = True,
    status: str | None = "active",
    salience: str | None = "medium",
    importance: float | None = 0.5,
    occurred_at: str = "2026-06-20T10:00:00",
    is_open_thread: bool = False,
    archived: bool = False,
) -> dict:
    moment = {
        "v": 1,
        "id": mid,
        "owner_user_id": owner,
        "visibility": visibility,
        "body_ct": f"ct_{mid}",
        "nonce": f"nonce_{mid}",
        "K_user": f"ku_{mid}",
        "occurred_at": occurred_at,
        "created_at": occurred_at,
        "updated_at": occurred_at,
        "type": "fact",
        "source": "test",
    }
    if k_enclave:
        moment["K_enclave"] = f"ke_{mid}"
    if status is not None:
        moment["status"] = status
    if salience is not None:
        moment["salience"] = salience
    if importance is not None:
        moment["importance"] = importance
    if is_open_thread:
        moment["is_open_thread"] = True
    if archived:
        moment["archived_at"] = "2026-06-20T11:00:00"
    return moment


@pytest.fixture()
def client():
    store = types.SimpleNamespace(user_id="usr_readside")
    return _CoreClient(store, "key_readside"), store


def test_memory_index_sends_full_light_index_and_calls_enclave(client, monkeypatch):
    c, store = client
    moments = [
        _moment("open_old", salience="medium", importance=0.1, occurred_at="2026-06-19T10:00:00", is_open_thread=True),
        _moment("high_new", salience="high", importance=0.9, occurred_at="2026-06-20T10:00:00"),
        _moment("local", visibility="local_only"),
        _moment("no_enclave", k_enclave=False),
        _moment("archived", archived=True),
        _moment("deleted", status="deleted"),
        _moment("superseded", status="superseded"),
        _moment("other_user", owner="usr_other"),
    ]
    moments.extend(
        _moment(f"bulk_{idx:02d}", salience="low", importance=0.1, occurred_at=f"2026-06-18T{idx:02d}:00:00")
        for idx in range(60)
    )
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["api_key"] = api_key
        captured["operation"] = operation
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    monkeypatch.setattr(sys.modules[__name__], "_memory_readside_post_enclave", fake_enclave)

    res = c.post("/v1/memory/index", json={}, headers={"X-API-Key": "key_readside"})

    assert res.status_code == 200
    assert captured["api_key"] == "key_readside"
    assert captured["operation"] == "index"
    assert captured["payload"]["include_sensitive"] is False
    assert len(captured["ids"]) == 62
    assert captured["ids"][:2] == ["high_new", "open_old"]
    assert "local" not in captured["ids"]
    assert "no_enclave" not in captured["ids"]
    assert "archived" not in captured["ids"]
    assert "deleted" not in captured["ids"]
    assert "superseded" not in captured["ids"]
    assert "other_user" not in captured["ids"]


def test_memory_fetch_splits_missing_unavailable_and_preserves_order(client, monkeypatch):
    c, _store = client
    moments = [
        _moment("ok_b"),
        _moment("local", visibility="local_only"),
        _moment("ok_a"),
        _moment("archived", archived=True),
        _moment("superseded", status="superseded"),
        _moment("other_user", owner="usr_other"),
    ]
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: moments)
    monkeypatch.setattr(memory_service, "_save_moments", lambda _store, _moments: None)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        return {
            "items": [{"id": m["id"], "summary": f"summary {m['id']}"} for m in candidates],
            "unavailable_ids": [],
        }

    monkeypatch.setattr(sys.modules[__name__], "_memory_readside_post_enclave", fake_enclave)

    res = c.post(
        "/v1/memory/fetch",
        json={"ids": ["ok_a", "missing", "local", "ok_b", "archived", "superseded", "other_user"]},
        headers={"X-API-Key": "key_readside"},
    )

    assert res.status_code == 200
    body = res.get_json()
    assert captured["ids"] == ["ok_a", "ok_b"]
    assert [item["id"] for item in body["items"]] == ["ok_a", "ok_b"]
    assert body["missing_ids"] == ["missing", "other_user"]
    assert body["unavailable_ids"] == ["local", "archived", "superseded"]


def test_enclave_index_item_hides_content_body_field():
    item = readside.build_memory_index_item(
        {
            "id": "mem_1",
            "status": "active",
            "salience": "high",
            "is_open_thread": True,
            "score": 0.91,
        },
        {
            "summary": "She needs presence first.",
            "content": "记忆: She needs presence first.\n上下文: Do not expose this in index.\n使用提示: Only fetch should see this.",
            "bucket": "comfort",
            "threads": ["comfort"],
            "sensitive_scope": "xp_private_detail",
        },
    )

    assert item == {
        "id": "mem_1",
        "summary": "She needs presence first.",
        "bucket": "comfort",
        "threads": ["comfort"],
        "importance": 0.5,
        "pulse": 0.3,
        "status": "active",
        "occurred_at": "",
        "created_at": "",
        "updated_at": "",
        "last_referenced_at": "",
        "is_sensitive": True,
        "score": 0.91,
    }
    assert "content" not in item


def test_enclave_index_filters_sensitive_items_by_default(monkeypatch):
    c = _enclave_client(monkeypatch)
    monkeypatch.setattr(
        readside,
        "decrypt_readside_items",
        lambda moments, authorized_user_id, content_sk, *, item_builder: (
            [
                {"id": "plain", "summary": "Plain memory.", "is_sensitive": False},
                {"id": "sensitive", "summary": "Private memory.", "is_sensitive": True},
            ],
            [],
        ),
    )

    default_res = c.post("/v1/memory/index",
                         json={"moments": [{"id": "plain"}, {"id": "sensitive"}]},
                         headers={"X-API-Key": "key_readside"})
    sensitive_res = c.post(
        "/v1/memory/index",
        json={"moments": [{"id": "plain"}, {"id": "sensitive"}], "include_sensitive": True},
        headers={"X-API-Key": "key_readside"},
    )

    assert default_res.status_code == 200
    assert [item["id"] for item in default_res.get_json()["items"]] == ["plain"]
    assert sensitive_res.status_code == 200
    assert [item["id"] for item in sensitive_res.get_json()["items"]] == ["plain", "sensitive"]


def test_enclave_index_and_fetch_honor_payload_limit_above_50(monkeypatch):
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_LIMIT", raising=False)
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", raising=False)
    c = _enclave_client(monkeypatch)
    captured_lengths = []

    def fake_decrypt(moments, authorized_user_id, content_sk, *, item_builder):
        captured_lengths.append(len(moments))
        return ([{"id": str(moment.get("id")), "summary": str(moment.get("id"))} for moment in moments], [])

    monkeypatch.setattr(readside, "decrypt_readside_items", fake_decrypt)
    payload = {"limit": 120, "moments": [{"id": f"mem_{idx:03d}"} for idx in range(130)]}

    index_res = c.post("/v1/memory/index", json=payload, headers={"X-API-Key": "key_readside"})
    fetch_res = c.post("/v1/memory/fetch", json=payload, headers={"X-API-Key": "key_readside"})

    assert index_res.status_code == 200
    assert fetch_res.status_code == 200
    assert captured_lengths == [120, 120]


def test_enclave_limit_zero_uses_hard_max_instead_of_unbounded(monkeypatch):
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "7")
    c = _enclave_client(monkeypatch)
    captured_lengths = []

    def fake_decrypt(moments, authorized_user_id, content_sk, *, item_builder):
        captured_lengths.append(len(moments))
        return ([{"id": str(moment.get("id")), "summary": str(moment.get("id"))} for moment in moments], [])

    monkeypatch.setattr(readside, "decrypt_readside_items", fake_decrypt)
    payload = {"limit": 0, "moments": [{"id": f"mem_{idx:03d}"} for idx in range(20)]}

    res = c.post("/v1/memory/index", json=payload, headers={"X-API-Key": "key_readside"})

    assert res.status_code == 200
    assert captured_lengths == [7]


def test_enclave_negative_env_limit_falls_back_to_default_not_full_open(monkeypatch):
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_LIMIT", "-1")
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", raising=False)
    c = _enclave_client(monkeypatch)
    captured_lengths = []

    def fake_decrypt(moments, authorized_user_id, content_sk, *, item_builder):
        captured_lengths.append(len(moments))
        return ([{"id": str(moment.get("id")), "summary": str(moment.get("id"))} for moment in moments], [])

    monkeypatch.setattr(readside, "decrypt_readside_items", fake_decrypt)

    res = c.post("/v1/memory/index",
                 json={"moments": [{"id": f"mem_{idx:03d}"} for idx in range(70)]},
                 headers={"X-API-Key": "key_readside"})

    assert res.status_code == 200
    assert captured_lengths == [50]


def test_enclave_fetch_item_returns_v1_full_card_without_sensitive_scope():
    item = readside.build_memory_fetch_item(
        {"id": "mem_1", "status": "active", "salience": "high", "source": "chat"},
        {
            "summary": "She needs presence first.",
            "content": "记忆: She needs presence first.\n上下文: I wanted someone to stay.\n使用提示: Start with comfort.",
            "bucket": "comfort",
            "threads": ["comfort"],
            "sensitive_scope": "xp_private_detail",
        },
    )

    assert item == {
        "id": "mem_1",
        "summary": "She needs presence first.",
        "content": "记忆: She needs presence first.\n上下文: I wanted someone to stay.\n使用提示: Start with comfort.",
        "bucket": "comfort",
        "threads": ["comfort"],
        "importance": 0.5,
        "pulse": 0.3,
        "status": "active",
        "source": "chat",
        "occurred_at": "",
        "created_at": "",
        "updated_at": "",
        "last_referenced_at": "",
        "voice_call_id": "",
        "is_sensitive": True,
    }
    assert "sensitive_scope" not in item
