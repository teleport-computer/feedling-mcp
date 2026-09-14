from __future__ import annotations

import base64
import hashlib
import json
import os
import sys

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from content import plaintext_migration  # noqa: E402
import migrate_user_content_to_plaintext as cli  # noqa: E402
import db  # noqa: E402
import object_storage  # noqa: E402
from conftest import capture_sleeps, seed_user  # noqa: E402


def test_cli_requires_an_exact_user_before_accessing_data(monkeypatch, capsys):
    monkeypatch.setattr(
        plaintext_migration,
        "run",
        lambda *_a, **_kw: pytest.fail("argument gate must precede data access"),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main([])

    assert exc.value.code == 2
    assert "--user" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("extra_args", "enable_env"),
    [
        ([], False),
        (["--allow-plaintext-rewrite"], False),
        ([], True),
    ],
)
def test_apply_requires_both_independent_write_gates(
    monkeypatch, capsys, extra_args, enable_env
):
    if enable_env:
        monkeypatch.setenv(plaintext_migration.APPLY_ENV, "1")
    else:
        monkeypatch.delenv(plaintext_migration.APPLY_ENV, raising=False)
    monkeypatch.setattr(
        plaintext_migration,
        "run",
        lambda *_a, **_kw: pytest.fail("write gate must precede data access"),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["--user", "usr_target", "--apply", *extra_args])

    assert exc.value.code == 2
    assert "requires" in capsys.readouterr().err


