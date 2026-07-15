#!/usr/bin/env python3
"""Run one fixed, parent-owned live P0 scenario probe.

This process receives no provider/admin key.  It consumes only the already
provisioned one-profile session, executes the scenario's fixed network actions,
and emits (1) a sanitized authoritative receipt and (2) a separate private facts
file for the qualification agent's semantic judgment.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from qa.request_live_scenario_probe import LIVE_SCENARIO_IDS  # noqa: E402
from qa.validate_live_scenario_receipts import (  # noqa: E402
    DETERMINISTIC_ASSERTIONS,
    RECEIPT_SCHEMA_VERSION,
    SEMANTIC_ASSERTIONS,
    canonical_json_sha256,
    validate_receipt_object,
)
from tools.provider_smoke import assertions as smoke_assertions  # noqa: E402
from tools.provider_smoke.client import Session, SmokeClient, SmokeError  # noqa: E402


LOCKED_BASE_URL = "https://test-api.feedling.app"
BASELINE_RUNTIME = "deployed_current"
RUNTIME_V2_RUNTIME = "hosted_resident"
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
CHAT_TIMEOUT_SECONDS = 120.0
CHAT_SETTLE_SECONDS = 1.0
CHAT_SETTLE_INTERVAL_SECONDS = 0.25
CHAT_SETTLE_READ_TIMEOUT_SECONDS = 5.0
_ASSISTANT_ROLES = frozenset({"openclaw", "assistant", "agent"})
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class LiveScenarioProbeError(RuntimeError):
    """A fixed, non-sensitive live probe setup failure."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _owned_private_file(path: Path, label: str, *, max_bytes: int) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise LiveScenarioProbeError(f"{label} is unsafe")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise LiveScenarioProbeError(f"{label} is unavailable") from None
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
            raise LiveScenarioProbeError(f"{label} is unsafe")
        content = os.read(descriptor, metadata.st_size + 1)
        if len(content) != metadata.st_size:
            raise LiveScenarioProbeError(f"{label} changed while reading")
        return content
    finally:
        os.close(descriptor)


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    if not path.is_absolute() or path.is_symlink() or path.exists():
        raise LiveScenarioProbeError("live probe output path is unsafe")
    try:
        parent = path.parent.resolve(strict=True)
        metadata = parent.stat()
    except (OSError, RuntimeError):
        raise LiveScenarioProbeError("live probe output parent is unavailable") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise LiveScenarioProbeError("live probe output parent is unsafe")
    try:
        content = (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError):
        raise LiveScenarioProbeError("live probe output is invalid") from None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        raise LiveScenarioProbeError("unable to write live probe output") from None


