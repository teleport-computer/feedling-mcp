"""notices.core：emit/resolve/list 的 upsert 去重 + 快照过滤 + never-raise
（spec Phase B / B1）。

Run:  python -m pytest tests/test_notices_core.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from notices import core  # noqa: E402


def _store(uid):
    # core 只碰 store.user_id——纯逻辑测试用轻量 shim，不必 get_store。
    return type("S", (), {"user_id": uid})()


def _uid():
    import uuid
    return "usr_" + uuid.uuid4().hex[:12]


def _emit(store, **over):
    kw = dict(source="genesis", error_class="genesis_failed", blame="system",
              severity="error", user_text="蒸馏失败", detail="boom",
              dedupe_key="genesis:job_ab12")
    kw.update(over)
    core.emit(store, **kw)


def test_emit_creates_notice():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s)
    rows = db.log_read_all(uid, core.NOTICES_STREAM)
    assert len(rows) == 1
    n = rows[0]
    assert n["source"] == "genesis" and n["dedupe_key"] == "genesis:job_ab12"
    assert n["occurrences"] == 1 and n["resolved"] is False and n["resolved_ts"] is None
    assert n["notice_id"].startswith("ntc_")
    assert n["first_ts"] == n["last_ts"]
    assert set(n.keys()) == {                      # doc 形状 == 契约 §四
        "notice_id", "source", "error_class", "blame", "severity", "user_text",
        "detail", "dedupe_key", "occurrences", "first_ts", "last_ts",
        "resolved", "resolved_ts"}


def test_emit_upsert_increments_occurrences():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s); _emit(s, detail="boom2", user_text="蒸馏失败(2)")
    rows = db.log_read_all(uid, core.NOTICES_STREAM)
    assert len(rows) == 1                            # 同 key 未 resolved → 合并
    assert rows[0]["occurrences"] == 2
    assert rows[0]["detail"] == "boom2" and rows[0]["user_text"] == "蒸馏失败(2)"
    assert rows[0]["last_ts"] >= rows[0]["first_ts"]


def test_emit_after_resolve_creates_new_notice():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s)
    core.resolve(s, "genesis:")
    _emit(s)                                         # 已 resolved → 新建
    rows = db.log_read_all(uid, core.NOTICES_STREAM)
    assert len(rows) == 2
    assert rows[0]["resolved"] is True
    assert rows[1]["resolved"] is False and rows[1]["occurrences"] == 1
    assert rows[0]["notice_id"] != rows[1]["notice_id"]


def test_resolve_prefix_marks_resolved():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s, dedupe_key="chat:quota_insufficient")
    _emit(s, dedupe_key="chat:rate_limited")
    _emit(s, dedupe_key="genesis:job_x")
    core.resolve(s, "chat:")
    rows = {r["dedupe_key"]: r for r in db.log_read_all(uid, core.NOTICES_STREAM)}
    assert rows["chat:quota_insufficient"]["resolved"] is True
    assert rows["chat:rate_limited"]["resolved"] is True
    assert rows["genesis:job_x"]["resolved"] is False   # 前缀不匹配，不动


def test_emit_never_raises_on_store_error(monkeypatch):
    uid = _uid(); seed_user(uid); s = _store(uid)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(core.db, "log_read_all", boom)
    _emit(s, dedupe_key="genesis:x")                 # 不抛
    monkeypatch.setattr(core.db, "log_read_all", lambda *a, **k: [])
    monkeypatch.setattr(core.db, "log_append", boom)
    _emit(s, dedupe_key="genesis:y")                 # 不抛


def test_resolve_never_raises_on_store_error(monkeypatch):
    uid = _uid(); seed_user(uid); s = _store(uid)

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(core.db, "log_read_all", boom)
    core.resolve(s, "chat:")                         # 不抛


def test_bad_enum_dropped():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s, source="not_a_source", dedupe_key="x:1")
    _emit(s, blame="somebody", dedupe_key="x:2")
    _emit(s, severity="loud", dedupe_key="x:3")
    assert db.log_read_all(uid, core.NOTICES_STREAM) == []


def test_detail_clipped_to_300():
    uid = _uid(); seed_user(uid); s = _store(uid)
    _emit(s, detail="z" * 900)
    assert len(db.log_read_all(uid, core.NOTICES_STREAM)[0]["detail"]) == 300


def test_trim_caps_rows(monkeypatch):
    uid = _uid(); seed_user(uid); s = _store(uid)
    monkeypatch.setattr(core, "NOTICES_MAX", 3)
    for i in range(5):
        _emit(s, dedupe_key=f"genesis:job_{i}")
    assert len(db.log_read_all(uid, core.NOTICES_STREAM)) == 3   # 只留最新 3


def test_list_active_and_resolved_window():
    uid = _uid(); seed_user(uid); s = _store(uid)
    import time
    now = time.time()
    # 直接 log_append 精确控制 ts，避开时间 mock：
    db.log_append(uid, core.NOTICES_STREAM, _doc("chat:active", now, resolved=False),
                  ts=now, item_key="chat:active")
    db.log_append(uid, core.NOTICES_STREAM,
                  _doc("chat:recent", now - 10, resolved=True, resolved_ts=now - 10),
                  ts=now - 10, item_key="chat:recent")
    db.log_append(uid, core.NOTICES_STREAM,
                  _doc("chat:old", now - 30 * 86400, resolved=True,
                       resolved_ts=now - 30 * 86400),
                  ts=now - 30 * 86400, item_key="chat:old")
    body, status = core.list_notices(s, include_resolved=True)
    assert status == 200
    keys = [n["dedupe_key"] for n in body["notices"]]
    assert keys == ["chat:active", "chat:recent"]    # old 超 7d 窗口被滤；按 last_ts 倒序
    body2, _ = core.list_notices(s, include_resolved=False)
    assert [n["dedupe_key"] for n in body2["notices"]] == ["chat:active"]


def _doc(key, ts, *, resolved, resolved_ts=None):
    return {
        "notice_id": "ntc_" + key.replace(":", "_"), "source": "chat",
        "error_class": "quota_insufficient", "blame": "user_provider",
        "severity": "error", "user_text": "x", "detail": "", "dedupe_key": key,
        "occurrences": 1, "first_ts": ts, "last_ts": ts,
        "resolved": resolved, "resolved_ts": resolved_ts}


def test_list_dedupes_same_key_keeping_latest():
    """同 dedupe_key 的重复未 resolved 行（emit 非原子/DB 抖动可能留下）→
    读侧只回最新一条，兑现契约「同 key 始终只有一条」。"""
    uid = _uid(); seed_user(uid); s = _store(uid)
    import time
    now = time.time()
    db.log_append(uid, core.NOTICES_STREAM,
                  _doc("chat:dup", now - 5, resolved=False), ts=now - 5, item_key="chat:dup")
    db.log_append(uid, core.NOTICES_STREAM,
                  _doc("chat:dup", now, resolved=False), ts=now, item_key="chat:dup")
    body, _ = core.list_notices(s)
    dups = [n for n in body["notices"] if n["dedupe_key"] == "chat:dup"]
    assert len(dups) == 1
    assert dups[0]["last_ts"] == now      # 保留的是最新那条


def test_list_dedupe_resolved_then_active_shows_active():
    """resolve 后又 emit 新增（resolved 行 + active 新行同 key）→ 读侧只回最新的 active。"""
    uid = _uid(); seed_user(uid); s = _store(uid)
    import time
    now = time.time()
    db.log_append(uid, core.NOTICES_STREAM,
                  _doc("chat:x", now - 10, resolved=True, resolved_ts=now - 10),
                  ts=now - 10, item_key="chat:x")
    db.log_append(uid, core.NOTICES_STREAM,
                  _doc("chat:x", now, resolved=False), ts=now, item_key="chat:x")
    body, _ = core.list_notices(s)
    xs = [n for n in body["notices"] if n["dedupe_key"] == "chat:x"]
    assert len(xs) == 1 and xs[0]["resolved"] is False
