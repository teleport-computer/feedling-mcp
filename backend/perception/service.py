"""Extended Perception business logic.

All the generic machinery driven by catalog.py:
  - ingest_snapshot_v2(): current iOS report adapter; updates snapshot fields
    while routing wake-capable observations through PerceptionDifferV2.
  - ingest(): legacy/internal sparse ingest kept during the strangler migration.
  - snapshot(): current authorized+fresh fields; unauthorized/stale -> null.
  - permissions / config views and updates.
  - user_state manual override; Focus is pull-only presence context.
  - photo encrypted ingest; photo_added wakes go through PerceptionDifferV2
    only when the V2 ingress rollout flag is enabled.
  - generic collection ingest/read kept for legacy Tier 2 health clients.

No business logic lives in the assembly layer. The only upward coupling is a
lazy import in _fire_wake_event_v2(): resident users enqueue the compatibility
proactive job, while pooled Runtime V2 users enqueue an ``agent_jobs`` wake.
"""
from __future__ import annotations

import logging
import json
import math
import time
from datetime import datetime
from typing import Any, Callable, Mapping

from content_encryption import random_item_id
from core import envelope as core_envelope
from core import enclave as core_enclave  # noqa: F401 — 读侧已改走 core_envelope.read_envelope_body；
# 这行保留是因为测试 monkeypatch `<module>.core_enclave` 上的
# _decrypt_envelope_via_enclave（patch 的是共享模块对象，仍然生效）。

from . import catalog, history, permissions, resolve, store
from .ingress_v2 import device_event_observations_v2, operation_observations_v2, observe_signal_v2
from .ios_contract_v2 import (
    ENCRYPTED_SIGNAL_KEYS_V2,
    IGNORED_SIGNAL_KEYS_V2,
    OPERATION_SIGNAL_KEYS_V2,
    classify_item_v2,
)

log = logging.getLogger("perception.service")


def _now() -> float:
    return time.time()


_FUTURE_TS_TOLERANCE_SEC = 60.0  # allow minor client clock skew
PERCEPTION_INGRESS_RUNTIME_V2_FLAG = "perception_ingress_runtime_v2_enabled"


def _coerce_ts(client_ts) -> float:
    """A report's logical time: its client_ts when present, else now. Using the
    record's own time makes batch/offline replay correct — per-field freshness
    and wake debounce are evaluated against when the signal actually happened.

    A FUTURE timestamp (clock skew, or milliseconds sent instead of seconds) is
    clamped to now: otherwise it would keep the snapshot permanently "fresh" and
    make the ordering guard reject every correctly-timestamped report until
    wall-clock catches up, freezing perception state."""
    now = _now()
    try:
        ts = float(client_ts)
    except (TypeError, ValueError):
        return now
    return now if ts > now + _FUTURE_TS_TOLERANCE_SEC else ts


def perception_ingress_runtime_v2_enabled(user_or_store) -> bool:
    """Per-user perception ingress lane — tied 1:1 to the hosted chat runtime
    fence (dual-runtime coexistence spec §6: "perception ingress 跟随 chat
    mode"). Delegates to the same ownership fence chat send routes on
    (``hosted.config_store.get_hosted_runtime_mode_strict``) instead of an
    independent flag, so this lane can never drift from chat mode (flag says
    v2, fence says resident). ``perception_ingress_runtime_v2_enabled`` in a
    persisted ``model_api_runtime`` profile is now a vestigial/legacy field —
    nothing reads it anymore.

    Reading only ``mode`` here (not the paired ``state``) is safe: the mode
    field and the runtime-state row are the SAME writer's atomic commit
    (``db.patch_blob_strict(runtime_state_target=...)`` flips both in one
    transaction), so mode can never be observed ahead of state.
    """
    try:
        user_store = user_or_store
        if isinstance(user_or_store, str):
            from core import store as core_store  # lazy
            user_store = core_store.get_store(user_or_store)

        from hosted import config_store as hosted_config_store  # lazy

        mode = hosted_config_store.get_hosted_runtime_mode_strict(user_store)
        return mode == hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    except Exception as e:
        uid = user_or_store if isinstance(user_or_store, str) else getattr(user_or_store, "user_id", "unknown")
        log.warning("perception ingress v2 fence read failed for %s; using legacy ingress: %s", uid, e)
        return False


# ---------------------------------------------------------------------------
# Permission gating
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Ingest (generic sparse report)
# ---------------------------------------------------------------------------

def _parse_data(data):
    """A context_snapshot item's `data` is a STRING: JSON, or "null"/""/None for
    "no value". Returns the parsed Python value, None for the null cases, or the
    raw string if it isn't valid JSON (lenient)."""
    if data is None:
        return None
    if isinstance(data, str):
        s = data.strip()
        if s == "" or s.lower() == "null":
            return None
        try:
            return json.loads(s)
        except Exception:
            return s
    return data  # already structured (lenient)


def _decode_decrypted_payload_v2(raw: bytes | str | Mapping[str, Any] | list[Any]) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _decrypt_signal_payload_v2(
    key: str,
    envelope: Mapping[str, Any],
    *,
    api_key: str | None = None,
    decrypt_envelope: Callable[..., bytes | str | Mapping[str, Any] | list[Any]] | None = None,
) -> tuple[Any | None, str]:
    if not isinstance(envelope, Mapping):
        return None, "invalid_envelope"
    if not api_key and decrypt_envelope is None:
        return None, "decrypt_skipped"
    try:
        # 默认走形状路由：明文行直读、信封行才打 enclave。签名与
        # _decrypt_envelope_via_enclave 逐字一致，注入方（测试/上层）不受影响。
        decrypt = decrypt_envelope or core_envelope.read_envelope_body
        raw = decrypt(dict(envelope), api_key, purpose=f"perception:{key}")
        return _decode_decrypted_payload_v2(raw), ""
    except Exception as e:
        return None, f"decrypt_failed:{type(e).__name__}"


def _record_decrypt_failure_v2(user_id: str, key: str, reason: str, ts: float) -> None:
    """Make a failed sensitive-signal decrypt visible.

    The ingest still answers "accepted" (the report contract is "we took your
    envelope") and deliberately writes no value — but a silent skip is
    indistinguishable from "this user has no reading", which is exactly how the
    2026-07-24 null-perception regression stayed invisible for hours. One
    warning + one audit row per failed signal, so a fleet-wide enclave hiccup
    or a caller that forgot to forward the api key (``decrypt_skipped``) shows
    up in logs and in a queryable stream instead of nowhere.

    The row goes to its OWN stream, never the wake-audit one: ``_last_wake_ts``
    / ``_last_v2_wake_ts`` scan only the newest 50 rows of that stream to find
    the last wake, so a burst of failures there would evict the wake row and
    silently disable burst/cluster dedup — precisely when the fleet is already
    unhealthy.
    """
    log.warning("perception v2 decrypt failed for %s key=%s: %s", user_id, key, reason)
    try:
        store.append_decrypt_failure(user_id, {
            "cap": "perception",
            "type": "decrypt_failed",
            "key": key,
            "reason": reason,
            "ts": ts,
        }, ts)
    except Exception as e:  # observability must never break ingest
        log.warning("perception decrypt-failure audit write failed for %s: %s", user_id, e)


