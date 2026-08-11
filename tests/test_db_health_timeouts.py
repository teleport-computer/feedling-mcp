from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *_args):
        return False


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows=()):
        self.rows = rows
        self.calls = []
        self.transactions = 0

    def transaction(self):
        self.transactions += 1
        return _Context(self)

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        return _Rows(self.rows)


class _Pool:
    def __init__(self, conn):
        self.conn = conn
        self.connection_calls = []

    def connection(self, **kwargs):
        self.connection_calls.append(kwargs)
        return _Context(self.conn)


def test_health_probe_bounds_acquire_and_statement_timeout(monkeypatch):
    conn = _Connection()
    pool = _Pool(conn)
    monkeypatch.setattr(db, "get_pool", lambda: pool)

    result = db.health_probe(timeout=1.0, statement_timeout_ms=1000)

    assert result["ok"] is True
    assert pool.connection_calls == [{"timeout": 1.0}]
    assert conn.transactions == 1
    assert conn.calls[0] == (
        "SELECT set_config('statement_timeout', %s, true)",
        ("1000ms",),
    )
    assert conn.calls[1][0] == "SELECT 1"


def test_runner_heartbeat_health_path_uses_same_bounds(monkeypatch):
    row = ("runner-a", "host", 0, 1, 0, 0, True, False, "v", 995.0, {})
    conn = _Connection([row])
    pool = _Pool(conn)
    monkeypatch.setattr(db, "get_pool", lambda: pool)

    rows = db.list_supervisor_instance_heartbeats(
        timeout=1.0,
        statement_timeout_ms=1000,
    )

    assert rows[0]["owner"] == "runner-a"
    assert pool.connection_calls == [{"timeout": 1.0}]
    assert conn.transactions == 1
    assert conn.calls[0][1] == ("1000ms",)


def test_runner_heartbeat_default_path_preserves_pool_defaults(monkeypatch):
    conn = _Connection([])
    pool = _Pool(conn)
    monkeypatch.setattr(db, "get_pool", lambda: pool)

    assert db.list_supervisor_instance_heartbeats() == []

    assert pool.connection_calls == [{}]
    assert conn.transactions == 0
    assert all("set_config" not in sql for sql, _params in conn.calls)
