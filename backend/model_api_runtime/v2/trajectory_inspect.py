"""Audited, runner-local break-glass inspection for one encrypted trajectory.

This module is intentionally not an HTTP route.  It may be invoked only inside
the trusted Runtime V2 runner and emits plaintext solely to its invoking
terminal after the durable access audit records success.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import re
import sys
from typing import Any, Callable
import uuid

from model_api_runtime.v2 import trajectory


_TRUE = frozenset({"1", "true", "yes", "on"})
_REASON_CODES = frozenset({"incident", "support", "security", "debug"})
_OPERATOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@:-]{2,79}$")
_CASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/#-]{2,119}$")
_PAGE_SIZE = 256


class TrajectoryInspectError(RuntimeError):
    """Stable, content-free failure safe to show to an operator."""

    def __init__(self, code: str, *, access_id: str = "") -> None:
        super().__init__(code)
        self.code = str(code)
        self.access_id = str(access_id)


@dataclass(frozen=True)
class InspectDeps:
    source_job: Callable[[int, str], dict | None]
    capture_state: Callable[[int, str], dict]
    list_events: Callable[..., list[dict]]
    append_audit: Callable[..., None]
    authorize_success: Callable[..., bool]
    mint_token: Callable[[str], str]
    decrypt: Callable[[str, dict, str], bytes]


def _enabled() -> bool:
    return os.environ.get(
        "FEEDLING_V2_TRAJECTORY_INSPECT_ENABLED", "0"
    ).strip().lower() in _TRUE


def _max_events() -> int:
    raw = os.environ.get("FEEDLING_V2_TRAJECTORY_INSPECT_MAX_EVENTS", "4096")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise TrajectoryInspectError("invalid_inspect_configuration") from exc
    if not 1 <= value <= 100_000:
        raise TrajectoryInspectError("invalid_inspect_configuration")
    return value


def _max_decoded_bytes() -> int:
    raw = os.environ.get(
        "FEEDLING_V2_TRAJECTORY_INSPECT_MAX_DECODED_BYTES",
        str(32 * 1024 * 1024),
    )
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise TrajectoryInspectError("invalid_inspect_configuration") from exc
    if not 1 <= value <= 256 * 1024 * 1024:
        raise TrajectoryInspectError("invalid_inspect_configuration")
    return value


def _validate_request(
    *,
    user_id: str,
    job_id: int,
    operator_id: str,
    reason_code: str,
    case_ref: str,
) -> tuple[str, int, str, str, str]:
    user = str(user_id or "").strip()
    operator = str(operator_id or "").strip()
    reason = str(reason_code or "").strip().lower()
    case = str(case_ref or "").strip()
    if not user or len(user) > 200:
        raise TrajectoryInspectError("invalid_user_id")
    try:
        job = int(job_id)
    except (TypeError, ValueError) as exc:
        raise TrajectoryInspectError("invalid_job_id") from exc
    if job <= 0:
        raise TrajectoryInspectError("invalid_job_id")
    if _OPERATOR_RE.fullmatch(operator) is None:
        raise TrajectoryInspectError("invalid_operator_id")
    if reason not in _REASON_CODES:
        raise TrajectoryInspectError("invalid_reason_code")
    if _CASE_RE.fullmatch(case) is None:
        raise TrajectoryInspectError("invalid_case_ref")
    return user, job, operator, reason, case


def _production_deps() -> InspectDeps:
    from core import enclave as core_enclave
    from core import runtime_token
    from model_api_runtime.v2 import jobs_store

    def mint(user_id: str) -> str:
        secret = os.environ.get(
            "FEEDLING_RUNTIME_TOKEN_SECRET", ""
        ).strip().encode("utf-8")
        if not secret:
            raise TrajectoryInspectError("runtime_token_unavailable")
        return runtime_token.mint(
            secret,
            user_id=user_id,
            runtime_instance_id="v2-trajectory-inspector",
            scope=["envelope_decrypt"],
            ttl=900.0,
        )

    def decrypt(user_id: str, envelope: dict, token: str) -> bytes:
        if str(envelope.get("owner_user_id") or "") != user_id:
            raise TrajectoryInspectError("trajectory_owner_mismatch")
        return core_enclave._decrypt_envelope_via_enclave(
            envelope,
            None,
            purpose="runtime_v2_trajectory_break_glass",
            runtime_token=token,
        )

    return InspectDeps(
        source_job=jobs_store.get_trajectory_source_job,
        capture_state=jobs_store.get_trajectory_capture_state,
        list_events=jobs_store.list_trajectory_events,
        append_audit=jobs_store.append_trajectory_access_audit,
        authorize_success=jobs_store.authorize_trajectory_inspection_success,
        mint_token=mint,
        decrypt=decrypt,
    )


def _audit(
    deps: InspectDeps,
    *,
    access_id: str,
    phase: str,
    user_id: str,
    job_id: int,
    operator_id: str,
    reason_code: str,
    case_ref: str,
    event_count: int | None,
    result_code: str,
) -> None:
    deps.append_audit(
        access_id=access_id,
        phase=phase,
        user_id=user_id,
        job_id=job_id,
        operator_id=operator_id,
        reason_code=reason_code,
        case_ref=case_ref,
        event_count=event_count,
        result_code=result_code,
    )


def inspect_trajectory(
    *,
    user_id: str,
    job_id: int,
    operator_id: str,
    reason_code: str,
    case_ref: str,
    deps: InspectDeps | None = None,
    max_events: int | None = None,
    max_decoded_bytes: int | None = None,
) -> dict[str, Any]:
    """Decrypt exactly one user's one job after durable audit authorization."""
    if not _enabled():
        raise TrajectoryInspectError("trajectory_inspection_disabled")
    user, job, operator, reason, case = _validate_request(
        user_id=user_id,
        job_id=job_id,
        operator_id=operator_id,
        reason_code=reason_code,
        case_ref=case_ref,
    )
    event_limit = _max_events() if max_events is None else int(max_events)
    if not 1 <= event_limit <= 100_000:
        raise TrajectoryInspectError("invalid_inspect_configuration")
    decoded_byte_limit = (
        _max_decoded_bytes()
        if max_decoded_bytes is None
        else int(max_decoded_bytes)
    )
    if not 1 <= decoded_byte_limit <= 256 * 1024 * 1024:
        raise TrajectoryInspectError("invalid_inspect_configuration")
    runtime = deps or _production_deps()
    access_id = str(uuid.uuid4())

    try:
        _audit(
            runtime,
            access_id=access_id,
            phase="requested",
            user_id=user,
            job_id=job,
            operator_id=operator,
            reason_code=reason,
            case_ref=case,
            event_count=None,
            result_code="pending",
        )
    except Exception:
        raise TrajectoryInspectError("trajectory_access_audit_unavailable") from None

    try:
        source = runtime.source_job(job, user)
        if source is None:
            raise TrajectoryInspectError("trajectory_source_not_found")
        token = runtime.mint_token(user)
        physical: list[dict[str, Any]] = []
        materialized_json_bytes = 0
        logical_json_bytes = 0
        chunk_documents: dict[str, int] = {}
        after_index = -1
        expected_index = 0
        while True:
            rows = runtime.list_events(
                job,
                user,
                after_index=after_index,
                limit=_PAGE_SIZE,
            )
            if not rows:
                break
            if len(physical) + len(rows) > event_limit:
                raise TrajectoryInspectError("trajectory_event_limit_exceeded")
            for row in rows:
                event_index = int(row["event_index"])
                if event_index != expected_index:
                    raise TrajectoryInspectError("trajectory_event_frontier_gap")
                try:
                    decoded = trajectory.decode_payload(
                        runtime.decrypt(user, dict(row["payload_envelope"]), token)
                    )
                except TrajectoryInspectError:
                    raise
                except Exception:
                    raise TrajectoryInspectError("trajectory_decrypt_failed") from None
                try:
                    decoded_size = len(
                        json.dumps(
                            decoded,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                except Exception:
                    raise TrajectoryInspectError("trajectory_decode_failed") from None
                materialized_json_bytes += decoded_size
                if decoded.get("schema") == "feedling.runtime_v2.trajectory_chunk.v1":
                    document_id = str(
                        decoded.get("document_id")
                        or decoded.get("document_sha256")
                        or ""
                    )
                    try:
                        declared_size = int(decoded.get("original_json_bytes") or 0)
                    except (TypeError, ValueError):
                        raise TrajectoryInspectError("trajectory_decode_failed") from None
                    if not document_id or declared_size < 1:
                        raise TrajectoryInspectError("trajectory_decode_failed")
                    prior_size = chunk_documents.get(document_id)
                    if prior_size is None:
                        chunk_documents[document_id] = declared_size
                        logical_json_bytes += declared_size
                    elif prior_size != declared_size:
                        raise TrajectoryInspectError("trajectory_decode_failed")
                else:
                    logical_json_bytes += decoded_size
                if (
                    materialized_json_bytes > decoded_byte_limit
                    or logical_json_bytes > decoded_byte_limit
                ):
                    raise TrajectoryInspectError("trajectory_decoded_byte_limit_exceeded")
                decoded["event_index"] = event_index
                decoded["capture_truncated"] = bool(row.get("truncated"))
                physical.append(decoded)
                expected_index += 1
                after_index = event_index
            if len(rows) < _PAGE_SIZE:
                break
        if not physical:
            raise TrajectoryInspectError("trajectory_missing")
        state = runtime.capture_state(job, user)
        if int(state.get("next_event_index") or 0) != expected_index:
            raise TrajectoryInspectError("trajectory_event_frontier_gap")
        try:
            logical = trajectory.reassemble_payload_parts(physical)
        except Exception:
            raise TrajectoryInspectError("trajectory_decode_failed") from None
        result = {
            "schema": "feedling.runtime_v2.trajectory_inspection.v1",
            "access_id": access_id,
            "user_id": user,
            "job_id": job,
            "source": {
                "lane": str(source.get("lane") or ""),
                "status": str(source.get("status") or ""),
            },
            "capture": state,
            "events": logical,
        }
        authorized = runtime.authorize_success(
            access_id=access_id,
            user_id=user,
            job_id=job,
            operator_id=operator,
            reason_code=reason,
            case_ref=case,
            event_count=len(physical),
            expected_next_event_index=expected_index,
        )
        if not authorized:
            raise TrajectoryInspectError("trajectory_source_changed")
        return result
    except Exception as exc:
        code = (
            exc.code
            if isinstance(exc, TrajectoryInspectError)
            else "trajectory_inspect_failed"
        )
        try:
            _audit(
                runtime,
                access_id=access_id,
                phase="failed",
                user_id=user,
                job_id=job,
                operator_id=operator,
                reason_code=reason,
                case_ref=case,
                event_count=None,
                result_code=code,
            )
        except Exception:
            raise TrajectoryInspectError(
                "trajectory_access_audit_unavailable", access_id=access_id
            ) from None
        raise TrajectoryInspectError(code, access_id=access_id) from None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audited runner-local inspection of one Runtime V2 trajectory."
    )
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--job-id", required=True, type=int)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--reason-code", required=True, choices=sorted(_REASON_CODES))
    parser.add_argument("--case-ref", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = inspect_trajectory(
            user_id=args.user_id,
            job_id=args.job_id,
            operator_id=args.operator_id,
            reason_code=args.reason_code,
            case_ref=args.case_ref,
        )
    except TrajectoryInspectError as exc:
        print(
            json.dumps(
                {"error": exc.code, "access_id": exc.access_id},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