def _decrypted_location_anchor_id_v2(plaintext: Any) -> str:
    if not isinstance(plaintext, Mapping):
        return ""
    values = plaintext.get("values")
    if isinstance(values, Mapping):
        plaintext = values
    anchor = plaintext.get("wifi_anchor_id")
    if anchor is None:
        return ""
    return str(anchor).strip()


def _decrypted_signal_values_and_message_v2(
    plaintext: Any,
    *,
    fallback_message: str = "",
) -> tuple[Any, str]:
    """Normalize the iOS EncryptedBody wrapper after enclave decrypt.

    Real iOS payloads decrypt to {"values": {...}, "message": "..."}; older
    tests and local callers may still provide the values object directly.
    """
    if isinstance(plaintext, Mapping):
        values = plaintext.get("values")
        if isinstance(values, Mapping):
            return values, str(plaintext.get("message") or fallback_message or "")
    return plaintext, fallback_message


def _storage_value_for_decrypted_signal_v2(key: str, values: Any) -> Any:
    sig = catalog.SIGNALS.get(key)
    if (
        sig is not None
        and sig.resolver is None
        and len(sig.outputs) == 1
        and isinstance(values, Mapping)
    ):
        output_key = sig.outputs[0]
        if output_key in values:
            return values[output_key]
    return values


def ingest_snapshot(user_id: str, items: list, client_ts=None) -> dict:
    """Ingest a {context_snapshot:[{key,data,message}]} report. `data` is a JSON
    string (or "null"). Composite keys (e.g. device) expand into their sub-signals;
    aliases (e.g. location_signal -> location) are normalized."""
    pairs: list[tuple] = []  # (input_name, value, message)
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "").strip()
        if not key:
            continue
        msg = it.get("message")
        value = _parse_data(it.get("data"))
        if key in catalog.COMPOSITE_KEYS:
            subs = catalog.COMPOSITE_KEYS[key]
            if isinstance(value, dict):
                for sub in subs:
                    if sub in value:
                        pairs.append((sub, value[sub], msg))
            elif value is None:
                for sub in subs:
                    pairs.append((sub, None, msg))
            continue
        pairs.append((key, value, msg))
    return _apply(user_id, pairs, client_ts)


def record_context_timezone(user_id: str, timezone: str, locale: str = "") -> bool:
    """Persist the device timezone/locale from a NON-perception, already-disclosed
    channel (the proactive app-presence device event), NOT the perception upload
    pipeline. Writes TWO places:

    1. The first-class user RECORD (via registry) — the timezone migration's
       authoritative source, read by /v1/users/whoami and the resident consumer's
       current_time anchor, decoupled from perception.
    2. The perception snapshot `time` signal (timezone + locale) — STILL written
       because `locale` from this channel feeds the resident consumer's
       reply-language guardrail: `_reply_language_line` reads `locale` from
       /v1/agent/perception?signals=now, so a perception-upload-OFF user relies on
       this write to keep proactive replies in their language. It also keeps the
       whoami perception fallback (stable_context_timezone) fed for transitional
       users.

    timezone/locale are TTL-exempt (see _STABLE_CONTEXT_FIELDS); the volatile
    `local_time` in the same signal expires normally. Returns True when a valid
    timezone was written. Kept out of the wake path: time is a non-significant
    signal, no wake fires."""
    tz = str(timezone or "").strip()
    if not tz:
        return False
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)  # reject unknown zones; don't poison state with junk
    except Exception:
        return False
    from datetime import timezone as _utc
    item = {
        "key": "time",
        "data": json.dumps({
            "local_time": datetime.now(_utc.utc).isoformat(),
            "timezone": tz,
            "locale": str(locale or "").strip(),
        }),
        "message": "device timezone/locale (proactive app-presence)",
    }
    ingest_snapshot(user_id, [item])
    from accounts import registry
    registry._set_user_timezone(user_id, tz)  # first-class record (may no-op if user unknown)
    return True


def ingest_snapshot_v2(
    user_id: str,
    items: list,
    client_ts=None,
    *,
    api_key: str | None = None,
    decrypt_envelope: Callable[..., bytes | str | Mapping[str, Any] | list[Any]] | None = None,
) -> dict:
    """Ingest the current iOS report contract without letting the old service
    directly create wakes.

    Plain operation values still update the existing snapshot store. Encrypted
    sensitive values are accepted as envelopes, but they do not become differ
    observations until an enclave/decrypt adapter supplies plaintext values.
    """
    now = _coerce_ts(client_ts)
    storage_items: list[dict] = []
    location_anchor_observations: list[tuple[str, Any]] = []
    results: dict[str, str] = {}

    for item in (items or []):
        if not isinstance(item, dict):
            continue
        signal = classify_item_v2(item)
        key = signal.key
        if not key:
            continue
        if key in IGNORED_SIGNAL_KEYS_V2:
            results[key] = "ignored"
            continue
        if signal.status in {
            "unknown_signal",
            "missing_key",
            "invalid_changed_flag",
            "invalid_plaintext_sensitive_signal",
        }:
            results[key] = signal.status
            continue
        if key in OPERATION_SIGNAL_KEYS_V2:
            if key in catalog.SIGNALS:
                storage_items.append(item)
            for observation in operation_observations_v2(key, signal.data):
                observe_signal_v2(
                    user_id,
                    observation.signal,
                    observation.value,
                    ts=now,
                    origin_refs=observation.origin_refs,
                    submit_wake=_submit_wake_event_v2_compat,
                )
            if key not in catalog.SIGNALS:
                results[key] = "accepted"
            continue
        if key in ENCRYPTED_SIGNAL_KEYS_V2:
            if signal.encrypted:
                results[key] = "accepted"
                plaintext, err = _decrypt_signal_payload_v2(
                    key,
                    item.get("envelope") if isinstance(item.get("envelope"), Mapping) else {},
                    api_key=api_key,
                    decrypt_envelope=decrypt_envelope,
                )
                if err == "":
                    values, msg = _decrypted_signal_values_and_message_v2(
                        plaintext,
                        fallback_message=signal.message,
                    )
                    storage_items.append({
                        "key": key,
                        "data": json.dumps(_storage_value_for_decrypted_signal_v2(key, values)),
                        "message": msg,
                    })
                    if key == "location_signal" and signal.changed is True:
                        location_anchor_observations.append((key, values))
                else:
                    _record_decrypt_failure_v2(user_id, key, err, now)
            else:
                storage_items.append(item)
            continue
        results[key] = "unknown_signal"

    if storage_items:
        results.update(_ingest_snapshot_storage_only(user_id, storage_items, client_ts=client_ts))
    for key, plaintext in location_anchor_observations:
        if results.get(key) != "accepted":
            continue
        anchor_id = _decrypted_location_anchor_id_v2(plaintext)
        if not anchor_id:
            continue
        observe_signal_v2(
            user_id,
            "wifi_anchor",
            anchor_id,
            ts=now,
            origin_refs=("ios_report:location_signal",),
            submit_wake=_submit_wake_event_v2_compat,
        )
    return results


