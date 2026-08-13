from __future__ import annotations

import sys
import types
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import memory_readside_core as readside_core  # noqa: E402


def _moment(
    mid: str,
    *,
    owner: str = "usr_core",
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


def _plaintext_moment(mid: str, summary: str, *, content: str = "") -> dict:
    moment = _moment(mid, k_enclave=False)
    moment.pop("body_ct", None)
    moment.pop("nonce", None)
    moment.pop("K_user", None)
    moment["body"] = __import__("json").dumps({
        "summary": summary,
        "content": content or summary,
        "bucket": "未分类",
        "threads": [],
    }, ensure_ascii=False)
    return moment


def test_plaintext_memory_index_never_posts_enclave(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_core")
    monkeypatch.setattr(
        readside_core.memory_service,
        "_load_moments",
        lambda _store: [_plaintext_moment("m1", "needle summary")],
    )

    result = readside_core.memory_index_core(
        store,
        None,
        {"query": "needle"},
        post_enclave=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("plaintext memory must not post to enclave")
        ),
    )

    assert [item["id"] for item in result["items"]] == ["m1"]


def test_mixed_memory_fetch_posts_only_ciphertext(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_core")
    plaintext = _plaintext_moment("p", "plain")
    ciphertext = _moment("c")
    monkeypatch.setattr(
        readside_core.memory_service,
        "_load_moments",
        lambda _store: [plaintext, ciphertext],
    )
    monkeypatch.setattr(readside_core.memory_service, "_save_moments", lambda *_args: None)
    seen = []

    def post(_api_key, candidates, *, operation, payload=None):
        seen.extend(candidates)
        return {
            "items": [{"id": "c", "summary": "cipher"}],
            "unavailable_ids": [],
        }

    result = readside_core.memory_fetch_core(
        store,
        "key",
        {"ids": ["p", "c"]},
        post_enclave=post,
    )

    assert [row["id"] for row in seen] == ["c"]
    assert all("body" not in row and "body_b64" not in row for row in seen)
    assert [item["id"] for item in result["items"]] == ["p", "c"]


def test_index_core_prefilters_sorts_caps_and_reports_card_count(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_core")
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
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["api_key"] = api_key
        captured["operation"] = operation
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    monkeypatch.setattr(readside_core, "post_enclave_readside", fake_enclave)

    body = readside_core.memory_index_core(store, "key_core", {"limit": 50})

    assert captured["api_key"] == "key_core"
    assert captured["operation"] == "index"
    assert captured["payload"]["include_sensitive"] is False
    assert len(captured["ids"]) == 50
    assert captured["ids"][:2] == ["high_new", "open_old"]
    assert body["user_card_count"] == 62
    assert body["limit"] == 50
    assert body["items"][0]["id"] == "high_new"
    assert "local" not in captured["ids"]
    assert "no_enclave" not in captured["ids"]
    assert "archived" not in captured["ids"]
    assert "deleted" not in captured["ids"]
    assert "superseded" not in captured["ids"]
    assert "other_user" not in captured["ids"]


def test_index_core_ignores_old_recall_window_env_and_returns_full_light_index(monkeypatch):
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_LIMIT", "120")
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", raising=False)
    store = types.SimpleNamespace(user_id="usr_core")
    moments = [_moment(f"card_{idx:03d}", occurred_at=f"2026-06-20T{idx % 24:02d}:00:00") for idx in range(130)]
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    monkeypatch.setattr(readside_core, "post_enclave_readside", fake_enclave)

    body = readside_core.memory_index_core(store, "key_core", {})

    assert len(captured["ids"]) == 130
    assert captured["payload"]["limit"] == 1000
    assert body["limit"] == 1000
    assert body["user_card_count"] == 130
    assert body["truncated"] is False


def test_index_core_limit_zero_opens_window_but_keeps_eligibility_and_sort(monkeypatch):
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_LIMIT", "0")
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "1000")
    store = types.SimpleNamespace(user_id="usr_core")
    moments = [
        _moment("low_old", salience="low", importance=0.1, occurred_at="2026-06-18T10:00:00"),
        _moment("open_thread", salience="low", importance=0.1, occurred_at="2026-06-18T09:00:00", is_open_thread=True),
        _moment("high_new", salience="high", importance=0.9, occurred_at="2026-06-20T10:00:00"),
        _moment("local", visibility="local_only"),
        _moment("superseded", status="superseded"),
        _moment("archived", archived=True),
    ]
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    monkeypatch.setattr(readside_core.memory_service, "_save_moments", lambda _store, _moments: None)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    monkeypatch.setattr(readside_core, "post_enclave_readside", fake_enclave)

    body = readside_core.memory_index_core(store, "key_core", {"query": "猫"})

    assert captured["ids"] == ["high_new", "open_thread", "low_old"]
    assert captured["payload"]["query"] == "猫"
    assert captured["payload"]["limit"] == 1000
    assert body["limit"] == 1000
    assert body["user_card_count"] == 3
    assert body["truncated"] is False


def test_index_core_hard_max_caps_full_open_window(monkeypatch):
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_LIMIT", "0")
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "3")
    store = types.SimpleNamespace(user_id="usr_core")
    moments = [_moment(f"card_{idx}") for idx in range(8)]
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    monkeypatch.setattr(readside_core, "post_enclave_readside", fake_enclave)

    body = readside_core.memory_index_core(store, "key_core", {})

    assert len(captured["ids"]) == 3
    assert captured["payload"]["limit"] == 3
    assert body["limit"] == 3
    assert body["user_card_count"] == 8
    assert body["truncated"] is True


def test_index_core_exact_query_pages_past_hard_max(monkeypatch):
    """HARD_MAX bounds each enclave request, never the searchable corpus."""
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "3")
    store = types.SimpleNamespace(user_id="usr_core")
    moments = [
        _moment(f"card_{idx}", importance=1.0 - idx / 10,
                occurred_at=f"2026-06-{20 - idx:02d}T00:00:00")
        for idx in range(7)
    ]
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    pages: list[list[str]] = []

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        ids = [m["id"] for m in candidates]
        pages.append(ids)
        return {
            "items": ([{"id": "card_6", "summary": "exact late match"}]
                      if "card_6" in ids else [])
        }

    body = readside_core.memory_index_core(
        store,
        "key_core",
        {"query": "exact late match", "limit": 1},
        post_enclave=fake_enclave,
    )

    assert [len(page) for page in pages] == [3, 3, 1]
    assert body["items"] == [{"id": "card_6", "summary": "exact late match"}]
    assert body["user_card_count"] == 7
    assert body["truncated"] is False


