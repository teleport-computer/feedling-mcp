"""Artifact-to-workspace ingestion through the mandatory sandbox boundary."""
from __future__ import annotations

import hashlib
from typing import Protocol

from workspace.backends import (
    ARTIFACTS_ROOT,
    WorkspaceConflict,
    WorkspaceEntry,
    WorkspaceNotFound,
)
from workspace.sandbox import LazySandbox, SandboxRequiredOperation


class ArtifactTextViewBackend(Protocol):
    def read(self, path: str) -> WorkspaceEntry: ...
    def put_read_only(self, path: str, content: str, *, kind: str,
                      source_ref: str = "", mime_type: str = "text/plain",
                      expected_revision: int = 0) -> WorkspaceEntry: ...


def artifact_text_view_path(source_ref: str, filename: str) -> str:
    """Stable source identity even when filename metadata is stale or missing."""
    # ``filename`` remains in the API because callers display it separately,
    # but it is not part of cache identity. Otherwise a stale UserStore row can
    # miss an existing text view and reacquire a billable sandbox every turn.
    _ = filename
    digest = hashlib.sha256(str(source_ref).encode("utf-8")).hexdigest()[:16]
    return f"{ARTIFACTS_ROOT}/{digest}.txt"


class ArtifactWorkspace:
    """Build durable read-only text views without touching the runner filesystem."""

    def __init__(self, backend: ArtifactTextViewBackend, sandbox: LazySandbox) -> None:
        self.backend = backend
        self.sandbox = sandbox

    def read_text_view(self, path: str) -> WorkspaceEntry:
        """A stored text view is ordinary encrypted VFS data: no sandbox cold start."""
        return self.backend.read(path)

    def ingest(
        self,
        *,
        source_ref: str,
        filename: str,
        mime_type: str,
        data: bytes,
    ) -> WorkspaceEntry:
        """Materialization and parsing always acquire the sandbox lazily."""
        self.sandbox.ensure(SandboxRequiredOperation.MATERIALIZE_ARTIFACT)
        physical_path = self.sandbox.materialize_artifact(
            name=filename,
            data=data,
            mime_type=mime_type,
        )
        text = self.sandbox.extract_untrusted_text(
            path=physical_path,
            mime_type=mime_type,
        )
        path = artifact_text_view_path(source_ref, filename)
        try:
            current = self.backend.read(path)
            expected_revision = current.revision
        except WorkspaceNotFound:
            expected_revision = 0
        try:
            return self.backend.put_read_only(
                path,
                text,
                kind="artifact",
                source_ref=source_ref,
                mime_type="text/plain",
                expected_revision=expected_revision,
            )
        except WorkspaceConflict:
            # Concurrent turn materialized the same immutable source first.
            return self.backend.read(path)
