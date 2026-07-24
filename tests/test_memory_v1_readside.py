from __future__ import annotations

import sys
import types
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import memory_readside_core as readside_core  # noqa: E402
from enclave import readside  # noqa: E402


def _moment(
    mid: str,
    *,
    owner: str = "usr_v1_read",
    importance: float = 0.5,
    pulse: float = 0.3,
    occurred_at: str = "2026-06-20T10:00:00",
    last_referenced_at: str | None = None,
) -> dict:
    return {
        "v": 1,
        "id": mid,
        "owner_user_id": owner,
        "visibility": "shared",
        "body_ct": f"ct_{mid}",
        "nonce": f"nonce_{mid}",
        "K_user": f"ku_{mid}",
        "K_enclave": f"ke_{mid}",
        "occurred_at": occurred_at,
        "created_at": occurred_at,
        "updated_at": occurred_at,
        "source": "test",
        "status": "active",
        "importance": importance,
        "pulse": pulse,
        "last_referenced_at": last_referenced_at or occurred_at,
    }


def test_index_core_defaults_to_full_light_index_not_top50(monkeypatch):
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_LIMIT", raising=False)
    monkeypatch.delenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", raising=False)
    store = types.SimpleNamespace(user_id="usr_v1_read")
    moments = [_moment(f"mem_{idx:03d}") for idx in range(62)]
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    body = readside_core.memory_index_core(store, "key", {}, post_enclave=fake_enclave)

    assert len(captured["ids"]) == 62
    assert body["truncated"] is False
    assert body["user_card_count"] == 62


