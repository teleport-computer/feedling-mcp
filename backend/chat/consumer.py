"""Resident-consumer liveness state + validation gate input."""

import copy
import math
import os
import time
from datetime import datetime
from typing import Callable, TypeVar

import db
from core.store import UserStore


_OFFICIAL_CONSUMER_NAME = "feedling-chat-resident"
VISION_OBSERVER_CAPABILITY = "vision_observer_v1"
_CONSUMER_RECENT_SEC = int(os.environ.get("FEEDLING_CONSUMER_RECENT_SEC", "180"))
_DECRYPT_HEALTH_STATUSES = frozenset(
    {"ok", "degraded", "unconfigured", "unreachable"}
)
_DECRYPT_HEALTH_RECENT_SEC = max(
    1, int(os.environ.get("FEEDLING_DECRYPT_HEALTH_RECENT_SEC", "300"))
)
_DECRYPT_HEALTH_FUTURE_SKEW_SEC = max(
    0, int(os.environ.get("FEEDLING_DECRYPT_HEALTH_FUTURE_SKEW_SEC", "60"))
)
_DECRYPT_HEALTH_EXISTING_UNKNOWN_GRACE_SEC = max(
    0,
    int(
        os.environ.get(
            "FEEDLING_DECRYPT_HEALTH_EXISTING_UNKNOWN_GRACE_SEC",
            str(7 * 24 * 60 * 60),
        )
    ),
)
_RESIDENT_BINDING_SEEN_INTERVAL_SEC = max(
    1, int(os.environ.get("FEEDLING_RESIDENT_BINDING_SEEN_INTERVAL_SEC", "60"))
)
_CONSUMER_STATE_CAS_ATTEMPTS = 5
_ConsumerStateResult = TypeVar("_ConsumerStateResult")


def expected_consumer_commit() -> str:
    """The git commit a self-hosted resident consumer should be running.

    Advertised to consumers (see chat poll response) so they can self-update to
    the commit this backend deploys — keeping client and server in lockstep.
    Operators may pin an explicit value; otherwise we fall back to this
    backend's own deployed commit (the same ``FEEDLING_GIT_COMMIT`` used by the
    enclave RELEASE block). Read at call time so it is unit-testable."""
    return (
        os.environ.get("FEEDLING_EXPECTED_CONSUMER_COMMIT")
        or os.environ.get("FEEDLING_GIT_COMMIT")
        or ""
    ).strip()


def _load_consumer_state(store: UserStore) -> dict:
    try:
        data = db.get_blob(store.user_id, "consumer_state")
        if isinstance(data, dict):
            return data
    except Exception as e:
        print(f"[{store.user_id}/consumer_state] failed to load: {e}")
    return {}


def _mutate_consumer_state(
    store: UserStore,
    mutate: Callable[[dict], _ConsumerStateResult],
) -> tuple[dict, _ConsumerStateResult] | None:
    """Atomically apply one consumer-state mutation across backend workers.

    ``consumer_state_lock`` only serializes threads sharing this ``UserStore``;
    gunicorn workers have independent stores and locks. The database CAS closes
    that process boundary. On conflict we reload the winner and rerun the
    field-level mutator, so unrelated fields/subtrees survive while a genuine
    same-field later write retains normal last-writer semantics.

    ``None`` means the bounded retries were exhausted. Callers whose next step
    has an external side effect (notably maintenance-message injection) must
    fail closed rather than act on a state transition that was not persisted.
    """
    with store.consumer_state_lock:
        for attempt in range(_CONSUMER_STATE_CAS_ATTEMPTS):
            current = _load_consumer_state(store)
            candidate = copy.deepcopy(current)
            result = mutate(candidate)
            if candidate == current:
                return candidate, result
            if db.set_blob_if_unchanged(
                store.user_id,
                "consumer_state",
                current,
                candidate,
                insert_if_missing=True,
            ):
                return candidate, result
            if attempt + 1 < _CONSUMER_STATE_CAS_ATTEMPTS:
                time.sleep(0.001 * (2**attempt))
    print(
        f"[{store.user_id}/consumer_state] CAS retries exhausted; mutation skipped"
    )
    return None