def ingest_device_event_v2(user_id: str, event: dict) -> dict:
    observations = device_event_observations_v2(event if isinstance(event, dict) else {})
    submitted = 0
    for observation in observations:
        result = observe_signal_v2(
            user_id,
            observation.signal,
            observation.value,
            ts=float((event or {}).get("ts") or _now()),
            origin_refs=observation.origin_refs,
            submit_wake=_submit_wake_event_v2_compat,
        )
        submitted += len(result.wake_events)
    return {
        "observations": len(observations),
        "wake_events": submitted,
    }


def ingest(user_id: str, signals: dict, client_ts: float | None = None) -> dict:
    """Back-compat / internal: ingest a flat {key: value} map (no messages)."""
    return _apply(user_id, [(k, v, None) for k, v in (signals or {}).items()], client_ts)


def _ingest_snapshot_storage_only(user_id: str, items: list, client_ts=None) -> dict:
    pairs: list[tuple] = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        key = str(it.get("key") or "").strip()
        if not key:
            continue
        msg = it.get("message")
        value = _parse_data(it.get("data"))
        if key in catalog.COMPOSITE_KEYS:
            subs = catalog.COMPOSITE_KEYS[key]
            if isinstance(value, dict):
                for sub in subs:
                    if sub in value:
                        pairs.append((sub, value[sub], msg))
            elif value is None:
                for sub in subs:
                    pairs.append((sub, None, msg))
            continue
        pairs.append((key, value, msg))
    return _apply(user_id, pairs, client_ts, emit_legacy_wakes=False)


def _cell(value, ts: float, msg) -> dict:
    cell = {"v": value, "ts": ts}
    if msg is not None:
        cell["msg"] = msg
    return cell


def _apply(user_id: str, pairs: list, client_ts=None, *, emit_legacy_wakes: bool = True) -> dict:
    now = _coerce_ts(client_ts)
    config = store.get_config(user_id)
    prev_state = store.get_state(user_id)
    results: dict[str, str] = {}
    patch: dict[str, dict] = {}          # field -> candidate cell (ts-guarded on write)
    input_fields: dict[str, list] = {}   # input_name -> output fields it proposed
    wake_pending: list[tuple] = []       # (cap_key, debounce, field, old, new)
    hist_obs: dict[str, dict] = {}       # catalog signal key -> resolved values (for Tier 2)
    tz_seen: str | None = None

    for input_name, value, msg in pairs:
        if input_name in catalog.IGNORED_KEYS:
            results[input_name] = "ignored"  # e.g. "unsupported" (all-null placeholder)
            continue
        # Manual user_state arrives as an explicit report key (folds POST /user_state).
        # The store applies it atomically under a row lock with a ts guard, so a
        # late/concurrent older report can't overwrite a newer manual value.
        if input_name == "user_state":
            doc = store.set_manual_user_state_guarded(
                user_id, value if value is not None else "default", now)
            results[input_name] = "accepted" if doc.get("manual_ts") == now else "stale_ignored"
            continue
        key = catalog.KEY_ALIASES.get(input_name, input_name)
        sig = catalog.SIGNALS.get(key)
        if sig is None:
            results[input_name] = "unknown_signal"
            continue

        fields: list[str] = []
        if value is None:
            # data:"null" -> field unavailable now; record null + message. No wake.
            for fname in sig.outputs:
                patch[fname] = _cell(None, now, msg)
                fields.append(fname)
        else:
            # Resolve raw -> label (raw discarded) or store as-is.
            if sig.resolver:
                fn = resolve.RESOLVERS.get(sig.resolver)
                resolved = fn(value, config) if fn else {}
            elif isinstance(value, Mapping) and len(sig.outputs) > 1:
                if any(fname in value for fname in sig.outputs):
                    resolved = {fname: value.get(fname) for fname in sig.outputs if fname in value}
                    if key == "calendar_next_event" and "calendar_next_event" not in resolved:
                        resolved["calendar_next_event"] = None
                else:
                    resolved = {sig.outputs[0]: value}
            else:
                resolved = {sig.outputs[0]: value}
            cap = catalog.CAPABILITIES[sig.capability]
            for fname in sig.outputs:
                if fname not in resolved:
                    continue
                new_v = resolved[fname]
                patch[fname] = _cell(new_v, now, msg)
                fields.append(fname)
                old = (prev_state.get(fname) or {}).get("v")
                if cap.wake_source and sig.significant and new_v != old:
                    wake_pending.append((sig.capability, cap.debounce_sec, fname, old, new_v))
            if key == "time" and isinstance(resolved, dict):
                tz_seen = resolved.get("timezone") or tz_seen
            if history.is_historized(key) and isinstance(resolved, dict):
                hist_obs[key] = dict(resolved)
        input_fields[input_name] = fields

    # Atomic ts-guarded write under a row lock: a field is persisted only if its
    # new ts >= the currently-stored ts. Doing the compare-and-write atomically
    # (not against the pre-read prev_state) is what makes the "late older record
    # never clobbers a newer value" guarantee hold under concurrent reports.
    written = store.merge_state_guarded(user_id, patch) if patch else set()

    for input_name, fields in input_fields.items():
        results[input_name] = "accepted" if any(f in written for f in fields) else "stale_ignored"

    # device "back after a long lock" wake (only if the field was actually written).
    if emit_legacy_wakes:
        _maybe_unlock_wake(user_id, patch, written, prev_state, now, wake_pending)

    for (capk, debounce, f, old, new_v) in wake_pending:
        if emit_legacy_wakes and f in written:
            _maybe_wake(user_id, capk, debounce, f, old, new_v, now)

    # Tier 2 quantitative history: fold each historized signal's observation into
    # its device-local daily rollup (field-agnostic — see perception/history.py).
    # Only for signals that actually wrote a field this report (skips stale/older).
    # Best-effort: history is Tier 2 and must NEVER break Tier 1 state ingest, so
    # the whole block is guarded (also tolerates stores without the daily helper).
    if hist_obs and hasattr(store, "merge_perception_daily"):
        try:
            local_date = _local_date(now, tz_seen or (prev_state.get("timezone") or {}).get("v"))
            for sig_key, obs in hist_obs.items():
                outs = catalog.SIGNALS[sig_key].outputs if sig_key in catalog.SIGNALS else ()
                if not any(f in written for f in outs):
                    continue
                store.merge_perception_daily(
                    user_id, local_date, sig_key,
                    lambda prev, _o=obs, _k=sig_key: history.record_daily(prev, _k, _o, ts=now),
                    now,
                )
        except Exception as e:
            log.warning("perception_daily history rollup failed (non-fatal): %s", e)

    return results


