"""Pluggable VFS backends for Runtime V2.

The protocol intentionally models files, not a host filesystem.  A backend may
store entries in PostgreSQL/R2, a CVM sandbox, or a remote sandbox provider.
No caller is allowed to infer that a model-visible path exists on the runner's
real filesystem.
"""
from __future__ import annotations

import hashlib
import posixpath
import threading
from dataclasses import dataclass, replace
from typing import Protocol, Sequence, runtime_checkable

from model_api_runtime.v2 import jobs_store


ARTIFACTS_ROOT = "/artifacts"
WORKSPACE_ROOT = "/workspace"
MEMORY_ROOT = "/memory"
SKILLS_ROOT = "/skills"
WORKING_MEMORY_PATH = "/memory/WORKING.md"

_ROOTS = (ARTIFACTS_ROOT, WORKSPACE_ROOT, MEMORY_ROOT, SKILLS_ROOT)
_READ_ONLY_ROOTS = (ARTIFACTS_ROOT, SKILLS_ROOT)
_MAX_PATH_CHARS = 512
_MAX_CONTENT_BYTES = 1_000_000
_MAX_WORKING_MEMORY_BYTES = 128_000


class WorkspaceError(RuntimeError):
    """Base class for stable, model-safe workspace failures."""


class WorkspaceNotFound(WorkspaceError):
    pass


class WorkspaceReadOnly(WorkspaceError):
    pass


class WorkspaceConflict(WorkspaceError):
    pass


class WorkspaceInvalidPath(WorkspaceError):
    pass


@dataclass(frozen=True)
class WorkspaceEntry:
    path: str
    kind: str
    revision: int
    content: str = ""
    mime_type: str = "text/plain"
    source_ref: str = ""

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@runtime_checkable
class WorkspaceBackend(Protocol):
    """Backend-neutral virtual workspace contract.

    Writes use optimistic concurrency.  ``expected_revision=0`` means create;
    replacing or deleting an existing entry requires the exact revision
    returned by ``read``/``list``.  This lets a future scheduler parallelize
    disjoint file writes while the backend remains the authoritative conflict
    detector.
    """

    def list(self, prefix: str = "/", *, recursive: bool = False,
             limit: int = 100) -> Sequence[WorkspaceEntry]: ...

    def read(self, path: str) -> WorkspaceEntry: ...

    def write(self, path: str, content: str, *, expected_revision: int,
              mime_type: str = "text/markdown") -> WorkspaceEntry: ...

    def delete(self, path: str, *, expected_revision: int) -> None: ...


class EnvelopeCodec(Protocol):
    """Encryption boundary injected into the durable backend."""

    def seal(self, path: str, plaintext: bytes) -> dict: ...
    def open(self, path: str, envelope: dict) -> bytes: ...


def canonical_path(raw: str, *, allow_root: bool = False) -> str:
    """Return one absolute POSIX VFS path and reject traversal/aliases."""
    value = str(raw or "").strip()
    if not value.startswith("/"):
        raise WorkspaceInvalidPath("workspace path must be absolute")
    if "\x00" in value or "\\" in value:
        raise WorkspaceInvalidPath("workspace path contains unsupported characters")
    if len(value) > _MAX_PATH_CHARS:
        raise WorkspaceInvalidPath("workspace path is too long")
    parts = value.split("/")
    if any(part in (".", "..") for part in parts):
        raise WorkspaceInvalidPath("workspace path traversal is not allowed")
    normalized = posixpath.normpath(value)
    if normalized != value:
        raise WorkspaceInvalidPath("workspace path must be canonical")
    if normalized == "/":
        if allow_root:
            return normalized
        raise WorkspaceInvalidPath("workspace root is not a file")
    if not any(normalized == root or normalized.startswith(root + "/") for root in _ROOTS):
        raise WorkspaceInvalidPath("path is outside the virtual workspace namespaces")
    return normalized