def test_index_core_thread_query_does_not_truncate_before_enclave_filter(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1_read")
    moments = [_moment(f"bulk_{idx:02d}", importance=0.9, pulse=0.9) for idx in range(60)]
    moments.append(_moment("late_thread_match", importance=0.1, pulse=0.1, occurred_at="2026-01-01T00:00:00"))
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": "late_thread_match", "summary": "低分但 thread 命中"}]}

    body = readside_core.memory_index_core(store, "key", {"thread": "蛋子"}, post_enclave=fake_enclave)

    assert "late_thread_match" in captured["ids"]
    assert len(captured["ids"]) == 61
    assert captured["payload"]["thread"] == "蛋子"
    assert body["items"] == [{"id": "late_thread_match", "summary": "低分但 thread 命中"}]


def test_index_core_private_query_limit_is_applied_after_enclave_search(monkeypatch):
    monkeypatch.setenv("FEEDLING_MEMORY_READSIDE_HARD_MAX", "10")
    store = types.SimpleNamespace(user_id="usr_v1_read")
    moments = [
        _moment("high_score", importance=0.9, pulse=0.9),
        _moment("late_private_match", importance=0.1, pulse=0.1,
                occurred_at="2026-01-01T00:00:00"),
    ]
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": "late_private_match", "summary": "private match"}]}

    body = readside_core.memory_index_core(
        store,
        "key",
        {"query": "blue-orchid", "limit": 1},
        post_enclave=fake_enclave,
    )

    assert captured["ids"] == ["high_score", "late_private_match"]
    assert captured["payload"]["limit"] == 1
    assert body["limit"] == 1
    assert body["items"] == [{"id": "late_private_match", "summary": "private match"}]


def test_index_core_ambient_uses_importance_pulse_recency_order(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1_read")
    moments = [
        _moment("old_high_importance_low_pulse", importance=0.95, pulse=0.1, occurred_at="2026-01-01T00:00:00"),
        _moment("recent_medium_pulse", importance=0.6, pulse=0.6, occurred_at="2026-06-24T00:00:00"),
        _moment("recent_high_pulse", importance=0.7, pulse=0.9, occurred_at="2026-06-25T00:00:00"),
    ]
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: moments)
    captured = {}

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        captured["ids"] = [m["id"] for m in candidates]
        captured["payload"] = dict(payload or {})
        return {"items": [{"id": m["id"], "summary": m["id"]} for m in candidates]}

    readside_core.memory_index_core(store, "key", {"ambient": True, "ambient_top_n": 2}, post_enclave=fake_enclave)

    assert captured["ids"] == ["recent_high_pulse", "recent_medium_pulse"]
    assert captured["payload"]["ambient"] is True


def test_fetch_core_updates_last_referenced_at_for_returned_items(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1_read")
    moments = [
        _moment("ok_fetch", last_referenced_at="2026-06-20T00:00:00"),
        _moment("not_returned", last_referenced_at="2026-06-20T00:00:00"),
    ]
    saved: list[dict] = []
    monkeypatch.setattr(readside_core, "_now_iso", lambda: "2026-06-25T12:00:00+00:00")
    monkeypatch.setattr(readside_core.memory_service, "_load_moments", lambda _store: [dict(m) for m in moments])
    monkeypatch.setattr(readside_core.memory_service, "_save_moments", lambda _store, new_moments: saved.extend(new_moments))

    def fake_enclave(api_key, candidates, *, operation, payload=None):
        return {
            "items": [{"id": "ok_fetch", "summary": "returned"}],
            "unavailable_ids": [],
        }

    body = readside_core.memory_fetch_core(
        store,
        "key",
        {"ids": ["ok_fetch", "not_returned"]},
        post_enclave=fake_enclave,
    )

    assert [item["id"] for item in body["items"]] == ["ok_fetch"]
    assert next(m for m in saved if m["id"] == "ok_fetch")["last_referenced_at"] == "2026-06-25T12:00:00+00:00"
    assert next(m for m in saved if m["id"] == "not_returned")["last_referenced_at"] == "2026-06-20T00:00:00"


def test_memory_score_applies_decay_from_last_referenced_at(monkeypatch):
    monkeypatch.setattr(readside_core, "_now_ts", lambda: 90 * 86400.0)

    fresh = _moment("fresh", importance=0.6, last_referenced_at="1970-03-31T00:00:00+00:00")
    stale = _moment("stale", importance=0.6, last_referenced_at="1970-01-01T00:00:00+00:00")

    assert readside_core.memory_score(fresh) > readside_core.memory_score(stale)
    assert readside_core.memory_score(stale) == 0.0


def test_enclave_index_and_fetch_use_v1_shape_without_content_in_index():
    index_item = readside.build_memory_index_item(
        {
            "id": "mem_v1",
            "status": "active",
            "importance": 0.8,
            "pulse": 0.4,
            "occurred_at": "2026-06-20T10:00:00",
            "last_referenced_at": "2026-06-21T10:00:00",
        },
        {
            "summary": "用户有只猫叫武松。",
            "content": "记忆: 用户有只猫叫武松。\n上下文: 用户明确说过。\n使用提示: 宠物话题自然使用。",
            "bucket": "宠物",
            "threads": ["武松", "猫"],
        },
    )
    fetch_item = readside.build_memory_fetch_item(
        {"id": "mem_v1", "status": "active", "source": "chat"},
        {
            "summary": "用户有只猫叫武松。",
            "content": "记忆: 用户有只猫叫武松。\n上下文: 用户明确说过。\n使用提示: 宠物话题自然使用。",
            "bucket": "宠物",
            "threads": ["武松", "猫"],
        },
    )

    assert index_item == {
        "id": "mem_v1",
        "summary": "用户有只猫叫武松。",
        "bucket": "宠物",
        "threads": ["武松", "猫"],
        "importance": 0.8,
        "pulse": 0.4,
        "status": "active",
        "occurred_at": "2026-06-20T10:00:00",
        "created_at": "",
        "updated_at": "",
        "last_referenced_at": "2026-06-21T10:00:00",
        "is_sensitive": False,
        "score": 0.0,
    }
    assert "content" not in index_item
    assert fetch_item["content"].startswith("记忆: 用户有只猫叫武松。")
    assert fetch_item["bucket"] == "宠物"
    assert fetch_item["threads"] == ["武松", "猫"]
