"""In-process ``local`` sandbox provider for Runtime V2 document reading.

Parses untrusted document bytes *inside the worker process* using the
pure-Python extractors in ``hosted.file_text`` (docx/xlsx via stdlib zip+XML,
pdf via pypdf, utf-8 text verbatim). Selected only when
``FEEDLING_V2_SANDBOX_PROVIDER=local``.

SECURITY: this deliberately reverses the 6ddc9676 isolation gate — it parses
attacker-controlled bytes in the main worker rather than an isolated sandbox.
It keeps plaintext inside the CVM (no egress) and is acceptable only because all
three parsers are pure-Python with no native RCE surface. The guards below bound
the remaining DoS surface:

  * ``materialize`` rejects inputs over ``FEEDLING_V2_LOCAL_ARTIFACT_MAX_BYTES``
    (default 25 MiB, matching the iOS upload ceiling).
  * ``extract_text`` refuses OOXML (docx/xlsx are zip containers) whose
    *uncompressed* size exceeds ``FEEDLING_V2_LOCAL_MAX_UNCOMPRESSED_BYTES``
    (default 200 MiB) — a zip-bomb guard — returning empty text so the turn
    keeps its ``[file: name]`` marker.
  * page/row/sheet/char ceilings live in ``file_text`` itself.

Extraction never raises (``file_text`` is best-effort); a failure returns empty
text so one bad file cannot kill a turn. Shell/code execution is refused
outright — this provider only reads text.
"""
from __future__ import annotations

import io
import logging
import os
import zipfile

from workspace.sandbox import SandboxUnavailable


log = logging.getLogger("feedling.workspace.local")

_DEFAULT_ARTIFACT_MAX_BYTES = 25 * 1024 * 1024
_DEFAULT_MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024


def _bounded_int(name: str, default: int, *, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise SandboxUnavailable(f"invalid {name}") from exc
    if value <= 0 or value > maximum:
        raise SandboxUnavailable(f"invalid {name}")
    return value


class _LocalSandboxSession:
    """In-process text extraction; virtual paths, no host FS, no execution."""

    def __init__(self, *, max_bytes: int, max_uncompressed: int) -> None:
        self._max_bytes = max_bytes
        self._max_uncompressed = max_uncompressed
        # path -> (name, data, mime). The name is retained here so extract_text
        # (which only receives path+mime) can still dispatch on the extension.
        self._files: dict[str, tuple[str, bytes, str]] = {}
        self._seq = 0
        self._closed = False

    def _assert_open(self) -> None:
        if self._closed:
            raise SandboxUnavailable("local sandbox session is closed")

    def materialize(self, *, name: str, data: bytes, mime_type: str) -> str:
        self._assert_open()
        raw = bytes(data)
        if len(raw) > self._max_bytes:
            raise SandboxUnavailable("artifact exceeds local materialization limit")
        self._seq += 1
        path = f"/local/artifacts/{self._seq:04d}"
        self._files[path] = (str(name or ""), raw, str(mime_type or ""))
        return path

    def _zip_within_limit(self, data: bytes) -> bool:
        # docx/xlsx are zip containers; a tiny archive can expand to gigabytes.
        # Non-zip inputs (pdf/text) rely on file_text's page/char ceilings.
        if data[:2] != b"PK":
            return True
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                total = sum(max(0, info.file_size) for info in z.infolist())
        except Exception:  # noqa: BLE001 — not a valid zip; file_text yields none
            return True
        return total <= self._max_uncompressed

    def extract_text(self, *, path: str, mime_type: str) -> str:
        self._assert_open()
        record = self._files.get(path)
        if record is None:
            raise SandboxUnavailable("local artifact not found")
        name, data, stored_mime = record
        if not self._zip_within_limit(data):
            log.warning(
                "[workspace.local] artifact exceeds decompression limit; skipping name=%r",
                name[:120],
            )
            return ""
        mime = str(mime_type or stored_mime or "")
        try:
            # Lazy import keeps the workspace package free of a module-level
            # dependency on the legacy hosted.file_text extractors.
            from hosted.file_text import extract_file_text

            result = extract_file_text(data, name=name, mime=mime)
        except Exception as exc:  # noqa: BLE001 — never let one file kill the turn
            log.warning("[workspace.local] extraction failed type=%s", type(exc).__name__)
            return ""
        return result.text or ""

    def run_shell(self, command: str) -> dict:
        raise SandboxUnavailable("local sandbox does not execute shell commands")

    def run_code(self, *, language: str, source: str) -> dict:
        raise SandboxUnavailable("local sandbox does not execute code")

    def close(self) -> None:
        self._closed = True
        self._files.clear()


class LocalSandboxProvider:
    """In-process reader; ``FEEDLING_V2_SANDBOX_PROVIDER=local`` selects it."""

    name = "local"

    def acquire(self, *, user_id: str, purpose: str) -> _LocalSandboxSession:
        max_bytes = _bounded_int(
            "FEEDLING_V2_LOCAL_ARTIFACT_MAX_BYTES",
            _DEFAULT_ARTIFACT_MAX_BYTES,
            maximum=100_000_000,
        )
        max_uncompressed = _bounded_int(
            "FEEDLING_V2_LOCAL_MAX_UNCOMPRESSED_BYTES",
            _DEFAULT_MAX_UNCOMPRESSED_BYTES,
            maximum=2_000_000_000,
        )
        return _LocalSandboxSession(max_bytes=max_bytes, max_uncompressed=max_uncompressed)
