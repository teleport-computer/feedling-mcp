from __future__ import annotations

import json
import os

import psycopg
import pytest
from psycopg.types.json import Jsonb

from plaintext_shadow import change_capture
from tee_shadow import table_registry


@pytest.fixture
def capture_conn():
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        change_capture.remove(conn)
        conn.execute("TRUNCATE plaintext_shadow_dirty_keys")
        yield conn
        change_capture.remove(conn)
        conn.execute("TRUNCATE plaintext_shadow_dirty_keys")


def _dirty(conn, table: str) -> tuple:
    return conn.execute(
        "SELECT key_json, operation, generation, attempts, last_error_slug "
        "FROM plaintext_shadow_dirty_keys WHERE table_name=%s",
        (table,),
    ).fetchone()


def test_install_is_idempotent_and_covers_every_synced_table(capture_conn) -> None:
    first = change_capture.install(capture_conn)
    second = change_capture.install(capture_conn)
    expected = {
        name
        for name, entry in table_registry.REGISTRY.items()
        if entry.lane not in (table_registry.SKIP, table_registry.LOGICAL)
    }

    assert set(first.installed) == expected
    assert first.missing_tables == ()
    assert set(second.installed) == expected
    assert change_capture.audit(capture_conn).ok is True


def test_committed_write_coalesces_to_latest_generation(capture_conn) -> None:
    change_capture.install(capture_conn)
    capture_conn.execute(
        "INSERT INTO server_config (key, value) VALUES (%s, %s)",
        ("plaintext-shadow-capture", b"first secret body"),
    )
    first = _dirty(capture_conn, "server_config")
    capture_conn.execute(
        "UPDATE server_config SET value=%s WHERE key=%s",
        (b"second secret body", "plaintext-shadow-capture"),
    )
    second = _dirty(capture_conn, "server_config")

    assert first[0] == {"key": "plaintext-shadow-capture"}
    assert first[1] == "INSERT"
    assert second[0] == {"key": "plaintext-shadow-capture"}
    assert second[1] == "UPDATE"
    assert second[2] > first[2]
    assert second[3:] == (0, None)
    assert "secret body" not in json.dumps(second)

    capture_conn.execute(
        "DELETE FROM server_config WHERE key=%s", ("plaintext-shadow-capture",)
    )


def test_rolled_back_write_creates_no_dirty_key(capture_conn) -> None:
    change_capture.install(capture_conn)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as tx_conn:
        with pytest.raises(RuntimeError, match="force rollback"):
            with tx_conn.transaction():
                tx_conn.execute(
                    "INSERT INTO server_config (key, value) VALUES (%s, %s)",
                    ("plaintext-shadow-rollback", b"never committed"),
                )
                raise RuntimeError("force rollback")

    assert capture_conn.execute(
        "SELECT count(*) FROM plaintext_shadow_dirty_keys "
        "WHERE table_name='server_config' AND key_json->>'key'=%s",
        ("plaintext-shadow-rollback",),
    ).fetchone() == (0,)


def test_sensitive_primary_key_uses_table_marker(capture_conn) -> None:
    change_capture.install(capture_conn)
    auth_token = "auth-token-must-not-enter-control-plane"
    device_token = "device-token-must-not-enter-control-plane"
    capture_conn.execute(
        "INSERT INTO notify_relay_configs (auth_token, device_token) VALUES (%s, %s)",
        (auth_token, device_token),
    )

    dirty = _dirty(capture_conn, "notify_relay_configs")
    rendered = json.dumps(dirty)
    assert dirty[0] == {}
    assert auth_token not in rendered
    assert device_token not in rendered

    capture_conn.execute(
        "DELETE FROM notify_relay_configs WHERE auth_token=%s", (auth_token,)
    )


def test_json_content_never_enters_dirty_key(capture_conn) -> None:
    change_capture.install(capture_conn)
    capture_conn.execute(
        "INSERT INTO global_blobs (key, doc) VALUES (%s, %s)",
        ("capture-json", Jsonb({"body": "plaintext must stay out"})),
    )

    dirty = _dirty(capture_conn, "global_blobs")
    assert dirty[0] == {"key": "capture-json"}
    assert "plaintext must stay out" not in json.dumps(dirty)

    capture_conn.execute("DELETE FROM global_blobs WHERE key=%s", ("capture-json",))


def test_audit_reports_disabled_and_remove_clears_triggers(capture_conn) -> None:
    change_capture.install(capture_conn)
    capture_conn.execute(
        "ALTER TABLE server_config DISABLE TRIGGER "
        "plaintext_shadow_capture_server_config"
    )

    report = change_capture.audit(capture_conn)
    assert report.ok is False
    assert report.disabled == ("server_config",)

    change_capture.remove(capture_conn)
    remaining = capture_conn.execute(
        "SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal "
        "AND tgname LIKE 'plaintext_shadow_capture_%%'"
    ).fetchone()
    assert remaining == (0,)

