"""runner spawn 失败/密钥/降级 → user_notices + 60s 去抖（spec Phase C / C4）。
Run:  python -m pytest tests/test_runner_notice.py -q
"""
from __future__ import annotations
import sys, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
from conftest import seed_user  # noqa: E402
from notices import core as notices_core  # noqa: E402


def _uid():
    return "usr_" + uuid.uuid4().hex[:12]


def _rows(uid):
    return {r["dedupe_key"]: r for r in db.log_read_all(uid, notices_core.NOTICES_STREAM)}


def _make_sup(monkeypatch, *, clock=None):
    from agent_runtime import supervisor as sup_mod
    kwargs = dict(spawn_fn=lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
                  alive_fn=lambda pid: False, kill_fn=lambda pid: None,
                  owner="test-owner", lease_ttl=30.0, data_root="/agent-data")
    if clock is not None:
        kwargs["now"] = clock
    return sup_mod.Supervisor(**kwargs)


def test_spawn_failure_emits_and_debounces(monkeypatch):
    uid = _uid(); seed_user(uid)
    sup = _make_sup(monkeypatch)
    sup._emit_runner_notice(uid, "runner_spawn_failed", "boom")
    n = _rows(uid)["runner:spawn_failed"]
    assert n["source"] == "runner" and n["error_class"] == "runner_spawn_failed"
    assert n["blame"] == "system"
    # 60s 内重复不新写（occurrences 不应再涨——去抖拦在 emit 之前）
    before = n["occurrences"]
    sup._emit_runner_notice(uid, "runner_spawn_failed", "boom2")
    assert _rows(uid)["runner:spawn_failed"]["occurrences"] == before


def test_key_decrypt_blame_system(monkeypatch):
    uid = _uid(); seed_user(uid)
    sup = _make_sup(monkeypatch)
    sup._emit_runner_notice(uid, "runner_key_decrypt_failed", "decrypt fail")
    assert _rows(uid)["runner:key_decrypt_failed"]["blame"] == "system"


def test_degraded_severity_warning(monkeypatch):
    uid = _uid(); seed_user(uid)
    sup = _make_sup(monkeypatch)
    sup._emit_runner_notice(uid, "runner_degraded", "token refresh failed", severity="warning")
    n = _rows(uid)["runner:degraded"]
    assert n["severity"] == "warning" and n["blame"] == "system"


def test_debounce_is_per_user_and_per_error_class(monkeypatch):
    """去抖键是 (user_id, error_class) —— 不同用户/不同类各自独立不互相拦。"""
    uid1 = _uid(); uid2 = _uid()
    seed_user(uid1); seed_user(uid2)
    sup = _make_sup(monkeypatch)
    sup._emit_runner_notice(uid1, "runner_spawn_failed", "boom")
    sup._emit_runner_notice(uid2, "runner_spawn_failed", "boom")  # different user: not debounced
    sup._emit_runner_notice(uid1, "runner_key_decrypt_failed", "d")  # different class: not debounced
    assert _rows(uid1)["runner:spawn_failed"]["occurrences"] == 1
    assert _rows(uid2)["runner:spawn_failed"]["occurrences"] == 1
    assert _rows(uid1)["runner:key_decrypt_failed"]["occurrences"] == 1


def test_debounce_expires_after_min_interval(monkeypatch):
    """去抖窗口过后（RUNNER_NOTICE_MIN_INTERVAL_SEC）允许再次写入。"""
    uid = _uid(); seed_user(uid)
    monkeypatch.setenv("RUNNER_NOTICE_MIN_INTERVAL_SEC", "0")
    from agent_runtime import supervisor as sup_mod
    sup = sup_mod.Supervisor(spawn_fn=lambda *a: None, alive_fn=lambda pid: False,
                             kill_fn=lambda pid: None, owner="test-owner", lease_ttl=30.0,
                             data_root="/agent-data")
    sup._emit_runner_notice(uid, "runner_spawn_failed", "boom")
    sup._emit_runner_notice(uid, "runner_spawn_failed", "boom2")
    assert _rows(uid)["runner:spawn_failed"]["occurrences"] == 2


def test_resolve_runner_notice_clears_only_named_classes(monkeypatch):
    """_resolve_runner_notice 现在按具体 error_class 精确清（不再是宽 'runner:' 前缀清
    全部三类）——传 spawn_failed + key_decrypt_failed 时只清这两类，degraded 原样保留。"""
    uid = _uid(); seed_user(uid)
    sup = _make_sup(monkeypatch)
    sup._emit_runner_notice(uid, "runner_spawn_failed", "boom")
    sup._emit_runner_notice(uid, "runner_key_decrypt_failed", "decrypt fail")
    sup._emit_runner_notice(uid, "runner_degraded", "warn", severity="warning")
    sup._resolve_runner_notice(uid, "runner_spawn_failed", "runner_key_decrypt_failed")
    rows = _rows(uid)
    assert rows["runner:spawn_failed"]["resolved"] is True
    assert rows["runner:key_decrypt_failed"]["resolved"] is True
    assert rows["runner:degraded"]["resolved"] is False  # untouched — independent concern