def test_dry_run_does_not_construct_decryptor(monkeypatch, capsys):
    monkeypatch.setattr(plaintext_migration, "user_exists", lambda _uid: True)
    monkeypatch.setattr(
        plaintext_migration,
        "inventory",
        lambda user_id: [
            plaintext_migration.Item(
                surface="chat_live",
                item_id="msg-1",
                classification="migratable_shared",
            )
        ],
    )
    monkeypatch.setattr(
        plaintext_migration,
        "make_decrypt",
        lambda *_a, **_kw: pytest.fail("dry-run must not construct decryptor"),
    )

    assert cli.main(["--user", "usr_target", "--json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "apply": False,
        "counts": {"migratable_shared": 1},
        "failures": 0,
        "user_id": "usr_target",
    }


@pytest.mark.parametrize("preference", [None, "on", ""])
def test_apply_rejects_any_preference_other_than_explicit_off(
    monkeypatch, capsys, preference
):
    monkeypatch.setenv(plaintext_migration.APPLY_ENV, "1")
    monkeypatch.setattr(plaintext_migration, "user_exists", lambda _uid: True)
    monkeypatch.setattr(
        plaintext_migration, "content_encryption_preference", lambda _uid: preference
    )
    monkeypatch.setattr(
        plaintext_migration,
        "inventory",
        lambda *_a, **_kw: pytest.fail("preference gate must precede inventory"),
    )

    assert cli.main(
        [
            "--user",
            "usr_target",
            "--apply",
            "--allow-plaintext-rewrite",
        ]
    ) == 2
    assert "explicitly off" in capsys.readouterr().err


def test_apply_gate_accepts_explicit_off_without_exposing_items(monkeypatch, capsys):
    monkeypatch.setenv(plaintext_migration.APPLY_ENV, "1")
    monkeypatch.setattr(plaintext_migration, "user_exists", lambda _uid: True)
    monkeypatch.setattr(
        plaintext_migration, "content_encryption_preference", lambda _uid: "off"
    )
    monkeypatch.setattr(plaintext_migration, "inventory", lambda _uid: [])

    assert cli.main(
        [
            "--user",
            "usr_target",
            "--apply",
            "--allow-plaintext-rewrite",
            "--json",
        ]
    ) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["counts"] == {}
    assert set(report) == {"apply", "counts", "failures", "user_id"}


def test_dry_run_rejects_unknown_user_before_inventory(monkeypatch, capsys):
    monkeypatch.setattr(plaintext_migration, "user_exists", lambda _uid: False)
    monkeypatch.setattr(
        plaintext_migration,
        "inventory",
        lambda _uid: pytest.fail("unknown-user gate must precede inventory"),
    )

    assert cli.main(["--user", "usr_typo", "--json"]) == 2
    assert "does not exist" in capsys.readouterr().err


def _encrypted(item_id: str, *, visibility: str = "shared") -> dict:
    doc = {
        "id": item_id,
        "visibility": visibility,
        "owner_user_id": "usr_inventory",
        "v": 1,
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "user-key",
    }
    if visibility != "local_only":
        doc["K_enclave"] = "enclave-key"
    return doc


def test_inventory_classifies_all_supported_surfaces_without_content_access(monkeypatch):
    user_id = "usr_inventory"
    seed_user(user_id, content_encryption="off")
    shared = _encrypted("chat-encrypted")
    local = _encrypted("chat-local", visibility="local_only")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES "
            "(%s,'chat-plain',1,%s),(%s,'chat-encrypted',2,%s),"
            "(%s,'chat-local',3,%s)",
            (
                user_id,
                Jsonb({"id": "chat-plain", "body": "plain"}),
                user_id,
                Jsonb(shared),
                user_id,
                Jsonb(local),
            ),
        )
        source_seq = conn.execute(
            "SELECT seq FROM chat_messages WHERE user_id=%s AND msg_id='chat-encrypted'",
            (user_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO chat_message_archive"
            "(user_id,source_seq,msg_id,ts,doc,storage_generation,clear_generation) "
            "VALUES (%s,%s,'archived',4,%s,0,1)",
            (user_id, source_seq, Jsonb(_encrypted("archived"))),
        )
        conn.execute(
            "INSERT INTO memory_moments(user_id,moment_id,occurred_at,doc) "
            "VALUES (%s,'memory-invalid','2026-01-01',%s)",
            (user_id, Jsonb({"id": "memory-invalid", "K_enclave": "key"})),
        )
        conn.execute(
            "INSERT INTO world_book_entries(user_id,entry_id,updated_at,doc) "
            "VALUES (%s,'world-encrypted','2026-01-02',%s)",
            (user_id, Jsonb(_encrypted("world-encrypted"))),
        )
        conn.execute(
            "INSERT INTO user_blobs(user_id,kind,doc) VALUES (%s,'identity',%s)",
            (user_id, Jsonb(_encrypted("identity", visibility="local_only"))),
        )
        frame_meta = _encrypted("frame-encrypted")
        frame_meta.pop("body_ct")
        conn.execute(
            "INSERT INTO frame_envelopes(user_id,frame_id,ts,doc,env_meta,body_key) "
            "VALUES (%s,'frame-encrypted',5,NULL,%s,'frames/opaque')",
            (user_id, Jsonb(frame_meta)),
        )

    monkeypatch.setattr(
        plaintext_migration,
        "make_decrypt",
        lambda *_a, **_kw: pytest.fail("inventory must not construct decryptor"),
    )

    items = list(plaintext_migration.inventory(user_id))

    assert [(item.surface, item.item_id) for item in items] == [
        ("chat_live", "chat-plain"),
        ("chat_live", "chat-encrypted"),
        ("chat_live", "chat-local"),
        ("chat_archive", str(source_seq)),
        ("memory", "memory-invalid"),
        ("world_book", "world-encrypted"),
        ("identity", "identity"),
        ("frame", "frame-encrypted"),
    ]
    assert [item.classification for item in items] == [
        "already_plaintext",
        "migratable_shared",
        "skipped_local_only",
        "migratable_shared",
        "invalid_shape",
        "migratable_shared",
        "skipped_local_only",
        "migratable_shared",
    ]


def test_chat_with_plain_main_and_encrypted_shared_subcontent_is_migratable():
    doc = {
        "id": "mixed",
        "body": "plain main",
        "thinking_body_ct": "ciphertext",
        "thinking_nonce": "nonce",
        "thinking_K_user": "user-key",
        "thinking_K_enclave": "enclave-key",
        "thinking_visibility": "shared",
    }

    assert plaintext_migration.classify_chat(doc) == "migratable_shared"


def test_chat_with_undecryptable_encrypted_subcontent_is_skipped():
    doc = {
        "id": "mixed-local",
        "body": "plain main",
        "caption_body_ct": "ciphertext",
        "caption_nonce": "nonce",
        "caption_K_user": "user-key",
        "caption_visibility": "local_only",
    }

    assert plaintext_migration.classify_chat(doc) == "skipped_local_only"


def test_apply_cas_migrates_all_inline_documents_and_is_idempotent(monkeypatch):
    user_id = "usr_apply_inline"
    seed_user(user_id, content_encryption="off")
    chat = _encrypted("chat-inline")
    chat.update(
        {
            "thinking_body_ct": "thinking-ciphertext",
            "thinking_nonce": "nonce",
            "thinking_K_user": "user-key",
            "thinking_K_enclave": "enclave-key",
            "thinking_visibility": "shared",
        }
    )
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES (%s,%s,1,%s)",
            (user_id, "chat-inline", Jsonb(chat)),
        )
        source_seq = conn.execute(
            "SELECT seq FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (user_id, "chat-inline"),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO chat_message_archive"
            "(user_id,source_seq,msg_id,ts,doc,storage_generation,clear_generation) "
            "VALUES (%s,%s,'archive-inline',2,%s,0,1)",
            (user_id, source_seq, Jsonb(_encrypted("archive-inline"))),
        )
        conn.execute(
            "INSERT INTO memory_moments(user_id,moment_id,occurred_at,doc) "
            "VALUES (%s,'memory-inline','2026-01-01',%s)",
            (user_id, Jsonb(_encrypted("memory-inline"))),
        )
        conn.execute(
            "INSERT INTO world_book_entries(user_id,entry_id,updated_at,doc) "
            "VALUES (%s,'world-inline','2026-01-02',%s)",
            (user_id, Jsonb(_encrypted("world-inline"))),
        )
        conn.execute(
            "INSERT INTO user_blobs(user_id,kind,doc) VALUES (%s,'identity',%s)",
            (user_id, Jsonb(_encrypted("identity-inline"))),
        )

    decrypt_calls = []

    def decrypt(_envelope, purpose):
        decrypt_calls.append(purpose)
        return f"plain:{purpose}".encode()

    monkeypatch.setattr(plaintext_migration, "make_decrypt", lambda _uid: decrypt)

    first = plaintext_migration.run(user_id, apply=True)

    assert first.failures == 0
    assert first.counts == {"migrated": 5}
    assert len(decrypt_calls) == 6
    with db.get_pool().connection() as conn:
        docs = [
            conn.execute(
                "SELECT doc FROM chat_messages WHERE user_id=%s AND msg_id='chat-inline'",
                (user_id,),
            ).fetchone()[0],
            conn.execute(
                "SELECT doc FROM chat_message_archive WHERE user_id=%s AND source_seq=%s",
                (user_id, source_seq),
            ).fetchone()[0],
            conn.execute(
                "SELECT doc FROM memory_moments WHERE user_id=%s AND moment_id='memory-inline'",
                (user_id,),
            ).fetchone()[0],
            conn.execute(
                "SELECT doc FROM world_book_entries WHERE user_id=%s AND entry_id='world-inline'",
                (user_id,),
            ).fetchone()[0],
            conn.execute(
                "SELECT doc FROM user_blobs WHERE user_id=%s AND kind='identity'",
                (user_id,),
            ).fetchone()[0],
        ]
    assert all(isinstance(doc.get("body"), str) for doc in docs)
    assert docs[0]["thinking"]["body"].startswith("plain:")
    assert all("body_ct" not in doc and "K_enclave" not in doc for doc in docs)

    monkeypatch.setattr(
        plaintext_migration,
        "make_decrypt",
        lambda _uid: pytest.fail("idempotent rerun must not decrypt plaintext"),
    )
    second = plaintext_migration.run(user_id, apply=True)
    assert second.counts == {"already_plaintext": 5}
    assert second.failures == 0


