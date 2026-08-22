"""Focused startup-contract tests for a promoted TEE database."""

from __future__ import annotations

import pytest


def _configure_startup(monkeypatch, *, actual_head, expected_head, triggers):
    import db
    from alembic.script import ScriptDirectory

    class _Rows:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, query, params=None):
            if "alembic_tee_version" in query:
                return _Rows([(actual_head,)])
            if "pg_trigger" in query:
                return _Rows([(name,) for name in triggers])
            raise AssertionError(f"unexpected startup query: {query}")

    class _Scripts:
        @staticmethod
        def get_heads():
            return [expected_head]

    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused/tee")
    monkeypatch.setattr(db.psycopg, "connect", lambda *args, **kwargs: _Connection())
    monkeypatch.setattr(
        ScriptDirectory,
        "from_config",
        classmethod(lambda cls, config: _Scripts()),
    )


def test_tee_primary_allows_missing_audit_marker_when_triggers_are_ready(
    monkeypatch,
):
    import db

    _configure_startup(
        monkeypatch,
        actual_head="test_tee_head",
        expected_head="test_tee_head",
        triggers=db._TEE_PRIMARY_TRIGGERS,
    )

    db.init_schema()


def test_tee_primary_rejects_migration_head_mismatch(monkeypatch):
    import db

    _configure_startup(
        monkeypatch,
        actual_head="stale_tee_head",
        expected_head="release_tee_head",
        triggers=db._TEE_PRIMARY_TRIGGERS,
    )

    with pytest.raises(RuntimeError, match="not at the application head"):
        db.init_schema()


def test_tee_primary_rejects_missing_primary_trigger(monkeypatch):
    import db

    _configure_startup(
        monkeypatch,
        actual_head="test_tee_head",
        expected_head="test_tee_head",
        triggers=db._TEE_PRIMARY_TRIGGERS - {"chat_messages_retire_r2_body"},
    )

    with pytest.raises(RuntimeError, match="primary triggers are incomplete"):
        db.init_schema()


def test_preservation_revert_stays_blocked_without_marker_after_cutover():
    import db
    from tee_replicator import terminal_preservation as preservation

    class _Rows:
        def __init__(self, *, rows=(), row=None):
            self._rows = rows
            self._row = row

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._row

    class _Destination:
        def execute(self, query, params=None):
            if "tee_pending_device_migration" in query:
                return _Rows(rows=[])
            if "server_config" in query:
                return _Rows(row=None)
            if "pg_trigger" in query:
                return _Rows(row=(1,))
            raise AssertionError(f"unexpected revert query: {query}")

    plan = preservation.build_revert_plan(object(), _Destination())

    assert "phase4_already_prepared:all:1" in plan.blockers
