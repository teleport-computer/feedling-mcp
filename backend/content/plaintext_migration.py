"""Single-user legacy content-envelope to plaintext migration.

The public entry point is deliberately small and fail closed.  Inventory is
read-only; apply requires an explicit per-user ``off`` preference and the CLI's
independent write gates.  Surface-specific inventory and CAS writers live in
this module so the command can be exercised without exposing content values.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

import db
import object_storage
from psycopg.types.json import Jsonb
from tee_replicator import transforms


APPLY_ENV = "FEEDLING_ENABLE_PLAINTEXT_CONTENT_MIGRATION"


@dataclass(frozen=True)
class Item:
    surface: str
    item_id: str
    classification: str
    doc: dict | None = field(default=None, repr=False)
    sort_value: str | int | float | None = None
    storage_generation: int = 0
    body_key: str | None = None
    env_meta: dict | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Result:
    apply: bool
    user_id: str
    counts: dict[str, int]
    failures: int = 0

    def public_dict(self) -> dict:
        """Return the intentionally content-free operator report."""
        return {
            "apply": self.apply,
            "counts": self.counts,
            "failures": self.failures,
            "user_id": self.user_id,
        }


def content_encryption_preference(user_id: str) -> str | None:
    """Read the stored three-state preference; absence is not explicit off."""
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT doc->>'content_encryption' FROM users WHERE user_id=%s",
            (str(user_id),),
        ).fetchone()
    if row is None:
        return None
    value = str(row[0] or "").strip().lower()
    return value or None


def make_decrypt(user_id: str):
    """Create the existing user-scoped enclave decrypt callback lazily."""
    from tee_replicator.worker import _make_decrypt

    return _make_decrypt(user_id)


def _decryptable(envelope: dict) -> bool:
    return (
        envelope.get("visibility") != "local_only"
        and bool(envelope.get("K_enclave"))
    )


def _classify_single(doc: dict | None, *, allow_pointer: bool = False) -> str:
    if not isinstance(doc, dict):
        return "invalid_shape"
    if isinstance(doc.get("body"), str) or doc.get("body_b64") is not None:
        return "already_plaintext"
    if allow_pointer and doc.get("body_key"):
        if doc.get("body_object_format") == "plaintext_v1":
            return "already_plaintext"
        return "migratable_shared" if _decryptable(doc) else "skipped_local_only"
    if doc.get("body_ct") is not None:
        return "migratable_shared" if _decryptable(doc) else "skipped_local_only"
    return "invalid_shape"


def classify_chat(doc: dict | None) -> str:
    """Classify the full Chat shape, including prefixed sub-envelopes."""
    if not isinstance(doc, dict):
        return "invalid_shape"

    main = _classify_single(doc, allow_pointer=True)
    if main == "invalid_shape" and isinstance(doc.get("images"), list):
        main = "already_plaintext"
    if main == "invalid_shape":
        return main

    encrypted = main == "migratable_shared"
    for prefix in ("thinking_", "caption_"):
        body_ct_key = f"{prefix}body_ct"
        body_key = f"{prefix}body"
        if body_ct_key not in doc and body_key not in doc:
            continue
        if body_ct_key in doc:
            sub = {
                key[len(prefix):]: value
                for key, value in doc.items()
                if key.startswith(prefix)
            }
            if not _decryptable(sub):
                return "skipped_local_only"
            encrypted = True
        elif not isinstance(doc.get(body_key), str):
            return "invalid_shape"
    if main == "skipped_local_only":
        return "skipped_local_only"
    return "migratable_shared" if encrypted else "already_plaintext"


def classify_frame(
    doc: dict | None, env_meta: dict | None, body_key: str | None
) -> str:
    carrier = doc if isinstance(doc, dict) else env_meta
    if not isinstance(carrier, dict):
        return "invalid_shape"
    if body_key:
        if carrier.get("body_object_format") == "plaintext_v1":
            return "already_plaintext"
        return (
            "migratable_shared" if _decryptable(carrier)
            else "skipped_local_only"
        )
    return _classify_single(carrier)


def inventory(user_id: str) -> Iterable[Item]:
    """Return stable, exact-user metadata inventory without decrypting bodies."""
    user_id = str(user_id)
    items: list[Item] = []
    with db.get_pool().connection() as conn:
        live_rows = conn.execute(
            "SELECT msg_id,seq,storage_generation,doc FROM chat_messages "
            "WHERE user_id=%s ORDER BY seq",
            (user_id,),
        ).fetchall()
        archive_rows = conn.execute(
            "SELECT source_seq,msg_id,storage_generation,doc "
            "FROM chat_message_archive WHERE user_id=%s ORDER BY source_seq",
            (user_id,),
        ).fetchall()
        memory_rows = conn.execute(
            "SELECT moment_id,occurred_at,doc FROM memory_moments "
            "WHERE user_id=%s ORDER BY occurred_at,moment_id",
            (user_id,),
        ).fetchall()
        world_rows = conn.execute(
            "SELECT entry_id,updated_at,doc FROM world_book_entries "
            "WHERE user_id=%s ORDER BY updated_at,entry_id",
            (user_id,),
        ).fetchall()
        identity_row = conn.execute(
            "SELECT doc FROM user_blobs WHERE user_id=%s AND kind='identity'",
            (user_id,),
        ).fetchone()
        frame_rows = conn.execute(
            "SELECT frame_id,ts,doc,env_meta,body_key FROM frame_envelopes "
            "WHERE user_id=%s ORDER BY ts,frame_id",
            (user_id,),
        ).fetchall()

    for msg_id, seq, generation, doc in live_rows:
        items.append(Item(
            "chat_live", str(msg_id), classify_chat(doc), doc,
            int(seq), int(generation),
            str(doc.get("body_key")) if isinstance(doc, dict) and doc.get("body_key") else None,
        ))
    for source_seq, msg_id, generation, doc in archive_rows:
        items.append(Item(
            "chat_archive", str(source_seq), classify_chat(doc), doc,
            str(msg_id), int(generation),
            str(doc.get("body_key")) if isinstance(doc, dict) and doc.get("body_key") else None,
        ))
    for moment_id, occurred_at, doc in memory_rows:
        items.append(Item(
            "memory", str(moment_id), _classify_single(doc), doc,
            str(occurred_at or ""),
        ))
    for entry_id, updated_at, doc in world_rows:
        items.append(Item(
            "world_book", str(entry_id), _classify_single(doc), doc,
            str(updated_at or ""),
        ))
    if identity_row is not None:
        doc = identity_row[0]
        items.append(Item("identity", "identity", _classify_single(doc), doc))
    for frame_id, ts, doc, env_meta, body_key in frame_rows:
        classification = classify_frame(doc, env_meta, body_key)
        items.append(Item(
            "frame", str(frame_id), classification, doc, float(ts), 0,
            str(body_key) if body_key else None, env_meta,
        ))
    return items


def _mark_requeue(user_id: str, item: Item) -> None:
    from tee_shadow import mirror

    table_for = {
        "chat_live": "chat_messages",
        "chat_archive": "chat_message_archive",
        "memory": "memory_moments",
        "world_book": "world_book_entries",
        "identity": "identity",
    }
    table = table_for.get(item.surface)
    if table:
        mirror.mark_pending(user_id, table, item.item_id, "requeue_plaintext_migration")


def cas_inline_doc(user_id: str, item: Item, new_doc: dict) -> bool:
    """Install one transformed doc iff preference and exact old doc still match."""
    if not isinstance(item.doc, dict):
        return False
    won = False
    with db.get_pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT doc->>'content_encryption' FROM users "
                    "WHERE user_id=%s FOR UPDATE",
                    (user_id,),
                )
                preference = cur.fetchone()
                if preference is None or str(preference[0] or "").strip().lower() != "off":
                    return False

                if item.surface == "chat_live":
                    cur.execute(
                        "UPDATE chat_messages SET doc=%s "
                        "WHERE user_id=%s AND msg_id=%s AND doc=%s RETURNING 1",
                        (Jsonb(new_doc), user_id, item.item_id, Jsonb(item.doc)),
                    )
                    won = cur.fetchone() is not None
                elif item.surface == "chat_archive":
                    # Archive UPDATE is intentionally forbidden by a trigger.
                    # Delete+insert occurs in one transaction after exact-doc CAS.
                    cur.execute(
                        "DELETE FROM chat_message_archive "
                        "WHERE user_id=%s AND source_seq=%s AND doc=%s "
                        "RETURNING msg_id,ts,storage_generation,clear_generation,cleared_at",
                        (user_id, int(item.item_id), Jsonb(item.doc)),
                    )
                    old = cur.fetchone()
                    if old is not None:
                        cur.execute(
                            "INSERT INTO chat_message_archive"
                            "(user_id,source_seq,msg_id,ts,doc,storage_generation,"
                            "clear_generation,cleared_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                            (
                                user_id,
                                int(item.item_id),
                                old[0],
                                old[1],
                                Jsonb(new_doc),
                                old[2],
                                old[3],
                                old[4],
                            ),
                        )
                        won = True
                elif item.surface == "memory":
                    cur.execute(
                        "UPDATE memory_moments SET doc=%s "
                        "WHERE user_id=%s AND moment_id=%s AND doc=%s RETURNING 1",
                        (Jsonb(new_doc), user_id, item.item_id, Jsonb(item.doc)),
                    )
                    won = cur.fetchone() is not None
                elif item.surface == "world_book":
                    cur.execute(
                        "UPDATE world_book_entries SET doc=%s "
                        "WHERE user_id=%s AND entry_id=%s AND doc=%s RETURNING 1",
                        (Jsonb(new_doc), user_id, item.item_id, Jsonb(item.doc)),
                    )
                    won = cur.fetchone() is not None
                elif item.surface == "identity":
                    cur.execute(
                        "UPDATE user_blobs SET doc=%s WHERE user_id=%s "
                        "AND kind='identity' AND doc=%s RETURNING 1",
                        (Jsonb(new_doc), user_id, Jsonb(item.doc)),
                    )
                    won = cur.fetchone() is not None
                else:
                    raise ValueError(f"surface is not inline-migratable: {item.surface}")
    if won:
        _mark_requeue(user_id, item)
    return won


def _transform_inline(item: Item, decrypt) -> dict:
    if not isinstance(item.doc, dict):
        raise ValueError("content doc is not an object")
    if item.surface in {"chat_live", "chat_archive"}:
        return transforms.plaintext_chat_doc(item.doc, decrypt)
    if item.surface == "memory":
        return transforms.plaintext_memory_doc(item.doc, decrypt)
    if item.surface == "world_book":
        return transforms.plaintext_world_book_doc(item.doc, decrypt)
    if item.surface == "identity":
        return transforms.plaintext_identity_doc(item.doc, decrypt)
    raise ValueError(f"surface is not inline-migratable: {item.surface}")


def _chat_has_encrypted_subcontent(doc: dict) -> bool:
    return any(doc.get(f"{prefix}body_ct") is not None
               for prefix in ("thinking_", "caption_"))


def _reload_chat_item(user_id: str, item: Item) -> Item | None:
    with db.get_pool().connection() as conn:
        if item.surface == "chat_live":
            row = conn.execute(
                "SELECT msg_id,seq,storage_generation,doc FROM chat_messages "
                "WHERE user_id=%s AND msg_id=%s",
                (user_id, item.item_id),
            ).fetchone()
            if row is None:
                return None
            msg_id, seq, generation, doc = row
            return Item(
                "chat_live", str(msg_id), classify_chat(doc), doc,
                int(seq), int(generation),
                str(doc.get("body_key")) if doc.get("body_key") else None,
            )
        row = conn.execute(
            "SELECT source_seq,msg_id,storage_generation,doc "
            "FROM chat_message_archive WHERE user_id=%s AND source_seq=%s",
            (user_id, int(item.item_id)),
        ).fetchone()
        if row is None:
            return None
        source_seq, msg_id, generation, doc = row
        return Item(
            "chat_archive", str(source_seq), classify_chat(doc), doc,
            str(msg_id), int(generation),
            str(doc.get("body_key")) if doc.get("body_key") else None,
        )


def _migrate_chat_pointer(user_id: str, item: Item, decrypt) -> str:
    doc = dict(item.doc or {})
    if doc.get("body_object_format") != "plaintext_v1":
        old_key = str(item.body_key or "")
        if not object_storage.chat_key_owned_by(old_key, user_id):
            return "failed_foreign_body_key"
        body_ct = object_storage.get_chat_body(old_key, user_id)
        if body_ct is None:
            return "failed_r2_object_missing"
        envelope = dict(doc)
        for key in (
            "body_key", "body_ct_len", "body_object_format",
            "body_size_bytes", "body_sha256", "body_ct",
        ):
            envelope.pop(key, None)
        envelope["body_ct"] = body_ct
        plaintext = decrypt(
            envelope,
            purpose=f"chat_r2_plaintext_migration:{item.item_id}",
        )
        promoted = db.migrate_chat_r2_pointer_to_plaintext(
            user_id,
            table="live" if item.surface == "chat_live" else "archive",
            item_id=item.item_id,
            old_body_key=old_key,
            storage_generation=item.storage_generation,
            plaintext=plaintext,
            content_type=str(doc.get("content_type") or ""),
        )
        if not promoted:
            return "cas_conflict"
        if not _chat_has_encrypted_subcontent(doc):
            return "migrated"
        item = _reload_chat_item(user_id, item)
        if item is None:
            return "cas_conflict"

    if item.classification == "already_plaintext":
        return "migrated"
    new_doc = transforms.plaintext_chat_doc(item.doc or {}, decrypt)
    return "migrated" if cas_inline_doc(user_id, item, new_doc) else "cas_conflict"


_FRAME_CRYPTO_FIELDS = {
    "v", "body_ct", "body_b64", "nonce", "K_user", "K_enclave",
    "enclave_pk_fpr", "content_pk_fpr", "body_object_format",
    "body_sha256", "body_size_bytes", "body_key",
}


def _migrate_frame(user_id: str, item: Item, decrypt) -> str:
    carrier = item.doc if isinstance(item.doc, dict) else item.env_meta
    if not isinstance(carrier, dict):
        return "failed_invalid_shape"
    envelope = dict(carrier)
    if item.body_key:
        if item.body_key == object_storage.frame_key(user_id, item.item_id):
            body_ct = object_storage.get_frame_body_strict(user_id, item.item_id)
        else:
            body_ct = object_storage.get_frame_body_by_key_strict(
                item.body_key, user_id,
            )
        if body_ct is None:
            return "failed_r2_object_missing"
        envelope["body_ct"] = body_ct
    plaintext = decrypt(
        envelope,
        purpose=f"frame_plaintext_migration:{item.item_id}",
    )
    semantic_meta = {
        key: value for key, value in carrier.items()
        if key not in _FRAME_CRYPTO_FIELDS
    }
    won = db.migrate_frame_to_plaintext(
        user_id,
        item.item_id,
        ts=float(item.sort_value or 0),
        old_doc=item.doc,
        old_env_meta=item.env_meta,
        old_body_key=item.body_key,
        plaintext=plaintext,
        semantic_meta=semantic_meta,
    )
    return "migrated" if won else "cas_conflict"


def migrate_item(user_id: str, item: Item, decrypt) -> str:
    if item.surface in {"chat_live", "chat_archive"} and item.body_key:
        return _migrate_chat_pointer(user_id, item, decrypt)
    if item.surface == "frame":
        return _migrate_frame(user_id, item, decrypt)
    new_doc = _transform_inline(item, decrypt)
    return "migrated" if cas_inline_doc(user_id, item, new_doc) else "cas_conflict"


def run(user_id: str, *, apply: bool = False) -> Result:
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ValueError("exact user_id is required")
    if apply and content_encryption_preference(user_id) != "off":
        raise PermissionError("content_encryption must be explicitly off")

    items = inventory(user_id)
    counts: Counter[str] = Counter()
    decrypt = None
    for item in items:
        if not apply or item.classification != "migratable_shared":
            counts[item.classification] += 1
            continue
        if decrypt is None:
            decrypt = make_decrypt(user_id)
        try:
            counts[migrate_item(user_id, item, decrypt)] += 1
        except Exception:  # noqa: BLE001 - report only redacted failure class
            counts["failed_transform_or_storage"] += 1
    failures = sum(
        count
        for status, count in counts.items()
        if status.startswith("failed_") or status == "cas_conflict"
    )
    return Result(
        apply=bool(apply),
        user_id=user_id,
        counts=dict(sorted(counts.items())),
        failures=failures,
    )
