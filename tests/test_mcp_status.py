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
    summaries = mcp_status.runtime_summaries_for_store(store)
    assert summaries == {
        "flaky": {
            "last_kind": "timeout",
            "last_at": 1011.0,
            "recent_ok": 0,
            "recent_total": 10,
        },
        "safe": {
            "last_kind": "available",
            "last_at": 1011.0,
            "recent_ok": 10,
            "recent_total": 10,
        },
    }
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


# ---------------------------------------------------------------------------
# V1 (resident_cli / 自托管) 的连接状态回写
#
# spec: docs/superpowers/specs/2026-08-13-mcp-handshake-wait-hint-design.md §2.3
# 在这之前 record_runtime_results 只有 v2/serve_worker 一处调用 —— 2026-08-13
# prod 上 340 个激活用户里 323 个跑在 V1,他们 GET /v1/mcp/servers 的 runtime
# 字段永远是空的,app 没法告诉他们哪台服务器是真坏了。
# ---------------------------------------------------------------------------


def test_v1_verdicts_land_in_the_public_summary(monkeypatch):
    _fake_blob_db(monkeypatch)
    store = types.SimpleNamespace(user_id="usr_v1")

    assert mcp_status.record_from_registered_trace(store, {
        "verdict": {"tavily_": "ok", "fetch_": "failed", "slow": "recovered"},
    }) is True

    summaries = mcp_status.runtime_summaries_for_store(store)
    assert summaries["tavily_"]["last_kind"] == "available"
    # 启动时没就绪、但真调通了 —— 用户问的是「能不能用」,不是「快不快」
    assert summaries["slow"]["last_kind"] == "available"
    assert summaries["fetch_"]["last_kind"] == "unavailable"


def test_inconclusive_is_not_reported_as_a_failure(monkeypatch):
    """「模型这一轮没调用它」不等于「它坏了」。折进 unavailable 会让每一台
    用户这轮碰巧没用到的服务器都在 app 里亮红点 —— 那正是 consumer 那道
    verdict 阶梯专门要区分开的东西。"""
    _fake_blob_db(monkeypatch)
    store = types.SimpleNamespace(user_id="usr_idle")

    mcp_status.record_from_registered_trace(store, {
        "verdict": {"idle_one": "inconclusive"},
    })
    assert mcp_status.runtime_summaries_for_store(store) == {}, (
        "没有观测就不该出现在公开摘要里,更不该算成一次失败")


def test_inconclusive_turns_do_not_dilute_the_success_ratio(monkeypatch):
    """not_observed 既不进分子也不进分母。否则一台一直健康、只是用户没用的
    服务器,会随着时间显示成「10 次里只成功 1 次」。"""
    _fake_blob_db(monkeypatch)
    store = types.SimpleNamespace(user_id="usr_ratio")

    mcp_status.record_from_registered_trace(store, {"verdict": {"s": "ok"}})
    for _ in range(3):
        mcp_status.record_from_registered_trace(
            store, {"verdict": {"s": "inconclusive"}})

    summary = mcp_status.runtime_summaries_for_store(store)["s"]
    assert (summary["recent_ok"], summary["recent_total"]) == (1, 1)
    assert summary["last_kind"] == "available", "最后一次真实观测才是结论"


def test_an_unusable_payload_writes_nothing(monkeypatch):
    """解析不出判据的 trace 不是一次观测。凭空补一条,就是把「没有信号」
    变成假绿 —— 这个埋点存在的理由正是反过来的。"""
    _fake_blob_db(monkeypatch)
    store = types.SimpleNamespace(user_id="usr_bad")

    for bad in (None, {}, {"verdict": {}}, {"verdict": "ok"},
                {"verdict": {"s": "brand_new_state_from_the_future"}}):
        assert mcp_status.record_from_registered_trace(store, bad) is False
    assert mcp_status.runtime_summaries_for_store(store) == {}