def test_inline_cas_refuses_a_concurrent_document_change():
    user_id = "usr_inline_cas"
    seed_user(user_id, content_encryption="off")
    original = _encrypted("memory-cas")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO memory_moments(user_id,moment_id,occurred_at,doc) "
            "VALUES (%s,'memory-cas','2026-01-01',%s)",
            (user_id, Jsonb(original)),
        )
    item = next(item for item in plaintext_migration.inventory(user_id)
                if item.surface == "memory")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE memory_moments SET doc=doc || '{\"concurrent\":true}'::jsonb "
            "WHERE user_id=%s AND moment_id='memory-cas'",
            (user_id,),
        )

    assert plaintext_migration.cas_inline_doc(
        user_id, item, {"id": "memory-cas", "body": "plain"}
    ) is False


def test_inline_cas_rechecks_explicit_off_in_the_write_transaction():
    user_id = "usr_inline_pref_flip"
    seed_user(user_id, content_encryption="off")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO memory_moments(user_id,moment_id,occurred_at,doc) "
            "VALUES (%s,'memory-pref','2026-01-01',%s)",
            (user_id, Jsonb(_encrypted("memory-pref"))),
        )
    item = next(item for item in plaintext_migration.inventory(user_id)
                if item.surface == "memory")
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE users SET doc=doc || '{\"content_encryption\":\"on\"}'::jsonb "
            "WHERE user_id=%s",
            (user_id,),
        )

    assert plaintext_migration.cas_inline_doc(
        user_id, item, {"id": "memory-pref", "body": "plain"}
    ) is False


