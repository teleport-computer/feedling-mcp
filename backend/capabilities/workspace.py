"""Native Runtime V2 workspace capabilities."""
from __future__ import annotations

from capabilities import errors
from capabilities.types import CapabilityResult, err, ok
from workspace import backends as workspace_backends
from workspace import service as workspace_service


def _backend(store, runtime_token: str | None):
    return workspace_service.production_backend(
        store, runtime_token=str(runtime_token or ""),
    )


def _failure(exc: Exception) -> CapabilityResult:
    # Keep decrypted paths/content/exception text out of the model-visible error.
    if isinstance(exc, workspace_backends.WorkspaceNotFound):
        return err(errors.NOT_FOUND, "workspace entry not found", retryable=False)
    if isinstance(exc, workspace_backends.WorkspaceReadOnly):
        return err(errors.FORBIDDEN, "workspace namespace is read-only", retryable=False)
    if isinstance(exc, workspace_backends.WorkspaceConflict):
        return err(errors.INVALID, "workspace revision conflict", retryable=False)
    if isinstance(exc, workspace_backends.WorkspaceInvalidPath):
        return err(errors.INVALID, "invalid workspace path", retryable=False)
    if isinstance(exc, workspace_backends.WorkspaceError):
        return err(errors.INVALID, "invalid workspace operation", retryable=False)
    return err(errors.UNAVAILABLE, "workspace unavailable", retryable=True)


def list_entries(store, *, api_key=None, runtime_token=None,
                 params=None) -> CapabilityResult:
    params = params or {}
    try:
        data = workspace_service.list_entries(
            _backend(store, runtime_token),
            path=str(params.get("path") or "/"),
            recursive=bool(params.get("recursive", True)),
            limit=int(params.get("limit") or 100),
        )
        return ok(errors.cap_data(data))
    except Exception as exc:  # noqa: BLE001 - stable capability boundary
        return _failure(exc)


def read(store, *, api_key=None, runtime_token=None,
         params=None) -> CapabilityResult:
    params = params or {}
    try:
        data = workspace_service.read_text(
            _backend(store, runtime_token),
            path=str(params.get("path") or ""),
            start_line=int(params.get("start_line") or 1),
            line_count=int(params.get("line_count") or 200),
        )
        return ok(errors.cap_data(data))
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


def write(store, *, api_key=None, runtime_token=None,
          params=None) -> CapabilityResult:
    params = params or {}
    try:
        data = workspace_service.write_text(
            _backend(store, runtime_token),
            path=str(params.get("path") or ""),
            content=params.get("content"),
            expected_revision=params.get("expected_revision"),
        )
        return ok(data)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)


def delete(store, *, api_key=None, runtime_token=None,
           params=None) -> CapabilityResult:
    params = params or {}
    try:
        data = workspace_service.delete_entry(
            _backend(store, runtime_token),
            path=str(params.get("path") or ""),
            expected_revision=params.get("expected_revision"),
        )
        return ok(data)
    except Exception as exc:  # noqa: BLE001
        return _failure(exc)

