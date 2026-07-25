"""tee_replicator._flush_batch 的连接韧性(2026-07-14 根因修复)。

direct-TLS 经 Phala 网关会掉线(``SSL error: unexpected eof`` / ``the connection
is lost``),旧代码的降级逐行路径复用同一条死连接 → 整表崩。新逻辑:连接断换新连
重试整批;活连接上的批失败(毒行)才逐行跳。用假连接确定性地验证三条路径。
"""
import types

import psycopg
import pytest

from tee_replicator import worker


class _FakeTxn:
    """psycopg conn.transaction() 的最小替身:出异常时传播(不吞)。"""
    def __enter__(self): return self
    def __exit__(self, *exc): return False


class _FakeConn:
    def __init__(self, *, op_error=False, poison=None):
        self.op_error = op_error          # True → 每次 execute 抛 OperationalError(连接断)
        self.poison = poison              # 某个 args → 抛 DataError(毒行,连接仍活)
        self.broken = False
        self.executed = []                # 记录成功执行的 (sql, params)

    def transaction(self): return _FakeTxn()

    def execute(self, sql, params=None):
        if self.op_error:
            self.broken = True
            raise psycopg.OperationalError("the connection is lost")
        if self.poison is not None and params == self.poison:
            raise psycopg.errors.DataError("unsupported Unicode escape sequence (NUL)")
        self.executed.append((sql, params))
        return self


class _FakePool:
    """按序吐出预置的假连接;记录发放了几条。"""
    def __init__(self, conns): self._conns = list(conns); self.handed = 0
    def connection(self):
        conn = self._conns[self.handed]
        self.handed += 1
        class _CM:
            def __enter__(self): return conn
            def __exit__(self, *exc): return False
        return _CM()


_CFG = types.SimpleNamespace(upsert_sql="UPSERT", requeue_delete_tee_sql=None,
                             requeue_by_user_only=False)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(worker, "_sleep", lambda *_: None)


def _patch_pool(monkeypatch, conns):
    pool = _FakePool(conns)
    monkeypatch.setattr(worker.mirror, "get_tee_pool", lambda: pool)
    return pool


def test_connection_lost_retries_on_a_fresh_connection(monkeypatch):
    """第一条连接断 → 换新连接重试整批 → 成功;写落在新连接上。"""
    dead = _FakeConn(op_error=True)
    good = _FakeConn()
    pool = _patch_pool(monkeypatch, [dead, good])

    skipped = worker._flush_batch(_CFG, "chat_messages", [("a",), ("b",)], [], 1.0, "id1")

    assert skipped == 0
    assert pool.handed == 2                       # 借了两条(第一条死、第二条好)
    assert ("UPSERT", ("a",)) in good.executed    # 整批写到了新连接
    assert ("UPSERT", ("b",)) in good.executed
    assert any(sql == worker._CURSOR_UPSERT for sql, _ in good.executed)  # 游标推进也写了


def test_poison_row_degrades_per_row_on_a_live_connection(monkeypatch):
    """活连接上批写失败(毒行)→ 逐行跳过存不了的行,不换连接、不整表崩。"""
    conn = _FakeConn(poison=("bad",))
    pool = _patch_pool(monkeypatch, [conn])

    skipped = worker._flush_batch(_CFG, "chat_messages", [("good",), ("bad",)], [], 1.0, "id1")

    assert skipped == 1                # 只有毒行被跳
    assert pool.handed == 1            # 没换连接(连接是活的)
    assert ("UPSERT", ("good",)) in conn.executed
    assert any(sql == worker._CURSOR_UPSERT for sql, _ in conn.executed)  # 其余照常推进游标


def test_connection_lost_exhausts_retries_then_raises(monkeypatch):
    """每条连接都断 → 重试次数用尽后抛(run_table 借此让整表本 tick 失败、游标不动)。"""
    monkeypatch.setenv("FEEDLING_TEE_REPLICATE_CONN_RETRIES", "3")
    conns = [_FakeConn(op_error=True) for _ in range(3)]
    pool = _patch_pool(monkeypatch, conns)

    with pytest.raises(psycopg.OperationalError):
        worker._flush_batch(_CFG, "chat_messages", [("a",)], [], 1.0, "id1")
    assert pool.handed == 3            # 恰好试满 3 条