def test_chat_r2_body_uses_existing_crash_safe_pointer_migration(monkeypatch):
    key = "chatfiles/usr_chat_r2/g0/msg-r2/old"
    item = plaintext_migration.Item(
        surface="chat_live",
        item_id="msg-r2",
        classification="migratable_shared",
        doc={
            "id": "msg-r2",
            "content_type": "file",
            "visibility": "shared",
            "owner_user_id": "usr_chat_r2",
            "body_key": key,
            "body_ct_len": 12,
            "K_enclave": "enclave-key",
            "nonce": "nonce",
            "K_user": "user-key",
        },
        storage_generation=0,
        body_key=key,
    )
    monkeypatch.setattr(object_storage, "chat_key_owned_by", lambda k, u: True)
    monkeypatch.setattr(
        object_storage,
        "get_chat_body",
        lambda k, u: base64.b64encode(b"sealed").decode(),
    )
    captured = {}

    def migrate(user_id, **kwargs):
        captured["user_id"] = user_id
        captured.update(kwargs)
        return True

    monkeypatch.setattr(db, "migrate_chat_r2_pointer_to_plaintext", migrate)

    status = plaintext_migration.migrate_item(
        "usr_chat_r2", item, lambda env, purpose: b"plain-file-bytes"
    )

    assert status == "migrated"
    assert captured["table"] == "live"
    assert captured["item_id"] == "msg-r2"
    assert captured["old_body_key"] == key
    assert captured["plaintext"] == b"plain-file-bytes"
    assert captured["content_type"] == "file"


def test_chat_r2_promotion_then_cas_migrates_encrypted_subcontent(monkeypatch):
    user_id = "usr_chat_r2_sub"
    seed_user(user_id, content_encryption="off")
    key = f"chatimages/{user_id}/g0/msg-r2-sub/old"
    old_doc = {
        **_encrypted("msg-r2-sub"),
        "content_type": "image",
        "body_key": key,
        "body_ct_len": 12,
        "body_ct": None,
        "caption_body_ct": "caption-sealed",
        "caption_nonce": "caption-nonce",
        "caption_K_user": "caption-user-key",
        "caption_K_enclave": "caption-enclave-key",
        "caption_visibility": "shared",
    }
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES (%s,%s,1,%s)",
            (user_id, "msg-r2-sub", Jsonb(old_doc)),
        )
    item = next(iter(plaintext_migration.inventory(user_id)))
    monkeypatch.setattr(object_storage, "chat_key_owned_by", lambda k, u: True)
    monkeypatch.setattr(
        object_storage,
        "get_chat_body",
        lambda k, u: base64.b64encode(b"sealed-main").decode(),
    )

    def promote(_user_id, **_kwargs):
        pointer = {
            "body_key": f"chatimages/{user_id}/g0/msg-r2-sub/new",
            "body_object_format": "plaintext_v1",
            "body_size_bytes": 10,
            "body_sha256": "a" * 64,
        }
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE chat_messages SET doc=(doc-'body_ct'-'body_ct_len'-'K_enclave'"
                "-'K_user'-'nonce') || %s WHERE user_id=%s AND msg_id=%s",
                (Jsonb(pointer), user_id, "msg-r2-sub"),
            )
        return True

    monkeypatch.setattr(db, "migrate_chat_r2_pointer_to_plaintext", promote)

    status = plaintext_migration.migrate_item(
        user_id,
        item,
        lambda env, purpose: b"caption-plain" if "caption" in purpose else b"main-plain",
    )

    assert status == "migrated"
    with db.get_pool().connection() as conn:
        doc = conn.execute(
            "SELECT doc FROM chat_messages WHERE user_id=%s AND msg_id=%s",
            (user_id, "msg-r2-sub"),
        ).fetchone()[0]
    assert doc["body_object_format"] == "plaintext_v1"
    assert doc["caption"]["body"] == "caption-plain"
    assert "caption_body_ct" not in doc