def test_index_core_negative_env_limit_is_ignored_by_v1_full_index(monkeypatch):
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_LIMIT", "-1")
    store = types.SimpleNamespace(user_id="usr_core")
    moments = [_moment(f"card_{idx:03d}") for idx in range(70)]
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    monkeypatch.setattr(readside_core, "post_enclave_readside", fake_enclave)

    body = readside_core.memory_index_core(store, "key_core", {})

    assert len(captured["ids"]) == 70
    assert captured["payload"]["limit"] == 1000
    assert body["limit"] == 1000


def test_fetch_core_splits_missing_unavailable_and_preserves_order(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_core")
    moments = [
        _moment("ok_b"),
        _moment("local", visibility="local_only"),
        _moment("ok_a"),
        _moment("archived", archived=True),
        _moment("superseded", status="superseded"),
        _moment("other_user", owner="usr_other"),
    ]
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    monkeypatch.setattr(readside_core.memory_service, "_save_moments", lambda _store, _moments: None)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        return {
            "items": [{"id": m["id"], "summary": f"summary {m['id']}"} for m in candidates],
            "unavailable_ids": [],
        }

    monkeypatch.setattr(readside_core, "post_enclave_readside", fake_enclave)

    body = readside_core.memory_fetch_core(
        store,
        "key_core",
        {"ids": ["ok_a", "missing", "local", "ok_b", "archived", "superseded", "other_user"]},
    )

    assert captured["ids"] == ["ok_a", "ok_b"]
    assert [item["id"] for item in body["items"]] == ["ok_a", "ok_b"]
    assert body["missing_ids"] == ["missing", "other_user"]
    assert body["unavailable_ids"] == ["local", "archived", "superseded"]


class _FakeResp:
    status_code = 200
    text = ""

    def json(self):
        return {"items": []}


class _FakeClient:
    """Captures the headers post_enclave_readside sends to the enclave."""

    captured: dict = {}

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        _FakeClient.captured = {"url": url, "headers": headers}
        return _FakeResp()


def test_post_enclave_readside_forwards_runtime_token(monkeypatch):
    # Runtime V2 workers have a scoped runtime token but no account API key.
    # The enclave call must forward X-Feedling-Runtime-Token — without this every
    # hosted agent memory read 503s with api_key_unavailable though data is present.
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "http://enclave:5003")
    monkeypatch.setattr(readside_core.httpx, "Client", _FakeClient)

    out = readside_core.post_enclave_readside(
        None, [], operation="index", runtime_token="rt_abc")
    assert out == {"items": []}
    assert _FakeClient.captured["headers"] == {"X-Feedling-Runtime-Token": "rt_abc"}

    # api_key path unchanged (back-compat); token preferred when both present.
    readside_core.post_enclave_readside("ak_1", [], operation="index")
    assert _FakeClient.captured["headers"] == {"X-API-Key": "ak_1"}
    readside_core.post_enclave_readside("ak_1", [], operation="index", runtime_token="rt_x")
    assert _FakeClient.captured["headers"] == {"X-Feedling-Runtime-Token": "rt_x"}


def test_post_enclave_readside_requires_some_credential(monkeypatch):
    import pytest
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "http://enclave:5003")
    with pytest.raises(RuntimeError, match="api_key_unavailable"):
        readside_core.post_enclave_readside(None, [], operation="index")
