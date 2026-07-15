"""Conversation target boundary for persona and memory regression runs.

The runner depends on the small :class:`ConversationTarget` protocol rather
than on a particular agent framework.  Tests can therefore inject an in-memory
target, while live runs can use :class:`FeedlingTarget` and the repository's
existing ``SmokeClient`` transport.

Two boundary actions are deliberately distinct:

``clear_history``
    Clears the visible chat transcript.  This proves only a transcript
    boundary; it does *not* prove that the hosted agent runtime session changed.

``rotate_runtime_session``
    Requires a deployment-specific callback that returns evidence containing
    different before/after runtime session identifiers.  Strong persistent
    memory scenarios must use this action.
"""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping, Protocol, Sequence, runtime_checkable


INFRA_ERROR = "INFRA_ERROR"
BLOCKED_EVIDENCE = "BLOCKED_EVIDENCE"

BOUNDARY_NONE = "none"
BOUNDARY_CLEAR_HISTORY = "clear_history"
BOUNDARY_ROTATE_RUNTIME_SESSION = "rotate_runtime_session"
DEFAULT_ALLOWED_FEEDLING_ORIGINS = ("https://test-api.feedling.app",)


class TargetError(RuntimeError):
    """A safe, classifiable target failure.

    ``detail`` must be suitable for a regression artifact.  Adapters should not
    place credentials, raw HTTP bodies, or decrypted private state in it.
    """

    def __init__(
        self,
        code: str,
        *,
        status: str = INFRA_ERROR,
        detail: str = "",
    ) -> None:
        if status not in {INFRA_ERROR, BLOCKED_EVIDENCE}:
            raise ValueError("target error status must be INFRA_ERROR or BLOCKED_EVIDENCE")
        self.code = str(code or "TARGET_ERROR")
        self.status = status
        self.detail = str(detail or self.code)
        super().__init__(f"{self.code}: {self.detail}")


@dataclass(frozen=True, kw_only=True)
class TargetCapabilities:
    """Boundary behavior a target can prove, not merely attempt."""

    clear_history: bool = False
    runtime_session_rotation: bool = False


@dataclass(frozen=True, kw_only=True)
class TargetContext:
    """Identity for one isolated target conversation."""

    run_id: str
    scenario_id: str
    repeat_index: int
    session_key: str = "default"


@dataclass(frozen=True, kw_only=True)
class TargetSession:
    """Opaque target session owned by one trajectory.

    ``session_id`` is an eval-harness identifier and must not be interpreted as
    proof of a hosted runtime session rotation.  Such proof is carried only by
    :class:`BoundaryResult`.
    """

    target_id: str
    session_key: str
    session_id: str
    generation: int = 0
    account_fingerprint: str = field(default="", repr=False)
    opaque: Any = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, kw_only=True)
class TargetResponse:
    """One correlated assistant response and its bounded transport evidence."""

    text: str
    request_id: str = ""
    response_id: str = ""
    trace_id: str = ""
    latency_ms: float | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class BoundaryResult:
    """Result of applying a boundary to a target conversation."""

    session: TargetSession
    action: str
    boundary_kind: str
    runtime_session_rotated: bool
    evidence: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ConversationTarget(Protocol):
    """Minimal injectable interface consumed by the state-machine runner."""

    @property
    def target_id(self) -> str: ...

    @property
    def capabilities(self) -> TargetCapabilities: ...

    def open_session(self, context: TargetContext) -> TargetSession: ...

    def send(
        self,
        session: TargetSession,
        *,
        turn_id: str,
        prompt: str,
        timeout_seconds: float,
    ) -> TargetResponse: ...

    def apply_boundary(
        self,
        session: TargetSession,
        *,
        action: str,
    ) -> BoundaryResult: ...

    def close_session(self, session: TargetSession) -> None: ...


@dataclass(frozen=True, kw_only=True)
class _FeedlingOpaqueSession:
    client: Any = field(repr=False, compare=False)
    credentials: Any = field(repr=False, compare=False)


SessionFactory = Callable[[TargetContext], Any]
ClientFactory = Callable[[str], Any]
RuntimeSessionRotator = Callable[[Any, Any], Mapping[str, Any]]
SessionCloser = Callable[[Any, Any], None]


