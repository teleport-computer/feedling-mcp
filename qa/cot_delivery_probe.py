#!/usr/bin/env python3
"""Capture a sanitized, turn-bound reasoning-delivery receipt.

This helper is deterministic.  It does not decide whether prose is persuasive
and it never persists reply text, thinking text, ciphertext, or raw traces.  It
binds one exact hosted-chat turn to the model-call trace, parsed-agent trace,
stored reply trace, and the separately encrypted thinking envelope returned by
chat history.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools.provider_smoke.client import (  # noqa: E402
    Session,
    SmokeClient,
    SmokeError,
    decrypt_reply_record,
)


LOCKED_BASE_URL = "https://test-api.feedling.app"
RECEIPT_SCHEMA_VERSION = 2
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
TRACE_POLL_SECONDS = 20.0
TRACE_POLL_INTERVAL_SECONDS = 1.0
TRACE_READ_TIMEOUT_CAP_SECONDS = 10.0
CONFIG_READ_ATTEMPTS = 2
CONFIG_READ_TIMEOUT_SECONDS = 15.0
CONFIG_READ_RETRY_BACKOFF_SECONDS = 2.0
CHAT_SEND_TIMEOUT_SECONDS = 45.0
DEFAULT_REPLY_POLL_SECONDS = 120.0
MAX_REPLY_POLL_SECONDS = 180.0
PARENT_COT_TIMEOUT_SECONDS = 300.0
AGENT_COT_WAIT_SECONDS = 360.0
EXPECTED_REASONING_EFFORT = "medium"
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_FAILURE_CODES = frozenset(
    {
        "NONE",
        "CHAT_TIMEOUT",
        "CHAT_REQUEST_FAILED",
        "FINAL_ANSWER_WRONG",
        "MODEL_REASONING_NOT_OBSERVED",
        "TRACE_UNAVAILABLE",
        "TRACE_AMBIGUOUS",
        "DOWNSTREAM_PARSE_DROPPED_REASONING",
        "THINKING_ENVELOPE_NOT_DELIVERED",
        "THINKING_ENVELOPE_UNREADABLE",
        "THINKING_METADATA_INVALID",
    }
)


class CotProbeError(RuntimeError):
    """A fixed, non-sensitive probe setup failure."""


def _request_worst_case_seconds(
    *, attempts: int, read_timeout: float
) -> float:
    """Bound one ``SmokeClient._req`` including its linear retry sleeps."""

    retry_sleeps = CONFIG_READ_RETRY_BACKOFF_SECONDS * sum(
        range(1, attempts)
    )
    return attempts * read_timeout + retry_sleeps


COT_PROBE_WORST_CASE_SECONDS = (
    _request_worst_case_seconds(
        attempts=CONFIG_READ_ATTEMPTS,
        read_timeout=CONFIG_READ_TIMEOUT_SECONDS,
    )
    + CHAT_SEND_TIMEOUT_SECONDS
    + MAX_REPLY_POLL_SECONDS
    + TRACE_POLL_SECONDS
)
if not (
    COT_PROBE_WORST_CASE_SECONDS < PARENT_COT_TIMEOUT_SECONDS
    < AGENT_COT_WAIT_SECONDS
):
    raise RuntimeError("COT qualification timeout hierarchy is invalid")


def _owned_private_file(path: Path, label: str, *, max_bytes: int) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise CotProbeError(f"{label} is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise CotProbeError(f"{label} is unavailable") from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size <= 0
            or metadata.st_size > max_bytes
        ):
            raise CotProbeError(f"{label} is unsafe")
        chunks: list[bytes] = []
        remaining = metadata.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise CotProbeError(f"{label} changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise CotProbeError(f"{label} changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _decode_key(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise CotProbeError(f"manifest {label} is invalid")
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        raise CotProbeError(f"manifest {label} is invalid") from None
    if len(raw) != 32:
        raise CotProbeError(f"manifest {label} is invalid")
    return raw


def _load_profile(
    manifest_path: Path,
    expected_profile_id: str | None,
    *,
    require_cot_context: bool,
) -> tuple[dict[str, Any], str, Session]:
    """Load one provisioned profile and optionally require COT route context."""
    raw = _owned_private_file(
        manifest_path, "one-profile manifest", max_bytes=MAX_MANIFEST_BYTES
    )
    try:
        document = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError):
        raise CotProbeError("one-profile manifest is invalid") from None
    profiles = document.get("profiles") if isinstance(document, dict) else None
    if (
        document.get("schema_version") != 1
        or not isinstance(profiles, list)
        or len(profiles) != 1
        or not isinstance(profiles[0], dict)
    ):
        raise CotProbeError("one-profile manifest is invalid")
    profile = dict(profiles[0])
    profile_id = str(profile.get("profile_id") or "")
    if expected_profile_id and profile_id != expected_profile_id:
        raise CotProbeError("one-profile manifest assignment does not match")
    user_id = str(profile.get("user_id") or "")
    api_key = str(profile.get("api_key") or "")
    base_url = str(document.get("base_url") or "")
    if (
        not profile_id
        or not user_id
        or not api_key
        or base_url != LOCKED_BASE_URL
        or profile.get("provision_status") != "ready"
    ):
        raise CotProbeError("one-profile manifest is not ready")
    if require_cot_context and (
        not str(profile.get("provider") or "").strip()
        or not str(profile.get("configured_model") or "").strip()
        or not isinstance(profile.get("configured_base_url"), str)
        or str(profile.get("reasoning_effort") or "").strip().lower()
        != EXPECTED_REASONING_EFFORT
    ):
        raise CotProbeError("one-profile manifest is not ready")
    return (
        profile,
        base_url,
        Session(
            user_id=user_id,
            api_key=api_key,
            sk=_decode_key(profile.get("secret_key_b64"), "secret key"),
            pk=_decode_key(profile.get("public_key_b64"), "public key"),
        ),
    )


def load_profile_context(
    manifest_path: Path, expected_profile_id: str | None = None
) -> tuple[dict[str, Any], str, Session]:
    """Load the COT route context plus its private authenticated session."""

    return _load_profile(
        manifest_path,
        expected_profile_id,
        require_cot_context=True,
    )


def load_profile_session(
    manifest_path: Path, expected_profile_id: str | None = None
) -> tuple[str, str, Session]:
    """Compatibility wrapper returning no profile document or credentials."""

    profile, base_url, session = _load_profile(
        manifest_path,
        expected_profile_id,
        require_cot_context=False,
    )
    return str(profile["profile_id"]), base_url, session


def read_configured_effort(
    client: SmokeClient, session: Session, profile: Mapping[str, Any]
) -> tuple[str, bool, bool]:
    """Read the persisted effort from the authenticated active-route endpoint.

    The booleans attest the effort field and whether the live route matches the
    manifest. Transport/auth/malformed responses stay unattested; they never
    suppress the delivery probe or masquerade as a product configuration.
    """

    try:
        status, body = client._req(
            "GET",
            "/v1/model_api/get",
            api_key=session.api_key,
            attempts=CONFIG_READ_ATTEMPTS,
            read_timeout=CONFIG_READ_TIMEOUT_SECONDS,
        )
    except Exception:  # External evidence failure must remain unavailable, not fatal.
        return "unknown", False, False
    if status != 200 or not isinstance(body, Mapping):
        return "unknown", False, False
    config = body.get("config")
    if not isinstance(config, Mapping):
        return "unknown", False, False
    if config.get("configured") is not True:
        return "unknown", False, False

    raw = config.get("reasoning_effort")
    value = str(raw).strip().lower() if isinstance(raw, (str, int)) else ""
    if value in {"off", "minimal", "low", "medium", "high", "xhigh"}:
        effort = value
    elif value:
        effort = "unsupported"
    else:
        return "unknown", False, False
    route_matches = bool(
        config.get("provider") == profile.get("provider")
        and config.get("model") == profile.get("configured_model")
        and config.get("base_url") == profile.get("configured_base_url")
    )
    return effort, True, route_matches


def _matching_events(
    events: Sequence[Mapping[str, Any]], event_type: str, trace_id: str
) -> list[Mapping[str, Any]]:
    return [
        event
        for event in events
        if event.get("type") == event_type and event.get("trace_id") == trace_id
    ]


def _explicit_reasoning_token_count(detail: Mapping[str, Any]) -> int | None:
    """Return one unambiguous provider-reported reasoning-token count.

    Ordinary input/output/total token counters are deliberately ignored.  The
    accepted paths are explicit reasoning/thinking counters used by OpenAI-
    compatible, Anthropic-style, and normalized provider usage envelopes.  If
    a trace carries conflicting explicit counters, the evidence is ambiguous
    and therefore remains unverified.
    """

    containers: list[Mapping[str, Any]] = [detail]
    for key in (
        "usage",
        "model_usage",
        "usage_metadata",
        "usageMetadata",
        "token_usage",
    ):
        value = detail.get(key)
        if isinstance(value, Mapping):
            containers.append(value)

    candidates: list[int] = []

    def add(value: object) -> None:
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ):
            candidates.append(value)

    for container in containers:
        for key in (
            "reasoning_tokens",
            "reasoning_token_count",
            "thinking_tokens",
            "thinking_token_count",
            "thoughtsTokenCount",
            "thoughts_token_count",
        ):
            add(container.get(key))
        for key in (
            "output_tokens_details",
            "completion_tokens_details",
            "output_token_details",
        ):
            details = container.get(key)
            if not isinstance(details, Mapping):
                continue
            for nested_key in (
                "reasoning_tokens",
                "reasoning",
                "thinking_tokens",
                "thinking",
            ):
                add(details.get(nested_key))

    distinct = set(candidates)
    return candidates[0] if len(distinct) == 1 else None


def correlate_trace(
    events: Sequence[Mapping[str, Any]],
    *,
    trace_id: str,
    reply_message_id: str,
    turn_started_at: float,
) -> dict[str, Any]:
    """Reduce raw trace events to bounded correlation facts."""
    model_calls = [
        event
        for event in _matching_events(events, "agent.model.call.done", trace_id)
        if event.get("status") in {None, "", "ok"}
    ]
    agent_replies = _matching_events(events, "agent.reply", trace_id)
    chat_responses = _matching_events(events, "chat.response", trace_id)
    response_matches = [
        event
        for event in chat_responses
        if isinstance(event.get("detail"), dict)
        and event["detail"].get("msg_id") == reply_message_id
    ]
    dropped = any(
        event.get("type") == "debug_trace.dropped"
        and float(event.get("ts") or 0) >= turn_started_at - 1.0
        for event in events
    )
    model_detail = (
        model_calls[0].get("detail")
        if len(model_calls) == 1 and isinstance(model_calls[0].get("detail"), dict)
        else {}
    )
    reply_detail = (
        agent_replies[0].get("detail")
        if len(agent_replies) == 1
        and isinstance(agent_replies[0].get("detail"), dict)
        else {}
    )
    model_duration = model_calls[0].get("dur_ms") if len(model_calls) == 1 else None
    api_duration = model_detail.get("api_ms") if model_detail else None
    reasoning_token_count = (
        _explicit_reasoning_token_count(model_detail)
        if len(model_calls) == 1
        else None
    )
    return {
        "dropped": dropped,
        "model_call_count": len(model_calls),
        "agent_reply_count": len(agent_replies),
        "chat_response_count": len(chat_responses),
        "chat_response_match_count": len(response_matches),
        "model_thinking_present": model_detail.get("thinking_present") is True,
        "model_thinking_len": (
            int(model_detail.get("thinking_len"))
            if isinstance(model_detail.get("thinking_len"), int)
            and not isinstance(model_detail.get("thinking_len"), bool)
            and model_detail.get("thinking_len") >= 0
            else 0
        ),
        "model_thinking_source": str(model_detail.get("thinking_source") or "")[:80],
        "agent_reply_thinking_kind": str(reply_detail.get("thinking_kind") or "")[:64],
        "model_duration_ms": (
            float(model_duration)
            if isinstance(model_duration, (int, float))
            and not isinstance(model_duration, bool)
            and model_duration >= 0
            else None
        ),
        "provider_api_duration_ms": (
            float(api_duration)
            if isinstance(api_duration, (int, float))
            and not isinstance(api_duration, bool)
            and api_duration >= 0
            else None
        ),
        "token_metadata_status": (
            "PRESENT" if reasoning_token_count is not None else "UNVERIFIED"
        ),
        "reasoning_token_count": reasoning_token_count,
    }


def classify_delivery(
    trace: Mapping[str, Any],
    reply: Mapping[str, Any] | None,
    *,
    decrypt_error: bool = False,
    final_answer_correct: bool = True,
) -> tuple[str, str]:
    """Classify only the observable reasoning-delivery contract."""
    ambiguous = (
        trace.get("dropped") is True
        or trace.get("model_call_count") != 1
        or trace.get("agent_reply_count") != 1
        or trace.get("chat_response_count") != 1
        or trace.get("chat_response_match_count") != 1
    )
    if ambiguous:
        return "UNVERIFIED", "TRACE_AMBIGUOUS"
    positive_model_trace = (
        trace.get("model_thinking_present") is True
        and isinstance(trace.get("model_thinking_len"), int)
        and trace.get("model_thinking_len", 0) > 0
    )
    if not positive_model_trace:
        return "UNVERIFIED", "MODEL_REASONING_NOT_OBSERVED"
    if not str(trace.get("agent_reply_thinking_kind") or ""):
        return "FAIL", "DOWNSTREAM_PARSE_DROPPED_REASONING"
    if decrypt_error:
        return "FAIL", "THINKING_ENVELOPE_UNREADABLE"
    if (
        not isinstance(reply, Mapping)
        or reply.get("thinking_present") is not True
        or not str(reply.get("thinking") or "").strip()
    ):
        return "FAIL", "THINKING_ENVELOPE_NOT_DELIVERED"
    if (
        not str(reply.get("thinking_kind") or "")
        or not str(reply.get("thinking_source") or "")
        or not str(reply.get("thinking_model") or "")
        or reply.get("thinking_native") is not True
    ):
        return "FAIL", "THINKING_METADATA_INVALID"
    if not final_answer_correct:
        return "FAIL", "FINAL_ANSWER_WRONG"
    return "PASS", "NONE"


def _write_receipt(path: Path, receipt: Mapping[str, Any]) -> None:
    if not path.is_absolute() or path.is_symlink() or path.exists():
        raise CotProbeError("COT receipt path is unsafe")
    try:
        parent = path.parent.resolve(strict=True)
        metadata = parent.stat()
    except (OSError, RuntimeError):
        raise CotProbeError("COT receipt parent is unavailable") from None
    if (
        not parent.is_dir()
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise CotProbeError("COT receipt parent is unsafe")
    payload = (
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise CotProbeError("unable to write COT receipt") from None


def _poll_trace(
    client: SmokeClient,
    session: Session,
    *,
    trace_id: str,
    reply_message_id: str,
    turn_started_at: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + TRACE_POLL_SECONDS
    last: dict[str, Any] = {}
    completed_observations = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        body = client.read_trace(
            session,
            limit=500,
            attempts=1,
            read_timeout=max(
                0.1,
                min(remaining, TRACE_READ_TIMEOUT_CAP_SECONDS),
            ),
        )
        events = [event for event in body.get("events", []) if isinstance(event, dict)]
        last = correlate_trace(
            events,
            trace_id=trace_id,
            reply_message_id=reply_message_id,
            turn_started_at=turn_started_at,
        )
        if last["dropped"] or any(
            last[key] > 1
            for key in (
                "model_call_count",
                "agent_reply_count",
                "chat_response_count",
                "chat_response_match_count",
            )
        ):
            return last
        completed = (
            last["model_call_count"] == 1
            and last["agent_reply_count"] == 1
            and last["chat_response_count"] == 1
        )
        if completed:
            completed_observations += 1
            if completed_observations == 2:
                return last
        else:
            completed_observations = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(TRACE_POLL_INTERVAL_SECONDS, remaining))
    return last or {
        "dropped": False,
        "model_call_count": 0,
        "agent_reply_count": 0,
        "chat_response_count": 0,
        "chat_response_match_count": 0,
        "model_thinking_present": False,
        "model_thinking_len": 0,
        "model_thinking_source": "",
        "agent_reply_thinking_kind": "",
        "model_duration_ms": None,
        "provider_api_duration_ms": None,
        "token_metadata_status": "UNVERIFIED",
        "reasoning_token_count": None,
    }


def run_probe(
    manifest_path: Path,
    output_path: Path,
    *,
    nonce: str,
    expected_profile_id: str | None = None,
    timeout_seconds: float = DEFAULT_REPLY_POLL_SECONDS,
    client: SmokeClient | None = None,
) -> dict[str, Any]:
    if not _NONCE_RE.fullmatch(nonce):
        raise CotProbeError("probe nonce is invalid")
    if not 10 <= timeout_seconds <= MAX_REPLY_POLL_SECONDS:
        raise CotProbeError("probe timeout is invalid")
    profile, base_url, session = load_profile_context(
        manifest_path, expected_profile_id
    )
    profile_id = str(profile["profile_id"])
    active_client = client or SmokeClient(base_url)
    requested_effort = str(profile["reasoning_effort"]).strip().lower()
    (
        configured_effort,
        configured_effort_attested,
        configured_route_matches_manifest,
    ) = read_configured_effort(active_client, session, profile)
    # Runtime V2 currently emits no trusted field proving which effort the
    # harness/provider actually applied.  Keep this explicitly unattested; a
    # visible reasoning event proves reasoning happened, not its effort level.
    effective_effort = "unknown"
    effective_effort_attested = False
    request_id = ""
    reply_message_id = ""
    ack_latency_ms: float | None = None
    reply_latency_ms: float | None = None
    trace: dict[str, Any] = {
        "dropped": False,
        "model_call_count": 0,
        "agent_reply_count": 0,
        "chat_response_count": 0,
        "chat_response_match_count": 0,
        "model_thinking_present": False,
        "model_thinking_len": 0,
        "model_thinking_source": "",
        "agent_reply_thinking_kind": "",
        "model_duration_ms": None,
        "provider_api_duration_ms": None,
        "token_metadata_status": "UNVERIFIED",
        "reasoning_token_count": None,
    }
    reply: Mapping[str, Any] | None = None
    decrypt_error = False
    final_answer_correct = False
    status = "UNVERIFIED"
    failure_code = "CHAT_REQUEST_FAILED"
    started_monotonic = time.monotonic()
    turn_started_at = time.time()
    try:
        response = active_client.send(
            session,
            (
                f"Qualification P0-12 {nonce}. Calculate 17 multiplied by 19. "
                "Give the numeric answer clearly."
            ),
            read_timeout=CHAT_SEND_TIMEOUT_SECONDS,
        )
        ack_latency_ms = (time.monotonic() - started_monotonic) * 1000.0
        user_message = response.get("user_message") or {}
        request_id = str(user_message.get("id") or "")
        user_message_ts = float(user_message.get("ts"))
        raw_reply = active_client.poll_reply_record(
            session,
            user_message_ts,
            timeout_seconds,
            include_thinking=False,
            user_message_id=request_id,
        )
        if raw_reply is None:
            failure_code = "CHAT_TIMEOUT"
        else:
            reply_latency_ms = (time.monotonic() - started_monotonic) * 1000.0
            message = raw_reply.get("message")
            reply_message_id = (
                str(message.get("id") or "") if isinstance(message, dict) else ""
            )
            final_answer_correct = bool(
                re.search(r"(?<!\d)323(?!\d)", str(raw_reply.get("reply") or ""))
            )
            try:
                reply = decrypt_reply_record(message, session.sk, session.pk)
            except SmokeError as exc:
                if exc.stage != "thinking-decrypt":
                    raise
                decrypt_error = True
            try:
                trace = _poll_trace(
                    active_client,
                    session,
                    trace_id=request_id,
                    reply_message_id=reply_message_id,
                    turn_started_at=turn_started_at,
                )
            except SmokeError:
                status = "UNVERIFIED"
                failure_code = "TRACE_UNAVAILABLE"
            else:
                status, failure_code = classify_delivery(
                    trace,
                    reply,
                    decrypt_error=decrypt_error,
                    final_answer_correct=final_answer_correct,
                )
    except SmokeError:
        status = "UNVERIFIED"
        failure_code = "CHAT_REQUEST_FAILED"

    if failure_code not in _FAILURE_CODES:
        raise CotProbeError("probe produced an unsupported failure code")
    delivered_thinking = str((reply or {}).get("thinking") or "")
    delivered_metadata_present = bool(
        str((reply or {}).get("thinking_kind") or "")
        and str((reply or {}).get("thinking_source") or "")
        and str((reply or {}).get("thinking_model") or "")
        and (reply or {}).get("thinking_native") is True
    )
    user_visible_disclosure_present = bool(
        (reply or {}).get("thinking_present") is True
        and delivered_thinking.strip()
    )
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "profile_id": profile_id,
        "request_id": request_id,
        "turn_id": request_id,
        "trace_id": request_id,
        "reply_message_id": reply_message_id,
        "status": status,
        "failure_code": failure_code,
        "requested_effort": requested_effort,
        "configured_effort": configured_effort,
        "configured_effort_attested": configured_effort_attested,
        "configured_route_matches_manifest": configured_route_matches_manifest,
        "effective_effort": effective_effort,
        "effective_effort_attested": effective_effort_attested,
        "release_qualified": False,
        "delivery_qualified": status == "PASS",
        "final_answer_correct": final_answer_correct,
        "ack_latency_ms": ack_latency_ms,
        "reply_latency_ms": reply_latency_ms,
        "model_duration_ms": trace.get("model_duration_ms"),
        "provider_api_duration_ms": trace.get("provider_api_duration_ms"),
        "trace_dropped": trace.get("dropped") is True,
        "model_call_count": trace.get("model_call_count", 0),
        "agent_reply_count": trace.get("agent_reply_count", 0),
        "chat_response_count": trace.get("chat_response_count", 0),
        "chat_response_match_count": trace.get("chat_response_match_count", 0),
        "model_thinking_present": trace.get("model_thinking_present") is True,
        "model_thinking_len": trace.get("model_thinking_len", 0),
        "reasoning_event_count": (
            1 if trace.get("model_thinking_present") is True else 0
        ),
        "model_thinking_source": str(trace.get("model_thinking_source") or ""),
        "agent_reply_thinking_kind": str(
            trace.get("agent_reply_thinking_kind") or ""
        ),
        "delivered_thinking_present": (reply or {}).get("thinking_present") is True,
        "delivered_thinking_len": len(delivered_thinking),
        "delivered_thinking_kind": str((reply or {}).get("thinking_kind") or ""),
        "delivered_thinking_source": str(
            (reply or {}).get("thinking_source") or ""
        ),
        "delivered_thinking_model": str((reply or {}).get("thinking_model") or ""),
        "delivered_thinking_native": (reply or {}).get("thinking_native"),
        "metadata_present": delivered_metadata_present,
        "user_visible_disclosure_present": user_visible_disclosure_present,
        "token_metadata_status": trace.get(
            "token_metadata_status", "UNVERIFIED"
        ),
        "reasoning_token_count": trace.get("reasoning_token_count"),
        "raw_reply_stored": False,
        "raw_thinking_stored": False,
        "raw_trace_stored": False,
    }
    _write_receipt(output_path, receipt)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--profile-id")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=DEFAULT_REPLY_POLL_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt = run_probe(
            args.manifest,
            args.output,
            nonce=args.nonce,
            expected_profile_id=args.profile_id,
            timeout_seconds=args.timeout_seconds,
        )
    except CotProbeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("ERROR: COT delivery probe encountered an internal error", file=sys.stderr)
        return 1
    print(
        "COT delivery probe completed "
        f"status={receipt['status']} code={receipt['failure_code']}"
    )
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
