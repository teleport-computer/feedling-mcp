"""Full-table TEE reflow regression tests.

These cover the recovery path that ordinary cursor replication cannot provide:
rows written under an older transform policy sit behind the durable cursor and
must be replayed without manually editing replication state.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from accounts import registry  # noqa: E402
from conftest import seed_user  # noqa: E402
from tee_replicator import reflow  # noqa: E402
from tee_replicator import worker  # noqa: E402
from tee_shadow import ciphertext_prune  # noqa: E402


def _tee(sql, params=()):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


def _seed_encrypted_user(user_id: str) -> None:
    seed_user(user_id, api_key_hash="h", doc={})
    registry._set_user_content_encryption(user_id, "on")
    worker._carry_verbatim_cache.clear()
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (user_id, doc) VALUES (%s, '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )


def _chat_doc(user_id: str, msg_id: str) -> dict:
    return {
        "id": msg_id,
        "role": "user",
        "source": "chat",
        "content_type": "text",
        "ts": 10.0,
        "v": 1,
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "user-key",
        "K_enclave": "enclave-key",
        "enclave_pk_fpr": "fingerprint",
        "visibility": "shared",
        "owner_user_id": user_id,
    }


def test_reflow_replays_rows_behind_cursor_and_clears_terminal_pending(backend_env):
    """Deleting the reflow scan would leave the source row forever absent in TEE."""
    user_id = f"usr_{uuid.uuid4().hex[:8]}"
    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    _seed_encrypted_user(user_id)
    doc = _chat_doc(user_id, msg_id)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES (%s,%s,10,%s)",
            (user_id, msg_id, Jsonb(doc)),
        )
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tee_replication_cursors "
            "(table_name,watermark_ts,watermark_id) VALUES ('chat_messages',99,%s) "
            "ON CONFLICT (table_name) DO UPDATE SET watermark_ts=99,watermark_id=EXCLUDED.watermark_id",
            ("past-the-row",),
        )
        conn.execute(
            "INSERT INTO tee_pending_device_migration (user_id,table_name,item_id,reason) "
            "VALUES (%s,'chat_messages',%s,'decrypt_failed:old-policy')",
            (user_id, msg_id),
        )

    assert worker.run_table("chat_messages", qps=0)["copied"] == 0

    dry = reflow.reflow_table("chat_messages", dry_run=True, qps=0)
    assert dry["scanned"] == 1
    assert dry["would_copy"] == 1
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s", (user_id,))[0][0] == 0
    assert _tee(
        "SELECT count(*) FROM tee_pending_device_migration WHERE user_id=%s", (user_id,)
    )[0][0] == 1

    applied = reflow.reflow_table("chat_messages", dry_run=False, qps=0)

    assert applied["copied"] == 1
    assert applied["pending_cleared"] == 1
    assert applied["errors"] == 0
    assert _tee(
        "SELECT doc FROM chat_messages WHERE user_id=%s AND msg_id=%s", (user_id, msg_id)
    ) == [(doc,)]
    assert _tee(
        "SELECT count(*) FROM tee_pending_device_migration WHERE user_id=%s", (user_id,)
    )[0][0] == 0
    assert _tee(
        "SELECT watermark_ts,watermark_id FROM tee_replication_cursors "
        "WHERE table_name='chat_messages'"
    ) == [(99.0, "past-the-row")]


def test_reflow_includes_negative_numeric_timestamps(backend_env):
    """Using the normal numeric cursor's 0.0 default silently omits valid old rows."""
    user_id = f"usr_{uuid.uuid4().hex[:8]}"
    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    _seed_encrypted_user(user_id)
    doc = _chat_doc(user_id, msg_id)
    doc["ts"] = -10.0
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES (%s,%s,-10,%s)",
            (user_id, msg_id, Jsonb(doc)),
        )

    report = reflow.reflow_table("chat_messages", dry_run=False, qps=0)

    assert report["scanned"] == 1
    assert report["copied"] == 1
    assert _tee(
        "SELECT count(*) FROM chat_messages WHERE user_id=%s AND msg_id=%s",
        (user_id, msg_id),
    )[0][0] == 1


def test_reflow_removes_pending_whose_source_row_is_gone(backend_env):
    """Skipping orphan cleanup would keep Phase 4 blocked after successful replay."""
    user_id = f"usr_{uuid.uuid4().hex[:8]}"
    _seed_encrypted_user(user_id)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO tee_pending_device_migration (user_id,table_name,item_id,reason) "
            "VALUES (%s,'chat_messages','gone','pdm:old-device')",
            (user_id,),
        )

    dry = reflow.reflow_table("chat_messages", dry_run=True, qps=0)
    assert dry["orphan_pending"] == 1
    assert _tee(
        "SELECT count(*) FROM tee_pending_device_migration WHERE user_id=%s", (user_id,)
    )[0][0] == 1

    applied = reflow.reflow_table("chat_messages", dry_run=False, qps=0)
    assert applied["orphan_pending_deleted"] == 1
    assert _tee(
        "SELECT count(*) FROM tee_pending_device_migration WHERE user_id=%s", (user_id,)
    )[0][0] == 0