def _local_date(now: float, tz: str | None) -> str:
    """Device-local 'YYYY-MM-DD' for day-bucketing the history rollup. Falls back
    to UTC when the timezone is unknown/invalid."""
    from datetime import datetime, timezone as _utc
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return datetime.fromtimestamp(now, ZoneInfo(tz)).strftime("%Y-%m-%d")
        except Exception:
            pass
    return datetime.fromtimestamp(now, _utc.utc).strftime("%Y-%m-%d")


def _maybe_unlock_wake(user_id, patch, written, prev_state, now, wake_candidates) -> None:
    if "last_unlock_ago_sec" not in written:
        return
    cell = patch.get("last_unlock_ago_sec")
    if not cell or cell.get("v") is None:
        return
    try:
        new = float(cell["v"])
    except (TypeError, ValueError):
        return
    prev = (prev_state.get("last_unlock_ago_sec") or {}).get("v")
    try:
        was_long = prev is None or float(prev) >= catalog.UNLOCK_BACK_THRESHOLD_SEC
    except (TypeError, ValueError):
        was_long = True
    if new <= 60 and was_long:
        wake_candidates.append(("device", 0.0, "last_unlock_ago_sec", prev, new))


# ---------------------------------------------------------------------------
# Wake triggering (debounced; reuses the existing proactive-job mechanism)
# ---------------------------------------------------------------------------

def _app_proactive_settings(user_id: str) -> dict:
    """Best-effort read of the app-level proactive settings (enabled/dnd/
    user_state). Lazy import like _fire_wake; failures mean "no block" so a
    broken app layer can't silently kill perception observability."""
    from core import store as core_store  # lazy; assembly loads core first
    return core_store.get_store(user_id).load_proactive_settings()


def _wake_block_reason(user_id: str) -> str:
    """Global mechanical gate for automatic legacy perception wakes.

    Dedicated photo/arrival/unlock consent is applied separately by
    ``_legacy_wake_control_decision``. The historical ``enabled`` field is the
    Heartbeat switch, not a master perception gate. DND and Away remain global.
    """
    try:
        settings = _app_proactive_settings(user_id) or {}
    except Exception as e:
        log.warning("wake gate settings read failed for %s: %s", user_id, e)
        settings = {}
    if settings.get("dnd", False):
        return "dnd_enabled"
    if str(settings.get("user_state") or "") == "away":
        return "user_away"
    if effective_user_state(user_id) == "away":
        return "user_away"
    return ""


def _last_wake_ts(user_id: str, cap_key: str) -> float:
    for ev in reversed(store.read_events(user_id, limit=50)):
        if ev.get("cap") == cap_key and ev.get("type") == "wake":
            return float(ev.get("ts") or 0)
    return 0.0


def _last_v2_wake_ts(user_id: str, trigger: str) -> float:
    """Timestamp of the last accepted runtime_v2 wake with this trigger.

    V2 wakes are recorded by _submit_wake_event_v2_compat under cap "runtime_v2";
    used to reapply the legacy burst/cluster dedup on the v2 path."""
    for ev in reversed(store.read_events(user_id, limit=50)):
        if (ev.get("cap") == "runtime_v2" and ev.get("type") == "wake"
                and ev.get("trigger") == trigger):
            return float(ev.get("ts") or 0)
    return 0.0


def _settings_v2_for_user(user_id: str):
    from proactive.store_v2 import DBProactiveSettingsStoreV2  # lazy
    return DBProactiveSettingsStoreV2().load(user_id)


def _proactive_activation_ready(user_id: str) -> bool:
    from core import store as core_store  # lazy
    return core_store.get_store(user_id).proactive_activation_ready()


_LEGACY_WAKE_TRIGGER_BY_CAPABILITY = {
    "photos": "photo_added",
    "location": "arrived_at_anchor",
    "wifi": "arrived_at_anchor",
    "region": "arrived_at_anchor",
    # The only legacy device wake candidate is last_unlock_ago_sec.
    "device": "unlock_after_absence",
}


def _legacy_wake_trigger(cap_key: str) -> str:
    """Map every supported legacy wake source onto its V2 consent trigger."""
    return _LEGACY_WAKE_TRIGGER_BY_CAPABILITY.get(str(cap_key or "").strip(), "")


def _legacy_wake_control_decision(user_id: str, cap_key: str):
    from proactive.controls_v2 import evaluate_wake_control_v2  # lazy

    trigger = _legacy_wake_trigger(cap_key)
    if not trigger:
        return trigger, None
    return trigger, evaluate_wake_control_v2(
        "perception_event",
        trigger=trigger,
        settings=_settings_v2_for_user(user_id),
    )


def _submit_wake_event_v2_compat(event) -> None:
    """Compatibility output: V2 differ event -> old proactive job queue.

    The wake has already been mechanically selected by PerceptionDifferV2. This
    function only applies the V2 switch gate and writes a legacy job so the
    existing hosted/resident consumers can pick it up during the strangler
    migration.
    """
    from proactive.controls_v2 import evaluate_wake_control_v2  # lazy

    settings = _settings_v2_for_user(event.user_id)
    decision = evaluate_wake_control_v2(
        event.source,
        trigger=event.trigger,
        manual=event.manual,
        settings=settings,
    )
    now = float(event.created_at or _now())
    if not event.manual and not _proactive_activation_ready(event.user_id):
        store.append_event(event.user_id, {
            "cap": "runtime_v2",
            "type": "suppressed",
            "reason": "activation_pending",
            "source": event.source,
            "trigger": event.trigger,
            "change_digest": event.change_digest,
            "origin_refs": list(event.origin_refs or ()),
            "ts": now,
        }, now)
        return
    if not decision.accepted:
        store.append_event(event.user_id, {
            "cap": "runtime_v2",
            "type": "suppressed",
            "reason": decision.reason,
            "source": event.source,
            "trigger": event.trigger,
            "change_digest": event.change_digest,
            "origin_refs": list(event.origin_refs or ()),
            "ts": now,
        }, now)
        return
    store.append_event(event.user_id, {
        "cap": "runtime_v2",
        "type": "wake",
        "wake_id": str(event.wake_id or ""),
        "source": event.source,
        "trigger": event.trigger,
        "change_digest": event.change_digest,
        "origin_refs": list(event.origin_refs or ()),
        "presence_hints": dict(event.presence_hints or {}),
        "ts": now,
    }, now)
    _fire_wake_event_v2(event)