def _consumer_headers_from_map(headers, remote_addr: str = "") -> dict:
    """Framework-neutral: consumer identity from a headers mapping + remote addr.

    Both Flask (request.headers) and ASGI (request.headers) expose a
    case-insensitive ``.get``, so the ASGI poll route reuses this (plan §9.1)."""
    name = (headers.get("X-Feedling-Consumer") or "").strip()
    if not name:
        return {}
    capabilities = sorted({
        item.strip().lower()
        for item in str(
            headers.get("X-Feedling-Consumer-Capabilities") or ""
        ).split(",")
        if item.strip()
    })
    input_modalities = sorted({
        item.strip().lower()
        for item in str(
            headers.get("X-Feedling-Agent-Input-Modalities") or ""
        ).split(",")
        if item.strip().lower() in {"text", "image", "audio", "video"}
    })
    return {
        "consumer_name": name,
        "consumer_id": (headers.get("X-Feedling-Consumer-Id") or "").strip(),
        "consumer_version": (headers.get("X-Feedling-Consumer-Version") or "").strip(),
        "consumer_capabilities": capabilities,
        "agent_provider": (
            headers.get("X-Feedling-Agent-Provider") or ""
        ).strip()[:120],
        "agent_model": (
            headers.get("X-Feedling-Agent-Model") or ""
        ).strip()[:240],
        "agent_input_modalities": input_modalities,
        "consumer_commit": (headers.get("X-Feedling-Consumer-Commit") or "").strip(),
        # Poll-only compatibility claim: the running image intentionally
        # skipped an irrelevant target while remaining protocol-compatible.
        # Keep empty values so a later poll clears an obsolete claim.
        "consumer_compat_commit": (
            headers.get("X-Feedling-Consumer-Compat-Commit") or ""
        ).strip(),
        # Why self-update is stalled ("dirty" | "disabled" | "fetch_failed" | "")
        # — self-reported by the consumer (tools/chat_resident_consumer.py:
        # _self_update_stall_reason()) so the 6h commit-mismatch maintenance
        # nudge can name a concrete fix. An old resident that omits this header
        # reports "" here, same as consumer_compat_commit, and the nudge falls
        # back to its pre-existing generic text (backward compatible).
        # pre 同文件同改（自托管专属字段，与 hosted/V2 路径无关）。
        "update_stall_reason": (
            headers.get("X-Feedling-Update-Stall") or ""
        ).strip().lower(),
        # Keep empty values: an old resident that omits these headers must clear
        # a previously cached report instead of inheriting a stale green state.
        "decrypt_status": (headers.get("X-Feedling-Decrypt-Status") or "").strip().lower(),
        "decrypt_checked_at_epoch": (
            headers.get("X-Feedling-Decrypt-Checked-At") or ""
        ).strip(),
        "official": name == _OFFICIAL_CONSUMER_NAME,
        "remote_addr": remote_addr or "",
        "user_agent": headers.get("User-Agent", ""),
    }


def _record_consumer_event(store: UserStore, event_type: str, *, info: dict | None = None) -> None:
    # ASGI callers always pass ``info`` (computed from the ASGI request off the
    # loop). Missing/empty info is a no-op.
    if not info:
        return
    now_epoch = time.time()
    now_iso = datetime.now().isoformat()

    def mutate(state: dict) -> None:
        event_info = dict(info)
        if event_type != "poll":
            # Decrypt health is a poll-heartbeat contract. Resident response
            # requests use the static consumer headers and would otherwise
            # replace the poll's fresh green report with empty/unknown exactly
            # when verify_loop receives its hidden ack.
            event_info.pop("decrypt_status", None)
            event_info.pop("decrypt_checked_at_epoch", None)
            event_info.pop("consumer_compat_commit", None)
            event_info.pop("consumer_capabilities", None)
            event_info.pop("agent_provider", None)
            event_info.pop("agent_model", None)
            event_info.pop("agent_input_modalities", None)
            event_info.pop("update_stall_reason", None)
        state.update(event_info)
        state["last_event"] = event_type
        state["last_seen_at"] = now_iso
        state["last_seen_epoch"] = now_epoch
        if event_type == "poll":
            state["last_poll_at"] = now_iso
            state["last_poll_epoch"] = now_epoch
            if state.get("official"):
                health = _decrypt_health_from_state(state, now_epoch=now_epoch)
                if health["status"] == "unknown":
                    state.setdefault("decrypt_health_unknown_since_epoch", now_epoch)
                else:
                    state.pop("decrypt_health_unknown_since_epoch", None)
        elif event_type == "response":
            state["last_response_at"] = now_iso
            state["last_response_epoch"] = now_epoch

    _mutate_consumer_state(store, mutate)
    if event_type == "poll":
        _touch_resident_binding_seen(store, info=info, now_epoch=now_epoch)


def _safe_epoch(value) -> float:
    try:
        parsed = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed) or parsed <= 0:
        return 0.0
    return parsed


