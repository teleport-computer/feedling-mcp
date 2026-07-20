"""Strict live proof for a Codex-backed Feedling runtime-session boundary.

The strong-memory regression cannot treat transcript clearing as proof that the
model runtime changed.  This module uses only existing authenticated test
endpoints to:

1. force and verify persistence of the fact learned by the preceding turn;
2. read that exact turn's Codex ``thread.started`` trace event;
3. clear the transcript, send a content-free boundary probe, and read the
   probe's exact Codex thread identifier; and
4. clear the probe before the regression runner sends its recall question.

Raw request, response, capture-job, and Codex thread identifiers leave this
callable only through a strictly allowlisted private debug sidecar. The normal
boundary evidence still carries hashes; the raw sidecar is excluded from judge
requests and public artifacts and may be projected only into an encrypted
failure bundle.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from typing import Any, Callable, Mapping


_TERMINAL_CAPTURE_STATUSES = frozenset({"completed", "failed", "skipped"})
_ACTIVE_CAPTURE_STATUSES = frozenset({"pending", "claimed", "realizing"})
_RUNTIME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_BOUNDARY_PROBE = (
    "Automated QA runtime-boundary probe. Reply briefly that the boundary probe "
    "was received; do not infer or store any user preference from this message."
)


class RotationEvidenceError(RuntimeError):
    """A fixed, credential-free reason why rotation could not be proven."""

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        protected_debug_identifiers: Mapping[str, list[str]] | None = None,
    ) -> None:
        self.code = str(code)
        self.detail = str(detail)
        self.protected_debug_identifiers = {
            key: list(values)
            for key, values in (protected_debug_identifiers or {}).items()
        }
        super().__init__(f"{self.code}: {self.detail}")


def _fail(code: str, detail: str) -> RotationEvidenceError:
    return RotationEvidenceError(code, detail)


def _with_debug_identifiers(
    error: RotationEvidenceError,
    *,
    capture_job_id: str = "",
    request_ids: tuple[str, ...] = (),
    response_ids: tuple[str, ...] = (),
    trace_ids: tuple[str, ...] = (),
    runtime_session_ids: tuple[str, ...] = (),
) -> RotationEvidenceError:
    """Attach only already-validated exact IDs to a fixed rotation failure."""

    fields = {
        "capture_job_ids": (capture_job_id,),
        "request_ids": request_ids,
        "response_ids": response_ids,
        "trace_ids": trace_ids,
        "runtime_session_ids": runtime_session_ids,
    }
    identifiers: dict[str, list[str]] = {}
    for field, values in fields.items():
        unique = list(dict.fromkeys(value for value in values if value))
        if any(_RUNTIME_ID_RE.fullmatch(value) is None for value in unique):
            return RotationEvidenceError(error.code, error.detail)
        if unique:
            identifiers[field] = unique
    return RotationEvidenceError(
        error.code,
        error.detail,
        protected_debug_identifiers=identifiers,
    )


def _clear_history(client: Any, credentials: Any) -> None:
    try:
        status, body = client._req(
            "DELETE",
            "/v1/chat/history",
            api_key=credentials.api_key,
            body={"confirm": "clear-chat-history"},
            attempts=1,
        )
    except Exception:
        raise _fail(
            "ROTATION_TRANSCRIPT_CLEAR_FAILED",
            "The synthetic transcript could not be cleared",
        ) from None
    if status != 200 or not isinstance(body, Mapping) or body.get("cleared") is not True:
        raise _fail(
            "ROTATION_TRANSCRIPT_CLEAR_FAILED",
            "The synthetic transcript clear was not confirmed",
        )


def _thread_id_from_event(event: Mapping[str, Any]) -> str:
    if (
        event.get("type") != "agent.model.call.done"
        or event.get("status") != "ok"
        or not isinstance(event.get("detail"), Mapping)
        or event["detail"].get("driver") != "codex"
        or not isinstance(event.get("content_excerpt"), Mapping)
    ):
        return ""
    reply_head = event["content_excerpt"].get("reply_head")
    if not isinstance(reply_head, str) or not reply_head:
        return ""
    identifiers: list[str] = []
    for line in reply_head.splitlines():
        try:
            document = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(document, Mapping) or document.get("type") != "thread.started":
            continue
        identifier = str(document.get("thread_id") or "").strip()
        if _RUNTIME_ID_RE.fullmatch(identifier) is not None:
            identifiers.append(identifier)
    unique = list(dict.fromkeys(identifiers))
    return unique[0] if len(unique) == 1 else ""


def _poll_thread_id(
    client: Any,
    credentials: Any,
    request_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> str:
    deadline = monotonic() + max(0.0, float(timeout_seconds))
    first = True
    while first or monotonic() < deadline:
        first = False
        try:
            trace = client.read_trace(
                credentials,
                limit=200,
                subsystem="agent",
                attempts=1,
                read_timeout=max(0.1, min(float(timeout_seconds), 10.0)),
            )
        except Exception:
            trace = None
        if isinstance(trace, Mapping):
            if trace.get("verbose") is not True:
                raise _fail(
                    "ROTATION_VERBOSE_TRACE_REQUIRED",
                    "Verbose trace evidence is disabled for the synthetic account",
                )
            events = trace.get("events")
            if isinstance(events, list):
                matching = [
                    event
                    for event in events
                    if isinstance(event, Mapping)
                    and event.get("type") == "agent.model.call.done"
                    and str(event.get("trace_id") or "") == request_id
                ]
                if len(matching) > 1:
                    raise _fail(
                        "ROTATION_TRACE_AMBIGUOUS",
                        "The exact request matched more than one completed model call",
                    )
                if matching:
                    identifier = _thread_id_from_event(matching[0])
                    if not identifier:
                        raise _fail(
                            "ROTATION_THREAD_ID_MISSING",
                            "The exact Codex trace contains no valid thread identifier",
                        )
                    return identifier
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(max(0.0, poll_interval_seconds), remaining))
    raise _fail(
        "ROTATION_TRACE_TIMEOUT",
        "The exact Codex trace was not available before the evidence deadline",
    )


def _positive_timestamp(value: Any) -> float | None:
    if type(value) not in {int, float}:
        return None
    try:
        timestamp = float(value)
    except (OverflowError, ValueError):
        return None
    return timestamp if math.isfinite(timestamp) and timestamp > 0 else None


def _capture_window(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if isinstance(job.get("capture_window"), Mapping):
        return job["capture_window"]
    if isinstance(job.get("window"), Mapping):
        return job["window"]
    return None


def _read_proactive_jobs(
    client: Any,
    credentials: Any,
    *,
    read_timeout: float,
) -> list[Mapping[str, Any]] | None:
    try:
        status, body = client._req(
            "GET",
            "/v1/proactive/debug",
            api_key=credentials.api_key,
            attempts=1,
            read_timeout=max(0.1, min(float(read_timeout), 10.0)),
        )
    except Exception:
        return None
    if (
        status != 200
        or not isinstance(body, Mapping)
        or not isinstance(body.get("jobs"), list)
    ):
        return None
    return [job for job in body["jobs"] if isinstance(job, Mapping)]


def _exact_capture_job(
    jobs: list[Mapping[str, Any]],
    job_id: str,
) -> Mapping[str, Any] | None:
    matches = [job for job in jobs if str(job.get("job_id") or "") == job_id]
    return matches[0] if len(matches) == 1 else None


def _force_capture(
    client: Any,
    credentials: Any,
    *,
    prior_response_id: str,
) -> str:
    try:
        status, body = client._req(
            "POST",
            "/v1/capture/force",
            api_key=credentials.api_key,
            attempts=1,
        )
    except Exception:
        raise _fail(
            "ROTATION_CAPTURE_FORCE_FAILED",
            "The learned turn could not be submitted for memory capture",
        ) from None
    if status != 200 or not isinstance(body, Mapping):
        raise _fail(
            "ROTATION_CAPTURE_FORCE_UNPROVEN",
            "Memory capture was not bound to the exact learned turn",
        )
    state = body.get("state")
    job = body.get("job")
    enqueued = body.get("enqueued")
    reason = str(body.get("reason") or "").strip()
    accepted_result = (enqueued is True and reason == "enqueued") or (
        enqueued is False and reason == "capture_already_pending"
    )
    last_seen_ts = (
        _positive_timestamp(state.get("last_seen_ts"))
        if isinstance(state, Mapping)
        else None
    )
    pending_capture_key = (
        str(state.get("pending_capture_key") or "").strip()
        if isinstance(state, Mapping)
        else ""
    )
    if (
        not accepted_result
        or not isinstance(state, Mapping)
        or str(state.get("last_seen_message_id") or "") != prior_response_id
        or last_seen_ts is None
        or not pending_capture_key
    ):
        raise _fail(
            "ROTATION_CAPTURE_FORCE_UNPROVEN",
            "Memory capture was not bound to the exact learned turn",
        )

    jobs = _read_proactive_jobs(client, credentials, read_timeout=10.0)
    persisted: Mapping[str, Any] | None = None
    job_id = ""
    if isinstance(job, Mapping):
        job_id = str(job.get("job_id") or "").strip()
        capture_key = str(job.get("capture_key") or "").strip()
        capture_status = str(job.get("status") or "").strip().lower()
        if (
            not job_id
            or _RUNTIME_ID_RE.fullmatch(job_id) is None
            or job.get("job_kind") != "memory_capture"
            or capture_status not in _ACTIVE_CAPTURE_STATUSES
            or capture_key != pending_capture_key
        ):
            raise _fail(
                "ROTATION_CAPTURE_FORCE_UNPROVEN",
                "Memory capture was not bound to the exact learned turn",
            )
        persisted = _exact_capture_job(jobs, job_id) if jobs is not None else None
    elif (
        job is None
        and enqueued is False
        and reason == "capture_already_pending"
        and jobs is not None
    ):
        keyed_matches = [
            candidate
            for candidate in jobs
            if candidate.get("job_kind") == "memory_capture"
            and str(candidate.get("capture_key") or "") == pending_capture_key
        ]
        active_matches = [
            candidate
            for candidate in keyed_matches
            if str(candidate.get("status") or "").strip().lower()
            in _ACTIVE_CAPTURE_STATUSES
        ]
        if len(active_matches) == 1:
            persisted = active_matches[0]
            job_id = str(persisted.get("job_id") or "").strip()
        elif not active_matches:
            terminal_matches = [
                candidate
                for candidate in keyed_matches
                if str(candidate.get("status") or "").strip().lower()
                in _TERMINAL_CAPTURE_STATUSES
            ]
            if len(terminal_matches) == 1:
                persisted = terminal_matches[0]
                job_id = str(persisted.get("job_id") or "").strip()

    window = _capture_window(persisted) if persisted is not None else None
    if (
        persisted is None
        or not job_id
        or _RUNTIME_ID_RE.fullmatch(job_id) is None
        or persisted.get("job_kind") != "memory_capture"
        or str(persisted.get("capture_key") or "") != pending_capture_key
        or str(persisted.get("status") or "").strip().lower()
        not in (_ACTIVE_CAPTURE_STATUSES | _TERMINAL_CAPTURE_STATUSES)
        or window is None
        or str(window.get("until_message_id") or "") != prior_response_id
        or _positive_timestamp(window.get("until_ts")) != last_seen_ts
    ):
        raise _fail(
            "ROTATION_CAPTURE_FORCE_UNPROVEN",
            "Memory capture was not bound to the exact learned turn",
        )
    return job_id


def _poll_capture_commit(
    client: Any,
    credentials: Any,
    job_id: str,
    *,
    prior_response_id: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    deadline = monotonic() + max(0.0, float(timeout_seconds))
    first = True
    while first or monotonic() < deadline:
        first = False
        jobs = _read_proactive_jobs(
            client,
            credentials,
            read_timeout=max(0.1, min(float(timeout_seconds), 10.0)),
        )
        if jobs is not None:
            matches = [
                job
                for job in jobs
                if str(job.get("job_id") or "") == job_id
            ]
            if len(matches) > 1:
                raise _fail(
                    "ROTATION_CAPTURE_AMBIGUOUS",
                    "The memory capture job identifier is not unique",
                )
            if matches:
                job = matches[0]
                capture_status = str(job.get("status") or "").strip().lower()
                if capture_status in _TERMINAL_CAPTURE_STATUSES:
                    window = _capture_window(job)
                    cards_added = job.get("cards_added")
                    cards_superseded = job.get("cards_superseded")
                    valid_mutation_counts = (
                        type(cards_added) is int
                        and cards_added >= 0
                        and type(cards_superseded) is int
                        and cards_superseded >= 0
                    )
                    mutations = (
                        cards_added + cards_superseded
                        if valid_mutation_counts
                        else 0
                    )
                    if (
                        capture_status != "completed"
                        or job.get("job_kind") != "memory_capture"
                        or window is None
                        or str(window.get("until_message_id") or "")
                        != prior_response_id
                        or not valid_mutation_counts
                        or mutations < 1
                    ):
                        raise _fail(
                            "ROTATION_MEMORY_COMMIT_UNPROVEN",
                            "The learned turn did not produce a verified memory mutation",
                        )
                    return
        remaining = deadline - monotonic()
        if remaining > 0:
            sleep(min(max(0.0, poll_interval_seconds), remaining))
    raise _fail(
        "ROTATION_CAPTURE_TIMEOUT",
        "The learned turn was not persisted before the evidence deadline",
    )


def _prove_codex_runtime_session_rotation(
    client: Any,
    credentials: Any,
    prior_turn: Mapping[str, str],
    *,
    _debug_state: dict[str, str],
    capture_timeout_seconds: float = 240.0,
    trace_timeout_seconds: float = 60.0,
    reply_timeout_seconds: float = 120.0,
    poll_interval_seconds: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Mapping[str, Any]:
    """Persist the learned fact and prove a distinct Codex runtime session.

    The returned raw identifiers are consumed by ``FeedlingTarget``. Runtime
    proof remains hash-based; a separate content-free allowlist is retained in
    the owner-only trajectory solely for encrypted failure debugging.
    """

    prior_request_id = str(prior_turn.get("prior_request_id") or "").strip()
    prior_response_id = str(prior_turn.get("prior_response_id") or "").strip()
    if not prior_request_id or not prior_response_id:
        raise _fail(
            "ROTATION_PRIOR_TURN_MISSING",
            "Rotation requires one preceding exactly correlated turn",
        )
    _debug_state["prior_request_id"] = prior_request_id
    _debug_state["prior_response_id"] = prior_response_id

    before_thread_id = _poll_thread_id(
        client,
        credentials,
        prior_request_id,
        timeout_seconds=trace_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        monotonic=monotonic,
        sleep=sleep,
    )
    _debug_state["before_runtime_session_id"] = before_thread_id
    _debug_state["prior_trace_id"] = prior_request_id
    capture_job_id = _force_capture(
        client,
        credentials,
        prior_response_id=prior_response_id,
    )
    _debug_state["capture_job_id"] = capture_job_id
    _poll_capture_commit(
        client,
        credentials,
        capture_job_id,
        prior_response_id=prior_response_id,
        timeout_seconds=capture_timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        monotonic=monotonic,
        sleep=sleep,
    )

    _clear_history(client, credentials)
    probe_error: RotationEvidenceError | None = None
    after_thread_id = ""
    probe_request_id = ""
    try:
        acknowledgement = client.send(
            credentials,
            _BOUNDARY_PROBE,
            read_timeout=max(0.1, min(float(reply_timeout_seconds), 45.0)),
        )
        user_message = (
            acknowledgement.get("user_message")
            if isinstance(acknowledgement, Mapping)
            else None
        )
        if not isinstance(user_message, Mapping):
            raise _fail(
                "ROTATION_PROBE_UNCORRELATED",
                "The boundary probe did not return an exact request identifier",
            )
        probe_request_id = str(user_message.get("id") or "").strip()
        try:
            probe_ts = float(user_message["ts"])
        except (KeyError, TypeError, ValueError):
            probe_ts = 0.0
        if not probe_request_id or probe_ts <= 0:
            raise _fail(
                "ROTATION_PROBE_UNCORRELATED",
                "The boundary probe did not return exact correlation metadata",
            )
        _debug_state["probe_request_id"] = probe_request_id
        record = client.poll_reply_record(
            credentials,
            probe_ts,
            max(0.1, float(reply_timeout_seconds)),
            include_thinking=False,
            user_message_id=probe_request_id,
        )
        if not isinstance(record, Mapping) or not isinstance(record.get("message"), Mapping):
            raise _fail(
                "ROTATION_PROBE_REPLY_MISSING",
                "The boundary probe did not receive an exactly correlated reply",
            )
        probe_response_id = str(record["message"].get("id") or "").strip()
        if not probe_response_id:
            raise _fail(
                "ROTATION_PROBE_REPLY_MISSING",
                "The boundary probe reply has no exact response identifier",
            )
        _debug_state["probe_response_id"] = probe_response_id
        after_thread_id = _poll_thread_id(
            client,
            credentials,
            probe_request_id,
            timeout_seconds=trace_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            monotonic=monotonic,
            sleep=sleep,
        )
        _debug_state["after_runtime_session_id"] = after_thread_id
        _debug_state["probe_trace_id"] = probe_request_id
    except RotationEvidenceError as exc:
        probe_error = exc
    except Exception:
        probe_error = _fail(
            "ROTATION_PROBE_FAILED",
            "The controlled runtime-boundary probe failed",
        )

    try:
        _clear_history(client, credentials)
    except RotationEvidenceError:
        raise _fail(
            "ROTATION_PROBE_CLEANUP_FAILED",
            "The controlled boundary probe could not be removed",
        ) from None
    if probe_error is not None:
        raise probe_error
    if not after_thread_id or before_thread_id == after_thread_id:
        raise _fail(
            "ROTATION_SESSION_NOT_DISTINCT",
            "The before and after Codex runtime sessions are not distinct",
        )

    evidence_id = hashlib.sha256(
        json.dumps(
            {
                "after": after_thread_id,
                "before": before_thread_id,
                "capture_job": capture_job_id,
                "probe_request": probe_request_id,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return {
        "rotated": True,
        "before_runtime_session_id": before_thread_id,
        "after_runtime_session_id": after_thread_id,
        "evidence_id": evidence_id,
        "protected_debug_identifiers": {
            "capture_job_ids": [capture_job_id],
            "request_ids": [probe_request_id],
            "response_ids": [_debug_state["probe_response_id"]],
            "trace_ids": [prior_request_id, probe_request_id],
            "runtime_session_ids": [before_thread_id, after_thread_id],
        },
    }


def prove_codex_runtime_session_rotation(
    client: Any,
    credentials: Any,
    prior_turn: Mapping[str, str],
    *,
    capture_timeout_seconds: float = 240.0,
    trace_timeout_seconds: float = 60.0,
    reply_timeout_seconds: float = 120.0,
    poll_interval_seconds: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Mapping[str, Any]:
    """Prove rotation and retain an exact, content-free private debug sidecar."""

    debug_state: dict[str, str] = {}
    try:
        return _prove_codex_runtime_session_rotation(
            client,
            credentials,
            prior_turn,
            _debug_state=debug_state,
            capture_timeout_seconds=capture_timeout_seconds,
            trace_timeout_seconds=trace_timeout_seconds,
            reply_timeout_seconds=reply_timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
            monotonic=monotonic,
            sleep=sleep,
        )
    except RotationEvidenceError as exc:
        raise _with_debug_identifiers(
            exc,
            capture_job_id=debug_state.get("capture_job_id", ""),
            request_ids=(
                debug_state.get("prior_request_id", ""),
                debug_state.get("probe_request_id", ""),
            ),
            response_ids=(
                debug_state.get("prior_response_id", ""),
                debug_state.get("probe_response_id", ""),
            ),
            trace_ids=(
                debug_state.get("prior_trace_id", ""),
                debug_state.get("probe_trace_id", ""),
            ),
            runtime_session_ids=(
                debug_state.get("before_runtime_session_id", ""),
                debug_state.get("after_runtime_session_id", ""),
            ),
        ) from None
