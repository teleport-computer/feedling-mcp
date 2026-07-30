"""Resident-consumer liveness state + validation gate input."""

import copy
import math
import os
import time
import uuid
from datetime import datetime
from typing import Callable, TypeVar

import db
from core.store import UserStore


_OFFICIAL_CONSUMER_NAME = "feedling-chat-resident"
VISION_OBSERVER_CAPABILITY = "vision_observer_v1"
VISION_PROBE_CAPABILITY = "vision_probe_v2"
_VISION_PROBE_TTL_SEC = max(
    30, min(300, int(os.environ.get("FEEDLING_VISION_PROBE_TTL_SEC", "120")))
)
_VISION_FAILURE_CODES = frozenset({
    "vision_model_auth_invalid",
    "vision_model_quota_insufficient",
    "vision_model_not_found",
    "vision_model_incompatible",
    "vision_model_rate_limited",
    "vision_model_unavailable",
    "vision_model_empty_response",
    "vision_model_failed",
})
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
_POLL_CONSUMER_HISTORY_LIMIT = 16
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
        "agent_input_modalities_source": (
            headers.get("X-Feedling-Agent-Input-Modalities-Source") or ""
        ).strip().lower()[:32],
        "agent_entry_signature": (
            headers.get("X-Feedling-Agent-Entry-Signature") or ""
        ).strip().lower()[:64],
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
            event_info.pop("agent_input_modalities_source", None)
            event_info.pop("agent_entry_signature", None)
            event_info.pop("update_stall_reason", None)
        state.update(event_info)
        state["last_event"] = event_type
        state["last_seen_at"] = now_iso
        state["last_seen_epoch"] = now_epoch
        if event_type == "poll":
            state["last_poll_at"] = now_iso
            state["last_poll_epoch"] = now_epoch
            # The legacy top-level consumer_id is last-writer-wins. Hosted V1
            # and a resident can alternate polls, continually hiding one
            # another. Keep a bounded per-identity last-poll record so the
            # admin plane can report both observations without treating the
            # latest sample as proof that the other consumer stopped.
            consumer_id = str(event_info.get("consumer_id") or "").strip()
            consumer_name = str(event_info.get("consumer_name") or "").strip()
            identity = consumer_id or consumer_name or "unknown"
            if consumer_id.startswith("agent-runner:"):
                responder = "hosted_v1"
            elif consumer_id == "hosted_runtime_v2":
                responder = "hosted_v2"
            else:
                responder = "resident"
            raw_pollers = state.get("poll_consumers")
            raw_pollers = raw_pollers if isinstance(raw_pollers, dict) else {}
            pollers = {
                str(key): dict(value)
                for key, value in raw_pollers.items()
                if isinstance(value, dict)
            }
            pollers[identity] = {
                "consumer_id": consumer_id,
                "consumer_name": consumer_name,
                "responder": responder,
                "last_poll_at": now_iso,
                "last_poll_epoch": now_epoch,
            }
            newest = sorted(
                pollers.items(),
                key=lambda item: float(item[1].get("last_poll_epoch") or 0),
                reverse=True,
            )[:_POLL_CONSUMER_HISTORY_LIMIT]
            state["poll_consumers"] = dict(newest)
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
        "agent_input_modalities_source": state.get(
            "agent_input_modalities_source", ""
        ),
        "agent_entry_signature": state.get("agent_entry_signature", ""),
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
        return {
            "provider": "",
            "model": "",
            "input_modalities": [],
            "input_modalities_source": "",
            "entry_signature": "",
            "consumer_id": "",
        }
    return {
        "provider": str(validation.get("agent_provider") or ""),
        "model": str(validation.get("agent_model") or ""),
        "input_modalities": sorted({
            str(item).strip().lower()
            for item in validation.get("agent_input_modalities") or []
            if str(item).strip().lower() in {"text", "image", "audio", "video"}
        }),
        "input_modalities_source": str(
            validation.get("agent_input_modalities_source") or ""
        ),
        "entry_signature": str(validation.get("agent_entry_signature") or ""),
        "consumer_id": str(validation.get("consumer_id") or ""),
    }


def _vision_binding(validation: dict) -> dict:
    return {
        "consumer_id": str(validation.get("consumer_id") or ""),
        "agent_entry_signature": str(
            validation.get("agent_entry_signature") or ""
        ),
        "provider": str(validation.get("agent_provider") or ""),
        "model": str(validation.get("agent_model") or ""),
    }