def _fire_wake_event_v2(event) -> None:
    if not event.change_digest or not event.origin_refs:
        log.error("drop v2 perception wake without digest/origin_refs: %s", event)
        return
    try:
        from core import store as core_store  # lazy
        from core import util as core_util  # lazy
        from core import wake_bus as core_wake_bus  # lazy
        from hosted import config_store as hosted_config_store  # lazy
        from model_api_runtime.v2 import jobs_store  # lazy
        from proactive import service as proactive_service  # lazy
        s = core_store.get_store(event.user_id)
        if not event.manual and not s.proactive_activation_ready():
            return
        # Strict fence before selecting the queue. A failed control-plane read
        # must not silently strand a V2 event in resident ``proactive_jobs``.
        runtime_mode = hosted_config_store.get_hosted_runtime_mode_strict(s)
        if runtime_mode == hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2:
            job_id, _ = jobs_store.enqueue_job_with_context_log(
                event.user_id,
                "heartbeat",
                reason=str(event.trigger or event.source or "perception_event")[:120],
                trace_id=str(event.wake_id or "")[:160] or None,
                context_stream=store.V2_WAKE_CONTEXT_STREAM,
                context_doc={
                    "wake_id": str(event.wake_id or "")[:160],
                    "source": str(event.source or "")[:120],
                    "trigger": str(event.trigger or "")[:120],
                    "change_digest": str(event.change_digest or "")[:2000],
                    "origin_refs": [
                        str(ref)[:200]
                        for ref in list(event.origin_refs or ())[:10]
                    ],
                    "presence_hints": dict(event.presence_hints or {}),
                    "created_at": float(event.created_at or _now()),
                },
                context_ts=float(event.created_at or _now()),
            )
            store.trim_v2_wake_context(event.user_id)
            # A coalesced pending job may have been discovered only through a
            # slow poll. Notify after every successful association so the new
            # context is visible promptly; duplicate NOTIFY is harmless.
            core_wake_bus.notify("v2_jobs", event.user_id)
            return
        job = {
            "job_id": core_util._new_public_id("pj"),
            "ts": float(event.created_at or _now()),
            "created_at": datetime.fromtimestamp(float(event.created_at or _now())).isoformat(),
            "source": proactive_service.PROACTIVE_JOB_SOURCE,
            "status": "pending",
            "intent_label": str(event.trigger or event.source)[:120],
            "trigger": str(event.trigger or event.source)[:120],
            "wake_kind": str(event.source or "perception_event")[:120],
            "context_hint": str(event.change_digest or "")[:2000],
            "change_digest": str(event.change_digest or "")[:2000],
            "presence_hints": dict(event.presence_hints or {}),
            "origin_refs": list(event.origin_refs or ()),
            "connections": [],
            "connection": {},
            "frame_ids": [],
            "device_event_ids": [],
            "current_app": "",
            "payload": {"v2_wake": dict(event.payload or {})},
        }
        s.append_proactive_job(job)
    except Exception as e:
        log.error("fire_wake_event_v2(%s,%s) failed: %s", event.user_id, event.trigger, e)


def _maybe_wake(user_id, cap_key, debounce, field, old, new_v, now) -> None:
    block = _wake_block_reason(user_id)
    if block:
        store.append_event(user_id, {
            "cap": cap_key, "type": "suppressed", "reason": block,
            "field": field, "old": old, "new": new_v, "ts": now,
        }, now)
        return
    if not _proactive_activation_ready(user_id):
        store.append_event(user_id, {
            "cap": cap_key, "type": "suppressed", "reason": "activation_pending",
            "field": field, "old": old, "new": new_v, "ts": now,
        }, now)
        return
    trigger, decision = _legacy_wake_control_decision(user_id, cap_key)
    if decision is None:
        log.warning("drop unmapped legacy perception wake: cap=%s field=%s", cap_key, field)
        return
    if not decision.accepted:
        store.append_event(user_id, {
            "cap": cap_key, "type": "suppressed", "reason": decision.reason,
            "trigger": trigger, "field": field, "old": old, "new": new_v, "ts": now,
        }, now)
        return
    if debounce and (now - _last_wake_ts(user_id, cap_key)) < debounce:
        store.append_event(user_id, {
            "cap": cap_key, "type": "debounced", "trigger": trigger, "field": field,
            "old": old, "new": new_v, "ts": now,
        }, now)
        return
    store.append_event(user_id, {
        "cap": cap_key, "type": "wake", "trigger": trigger, "field": field,
        "old": old, "new": new_v, "ts": now,
    }, now)
    _fire_wake(user_id, cap_key, _wake_hint(cap_key, field, old, new_v), now)


def _wake_hint(cap_key: str, field: str, old, new_v) -> str:
    if cap_key == "location":
        return f"她到了一个新地方：place_label = {new_v}（之前 {old or '未知'}）。"
    if cap_key == "wifi":
        return f"她连上了 {new_v}（之前 {old or '未知'}）。"
    if cap_key == "app":
        return f"她切到了 {new_v} 类应用（之前 {old or '未知'}）。"
    if cap_key == "motion":
        return f"她的运动状态变成了 {new_v}（之前 {old or '未知'}）。"
    if cap_key == "device" and field == "last_unlock_ago_sec":
        return "她长时间锁屏后刚刚解锁——拿起手机回来了。"
    if cap_key == "region":
        return f"她到了 {new_v}——一个明确的'今天联系一下'时刻。"
    return f"{cap_key} 发生了变化：{field} = {new_v}。"


def _fire_wake(user_id: str, cap_key: str, hint: str, now: float) -> None:
    """Enqueue a proactive job so the resident agent wakes. Lazy-imports app to
    avoid an import cycle (app registers this module at the bottom of startup)."""
    trigger = _legacy_wake_trigger(cap_key)
    if not trigger:
        log.warning("drop unmapped legacy perception enqueue: cap=%s", cap_key)
        return
    try:
        from core import store as core_store  # lazy
        from core import util as core_util  # lazy
        from proactive import service as proactive_service  # lazy
        s = core_store.get_store(user_id)
        if not s.proactive_activation_ready():
            return
        job = {
            "job_id": core_util._new_public_id("pj"),
            "ts": now,
            "created_at": datetime.fromtimestamp(now).isoformat(),
            "source": proactive_service.PROACTIVE_JOB_SOURCE,
            "status": "pending",
            "intent_label": trigger[:120],
            # trigger/wake_kind keep the V2 job schema consistent with the
            # tick path so consumers and the wake dashboard see one shape.
            "trigger": trigger[:120],
            "wake_kind": "presence",
            "context_hint": hint[:2000],
            "connections": [],
            "connection": {},
            "frame_ids": [],
            "device_event_ids": [],
            "current_app": "",
        }
        s.append_proactive_job(job)
    except Exception as e:
        log.error("fire_wake(%s,%s) failed: %s", user_id, cap_key, e)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