def _origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(str(value or ""))
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Feedling target origin must be a credential-free HTTPS origin")
    return urllib.parse.urlunsplit(
        ("https", parsed.netloc.lower(), "", "", "")
    )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FeedlingTarget:
    """Adapter from the regression protocol to ``SmokeClient``.

    A ``session_factory`` is required so every repeat can receive an isolated
    synthetic account/session.  The optional runtime rotator is intentionally
    separate from transcript clearing; it must return ``rotated=True`` plus
    distinct ``before_runtime_session_id`` and ``after_runtime_session_id``
    values before the adapter will publish rotation evidence.
    """

    def __init__(
        self,
        *,
        target_id: str,
        base_url: str,
        session_factory: SessionFactory,
        client_factory: ClientFactory | None = None,
        runtime_session_rotator: RuntimeSessionRotator | None = None,
        session_closer: SessionCloser | None = None,
        external_cleanup_guaranteed: bool = False,
        allowed_origins: Sequence[str] = DEFAULT_ALLOWED_FEEDLING_ORIGINS,
        default_timeout_seconds: float = 120.0,
    ) -> None:
        clean_target_id = str(target_id or "").strip()
        if not clean_target_id:
            raise ValueError("target_id is required")
        if default_timeout_seconds <= 0:
            raise ValueError("default_timeout_seconds must be positive")
        normalized_origin = _origin(base_url)
        normalized_allowed = {_origin(value) for value in allowed_origins}
        if normalized_origin not in normalized_allowed:
            raise ValueError("Feedling target origin is not explicitly allowed")
        if session_closer is None and not external_cleanup_guaranteed:
            raise ValueError(
                "session_closer is required unless external cleanup is guaranteed"
            )
        self._target_id = clean_target_id
        self._base_url = normalized_origin
        self._session_factory = session_factory
        self._client_factory = client_factory
        self._runtime_session_rotator = runtime_session_rotator
        self._session_closer = session_closer
        self._default_timeout_seconds = float(default_timeout_seconds)
        self._account_lock = threading.Lock()
        self._active_account_fingerprints: set[str] = set()
        self._seen_account_fingerprints: set[str] = set()

    @property
    def target_id(self) -> str:
        return self._target_id

    @property
    def capabilities(self) -> TargetCapabilities:
        return TargetCapabilities(
            clear_history=True,
            runtime_session_rotation=self._runtime_session_rotator is not None,
        )

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory(self._base_url)
        # Lazy import keeps fake-target unit tests independent of live crypto and
        # HTTP transport dependencies.
        from tools.provider_smoke.client import SmokeClient

        return SmokeClient(self._base_url)

    @staticmethod
    def _opaque(session: TargetSession) -> _FeedlingOpaqueSession:
        if not isinstance(session.opaque, _FeedlingOpaqueSession):
            raise TargetError("INVALID_TARGET_SESSION", detail="Feedling session is invalid")
        return session.opaque

    def open_session(self, context: TargetContext) -> TargetSession:
        try:
            client = self._new_client()
            credentials = self._session_factory(context)
        except TargetError:
            raise
        except Exception:
            raise TargetError(
                "SESSION_OPEN_FAILED",
                detail="Feedling synthetic session could not be opened",
            ) from None
        if credentials is None:
            raise TargetError(
                "SESSION_OPEN_FAILED",
                detail="Feedling session factory returned no credentials",
            )
        user_id = str(getattr(credentials, "user_id", "") or "").strip()
        api_key = str(getattr(credentials, "api_key", "") or "").strip()
        secret_key = getattr(credentials, "sk", None)
        public_key = getattr(credentials, "pk", None)
        if (
            not user_id
            or not api_key
            or not isinstance(secret_key, bytes)
            or len(secret_key) != 32
            or not isinstance(public_key, bytes)
            or len(public_key) != 32
        ):
            raise TargetError(
                "INVALID_SESSION_CREDENTIALS",
                detail="Feedling session credentials have an invalid shape",
            )
        account_fingerprint = _digest(user_id)
        with self._account_lock:
            if account_fingerprint in self._seen_account_fingerprints:
                raise TargetError(
                    "SESSION_ISOLATION_FAILED",
                    status=BLOCKED_EVIDENCE,
                    detail="Feedling session factory reused a synthetic account",
                )
            self._active_account_fingerprints.add(account_fingerprint)
            self._seen_account_fingerprints.add(account_fingerprint)
        return TargetSession(
            target_id=self.target_id,
            session_key=context.session_key,
            session_id=f"eval-{uuid.uuid4().hex}",
            account_fingerprint=account_fingerprint,
            opaque=_FeedlingOpaqueSession(client=client, credentials=credentials),
        )

    def send(
        self,
        session: TargetSession,
        *,
        turn_id: str,
        prompt: str,
        timeout_seconds: float,
    ) -> TargetResponse:
        opaque = self._opaque(session)
        timeout = (
            float(timeout_seconds)
            if timeout_seconds and timeout_seconds > 0
            else self._default_timeout_seconds
        )
        started = time.monotonic()
        try:
            deadline = started + timeout
            acknowledgement = opaque.client.send(
                opaque.credentials,
                prompt,
                read_timeout=max(0.1, min(deadline - time.monotonic(), 45.0)),
            )
            ack_latency_ms = (time.monotonic() - started) * 1000.0
            user_message = acknowledgement.get("user_message") or {}
            request_id = str(user_message.get("id") or "").strip()
            user_ts = float(user_message["ts"])
            if not request_id:
                raise ValueError("missing user message id")
            record = opaque.client.poll_reply_record(
                opaque.credentials,
                user_ts,
                max(0.1, deadline - time.monotonic()),
                include_thinking=False,
                user_message_id=request_id,
            )
            if record is None:
                raise TargetError(
                    "MISSING_REPLY",
                    detail="Feedling target did not return a correlated reply in time",
                )
            message = record.get("message") or {}
            response_id = str(message.get("id") or "").strip()
            if not response_id:
                raise TargetError(
                    "REPLY_CORRELATION_FAILED",
                    status=BLOCKED_EVIDENCE,
                    detail="Feedling reply has no correlated response id",
                )
            return TargetResponse(
                text=str(record.get("reply") or ""),
                request_id=request_id,
                response_id=response_id,
                # A request id is useful for an optional trace lookup but is not
                # itself proof that a matching trace event was collected.
                trace_id="",
                latency_ms=(time.monotonic() - started) * 1000.0,
                metadata={
                    "ack_latency_ms": ack_latency_ms,
                    "reply_correlation": "exact_message_id",
                    "trace_correlation": "not_collected",
                    "turn_id": str(turn_id),
                },
            )
        except TargetError:
            raise
        except Exception as exc:
            stage = str(getattr(exc, "stage", "target") or "target")[:64]
            status = BLOCKED_EVIDENCE if stage == "reply-correlation" else INFRA_ERROR
            code = (
                "REPLY_CORRELATION_FAILED"
                if stage == "reply-correlation"
                else "TARGET_REQUEST_FAILED"
            )
            raise TargetError(
                code,
                status=status,
                detail=f"Feedling target failed at stage {stage}",
            ) from None

    def apply_boundary(
        self,
        session: TargetSession,
        *,
        action: str,
    ) -> BoundaryResult:
        opaque = self._opaque(session)
        if action == BOUNDARY_NONE:
            return BoundaryResult(
                session=session,
                action=action,
                boundary_kind="none",
                runtime_session_rotated=False,
            )
        if action == BOUNDARY_CLEAR_HISTORY:
            try:
                status, body = opaque.client._req(
                    "DELETE",
                    "/v1/chat/history",
                    api_key=opaque.credentials.api_key,
                    body={"confirm": "clear-chat-history"},
                    attempts=1,
                )
            except Exception:
                raise TargetError(
                    "TRANSCRIPT_BOUNDARY_FAILED",
                    detail="Feedling chat transcript could not be cleared",
                ) from None
            if status != 200 or not isinstance(body, Mapping) or body.get("cleared") is not True:
                raise TargetError(
                    "TRANSCRIPT_BOUNDARY_FAILED",
                    detail="Feedling did not confirm transcript clearing",
                )
            next_session = replace(session, generation=session.generation + 1)
            deleted = body.get("deleted")
            return BoundaryResult(
                session=next_session,
                action=action,
                boundary_kind="transcript",
                runtime_session_rotated=False,
                evidence={
                    "transcript_cleared": True,
                    "deleted_count": deleted if type(deleted) is int and deleted >= 0 else None,
                    "runtime_session_rotation_claimed": False,
                },
            )
        if action == BOUNDARY_ROTATE_RUNTIME_SESSION:
            if self._runtime_session_rotator is None:
                raise TargetError(
                    "SESSION_BOUNDARY_UNPROVEN",
                    status=BLOCKED_EVIDENCE,
                    detail="Target has no runtime session rotation evidence provider",
                )
            try:
                evidence = dict(
                    self._runtime_session_rotator(opaque.client, opaque.credentials)
                )
            except TargetError:
                raise
            except Exception:
                raise TargetError(
                    "SESSION_BOUNDARY_UNPROVEN",
                    status=BLOCKED_EVIDENCE,
                    detail="Runtime session rotation evidence could not be collected",
                ) from None
            before_id = str(evidence.get("before_runtime_session_id") or "").strip()
            after_id = str(evidence.get("after_runtime_session_id") or "").strip()
            if (
                evidence.get("rotated") is not True
                or not before_id
                or not after_id
                or before_id == after_id
            ):
                raise TargetError(
                    "SESSION_BOUNDARY_UNPROVEN",
                    status=BLOCKED_EVIDENCE,
                    detail="Runtime session rotation evidence is incomplete",
                )
            next_session = replace(session, generation=session.generation + 1)
            return BoundaryResult(
                session=next_session,
                action=action,
                boundary_kind="runtime_session",
                runtime_session_rotated=True,
                evidence={
                    "rotated": True,
                    "before_runtime_session_sha256": _digest(before_id),
                    "after_runtime_session_sha256": _digest(after_id),
                    "evidence_sha256": (
                        _digest(str(evidence["evidence_id"]))
                        if evidence.get("evidence_id")
                        else ""
                    ),
                },
            )
        raise TargetError(
            "UNSUPPORTED_BOUNDARY",
            status=BLOCKED_EVIDENCE,
            detail=f"Target does not support boundary action {str(action)[:64]}",
        )

    def close_session(self, session: TargetSession) -> None:
        opaque = self._opaque(session)
        try:
            if self._session_closer is not None:
                self._session_closer(opaque.client, opaque.credentials)
        except Exception:
            raise TargetError(
                "SESSION_CLOSE_FAILED",
                detail="Feedling synthetic session could not be closed",
            ) from None
        finally:
            with self._account_lock:
                self._active_account_fingerprints.discard(
                    session.account_fingerprint
                )
