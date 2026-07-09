import sys, pathlib
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
    assert r.data == {"items": [1, 2], "limit": 50}
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
    def fake_actions(store, api_key, payload):
        seen["payload"] = payload
        return {"applied": 1}, 200
    monkeypatch.setattr(memory_core, "actions", fake_actions)
    r = cap_memory.write("STORE", api_key="k", params={"actions": [{"type": "memory.add"}]})
    assert r.ok is True and r.data == {"applied": 1}
    assert seen["payload"] == {"actions": [{"type": "memory.add"}]}


def test_index_caps_large_item_list(monkeypatch):
    monkeypatch.setattr(memory_core, "index",
                        lambda *a, **k: ({"items": list(range(1000)), "limit": 1000}, 200))
    r = cap_memory.index("STORE", params={})
    assert r.ok is True
    assert len(r.data["items"]) == 50


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
    assert r.data == {"items": [{"id": "1", "text": "hello"}]}
    assert captured["payload"] == {"query": "hello", "limit": 10}
    assert callable(captured["post_enclave"])


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