def test_inline_frame_migrates_to_plaintext_without_r2(monkeypatch):
    user_id = "usr_frame_inline_migrate"
    seed_user(user_id, content_encryption="off")
    frame_doc = _encrypted("frame-inline")
    frame_doc["source"] = "screen"
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes(user_id,frame_id,ts,doc) VALUES (%s,%s,1,%s)",
            (user_id, "frame-inline", Jsonb(frame_doc)),
        )
    item = next(iter(plaintext_migration.inventory(user_id)))
    monkeypatch.setattr(object_storage, "enabled", lambda: False)

    assert plaintext_migration.migrate_item(
        user_id, item, lambda env, purpose: b"plain-frame"
    ) == "migrated"

    with db.get_pool().connection() as conn:
        doc, meta, key = conn.execute(
            "SELECT doc,env_meta,body_key FROM frame_envelopes "
            "WHERE user_id=%s AND frame_id='frame-inline'",
            (user_id,),
        ).fetchone()
    assert base64.b64decode(doc["body_b64"]) == b"plain-frame"
    assert doc["body_size_bytes"] == len(b"plain-frame")
    assert doc["body_sha256"] == hashlib.sha256(b"plain-frame").hexdigest()
    assert "body_ct" not in doc and "K_enclave" not in doc
    assert meta is None and key is None


def test_r2_frame_uses_fresh_plaintext_key_then_cas_and_retires_old(monkeypatch):
    user_id = "usr_frame_r2_migrate"
    seed_user(user_id, content_encryption="off")
    old_key = f"frames/{user_id}/frame-r2"
    meta = _encrypted("frame-r2")
    meta.pop("body_ct")
    meta["source"] = "photo"
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes(user_id,frame_id,ts,doc,env_meta,body_key) "
            "VALUES (%s,'frame-r2',1,NULL,%s,%s)",
            (user_id, Jsonb(meta), old_key),
        )
    item = next(iter(plaintext_migration.inventory(user_id)))
    monkeypatch.setattr(object_storage, "enabled", lambda: True)
    monkeypatch.setattr(
        object_storage,
        "get_frame_body_by_key_strict",
        lambda key, uid: base64.b64encode(b"sealed-frame").decode(),
    )
    puts = []
    deleted = []

    def put(uid, frame_id, raw):
        puts.append((uid, frame_id, raw))
        return f"frames-plaintext/{uid}/{frame_id}/digest"

    monkeypatch.setattr(object_storage, "put_frame_plaintext_body", put)
    monkeypatch.setattr(
        object_storage,
        "delete_frame_body_key",
        lambda key, uid: deleted.append((key, uid)) or True,
    )

    assert plaintext_migration.migrate_item(
        user_id, item, lambda env, purpose: b"plain-r2-frame"
    ) == "migrated"

    with db.get_pool().connection() as conn:
        doc, stored_meta, stored_key = conn.execute(
            "SELECT doc,env_meta,body_key FROM frame_envelopes "
            "WHERE user_id=%s AND frame_id='frame-r2'",
            (user_id,),
        ).fetchone()
    assert doc is None
    assert stored_key.startswith(f"frames-plaintext/{user_id}/frame-r2/")
    assert stored_meta["body_object_format"] == "plaintext_v1"
    assert stored_meta["body_sha256"] == hashlib.sha256(b"plain-r2-frame").hexdigest()
    assert puts == [(user_id, "frame-r2", b"plain-r2-frame")]
    assert deleted == [(old_key, user_id)]


def test_r2_frame_missing_object_fails_before_decrypt(monkeypatch):
    user_id = "usr_frame_r2_missing"
    seed_user(user_id, content_encryption="off")
    old_key = f"frames/{user_id}/missing"
    meta = _encrypted("missing")
    meta.pop("body_ct")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes(user_id,frame_id,ts,doc,env_meta,body_key) "
            "VALUES (%s,'missing',1,NULL,%s,%s)",
            (user_id, Jsonb(meta), old_key),
        )
    item = next(iter(plaintext_migration.inventory(user_id)))
    monkeypatch.setattr(object_storage, "get_frame_body_strict", lambda *_a: None)

    status = plaintext_migration.migrate_item(
        user_id,
        item,
        lambda *_a, **_kw: pytest.fail("missing object must not decrypt"),
    )

    assert status == "failed_r2_object_missing"