def test_resolve_runner_notice_clears_debounce_state_for_that_user():
    """恢复后应允许下次故障立即再报（不再被旧的去抖时间戳挡住）。"""
    from agent_runtime import supervisor as sup_mod
    uid = _uid(); seed_user(uid)
    sup = sup_mod.Supervisor(spawn_fn=lambda *a: None, alive_fn=lambda pid: False,
                             kill_fn=lambda pid: None, owner="test-owner", lease_ttl=30.0,
                             data_root="/agent-data")
    sup._emit_runner_notice(uid, "runner_spawn_failed", "boom")
    sup._resolve_runner_notice(uid, "runner_spawn_failed")
    sup._emit_runner_notice(uid, "runner_spawn_failed", "boom-again")
    n = _rows(uid)["runner:spawn_failed"]
    # resolve 关闭了旧的一条，再次 emit 因去抖状态已清 → 新开一条（occurrences=1，未 resolved）
    assert n["occurrences"] == 1
    assert n["resolved"] is False


def test_spawn_success_does_not_self_clear_same_tick_degraded(monkeypatch):
    """锁住 Important 回归：spawn 成功分支的 _resolve_runner_notice 只清
    spawn_failed/key_decrypt_failed，绝不清 runner_degraded —— 即使 _write_token
    刚好在同一 tick 里先失败发了 degraded。之前用宽 'runner:' 前缀会把这条刚发的
    degraded 当场清空（emit 即清，一次真实的 token 降级从未被用户看到）。"""
    uid = _uid(); seed_user(uid)
    sup = _make_sup(monkeypatch)

    # Simulate: _write_token failed this tick (its except branch emits degraded).
    sup.token_writer = lambda user_id, home: (_ for _ in ()).throw(RuntimeError("token boom"))
    sup._write_token(uid, "/agent-data/users/" + uid)
    assert _rows(uid)["runner:degraded"]["resolved"] is False

    # Immediately after, spawn's success path resolves spawn_failed/key_decrypt_failed
    # (mirrors the two call sites in tick()) — degraded must survive untouched.
    sup._resolve_runner_notice(uid, "runner_spawn_failed", "runner_key_decrypt_failed")
    rows = _rows(uid)
    assert rows["runner:degraded"]["resolved"] is False, \
        "spawn success must not self-clear a same-tick runner_degraded notice"


def test_write_token_success_clears_degraded(monkeypatch):
    """token 恢复（写成功）才是清 degraded 的唯一路径。"""
    uid = _uid(); seed_user(uid)
    sup = _make_sup(monkeypatch)

    calls = {"n": 0}

    def flaky_writer(user_id, home):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("token boom")
        return None

    sup.token_writer = flaky_writer
    home = "/agent-data/users/" + uid
    sup._write_token(uid, home)  # fails -> emits degraded
    assert _rows(uid)["runner:degraded"]["resolved"] is False

    sup._write_token(uid, home)  # succeeds -> clears degraded
    assert _rows(uid)["runner:degraded"]["resolved"] is True


def test_write_token_success_with_no_pending_degraded_skips_resolve_db_call(monkeypatch):
    """守卫：健康用户（无 pending degraded 记录）每 tick 走 _write_token 成功路径
    不应该触发一次 resolve() DB 往返——用 mock 断言 notices.resolve 未被调用。"""
    uid = _uid(); seed_user(uid)
    sup = _make_sup(monkeypatch)
    sup.token_writer = lambda user_id, home: None  # always succeeds, no prior degraded

    from notices import core as notices_core
    calls = []
    monkeypatch.setattr(notices_core, "resolve", lambda store, prefix: calls.append(prefix))
    sup._write_token(uid, "/agent-data/users/" + uid)
    assert calls == []


def test_tick_spawn_failure_does_not_take_down_other_users(monkeypatch):
    """per-user try/except 是地基：一个用户 spawn 失败绝不能连坐同批其他用户。"""
    from agent_runtime import leases
    from agent_runtime import supervisor as sup_mod

    uid_bad = _uid(); uid_good = _uid()
    seed_user(uid_bad); seed_user(uid_good)
    with db.get_pool().connection() as conn:
        conn.execute("TRUNCATE agent_runtime_instances")

    spawned = []

    def spawn_fn(entry, user_id, home):
        if user_id == uid_bad:
            raise RuntimeError("boom")
        spawned.append(user_id)
        return 4242

    sup = sup_mod.Supervisor(spawn_fn=spawn_fn, alive_fn=lambda pid: True,
                             kill_fn=lambda pid: None, owner="test-owner",
                             lease_ttl=300.0, data_root="/agent-data")
    roster = [{"user_id": uid_bad, "api_key": "k1"}, {"user_id": uid_good, "api_key": "k2"}]
    sup.tick(roster)

    # good user must still be spawned + leased despite bad user's spawn_fn raising
    assert spawned == [uid_good]
    assert uid_good in sup.children
    assert uid_bad not in sup.children
    assert leases.get(uid_good)["status"] == "running"
    lease_bad = leases.get(uid_bad)
    assert lease_bad is not None and lease_bad["status"] == "error"
    assert "boom" in (lease_bad.get("error") or "")
    n = _rows(uid_bad)["runner:spawn_failed"]
    assert n["error_class"] == "runner_spawn_failed" and n["blame"] == "system"