def _decrypt_health_from_state(
    state: dict,
    *,
    now_epoch: float | None = None,
) -> dict:
    """Normalize a resident's authenticated decrypt-health self-report.

    Poll freshness and health freshness are deliberately separate. A resident
    may keep polling while replaying an old ``ok`` report, so only a valid,
    recent ``checked_at`` can pass this gate.
    """
    now = time.time() if now_epoch is None else float(now_epoch)
    raw_status = str(state.get("decrypt_status") or "").strip().lower()
    checked_at = _safe_epoch(state.get("decrypt_checked_at_epoch"))
    valid_status = raw_status in _DECRYPT_HEALTH_STATUSES
    future = checked_at > now + _DECRYPT_HEALTH_FUTURE_SKEW_SEC
    age_sec = max(0.0, now - checked_at) if checked_at else None
    fresh = bool(
        checked_at
        and not future
        and age_sec is not None
        and age_sec <= _DECRYPT_HEALTH_RECENT_SEC
    )

    if not valid_status:
        status = "unknown"
        reason = "decrypt_health_unknown"
    elif not checked_at or future:
        status = "unknown"
        reason = "decrypt_health_invalid_timestamp"
    elif not fresh:
        status = "unknown"
        reason = "decrypt_health_stale"
    else:
        status = raw_status
        reason = {
            "ok": "",
            "degraded": "decrypt_source_degraded",
            "unconfigured": "decrypt_source_unconfigured",
            "unreachable": "decrypt_source_unreachable",
        }[status]

    required = {
        "decrypt_health_unknown": (
            "Update the resident consumer so every poll reports current decrypt "
            "health, then retry onboarding verification."
        ),
        "decrypt_health_invalid_timestamp": (
            "The resident consumer reported decrypt health without a valid "
            "checked-at time. Check its clock and update the consumer."
        ),
        "decrypt_health_stale": (
            "The resident consumer's decrypt-health check is stale. Confirm the "
            "consumer is still checking its configured decrypt source."
        ),
        "decrypt_source_degraded": (
            "The resident consumer claimed an encrypted message but could not "
            "recover non-empty plaintext. Check its enclave key/decrypt path."
        ),
        "decrypt_source_unconfigured": (
            "Configure FEEDLING_ENCLAVE_URL for the resident consumer; real "
            "encrypted user messages cannot be answered without it."
        ),
        "decrypt_source_unreachable": (
            "The resident consumer cannot reach FEEDLING_ENCLAVE_URL. Restore "
            "network/TLS access before completing onboarding."
        ),
    }.get(reason, "")
    return {
        "passing": status == "ok",
        "status": status,
        "reported_status": raw_status if valid_status else "",
        "checked_at_epoch": checked_at,
        "age_sec": age_sec,
        "fresh_window_sec": _DECRYPT_HEALTH_RECENT_SEC,
        "reported": bool(valid_status and checked_at),
        "fresh": fresh,
        "reason": reason,
        "required": required,
        "unknown_since_epoch": _safe_epoch(
            state.get("decrypt_health_unknown_since_epoch")
        ),
    }


def _resident_onboarding_completed(store: UserStore) -> bool:
    """Durable cohort marker for rollout compatibility.

    ``first_chat_ok_at`` is written only after a reply to an ordinary user
    message, which is also the resident onboarding validation's final real-chat
    acceptance. Synthetic verify replies never set it.
    """
    try:
        return bool(str(store.first_chat_ok_at() or "").strip())
    except Exception:
        return False


def _decrypt_health_enforcement_state(
    store: UserStore,
    consumer_state: dict | None = None,
    *,
    now_epoch: float | None = None,
) -> dict:
    """Rollout policy for new onboarding versus established residents."""
    now = time.time() if now_epoch is None else float(now_epoch)
    validation = consumer_state or _consumer_validation_state(store, now_epoch=now)
    health = validation.get("decrypt_health")
    if not isinstance(health, dict):
        health = _decrypt_health_from_state({}, now_epoch=now)
    established = _resident_onboarding_completed(store)

    if health["passing"]:
        mode = "healthy"
        blocks_onboarding = blocks_verify = blocks_chat = False
        grace_active = False
        grace_remaining_sec = 0
    elif not established:
        mode = "new_onboarding_blocked"
        blocks_onboarding = blocks_verify = blocks_chat = True
        grace_active = False
        grace_remaining_sec = 0
    elif health["status"] != "unknown":
        mode = "established_explicit_failure"
        blocks_onboarding = blocks_verify = True
        # Proven-working residents already fail naturally when decryption is
        # genuinely down. Do not add a second hard chat outage for a transient
        # enclave blip; surface the actionable warning and prevent a new green
        # onboarding/verify result instead.
        blocks_chat = False
        grace_active = False
        grace_remaining_sec = 0
    else:
        unknown_since = float(health.get("unknown_since_epoch") or now)
        unknown_age = max(0.0, now - unknown_since)
        grace_remaining_sec = max(
            0, int(_DECRYPT_HEALTH_EXISTING_UNKNOWN_GRACE_SEC - unknown_age)
        )
        grace_active = unknown_age < _DECRYPT_HEALTH_EXISTING_UNKNOWN_GRACE_SEC
        mode = (
            "established_unknown_grace"
            if grace_active
            else "established_unknown_expired"
        )
        blocks_onboarding = not grace_active
        # A diagnostic verify must never mint a new sticky green result from an
        # unknown decrypt path, even while ordinary established chat is in grace.
        blocks_verify = True
        blocks_chat = False

    return {
        "mode": mode,
        "established": established,
        "blocks_onboarding": blocks_onboarding,
        "blocks_verify": blocks_verify,
        "blocks_chat": blocks_chat,
        "warning_only": bool(
            mode == "established_unknown_grace"
        ),
        "grace_active": grace_active,
        "grace_remaining_sec": grace_remaining_sec,
        "existing_unknown_grace_sec": (
            _DECRYPT_HEALTH_EXISTING_UNKNOWN_GRACE_SEC
        ),
        "reason": health["reason"],
    }


