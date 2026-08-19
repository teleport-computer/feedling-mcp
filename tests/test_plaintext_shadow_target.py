from __future__ import annotations

import os
import uuid

import psycopg
import pytest
from psycopg.types.json import Jsonb

import db
from accounts import registry
from conftest import seed_user
from plaintext_shadow.config import TargetPolicy
from tee_replicator import transforms
from tee_replicator import worker
from tee_shadow import mirror


@pytest.fixture(autouse=True)
def _reset_target_pool():
    yield
    if mirror._pool is not None:
        mirror._pool.close()
        mirror._pool = None
    if hasattr(mirror, "_pool_dsn"):
        mirror._pool_dsn = None


def _enable_new_target(monkeypatch: pytest.MonkeyPatch) -> TargetPolicy:
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "1")
    monkeypatch.setenv("PLAINTEXT_SHADOW_DATABASE_URL", os.environ["TEE_DATABASE_URL"])
    monkeypatch.delenv("FEEDLING_TEE_DUAL_WRITE", raising=False)
    return TargetPolicy(dsn=os.environ["TEE_DATABASE_URL"])


def test_new_target_is_enabled_in_tee_primary_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_new_target(monkeypatch)
    assert mirror.enabled() is True


def test_stale_legacy_target_remains_disabled_in_tee_primary_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FEEDLING_DATABASE_SCHEMA", "tee")
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    monkeypatch.setenv("TEE_DATABASE_URL", os.environ["TEE_DATABASE_URL"])
    monkeypatch.setenv("FEEDLING_PLAINTEXT_SHADOW_ENABLED", "0")
    assert mirror.enabled() is False


def test_target_pool_uses_plaintext_shadow_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = _enable_new_target(monkeypatch)
    with mirror.get_target_pool(policy).connection() as conn:
        target_name = conn.execute("SELECT current_database()").fetchone()[0]
    with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as conn:
        expected_name = conn.execute("SELECT current_database()").fetchone()[0]
    assert target_name == expected_name


def test_hot_mirror_writes_new_target_in_tee_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_new_target(monkeypatch)
    key = f"plaintext-shadow-target-{uuid.uuid4().hex}"
    try:
        mirror.execute(
            "INSERT INTO server_config (key, value) VALUES (%s, %s)",
            (key, b"value"),
        )
        with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as conn:
            assert conn.execute(
                "SELECT value FROM server_config WHERE key=%s", (key,)
            ).fetchone() == (b"value",)
    finally:
        with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
            conn.execute("DELETE FROM server_config WHERE key=%s", (key,))


class _Cfg:
    transform = staticmethod(transforms.plaintext_chat_doc)


def _sealed_doc(uid: str, msg_id: str, body_ct: str) -> dict:
    return {
        "id": msg_id,
        "role": "user",
        "ts": 1.0,
        "source": "app",
        "v": 1,
        "body_ct": body_ct,
        "nonce": "nonce",
        "K_user": "user-key",
        "K_enclave": "enclave-key",
        "enclave_pk_fpr": "fingerprint",
        "visibility": "shared",
        "owner_user_id": uid,
        "content_type": "text",
    }


def test_plaintext_all_decrypts_explicit_on_user(
    backend_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = f"usr_{uuid.uuid4().hex[:12]}"
    seed_user(uid, api_key_hash="h", doc={})
    registry._set_user_content_encryption(uid, "on")
    worker._carry_verbatim_cache.clear()
    monkeypatch.setattr(
        worker,
        "_get_decrypt",
        lambda _uid, **_kwargs: lambda _doc, purpose=None: b"decrypted",
    )

    output = worker._transform_with_retry(
        _Cfg(),
        _sealed_doc(uid, "msg-policy", "ciphertext"),
        uid,
        target_policy=TargetPolicy(dsn=os.environ["TEE_DATABASE_URL"]),
    )

    assert output["body"] == "decrypted"
    assert not ({"body_ct", "nonce", "K_user", "K_enclave"} & output.keys())


def test_every_ciphertext_config_has_keyed_current_row_contract() -> None:
    missing = {
        name: {
            "fetch": cfg.key_fetch_sql,
            "delete": cfg.key_delete_sql,
            "params": cfg.key_params,
        }
        for name, cfg in worker._TABLES.items()
        if not (cfg.key_fetch_sql and cfg.key_delete_sql and cfg.key_params)
    }
    assert missing == {}


def test_keyed_replay_reads_current_row_and_propagates_delete(
    backend_env,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = f"usr_{uuid.uuid4().hex[:12]}"
    msg_id = f"msg-{uuid.uuid4().hex[:10]}"
    seed_user(uid, api_key_hash="h", doc={})
    registry._set_user_content_encryption(uid, "on")
    worker._carry_verbatim_cache.clear()
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as target:
        target.execute(
            "INSERT INTO users (user_id, doc) VALUES (%s, '{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )
    monkeypatch.setattr(
        worker,
        "_get_decrypt",
        lambda _uid, **_kwargs: (
            lambda envelope, purpose=None: f"PT:{envelope['body_ct']}".encode()
        ),
    )
    policy = TargetPolicy(dsn=os.environ["TEE_DATABASE_URL"])

    with db.get_pool().connection() as source:
        source.execute(
            "INSERT INTO chat_messages (user_id, msg_id, ts, doc) VALUES (%s,%s,%s,%s)",
            (uid, msg_id, 1.0, Jsonb(_sealed_doc(uid, msg_id, "first"))),
        )

    first = worker.run_keys(
        "chat_messages",
        [{"user_id": uid, "msg_id": msg_id}],
        target_policy=policy,
    )
    assert first == {"table": "chat_messages", "applied": 1, "deleted": 0, "pending": 0}

    with db.get_pool().connection() as source:
        source.execute(
            "UPDATE chat_messages SET doc=%s WHERE user_id=%s AND msg_id=%s",
            (Jsonb(_sealed_doc(uid, msg_id, "latest")), uid, msg_id),
        )
    worker.run_keys(
        "chat_messages",
        [{"user_id": uid, "msg_id": msg_id}],
        target_policy=policy,
    )
    with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as target:
        assert target.execute(
            "SELECT doc->>'body' FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, msg_id),
        ).fetchone() == ("PT:latest",)

    with db.get_pool().connection() as source:
        source.execute(
            "DELETE FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, msg_id),
        )
    deleted = worker.run_keys(
        "chat_messages",
        [{"user_id": uid, "msg_id": msg_id}],
        target_policy=policy,
    )
    assert deleted["deleted"] == 1
    with psycopg.connect(os.environ["TEE_DATABASE_URL"]) as target:
        assert target.execute(
            "SELECT count(*) FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (uid, msg_id),
        ).fetchone() == (0,)
