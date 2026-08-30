from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db


_BACKEND = Path(__file__).parent.parent / "backend"


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


def test_health_pool_is_lazy_bounded_and_separate(monkeypatch):
    created = []
    ordinary_pool = object()
    check_connection = db.ConnectionPool.check_connection

    class FakeConnectionPool:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            created.append(self)

    monkeypatch.setenv("DATABASE_URL", "postgresql://health-test")
    monkeypatch.setattr(db, "ConnectionPool", FakeConnectionPool)
    monkeypatch.setattr(db, "_pool", ordinary_pool)
    monkeypatch.setattr(db, "_health_pool", None)

    first = db.get_health_pool()
    second = db.get_health_pool()

    assert first is second
    assert first is not ordinary_pool
    assert created == [first]
    assert first.args == ("postgresql://health-test",)
    assert first.kwargs == {
        "min_size": 1,
        "max_size": 2,
        "timeout": 1.0,
        "max_idle": 300,
        "check": check_connection,
        "kwargs": {"autocommit": True},
        "open": True,
    }


def test_health_pool_replaces_a_connection_closed_while_idle(monkeypatch):
    dsn = os.environ.get("FEEDLING_TEST_PG")
    if not dsn:
        pytest.skip("FEEDLING_TEST_PG is required for the stale connection test")

    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setattr(db, "HEALTH_DB_POOL_MAX_SIZE", 1)
    monkeypatch.setattr(db, "_health_pool", None)
    health_pool = db.get_health_pool()
    try:
        with health_pool.connection(timeout=5.0) as conn:
            stale_backend_pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]

        with psycopg.connect(dsn, autocommit=True) as admin:
            terminated = admin.execute(
                "SELECT pg_terminate_backend(%s)", (stale_backend_pid,)
            ).fetchone()[0]
        assert terminated is True

        with health_pool.connection(timeout=5.0) as replacement:
            assert replacement.execute("SELECT 1").fetchone() == (1,)
            assert replacement.execute("SELECT pg_backend_pid()").fetchone()[0] != (
                stale_backend_pid
            )
    finally:
        health_pool.close()
        db._health_pool = None


def test_tee_primary_pool_rotates_and_keeps_direct_tls_connections_alive(monkeypatch):
    created = []

    class FakeConnectionPool:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            created.append(self)

    monkeypatch.setenv("DATABASE_URL", "postgresql://tee-primary")
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setattr(db, "ConnectionPool", FakeConnectionPool)
    monkeypatch.setattr(db, "_pool", None)

    pool = db.get_pool()

    assert created == [pool]
    assert pool.kwargs["max_lifetime"] == 180.0
    assert pool.kwargs["kwargs"] == {
        "autocommit": True,
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "tcp_user_timeout": 30000,
    }


def test_tee_listen_connection_uses_direct_tls_keepalives(monkeypatch):
    calls = []
    sentinel = object()

    def fake_connect(*args, **kwargs):
        calls.append((args, kwargs))
        return sentinel

    monkeypatch.setenv("DATABASE_URL", "postgresql://tee-primary")
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setattr(db.psycopg, "connect", fake_connect)

    assert db.listen_connection() is sentinel
    assert calls == [
        (
            ("postgresql://tee-primary",),
            {
                "autocommit": True,
                "connect_timeout": 10,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
                "tcp_user_timeout": 30000,
            },
        )
    ]


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
    monkeypatch.setattr(db, "get_health_pool", lambda: pool)

    rows = db.list_supervisor_instance_heartbeats_for_health(
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


def test_set_blob_propagates_primary_write_failure(monkeypatch):
    def fail_pool():
        raise RuntimeError("primary unavailable")

    monkeypatch.setattr(db, "get_pool", fail_pool)

    with pytest.raises(RuntimeError, match="primary unavailable"):
        db.set_blob("usr_contract", "identity", {"v": 1})


def test_set_blob_best_effort_is_explicit_and_reports_failure(monkeypatch, caplog):
    def fail_pool():
        raise RuntimeError("primary unavailable")

    monkeypatch.setattr(db, "get_pool", fail_pool)

    assert db.set_blob_best_effort("usr_contract", "push_state", {"v": 1}) is False
    assert "set_blob_best_effort(usr_contract,push_state) failed" in caplog.text


def test_memory_load_strict_propagates_while_legacy_loader_is_explicitly_fail_soft(
    monkeypatch, caplog,
):
    monkeypatch.setattr(db, "_memory_mutation_context", lambda _user_id: None)

    def fail_pool():
        raise RuntimeError("memory database unavailable")

    monkeypatch.setattr(db, "get_pool", fail_pool)

    with pytest.raises(RuntimeError, match="memory database unavailable"):
        db.memory_load_strict("usr_memory_contract")
    assert db.memory_load("usr_memory_contract") == []
    assert "memory_load(usr_memory_contract) failed" in caplog.text


def _db_blob_call_sites(method: str) -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in _BACKEND.rglob("*.py"):
        if path.name == "db.py":
            continue
        stack: list[str] = []

        class Visitor(ast.NodeVisitor):
            def visit_FunctionDef(self, node):  # noqa: N802 - ast visitor API
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):  # noqa: N802 - ast visitor API
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == method
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "db"
                ):
                    sites.add((str(path.relative_to(_BACKEND)), ".".join(stack)))
                self.generic_visit(node)

        Visitor().visit(ast.parse(path.read_text()))
    return sites


def test_blob_write_callers_keep_strict_and_best_effort_contracts_explicit():
    assert _db_blob_call_sites("set_blob_best_effort") == {
        ("core/store.py", "_persist_frames_meta"),
        ("core/store.py", "_save_tokens_best_effort"),
        ("core/store.py", "record_successful_push"),
        ("core/store.py", "_save_live_activity_state"),
        ("hosted/turn.py", "_state_pending_items"),
    }
    assert _db_blob_call_sites("set_blob") == {
        ("bootstrap/bootstrap_core.py", "bootstrap_payload"),
        ("core/store.py", "_save_tokens"),
        ("genesis/service.py", "write_genesis_checkpoint"),
        ("genesis/service.py", "write_genesis_state"),
        ("genesis/service.py", "_write_persona_artifact"),
        ("genesis/service.py", "_write_voice_artifact"),
        ("hosted/history_import.py", "_save_history_job"),
        ("hosted/mcp_core.py", "_save"),
        ("identity/service.py", "_save_identity"),
        ("memory/memory_core.py", "migration_state_post"),
        ("proactive/capture_scheduler.py", "save_capture_state"),
        ("proactive/dream_scheduler.py", "save_dream_state"),
        ("proactive/store_v2.py", "save"),
    }