def _touch_resident_binding_seen(
    store: UserStore,
    *,
    info: dict | None,
    now_epoch: float | None = None,
) -> bool:
    """Refresh resident access liveness for a real resident-consumer poll.

    The consumer identity header prevents ordinary app/API polling from claiming
    the resident is online. The active-route check excludes hosted model_api and
    official_import users even though the hosted runner uses the same consumer
    binary and identity header. Best-effort: liveness telemetry must never break
    chat polling when its route read or registry persist is unavailable.
    """
    if not isinstance(info, dict) or not info.get("official"):
        return False
    try:
        from accounts import onboarding, registry

        if onboarding._load_onboarding_route(store) != "resident":
            return False
        return registry._touch_resident_binding_seen(
            store.user_id,
            min_interval_sec=_RESIDENT_BINDING_SEEN_INTERVAL_SEC,
            now_epoch=now_epoch,
        )
    except Exception as e:
        print(f"[{store.user_id}/resident-seen] heartbeat update failed: {e}")
        return False


def _consumer_validation_state(
    store: UserStore,
    *,
    now_epoch: float | None = None,
) -> dict:
    with store.consumer_state_lock:
        state = _load_consumer_state(store)
    now = time.time() if now_epoch is None else float(now_epoch)
    last_poll_epoch = 0.0
    try:
        last_poll_epoch = float(state.get("last_poll_epoch") or 0)
    except Exception:
        last_poll_epoch = 0.0
    age_sec = now - last_poll_epoch if last_poll_epoch > 0 else None
    official = bool(state.get("official"))
    recent = age_sec is not None and age_sec <= _CONSUMER_RECENT_SEC
    passing = official and recent
    return {
        "passing": passing,
        "official": official,
        "consumer_name": state.get("consumer_name", ""),
        "consumer_id": state.get("consumer_id", ""),
        "consumer_version": state.get("consumer_version", ""),
        "consumer_capabilities": list(state.get("consumer_capabilities") or []),
        "agent_provider": state.get("agent_provider", ""),
        "agent_model": state.get("agent_model", ""),
        "agent_input_modalities": list(
            state.get("agent_input_modalities") or []
        ),
        "consumer_commit": state.get("consumer_commit", ""),
        "consumer_compat_commit": state.get("consumer_compat_commit", ""),
        "update_stall_reason": state.get("update_stall_reason", ""),
        "last_poll_at": state.get("last_poll_at", ""),
        "last_response_at": state.get("last_response_at", ""),
        "age_sec": age_sec,
        "recent_window_sec": _CONSUMER_RECENT_SEC,
        "decrypt_health": _decrypt_health_from_state(state, now_epoch=now),
        "required": (
            "Run the standard independent feedling-chat-resident / IO resident "
            "consumer with the current FEEDLING_API_KEY. It must poll "
            "FEEDLING_API_URL/v1/chat/poll and identify itself with the "
            "X-Feedling-Consumer headers."
        ),
    }


def consumer_supports_capability(
    store: UserStore,
    capability: str,
    *,
    now_epoch: float | None = None,
) -> bool:
    """Require a fresh official poll before trusting a resident capability."""
    validation = _consumer_validation_state(store, now_epoch=now_epoch)
    advertised = {
        str(item).strip().lower()
        for item in validation.get("consumer_capabilities") or []
        if str(item).strip()
    }
    return bool(validation.get("passing") and capability.lower() in advertised)


def consumer_agent_runtime(
    store: UserStore,
    *,
    now_epoch: float | None = None,
) -> dict:
    """Return fresh model metadata advertised by the official resident."""
    validation = _consumer_validation_state(store, now_epoch=now_epoch)
    if not validation.get("passing"):
        return {"provider": "", "model": "", "input_modalities": []}
    return {
        "provider": str(validation.get("agent_provider") or ""),
        "model": str(validation.get("agent_model") or ""),
        "input_modalities": sorted({
            str(item).strip().lower()
            for item in validation.get("agent_input_modalities") or []
            if str(item).strip().lower() in {"text", "image", "audio", "video"}
        }),
    }
