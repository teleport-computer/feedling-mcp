"""Deterministic multi-turn state-machine runner.

This module executes versioned regression scenarios against an injectable
conversation target.  It has no LangChain/DeepEval dependency: scenario turns
form a small deterministic graph, every target response is retained as private
trajectory evidence, and infrastructure/evidence failures remain distinct from
later product evaluation failures.
"""

from __future__ import annotations

import hashlib
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from qa.regression.contracts import Scenario, Trajectory
from qa.regression.target import (
    BLOCKED_EVIDENCE,
    BOUNDARY_CLEAR_HISTORY,
    BOUNDARY_NONE,
    BOUNDARY_ROTATE_RUNTIME_SESSION,
    INFRA_ERROR,
    BoundaryResult,
    ConversationTarget,
    TargetCapabilities,
    TargetContext,
    TargetError,
    TargetResponse,
    TargetSession,
)


COMPLETED = "COMPLETED"
PERSISTENT_MEMORY_STRONG = "persistent_memory_strong"

_MISSING = object()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_CONDITIONS = frozenset(
    {
        "always",
        "contains",
        "not_contains",
        "equals",
        "regex",
        "contains_any",
        "contains_all",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _metadata(value: Any) -> dict[str, Any]:
    raw = _get(value, "metadata", {})
    return dict(raw) if isinstance(raw, Mapping) else {}


def _strings(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return {item for item in value if isinstance(item, str) and item}
    return set()


def _scenario_id(scenario: Any) -> str:
    value = _get(scenario, "scenario_id", _get(scenario, "id", ""))
    return str(value or "").strip()


def _scenario_version(scenario: Any) -> str:
    return str(_get(scenario, "version", _get(scenario, "scenario_version", "1")))


def _scenario_fingerprint(scenario: Any) -> str:
    method = getattr(scenario, "fingerprint_sha256", None)
    if callable(method):
        return str(method())
    value = _get(scenario, "scenario_sha256", _get(scenario, "fingerprint", ""))
    return str(value or "")


def _turn_id(turn: Any) -> str:
    return str(_get(turn, "turn_id", _get(turn, "id", "")) or "").strip()


def _turn_content(turn: Any) -> str:
    return str(_get(turn, "content", _get(turn, "prompt", "")) or "")


def _turn_requirements(turn: Any) -> set[str]:
    return _strings(_get(turn, "requirements", ()))


def _scenario_requirements(scenario: Any) -> set[str]:
    requirements = _strings(_get(scenario, "requirements", ()))
    for turn in _get(scenario, "turns", ()) or ():
        requirements.update(_turn_requirements(turn))
    return requirements


def _boundary_before(turn: Any) -> str:
    value = _get(turn, "boundary_before", _MISSING)
    if value is _MISSING:
        value = _metadata(turn).get("boundary_before", BOUNDARY_NONE)
    return str(value or BOUNDARY_NONE)


def _target_capabilities(target: ConversationTarget) -> TargetCapabilities:
    capabilities = target.capabilities
    if not isinstance(capabilities, TargetCapabilities):
        raise TargetError(
            "INVALID_TARGET_CAPABILITIES",
            detail="Target capabilities have an invalid shape",
        )
    return capabilities


def _strong_memory_preflight(
    scenario: Any,
    target: ConversationTarget,
) -> str | None:
    if PERSISTENT_MEMORY_STRONG not in _scenario_requirements(scenario):
        return None
    boundaries = {_boundary_before(turn) for turn in (_get(scenario, "turns", ()) or ())}
    if BOUNDARY_ROTATE_RUNTIME_SESSION not in boundaries:
        return "SESSION_BOUNDARY_UNPROVEN"
    if not _target_capabilities(target).runtime_session_rotation:
        return "SESSION_BOUNDARY_UNPROVEN"
    return None


def _transition_rows(turn: Any) -> list[Any]:
    value = _get(turn, "transitions", _MISSING)
    if value is _MISSING:
        value = _metadata(turn).get("transitions", ())
    if value is None:
        return []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    raise TargetError(
        "INVALID_SCENARIO_TRANSITION",
        detail=f"Turn {_turn_id(turn)} transitions must be a list",
    )


def _transition_target(transition: Any) -> str | None:
    for key in ("next_turn_id", "target_turn_id", "to_turn_id", "target", "next"):
        value = _get(transition, key, _MISSING)
        if value is not _MISSING:
            if value is None:
                return None
            return str(value).strip() or None
    raise TargetError(
        "INVALID_SCENARIO_TRANSITION",
        detail="Transition is missing next_turn_id",
    )


def _condition_parts(transition: Any) -> tuple[str, Any, bool]:
    condition = _get(transition, "condition", _MISSING)
    if condition is _MISSING:
        condition = _get(transition, "when", "always")
    if isinstance(condition, str):
        kind = condition
        value = _get(
            transition,
            "value",
            _get(transition, "pattern", _get(transition, "text", "")),
        )
        case_sensitive = _get(transition, "case_sensitive", False) is True
    elif isinstance(condition, Mapping) or hasattr(condition, "__dict__"):
        kind = str(
            _get(
                condition,
                "kind",
                _get(condition, "operator", _get(condition, "type", "always")),
            )
        )
        value = _get(
            condition,
            "value",
            _get(condition, "pattern", _get(condition, "text", "")),
        )
        case_sensitive = _get(condition, "case_sensitive", False) is True
    else:
        raise TargetError(
            "INVALID_SCENARIO_TRANSITION",
            detail="Transition condition has an invalid shape",
        )
    normalized_kind = kind.strip().lower()
    if normalized_kind not in _SUPPORTED_CONDITIONS:
        raise TargetError(
            "INVALID_SCENARIO_TRANSITION",
            detail=f"Unsupported transition condition {normalized_kind[:64]}",
        )
    return normalized_kind, value, case_sensitive


def _condition_matches(kind: str, value: Any, response: str, case_sensitive: bool) -> bool:
    if kind == "always":
        return True
    candidate = response if case_sensitive else response.casefold()

    def normalize(item: Any) -> str:
        text = str(item)
        return text if case_sensitive else text.casefold()

    if kind == "equals":
        return candidate == normalize(value)
    if kind == "contains":
        return normalize(value) in candidate
    if kind == "not_contains":
        return normalize(value) not in candidate
    if kind == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            return re.search(str(value), response, flags=flags) is not None
        except re.error:
            raise TargetError(
                "INVALID_SCENARIO_TRANSITION",
                detail="Transition contains an invalid regular expression",
            ) from None
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TargetError(
            "INVALID_SCENARIO_TRANSITION",
            detail=f"Transition condition {kind} requires a list value",
        )
    needles = [normalize(item) for item in value]
    if not needles:
        raise TargetError(
            "INVALID_SCENARIO_TRANSITION",
            detail=f"Transition condition {kind} requires a non-empty list",
        )
    matched = [needle in candidate for needle in needles]
    return any(matched) if kind == "contains_any" else all(matched)


def _choose_next_turn(
    turn: Any,
    *,
    response: str,
    default_next_turn_id: str | None,
) -> tuple[str | None, dict[str, Any]]:
    transitions = _transition_rows(turn)
    for position, transition in enumerate(transitions):
        kind, value, case_sensitive = _condition_parts(transition)
        if _condition_matches(kind, value, response, case_sensitive):
            target = _transition_target(transition)
            return target, {
                "source": "transition",
                "transition_index": position,
                "condition": kind,
                "next_turn_id": target,
            }
    if transitions:
        raise TargetError(
            "NO_SCENARIO_TRANSITION_MATCHED",
            detail=f"No transition matched after turn {_turn_id(turn)}",
        )

    formal = _get(turn, "next_turn_id", _MISSING)
    metadata = _metadata(turn)
    # Contract turns use ``None`` as the default (unspecified) value. Explicit
    # terminal graph edges use a Transition with next_turn_id=None or
    # metadata.terminal=true.
    if formal is not _MISSING and formal is not None:
        target = str(formal).strip() or None
        return target, {"source": "next_turn_id", "next_turn_id": target}
    if "next_turn_id" in metadata:
        raw = metadata["next_turn_id"]
        target = str(raw).strip() if raw is not None else None
        return target or None, {"source": "next_turn_id", "next_turn_id": target or None}
    if metadata.get("terminal") is True:
        return None, {"source": "terminal", "next_turn_id": None}
    return default_next_turn_id, {
        "source": "sequence",
        "next_turn_id": default_next_turn_id,
    }


def _coerce_response(value: Any) -> TargetResponse:
    if isinstance(value, TargetResponse):
        response = value
    elif isinstance(value, Mapping):
        response = TargetResponse(
            text=str(value.get("text", value.get("response", "")) or ""),
            request_id=str(value.get("request_id") or ""),
            response_id=str(value.get("response_id") or ""),
            trace_id=str(value.get("trace_id") or ""),
            latency_ms=value.get("latency_ms"),
            metadata=dict(value.get("metadata") or {}),
        )
    else:
        raise TargetError(
            "INVALID_TARGET_RESPONSE",
            status=BLOCKED_EVIDENCE,
            detail="Target response has an invalid shape",
        )
    if not isinstance(response.text, str):
        raise TargetError(
            "INVALID_TARGET_RESPONSE",
            status=BLOCKED_EVIDENCE,
            detail="Target response text is invalid",
        )
    if not str(response.request_id).strip() or not str(response.response_id).strip():
        raise TargetError(
            "RESPONSE_CORRELATION_MISSING",
            status=BLOCKED_EVIDENCE,
            detail="Target response lacks request/reply correlation",
        )
    return response


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rotation_digests(evidence: Mapping[str, Any]) -> tuple[str, str]:
    before = str(evidence.get("before_runtime_session_sha256") or "").strip()
    after = str(evidence.get("after_runtime_session_sha256") or "").strip()
    if not before and evidence.get("before_runtime_session_id"):
        before = _sha256(str(evidence["before_runtime_session_id"]))
    if not after and evidence.get("after_runtime_session_id"):
        after = _sha256(str(evidence["after_runtime_session_id"]))
    return before, after


def _safe_target_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project only non-content transport fields into private trajectory JSON."""

    result: dict[str, Any] = {}
    for key in (
        "ack_latency_ms",
        "reply_correlation",
        "trace_correlation",
        "provider",
        "model",
        "runtime_mode",
    ):
        item = value.get(key)
        if type(item) in (bool, int, float):
            result[key] = item
        elif isinstance(item, str):
            result[key] = item[:256]
    return result


def _boundary_evidence(
    *,
    turn_id: str,
    session_key: str,
    requested_action: str,
    previous_session: TargetSession,
    result: BoundaryResult,
) -> dict[str, Any]:
    if not isinstance(result, BoundaryResult):
        raise TargetError(
            "INVALID_BOUNDARY_EVIDENCE",
            status=BLOCKED_EVIDENCE,
            detail="Target boundary result has an invalid shape",
        )
    if result.action != requested_action:
        raise TargetError(
            "INVALID_BOUNDARY_EVIDENCE",
            status=BLOCKED_EVIDENCE,
            detail="Target boundary evidence does not match the requested action",
        )
    if (
        not isinstance(result.session, TargetSession)
        or result.session.target_id != previous_session.target_id
        or result.session.session_key != previous_session.session_key
        or result.session.session_id != previous_session.session_id
        or result.session.account_fingerprint != previous_session.account_fingerprint
        or result.session.generation <= previous_session.generation
    ):
        raise TargetError(
            "INVALID_BOUNDARY_EVIDENCE",
            status=BLOCKED_EVIDENCE,
            detail="Target boundary returned an invalid session generation",
        )
    if result.action == BOUNDARY_CLEAR_HISTORY:
        evidence = result.evidence if isinstance(result.evidence, Mapping) else {}
        if (
            result.boundary_kind != "transcript"
            or result.runtime_session_rotated
            or evidence.get("transcript_cleared") is not True
        ):
            raise TargetError(
                "INVALID_BOUNDARY_EVIDENCE",
                status=BLOCKED_EVIDENCE,
                detail="clear_history may prove only a transcript boundary",
            )
        deleted = evidence.get("deleted_count")
        safe_evidence: dict[str, Any] = {
            "transcript_cleared": True,
            "runtime_session_rotation_claimed": False,
        }
        if type(deleted) is int and deleted >= 0:
            safe_evidence["deleted_count"] = deleted
    else:
        safe_evidence = {}
    if result.action == BOUNDARY_ROTATE_RUNTIME_SESSION:
        evidence = result.evidence if isinstance(result.evidence, Mapping) else {}
        before_sha256, after_sha256 = _rotation_digests(evidence)
        if (
            result.boundary_kind != "runtime_session"
            or not result.runtime_session_rotated
            or not _SHA256_RE.fullmatch(before_sha256)
            or not _SHA256_RE.fullmatch(after_sha256)
            or before_sha256 == after_sha256
        ):
            raise TargetError(
                "SESSION_BOUNDARY_UNPROVEN",
                status=BLOCKED_EVIDENCE,
                detail="Runtime session rotation lacks distinct before/after evidence",
            )
        safe_evidence = {
            "rotated": True,
            "before_runtime_session_sha256": before_sha256,
            "after_runtime_session_sha256": after_sha256,
        }
        evidence_sha256 = str(evidence.get("evidence_sha256") or "").strip()
        if _SHA256_RE.fullmatch(evidence_sha256):
            safe_evidence["evidence_sha256"] = evidence_sha256
    return {
        "turn_id": turn_id,
        "session_key": session_key,
        "action": result.action,
        "boundary_kind": result.boundary_kind,
        "runtime_session_rotated": result.runtime_session_rotated,
        "generation_before": previous_session.generation,
        "generation_after": result.session.generation,
        "evidence": safe_evidence,
    }


def _trajectory_from_payload(payload: Mapping[str, Any]) -> Trajectory:
    from_dict = getattr(Trajectory, "from_dict", None)
    if callable(from_dict):
        return from_dict(dict(payload))
    return Trajectory(**dict(payload))


def _build_trajectory(
    *,
    experiment_id: str,
    trajectory_id: str,
    target_id: str,
    scenario: Any,
    repeat_index: int,
    status: str,
    failure_code: str,
    started_at: str,
    turns: Sequence[Mapping[str, Any]],
    boundaries: Sequence[Mapping[str, Any]],
    metadata: Mapping[str, Any] | None = None,
) -> Trajectory:
    return _trajectory_from_payload(
        {
            "kind": "trajectory",
            "schema_version": 1,
            "trajectory_id": trajectory_id,
            "experiment_id": experiment_id,
            "target_id": target_id,
            "scenario_id": _scenario_id(scenario),
            "scenario_version": _scenario_version(scenario),
            "scenario_sha256": _scenario_fingerprint(scenario),
            "repeat_index": repeat_index,
            "status": status,
            "failure_code": failure_code,
            "started_at": started_at,
            "finished_at": _now(),
            "turns": [dict(turn) for turn in turns],
            "boundary_evidence": [dict(item) for item in boundaries],
            "metadata": dict(metadata or {}),
        }
    )


def run_scenario(
    scenario: Scenario,
    target: ConversationTarget,
    *,
    experiment_id: str = "standalone-experiment",
    repeat_index: int = 0,
    turn_timeout_seconds: float = 120.0,
    max_turns: int = 100,
) -> Trajectory:
    """Execute one isolated scenario repeat and return private evidence.

    Target and harness exceptions are converted into explicit trajectory
    statuses.  Product semantics are intentionally not judged here; a fully
    collected trajectory is ``COMPLETED`` and is scored by evaluator modules.
    """

    if turn_timeout_seconds <= 0:
        raise ValueError("turn_timeout_seconds must be positive")
    if max_turns < 1:
        raise ValueError("max_turns must be positive")
    if repeat_index < 0:
        raise ValueError("repeat_index must be non-negative")

    started_at = _now()
    trajectory_id = f"traj-{uuid.uuid4().hex}"
    evidence_turns: list[dict[str, Any]] = []
    boundaries: list[dict[str, Any]] = []
    sessions: dict[str, TargetSession] = {}
    sent_counts: dict[str, int] = {}
    rotated_session_keys: set[str] = set()
    post_rotation_session_keys: set[str] = set()
    completed_strong_probe_session_keys: set[str] = set()
    trajectory_metadata: dict[str, Any] = {}
    status = COMPLETED
    failure_code = "NONE"
    strong_memory_required = False
    active_turn_id = ""

    try:
        scenario_id = _scenario_id(scenario)
        if not scenario_id:
            raise TargetError(
                "INVALID_SCENARIO",
                detail="Scenario id is required",
            )
        strong_memory_required = (
            PERSISTENT_MEMORY_STRONG in _scenario_requirements(scenario)
        )
        strong_failure = _strong_memory_preflight(scenario, target)
        if strong_failure:
            raise TargetError(
                strong_failure,
                status=BLOCKED_EVIDENCE,
                detail="Strong persistent-memory scenario lacks runtime session rotation proof",
            )

        ordered_turns = list(_get(scenario, "turns", ()) or ())
        if not ordered_turns:
            raise TargetError("INVALID_SCENARIO", detail="Scenario has no turns")
        by_id: dict[str, Any] = {}
        for turn in ordered_turns:
            identifier = _turn_id(turn)
            if not identifier or identifier in by_id:
                raise TargetError(
                    "INVALID_SCENARIO",
                    detail="Scenario turn ids must be non-empty and unique",
                )
            by_id[identifier] = turn
        all_session_keys = {
            str(_get(turn, "session_key", "default") or "default")
            for turn in ordered_turns
        }
        strong_turn_session_keys = {
            str(_get(turn, "session_key", "default") or "default")
            for turn in ordered_turns
            if PERSISTENT_MEMORY_STRONG in _turn_requirements(turn)
        }
        if (
            strong_memory_required
            and not strong_turn_session_keys
            and len(all_session_keys) != 1
        ):
            raise TargetError(
                "SESSION_BOUNDARY_UNPROVEN",
                status=BLOCKED_EVIDENCE,
                detail="Multi-session strong memory scenario must mark its probe turns",
            )
        required_strong_session_keys = strong_turn_session_keys or all_session_keys

        entry_turn_id = str(
            _get(
                scenario,
                "entry_turn_id",
                _metadata(scenario).get("entry_turn_id", _turn_id(ordered_turns[0])),
            )
            or ""
        ).strip()
        if entry_turn_id not in by_id:
            raise TargetError(
                "INVALID_SCENARIO",
                detail="Scenario entry turn does not exist",
            )

        current_id: str | None = entry_turn_id
        executed = 0
        while current_id is not None:
            active_turn_id = current_id
            if executed >= max_turns:
                raise TargetError(
                    "SCENARIO_MAX_TURNS_EXCEEDED",
                    detail="Scenario exceeded the runner turn limit",
                )
            turn = by_id.get(current_id)
            if turn is None:
                raise TargetError(
                    "INVALID_SCENARIO_TRANSITION",
                    detail="Transition points to an unknown turn",
                )
            role = str(_get(turn, "role", "user") or "user").strip().lower()
            if role != "user":
                raise TargetError(
                    "UNSUPPORTED_SCENARIO_ROLE",
                    detail="Runner sends only user turns",
                )
            session_key = str(_get(turn, "session_key", "default") or "default")
            if session_key not in sessions:
                sessions[session_key] = target.open_session(
                    TargetContext(
                        run_id=trajectory_id,
                        scenario_id=scenario_id,
                        repeat_index=repeat_index,
                        session_key=session_key,
                    )
                )
                if not isinstance(sessions[session_key], TargetSession):
                    raise TargetError(
                        "INVALID_TARGET_SESSION",
                        detail="Target open_session returned an invalid session",
                    )
                opened = sessions[session_key]
                if (
                    opened.target_id != str(target.target_id)
                    or opened.session_key != session_key
                    or not opened.session_id
                ):
                    raise TargetError(
                        "INVALID_TARGET_SESSION",
                        status=BLOCKED_EVIDENCE,
                        detail="Target session identity does not match the request",
                    )

            boundary = _boundary_before(turn)
            if boundary not in {
                BOUNDARY_NONE,
                BOUNDARY_CLEAR_HISTORY,
                BOUNDARY_ROTATE_RUNTIME_SESSION,
            }:
                raise TargetError(
                    "UNSUPPORTED_BOUNDARY",
                    status=BLOCKED_EVIDENCE,
                    detail="Scenario requested an unsupported boundary action",
                )
            if boundary != BOUNDARY_NONE:
                capabilities = _target_capabilities(target)
                if boundary == BOUNDARY_CLEAR_HISTORY and not capabilities.clear_history:
                    raise TargetError(
                        "TRANSCRIPT_BOUNDARY_UNSUPPORTED",
                        status=BLOCKED_EVIDENCE,
                        detail="Target cannot clear transcript history",
                    )
                if (
                    boundary == BOUNDARY_ROTATE_RUNTIME_SESSION
                    and not capabilities.runtime_session_rotation
                ):
                    raise TargetError(
                        "SESSION_BOUNDARY_UNPROVEN",
                        status=BLOCKED_EVIDENCE,
                        detail="Target cannot prove runtime session rotation",
                    )
                if (
                    boundary == BOUNDARY_ROTATE_RUNTIME_SESSION
                    and strong_memory_required
                    and sent_counts.get(session_key, 0) < 1
                ):
                    raise TargetError(
                        "SESSION_BOUNDARY_UNPROVEN",
                        status=BLOCKED_EVIDENCE,
                        detail="Runtime rotation occurred before any turn in this session",
                    )
                previous = sessions[session_key]
                result = target.apply_boundary(previous, action=boundary)
                boundary_row = _boundary_evidence(
                    turn_id=current_id,
                    session_key=session_key,
                    requested_action=boundary,
                    previous_session=previous,
                    result=result,
                )
                boundaries.append(boundary_row)
                if boundary_row["runtime_session_rotated"] is True:
                    rotated_session_keys.add(session_key)
                sessions[session_key] = result.session

            metadata = _metadata(turn)
            timeout = metadata.get("timeout_seconds", turn_timeout_seconds)
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
                raise TargetError(
                    "INVALID_SCENARIO",
                    detail="Turn timeout must be positive",
                )
            turn_started = time.monotonic()
            response = _coerce_response(
                target.send(
                    sessions[session_key],
                    turn_id=current_id,
                    prompt=_turn_content(turn),
                    timeout_seconds=float(timeout),
                )
            )
            elapsed_ms = (time.monotonic() - turn_started) * 1000.0
            sent_counts[session_key] = sent_counts.get(session_key, 0) + 1
            if session_key in rotated_session_keys:
                post_rotation_session_keys.add(session_key)
                if PERSISTENT_MEMORY_STRONG in _turn_requirements(turn):
                    completed_strong_probe_session_keys.add(session_key)
            executed += 1
            evidence_turns.append(
                {
                    "turn_id": current_id,
                    "turn_index": executed,
                    "role": "assistant",
                    "prompt": _turn_content(turn),
                    "response": response.text,
                    "session_key": session_key,
                    "session_id": sessions[session_key].session_id,
                    "session_generation": sessions[session_key].generation,
                    "boundary_before": boundary,
                    "request_id": response.request_id,
                    "response_id": response.response_id,
                    "trace_id": response.trace_id,
                    "latency_ms": elapsed_ms,
                    "next_turn_id": None,
                    "metadata": {
                        "target": _safe_target_metadata(
                            response.metadata
                            if isinstance(response.metadata, Mapping)
                            else {}
                        ),
                        "transition": {"source": "pending", "next_turn_id": None},
                    },
                }
            )
            if elapsed_ms > float(timeout) * 1000.0:
                raise TargetError(
                    "TARGET_TIMEOUT",
                    detail="Target exceeded the cooperative turn timeout",
                )
            index = ordered_turns.index(turn)
            default_next = (
                _turn_id(ordered_turns[index + 1])
                if index + 1 < len(ordered_turns)
                else None
            )
            next_turn_id, transition = _choose_next_turn(
                turn,
                response=response.text,
                default_next_turn_id=default_next,
            )
            if next_turn_id is not None and next_turn_id not in by_id:
                raise TargetError(
                    "INVALID_SCENARIO_TRANSITION",
                    detail="Transition points to an unknown turn",
                )
            evidence_turns[-1]["next_turn_id"] = next_turn_id
            evidence_turns[-1]["metadata"]["transition"] = transition
            current_id = next_turn_id
        observed_strong_sessions = (
            completed_strong_probe_session_keys
            if strong_turn_session_keys
            else post_rotation_session_keys
        )
        if strong_memory_required and not required_strong_session_keys.issubset(
            observed_strong_sessions
        ):
            raise TargetError(
                "SESSION_BOUNDARY_UNPROVEN",
                status=BLOCKED_EVIDENCE,
                detail="Strong persistent-memory path did not observe runtime rotation",
            )
    except TargetError as exc:
        status = exc.status
        failure_code = exc.code
        if active_turn_id:
            trajectory_metadata["failed_turn_id"] = active_turn_id
    except Exception:
        status = INFRA_ERROR
        failure_code = "UNEXPECTED_RUNNER_ERROR"
        if active_turn_id:
            trajectory_metadata["failed_turn_id"] = active_turn_id
    finally:
        close_failed = False
        for session in list(sessions.values()):
            try:
                target.close_session(session)
            except Exception:
                close_failed = True
        if close_failed:
            if status != COMPLETED:
                trajectory_metadata["primary_status"] = status
                trajectory_metadata["primary_failure_code"] = failure_code
            status = INFRA_ERROR
            failure_code = "SESSION_CLOSE_FAILED"

    return _build_trajectory(
        experiment_id=experiment_id,
        trajectory_id=trajectory_id,
        target_id=str(target.target_id),
        scenario=scenario,
        repeat_index=repeat_index,
        status=status,
        failure_code=failure_code,
        started_at=started_at,
        turns=evidence_turns,
        boundaries=boundaries,
        metadata=trajectory_metadata,
    )


class RegressionRunner:
    """Run scenario repeats concurrently with stable result ordering."""

    def __init__(
        self,
        target: ConversationTarget,
        *,
        max_concurrency: int = 3,
        turn_timeout_seconds: float = 120.0,
        max_turns: int = 100,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.target = target
        self.max_concurrency = int(max_concurrency)
        self.turn_timeout_seconds = float(turn_timeout_seconds)
        self.max_turns = int(max_turns)

    def run(
        self,
        scenarios: Sequence[Scenario],
        *,
        repeats: int = 3,
        experiment_id: str | None = None,
    ) -> list[Trajectory]:
        if repeats < 1:
            raise ValueError("repeats must be positive")
        active_experiment_id = str(
            experiment_id or f"experiment-{uuid.uuid4().hex}"
        )
        jobs = [
            (scenario, repeat_index)
            for scenario in scenarios
            for repeat_index in range(repeats)
        ]
        if not jobs:
            return []
        results: list[Trajectory | None] = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=min(self.max_concurrency, len(jobs))) as pool:
            future_indexes = {
                pool.submit(
                    run_scenario,
                    scenario,
                    self.target,
                    experiment_id=active_experiment_id,
                    repeat_index=repeat_index,
                    turn_timeout_seconds=self.turn_timeout_seconds,
                    max_turns=self.max_turns,
                ): index
                for index, (scenario, repeat_index) in enumerate(jobs)
            }
            for future in as_completed(future_indexes):
                index = future_indexes[future]
                try:
                    results[index] = future.result()
                except Exception:
                    # ``run_scenario`` is fail-closed, but preserve the batch even
                    # if trajectory serialization itself unexpectedly fails.
                    scenario, repeat_index = jobs[index]
                    results[index] = _build_trajectory(
                        experiment_id=active_experiment_id,
                        trajectory_id=f"traj-{uuid.uuid4().hex}",
                        target_id=str(self.target.target_id),
                        scenario=scenario,
                        repeat_index=repeat_index,
                        status=INFRA_ERROR,
                        failure_code="UNEXPECTED_WORKER_ERROR",
                        started_at=_now(),
                        turns=[],
                        boundaries=[],
                    )
        return [result for result in results if result is not None]


def run_scenarios(
    scenarios: Sequence[Scenario],
    target: ConversationTarget,
    *,
    repeats: int = 3,
    max_concurrency: int = 3,
    experiment_id: str | None = None,
    turn_timeout_seconds: float = 120.0,
    max_turns: int = 100,
) -> list[Trajectory]:
    """Convenience wrapper around :class:`RegressionRunner`."""

    return RegressionRunner(
        target,
        max_concurrency=max_concurrency,
        turn_timeout_seconds=turn_timeout_seconds,
        max_turns=max_turns,
    ).run(scenarios, repeats=repeats, experiment_id=experiment_id)
