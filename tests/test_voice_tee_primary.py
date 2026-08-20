"""Real PostgreSQL regression for voice lifecycle after TEE promotion."""

from __future__ import annotations

import os
import uuid

from psycopg.types.json import Jsonb

import db


def test_voice_lifecycle_uses_the_tee_primary_database(monkeypatch):
    """The current primary must serialize cancel/finalize without an RDS sidecar."""
    original_url = os.environ["DATABASE_URL"]
    tee_url = os.environ["TEE_DATABASE_URL"]
    uid = f"usr_voice_tee_primary_{uuid.uuid4().hex[:12]}"

    db.close_pool()
    monkeypatch.setenv("DATABASE_URL", tee_url)
    try:
        with db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id,created_at,doc) VALUES (%s,'',%s)",
                (uid, Jsonb({"user_id": uid})),
            )

        db.voice_call_create_active(uid, "cancel-wins")
        assert db.voice_call_cancel(uid, "cancel-wins", "connect_failed") == {
            "status": "cancelled",
            "replayed": False,
        }
        assert db.voice_call_begin_finalize(uid, "cancel-wins") == {
            "status": "cancelled",
            "replayed": True,
        }
        assert db.voice_call_status(uid, "cancel-wins") == "cancelled"

        db.voice_call_create_active(uid, "finalize-wins")
        assert db.voice_call_begin_finalize(uid, "finalize-wins") == {
            "status": "finalizing",
            "replayed": False,
        }
        assert db.voice_call_mark_finalized(uid, "finalize-wins") == {
            "status": "finalized",
            "replayed": False,
        }
        assert db.voice_call_cancel(uid, "finalize-wins", "user_hangup") == {
            "status": "finalized",
            "replayed": True,
        }
        assert db.voice_call_status(uid, "finalize-wins") == "finalized"
    finally:
        try:
            with db.get_pool().connection() as conn:
                conn.execute("DELETE FROM users WHERE user_id=%s", (uid,))
        finally:
            db.close_pool()
            monkeypatch.setenv("DATABASE_URL", original_url)
