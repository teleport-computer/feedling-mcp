"""Query encoder in the Runtime V2 parent process (T779 step 2b).

The serve-worker parent owns the only E5 instance (the one the card-vector sweep
already uses). Slot processes select cards next to the data and ask this service
to turn their 1-2 query texts into vectors. Every encode of that instance goes
through one scheduler:

- one worker thread; a waiting query always runs before the next sweep segment,
  so a query is blocked by the sweep for at most the segment already running
  (other queued queries are separate);
- sweep work is cut into segments of ``segment_size()`` cards while this service
  runs; with the service off the sweep calls the model exactly as before;
- at most ``MAX_QUEUED_QUERIES`` queries wait; more are refused (``encoder_busy``);
- a query whose deadline passed before it starts is dropped; a query already
  computing keeps the worker until it finishes, even if its caller gave up.

The HTTP endpoint binds 127.0.0.1 only and requires the per-process token the
parent hands to the slot processes it spawns. It never logs texts or vectors and
reads no user data.
"""
from __future__ import annotations

import collections
import hmac
import http.server
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("feedling.memory.query_service")

PORT_ENV = "FEEDLING_V2_QUERY_ENCODER_PORT"
TOKEN_ENV = "FEEDLING_V2_QUERY_ENCODER_TOKEN"
OWNER_ENV = "FEEDLING_EMBEDDER_OWNER_PID"
SEGMENT_ENV = "FEEDLING_EMBED_SWEEP_SEGMENT"
DEFAULT_PORT = 5098
DEFAULT_SEGMENT = 4
MAX_QUEUED_QUERIES = 4
MAX_TEXTS = 2
# No explicit chat text cap exists; the longest realistic query is a voice
# transcript (~180 KB for an hour). These bound the request, they never truncate:
# a larger query is refused as query_too_large and that turn stays lexical.
MAX_TEXT_BYTES = 1024 * 1024
# The wire carries JSON: one text byte can become up to six bytes when escaped
# (a control character as \\u0001). The quota that matters is MAX_TEXT_BYTES per
# text, applied after parsing; this only bounds the loopback request.
MAX_REQUEST_BYTES = 6 * MAX_TEXTS * MAX_TEXT_BYTES + 4096
TOKEN_HEADER = "X-Feedling-Encoder-Token"


