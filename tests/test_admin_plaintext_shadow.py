from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager

import psycopg
import pytest

import db
from admin import plaintext_shadow
from plaintext_shadow.config import TargetPolicy


class _Pool:
    def __init__(self, dsn):
        self.dsn = dsn

    @contextmanager
    def connection(self):
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            yield conn


def test_preflight_redacts_dsn_on_configuration_failure(monkeypatch, capsys):
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "rds")
    monkeypatch.setenv(
        "PLAINTEXT_SHADOW_DATABASE_URL",
        "postgresql://dsn-user-must-not-leak:super-secret-password@shadow.invalid/plaintext",
    )

    rc = plaintext_shadow.main(["preflight"])
    output = capsys.readouterr().out

    assert rc == 2
    assert "super-secret-password" not in output
    assert "dsn-user-must-not-leak" not in output


def test_verify_require_green_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        plaintext_shadow,
        "strict_report",
        lambda: {"ok": False, "failure_slugs": ["pending_keys"]},
    )

    rc = plaintext_shadow.main(["verify", "--require-green"])

    assert rc == 2
    assert json.loads(capsys.readouterr().out)["failure_slugs"] == ["pending_keys"]


def test_verify_without_gate_reports_red_but_returns_success(monkeypatch):
    monkeypatch.setattr(
        plaintext_shadow,
        "strict_report",
        lambda: {"ok": False, "failure_slugs": ["trigger_drift"]},
    )
    assert plaintext_shadow.main(["verify"]) == 0


def test_preflight_red_report_returns_nonzero(monkeypatch):
    monkeypatch.setattr(
        plaintext_shadow,
        "preflight",
        lambda: {"ok": False, "failure_slugs": ["target_tls_required"]},
    )
    assert plaintext_shadow.main(["preflight"]) == 2


def test_install_and_remove_trigger_commands_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(
        plaintext_shadow,
        "install_triggers",
        lambda: calls.append("install") or {"ok": True},
    )
    monkeypatch.setattr(
        plaintext_shadow,
        "remove_triggers",
        lambda: calls.append("remove") or {"ok": True},
    )

    assert plaintext_shadow.main(["install-triggers"]) == 0
    assert plaintext_shadow.main(["remove-triggers"]) == 0
    assert calls == ["install", "remove"]


def test_backfill_uses_explicit_plaintext_all_target(monkeypatch):
    calls = []
    policy = object()
    monkeypatch.setattr(plaintext_shadow.config, "require_target", lambda: policy)
    monkeypatch.setattr(plaintext_shadow, "_require_live_topology", lambda _policy: None)
    monkeypatch.setattr(plaintext_shadow, "_require_trigger_audit", lambda: None)
    monkeypatch.setattr(plaintext_shadow, "_capture_high_water", lambda: 41)
    monkeypatch.setattr(
        plaintext_shadow,
        "_backfill_mirror",
        lambda target: calls.append(("mirror", target)) or [],
    )
    monkeypatch.setattr(
        plaintext_shadow,
        "_backfill_ciphertext",
        lambda target: calls.append(("ciphertext", target)) or [],
    )
    monkeypatch.setattr(
        plaintext_shadow,
        "_backfill_snapshot",
        lambda target: calls.append(("snapshot", target)) or [],
    )
    monkeypatch.setattr(
        plaintext_shadow,
        "_drain_to_high_water",
        lambda high_water: calls.append(("drain", high_water))
        or {"pending_through_high_water": 0},
    )

    report = plaintext_shadow.backfill()

    assert report["high_water_generation"] == 41
    assert calls == [
        ("mirror", policy),
        ("ciphertext", policy),
        ("snapshot", policy),
        ("drain", 41),
    ]


@pytest.mark.parametrize("field", ["pending", "errors", "skipped", "quarantined"])
def test_backfill_fails_closed_on_incomplete_ciphertext_report(monkeypatch, field):
    policy = object()
    monkeypatch.setattr(plaintext_shadow.config, "require_target", lambda: policy)
    monkeypatch.setattr(plaintext_shadow, "_require_live_topology", lambda _policy: None)
    monkeypatch.setattr(plaintext_shadow, "_require_trigger_audit", lambda: None)
    monkeypatch.setattr(plaintext_shadow, "_capture_high_water", lambda: 9)
    monkeypatch.setattr(plaintext_shadow, "_backfill_mirror", lambda _policy: [])
    monkeypatch.setattr(
        plaintext_shadow,
        "_backfill_ciphertext",
        lambda _policy: [{"table": "chat_messages", field: 1}],
    )
    monkeypatch.setattr(plaintext_shadow, "_backfill_snapshot", lambda _policy: [])
    monkeypatch.setattr(
        plaintext_shadow,
        "_drain_to_high_water",
        lambda _high_water: {"pending_through_high_water": 0},
    )

    report = plaintext_shadow.backfill()

    assert report["ok"] is False
    assert report["ciphertext_failures"] == 1