# Stable identity-ish context fields don't go stale like volatile sensor
# readings: a timezone/locale reported hours ago is still the user's timezone/
# locale. Exempt them from the freshness null-out so a proactive wake that fires
# long after the last foreground report still resolves the user's real timezone
# (the resident consumer's current_time anchor) instead of falling back to UTC.
# The volatile `local_time` in the same `time` signal keeps expiring normally.
_STABLE_CONTEXT_FIELDS = frozenset({"timezone", "locale"})


def stable_context_timezone(user_id: str) -> str | None:
    """Device timezone from the perception snapshot state. Used ONLY as the
    transitional fallback in /v1/users/whoami for users whose first-class
    record timezone is not yet populated. Returns None when unset. `timezone`
    is a stable-context field (never TTL-expired), so a raw cell read is safe."""
    cell = store.get_state(user_id).get("timezone")
    if isinstance(cell, dict):
        v = str(cell.get("v") or "").strip()
        return v or None
    return None


def _catalog_snapshot_fields(user_id: str, now: float | None = None, *, include_query_tools: bool = False) -> dict:
    now = now or _now()
    state = store.get_state(user_id)
    snap: dict = {}
    for sig in catalog.SIGNALS.values():
        cap = catalog.CAPABILITIES.get(sig.capability)
        if not cap or not (cap.context_field or (include_query_tools and cap.query_tool)):
            continue
        for f in sig.outputs:
            if f == "user_state":
                continue
            cell = state.get(f)
            if not isinstance(cell, dict):
                snap[f] = None
                continue
            if f not in _STABLE_CONTEXT_FIELDS and (now - float(cell.get("ts") or 0)) > sig.ttl_sec:
                snap[f] = None  # stale -> agent treats as "don't infer"
            else:
                snap[f] = cell.get("v")  # null cell -> None (= no permission now)
    return snap


def _merged_app_events(
    user_id: str,
    *,
    now: float,
    limit: int,
    since_epoch: float = 0.0,
) -> list[dict]:
    """Project the two bounded app streams into one newest-first timeline."""
    rows = [
        (row, "open")
        for row in (store.read_app_opens(
            user_id, limit=limit, since_epoch=since_epoch
        ) or [])
    ]
    rows.extend(
        (row, "close")
        for row in (store.read_app_closes(
            user_id, limit=limit, since_epoch=since_epoch
        ) or [])
    )
    events: list[dict] = []
    for row, event in rows:
        if not isinstance(row, dict):
            continue
        app = str(row.get("app") or "").strip()
        if not app:
            continue
        try:
            ts = float(row.get("ts") or 0.0)
        except (TypeError, ValueError):
            continue
        category = row.get("category")
        events.append({
            "app": app,
            "category": (str(category).strip() or None) if category else None,
            "ts": ts,
            "event": event,
            "minutes_ago": round(max(0.0, now - ts) / 60.0, 1),
        })
    # Python's stable sort keeps an open immediately before a close when both
    # streams report the exact same timestamp. The event names make that tie
    # explicit to consumers; neither stream silently wins or overwrites it.
    events.sort(key=lambda item: item["ts"], reverse=True)
    return events[:limit] if limit > 0 else events


def _snapshot_recent_app_events(user_id: str, now: float) -> list[dict]:
    """App open/close events fresh under the app signal's own TTL contract."""
    ttl_sec = float(catalog.SIGNALS["app"].ttl_sec)
    cutoff = now - ttl_sec
    # db.log_read uses ts > since_epoch, while snapshot freshness includes the
    # exact TTL boundary. Step one float below the cutoff, then enforce >= here.
    since_epoch = math.nextafter(cutoff, -math.inf) if cutoff > 0 else 0.0
    events = _merged_app_events(
        user_id, now=now, limit=0, since_epoch=since_epoch
    )
    return [event for event in events if event["ts"] >= cutoff]


def snapshot(user_id: str, now: float | None = None) -> dict:
    now_epoch = _now() if now is None else float(now)
    snap = _catalog_snapshot_fields(user_id, now_epoch, include_query_tools=False)
    # user_state is always present (manual default if nothing set).
    snap["user_state"] = effective_user_state(user_id)
    # Keep the legacy open-only field unchanged, and expose the merged fresh
    # trajectory separately so existing snapshot consumers do not change shape.
    snap["recent_apps"] = store.read_app_opens(user_id, limit=catalog.RECENT_APPS_LIMIT)
    snap["recent_app_events"] = _snapshot_recent_app_events(user_id, now_epoch)
    return snap


def pull_snapshot(user_id: str, now: float | None = None) -> dict:
    """Authorized fresh state for explicit pull tools.

    This includes query_tool capabilities such as weather/health without adding
    them to the cheap wake-attached `perception.now` snapshot.
    """
    now_epoch = _now() if now is None else float(now)
    snap = _catalog_snapshot_fields(user_id, now_epoch, include_query_tools=True)
    snap["recent_app_events"] = _snapshot_recent_app_events(user_id, now_epoch)
    return snap


def admin_perception_freshness(user_id: str, now: float | None = None) -> dict:
    """Per-field last-report freshness for admin/support triage.

    Timestamps ONLY — never the (often encrypted) values. Answers "is the device
    still feeding location / steps / … or did the client stop reporting?" without
    the user's key. Every catalog signal field is listed; an unreported field
    shows ``reported=False`` so "never sent" is distinct from "sent but stale".
    ``fresh`` mirrors the snapshot's own TTL rule (``_catalog_snapshot_fields``),
    so a field reading ``fresh=False`` is exactly one the agent currently sees as
    null. Two prod blind spots (usr_7f30 app_open, usr_5d3d location/steps) —
    this closes them from the admin side without any user key.
    """
    now = now or _now()
    state = store.get_state(user_id)
    fields: list[dict] = []
    seen: set[str] = set()
    for sig in catalog.SIGNALS.values():
        cap = catalog.CAPABILITIES.get(sig.capability)
        for f in sig.outputs:
            if f == "user_state" or f in seen:
                continue
            seen.add(f)
            cell = state.get(f)
            # A single dirty cell.ts (non-numeric historical junk) must not blow
            # up the whole freshness read — degrade that one field to "unreported".
            try:
                ts = float(cell.get("ts") or 0) if isinstance(cell, dict) else 0.0
            except (TypeError, ValueError):
                ts = 0.0
            reported = ts > 0
            stable = f in _STABLE_CONTEXT_FIELDS
            age = (now - ts) if reported else None
            fresh = bool(reported and (stable or age <= sig.ttl_sec))
            fields.append({
                "field": f,
                "capability": sig.capability,
                "capability_label": cap.label if cap else "",
                "reported": reported,
                "last_report_ts": ts if reported else None,
                "age_sec": int(age) if age is not None else None,
                "ttl_sec": None if stable else int(sig.ttl_sec),
                "fresh": fresh,
            })
    fields.sort(key=lambda r: (r["capability"], r["field"]))
    def _last_ts(rows: list[dict]) -> float:
        try:
            return float(rows[0].get("ts") or 0) if (rows and isinstance(rows[0], dict)) else 0.0
        except (TypeError, ValueError):
            return 0.0

    last_app_open_ts = _last_ts(store.read_app_opens(user_id, limit=1))
    last_app_close_ts = _last_ts(store.read_app_closes(user_id, limit=1))
    return {
        "fields": fields,
        "recent_app_open_ts": last_app_open_ts or None,
        "recent_app_open_age_sec": int(now - last_app_open_ts) if last_app_open_ts else None,
        "recent_app_close_ts": last_app_close_ts or None,
        "recent_app_close_age_sec": int(now - last_app_close_ts) if last_app_close_ts else None,
    }


