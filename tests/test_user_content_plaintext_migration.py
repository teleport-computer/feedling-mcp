from __future__ import annotations

import json
import os
import sys

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from content import plaintext_migration  # noqa: E402
import migrate_user_content_to_plaintext as cli  # noqa: E402
import db  # noqa: E402
from conftest import seed_user  # noqa: E402


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
