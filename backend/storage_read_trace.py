"""Content-free observations for synchronous Canvas and chat-body reads.

The request-local hydrate observation inherits an inner GET failure even when
the storage API returns its historical None sentinel, and propagates that
failure to an enclosing endpoint read. No keys or bodies are retained here,
and concurrent reads cannot exchange observations.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import time


_hydrate_observation: ContextVar[tuple[str, dict] | None] = ContextVar(
    "chat_body_hydrate_observation", default=None,
)
_store: ContextVar[object | None] = ContextVar("chat_body_trace_store", default=None)
_read_observation: ContextVar[tuple[str, dict] | None] = ContextVar(
    "chat_endpoint_read_observation", default=None,
)


@contextmanager
def bind_store(store):
    """Bind the authenticated caller's actual store for this delivery only."""
    token = _store.set(store)
    try:
        yield
    finally:
        _store.reset(token)


def failure(exc: Exception) -> dict:
    """Classify without serializing exception text, URLs or provider payloads."""
    name = type(exc).__name__
    response = getattr(exc, "response", None)
    response = response if isinstance(response, dict) else {}
    error = response.get("Error") or {}
    metadata = response.get("ResponseMetadata") or {}
    code = error.get("Code") if isinstance(error, dict) else None
    code = code if isinstance(code, str) else None
    http_status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
    if isinstance(exc, TimeoutError) or name in {"ReadTimeoutError", "ConnectTimeoutError"}:
        status = "timeout"
    elif code in {"NoSuchKey", "NoSuchBucket", "404"} or name in {"NoSuchKey", "404"} or http_status == 404:
        status = "not_found"
    elif (isinstance(http_status, int) and 500 <= http_status < 600) or (
        isinstance(code, str) and code.isdigit() and 500 <= int(code) < 600
    ):
        status = "http_5xx"
    else:
        status = "other"
    # Error class names are a closed set as well; never forward arbitrary data.
    safe_name = name if name in {
        "ClientError", "ReadTimeoutError", "ConnectTimeoutError", "TimeoutError",
        "ValueError", "OSError", "ConnectionError", "NoSuchKey", "NoSuchBucket",
    } else "other"
    return {"status": status, "error_class": safe_name}


def _emit(user_id: str, event_type: str, detail: dict) -> None:
    try:
        store = _store.get()
        if store is None or getattr(store, "user_id", None) != user_id:
            return
        # These are peers of db/object_storage. Defer the import to avoid the
        # debug_trace -> db -> object_storage import cycle; no UserStore load.
        import debug_trace

        debug_trace.trace_event(
            store,
            subsystem="chat",
            type=event_type,
            status="ok" if detail["status"] == "ok" else "error",
            detail=detail,
            dur_ms=detail["dur_ms"],
        )
    except Exception:
        # Match debug_trace's best-effort contract; telemetry cannot break reads.
        pass


@contextmanager
def observe_read(store, *, event_type: str, source: str):
    """Observe a complete read, including inline bodies and missing rows.

    Source describes the server's actual read path, never the client's reason
    for requesting it. In particular, history pagination cannot distinguish a
    Canvas fallback from ordinary chat history. Nor does a storage source prove
    which runtime originally wrote a card.
    """
    detail = {
        "source": source, "status": "ok", "error_class": None,
        "http_status": None, "response_bytes": None,
    }
    token = _read_observation.set((store.user_id, detail))
    started = time.monotonic()
    with bind_store(store):
        try:
            yield detail
        except Exception as exc:
            detail.update(failure(exc))
            raise
        finally:
            detail["dur_ms"] = round((time.monotonic() - started) * 1000, 1)
            _read_observation.reset(token)
            _emit(store.user_id, event_type, detail)


def finish_response(detail: dict, body: dict, status: int) -> tuple[dict, int]:
    """Record response size, without retaining or decoding the response body.

    This is the compact UTF-8 JSON size emitted by JSONResponse, including the
    opaque envelope/metadata; it is NOT plaintext size or storage GET bytes.
    """
    detail["http_status"] = status
    if status >= 400:
        detail["status"] = (
            "not_found" if status == 404 else
            "invalid_request" if status < 500 else "http_5xx"
        )
    try:
        detail["response_bytes"] = len(json.dumps(
            body, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        # Size unavailable must not become zero or change response behavior.
        pass
    return body, status


@contextmanager
def observe(user_id: str, *, hydrate: bool = False):
    detail = {"status": "ok", "error_class": None, "bytes": None}
    token = _hydrate_observation.set((user_id, detail)) if hydrate else None
    started = time.monotonic()
    try:
        yield detail
    except Exception as exc:
        detail.update(failure(exc))
        raise
    finally:
        detail["dur_ms"] = round((time.monotonic() - started) * 1000, 1)
        if hydrate:
            _hydrate_observation.reset(token)
            read = _read_observation.get()
            if read is not None and read[0] == user_id and detail["status"] != "ok":
                # A swallowed storage failure can still return HTTP 200 with
                # no body. Preserve that failure in the endpoint observation.
                read[1].update({key: detail[key] for key in ("status", "error_class")})
        else:
            parent = _hydrate_observation.get()
            if parent is not None and parent[0] == user_id:
                parent[1].update({key: detail[key] for key in ("status", "error_class", "bytes")})
        _emit(user_id, "chat.file_body.hydrate" if hydrate else "object_storage.get", detail)