def test_frame_preference_flip_blocks_before_r2_upload(monkeypatch):
    user_id = "usr_frame_pref_flip"
    seed_user(user_id, content_encryption="off")
    doc = _encrypted("frame-pref")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes(user_id,frame_id,ts,doc) VALUES (%s,%s,1,%s)",
            (user_id, "frame-pref", Jsonb(doc)),
        )
    item = next(iter(plaintext_migration.inventory(user_id)))
    with db.get_pool().connection() as conn:
        conn.execute(
            "UPDATE users SET doc=doc || '{\"content_encryption\":\"on\"}'::jsonb "
            "WHERE user_id=%s",
            (user_id,),
        )
    monkeypatch.setattr(object_storage, "enabled", lambda: True)
    monkeypatch.setattr(
        object_storage,
        "put_frame_plaintext_body",
        lambda *_a: pytest.fail("preference flip must block before R2 upload"),
    )

    assert plaintext_migration.migrate_item(
        user_id, item, lambda *_a, **_kw: b"plain"
    ) == "cas_conflict"


def test_frame_cas_loss_cleans_candidate_and_preserves_legacy_object(monkeypatch):
    user_id = "usr_frame_cas_loss"
    seed_user(user_id, content_encryption="off")
    old_key = f"frames/{user_id}/frame-race"
    meta = _encrypted("frame-race")
    meta.pop("body_ct")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes(user_id,frame_id,ts,doc,env_meta,body_key) "
            "VALUES (%s,'frame-race',1,NULL,%s,%s)",
            (user_id, Jsonb(meta), old_key),
        )
    item = next(iter(plaintext_migration.inventory(user_id)))
    monkeypatch.setattr(object_storage, "enabled", lambda: True)
    monkeypatch.setattr(
        object_storage,
        "get_frame_body_strict",
        lambda *_a: base64.b64encode(b"sealed").decode(),
    )
    candidate = f"frames-plaintext/{user_id}/frame-race/digest"

    def put_then_race(*_args):
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE frame_envelopes SET env_meta=env_meta || "
                "'{\"concurrent\":true}'::jsonb WHERE user_id=%s AND frame_id='frame-race'",
                (user_id,),
            )
        return candidate

    deleted = []
    monkeypatch.setattr(object_storage, "put_frame_plaintext_body", put_then_race)
    monkeypatch.setattr(
        object_storage,
        "delete_frame_body_key",
        lambda key, uid: deleted.append((key, uid)) or True,
    )

    assert plaintext_migration.migrate_item(
        user_id, item, lambda *_a, **_kw: b"plain"
    ) == "cas_conflict"
    assert deleted == [(candidate, user_id)]
    with db.get_pool().connection() as conn:
        stored_key = conn.execute(
            "SELECT body_key FROM frame_envelopes WHERE user_id=%s AND frame_id='frame-race'",
            (user_id,),
        ).fetchone()[0]
    assert stored_key == old_key


def test_frame_get_follows_persisted_plaintext_migration_key(monkeypatch):
    user_id = "usr_frame_key_read"
    seed_user(user_id, content_encryption="off")
    raw = b"frame-body"
    key = f"frames-plaintext/{user_id}/frame-key/{hashlib.sha256(raw).hexdigest()}"
    meta = {
        "id": "frame-key",
        "body_object_format": "plaintext_v1",
        "body_sha256": hashlib.sha256(raw).hexdigest(),
        "body_size_bytes": len(raw),
        db.FRAME_PLAINTEXT_CLEANUP_PENDING_FIELD: True,
    }
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes(user_id,frame_id,ts,doc,env_meta,body_key) "
            "VALUES (%s,'frame-key',1,NULL,%s,%s)",
            (user_id, Jsonb(meta), key),
        )
    monkeypatch.setattr(
        object_storage,
        "get_frame_body_strict",
        lambda *_a: pytest.fail("reader must follow the persisted versioned key"),
    )
    monkeypatch.setattr(
        object_storage,
        "get_frame_body_by_key_strict",
        lambda stored_key, uid: base64.b64encode(raw).decode(),
    )

    loaded = db.frame_get(user_id, "frame-key", unavailable_raises=True)

    assert base64.b64decode(loaded["body_b64"]) == raw
    assert db.FRAME_PLAINTEXT_CLEANUP_PENDING_FIELD not in loaded


