"""Streaming Markdown archive from one repeatable-read durable snapshot.

Conversation summaries are bounded context-compaction caches, not user profiles.
The long-term MEMORY/STYLE/user profile is exported separately.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
import posixpath
import zipfile
from datetime import datetime, timezone
from collections import Counter

from psycopg.rows import dict_row

import db
from chat.service import NON_CONVERSATION_USER_SOURCES
from memory import service as memory_service
from core import envelope as core_envelope


def _rows(conn, query: str, params: tuple, deadline: float):
    with conn.cursor(name="export_" + uuid.uuid4().hex,
                     row_factory=dict_row) as cur:
        cur.execute(query, params)
        while batch := cur.fetchmany(200):
            for row in batch:
                if time.monotonic() >= deadline:
                    raise TimeoutError("export_read_deadline")
                yield row


class DevicePrivate(Exception):
    """An explicitly device-only sealed record has no server decryption key."""


def _check_readable(doc: dict, user_id: str) -> None:
    owner = doc.get("owner_user_id")
    if owner not in (None, "", user_id):
        raise ValueError("export_owner_mismatch")
    sealed = bool(doc.get("body_ct")) or (
        bool(doc.get("body_key")) and
        (doc.get("body_object_format") or "sealed_v1") == "sealed_v1"
    )
    if doc.get("visibility") == "local_only" and sealed and not doc.get("K_enclave"):
        raise DevicePrivate()


def _text(doc: dict, user_id: str, token: str) -> str:
    _check_readable(doc, user_id)
    raw = core_envelope.read_envelope_body(
        doc, None, purpose="user_export", caller_user_id=user_id, runtime_token=token)
    return raw.decode("utf-8")  # Never silently claim an unreadable record was exported.


def _body(row: dict, user_id: str, token: str) -> str:
    _check_readable(row, user_id)
    if row.get("body_key"):
        # No image/binary objects reach here. Preserve the validated ownership/
        # hash/format contract rather than bypassing it with a raw S3 get.
        body = db.hydrate_chat_file_body(user_id, row)
        return _text(body, user_id, token)
    return _text(row, user_id, token)


def _markdown(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n\n".join(f"## {key}\n\n{_markdown(item)}"
                           for key, item in value.items())
    if isinstance(value, list):
        return "\n\n".join(_markdown(item) for item in value)
    return str(value) if value is not None else ""


def _present(text: str) -> str:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return text
    return _markdown(value) if isinstance(value, (dict, list)) else text


def _safe_workspace_path(path: str) -> str | None:
    if not isinstance(path, str) or "\\" in path or "\x00" in path:
        return None
    if posixpath.normpath(path) != path or not path.startswith("/"):
        return None
    if path == "/memory/WORKING.md" or (
        path.startswith("/workspace/") and path.lower().endswith(".md")
    ):
        return "workspace/" + path.lstrip("/")
    return None


def build(user_id: str, token: str, destination) -> None:
    with db.get_pool().connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        deadline = time.monotonic() + 1200

        def rows(query, params):
            return _rows(conn, query, params, deadline)

        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED,
                             allowZip64=True) as archive:
            skipped = Counter()

            def read(doc, section, *, hydrate=False):
                try:
                    return (_body(doc, user_id, token) if hydrate
                            else _text(doc, user_id, token))
                except DevicePrivate:
                    skipped[section] += 1
                    return None

            chat = None
            day = None
            try:
                for item in rows(
                        "SELECT ts,doc FROM chat_messages WHERE user_id=%s "
                        "ORDER BY ts,seq", (user_id,)):
                    doc = item["doc"]
                    if (doc.get("source") in NON_CONVERSATION_USER_SOURCES
                            or doc.get("role") == "system"):
                        continue
                    kind = doc.get("content_type", "text")
                    if kind == "file":
                        mime = str(doc.get("file_mime") or "").split(";")[0].lower()
                        filename = str(doc.get("file_name") or "").lower()
                        if not (mime.startswith("text/") and
                                filename.endswith((".md", ".txt"))):
                            continue
                    elif kind not in {"text", "image"}:
                        continue
                    if kind == "image":
                        text = "[图片 / Image]"
                        caption = core_envelope.caption_envelope_from_row(doc)
                        if caption:
                            caption["visibility"] = doc.get(
                                "caption_visibility", doc.get("visibility", "shared"))
                            value = read(caption, "Image captions")
                            if value is not None:
                                text += "\n\n" + value
                    else:
                        text = read(doc, "Chat messages", hydrate=True)
                        if text is None:
                            continue
                    stamp = datetime.fromtimestamp(float(item["ts"]), timezone.utc)
                    next_day = stamp.strftime("%Y-%m-%d")
                    if next_day != day:
                        if chat:
                            chat.close()
                        day = next_day
                        chat = archive.open(f"chat/{day}.md", "w", force_zip64=True)
                    role = doc.get("role")
                    speaker = ("我 / Me" if role in {"user", "human"} else
                               "TA / Agent" if role in {"openclaw", "agent", "assistant"}
                               else "对话 / Conversation")
                    heading = f"## {stamp.isoformat()} — {speaker}\n\n"
                    chat.write((heading + text + "\n\n").encode("utf-8"))
            finally:
                if chat:
                    chat.close()
            with archive.open("memories.md", "w", force_zip64=True) as output:
                output.write(b"# Memories\n\n")
                for item in rows( "SELECT doc FROM memory_moments "
                                  "WHERE user_id=%s ORDER BY occurred_at,moment_id", (user_id,)):
                    doc = item["doc"]
                    value = read(doc, "Memory cards")
                    if value is None:
                        continue
                    archived = memory_service._memory_is_archived(doc)
                    label = " — 已归档 / Archived" if archived else ""
                    text = _present(value)
                    output.write((f"## {doc.get('occurred_at', '')}{label}\n\n"
                                  f"{text}\n\n").encode("utf-8"))
            for item in rows( "SELECT kind,doc FROM user_blobs "
                              "WHERE user_id=%s AND kind=ANY(%s) ORDER BY kind",
                              (user_id, ["identity", "genesis_persona", "v2_agent_profile"])):
                kind, doc = item["kind"], item["doc"]
                if kind == "identity":
                    value = read(doc, "Identity")
                    if value is None:
                        continue
                    text = _present(value)
                    name = "identity.md"
                elif kind == "genesis_persona":
                    value = read(doc["content_envelope"], "Persona")
                    if value is None:
                        continue
                    text = _present(value)
                    name = "persona.md"
                else:
                    # Old plaintext sections and current encrypted content records.
                    fields = []
                    for key in ("memory", "style", "user"):
                        section = doc.get(key)
                        if section is None:
                            continue
                        if not isinstance(section, dict) or not isinstance(
                            section.get("envelope"), dict
                        ):
                            raise ValueError("export_profile_shape_invalid")
                        value = read(section["envelope"], "Profile fields")
                        if value is None:
                            continue
                        fields.append(f"## {key}\n\n{value}")
                    text, name = "\n\n".join(fields), "profile.md"
                archive.writestr(name, text)
            for item in rows( "SELECT entry_id,doc FROM world_book_entries "
                              "WHERE user_id=%s ORDER BY entry_id", (user_id,)):
                # Stable safe name, not a user-authored filename with traversal.
                name = hashlib.sha256(str(item["entry_id"]).encode()).hexdigest()[:24]
                value = read(item["doc"], "World book entries")
                if value is not None:
                    archive.writestr(f"world-book/{name}.md", _present(value))
            for item in rows( "SELECT path,content_envelope FROM v2_workspace_entries "
                              "WHERE user_id=%s ORDER BY path", (user_id,)):
                target = _safe_workspace_path(item["path"])
                if target:
                    value = read(item["content_envelope"], "Workspace files")
                    if value is not None:
                        archive.writestr(target, value)
            labels = ("Chat messages", "Image captions", "Memory cards", "Identity",
                      "Persona", "Profile fields", "World book entries", "Workspace files")
            archive.writestr("README.md", (
                "# IO data export\n\n"
                f"Exported at {datetime.now(timezone.utc).isoformat()}.\n\n"
                "Chat timestamps and date files use UTC. Speakers: 我 / Me and TA / Agent.\n\n"
                "Contains current chat, all memory cards (archived cards individually marked), "
                "identity, persona, world book, profile and owned Markdown workspace files.\n\n"
                "Excluded: cleared chat, internal messages, image bytes/frames, binary files, "
                "Canvas, skills, artifacts and conversation-compaction caches. "
                "Image messages retain only an Image placeholder and available captions.\n\n"
                "## Private to your device — not included\n\n"
                "Encrypted local_only records without a server key cannot be decrypted here. "
                "Plaintext local_only records are included. Unexpected decrypt errors fail "
                "the export instead of being counted as omissions. Skipped counts:\n\n"
                + "".join(f"- {label}: {skipped[label]}\n" for label in labels)
            ))
