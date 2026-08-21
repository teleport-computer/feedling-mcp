"""Release-local terminal preservation CLI safety gates."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from unittest.mock import Mock

import pytest
import psycopg

from admin import tee_terminal_preservation as cli
from tee_replicator import terminal_preservation as preservation


def _empty_plan() -> preservation.PreservationPlan:
    return preservation.PreservationPlan(
        rows=(), sha256="a" * 64, counts={}, blockers=()
    )


def test_apply_ignores_source_read_only_cleanup_eof_after_destination_commit(
    monkeypatch,
):
    """A dead source socket after the TEE commit must not report false failure."""
    events: list[str] = []

    class Source:
        @contextmanager
        def transaction(self):
            yield
            events.append("source-cleanup")
            raise psycopg.OperationalError("SSL EOF")

        def execute(self, _sql):
            return None

    class Destination:
        @contextmanager
        def transaction(self):
            yield
            events.append("destination-committed")

    plan = _empty_plan()
    monkeypatch.setattr(preservation, "build_plan", lambda *_args: plan)

    report = preservation.apply_plan(
        Source(),
        Destination(),
        plan,
        expected_count=0,
        expected_plan_sha256="a" * 64,
    )

    assert report["ok"] is True
    assert events == ["destination-committed", "source-cleanup"]


def _wire_connections(
    monkeypatch,
    *,
    same_source=False,
    owner_mismatch=False,
    include_owner=True,
):
    source, app, owner = Mock(name="source"), Mock(name="app"), Mock(name="owner")
    connections = iter((source, app, owner) if include_owner else (source, app))
    monkeypatch.setenv("DATABASE_URL", "postgresql://source")
    monkeypatch.setenv("TEE_DATABASE_URL", "postgresql://tee-app")
    if include_owner:
        monkeypatch.setenv("TEE_MIGRATION_DATABASE_URL", "postgresql://tee-owner")
    else:
        monkeypatch.delenv("TEE_MIGRATION_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        cli.psycopg,
        "connect",
        lambda *_args, **_kwargs: nullcontext(next(connections)),
    )
    fingerprints = {
        source: ("source", "10.0.0.1", 5432),
        app: ("tee", "10.0.0.2", 5432),
        owner: ("other" if owner_mismatch else "tee", "10.0.0.2", 5432),
    }
    if same_source:
        fingerprints[source] = fingerprints[app]
    monkeypatch.setattr(cli, "_fingerprint", lambda conn: fingerprints[conn])
    monkeypatch.setattr(cli, "_expected_tee_heads", lambda: {"0025_head"})
    monkeypatch.setattr(cli, "_actual_tee_heads", lambda _conn: {"0025_head"})
    return source, app, owner


def test_cli_defaults_to_read_only_dry_run(monkeypatch):
    source, app, _owner = _wire_connections(monkeypatch, include_owner=False)
    build_plan = Mock(return_value=_empty_plan())
    monkeypatch.setattr(preservation, "build_plan", build_plan)

    report = cli.run(
        apply=False,
        revert=False,
        confirm=None,
        expected_count=None,
        expected_plan_sha256=None,
    )

    assert report["mode"] == "dry-run"
    assert report["ok"] is True
    assert report["plan_sha256"] == "a" * 64
    source.execute.assert_called_once_with("SET default_transaction_read_only = on")
    build_plan.assert_called_once_with(source, app)


@pytest.mark.parametrize(
    "kwargs,match",
    [
        (
            dict(
                apply=True,
                revert=True,
                confirm="PRESERVE-TERMINAL-CIPHERTEXT",
                expected_count=0,
                expected_plan_sha256="a" * 64,
            ),
            "mutually exclusive",
        ),
        (
            dict(
                apply=True,
                revert=False,
                confirm="MIGRATE",
                expected_count=4,
                expected_plan_sha256="a" * 64,
            ),
            "confirm mismatch",
        ),
        (
            dict(
                apply=False,
                revert=True,
                confirm="PRESERVE-TERMINAL-CIPHERTEXT",
                expected_count=4,
                expected_plan_sha256="a" * 64,
            ),
            "confirm mismatch",
        ),
        (
            dict(
                apply=True,
                revert=False,
                confirm="PRESERVE-TERMINAL-CIPHERTEXT",
                expected_count=None,
                expected_plan_sha256="a" * 64,
            ),
            "expected-count",
        ),
        (
            dict(
                apply=True,
                revert=False,
                confirm="PRESERVE-TERMINAL-CIPHERTEXT",
                expected_count=4,
                expected_plan_sha256="bad",
            ),
            "expected-plan-sha256",
        ),
    ],
)
def test_cli_rejects_invalid_mutation_guards_before_connect(monkeypatch, kwargs, match):
    connect = Mock()
    monkeypatch.setattr(cli.psycopg, "connect", connect)

    with pytest.raises(RuntimeError, match=match):
        cli.run(**kwargs)

    connect.assert_not_called()


@pytest.mark.parametrize(
    "same_source,owner_mismatch,match",
    [
        (True, False, "same database"),
        (False, True, "does not resolve"),
    ],
)
def test_cli_rejects_database_fingerprint_mismatch(
    monkeypatch, same_source, owner_mismatch, match
):
    _wire_connections(
        monkeypatch,
        same_source=same_source,
        owner_mismatch=owner_mismatch,
    )
    if owner_mismatch:
        # Owner discovery is intentionally delayed until the read-only plan is
        # complete so the mutation connection cannot age out behind the plan.
        monkeypatch.setattr(
            preservation, "build_plan", Mock(return_value=_empty_plan())
        )

    kwargs = dict(
        apply=owner_mismatch,
        revert=False,
        confirm="PRESERVE-TERMINAL-CIPHERTEXT" if owner_mismatch else None,
        expected_count=0 if owner_mismatch else None,
        expected_plan_sha256="a" * 64 if owner_mismatch else None,
    )
    with pytest.raises(RuntimeError, match=match):
        cli.run(**kwargs)


def test_cli_rejects_tee_schema_head_mismatch(monkeypatch):
    _wire_connections(monkeypatch, include_owner=False)
    monkeypatch.setattr(cli, "_actual_tee_heads", lambda _conn: {"old"})

    with pytest.raises(RuntimeError, match="schema is not at head"):
        cli.run(
            apply=False,
            revert=False,
            confirm=None,
            expected_count=None,
            expected_plan_sha256=None,
        )


def test_cli_dispatches_guarded_apply_to_owner_connection(monkeypatch):
    source, app, owner = _wire_connections(monkeypatch)
    plan = _empty_plan()
    monkeypatch.setattr(preservation, "build_plan", Mock(return_value=plan))
    apply_plan = Mock(return_value={"ok": True, "preserved": 0})
    monkeypatch.setattr(preservation, "apply_plan", apply_plan)

    report = cli.run(
        apply=True,
        revert=False,
        confirm="PRESERVE-TERMINAL-CIPHERTEXT",
        expected_count=0,
        expected_plan_sha256="a" * 64,
    )

    assert report["mode"] == "apply"
    preservation.build_plan.assert_called_once_with(source, app)
    apply_plan.assert_called_once_with(
        source,
        owner,
        plan,
        expected_count=0,
        expected_plan_sha256="a" * 64,
    )


def test_cli_opens_owner_only_after_read_only_plan_is_built(monkeypatch):
    source, app, owner = Mock(name="source"), Mock(name="app"), Mock(name="owner")
    connections = {
        "postgresql://source": source,
        "postgresql://tee-app": app,
        "postgresql://tee-owner": owner,
    }
    events: list[str] = []

    @contextmanager
    def connect(url, **_kwargs):
        events.append(f"connect:{url}")
        yield connections[url]

    monkeypatch.setenv("DATABASE_URL", "postgresql://source")
    monkeypatch.setenv("TEE_DATABASE_URL", "postgresql://tee-app")
    monkeypatch.setenv("TEE_MIGRATION_DATABASE_URL", "postgresql://tee-owner")
    monkeypatch.setattr(cli.psycopg, "connect", connect)
    monkeypatch.setattr(
        cli,
        "_fingerprint",
        lambda conn: ("source", "10.0.0.1", 5432)
        if conn is source
        else ("tee", "10.0.0.2", 5432),
    )
    monkeypatch.setattr(cli, "_expected_tee_heads", lambda: {"0025_head"})
    monkeypatch.setattr(cli, "_actual_tee_heads", lambda _conn: {"0025_head"})

    plan = _empty_plan()

    def build_plan(_source, _app):
        assert "connect:postgresql://tee-owner" not in events
        events.append("plan-built")
        return plan

    monkeypatch.setattr(preservation, "build_plan", build_plan)
    monkeypatch.setattr(
        preservation,
        "apply_plan",
        Mock(return_value={"ok": True, "preserved": 0}),
    )

    cli.run(
        apply=True,
        revert=False,
        confirm="PRESERVE-TERMINAL-CIPHERTEXT",
        expected_count=0,
        expected_plan_sha256="a" * 64,
    )

    assert events.index("plan-built") < events.index("connect:postgresql://tee-owner")


def test_cli_dispatches_guarded_revert_to_owner_connection(monkeypatch):
    source, app, owner = _wire_connections(monkeypatch)
    plan = _empty_plan()
    monkeypatch.setattr(preservation, "build_revert_plan", Mock(return_value=plan))
    revert_plan = Mock(return_value={"ok": True, "reverted": 0})
    monkeypatch.setattr(preservation, "revert_plan", revert_plan)

    report = cli.run(
        apply=False,
        revert=True,
        confirm="REVERT-PRESERVED-CIPHERTEXT",
        expected_count=0,
        expected_plan_sha256="a" * 64,
    )

    assert report["mode"] == "revert"
    preservation.build_revert_plan.assert_called_once_with(source, app)
    revert_plan.assert_called_once_with(
        source,
        owner,
        plan,
        expected_count=0,
        expected_plan_sha256="a" * 64,
    )
