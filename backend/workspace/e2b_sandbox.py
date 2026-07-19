"""Optional E2B implementation of the Runtime V2 sandbox contract.

The SDK import is deliberately lazy, so text-only Runtime V2 deployments do
not need the E2B package.  The configured template owns one fixed artifact
extractor contract; user/model strings are never interpolated into that
extractor command.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from workspace.sandbox import SandboxSession, SandboxUnavailable


log = logging.getLogger("feedling.workspace.e2b")

_ARTIFACT_PATH = "/tmp/feedling-artifact.bin"
_ARTIFACT_META_PATH = "/tmp/feedling-artifact-meta.json"
_ARTIFACT_TEXT_PATH = "/tmp/feedling-artifact-text.utf8"
_TEMPLATE_VERSION_PATH = "/opt/feedling/TEMPLATE_VERSION"
_TEMPLATE_RE = re.compile(
    r"^feedling-runtime-v2-artifacts-v1-(?P<digest>[0-9a-f]{64})$"
)
_EXTRACT_COMMAND = "/opt/feedling/bin/extract-artifact"
_CODE_PATHS_AND_COMMANDS = {
    "python": ("/tmp/feedling-code.py", "python /tmp/feedling-code.py"),
    "python3": ("/tmp/feedling-code.py", "python /tmp/feedling-code.py"),
}


def _bounded_positive_int(name: str, default: int, *, maximum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise SandboxUnavailable(f"invalid {name}") from exc
    if value <= 0 or value > maximum:
        raise SandboxUnavailable(f"invalid {name}")
    return value


def _enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _result_value(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _bounded_text(value: Any, maximum: int) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return text[:maximum], len(text) > maximum


class E2BSandboxSession:
    """One E2B microVM with fixed resource and output boundaries."""

    def __init__(
        self,
        sandbox: Any,
        *,
        command_timeout_sec: int,
        output_max_chars: int,
        artifact_max_bytes: int,
    ) -> None:
        self._sandbox = sandbox
        self._command_timeout_sec = command_timeout_sec
        self._output_max_chars = output_max_chars
        self._artifact_max_bytes = artifact_max_bytes
        self._closed = False

    def _assert_open(self) -> None:
        if self._closed:
            raise SandboxUnavailable("e2b sandbox session is closed")

    def _run(self, command: str) -> Any:
        self._assert_open()
        try:
            return self._sandbox.commands.run(
                command,
                timeout=self._command_timeout_sec,
            )
        except Exception as exc:  # SDK/network boundary
            raise SandboxUnavailable("e2b command failed") from exc

    def _command_dict(self, result: Any) -> dict:
        exit_code = _result_value(result, "exit_code")
        stdout, stdout_truncated = _bounded_text(
            _result_value(result, "stdout", ""), self._output_max_chars,
        )
        stderr, stderr_truncated = _bounded_text(
            _result_value(result, "stderr", ""), self._output_max_chars,
        )
        return {
            "ok": exit_code == 0,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": stdout_truncated or stderr_truncated,
        }

    def materialize(self, *, name: str, data: bytes, mime_type: str) -> str:
        self._assert_open()
        raw = bytes(data)
        if len(raw) > self._artifact_max_bytes:
            raise SandboxUnavailable("artifact exceeds e2b materialization limit")
        metadata = json.dumps(
            {
                "name": str(name or "artifact")[:512],
                "mime_type": str(mime_type or "application/octet-stream")[:200],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            self._sandbox.files.write(_ARTIFACT_PATH, raw)
            self._sandbox.files.write(_ARTIFACT_META_PATH, metadata)
            # Prevent stale output from a prior extraction in the same session.
            self._sandbox.files.write(_ARTIFACT_TEXT_PATH, b"")
        except Exception as exc:
            raise SandboxUnavailable("e2b artifact materialization failed") from exc
        return _ARTIFACT_PATH

    def extract_text(self, *, path: str, mime_type: str) -> str:
        self._assert_open()
        if path != _ARTIFACT_PATH:
            raise SandboxUnavailable("artifact path is outside the fixed e2b contract")
        result = self._run(_EXTRACT_COMMAND)
        if _result_value(result, "exit_code") != 0:
            raise SandboxUnavailable("e2b artifact extractor failed")
        try:
            value = self._sandbox.files.read(_ARTIFACT_TEXT_PATH)
        except Exception as exc:
            raise SandboxUnavailable("e2b artifact text unavailable") from exc
        text, _truncated = _bounded_text(value, self._output_max_chars)
        return text

    def run_shell(self, command: str) -> dict:
        command = str(command or "")
        if not command.strip() or len(command) > 16_000:
            raise SandboxUnavailable("invalid sandbox shell command")
        return self._command_dict(self._run(command))

    def run_code(self, *, language: str, source: str) -> dict:
        key = str(language or "").strip().lower()
        target = _CODE_PATHS_AND_COMMANDS.get(key)
        if target is None:
            raise SandboxUnavailable("unsupported sandbox code language")
        encoded = str(source or "").encode("utf-8")
        if len(encoded) > self._artifact_max_bytes:
            raise SandboxUnavailable("sandbox code exceeds source limit")
        path, command = target
        try:
            self._sandbox.files.write(path, encoded)
        except Exception as exc:
            raise SandboxUnavailable("e2b code materialization failed") from exc
        return self._command_dict(self._run(command))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._sandbox.kill()
        except Exception as exc:  # cleanup must not replace a completed turn
            log.warning("[workspace.e2b] sandbox kill failed type=%s", type(exc).__name__)


class E2BSandboxProvider:
    name = "e2b"

    def __init__(self, *, sandbox_cls: Any | None = None) -> None:
        self._sandbox_cls = sandbox_cls

    def _class(self) -> Any:
        if self._sandbox_cls is not None:
            return self._sandbox_cls
        try:
            from e2b import Sandbox
        except (ImportError, ModuleNotFoundError) as exc:
            raise SandboxUnavailable("e2b SDK is not installed") from exc
        return Sandbox

    def acquire(self, *, user_id: str, purpose: str) -> SandboxSession:
        api_key = os.environ.get("E2B_API_KEY", "").strip()
        template = os.environ.get("FEEDLING_V2_E2B_TEMPLATE", "").strip()
        if not api_key:
            raise SandboxUnavailable("E2B_API_KEY is not configured")
        if not template:
            raise SandboxUnavailable("FEEDLING_V2_E2B_TEMPLATE is not configured")
        template_match = _TEMPLATE_RE.fullmatch(template)
        if template_match is None:
            raise SandboxUnavailable("E2B template is not content-addressed")
        lifetime = _bounded_positive_int(
            "FEEDLING_V2_E2B_TIMEOUT_SEC", 300, maximum=3600,
        )
        command_timeout = _bounded_positive_int(
            "FEEDLING_V2_E2B_COMMAND_TIMEOUT_SEC", 30, maximum=lifetime,
        )
        output_max = _bounded_positive_int(
            "FEEDLING_V2_E2B_OUTPUT_MAX_CHARS", 64_000, maximum=1_000_000,
        )
        artifact_max = _bounded_positive_int(
            "FEEDLING_V2_E2B_ARTIFACT_MAX_BYTES", 25_000_000, maximum=100_000_000,
        )
        try:
            sandbox = self._class().create(
                template=template,
                timeout=lifetime,
                secure=True,
                allow_internet_access=_enabled(
                    "FEEDLING_V2_E2B_ALLOW_INTERNET", default=False,
                ),
                api_key=api_key,
            )
        except Exception as exc:
            raise SandboxUnavailable("e2b sandbox acquisition failed") from exc
        try:
            raw_version = sandbox.files.read(_TEMPLATE_VERSION_PATH)
            version = (
                raw_version.decode("ascii", errors="strict")
                if isinstance(raw_version, bytes)
                else str(raw_version)
            ).strip()
            expected_digest = template_match.group("digest")
            if version != expected_digest:
                raise ValueError("template version mismatch")
        except Exception as exc:
            try:
                sandbox.kill()
            except Exception:
                pass
            raise SandboxUnavailable("e2b template identity verification failed") from exc
        return E2BSandboxSession(
            sandbox,
            command_timeout_sec=command_timeout,
            output_max_chars=output_max,
            artifact_max_bytes=artifact_max,
        )
