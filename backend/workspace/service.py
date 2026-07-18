"""Production assembly and model-facing operations for the V2 workspace."""
from __future__ import annotations

import hashlib
from dataclasses import asdict

from core import enclave as core_enclave
from core import envelope as core_envelope
from workspace.backends import (
    WORKING_MEMORY_PATH,
    PostgresWorkspaceBackend,
    WorkspaceBackend,
    WorkspaceEntry,
)


_WORKING_MEMORY_TEMPLATE = """# Working memory

This file is editable task/agent working state, separate from the user's Memory Garden.
Keep durable plans, project state, and continuation notes here. Do not copy secrets.
"""


class SharedEnvelopeCodec:
    """V1 shared-envelope codec bound to one user and enclave token."""

    def __init__(self, store, *, runtime_token: str) -> None:
        self.store = store
        self.runtime_token = str(runtime_token or "")

    def _item_id(self, path: str) -> str:
        material = f"feedling:v2:workspace:v1\0{self.store.user_id}\0{path}"
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]

    def seal(self, path: str, plaintext: bytes) -> dict:
        envelope, error = core_envelope._build_shared_envelope_for_store(
            self.store, plaintext, item_id=self._item_id(path),
        )
        if envelope is None:
            raise RuntimeError(error or "workspace envelope build failed")
        return envelope

    def open(self, path: str, envelope: dict) -> bytes:
        expected_id = self._item_id(path)
        if str(envelope.get("id") or "") != expected_id:
            raise RuntimeError("workspace envelope id mismatch")
        if str(envelope.get("owner_user_id") or "") != str(self.store.user_id):
            raise RuntimeError("workspace envelope owner mismatch")
        return core_enclave._decrypt_envelope_via_enclave(
            envelope,
            None,
            purpose="v2_workspace_read",
            runtime_token=self.runtime_token,
        )


def production_backend(store, *, runtime_token: str) -> PostgresWorkspaceBackend:
    return PostgresWorkspaceBackend(
        str(store.user_id), SharedEnvelopeCodec(store, runtime_token=runtime_token),
    )


def ensure_working_memory(backend: WorkspaceBackend) -> WorkspaceEntry:
    """Create the default Markdown file once; never overwrite an existing one."""
    try:
        return backend.read(WORKING_MEMORY_PATH)
    except Exception as exc:
        # Import locally to avoid making callers depend on concrete backends.
        from workspace.backends import WorkspaceConflict, WorkspaceNotFound

        if not isinstance(exc, WorkspaceNotFound):
            raise
        try:
            return backend.write(
                WORKING_MEMORY_PATH,
                _WORKING_MEMORY_TEMPLATE,
                expected_revision=0,
                mime_type="text/markdown",
            )
        except WorkspaceConflict:
            # Another worker won the create race.
            return backend.read(WORKING_MEMORY_PATH)


def list_entries(backend: WorkspaceBackend, *, path: str = "/",
                 recursive: bool = False, limit: int = 100) -> dict:
    entries = backend.list(path, recursive=recursive, limit=limit)
    return {
        "entries": [
            {k: v for k, v in asdict(entry).items() if k != "content"}
            for entry in entries
        ]
    }


def read_text(backend: WorkspaceBackend, *, path: str,
              start_line: int = 1, line_count: int = 200) -> dict:
    entry = backend.read(path)
    start = max(1, int(start_line))
    count = max(1, min(int(line_count), 500))
    lines = entry.content.splitlines()
    selected = lines[start - 1:start - 1 + count]
    return {
        "path": entry.path,
        "kind": entry.kind,
        "revision": entry.revision,
        "mime_type": entry.mime_type,
        "start_line": start,
        "line_count": len(selected),
        "total_lines": len(lines),
        "content": "\n".join(selected),
    }


def write_text(backend: WorkspaceBackend, *, path: str, content: str,
               expected_revision: int) -> dict:
    entry = backend.write(
        path, content, expected_revision=expected_revision,
        mime_type="text/markdown",
    )
    return {
        "path": entry.path,
        "kind": entry.kind,
        "revision": entry.revision,
        "content_sha256": entry.content_sha256,
    }


def delete_entry(backend: WorkspaceBackend, *, path: str,
                 expected_revision: int) -> dict:
    backend.delete(path, expected_revision=expected_revision)
    return {"path": path, "deleted": True}