# ---------------------------------------------------------------------------
# user_state
# ---------------------------------------------------------------------------

def _effective_from_doc(doc: dict) -> str:
    return doc.get("manual") or "default"


def effective_user_state(user_id: str) -> str:
    return _effective_from_doc(store.get_user_state_doc(user_id))


def set_manual_user_state(user_id: str, value: str, ts: float | None = None) -> str:
    """Set the manual user_state, ts-guarded ATOMICALLY in the store (a write
    older than the stored manual_ts is dropped under a row lock, so a late/
    concurrent report can't clobber a newer manual value). ts=None means now."""
    ts = _now() if ts is None else float(ts)
    return _effective_from_doc(store.set_manual_user_state_guarded(user_id, value, ts))


# ---------------------------------------------------------------------------
# Permissions & config
# ---------------------------------------------------------------------------

def set_config(user_id: str, patch: dict) -> dict:
    return store.merge_config(user_id, patch or {})


# ---------------------------------------------------------------------------
# Photos (single-step encrypted ingest)
# ---------------------------------------------------------------------------

def _truthy(v) -> bool:
    """Coerce a bool that may have arrived as a string ("默认字符串"). "false"/
    "0"/"" -> False."""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes")
    return bool(v)


def _photo_sensitivity(meta: dict) -> bool:
    """Return whether metadata says the photo may need careful expression.

    This is not a gate. V2 intentionally lets encrypted photo content reach the
    companion inside the trusted boundary; the prompt/policy layer decides how
    to talk about sensitive scenes.
    """
    scene = str(meta.get("scene_hint") or "").lower()
    return scene in catalog.SENSITIVE_PHOTO_SCENES or _truthy(meta.get("is_screenshot"))


def photo_evaluate(user_id: str, metadata: dict,
                   content_envelope: dict | None = None,
                   exif_gps: dict | None = None,
                   meta_envelope: dict | None = None) -> tuple[dict, int]:
    """Single-step photo ingest: evaluate metadata AND (if usable) store the
    encrypted image in one call.

    V2 does not hard-block sensitive scene hints. The ciphertext goes into the
    screen-frame envelope channel (reuses the enclave's existing frame-decrypt
    path); the backend never sees plaintext. frame_id == photo_id ==
    content_envelope.id. Optional meta_envelope is stored encrypted and returned
    only on the single-photo content read path.
    """
    now = _now()
    metadata = metadata or {}
    config = store.get_config(user_id)
    place_label = None
    if exif_gps:
        place_label = resolve.resolve_geofence(exif_gps, config).get("place_label")
    sensitive = _photo_sensitivity(metadata)
    photo_id = str((content_envelope or {}).get("id") or random_item_id())
    meta_out = {k: metadata.get(k) for k in catalog.PHOTO_METADATA_FIELDS}
    meta_out["place_label"] = place_label

    if not content_envelope:
        return {"error": "content_envelope_required"}, 400

    # Store ciphertext in the frame channel + metadata as a confirmed item.
    store.put_photo_envelope(user_id, photo_id, now, content_envelope)
    doc = {"photo_id": photo_id, "metadata": meta_out, "status": "confirmed",
           "usable": True, "sensitive": sensitive, "frame_id": photo_id}
    if meta_envelope:
        doc["meta_envelope"] = dict(meta_envelope)
    store.item_upsert(user_id, "photo", photo_id, now, doc, expires_at=None)

    if perception_ingress_runtime_v2_enabled(user_id):
        # Reapply the legacy 30s burst/cluster dedup: rapid photo captures must
        # not each spawn a separate wake job. The differ keys on a unique
        # photo_id so it never debounces these on its own.
        if (now - _last_v2_wake_ts(user_id, "photo_added")) < catalog.PHOTO_CLUSTER_SEC:
            store.append_event(user_id, {
                "cap": "runtime_v2", "type": "debounced", "source": "perception_event",
                "trigger": "photo_added", "origin_refs": [f"photo:{photo_id}"], "ts": now,
            }, now)
        else:
            observe_signal_v2(
                user_id,
                "photo_added",
                {"photo_id": photo_id, "sensitive": sensitive},
                ts=now,
                origin_refs=(f"photo:{photo_id}",),
                submit_wake=_submit_wake_event_v2_compat,
            )
    else:
        _maybe_wake(user_id, "photos", catalog.PHOTO_CLUSTER_SEC, "photo_id", None, photo_id, now)
    return {"photo_id": photo_id, "metadata": meta_out, "usable": True,
            "sensitive": sensitive, "status": "stored"}, 200


def photos_recent(user_id: str, limit: int = 20) -> tuple[dict, int]:
    now = _now()
    items = [i for i in store.item_list(user_id, "photo", limit=limit, now=now)
             if i.get("status") == "confirmed"]
    out = [{"photo_id": i.get("photo_id"), "metadata": i.get("metadata")} for i in items]
    return {"photos": out}, 200


def photo_content(user_id: str, photo_id: str) -> tuple[dict, int]:
    """Permission + status gate for one confirmed photo. Returns metadata and the
    frame_id; the caller decrypts pixels via the enclave's existing
    /v1/screen/frames/<frame_id>/decrypt path. The backend never holds plaintext
    pixels — only the enclave decrypts."""
    now = _now()
    doc = store.item_get(user_id, "photo", photo_id, now=now)
    if not doc or doc.get("status") != "confirmed":
        return {"error": "not_found"}, 404
    out = {
        "photo_id": photo_id,
        "frame_id": doc.get("frame_id") or photo_id,
        "metadata": doc.get("metadata"),
        "decrypt_path": f"/v1/screen/frames/{doc.get('frame_id') or photo_id}/decrypt",
    }
    if doc.get("meta_envelope"):
        out["meta_envelope"] = doc.get("meta_envelope")
    return out, 200


# ---------------------------------------------------------------------------
# Legacy Tier 2 health collections — generic
# ---------------------------------------------------------------------------

