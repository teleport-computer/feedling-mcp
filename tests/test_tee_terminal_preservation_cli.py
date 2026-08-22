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


def test_audit_preserved_groups_contract_reads_by_table_for_gateway():
    """Large frames use one compact checksum query per database."""
    rows = {
        ("usr_gateway", f"frame-{index:03d}"): (
            "usr_gateway",
            f"frame-{index:03d}",
            float(index),
            {"body_ct": "cipher" * 1000, "index": index},
            {"nonce": f"nonce-{index}"},
            None,
        )
        for index in range(120)
    }
    audit_rows = {
        key: (row[0], row[1], row[2], f"doc-{index}", len(str(row[3])),
              f"meta-{index}", len(str(row[4])), row[5])
        for index, (key, row) in enumerate(rows.items())
    }
    markers = [
        preservation.PreservedPending(
            user_id=user_id,
            table="frame_envelopes",
            item_id=item_id,
            reason=preservation.encode_preserved_reason(
                preservation.canonical_row_sha256("frame_envelopes", row),
                "decrypt_failed:historical",
            ),
        )
        for (user_id, item_id), row in rows.items()
    ]

    class Cursor:
        def __init__(self, fetched):
            self._fetched = fetched

        def fetchall(self):
            return self._fetched

    class BatchOnlyConnection:
        def __init__(self):
            self.batch_sizes: list[int] = []

        def execute(self, query, args):
            if "FROM frames " in query:
                return Cursor([])
            assert "unnest" in query, "audit issued a point read"
            assert "md5(doc::text)" in query
            assert "octet_length(doc::text)" in query
            user_ids, item_ids = args
            assert len(user_ids) == len(item_ids)
            self.batch_sizes.append(len(user_ids))
            return Cursor(
                [audit_rows[key] for key in zip(user_ids, item_ids) if key in rows]
            )

    source = BatchOnlyConnection()
    destination = BatchOnlyConnection()

    audit = preservation.audit_preserved(source, destination, markers)

    assert audit.preserved == 120
    assert audit.counts == {"frame_envelopes": 120}
    assert audit.mismatches == ()
    assert source.batch_sizes == [120]
    assert destination.batch_sizes == [120]


def test_identity_batch_lookup_preserves_historical_marker_item_ids():
    """Identity markers are compatible with both item-id conventions."""

    class Cursor:
        def fetchall(self):
            return [("usr_identity", "identity", {"body_ct": "cipher"})]

    class Connection:
        def execute(self, query, args):
            assert args == (["usr_identity", "usr_identity"],)
            return Cursor()

    row = ("usr_identity", "identity", {"body_ct": "cipher"})
    fetched = preservation._fetch_keyed_rows(
        Connection(),
        preservation.CONTRACTS["identity"].audit_fetch_sql,
        [("usr_identity", "usr_identity"), ("usr_identity", "identity")],
        by_user_only=True,
    )

    assert fetched == {
        ("usr_identity", "usr_identity"): row,
        ("usr_identity", "identity"): row,
    }


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