class Refused(Exception):
    """A fixed-vocabulary refusal (encoder_busy, deadline_exceeded, ...)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def segment_size() -> int:
    try:
        return max(1, int(os.environ.get(SEGMENT_ENV, DEFAULT_SEGMENT)))
    except (TypeError, ValueError):
        return DEFAULT_SEGMENT


def claim_embedder_ownership() -> None:
    """Called by the parent before it spawns slot processes."""
    os.environ[OWNER_ENV] = str(os.getpid())


def assert_embedder_owner() -> None:
    """Raise in any process other than the one that claimed the model."""
    owner = os.environ.get(OWNER_ENV)
    if owner and owner != str(os.getpid()):
        raise RuntimeError("embedder_not_owned_by_this_process")


@dataclass
class _Job:
    kind: str                     # "query" | "segment"
    texts: list
    deadline: float | None = None
    submitted: float = field(default_factory=time.monotonic)
    started: float | None = None
    finished: float | None = None
    vectors: list | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


_STAT_SAMPLES = 500


def _summary(values) -> dict:
    """p50 / p95 / max of a bounded sample, or all None when empty."""
    ordered = sorted(values)
    if not ordered:
        return {"n": 0, "p50": None, "p95": None, "max": None}
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]  # noqa: E731
    return {"n": len(ordered), "p50": round(pick(0.5), 1), "p95": round(pick(0.95), 1),
            "max": round(ordered[-1], 1)}


class Scheduler:
    def __init__(self, embedder):
        self._embedder = embedder
        self._cv = threading.Condition()
        self._queries: collections.deque = collections.deque()
        self._segments: collections.deque = collections.deque()
        self._closed = False
        # Content-free counters for step 2c measurement. Waits cover every query
        # that reached the worker (including ones dropped as expired), not only
        # successful answers, so their max is a real worst case for that window.
        self._running_kind: str | None = None
        self._counts = collections.Counter()
        self._query_wait_ms: collections.deque = collections.deque(maxlen=_STAT_SAMPLES)
        self._segment_ms: collections.deque = collections.deque(maxlen=_STAT_SAMPLES)
        self._thread = threading.Thread(target=self._run, name="query-encoder", daemon=True)
        self._thread.start()

    @property
    def model_id(self) -> str:
        return self._embedder.model_id

    @property
    def dim(self) -> int:
        return self._embedder.dim

    def submit_query(self, texts: list, deadline: float) -> _Job:
        job = _Job("query", list(texts), deadline)
        with self._cv:
            if self._closed:
                self._counts["query_refused_closed"] += 1
                raise Refused("encoder_service_unavailable")
            if len(self._queries) >= MAX_QUEUED_QUERIES:
                self._counts["query_refused_busy"] += 1
                raise Refused("encoder_busy")
            self._counts["query_submitted"] += 1
            if self._running_kind == "segment":
                self._counts["query_behind_segment"] += 1
            self._queries.append(job)
            self._cv.notify()
        return job

    def stats(self) -> dict:
        """Content-free snapshot for the parent heartbeat (T779 step 2c)."""
        with self._cv:
            return {
                "counts": dict(self._counts),
                "queued": len(self._queries),
                "segments_queued": len(self._segments),
                "query_wait_ms": _summary(list(self._query_wait_ms)),
                "segment_ms": _summary(list(self._segment_ms)),
            }

    def encode_segment(self, texts: list) -> list:
        job = _Job("segment", list(texts))
        with self._cv:
            if self._closed:
                raise Refused("encoder_service_unavailable")
            self._segments.append(job)
            self._cv.notify()
        job.done.wait()
        if job.error:
            raise RuntimeError(job.error)
        return job.vectors

    def close(self) -> None:
        with self._cv:
            self._closed = True
            pending = list(self._queries) + list(self._segments)
            self._queries.clear()
            self._segments.clear()
            self._cv.notify_all()
        for job in pending:
            job.error = "encoder_service_unavailable"
            job.done.set()

    def _next(self):
        with self._cv:
            while not self._closed and not self._queries and not self._segments:
                self._cv.wait()
            if self._closed:
                return None
            # A waiting query always goes before the next sweep segment.
            job = self._queries.popleft() if self._queries else self._segments.popleft()
            self._running_kind = job.kind
            if job.kind == "query":
                self._query_wait_ms.append((time.monotonic() - job.submitted) * 1000.0)
            return job

    def _run(self) -> None:
        while True:
            job = self._next()
            if job is None:
                return
            if job.kind == "query" and job.deadline is not None and time.monotonic() >= job.deadline:
                job.error = "deadline_exceeded"
                with self._cv:
                    self._counts["query_expired_dropped"] += 1
                    self._running_kind = None
                job.done.set()
                continue
            job.started = time.monotonic()
            try:
                if job.kind == "query":
                    job.vectors = [self._embedder.encode_query(text) for text in job.texts]
                else:
                    job.vectors = self._embedder.encode_passages(job.texts)
            except Exception:
                job.error = "encode_failed"
            job.finished = time.monotonic()
            with self._cv:
                self._running_kind = None
                if job.kind == "segment":
                    self._counts["segments"] += 1
                    self._segment_ms.append((job.finished - job.started) * 1000.0)
                else:
                    self._counts["query_failed" if job.error else "query_served"] += 1
            job.done.set()


_scheduler: Scheduler | None = None
_server: http.server.ThreadingHTTPServer | None = None


def active_scheduler() -> Scheduler | None:
    return _scheduler


def status() -> dict:
    """Parent-side readiness + scheduler counters for the heartbeat (content-free)."""
    scheduler = _scheduler
    return {
        "owner_pid": os.environ.get(OWNER_ENV) or None,
        "reserved": _reserved is not None,
        "loader_started": _loader is not None,
        "serving": scheduler is not None,
        "model_id": (str(scheduler.model_id)[:200] if scheduler is not None else None),
        "scheduler": scheduler.stats() if scheduler is not None else None,
    }


def encode_passages(embedder, texts: list) -> list:
    """The sweep's encode call: unchanged without the service, segmented through
    the scheduler (behind any waiting query) while it runs."""
    scheduler = _scheduler
    if scheduler is None:
        return embedder.encode_passages(texts)
    size = segment_size()
    out: list = []
    for offset in range(0, len(texts), size):
        out.extend(scheduler.encode_segment(texts[offset:offset + size]))
    return out


def _encode_request(scheduler: Scheduler, body: dict) -> dict:
    texts = body.get("texts")
    if (not isinstance(texts, list) or not 1 <= len(texts) <= MAX_TEXTS
            or not all(isinstance(t, str) and t for t in texts)):
        raise Refused("invalid_request")
    if any(len(t.encode("utf-8")) > MAX_TEXT_BYTES for t in texts):
        raise Refused("query_too_large")
    try:
        budget_ms = float(body.get("budget_ms"))
    except (TypeError, ValueError):
        raise Refused("invalid_request") from None
    if budget_ms <= 0:
        raise Refused("deadline_exceeded")
    deadline = time.monotonic() + budget_ms / 1000.0
    job = scheduler.submit_query(texts, deadline)
    if not job.done.wait(max(0.0, deadline - time.monotonic())):
        # The caller's budget is spent. A job still queued is dropped when it
        # reaches the worker; one already computing finishes and is discarded.
        raise Refused("deadline_exceeded")
    if job.error:
        raise Refused(job.error)
    return {
        "model_id": scheduler.model_id, "dim": scheduler.dim, "vectors": job.vectors,
        "timing": {"encode_queue_ms": round(((job.started or job.submitted) - job.submitted) * 1000.0, 1),
                   "encode_compute_ms": round(((job.finished or 0) - (job.started or 0)) * 1000.0, 1)},
    }


_STATUS = {"invalid_request": 400, "query_too_large": 413, "unauthorized": 401,
           "encoder_busy": 429, "deadline_exceeded": 504, "encode_failed": 500,
           "encoder_service_unavailable": 503}


def _handler_for(scheduler: Scheduler, token: str):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):  # never log request contents
            return

        def _send(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):  # noqa: N802
            if self.path != "/encode":
                return self._send(404, {"error": "not_found"})
            if not hmac.compare_digest(str(self.headers.get(TOKEN_HEADER) or ""), token):
                return self._send(401, {"error": "unauthorized"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > MAX_REQUEST_BYTES:
                return self._send(413 if length > MAX_REQUEST_BYTES else 400,
                                  {"error": "query_too_large" if length > MAX_REQUEST_BYTES
                                   else "invalid_request"})
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                payload = _encode_request(scheduler, body if isinstance(body, dict) else {})
            except Refused as exc:
                return self._send(_STATUS.get(exc.reason, 400), {"error": exc.reason})
            except (ValueError, UnicodeDecodeError):
                return self._send(400, {"error": "invalid_request"})
            return self._send(200, payload)

    return Handler


# Parent-process lifetime (not per _serve generation): the endpoint, its token,
# the loader thread and the running service exist at most once per process. A
# _serve restart inside the same parent re-exports the same port/token to its new
# slot processes and finds the service already running or loading.
_lifecycle_lock = threading.Lock()
_reserved: tuple[int, str] | None = None
_loader: threading.Thread | None = None


def reserve(port: int | None = None) -> tuple[int, str]:
    """Fix the endpoint and token in this (parent) process's environment before
    slot processes are spawned; they inherit both. Idempotent: a second call in
    the same process keeps the first port and token. Serving starts later with
    ``start`` once the model is loaded; until then clients get
    ``encoder_service_unavailable`` and stay lexical."""
    global _reserved
    with _lifecycle_lock:
        if _reserved is None:
            chosen = int(port if port is not None else os.environ.get(PORT_ENV, DEFAULT_PORT))
            _reserved = (chosen, secrets.token_hex(32))
        os.environ[PORT_ENV] = str(_reserved[0])
        os.environ[TOKEN_ENV] = _reserved[1]
        return _reserved


def start(embedder) -> int:
    """Start the scheduler and the loopback endpoint with the reserved port/token.

    Idempotent: returns the running port if the service is already up. If the
    endpoint cannot be bound, the scheduler created for it is closed again, so a
    failed start leaves no worker thread behind.
    """
    global _scheduler, _server, _reserved
    assert_embedder_owner()
    with _lifecycle_lock:
        if _server is not None:
            return _server.server_address[1]
        if _reserved is None:
            raise RuntimeError("encoder_endpoint_not_reserved")
        port, token = _reserved
        scheduler = Scheduler(embedder)
        try:
            server = http.server.ThreadingHTTPServer(
                ("127.0.0.1", port), _handler_for(scheduler, token))
        except BaseException:
            scheduler.close()
            raise
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, name="query-encoder-http", daemon=True).start()
        _scheduler, _server = scheduler, server
        bound = server.server_address[1]
        if bound != port:  # port 0 asked for an ephemeral port
            _reserved = (bound, token)
            os.environ[PORT_ENV] = str(bound)
        return bound


def ensure_loader(load) -> bool:
    """Start ``load()`` on a background thread once per process; later calls
    (for example after a _serve restart) do nothing and return False."""
    global _loader
    with _lifecycle_lock:
        if _loader is not None:
            return False
        _loader = threading.Thread(target=load, name="query-encoder-load", daemon=True)
        _loader.start()
        return True


def stop() -> None:
    """Close the service and forget the reservation (process exit and tests)."""
    global _scheduler, _server, _reserved, _loader
    with _lifecycle_lock:
        server, scheduler = _server, _scheduler
        _scheduler, _server, _reserved, _loader = None, None, None, None
    if server is not None:
        server.shutdown()
        server.server_close()
    if scheduler is not None:
        scheduler.close()
