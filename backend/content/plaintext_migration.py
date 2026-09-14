"""Single-user legacy content-envelope to plaintext migration.

The public entry point is deliberately small and fail closed.  Inventory is
read-only; apply requires an explicit per-user ``off`` preference and the CLI's
independent write gates.  Surface-specific inventory and CAS writers live in
this module so the command can be exercised without exposing content values.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable

import db


APPLY_ENV = "FEEDLING_ENABLE_PLAINTEXT_CONTENT_MIGRATION"


@dataclass(frozen=True)
class Item:
    surface: str
    item_id: str
    classification: str
    doc: dict | None = None
    sort_value: str | int | float | None = None
    storage_generation: int = 0
    body_key: str | None = None
    env_meta: dict | None = None


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


def run(user_id: str, *, apply: bool = False) -> Result:
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ValueError("exact user_id is required")
    if apply and content_encryption_preference(user_id) != "off":
        raise PermissionError("content_encryption must be explicitly off")

    items = inventory(user_id)
    counts = Counter(item.classification for item in items)
    return Result(
        apply=bool(apply),
        user_id=user_id,
        counts=dict(sorted(counts.items())),
    )