def _decode_key(value: object, label: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise LiveScenarioProbeError(f"manifest {label} is invalid")
    try:
        result = base64.b64decode(value, validate=True)
    except Exception:
        raise LiveScenarioProbeError(f"manifest {label} is invalid") from None
    if len(result) != 32:
        raise LiveScenarioProbeError(f"manifest {label} is invalid")
    return result


def load_profile(
    manifest_path: Path, expected_profile_id: str
) -> tuple[dict[str, Any], Session]:
    try:
        document = json.loads(
            _owned_private_file(
                manifest_path, "one-profile manifest", max_bytes=MAX_MANIFEST_BYTES
            )
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise LiveScenarioProbeError("one-profile manifest is invalid") from None
    profiles = document.get("profiles") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("base_url") != LOCKED_BASE_URL
        or not isinstance(profiles, list)
        or len(profiles) != 1
        or not isinstance(profiles[0], dict)
    ):
        raise LiveScenarioProbeError("one-profile manifest is invalid")
    profile = dict(profiles[0])
    runtime_requirement = document.get("runtime_mode")
    if (
        profile.get("profile_id") != expected_profile_id
        or profile.get("provision_status") != "ready"
        or not isinstance(profile.get("user_id"), str)
        or not profile["user_id"]
        or not isinstance(profile.get("api_key"), str)
        or not profile["api_key"]
        or runtime_requirement not in {BASELINE_RUNTIME, RUNTIME_V2_RUNTIME}
    ):
        raise LiveScenarioProbeError("one-profile manifest is not ready")
    profile["_qualification_runtime_requirement"] = runtime_requirement
    return profile, Session(
        user_id=profile["user_id"],
        api_key=profile["api_key"],
        sk=_decode_key(profile.get("secret_key_b64"), "secret key"),
        pk=_decode_key(profile.get("public_key_b64"), "public key"),
    )


def _probe_request_id(nonce: str) -> str:
    return f"probe-{hashlib.sha256(nonce.encode('utf-8')).hexdigest()[:24]}"


def _message_count(client: SmokeClient, session: Session) -> int:
    status, body = client._req(
        "GET", "/v1/chat/history?limit=200", api_key=session.api_key
    )
    messages = body.get("messages") if isinstance(body, Mapping) else None
    if status != 200 or not isinstance(messages, list):
        raise SmokeError("history", "chat history readback failed")
    return len(messages)


def _history_timestamp(message: Mapping[str, Any]) -> float | None:
    try:
        value = float(message.get("ts"))
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _settled_turn_summary(
    messages: object,
    *,
    user_message_id: str,
    user_message_ts: float,
    expected_reply_id: str,
) -> dict[str, Any]:
    """Derive duplicate/order facts from one settled transcript snapshot."""

    if not isinstance(messages, list) or any(
        not isinstance(message, Mapping) for message in messages
    ):
        return {
            "reply_count": 0,
            "duplicate_detected": False,
            "out_of_order_detected": True,
            "reply_message_ids": [],
        }
    rows = list(messages)
    ids = [str(message.get("id") or "").strip() for message in rows]
    timestamps = [_history_timestamp(message) for message in rows]
    duplicate_ids = bool(
        any(not message_id for message_id in ids)
        or len(ids) != len(set(ids))
    )
    chronological = bool(
        all(timestamp is not None for timestamp in timestamps)
        and all(
            float(timestamps[index]) >= float(timestamps[index - 1])
            for index in range(1, len(timestamps))
        )
    )
    user_rows = [
        message
        for message in rows
        if str(message.get("id") or "").strip() == user_message_id
    ]
    user_row = user_rows[0] if len(user_rows) == 1 else None
    user_row_ts = _history_timestamp(user_row) if user_row is not None else None
    assistants_after = [
        message
        for message in rows
        if str(message.get("role") or "") in _ASSISTANT_ROLES
        and bool(message.get("body_ct"))
        and (_history_timestamp(message) or -1) > user_message_ts
    ]
    reply_ids = [
        str(message.get("id") or "").strip() for message in assistants_after
    ]
    linked_id = (
        str(user_row.get("reply_message_id") or "").strip()
        if user_row is not None
        else ""
    )
    linked_rows = [
        message
        for message in assistants_after
        if (
            (linked_id and str(message.get("id") or "").strip() == linked_id)
            or str(
                message.get("reply_to_message_id")
                or message.get("reply_to_id")
                or message.get("in_reply_to")
                or ""
            ).strip()
            == user_message_id
        )
    ]
    linked_reply_ids = {
        str(message.get("id") or "").strip() for message in linked_rows
    }
    later_users = [
        message
        for message in rows
        if str(message.get("role") or "") == "user"
        and str(message.get("id") or "").strip() != user_message_id
        and (_history_timestamp(message) or -1) > user_message_ts
    ]
    timestamp_matches = bool(
        user_row_ts is not None and abs(user_row_ts - user_message_ts) <= 0.001
    )
    expected_reply_present = bool(
        expected_reply_id and expected_reply_id in linked_reply_ids
    )
    unlinked_reply = any(
        str(message.get("id") or "").strip() not in linked_reply_ids
        for message in assistants_after
    )
    reply_count = len(assistants_after)
    return {
        "reply_count": reply_count,
        "duplicate_detected": bool(duplicate_ids or reply_count > 1),
        "out_of_order_detected": bool(
            not chronological
            or user_row is None
            or not timestamp_matches
            or bool(later_users)
            or unlinked_reply
            or not expected_reply_present
        ),
        "reply_message_ids": reply_ids[:16],
    }


def _settle_turn_history(
    client: SmokeClient,
    session: Session,
    *,
    user_message_id: str,
    user_message_ts: float,
    expected_reply_id: str,
    settle_seconds: float,
    settle_interval_seconds: float,
) -> dict[str, Any]:
    """Re-poll for a fixed window so a late duplicate cannot false-green."""

    deadline = time.monotonic() + max(0.0, settle_seconds)
    latest: object = []
    while True:
        since = max(0.0, user_message_ts - 0.001)
        status, body = client._req(
            "GET",
            f"/v1/chat/history?since={since:.6f}&limit=200",
            api_key=session.api_key,
            attempts=1,
            read_timeout=CHAT_SETTLE_READ_TIMEOUT_SECONDS,
        )
        if status != 200 or not isinstance(body, Mapping):
            raise SmokeError("history", "settled chat history readback failed")
        latest = body.get("messages")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(0.01, settle_interval_seconds), remaining))
    return _settled_turn_summary(
        latest,
        user_message_id=user_message_id,
        user_message_ts=user_message_ts,
        expected_reply_id=expected_reply_id,
    )