def _kind_for_path(path: str) -> str:
    if path.startswith(ARTIFACTS_ROOT + "/"):
        return "artifact"
    if path.startswith(WORKSPACE_ROOT + "/"):
        return "workspace"
    if path == WORKING_MEMORY_PATH:
        return "working_memory"
    if path.startswith(SKILLS_ROOT + "/"):
        return "skill"
    raise WorkspaceInvalidPath("only /memory/WORKING.md is a writable memory file")


def _validate_content(path: str, content: str) -> str:
    if not isinstance(content, str):
        raise WorkspaceError("workspace content must be UTF-8 text")
    size = len(content.encode("utf-8"))
    maximum = _MAX_WORKING_MEMORY_BYTES if path == WORKING_MEMORY_PATH else _MAX_CONTENT_BYTES
    if size > maximum:
        raise WorkspaceError("workspace content exceeds the entry size limit")
    return content


def _assert_model_writable(path: str) -> None:
    if any(path == root or path.startswith(root + "/") for root in _READ_ONLY_ROOTS):
        raise WorkspaceReadOnly("artifacts and skills are read-only")
    _kind_for_path(path)


class InMemoryWorkspaceBackend:
    """Thread-safe, host-filesystem-free backend for tests and local harnesses."""

    def __init__(self) -> None:
        self._entries: dict[str, WorkspaceEntry] = {}
        self._lock = threading.RLock()

    def list(self, prefix: str = "/", *, recursive: bool = False,
             limit: int = 100) -> Sequence[WorkspaceEntry]:
        prefix = canonical_path(prefix, allow_root=True)
        maximum = max(1, min(int(limit), 500))
        with self._lock:
            rows = []
            for path in sorted(self._entries):
                if prefix != "/" and not (path == prefix or path.startswith(prefix + "/")):
                    continue
                if not recursive:
                    relative = path[1:] if prefix == "/" else path[len(prefix):].lstrip("/")
                    if "/" in relative:
                        continue
                rows.append(replace(self._entries[path], content=""))
                if len(rows) >= maximum:
                    break
            return rows

    def read(self, path: str) -> WorkspaceEntry:
        path = canonical_path(path)
        with self._lock:
            entry = self._entries.get(path)
            if entry is None:
                raise WorkspaceNotFound("workspace entry not found")
            return entry

    def write(self, path: str, content: str, *, expected_revision: int,
              mime_type: str = "text/markdown") -> WorkspaceEntry:
        path = canonical_path(path)
        _assert_model_writable(path)
        return self._write_internal(
            path, content, expected_revision=expected_revision,
            mime_type=mime_type, kind=_kind_for_path(path), source_ref="",
        )

    def delete(self, path: str, *, expected_revision: int) -> None:
        path = canonical_path(path)
        _assert_model_writable(path)
        with self._lock:
            current = self._entries.get(path)
            if current is None:
                raise WorkspaceNotFound("workspace entry not found")
            if current.revision != expected_revision:
                raise WorkspaceConflict("workspace revision conflict")
            del self._entries[path]

    def put_read_only(self, path: str, content: str, *, kind: str,
                      source_ref: str = "", mime_type: str = "text/plain",
                      expected_revision: int = 0) -> WorkspaceEntry:
        """Trusted ingestion seam for artifact text views and skills."""
        path = canonical_path(path)
        if kind not in {"artifact", "skill"} or _kind_for_path(path) != kind:
            raise WorkspaceInvalidPath("read-only entry kind does not match its namespace")
        return self._write_internal(
            path, content, expected_revision=expected_revision, mime_type=mime_type,
            kind=kind, source_ref=source_ref,
        )

    def _write_internal(self, path: str, content: str, *, expected_revision: int,
                        mime_type: str, kind: str, source_ref: str) -> WorkspaceEntry:
        content = _validate_content(path, content)
        if type(expected_revision) is not int or expected_revision < 0:
            raise WorkspaceConflict("expected_revision must be a non-negative integer")
        with self._lock:
            current = self._entries.get(path)
            actual = current.revision if current else 0
            if actual != expected_revision:
                raise WorkspaceConflict("workspace revision conflict")
            entry = WorkspaceEntry(
                path=path, kind=kind, revision=actual + 1, content=content,
                mime_type=str(mime_type or "text/plain")[:200],
                source_ref=str(source_ref or "")[:512],
            )
            self._entries[path] = entry
            return entry


