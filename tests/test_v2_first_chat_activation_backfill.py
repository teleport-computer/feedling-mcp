from __future__ import annotations

import sys
from pathlib import Path

import conftest
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402


def _migration():
    backend = Path(__file__).parent.parent / "backend"
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    return ScriptDirectory.from_config(config).get_revision(
        "0087_v2_first_chat_activation"
    ).module


def _seed_turn(
    uid: str,
    *,
    parent_source: str = "model_api",
    failure: bool = False,
    reply_ts: float = 1_786_320_002.0,
) -> None:
    conftest.seed_user(uid)
    parent = {
        "id": "parent",
        "role": "user",
        "source": parent_source,
    }
    reply = {
        "id": "reply",
        "role": "openclaw",
        "source": "model_api",
        "reply_to_message_id": "parent",
    }
    if failure:
        reply["turn_failure_error_class"] = "upstream_unavailable"
    db.chat_append(uid, "parent", reply_ts - 1.0, parent, 0)
    db.chat_append(uid, "reply", reply_ts, reply, 0)


@pytest.fixture()
def seeded_users():
    users = {
        "u_activation_backfill_success",
        "u_activation_backfill_failure",
        "u_activation_backfill_verify",
        "u_activation_backfill_pre_cutoff",
        "u_activation_backfill_existing",
    }
    _seed_turn("u_activation_backfill_success")
    _seed_turn("u_activation_backfill_failure", failure=True)
    _seed_turn("u_activation_backfill_verify", parent_source="verify_ping")
    _seed_turn("u_activation_backfill_pre_cutoff", reply_ts=1_784_073_599.0)
    _seed_turn("u_activation_backfill_existing")
    db.patch_proactive_settings_strict(
        "u_activation_backfill_success",
        {"enabled": True, "wake_interval_sec": 1800},
    )
    db.patch_proactive_settings_strict(
        "u_activation_backfill_existing",
        {"first_chat_ok_at": "2026-08-01T12:34:56", "enabled": False},
    )
    yield users
    with db.get_pool().connection() as connection:
        for uid in users:
            connection.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_backfill_activates_only_successful_real_v2_conversations(seeded_users):
    with db.get_pool().connection() as connection:
        connection.execute(_migration()._BACKFILL_SQL)

    success = db.get_blob_strict(
        "u_activation_backfill_success", "proactive_settings"
    )
    assert str(success.get("first_chat_ok_at") or "").endswith("Z")
    assert success["enabled"] is True
    assert success["wake_interval_sec"] == 1800
    for uid in (
        "u_activation_backfill_failure",
        "u_activation_backfill_verify",
        "u_activation_backfill_pre_cutoff",
    ):
        settings = db.get_blob_strict(uid, "proactive_settings") or {}
        assert not str(settings.get("first_chat_ok_at") or "").strip()


def test_backfill_is_idempotent_and_preserves_existing_activation(seeded_users):
    migration = _migration()
    with db.get_pool().connection() as connection:
        connection.execute(migration._BACKFILL_SQL)
        connection.execute(migration._BACKFILL_SQL)

    existing = db.get_blob_strict(
        "u_activation_backfill_existing", "proactive_settings"
    )
    assert existing["first_chat_ok_at"] == "2026-08-01T12:34:56"
    assert existing["enabled"] is False
