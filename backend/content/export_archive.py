"""Streaming Markdown and import JSON from one repeatable-read durable snapshot.

Conversation summaries are bounded context-compaction caches, not user profiles.
The long-term MEMORY/STYLE/user profile is exported separately.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
import posixpath
import re
import shutil
import tempfile
import zipfile
from datetime import datetime, timezone
from collections import Counter

from psycopg.rows import dict_row
from memgarden import timestamps as memory_timestamps

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


def _chat_speaker(role: str) -> tuple[str, str]:
    if role in {"user", "human"}:
        return "用户", "user"
    if role in {"openclaw", "agent", "assistant"}:
        return "助手", "assistant"
    raise ValueError("export_chat_role_invalid")


# Compatibility superset of Rokku's generic speaker grammar (7845015f).
# Leading indentation is included conservatively; ordinary prose stays literal.
_SPEAKER_LINE = re.compile(
    r"^\s*(?:\[[^\]\r\n]*\]\s*)?(?:#{1,6}\s*|\*\*)?"
    r"(?:User|Human|Assistant|Bot|ChatGPT|Claude|你|用户|助手)"
    r"(?:\*\*)?\s*(?::|：|$)", re.IGNORECASE | re.MULTILINE,
)


def _chat_markdown(stamp: datetime, role: str, text: str) -> str:
    """Keep plain messages unchanged; protect risky Markdown continuations."""
    speaker, _ = _chat_speaker(role)
    continuation = text.partition("\n")[2]
    if "```" in text or "~~~" in text or _SPEAKER_LINE.search(continuation):
        longest = max((len(match[0]) for match in re.finditer(r"`+", text)), default=2)
        fence = "`" * max(3, longest + 1)
        text = f"{fence}text\n{text}\n{fence}"
    return f"[{stamp.isoformat()}] {speaker}: {text}\n\n"


class _ImportJSON:
    """Spool the same selected plaintext rows; never retain the whole archive."""

    def __init__(self, conversations, memories):
        self.conversations, self.memories = conversations, memories
        self.day = None
        self.first_message = True
        self.conversations.write(b"[")

    def chat(self, stamp, role, message_id, text):
        day = stamp.strftime("%Y-%m-%d")
        if day != self.day:
            if self.day is not None:
                self.conversations.write(b"]},")
            header = {"id": "io-chat:" + day, "title": "IO chat " + day}
            self.conversations.write(json.dumps(header, ensure_ascii=False).encode()[:-1])
            self.conversations.write(b',"messages":[')
            self.day, self.first_message = day, True
        if not self.first_message:
            self.conversations.write(b",")
        self.first_message = False
        value = {"id": message_id, "role": _chat_speaker(role)[1],
                 "content": text, "created_at": stamp.isoformat()}
        self.conversations.write(json.dumps(value, ensure_ascii=False).encode())

    def memory(self, path, text, updated_at=None):
        if not text.strip():
            return
        # One memory_file per shard avoids Rokku's root=True whole-document
        # construction growing with the total number of cards/workspace files.
        value = {"path": path, "content": text}
        # Match IO's historical UTC read convention (memgarden.timestamps), also
        # used by Rokku for naive/date-only values. Never invent a missing date.
        stamp = memory_timestamps.parse_ts(updated_at)
        if stamp is not None:
            value["updated_at"] = stamp.isoformat()
        self.memories.write(json.dumps(value, ensure_ascii=False).encode() + b"\n")

    def finish(self, archive):
        self.conversations.write(b"]}]" if self.day is not None else b"]")
        self.conversations.seek(0)
        with archive.open("conversations.json", "w", force_zip64=True) as output:
            shutil.copyfileobj(self.conversations, output)
        self.memories.seek(0)
        for line in self.memories:
            item = json.loads(line)
            name = hashlib.sha256(item["path"].encode()).hexdigest()
            # A stable non-personal namespace; paths and content keep unchanged
            # reimports stable without exposing the IO account identifier.
            value = {"account_uuid": "io-export", "memory_files": [item]}
            archive.writestr(f"memories/{name}.json", json.dumps(value, ensure_ascii=False))


def build(user_id: str, token: str, destination) -> None:
    with db.get_pool().connection() as conn, conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        deadline = time.monotonic() + 1200

        def rows(query, params):
            return _rows(conn, query, params, deadline)

        with (tempfile.TemporaryFile() as conversations,
              tempfile.TemporaryFile() as memories,
              zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED,
                              allowZip64=True) as archive):
            migration = _ImportJSON(conversations, memories)
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
                        "SELECT ts,doc,msg_id FROM chat_messages WHERE user_id=%s "
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
                    chat.write(_chat_markdown(stamp, doc.get("role"), text).encode("utf-8"))
                    migration.chat(stamp, doc.get("role"), item["msg_id"], text)
            finally:
                if chat:
                    chat.close()
            with archive.open("memories.md", "w", force_zip64=True) as output:
                output.write(b"# Memories\n\n")
                for item in rows( "SELECT moment_id,doc FROM memory_moments "
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
                    mid = str(item["moment_id"])
                    migration.memory("memory-cards/" + mid + ".md",
                                     ("已归档 / Archived\n\n" if archived else "") + text,
                                     doc.get("occurred_at"))
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
                migration.memory(name, text, doc.get("updated_at"))
            for item in rows( "SELECT entry_id,doc FROM world_book_entries "
                              "WHERE user_id=%s ORDER BY entry_id", (user_id,)):
                # Stable safe name, not a user-authored filename with traversal.
                name = hashlib.sha256(str(item["entry_id"]).encode()).hexdigest()[:24]
                value = read(item["doc"], "World book entries")
                if value is not None:
                    path = f"world-book/{name}.md"
                    archive.writestr(path, _present(value))
                    migration.memory(path, _present(value), item["doc"].get("updated_at"))
            for item in rows( "SELECT path,content_envelope,updated_at FROM v2_workspace_entries "
                              "WHERE user_id=%s ORDER BY path", (user_id,)):
                target = _safe_workspace_path(item["path"])
                if target:
                    value = read(item["content_envelope"], "Workspace files")
                    if value is not None:
                        archive.writestr(target, value)
                        migration.memory(target, value, item["updated_at"])
            migration.finish(archive)
            labels = ("Chat messages", "Image captions", "Memory cards", "Identity",
                      "Persona", "Profile fields", "World book entries", "Workspace files")
            archive.writestr("README.md", (
                "# IO data export\n\n"
                f"Exported at {datetime.now(timezone.utc).isoformat()}.\n\n"
                "Chat timestamps and date files use UTC (+00:00). Speakers: 用户 (you) and 助手 (assistant).\n\n"
                "Chat Markdown uses bracketed ISO timestamps and readable speakers. "
                "Ordinary message bodies are unchanged. Risky speaker-like continuations "
                "and embedded code fences use a variable-length literal code block.\n\n"
                "conversations.json and memories/*.json are for Rokku history import. "
                "They use the same selected plaintext as the Markdown, without Markdown "
                "chat fences. JSON takes precedence, so Markdown is not imported twice. "
                "Memories, identity, persona, profile, world book and owned workspace "
                "Markdown become imported memory summaries, not restored Rokku settings "
                "or workspace files. Archived cards remain explicitly marked. "
                "Rokku may label this mixed archive as Claude because it recognizes the "
                "memory_files structure; this label does not identify the source product. "
                "The stable io-export namespace and stable paths aid repeated imports; "
                "content changes remain separate records. Missing original dates produce "
                "timestamp_missing warnings. Rokku's own import size/resource limits apply.\n\n"
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