def _chat_turn(
    client: SmokeClient,
    session: Session,
    *,
    turn_index: int,
    prompt: str,
    content_check: Callable[[str], bool] | None,
    settle_seconds: float = CHAT_SETTLE_SECONDS,
    settle_interval_seconds: float = CHAT_SETTLE_INTERVAL_SECONDS,
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.monotonic()
    response = client.send(session, prompt)
    ack_latency = (time.monotonic() - started) * 1000.0
    user_message = response.get("user_message") or {}
    request_id = str(user_message.get("id") or "")
    try:
        user_ts = float(user_message["ts"])
    except (KeyError, TypeError, ValueError):
        raise SmokeError("chat", "hosted acknowledgement is incomplete") from None
    correlation_error: SmokeError | None = None
    try:
        reply = client.poll_reply_record(
            session,
            user_ts,
            CHAT_TIMEOUT_SECONDS,
            include_thinking=False,
            user_message_id=request_id,
        )
    except SmokeError as exc:
        if exc.stage != "reply-correlation":
            raise
        correlation_error = exc
        reply = None
    reply_latency = (time.monotonic() - started) * 1000.0
    if reply is None and correlation_error is None:
        raise SmokeError("chat", "correlated reply timed out")
    reply_message = reply.get("message") if isinstance(reply, Mapping) else None
    expected_reply_id = (
        str(reply_message.get("id") or "").strip()
        if isinstance(reply_message, Mapping)
        else ""
    )
    settled = _settle_turn_history(
        client,
        session,
        user_message_id=request_id,
        user_message_ts=user_ts,
        expected_reply_id=expected_reply_id,
        settle_seconds=settle_seconds,
        settle_interval_seconds=settle_interval_seconds,
    )
    if correlation_error is not None and not (
        settled["duplicate_detected"] or settled["out_of_order_detected"]
    ):
        raise correlation_error
    plaintext = str(reply.get("reply") or "") if isinstance(reply, Mapping) else ""
    content_passed = (
        None if content_check is None else bool(reply is not None and content_check(plaintext))
    )
    fallback = bool(reply is not None and smoke_assertions.is_fallback(plaintext))
    turn = {
        "turn_index": turn_index,
        "request_id": request_id,
        "turn_id": request_id,
        "trace_id": request_id,
        "ack_latency_ms": ack_latency,
        "reply_latency_ms": reply_latency,
        "reply_count": settled["reply_count"],
        "content_assertion_passed": content_passed,
        "fallback_detected": fallback,
        "duplicate_detected": settled["duplicate_detected"],
        "out_of_order_detected": settled["out_of_order_detected"],
    }
    private = {
        "turn_index": turn_index,
        "request_id": request_id,
        "reply": plaintext,
        "settled_reply_message_ids": settled["reply_message_ids"],
        "correlation_error_stage": (
            str(correlation_error.stage) if correlation_error is not None else ""
        ),
    }
    return turn, private


def _route_candidates(value: object, *, depth: int = 0) -> list[str]:
    if depth > 4:
        return []
    results: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).lower() in {
                "provider",
                "model",
                "model_id",
                "runtime",
                "runtime_mode",
                "engine",
            } and isinstance(child, (str, int)):
                results.append(f"{str(key)[:32]}={str(child)[:160]}")
            else:
                results.extend(_route_candidates(child, depth=depth + 1))
    elif isinstance(value, list):
        for child in value[:100]:
            results.extend(_route_candidates(child, depth=depth + 1))
    return list(dict.fromkeys(results))[:64]