def items_ingest(user_id: str, kind: str, items: list[dict]) -> tuple[dict, int]:
    cap = catalog.KIND_CAPABILITY.get(kind)
    if not cap:
        return {"error": "unknown_kind"}, 400
    if not isinstance(items, list) or not all(isinstance(it, dict) for it in items):
        return {"error": "invalid_items"}, 400
    now = _now()
    wrote = 0
    for it in items:
        iid = str(it.get("item_id") or random_item_id())
        ts = float(it.get("ts") or now)
        store.item_upsert(user_id, kind, iid, ts, it.get("doc") or {}, it.get("expires_at"))
        wrote += 1
    return {"written": wrote}, 200


def items_recent(user_id: str, kind: str, limit: int = 20) -> tuple[dict, int]:
    cap = catalog.KIND_CAPABILITY.get(kind)
    if not cap:
        return {"error": "unknown_kind"}, 400
    now = _now()
    return {"items": store.item_list(user_id, kind, limit=limit, now=now)}, 200


# ---------------------------------------------------------------------------
# App usage (iOS Shortcut GET endpoints) — "what app at what time"
# ---------------------------------------------------------------------------

def _current_app_cells(user_id: str) -> tuple[str | None, str | None]:
    """(current app_name, current app_category) as plain values from the snapshot."""
    state = store.get_state(user_id)

    def _value(field: str):
        cell = state.get(field)
        return cell.get("v") if isinstance(cell, dict) else None

    return _value("app_name"), _value("app_category")


def _record_app_event(user_id: str, app: str, category: str | None,
                      client_ts, *, closing: bool) -> tuple[dict, int]:
    """Record one app open/close event fired by an iOS Shortcut automation.

    Both directions append to their own stream and refresh the snapshot's app
    cells. The ONE asymmetry: an open unconditionally becomes the current app,
    while a close only touches the snapshot when the app being closed IS the
    current one. A background app killed after the user already switched away
    carries a NEWER ts than the next app's open, so the per-field ts guard would
    happily let it clobber what is actually on screen.

    On close all three cells are rewritten with the close ts (app_name/
    app_category keep their values): leaving app_name at the open ts would let
    it expire under the 900s TTL while app_state still read "closed" — i.e. the
    agent would see "closed a null".

    Per-field ts guard caveat: `_current_app_cells` above is a plain read, not
    part of the same transaction as the `merge_state_guarded` call below it.
    If the user switches apps fast enough — close-A and open-B fired by iOS
    within the same narrow window — close-A's read of the snapshot can land
    before open-B's write commits, and if close-A's ts is newer than open-B's
    ts, the per-field guard lets close-A overwrite B's just-written state with
    "closed A". The blast radius is bounded: at most one 900s TTL window shows
    a wrong snapshot, no history rows are lost, and the very next app event
    self-heals it. A real fix means moving the current-app comparison inside
    `merge_state_guarded` itself (a store API change) so the read-compare-write
    is atomic; not done here.
    """
    app = (app or "").strip()
    if not app:
        return {"error": "app_required"}, 400
    now = _coerce_ts(client_ts)
    category = (category or "").strip() or None

    if closing:
        # Append the event row BEFORE touching the snapshot (open does the
        # reverse). Deliberate: a close is the more important signal to not
        # lose, so if the snapshot write below fails silently, the close is
        # still on record in the app-close stream.
        store.append_app_close(user_id, {"app": app, "category": category, "ts": now}, now)
        current_app, current_category = _current_app_cells(user_id)
        if current_app and str(current_app).strip().casefold() == app.casefold():
            # Keep the spelling the OPEN recorded, not the one this close
            # happened to use: the two Shortcut automations are typed by hand,
            # so "  instagram " closing "Instagram" must not rewrite the name
            # the agent will read back. The match above is already
            # case-insensitive; only the stored label is at stake here.
            store.merge_state_guarded(user_id, {
                "app_name": _cell(str(current_app).strip(), now, None),
                "app_category": _cell(category if category is not None else current_category,
                                      now, None),
                "app_state": _cell("closed", now, None),
            })
    else:
        store.merge_state_guarded(user_id, {
            "app_name": _cell(app, now, None),
            "app_category": _cell(category, now, None),
            "app_state": _cell("foreground", now, None),
        })
        store.append_app_open(user_id, {"app": app, "category": category, "ts": now}, now)
    return {"status": "ok", "app": app, "category": category, "ts": now}, 200


def app_open(user_id: str, app: str, category: str | None = None,
             client_ts=None) -> tuple[dict, int]:
    """Record one app-open event (fired by an iOS Shortcut when the user opens an
    app). Updates current app in the snapshot AND appends to the usage time
    series for later stats."""
    return _record_app_event(user_id, app, category, client_ts, closing=False)


def app_close(user_id: str, app: str, category: str | None = None,
              client_ts=None) -> tuple[dict, int]:
    """Record one app-close event (the Shortcut automation's "is closed"
    trigger). Mirror of ``app_open`` on the wire; see ``_record_app_event`` for
    the one place the two intentionally differ."""
    return _record_app_event(user_id, app, category, client_ts, closing=True)


def app_history_denied_reason(user_id: str) -> str:
    """Return "" when app-event history is readable, else the disable reason.

    Fail-closed on a settings read error: if we cannot prove the user still
    allows `app`, we do not hand over their usage trajectory.
    """
    try:
        settings = _app_proactive_settings(user_id) or {}
    except Exception as e:
        log.error("app_history_denied_reason(%s) settings read failed: %s", user_id, e)
        return "unavailable"
    return permissions.permission_states_reason(settings, "recent_apps")


def recent_apps(user_id: str, *, limit: int | None = None, hours: float | None = None,
                now: float | None = None) -> dict:
    """Recent app open/close history for an explicit agent pull.

    Merges the existing bounded open and close streams. Newest first, each entry
    is projected to exactly {app, category, ts, event, minutes_ago} — the raw log
    row is never passed through, so nothing else the ingest wrote can leak into
    a model prompt. No data -> an explicit empty list, never a guess.

    Authorization is enforced HERE, not at the callers: the dedicated route,
    the signal path and the V2 executor adapter all funnel through this
    function, so turning the `app` capability off stops every one of them.
    A denied read returns disabled=True (never silently an empty list, which
    the agent would read as "she hasn't used any apps").
    """
    reason = app_history_denied_reason(user_id)
    if reason:
        return {"ok": True, "disabled": True, "reason": reason,
                "apps": [], "count": 0, "window_hours": hours}

    now = _now() if now is None else float(now)
    limit = catalog.RECENT_APPS_TOOL_LIMIT if limit is None else limit
    limit = max(1, min(int(limit), catalog.RECENT_APPS_TOOL_MAX))
    since = (now - hours * 3600.0) if hours else 0.0

    entries = _merged_app_events(
        user_id, now=now, limit=limit, since_epoch=since
    )
    return {"ok": True, "apps": entries, "count": len(entries), "window_hours": hours}