def test_strict_report_rejects_legacy_target_pending(backend_env, monkeypatch):
    policy = TargetPolicy(dsn=os.environ["TEE_DATABASE_URL"])
    monkeypatch.setattr(
        plaintext_shadow.db,
        "get_pool",
        lambda: _Pool(os.environ["TEE_DATABASE_URL"]),
    )
    uid = f"strict-pending-{uuid.uuid4().hex}"
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as target:
        target.execute(
            "INSERT INTO tee_pending_device_migration "
            "(user_id, table_name, item_id, reason) VALUES (%s,%s,%s,%s)",
            (uid, "chat_messages", "msg", "decrypt_failed:test"),
        )
    try:
        monkeypatch.setattr(plaintext_shadow.config, "require_target", lambda: policy)
        monkeypatch.setattr(
            plaintext_shadow,
            "preflight",
            lambda: {"ok": True, "failure_slugs": []},
        )
        monkeypatch.setattr(
            plaintext_shadow.verify,
            "run",
            lambda **_kwargs: {"ok": True, "strict_ok": True, "mismatches": []},
        )
        monkeypatch.setattr(plaintext_shadow, "_unexpected_ciphertext_count", lambda _p: 0)
        monkeypatch.setattr(plaintext_shadow, "_strict_extended_content", lambda _p: (0, 0))
        monkeypatch.setattr(plaintext_shadow, "_strict_snapshot_mismatch_count", lambda _p: 0)

        report = plaintext_shadow.strict_report()

        assert report["ok"] is False
        assert report["target_pending_rows"] >= 1
        assert "target_pending_rows" in report["failure_slugs"]
    finally:
        with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as target:
            target.execute(
                "DELETE FROM tee_pending_device_migration WHERE user_id=%s", (uid,)
            )


def test_record_restore_evidence_has_no_free_form_note_argument():
    with pytest.raises(SystemExit):
        plaintext_shadow.main(
            [
                "record-restore-evidence",
                "--restored-at",
                "2026-08-20T00:00:00Z",
                "--source-backup-at",
                "2026-08-20T00:00:00Z",
                "--schema-head",
                "0026_plaintext_shadow_control",
                "--verifier-digest",
                "sha256:abc",
                "--operator-id",
                "operator-1",
                "--expires-at",
                "2026-08-21T00:00:00Z",
                "--note",
                "do not persist this",
            ]
        )


def test_every_strict_ciphertext_table_has_inspectable_upsert_shape():
    strict_tables = set(plaintext_shadow.verify._CIPHERTEXT_TABLES)
    assert strict_tables
    assert {"chat_messages", "memory_moments", "identity"} <= strict_tables
    for table in strict_tables:
        target, columns = plaintext_shadow._upsert_shape(
            plaintext_shadow.worker._TABLES[table]
        )
        assert target
        assert columns


def test_snapshot_strict_digest_compares_common_columns_only():
    worker_id = f"strict-shadow-{uuid.uuid4().hex}"
    policy = TargetPolicy(dsn=os.environ["TEE_DATABASE_URL"])
    try:
        with db.get_pool().connection() as source:
            source.execute(
                "INSERT INTO v2_worker_heartbeats (worker_id, beat_at) "
                "VALUES (%s, '2026-08-20T00:00:00Z')",
                (worker_id,),
            )
        with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as target:
            target.execute(
                "INSERT INTO v2_worker_heartbeats (worker_id, beat_at) "
                "VALUES (%s, '2026-08-20T00:00:00Z')",
                (worker_id,),
            )

        assert plaintext_shadow._strict_snapshot_mismatch_count(
            policy, tables=("v2_worker_heartbeats",)
        ) == 0

        with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as target:
            target.execute(
                "UPDATE v2_worker_heartbeats SET beat_at='2026-08-20T00:00:01Z' "
                "WHERE worker_id=%s",
                (worker_id,),
            )
        assert plaintext_shadow._strict_snapshot_mismatch_count(
            policy, tables=("v2_worker_heartbeats",)
        ) == 1
    finally:
        with db.get_pool().connection() as source:
            source.execute(
                "DELETE FROM v2_worker_heartbeats WHERE worker_id=%s", (worker_id,)
            )
        with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as target:
            target.execute(
                "DELETE FROM v2_worker_heartbeats WHERE worker_id=%s", (worker_id,)
            )