def _run_actions(
    scenario_id: str,
    *,
    nonce: str,
    profile: Mapping[str, Any],
    session: Session,
    client: SmokeClient,
) -> tuple[dict[str, bool], list[dict[str, Any]], dict[str, Any]]:
    if scenario_id == "P0-02":
        who_status, who = client._req(
            "GET", "/v1/users/whoami", api_key=session.api_key
        )
        history_status, history = client._req(
            "GET", "/v1/chat/history?limit=1", api_key=session.api_key
        )
        memory_status, memory = client._req(
            "GET", "/v1/memory/list?limit=1", api_key=session.api_key
        )
        client.clear_trace(session)
        assertions = {
            "synthetic_account_is_fresh": bool(
                profile.get("fresh_state_verified") is True
                and history_status == 200
                and memory_status == 200
                and not (history.get("messages") or [])
                and not (memory.get("moments") or [])
            ),
            "whoami_matches": bool(
                who_status == 200
                and isinstance(who, Mapping)
                and who.get("user_id") == session.user_id
                and profile.get("registration_verified") is True
            ),
            "trace_cleared": True,
        }
        return assertions, [], {"whoami_user_id": str(who.get("user_id") or "")}

    if scenario_id == "P0-03":
        receipt = profile.get("invalid_key_receipt")
        serialized = json.dumps(receipt, sort_keys=True) if isinstance(receipt, Mapping) else ""
        assertions = {
            "invalid_key_rejected": bool(
                profile.get("invalid_key_rejected") is True
                and isinstance(receipt, Mapping)
                and receipt.get("http_status") == 400
                and receipt.get("error") == "provider_test_failed"
                and receipt.get("provider_status_code") in (400, 401, 403)
            ),
            "invalid_key_not_echoed": "qa-invalid-key" not in serialized.lower(),
            "hosted_chat_not_started": profile.get("fresh_state_verified") is True,
        }
        return assertions, [], {"receipt_fields": sorted(receipt) if isinstance(receipt, Mapping) else []}

    if scenario_id == "P0-04":
        receipt = profile.get("valid_key_receipt")
        assertions = {
            "valid_key_accepted": bool(
                profile.get("valid_key_configured") is True
                and isinstance(receipt, Mapping)
                and receipt.get("status") == "configured"
            ),
            "provider_config_matches": bool(
                isinstance(receipt, Mapping)
                and receipt.get("provider") == profile.get("provider")
                and receipt.get("model") == profile.get("configured_model")
                and receipt.get("base_url") == profile.get("configured_base_url")
                and receipt.get("reasoning_effort") == profile.get("reasoning_effort")
            ),
            "credential_omitted": bool(
                isinstance(receipt, Mapping)
                and "api_key" not in receipt
                and profile.get("api_key") not in json.dumps(receipt, sort_keys=True)
            ),
        }
        return assertions, [], {"configured_provider": str(profile.get("provider") or ""), "configured_model": str(profile.get("configured_model") or "")}

    if scenario_id == "P0-05":
        runtime = client.runtime_status(session)
        mode = runtime.get("runtime_mode") if isinstance(runtime, Mapping) else None
        version = runtime.get("runtime_version") if isinstance(runtime, Mapping) else None
        strict_v2 = (
            profile.get("_qualification_runtime_requirement") == RUNTIME_V2_RUNTIME
        )
        runtime_matches_target = bool(
            isinstance(mode, str)
            and mode
            and type(version) is int
            and version >= 1
            and (
                not strict_v2
                or (mode == RUNTIME_V2_RUNTIME and version == 2)
            )
        )
        assertions = {
            "runtime_status_readback_succeeds": isinstance(runtime, Mapping),
            "runtime_configured": runtime.get("configured") is True,
            "runtime_metadata_recorded": runtime_matches_target,
        }
        return assertions, [], {"runtime_mode": mode, "runtime_version": version}

    if scenario_id == "P0-07":
        before = _message_count(client, session)
        driver = client.enable_hosting(session)
        verification = client.open_chat_gate(session)
        after = _message_count(client, session)
        runtime = client.runtime_status(session)
        mode = runtime.get("runtime_mode") if isinstance(runtime, Mapping) else None
        version = runtime.get("runtime_version") if isinstance(runtime, Mapping) else None
        strict_v2 = (
            profile.get("_qualification_runtime_requirement") == RUNTIME_V2_RUNTIME
        )
        runtime_matches_target = bool(
            isinstance(runtime, Mapping)
            and runtime.get("configured") is True
            and isinstance(mode, str)
            and mode
            and type(version) is int
            and version >= 1
            and (
                not strict_v2
                or (mode == RUNTIME_V2_RUNTIME and version == 2)
            )
        )
        assertions = {
            "driver_enabled": bool(driver),
            "chat_loop_verified": verification.get("passing") is True,
            "runtime_status_readback_succeeds": runtime_matches_target,
            "no_orphan_turn": before == after,
        }
        return assertions, [], {
            "driver": driver[:80],
            "verify_passing": verification.get("passing") is True,
            "runtime_mode": mode,
            "runtime_version": version,
        }

    turns: list[dict[str, Any]] = []
    private_turns: list[dict[str, Any]] = []
    if scenario_id == "P0-08":
        token = f"ECHO-{nonce}"
        turn, private = _chat_turn(
            client,
            session,
            turn_index=1,
            prompt=f"Reply with exactly this token and nothing else: {token}",
            content_check=lambda reply: token.lower() in reply.lower(),
        )
        turns.append(turn)
        private_turns.append(private)
        delivery_is_exact = bool(
            turn["reply_count"] == 1
            and turn["duplicate_detected"] is False
            and turn["out_of_order_detected"] is False
        )
        assertions = {
            "async_ack_received": bool(turn["request_id"]),
            "exact_reply_correlated": delivery_is_exact,
            "nonce_echo_confirmed": turn["content_assertion_passed"] is True,
            "fallback_absent": turn["fallback_detected"] is False,
            "latency_recorded": turn["ack_latency_ms"] is not None and turn["reply_latency_ms"] is not None,
        }
        return assertions, turns, {"turns": private_turns}

    if scenario_id == "P0-09":
        memory_token = f"MEM-{nonce}"
        for index in range(1, 11):
            turn_token = f"T{index}-{nonce}"
            if index == 1:
                prompt = f"Remember the token {memory_token} for turn ten. Reply exactly {turn_token}."
                expected = turn_token
            elif index == 10:
                prompt = "Reply with only the original memory token from turn one."
                expected = memory_token
            else:
                prompt = f"This is distractor turn {index}. Reply exactly {turn_token}."
                expected = turn_token

            def checker(reply: str, expected: str = expected) -> bool:
                return expected.lower() in reply.lower()

            turn, private = _chat_turn(
                client,
                session,
                turn_index=index,
                prompt=prompt,
                content_check=checker,
            )
            turns.append(turn)
            private_turns.append(private)
        assertions = {
            "ten_turns_ordered": len(turns) == 10 and all(turn["turn_index"] == index for index, turn in enumerate(turns, start=1)),
            "exact_replies_correlated": all(
                turn["reply_count"] == 1
                and turn["duplicate_detected"] is False
                and turn["out_of_order_detected"] is False
                for turn in turns
            ),
            "memory_recall_confirmed": turns[-1]["content_assertion_passed"] is True,
            "no_orphan_turn": bool(
                len({turn["request_id"] for turn in turns}) == 10
                and all(
                    turn["duplicate_detected"] is False
                    and turn["out_of_order_detected"] is False
                    for turn in turns
                )
            ),
        }
        return assertions, turns, {"turns": private_turns, "memory_token": memory_token}

    if scenario_id == "P0-10":
        prompts = (
            "What is Rowan's reset ritual, and what foods and music define Lantern Evening?",
            "I had a draining day and feel scattered. Respond naturally in your usual style.",
        )
        for index, prompt in enumerate(prompts, start=1):
            turn, private = _chat_turn(
                client,
                session,
                turn_index=index,
                prompt=prompt,
                content_check=None,
            )
            turns.append(turn)
            private_turns.append(private)
        return {
            "transport_correlated": bool(
                len(turns) == 2
                and all(
                    turn["reply_count"] == 1
                    and turn["duplicate_detected"] is False
                    and turn["out_of_order_detected"] is False
                    for turn in turns
                )
            )
        }, turns, {
            "turns": private_turns,
            "expected_fact_ids": ["rowan-reset-ritual", "lantern-evening", "conversation-preference"],
            "judge_required": True,
        }

    if scenario_id == "P0-11":
        turn, private = _chat_turn(
            client,
            session,
            turn_index=1,
            prompt="Who are you as my companion, and which provider/model route is serving this turn?",
            content_check=None,
        )
        turns.append(turn)
        private_turns.append(private)
        trace = client.read_trace(session, limit=500)
        matching = [
            event
            for event in trace.get("events", [])
            if isinstance(event, Mapping) and event.get("trace_id") == turn["trace_id"]
        ]
        route_candidates = _route_candidates(matching)
        valid_receipt = profile.get("valid_key_receipt")
        assertions = {
            "transport_correlated": bool(
                turn["reply_count"] == 1
                and turn["duplicate_detected"] is False
                and turn["out_of_order_detected"] is False
            ),
            "provider_config_matches": bool(
                isinstance(valid_receipt, Mapping)
                and valid_receipt.get("provider") == profile.get("provider")
                and valid_receipt.get("model") == profile.get("configured_model")
            ),
            "trace_route_correlated": bool(matching),
        }
        return assertions, turns, {
            "turns": private_turns,
            "configured_provider": str(profile.get("provider") or ""),
            "configured_model": str(profile.get("configured_model") or ""),
            "route_candidates": route_candidates,
            "judge_required": True,
        }
    raise LiveScenarioProbeError("unsupported live scenario")