class PostgresWorkspaceBackend:
    """Encrypted PostgreSQL-backed workspace.

    ``jobs_store`` sees only opaque envelopes and routing metadata.  Plaintext
    exists briefly in the trusted runner while a tool executes and inside the
    enclave while an envelope is opened.
    """

    def __init__(self, user_id: str, codec: EnvelopeCodec) -> None:
        self.user_id = str(user_id)
        self.codec = codec

    @staticmethod
    def _meta(row: dict) -> WorkspaceEntry:
        return WorkspaceEntry(
            path=str(row["path"]), kind=str(row["kind"]),
            revision=int(row["revision"]), mime_type=str(row.get("mime_type") or "text/plain"),
            source_ref=str(row.get("source_ref") or ""),
        )

    def list(self, prefix: str = "/", *, recursive: bool = False,
             limit: int = 100) -> Sequence[WorkspaceEntry]:
        prefix = canonical_path(prefix, allow_root=True)
        rows = jobs_store.list_workspace_entries(
            self.user_id, prefix=prefix, recursive=bool(recursive), limit=limit,
        )
        return [self._meta(row) for row in rows]

    def read(self, path: str) -> WorkspaceEntry:
        path = canonical_path(path)
        row = jobs_store.get_workspace_entry(self.user_id, path)
        if row is None:
            raise WorkspaceNotFound("workspace entry not found")
        plaintext = self.codec.open(path, dict(row["content_envelope"]))
        try:
            content = plaintext.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WorkspaceError("workspace entry is not UTF-8 text") from exc
        return replace(self._meta(row), content=content)

    def write(self, path: str, content: str, *, expected_revision: int,
              mime_type: str = "text/markdown") -> WorkspaceEntry:
        path = canonical_path(path)
        _assert_model_writable(path)
        return self._write_internal(
            path, content, expected_revision=expected_revision,
            mime_type=mime_type, kind=_kind_for_path(path), source_ref="",
        )

    def delete(self, path: str, *, expected_revision: int) -> None:
        path = canonical_path(path)
        _assert_model_writable(path)
        if not jobs_store.delete_workspace_entry_cas(
            self.user_id, path, expected_revision=expected_revision,
        ):
            raise WorkspaceConflict("workspace revision conflict or entry missing")

    def put_read_only(self, path: str, content: str, *, kind: str,
                      source_ref: str = "", mime_type: str = "text/plain",
                      expected_revision: int = 0) -> WorkspaceEntry:
        """Trusted ingestion seam; never reachable through a model write tool."""
        path = canonical_path(path)
        if kind not in {"artifact", "skill"} or _kind_for_path(path) != kind:
            raise WorkspaceInvalidPath("read-only entry kind does not match its namespace")
        return self._write_internal(
            path, content, expected_revision=expected_revision,
            mime_type=mime_type, kind=kind, source_ref=source_ref,
        )

    def _write_internal(self, path: str, content: str, *, expected_revision: int,
                        mime_type: str, kind: str, source_ref: str) -> WorkspaceEntry:
        content = _validate_content(path, content)
        if type(expected_revision) is not int or expected_revision < 0:
            raise WorkspaceConflict("expected_revision must be a non-negative integer")
        envelope = self.codec.seal(path, content.encode("utf-8"))
        row = jobs_store.put_workspace_entry_cas(
            self.user_id,
            path,
            kind=kind,
            content_envelope=envelope,
            mime_type=str(mime_type or "text/plain")[:200],
            source_ref=str(source_ref or "")[:512],
            expected_revision=expected_revision,
        )
        if row is None:
            raise WorkspaceConflict("workspace revision conflict")
        return replace(self._meta(row), content=content)
