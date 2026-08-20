from __future__ import annotations

import base64
import json
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import psycopg
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg.types.json import Jsonb

import db
from admin import plaintext_shadow
from plaintext_shadow.config import TargetPolicy
from conftest import seed_user


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
                "0029_plaintext_shadow_merge",
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


def test_infrastructure_evidence_requires_valid_ed25519_signature(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv(
        "FEEDLING_PLAINTEXT_SHADOW_INFRA_EVIDENCE_PUBLIC_KEY",
        base64.b64encode(public_key).decode(),
    )
    payload_bytes = b'{"proof":"external"}'
    payload = base64.b64encode(payload_bytes).decode()

    with pytest.raises(RuntimeError, match="signature is invalid"):
        plaintext_shadow._verified_infra_attestation(
            payload, base64.b64encode(b"not-a-signature").decode()
        )

    signature = private_key.sign(base64.b64decode(payload))
    decoded, key_fingerprint, signature_digest = (
        plaintext_shadow._verified_infra_attestation(
            payload, base64.b64encode(signature).decode()
        )
    )
    assert decoded == {"proof": "external"}
    assert len(key_fingerprint) == 16
    assert signature_digest.startswith("sha256:")

    noncanonical = b'{"proof": "external"}'
    with pytest.raises(RuntimeError, match="not canonical JSON"):
        plaintext_shadow._verified_infra_attestation(
            base64.b64encode(noncanonical).decode(),
            base64.b64encode(private_key.sign(noncanonical)).decode(),
        )


@pytest.mark.parametrize("value", [True, "100", 0, -1])
def test_infrastructure_evidence_rejects_non_positive_or_coerced_scalars(value):
    with pytest.raises(RuntimeError, match="target_capacity_bytes"):
        plaintext_shadow._positive_int_claim(
            {"target_capacity_bytes": value}, "target_capacity_bytes"
        )


@pytest.mark.parametrize("bad_time", ["2026-08-20T00:00:00", 123])
def test_restore_claims_normalize_malformed_timestamps_to_runtime_error(bad_time):
    payload = {
        "backup_artifact_digest": "sha256:" + "a" * 64,
        "expires_at": "2026-08-21T00:00:00Z",
        "ha_verified": True,
        "restored_at": bad_time,
        "schema_head": plaintext_shadow._SCHEMA_HEAD,
        "source_backup_at": "2026-08-19T00:00:00Z",
        "target_capacity_bytes": 1,
        "target_connection_limit": 1,
        "target_fingerprint": "target",
        "verifier_digest": "sha256:" + "b" * 64,
    }
    with pytest.raises(RuntimeError, match="timestamp"):
        plaintext_shadow._validated_restore_claims(
            payload, target_fingerprint="target", target_connection_limit=1
        )


def _signed_restore_claims(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    monkeypatch.setenv(
        "FEEDLING_PLAINTEXT_SHADOW_INFRA_EVIDENCE_PUBLIC_KEY",
        base64.b64encode(public_key).decode(),
    )
    primary_dsn = os.environ["TEE_DATABASE_URL"]
    policy = TargetPolicy(dsn=os.environ["DATABASE_URL"])
    monkeypatch.setenv("DATABASE_URL", primary_dsn)
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.delenv("TEE_DATABASE_URL")
    monkeypatch.delenv("FEEDLING_TEE_DUAL_WRITE", raising=False)
    monkeypatch.setenv("PLAINTEXT_SHADOW_DATABASE_URL", policy.dsn)
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")
    monkeypatch.setattr(plaintext_shadow.db, "get_pool", lambda: _Pool(primary_dsn))
    with psycopg.connect(policy.dsn) as target:
        target_fingerprint = plaintext_shadow._live_database_fingerprint(target)
        connection_limit = int(target.execute("SHOW max_connections").fetchone()[0])
    now = datetime.now(timezone.utc)
    claims = {
        "backup_artifact_digest": "sha256:" + "a" * 64,
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "ha_verified": True,
        "restored_at": (now - timedelta(minutes=1)).isoformat(),
        "schema_head": plaintext_shadow._SCHEMA_HEAD,
        "source_backup_at": (now - timedelta(minutes=2)).isoformat(),
        "target_capacity_bytes": 1_000_000_000,
        "target_connection_limit": connection_limit,
        "target_fingerprint": target_fingerprint,
        "verifier_digest": "sha256:" + "b" * 64,
    }
    payload = json.dumps(
        claims, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    signature = private_key.sign(payload)
    return policy, claims, payload, signature


def test_restore_evidence_rejects_direct_unsigned_database_row(monkeypatch):
    policy, claims, _payload, _signature = _signed_restore_claims(monkeypatch)
    _key, key_fingerprint = plaintext_shadow._infra_public_key()
    with db.get_pool().connection() as primary:
        row = primary.execute(
            "INSERT INTO plaintext_shadow_restore_evidence "
            "(restored_at, source_backup_at, schema_head, verifier_digest, "
            "backup_artifact_digest, target_fingerprint, target_capacity_bytes, "
            "target_connection_limit, ha_verified, attestation_key_fingerprint, "
            "attestation_signature_digest, operator_id, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,true,%s,%s,%s,%s) RETURNING id",
            (
                claims["restored_at"], claims["source_backup_at"],
                claims["schema_head"], claims["verifier_digest"],
                claims["backup_artifact_digest"], claims["target_fingerprint"],
                claims["target_capacity_bytes"], claims["target_connection_limit"],
                key_fingerprint, "sha256:" + "c" * 64, "direct-sql",
                claims["expires_at"],
            ),
        ).fetchone()
        try:
            assert not plaintext_shadow._restore_evidence(
                primary,
                target_fingerprint=claims["target_fingerprint"],
                minimum_capacity_bytes=claims["target_capacity_bytes"],
                target_connection_limit=claims["target_connection_limit"],
                attestation_key_fingerprint=key_fingerprint,
            )
        finally:
            primary.execute(
                "DELETE FROM plaintext_shadow_restore_evidence WHERE id=%s", (row[0],)
            )


def test_restore_evidence_rejects_claim_tampered_after_signature(monkeypatch):
    policy, claims, payload, signature = _signed_restore_claims(monkeypatch)
    args = SimpleNamespace(
        attestation_payload_b64=base64.b64encode(payload).decode(),
        attestation_signature_b64=base64.b64encode(signature).decode(),
        operator_id="signed-test",
    )
    result = plaintext_shadow.record_restore_evidence(args)
    _key, key_fingerprint = plaintext_shadow._infra_public_key()
    with db.get_pool().connection() as primary:
        try:
            primary.execute(
                "UPDATE plaintext_shadow_restore_evidence "
                "SET target_capacity_bytes=target_capacity_bytes+1 WHERE id=%s",
                (result["evidence_id"],),
            )
            assert not plaintext_shadow._restore_evidence(
                primary,
                target_fingerprint=claims["target_fingerprint"],
                minimum_capacity_bytes=claims["target_capacity_bytes"],
                target_connection_limit=claims["target_connection_limit"],
                attestation_key_fingerprint=key_fingerprint,
            )
        finally:
            primary.execute(
                "DELETE FROM plaintext_shadow_restore_evidence WHERE id=%s",
                (result["evidence_id"],),
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


def test_strict_identity_comparison_accounts_for_literal_kind(backend_env):
    uid = f"strict-identity-{uuid.uuid4().hex}"
    doc = {"id": "identity", "body": "Ada", "visibility": "shared"}
    seed_user(uid, api_key_hash="strict-identity-hash", doc={})
    policy = TargetPolicy(dsn=os.environ["TEE_DATABASE_URL"])
    try:
        with db.get_pool().connection() as source:
            source.execute(
                "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s,'identity',%s)",
                (uid, Jsonb(doc)),
            )
        with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as target:
            target.execute(
                "INSERT INTO users (user_id, doc) VALUES (%s, '{}'::jsonb) "
                "ON CONFLICT (user_id) DO NOTHING",
                (uid,),
            )
            target.execute(
                "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s,'identity',%s)",
                (uid, Jsonb(doc)),
            )

        assert plaintext_shadow._strict_extended_content(policy) == (0, 0)
    finally:
        with db.get_pool().connection() as source:
            source.execute("DELETE FROM users WHERE user_id=%s", (uid,))
        with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as target:
            target.execute("DELETE FROM users WHERE user_id=%s", (uid,))