def _classify_smoke_error(
    scenario_id: str, error: SmokeError
) -> tuple[str, str]:
    """Allow retry only for a missing or timed-out chat observation.

    A route/runtime/trace failure is useful product or evidence failure, but it
    is not a license to rerun a non-idempotent chat mutation.  The deterministic
    gate accepts a second attempt only for the two locked transient codes below.
    """

    if scenario_id in {"P0-08", "P0-09", "P0-10", "P0-11"}:
        if error.stage == "chat":
            code = (
                "CHAT_TIMEOUT"
                if "timed out" in str(error.detail).lower()
                else "MISSING_REPLY"
            )
            return "AGENT_ERROR", code
        if error.stage == "history":
            return "AGENT_ERROR", "MISSING_REPLY"
        if error.stage == "not-hosted":
            return "PRODUCT_FAIL", "ASSERTION_FAILED"
    return "BLOCKED_EVIDENCE", "LIVE_PROBE_ERROR"


def run_probe(
    *,
    manifest_path: Path,
    output_path: Path,
    private_facts_path: Path,
    run_id: str,
    profile_id: str,
    scenario_id: str,
    attempt: int,
    nonce: str,
    client: SmokeClient | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        not _SAFE_ID_RE.fullmatch(run_id)
        or not _SAFE_ID_RE.fullmatch(profile_id)
        or not _SAFE_ID_RE.fullmatch(nonce)
        or scenario_id not in LIVE_SCENARIO_IDS
        or attempt not in (1, 2)
    ):
        raise LiveScenarioProbeError("live probe identity is invalid")
    profile, session = load_profile(manifest_path, profile_id)
    active_client = client or SmokeClient(LOCKED_BASE_URL)
    started_at = _utc_now()
    request_ids: list[str] = []
    turn_ids: list[str] = []
    trace_ids: list[str] = []
    turns: list[dict[str, Any]] = []
    assertions = {key: False for key in DETERMINISTIC_ASSERTIONS[scenario_id]}
    private_facts: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "profile_id": profile_id,
        "scenario_id": scenario_id,
        "attempt": attempt,
        "observations": {},
    }
    status = "BLOCKED_EVIDENCE"
    failure_code = "LIVE_PROBE_ERROR"
    try:
        assertions, turns, observations = _run_actions(
            scenario_id,
            nonce=nonce,
            profile=profile,
            session=session,
            client=active_client,
        )
        private_facts["observations"] = observations
        if turns:
            request_ids = [turn["request_id"] for turn in turns]
            turn_ids = list(request_ids)
            trace_ids = list(request_ids)
        else:
            request_ids = [_probe_request_id(nonce)]
        status = "PASS" if all(assertions.values()) else "PRODUCT_FAIL"
        failure_code = "NONE" if status == "PASS" else "ASSERTION_FAILED"
    except SmokeError as exc:
        private_facts["observations"] = {"error_stage": str(exc.stage)[:64]}
        status, failure_code = _classify_smoke_error(scenario_id, exc)
    except Exception:
        private_facts["observations"] = {"error_stage": "internal"}
        failure_code = "LIVE_PROBE_ERROR"
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "kind": "live_scenario_probe",
        "run_id": run_id,
        "profile_id": profile_id,
        "scenario_id": scenario_id,
        "attempt": attempt,
        "nonce": nonce,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "status": status,
        "failure_code": failure_code,
        "assertions": assertions,
        "semantic_assertions": list(SEMANTIC_ASSERTIONS[scenario_id]),
        "request_ids": request_ids,
        "turn_ids": turn_ids,
        "trace_ids": trace_ids,
        "turns": turns,
        "private_facts_sha256": canonical_json_sha256(private_facts),
        "raw_content_stored": False,
    }
    validate_receipt_object(
        receipt,
        run_id=run_id,
        profile_id=profile_id,
        scenario_id=scenario_id,
        attempt=attempt,
    )
    _write_private_json(private_facts_path, private_facts)
    _write_private_json(output_path, receipt)
    return receipt, private_facts


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="run one trusted live QA scenario")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--private-facts", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--scenario", choices=LIVE_SCENARIO_IDS, required=True)
    parser.add_argument("--attempt", type=int, choices=(1, 2), required=True)
    parser.add_argument("--nonce", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        receipt, _ = run_probe(
            manifest_path=args.manifest,
            output_path=args.output,
            private_facts_path=args.private_facts,
            run_id=args.run_id,
            profile_id=args.profile_id,
            scenario_id=args.scenario,
            attempt=args.attempt,
            nonce=args.nonce,
        )
    except LiveScenarioProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0 if receipt["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