def _vision_binding_matches(binding: dict, current: dict) -> bool:
    return all(
        str(binding.get(key) or "") == str(current.get(key) or "")
        for key in ("consumer_id", "agent_entry_signature", "provider", "model")
    )


def begin_vision_probe(
    store: UserStore,
    *,
    images: list[str],
    expected: list[str],
    now_epoch: float | None = None,
) -> tuple[dict | None, str]:
    """Persist a hidden two-image probe bound to one fresh resident entry."""
    now = time.time() if now_epoch is None else float(now_epoch)
    validation = _consumer_validation_state(store, now_epoch=now)
    advertised = {
        str(item).strip().lower()
        for item in validation.get("consumer_capabilities") or []
    }
    binding = _vision_binding(validation)
    if (
        not validation.get("passing")
        or VISION_PROBE_CAPABILITY not in advertised
        or not binding["consumer_id"]
        or not binding["agent_entry_signature"]
    ):
        return None, "vision_resident_update_required"
    if len(images) != 2 or len(expected) != 2:
        return None, "vision_probe_invalid"

    created: dict = {}

    def mutate(state: dict) -> bool:
        current = _vision_binding({
            "consumer_id": state.get("consumer_id"),
            "agent_entry_signature": state.get("agent_entry_signature"),
            "agent_provider": state.get("agent_provider"),
            "agent_model": state.get("agent_model"),
        })
        if not _vision_binding_matches(binding, current):
            return False
        probe = state.get("resident_vision_probe")
        if (
            isinstance(probe, dict)
            and float(probe.get("expires_at_epoch") or 0) > now
            and _vision_binding_matches(binding, probe)
        ):
            created.update(probe)
            return True
        probe = {
            "probe_id": uuid.uuid4().hex,
            **binding,
            "created_at_epoch": now,
            "expires_at_epoch": now + _VISION_PROBE_TTL_SEC,
            "images": list(images),
            "expected": list(expected),
        }
        state["resident_vision_probe"] = probe
        created.update(probe)
        return True

    mutated = _mutate_consumer_state(store, mutate)
    if mutated is None or not mutated[1] or not created:
        return None, "vision_resident_changed"
    return created, ""


def vision_probe_for_poll(
    store: UserStore,
    consumer_info: dict | None,
    *,
    now_epoch: float | None = None,
) -> dict | None:
    """Project the pending probe without its server-only expected answers."""
    if not isinstance(consumer_info, dict) or not consumer_info.get("official"):
        return None
    advertised = {
        str(item).strip().lower()
        for item in consumer_info.get("consumer_capabilities") or []
    }
    if VISION_PROBE_CAPABILITY not in advertised:
        return None
    now = time.time() if now_epoch is None else float(now_epoch)
    state = _load_consumer_state(store)
    probe = state.get("resident_vision_probe")
    if not isinstance(probe, dict) or float(probe.get("expires_at_epoch") or 0) <= now:
        return None
    binding = _vision_binding({
        "consumer_id": consumer_info.get("consumer_id"),
        "agent_entry_signature": consumer_info.get("agent_entry_signature"),
        "agent_provider": consumer_info.get("agent_provider"),
        "agent_model": consumer_info.get("agent_model"),
    })
    if not _vision_binding_matches(binding, probe):
        return None
    images = probe.get("images")
    if not isinstance(images, list) or len(images) != 2:
        return None
    return {
        "probe_id": str(probe.get("probe_id") or ""),
        "expires_at_epoch": float(probe.get("expires_at_epoch") or 0),
        "images": [
            {
                "mime_type": "image/png",
                "data_url": f"data:image/png;base64,{image}",
            }
            for image in images
        ],
    }