def test_frame_cleanup_failure_is_durable_and_rerun_needs_no_decrypt(monkeypatch):
    user_id = "usr_frame_cleanup_resume"
    seed_user(user_id, content_encryption="off")
    old_key = f"frames/{user_id}/frame-cleanup"
    meta = _encrypted("frame-cleanup")
    meta.pop("body_ct")
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes(user_id,frame_id,ts,doc,env_meta,body_key) "
            "VALUES (%s,'frame-cleanup',1,NULL,%s,%s)",
            (user_id, Jsonb(meta), old_key),
        )
    monkeypatch.setattr(object_storage, "enabled", lambda: True)
    monkeypatch.setattr(
        object_storage,
        "get_frame_body_strict",
        lambda *_a: base64.b64encode(b"sealed").decode(),
    )
    monkeypatch.setattr(
        object_storage,
        "put_frame_plaintext_body",
        lambda uid, fid, raw: f"frames-plaintext/{uid}/{fid}/digest",
    )
    delete_outcomes = iter([False, True])
    monkeypatch.setattr(
        object_storage,
        "delete_frame_body_key",
        lambda *_a: next(delete_outcomes),
    )
    monkeypatch.setattr(
        plaintext_migration,
        "make_decrypt",
        lambda _uid: lambda *_a, **_kw: b"plain-frame",
    )

    first = plaintext_migration.run(user_id, apply=True, rate=1000)
    assert first.counts == {"failed_frame_cleanup_pending": 1}
    assert first.failures == 1
    assert [item.classification for item in plaintext_migration.inventory(user_id)] == [
        "cleanup_pending"
    ]

    monkeypatch.setattr(
        plaintext_migration,
        "make_decrypt",
        lambda _uid: pytest.fail("cleanup resume must not decrypt plaintext"),
    )
    second = plaintext_migration.run(user_id, apply=True, rate=1000)
    assert second.counts == {"cleanup_completed": 1}
    assert second.failures == 0
    assert [item.classification for item in plaintext_migration.inventory(user_id)] == [
        "already_plaintext"
    ]


def test_frame_delete_retires_the_persisted_plaintext_object_key(monkeypatch):
    user_id = "usr_frame_delete_plaintext_key"
    seed_user(user_id, content_encryption="off")
    key = f"frames-plaintext/{user_id}/frame-delete/digest"
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes(user_id,frame_id,ts,doc,env_meta,body_key) "
            "VALUES (%s,'frame-delete',1,NULL,%s,%s)",
            (
                user_id,
                Jsonb({"body_object_format": "plaintext_v1"}),
                key,
            ),
        )
    monkeypatch.setattr(object_storage, "enabled", lambda: True)
    deleted_keys = []
    monkeypatch.setattr(
        object_storage,
        "delete_frame_body_key",
        lambda stored_key, uid: deleted_keys.append((stored_key, uid)) or True,
    )
    monkeypatch.setattr(object_storage, "delete_frame_body", lambda *_a: None)
    monkeypatch.setattr(object_storage, "delete_frame_tee_body", lambda *_a: None)

    db.frame_delete(user_id, "frame-delete")

    assert deleted_keys == [(key, user_id)]


def test_frame_prune_retires_evicted_plaintext_object_key(monkeypatch):
    user_id = "usr_frame_prune_plaintext_key"
    seed_user(user_id, content_encryption="off")
    old_key = f"frames-plaintext/{user_id}/old/digest"
    new_key = f"frames-plaintext/{user_id}/new/digest"
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO frame_envelopes(user_id,frame_id,ts,doc,env_meta,body_key) "
            "VALUES (%s,'old',1,NULL,%s,%s),(%s,'new',2,NULL,%s,%s)",
            (
                user_id,
                Jsonb({"body_object_format": "plaintext_v1"}),
                old_key,
                user_id,
                Jsonb({"body_object_format": "plaintext_v1"}),
                new_key,
            ),
        )
    monkeypatch.setattr(object_storage, "enabled", lambda: True)
    deleted_keys = []
    monkeypatch.setattr(
        object_storage,
        "delete_frame_body_key",
        lambda stored_key, uid: deleted_keys.append((stored_key, uid)) or True,
    )
    monkeypatch.setattr(object_storage, "delete_frame_body", lambda *_a: None)
    monkeypatch.setattr(object_storage, "delete_frame_tee_body", lambda *_a: None)

    assert db.frame_prune_to(user_id, 1) == ["old"]
    assert deleted_keys == [(old_key, user_id)]