def test_prune_requires_exact_stale_count_to_override_delete_guard(backend_env, monkeypatch):
    """A broad override must not delete if the live stale count changed after preview."""
    monkeypatch.setattr("tee_shadow.ciphertext_prune._MIN_ABS_GUARD", 0)
    monkeypatch.setattr("tee_shadow.ciphertext_prune._MAX_FRACTION", 0.0)
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO v2_trajectory_events "
            "(user_id,job_id,event_index,event_kind,idempotency_key,payload_bytes,truncated,created_at,payload_envelope) "
            "VALUES ('usr_gone',900001,0,'turn','stale',1,false,now(),'{}'::jsonb)"
        )

    refused = ciphertext_prune.prune_table(
        "v2_trajectory_events", dry_run=False, expected_stale=2
    )
    assert refused["stale"] == 1
    assert refused["deleted"] == 0
    assert refused["refused"]
    assert _tee("SELECT count(*) FROM v2_trajectory_events WHERE job_id=900001")[0][0] == 1

    applied = ciphertext_prune.prune_table(
        "v2_trajectory_events", dry_run=False, expected_stale=1
    )
    assert applied["deleted"] == 1
    assert applied["refused"] is None
    assert _tee("SELECT count(*) FROM v2_trajectory_events WHERE job_id=900001")[0][0] == 0


def test_reflow_dry_run_does_not_write_error_logs(backend_env, monkeypatch):
    """Calling the durable error logger from dry-run would mutate RDS and TEE."""
    user_id = f"usr_{uuid.uuid4().hex[:8]}"
    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    _seed_encrypted_user(user_id)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES (%s,%s,10,%s)",
            (user_id, msg_id, Jsonb(_chat_doc(user_id, msg_id))),
        )
    logged = []
    monkeypatch.setattr(
        worker, "_produce_write", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    monkeypatch.setattr(worker, "_log_row_error", lambda *args: logged.append(args))

    report = reflow.reflow_table("chat_messages", dry_run=True, qps=0)

    assert report["errors"] == 1
    assert logged == []
    assert _tee("SELECT count(*) FROM chat_messages WHERE user_id=%s", (user_id,))[0][0] == 0


def test_voice_reflow_terminal_row_deletes_prior_tee_plaintext(backend_env, monkeypatch):
    """Missing the typed voice delete leaves plaintext beside terminal pending."""
    user_id = f"usr_{uuid.uuid4().hex[:8]}"
    call_id = f"vcall_{uuid.uuid4().hex[:8]}"
    seed_user(user_id, api_key_hash="h", doc={})
    worker._carry_verbatim_cache.clear()
    def _permanent_failure(*_args, **_kwargs):
        raise RuntimeError("enclave_http_403:decrypt_failed:old-key")

    monkeypatch.setattr(
        worker, "_get_decrypt", lambda *_args, **_kwargs: _permanent_failure
    )
    envelope = {
        "v": 1,
        "id": call_id,
        "owner_user_id": user_id,
        "visibility": "shared",
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "user-key",
        "K_enclave": "enclave-key",
        "enclave_pk_fpr": "fingerprint",
    }
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO voice_transcripts (user_id,call_id,transcript_envelope) "
            "VALUES (%s,%s,%s)",
            (user_id, call_id, Jsonb(envelope)),
        )
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO users (user_id,doc) VALUES (%s,'{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (user_id,),
        )
        conn.execute(
            "INSERT INTO voice_transcripts (user_id,call_id,transcript_envelope) "
            "VALUES (%s,%s,%s)",
            (
                user_id,
                call_id,
                Jsonb(
                    {
                        "id": call_id,
                        "owner_user_id": user_id,
                        "visibility": "shared",
                        "body": "old plaintext",
                    }
                ),
            ),
        )

    report = reflow.reflow_table("voice_transcripts", dry_run=False, qps=0)

    assert report["scanned"] == 1, report
    assert report["quarantined"] == 1, report
    assert report["errors"] == 0
    assert _tee(
        "SELECT count(*) FROM voice_transcripts WHERE user_id=%s AND call_id=%s",
        (user_id, call_id),
    )[0][0] == 0
    assert _tee(
        "SELECT count(*) FROM tee_pending_device_migration "
        "WHERE user_id=%s AND table_name='voice_transcripts' AND item_id=%s",
        (user_id, call_id),
    )[0][0] == 1


def test_chat_reflow_fast_forwards_tee_identity_sequence(backend_env):
    """Reflow-only recovery must not leave the next TEE-primary insert colliding."""
    user_id = f"usr_{uuid.uuid4().hex[:8]}"
    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    _seed_encrypted_user(user_id)
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages (user_id,msg_id,ts,doc,seq) "
            "OVERRIDING SYSTEM VALUE VALUES (%s,%s,10,%s,50000)",
            (user_id, msg_id, Jsonb(_chat_doc(user_id, msg_id))),
        )
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "SELECT setval(pg_get_serial_sequence('chat_messages','seq'),1,false)"
        )

    report = reflow.reflow_table("chat_messages", dry_run=False, qps=0)

    assert report["ok"] is True
    seq = _tee(
        "SELECT last_value FROM chat_messages_seq_seq"
    )[0][0]
    assert seq >= 50000