def complete_vision_probe(
    store: UserStore,
    payload: dict,
    consumer_info: dict | None,
    *,
    now_epoch: float | None = None,
) -> tuple[dict, int]:
    """Validate the exact resident/entry/expiry tuple and persist its verdict."""
    if not isinstance(consumer_info, dict) or not consumer_info.get("official"):
        return {"error": "vision_probe_consumer_mismatch"}, 409
    now = time.time() if now_epoch is None else float(now_epoch)
    probe_id = str(payload.get("probe_id") or "").strip()
    binding = _vision_binding({
        "consumer_id": consumer_info.get("consumer_id"),
        "agent_entry_signature": consumer_info.get("agent_entry_signature"),
        "agent_provider": consumer_info.get("agent_provider"),
        "agent_model": consumer_info.get("agent_model"),
    })
    outcome: dict = {}

    def mutate(state: dict) -> str:
        probe = state.get("resident_vision_probe")
        previous = state.get("resident_vision_validation")
        if (
            not isinstance(probe, dict)
            and isinstance(previous, dict)
            and str(previous.get("probe_id") or "") == probe_id
            and _vision_binding_matches(binding, previous)
        ):
            outcome.update(previous)
            return "idempotent"
        if not isinstance(probe, dict) or str(probe.get("probe_id") or "") != probe_id:
            return "probe_mismatch"
        if not _vision_binding_matches(binding, probe):
            return "consumer_mismatch"
        if float(probe.get("expires_at_epoch") or 0) <= now:
            return "expired"

        reported_status = str(payload.get("status") or "ok").strip().lower()
        if reported_status == "failed":
            error_code = str(payload.get("error_code") or "vision_model_failed")
            if error_code not in _VISION_FAILURE_CODES:
                error_code = "vision_model_failed"
            status = "failed"
        else:
            observed = payload.get("observed")
            expected = probe.get("expected")
            matched = (
                isinstance(observed, list)
                and len(observed) == 2
                and all(isinstance(item, str) for item in observed)
                and observed == expected
            )
            status = "ok" if matched else "unsupported"
            error_code = "" if matched else "vision_model_incompatible"
        result = {
            "probe_id": probe_id,
            **binding,
            "status": status,
            "error_code": error_code,
            "tested_at_epoch": now,
        }
        state["resident_vision_validation"] = result
        state.pop("resident_vision_probe", None)
        outcome.update(result)
        return "completed"

    mutated = _mutate_consumer_state(store, mutate)
    if mutated is None:
        return {"error": "vision_probe_state_unavailable"}, 503
    result = mutated[1]
    if result == "probe_mismatch":
        return {"error": "vision_probe_not_found"}, 404
    if result == "consumer_mismatch":
        return {"error": "vision_probe_consumer_mismatch"}, 409
    if result == "expired":
        return {"error": "vision_probe_expired"}, 410
    return {
        "status": str(outcome.get("status") or "failed"),
        "probe_id": probe_id,
        "error_code": str(outcome.get("error_code") or ""),
    }, 200


def resident_vision_validation(
    store: UserStore,
    *,
    now_epoch: float | None = None,
) -> dict:
    """Return a four-state verdict, invalidating it on any entry change."""
    now = time.time() if now_epoch is None else float(now_epoch)
    validation = _consumer_validation_state(store, now_epoch=now)
    runtime = consumer_agent_runtime(store, now_epoch=now)
    current = {
        "consumer_id": runtime.get("consumer_id", ""),
        "agent_entry_signature": runtime.get("entry_signature", ""),
        "provider": runtime.get("provider", ""),
        "model": runtime.get("model", ""),
    }
    if not validation.get("passing"):
        return {"status": "untested", "error_code": "resident_unavailable", **runtime}
    state = _load_consumer_state(store)
    probe = state.get("resident_vision_probe")
    if isinstance(probe, dict) and _vision_binding_matches(current, probe):
        if float(probe.get("expires_at_epoch") or 0) <= now:
            return {"status": "failed", "error_code": "vision_probe_expired", **runtime}
        return {"status": "testing", "error_code": "", "pending": True, **runtime}
    saved = state.get("resident_vision_validation")
    if isinstance(saved, dict) and _vision_binding_matches(current, saved):
        return {
            "status": str(saved.get("status") or "untested"),
            "error_code": str(saved.get("error_code") or ""),
            "tested_at_epoch": float(saved.get("tested_at_epoch") or 0),
            **runtime,
        }
    source = str(runtime.get("input_modalities_source") or "")
    if source in {"pi_catalog", "explicit"}:
        modalities = set(runtime.get("input_modalities") or [])
        return {
            "status": "ok" if "image" in modalities else "unsupported",
            "error_code": "" if "image" in modalities else "vision_model_incompatible",
            **runtime,
        }
    return {"status": "untested", "error_code": "", **runtime}