def test_apply_limit_and_rate_only_attempt_bounded_migratable_items(monkeypatch):
    items = [
        plaintext_migration.Item(
            "memory", f"m-{index}", "migratable_shared",
            {"id": f"m-{index}", "body_ct": "sealed", "K_enclave": "key"},
        )
        for index in range(3)
    ]
    items.insert(
        0,
        plaintext_migration.Item(
            "memory", "plain", "already_plaintext", {"id": "plain", "body": "ok"}
        ),
    )
    monkeypatch.setattr(
        plaintext_migration, "content_encryption_preference", lambda _uid: "off"
    )
    monkeypatch.setattr(plaintext_migration, "user_exists", lambda _uid: True)
    monkeypatch.setattr(plaintext_migration, "inventory", lambda _uid: items)
    monkeypatch.setattr(plaintext_migration, "make_decrypt", lambda _uid: object())
    attempted = []
    monkeypatch.setattr(
        plaintext_migration,
        "migrate_item",
        lambda _uid, item, _decrypt: attempted.append(item.item_id) or "migrated",
    )
    sleeps = capture_sleeps(monkeypatch, plaintext_migration)

    result = plaintext_migration.run(
        "usr_rate_limit", apply=True, limit=2, rate=2.0
    )

    assert attempted == ["m-0", "m-1"]
    assert sleeps == [0.5]
    assert result.counts == {
        "already_plaintext": 1,
        "migrated": 2,
        "not_attempted_limit": 1,
    }


@pytest.mark.parametrize(("limit", "rate"), [(-1, 1.0), (0, 0), (0, -2.0)])
def test_run_rejects_invalid_limit_or_rate_before_inventory(monkeypatch, limit, rate):
    monkeypatch.setattr(
        plaintext_migration,
        "inventory",
        lambda _uid: pytest.fail("validation must precede inventory"),
    )
    with pytest.raises(ValueError):
        plaintext_migration.run("usr_invalid_controls", limit=limit, rate=rate)


def test_cli_failure_report_does_not_expose_exception_or_item_id(monkeypatch, capsys):
    secret = "secret-body-and-item-id"
    monkeypatch.setenv(plaintext_migration.APPLY_ENV, "1")
    monkeypatch.setattr(
        plaintext_migration, "content_encryption_preference", lambda _uid: "off"
    )
    monkeypatch.setattr(plaintext_migration, "user_exists", lambda _uid: True)
    monkeypatch.setattr(
        plaintext_migration,
        "inventory",
        lambda _uid: [
            plaintext_migration.Item(
                "memory",
                secret,
                "migratable_shared",
                {"id": secret, "body_ct": secret, "K_enclave": "key"},
            )
        ],
    )
    monkeypatch.setattr(plaintext_migration, "make_decrypt", lambda _uid: object())
    monkeypatch.setattr(
        plaintext_migration,
        "migrate_item",
        lambda *_a: (_ for _ in ()).throw(RuntimeError(secret)),
    )

    rc = cli.main(
        [
            "--user",
            "usr_redacted",
            "--apply",
            "--allow-plaintext-rewrite",
            "--json",
        ]
    )

    output = capsys.readouterr()
    assert rc == 1
    assert secret not in output.out and secret not in output.err
    assert json.loads(output.out)["counts"] == {"failed_transform_or_storage": 1}


def test_cli_redacts_unexpected_database_or_setup_failure(monkeypatch, capsys):
    secret_dsn = "postgresql://secret:password@example.invalid/prod"
    monkeypatch.setattr(
        plaintext_migration,
        "run",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError(secret_dsn)),
    )

    assert cli.main(["--user", "usr_setup_failure", "--json"]) == 1

    output = capsys.readouterr()
    assert secret_dsn not in output.out and secret_dsn not in output.err
    assert output.out == ""
    assert "runtimeerror" in output.err
