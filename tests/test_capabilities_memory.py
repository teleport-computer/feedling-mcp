import hashlib
import json
import sys, pathlib
from types import SimpleNamespace
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))  # noqa: E402

from memory import memory_core  # noqa: E402
from capabilities import memory as cap_memory  # noqa: E402


def test_index_wraps_core_body(monkeypatch):
    captured = {}
    def fake_index(store, api_key, payload, *, post_enclave):
        captured["payload"] = payload
        captured["post_enclave"] = post_enclave
        return {"items": [1, 2], "limit": 50}, 200
    monkeypatch.setattr(memory_core, "index", fake_index)

    r = cap_memory.index("STORE", api_key="k", runtime_token="rt", params={"limit": 50})
    assert r.ok is True
    assert r.data == {"items": [1, 2], "limit": 50, "total": 2, "returned": 2}
    assert captured["payload"] == {"limit": 50}
    assert callable(captured["post_enclave"])  # closure bound to runtime_token


def test_index_maps_503_retryable(monkeypatch):
    monkeypatch.setattr(memory_core, "index",
                        lambda *a, **k: ({"error": "enclave down"}, 503))
    r = cap_memory.index("STORE", params={})
    assert r.ok is False
    assert r.error == {"code": "capability_upstream_error", "message": "enclave down", "retryable": True}


def test_write_delegates_to_actions(monkeypatch):
    seen = {}
    def fake_actions(store, api_key, payload, *, runtime_token=""):
        seen["payload"] = payload
        seen["runtime_token"] = runtime_token
        return {"applied": 1}, 200
    monkeypatch.setattr(memory_core, "actions", fake_actions)
    r = cap_memory.write("STORE", api_key="k", params={"actions": [{"type": "memory.add"}]})
    assert r.ok is True and r.data == {"applied": 1}
    assert seen["payload"] == {"actions": [{"type": "memory.add"}]}
    assert seen["runtime_token"] == ""


def test_index_caps_large_item_list(monkeypatch):
    monkeypatch.setattr(memory_core, "index",
                        lambda *a, **k: ({"items": list(range(1000)), "limit": 1000}, 200))
    r = cap_memory.index("STORE", params={})
    assert r.ok is True
    assert len(r.data["items"]) == 50
    assert r.data["returned"] == r.data["total"] == 1000


def test_search_forwards_query_to_index(monkeypatch):
    captured = {}
    def fake_index(store, api_key, payload, *, post_enclave):
        captured["payload"] = payload
        captured["post_enclave"] = post_enclave
        return {"items": [{"id": "1", "text": "hello"}]}, 200
    monkeypatch.setattr(memory_core, "index", fake_index)

    r = cap_memory.search("STORE", api_key="k", runtime_token="rt",
                          params={"query": "hello", "limit": 10})
    assert r.ok is True
    assert r.data == {
        "items": [{"id": "1", "text": "hello"}],
        "total": 1,
        "returned": 1,
    }
    assert captured["payload"] == {"query": "hello", "limit": 10}
    assert callable(captured["post_enclave"])


def test_index_total_is_global_card_count_and_filters_pass_through(monkeypatch):
    captured = {}

    def fake_index(store, api_key, payload, *, post_enclave):
        captured["payload"] = payload
        return {
            "items": [{"id": "travel-1"}, {"id": "travel-2"}],
            "user_card_count": 103,
            "limit": 1000,
        }, 200

    monkeypatch.setattr(memory_core, "index", fake_index)

    result = cap_memory.index(
        "STORE",
        params={"bucket": "旅行", "thread": "京都"},
    )

    assert captured["payload"] == {"bucket": "旅行", "thread": "京都"}
    assert result.data["total"] == 103
    assert result.data["returned"] == 2


def test_search_requires_nonempty_query(monkeypatch):
    called = {"n": 0}
    def fake_index(*a, **k):
        called["n"] += 1
        return {}, 200
    monkeypatch.setattr(memory_core, "index", fake_index)

    r = cap_memory.search("STORE", params={})
    assert r.ok is False
    assert r.error["code"] == "capability_invalid_input"
    assert called["n"] == 0

    r2 = cap_memory.search("STORE", params={"query": "   "})
    assert r2.ok is False
    assert r2.error["code"] == "capability_invalid_input"
    assert called["n"] == 0


def _record_memory_trace(monkeypatch, *, response=None, failure: str = ""):
    events = []

    def fake_index_core(store, api_key, payload, *, post_enclave):
        if failure:
            raise RuntimeError(failure)
        return response or {"items": []}

    monkeypatch.setattr(
        memory_core.memory_readside_core, "memory_index_core", fake_index_core,
    )
    monkeypatch.setattr(
        memory_core.debug_trace,
        "trace_event",
        lambda _store, **event: events.append(event),
    )
    return events


def test_search_trace_is_distinct_from_index_and_uses_confirmed_hit_count(monkeypatch):
    events = _record_memory_trace(
        monkeypatch,
        response={"items": [{"id": "1"}, {"id": "2"}]},
    )
    store = SimpleNamespace(user_id="usr_trace")

    search = cap_memory.search(
        store,
        params={"query": "她的生日", "limit": 7},
    )
    index = cap_memory.index(store, params={"limit": 11})

    assert search.ok is True and index.ok is True
    assert [event["type"] for event in events] == [
        "memory.search.called",
        "memory.index.called",
    ]
    search_event = events[0]
    assert search_event["subsystem"] == "memory"
    assert search_event["actor"] == "agent"
    assert search_event["detail"]["counts"] == {"items": 2, "limit": 7}
    assert search_event["detail"]["query_fingerprint"] == hashlib.sha256(
        "她的生日".encode("utf-8")
    ).hexdigest()[:12]
    assert "她的生日" not in json.dumps(search_event, ensure_ascii=False)


def test_search_trace_fingerprint_is_stable_and_query_sensitive(monkeypatch):
    events = _record_memory_trace(monkeypatch, response={"items": []})
    store = SimpleNamespace(user_id="usr_trace")

    for query in ("同一个问题", "同一个问题", "另一个问题"):
        result = cap_memory.search(store, params={"query": query})
        assert result.ok is True

    fingerprints = [
        event["detail"]["query_fingerprint"] for event in events
    ]
    assert fingerprints[0] == fingerprints[1]
    assert fingerprints[0] != fingerprints[2]
    rendered = json.dumps(events, ensure_ascii=False)
    assert "同一个问题" not in rendered
    assert "另一个问题" not in rendered


def test_search_failure_still_emits_content_free_search_event(monkeypatch):
    secret_query = "不能进入埋点的私密问题"
    events = _record_memory_trace(
        monkeypatch,
        failure=f"enclave failed while searching {secret_query}",
    )

    result = cap_memory.search(
        SimpleNamespace(user_id="usr_trace"),
        params={"query": secret_query, "limit": 3},
    )

    assert result.ok is False
    assert result.error["code"] == "capability_upstream_error"
    assert len(events) == 1
    event = events[0]
    assert event["type"] == "memory.search.called"
    assert event["status"] == "failed"
    assert event["detail"]["counts"] == {"limit": 3}
    assert event["detail"]["query_fingerprint"] == hashlib.sha256(
        secret_query.encode("utf-8")
    ).hexdigest()[:12]
    assert secret_query not in json.dumps(event, ensure_ascii=False)
