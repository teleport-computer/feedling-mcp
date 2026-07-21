"""The one-time backfill: existing V2 users keep web, everyone else does not.

DB-backed (it exercises the real migration SQL against real rows), so this file
is NOT in conftest's ``_PURE_UNIT``.

The rule being pinned: "preserve what each account had", not "turn it on for
everybody". A `resident_cli` user never had these tools, so writing `true` for
them would not preserve their status quo — it would hand them a new capability
the day they move to V2.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import conftest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from core.store import WEB_SETTINGS_BLOB  # noqa: E402

_BACKFILL_SQL = (
    Path(__file__).parent.parent
    / "backend/alembic/versions/0051_web_settings_backfill.py"
)


def _upgrade_sql() -> str:
    """Run the migration's own statement, so this test cannot drift from it."""
    source = _BACKFILL_SQL.read_text(encoding="utf-8")
    body = source.split("def upgrade():", 1)[1].split("def downgrade():", 1)[0]
    return body.split('"""', 1)[1].rsplit('"""', 1)[0]


def _seed_user(conn, user_id: str, *, mode: str | None, state: str | None) -> None:
    # conftest.seed_user handles the users row + the in-memory registry mirror;
    # hand-rolling the INSERT misses NOT NULL columns and the registry half.
    conftest.seed_user(user_id)
    if mode is not None:
        conn.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s::jsonb) "
            "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc",
            (user_id, "model_api_runtime", json.dumps({"hosted_runtime_mode": mode})),
        )
    if state is not None:
        conn.execute(
            "INSERT INTO v2_runtime_state (user_id, hosted_runtime_state) "
            "VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE "
            "SET hosted_runtime_state = EXCLUDED.hosted_runtime_state",
            (user_id, state),
        )


def _web_setting(conn, user_id: str):
    row = conn.execute(
        "SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s",
        (user_id, WEB_SETTINGS_BLOB),
    ).fetchone()
    return row[0] if row else None


@pytest.fixture()
def seeded():
    users = {
        "u_bf_v2": ("db_action_v2", "v2"),          # on V2 today -> keep web
        "u_bf_resident": ("resident_cli", "resident"),  # never had it
        "u_bf_unset": (None, None),                  # defaults to resident
        # persisted target is V2; the transient state is mid-handover. These
        # must still be backfilled — otherwise a user who happened to be
        # draining at migration time silently loses the capability.
        "u_bf_draining": ("db_action_v2", "draining"),
        "u_bf_mode_only": ("db_action_v2", "resident"),
    }
    with db.get_pool().connection() as conn:
        for uid, (mode, state) in users.items():
            conn.execute(
                "DELETE FROM user_blobs WHERE user_id=%s AND kind=%s",
                (uid, WEB_SETTINGS_BLOB),
            )
            _seed_user(conn, uid, mode=mode, state=state)
    yield users
    with db.get_pool().connection() as conn:
        for uid in users:
            conn.execute("DELETE FROM user_blobs WHERE user_id=%s", (uid,))
            conn.execute("DELETE FROM v2_runtime_state WHERE user_id=%s", (uid,))
            conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))


def test_persisted_v2_target_is_backfilled_regardless_of_transient_state(seeded):
    with db.get_pool().connection() as conn:
        conn.execute(_upgrade_sql())
        for uid in ("u_bf_v2", "u_bf_draining", "u_bf_mode_only"):
            assert _web_setting(conn, uid) == {"version": 1, "enabled": True}, uid
        # never had the tools -> not "preserving" anything by switching them on
        for uid in ("u_bf_resident", "u_bf_unset"):
            assert _web_setting(conn, uid) is None, uid


def test_backfill_is_idempotent(seeded):
    with db.get_pool().connection() as conn:
        conn.execute(_upgrade_sql())
        conn.execute(_upgrade_sql())
        assert _web_setting(conn, "u_bf_v2") == {"version": 1, "enabled": True}


def test_an_existing_preference_is_never_overwritten(seeded):
    """A user who already said "off" must not be flipped back on."""
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s::jsonb)",
            ("u_bf_v2", WEB_SETTINGS_BLOB, json.dumps({"version": 1, "enabled": False})),
        )
        conn.execute(_upgrade_sql())
        assert _web_setting(conn, "u_bf_v2") == {"version": 1, "enabled": False}


def test_a_user_created_after_the_backfill_defaults_to_off(seeded):
    """The code default stays false — the backfill is a one-time snapshot of who
    existed, not a standing "absence means enabled" rule."""
    with db.get_pool().connection() as conn:
        conn.execute(_upgrade_sql())
        _seed_user(conn, "u_bf_newcomer", mode="db_action_v2", state="v2")
        try:
            assert _web_setting(conn, "u_bf_newcomer") is None
        finally:
            conn.execute("DELETE FROM v2_runtime_state WHERE user_id=%s", ("u_bf_newcomer",))
            conn.execute("DELETE FROM users WHERE user_id=%s", ("u_bf_newcomer",))
