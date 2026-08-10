"""Bounded, content-free V2 MCP runtime status persistence."""

import copy
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import mcp_status  # noqa: E402


def _fake_blob_db(monkeypatch):
    blobs = {}

    def get_blob(user_id, kind):
        value = blobs.get((user_id, kind))
        return copy.deepcopy(value)

    def set_blob_if_unchanged(
        user_id, kind, expected, new_doc, *, insert_if_missing=False,
    ):
        key = (user_id, kind)
        if key not in blobs:
            if not (insert_if_missing and expected == {}):
                return False
        elif blobs[key] != expected:
            return False
        blobs[key] = copy.deepcopy(new_doc)
        return True

    monkeypatch.setattr(mcp_status.db, "get_blob", get_blob)
    monkeypatch.setattr(
        mcp_status.db, "set_blob_if_unchanged", set_blob_if_unchanged)
    return blobs


def test_runtime_status_is_bounded_prunes_disabled_servers_and_never_persists_secrets(
    monkeypatch,
):
    blobs = _fake_blob_db(monkeypatch)
    store = types.SimpleNamespace(user_id="usr_status")

    for turn in range(12):
        assert mcp_status.record_runtime_results(store, [
            {
                "name": "safe",
                "kind": "available",
                "url": "https://must-not-persist.example/mcp",
                "headers": {"Authorization": "Bearer secret"},
            },
            {"name": "flaky", "kind": "timeout", "detail": "private body"},
        ], now=1000 + turn)

    doc = blobs[(store.user_id, mcp_status.RUNTIME_STATUS_BLOB)]
    assert len(doc["servers"]["safe"]["recent"]) == mcp_status.MAX_RECENT_TURNS
    assert len(doc["servers"]["flaky"]["recent"]) == mcp_status.MAX_RECENT_TURNS
    assert doc["servers"]["safe"]["recent"][0]["at"] == 1002
    dumped = json.dumps(doc)
    assert "must-not-persist" not in dumped
    assert "Bearer secret" not in dumped
    assert "private body" not in dumped

    # A successfully observed enabled-server set is authoritative. A disabled
    # or deleted server absent from the next turn must not keep a stale red row.
    assert mcp_status.record_runtime_results(
        store, [{"name": "safe", "kind": "available"}], now=2000)
    current = mcp_status.runtime_status_for_store(store)
    assert set(current["servers"]) == {"safe"}
    assert current["servers"]["safe"]["last_kind"] == "available"
    assert len(current["servers"]["safe"]["recent"]) == 10

    assert mcp_status.record_runtime_results(store, [], now=2001)
    assert mcp_status.runtime_status_for_store(store)["servers"] == {}


def test_runtime_status_write_rejects_dirty_names_and_kinds(monkeypatch):
    blobs = _fake_blob_db(monkeypatch)
    store = types.SimpleNamespace(user_id="usr_dirty_status")
    dirty_kind_url = "https://secret.example/mcp"
    dirty_kind_space = "transport failure"
    dirty_kind_long = "x" * 49

    assert mcp_status.record_runtime_results(store, [
        {"name": "safe", "kind": "available"},
        {"name": "bad/name", "kind": "available"},
        {"name": "urlkind", "kind": dirty_kind_url},
        {"name": "spacekind", "kind": dirty_kind_space},
        {"name": "longkind", "kind": dirty_kind_long},
    ], now=1000)

    doc = blobs[(store.user_id, mcp_status.RUNTIME_STATUS_BLOB)]
    assert set(doc["servers"]) == {"safe"}
    dumped = json.dumps(doc)
    assert "bad/name" not in dumped
    assert dirty_kind_url not in dumped
    assert dirty_kind_space not in dumped
    assert dirty_kind_long not in dumped


def test_runtime_status_retries_a_concurrent_cas_loss(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_cas")
    state = {"doc": None, "cas_calls": 0}

    monkeypatch.setattr(
        mcp_status.db,
        "get_blob",
        lambda _user_id, _kind: copy.deepcopy(state["doc"]),
    )

    def cas(_user_id, _kind, expected, new_doc, *, insert_if_missing=False):
        state["cas_calls"] += 1
        if state["cas_calls"] == 1:
            state["doc"] = {
                "version": 1,
                "updated_at": 9,
                "servers": {
                    "safe": {
                        "last_at": 9,
                        "last_kind": "timeout",
                        "recent": [{"at": 9, "kind": "timeout"}],
                    },
                },
            }
            return False
        assert state["doc"] == expected
        state["doc"] = copy.deepcopy(new_doc)
        return True

    monkeypatch.setattr(mcp_status.db, "set_blob_if_unchanged", cas)

    assert mcp_status.record_runtime_results(
        store, [{"name": "safe", "kind": "available"}], now=10)
    assert state["cas_calls"] == 2
    assert state["doc"]["servers"]["safe"]["recent"] == [
        {"at": 9.0, "kind": "timeout"},
        {"at": 10.0, "kind": "available"},
    ]

    # A delayed writer may win CAS after a newer observation. Keep history
    # ordered by observation time and never let it replace the latest verdict.
    assert mcp_status.record_runtime_results(
        store, [{"name": "safe", "kind": "timeout"}], now=8)
    assert state["doc"]["servers"]["safe"]["last_at"] == 10.0
    assert state["doc"]["servers"]["safe"]["last_kind"] == "available"
    assert state["doc"]["updated_at"] == 10.0
    assert [row["at"] for row in state["doc"]["servers"]["safe"]["recent"]] == [
        8.0, 9.0, 10.0,
    ]


def test_empty_status_is_a_noop_when_there_is_nothing_to_clear(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_empty")
    writes = []
    monkeypatch.setattr(mcp_status.db, "get_blob", lambda *_args: None)
    monkeypatch.setattr(
        mcp_status.db,
        "set_blob_if_unchanged",
        lambda *_args, **_kwargs: writes.append(1) or True,
    )

    assert mcp_status.record_runtime_results(store, [], now=10)
    assert writes == []


def test_runtime_status_reader_drops_malformed_or_unbounded_fields(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_read")
    raw = {
        "version": 1,
        "updated_at": "12.5",
        "servers": {
            "safe": {
                "last_kind": "forged",
                "secret": "do-not-return",
                "recent": [{"at": i, "kind": "available"} for i in range(1, 20)],
            },
            "INVALID NAME": {"recent": [{"at": 1, "kind": "available"}]},
        },
    }
    monkeypatch.setattr(mcp_status.db, "get_blob", lambda *_args: raw)

    result = mcp_status.runtime_status_for_store(store)

    assert result["updated_at"] == 12.5
    assert set(result["servers"]) == {"safe"}
    assert len(result["servers"]["safe"]["recent"]) == 10
    assert result["servers"]["safe"]["last_at"] == 19.0
    assert "secret" not in result["servers"]["safe"]
