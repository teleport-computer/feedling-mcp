"""Framework-neutral payload builders for the agent perception endpoints.

Lifted out of ``agent.routes`` so the native ASGI routes (``agent.routes_asgi``)
reuse the exact same bodies the Flask oracle returns — no Flask/FastAPI request
object here. The caller resolves the store, extracts the raw query params, and
passes them in; each builder does its own parse + validation and raises
``AgentRouteError`` for the 400 responses so both frameworks map the identical
error shape.

These builders call sync perception store/service functions that touch the DB,
so ASGI callers must run them on the threadpool (``asgi.threadpool.run_db``),
never on the event loop.
"""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any

from perception import history as perception_history
from perception import service as perception_service
from perception import store as perception_store
from perception.agent_fields import (
    AGENT_PERCEPTION_SIGNALS,
    AGENT_SIGNAL_FIELDS as _SIGNAL_FIELDS,
    FAST_AGENT_PERCEPTION_SIGNALS,
    project_signal,
)


class AgentRouteError(Exception):
    """Typed 4xx failure carrying the exact JSON body + status the route returns.

    Both the Flask route and the ASGI route catch this and render
    ``(body, status_code)`` — keeping the error shape byte-identical across
    frameworks."""

    def __init__(self, status_code: int, body: dict):
        super().__init__(str(body.get("error", "error")))
        self.status_code = status_code
        self.body = body


_SIGNAL_PERMISSION_KEYS: dict[str, tuple[str, ...]] = {
    "now": ("now", "time", "device", "battery", "broadcast"),
    "location": ("location", "location_signal"),
    "weather": ("weather",),
    "motion": ("motion", "motion_state"),
    "calendar": ("calendar", "calendar_next_event"),
    "focus": ("focus",),
    "audio_route": ("audio_route",),
    "steps": ("steps", "health", "health_vitals"),
    "sleep": ("sleep", "health", "health_sleep"),
    "workout": ("workout", "health", "health_workout"),
    "vitals": ("vitals", "health", "health_vitals"),
    "activity": ("activity", "health", "health_activity"),
    "body": ("body", "health", "health_body"),
    "metabolic": ("metabolic", "health", "health_metabolic"),
    "cycle": ("cycle", "health", "health_cycle"),
    "mood": ("mood", "health", "health_mood"),
    "reminders": ("reminders",),
    # history rides the same `app` permission as the current-app field
    "recent_apps": ("app",),
}

_OFF_VALUES = {"0", "false", "off", "disabled", "switch_off", "switch-off", "no"}
_DENIED_VALUES = {
    "denied",
    "not_permitted",
    "not-permitted",
    "not_allowed",
    "not-allowed",
    "not_authorized",
    "not-authorized",
    "unauthorized",
    "restricted",
    "permission_denied",
}
_ALLOW_VALUES = {"1", "true", "on", "enabled", "allowed", "authorized", "granted", "yes"}


def _parse_signals(raw: str | None) -> list[str]:
    if not raw:
        return list(FAST_AGENT_PERCEPTION_SIGNALS)
    out: list[str] = []
    for part in str(raw or "").split(","):
        signal = part.strip().lower()
        if signal and signal not in out:
            out.append(signal)
    return out or list(FAST_AGENT_PERCEPTION_SIGNALS)


def _disabled(reason: str) -> dict[str, Any]:
    return {"disabled": True, "reason": reason}


def _boolish_doc_reason(value: Any) -> str:
    if isinstance(value, bool):
        return "" if value else "switch_off"
    if isinstance(value, (int, float)):
        return "" if bool(value) else "switch_off"
    normalized = str(value or "").strip().lower()
    if not normalized or normalized in _ALLOW_VALUES:
        return ""
    if normalized in _OFF_VALUES:
        return "switch_off"
    if normalized in _DENIED_VALUES:
        return "not_permitted"
    return ""


def _permission_state_reason(value: Any) -> str:
    if isinstance(value, Mapping):
        explicit_reason = str(value.get("reason") or "").strip().lower()
        for key in ("enabled", "allowed", "authorized", "granted", "permitted"):
            if key in value:
                reason = _boolish_doc_reason(value.get(key))
                if reason:
                    return "not_permitted" if explicit_reason in _DENIED_VALUES else reason
                return ""
        for key in ("state", "status", "permission", "value"):
            if key in value:
                reason = _boolish_doc_reason(value.get(key))
                if reason:
                    return reason
        return _boolish_doc_reason(explicit_reason)
    return _boolish_doc_reason(value)


def _permission_states_reason(settings: Mapping[str, Any], signal: str) -> str:
    states = settings.get("permission_states") if isinstance(settings, Mapping) else {}
    if not isinstance(states, Mapping):
        return ""
    for key in _SIGNAL_PERMISSION_KEYS.get(signal, (signal,)):
        if key not in states:
            continue
        reason = _permission_state_reason(states.get(key))
        if reason:
            return reason
    return ""


def _null_state_message_reason(state: Mapping[str, Any], signal: str) -> str:
    fields = _SIGNAL_FIELDS.get(signal, ())
    messages: list[str] = []
    for field in fields:
        cell = state.get(field) if isinstance(state, Mapping) else None
        if isinstance(cell, Mapping) and cell.get("v") is None and cell.get("msg"):
            messages.append(str(cell.get("msg") or ""))
    if not messages:
        return ""
    joined = " ".join(messages).lower()
    if any(marker in joined for marker in ("关闭", "已关", "switch off", "switched off", "disabled")):
        return "switch_off"
    if any(marker in joined for marker in ("未授权", "无授权", "not authorized", "not permitted", "permission")):
        return "not_permitted"
    if signal == "weather" and "weatherkit" in joined and "不可用" in joined:
        return "not_permitted"
    if signal in {"steps", "sleep", "workout", "vitals"} and "healthkit" in joined and "不可用" in joined:
        return "not_permitted"
    return ""


def _signal_doc(signal: str, snapshot: Mapping[str, Any], pull_snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return project_signal(signal, snapshot, pull_snapshot)


# Agent signal name -> canonical catalog signal key for the quantitative history
# (perception_daily) rollups. Mirrors AGENT_PERCEPTION_SIGNALS where historized.
_HISTORY_SIGNAL_TO_CATALOG: dict[str, str] = {
    "vitals": "health_vitals",
    "steps": "health_vitals",          # step_count lives in the vitals signal
    "sleep": "health_sleep",
    "workout": "health_workout",
    "activity": "health_activity",
    "body": "health_body",
    "metabolic": "health_metabolic",
    "cycle": "health_cycle",
    "mood": "health_mood",
    "weather": "weather",
    "motion": "motion_state",
    "location": "location_signal",
    "calendar": "calendar_next_event",
    "focus": "focus",
    "audio_route": "audio_route",
    "reminders": "reminders",
    "now_playing": "playback",
    "music": "playback",
}


def _history_signal(raw: str | None) -> str | None:
    sig = str(raw or "").strip().lower()
    return _HISTORY_SIGNAL_TO_CATALOG.get(sig)


def _parse_days(raw: str | None, default: str) -> int:
    """Clamp the ``days`` query param to [1, 365]; raise 400 on non-numeric.

    Mirrors the Flask ``int(request.args.get("days", <default>))`` behavior:
    an absent param uses ``default``; a present-but-unparseable value 400s."""
    try:
        return max(1, min(int(raw if raw is not None else default), 365))
    except (TypeError, ValueError):
        raise AgentRouteError(400, {"ok": False, "error": "invalid_days"})


def _digest_notable_max() -> int:
    try:
        return max(1, min(int(os.environ.get("FEEDLING_DIGEST_NOTABLE_MAX", "8")), 50))
    except (TypeError, ValueError):
        return 8


def agent_perception_payload(store, *, signals_raw: str | None) -> dict[str, Any]:
    signals = _parse_signals(signals_raw)
    unknown = [signal for signal in signals if signal not in AGENT_PERCEPTION_SIGNALS]
    if unknown:
        raise AgentRouteError(400, {
            "ok": False,
            "error": "unknown_signals",
            "unknown": unknown,
            "available": list(AGENT_PERCEPTION_SIGNALS),
        })

    settings = store.load_proactive_settings() if hasattr(store, "load_proactive_settings") else {}
    state = perception_store.get_state(store.user_id)
    snapshot = perception_service.snapshot(store.user_id)
    pull_snapshot = perception_service.pull_snapshot(store.user_id)

    out: dict[str, Any] = {}
    for signal in signals:
        reason = _permission_states_reason(settings, signal) or _null_state_message_reason(state, signal)
        if reason:
            out[signal] = _disabled(reason)
        elif signal == "recent_apps":
            # history, not a state field -- one shape with the dedicated route
            out[signal] = perception_service.recent_apps(store.user_id)
        else:
            out[signal] = _signal_doc(signal, snapshot, pull_snapshot)
    return {"ok": True, "signals": out}


def recent_apps_payload(store, *, limit_raw: str | None, hours_raw: str | None,
                        now: float | None = None) -> dict[str, Any]:
    """Recent app-open history (the `perception.recent_apps` tool).

    The current-app fields in ``/v1/agent/perception?signals=app`` only answer
    "what is open right now"; this answers "what has she been using", which is
    what a chat turn minutes after the fact actually needs.
    """
    limit = None
    if limit_raw is not None and str(limit_raw).strip():
        try:
            limit = int(limit_raw)
        except (TypeError, ValueError):
            raise AgentRouteError(400, {"ok": False, "error": "invalid_limit"})

    hours = None
    if hours_raw is not None and str(hours_raw).strip():
        try:
            hours = float(hours_raw)
        except (TypeError, ValueError):
            raise AgentRouteError(400, {"ok": False, "error": "invalid_hours"})
        if hours <= 0:
            raise AgentRouteError(400, {"ok": False, "error": "invalid_hours"})

    return perception_service.recent_apps(store.user_id, limit=limit, hours=hours, now=now)


def perception_trend_payload(store, *, signal_raw: str | None, field_raw: str | None, days_raw: str | None) -> dict[str, Any]:
    """Rolling baseline + delta for one numeric field over the last N days."""
    sig = _history_signal(signal_raw)
    if sig is None:
        raise AgentRouteError(400, {"ok": False, "error": "unknown_or_unhistorized_signal",
                                    "available": sorted(_HISTORY_SIGNAL_TO_CATALOG)})
    field = (field_raw or "").strip() or None
    days = _parse_days(days_raw, "30")
    rows = perception_store.list_perception_daily(store.user_id, sig, days)
    return {"ok": True, "trend": perception_history.read_trend(rows, sig, field)}


def perception_history_payload(store, *, signal_raw: str | None, days_raw: str | None) -> dict[str, Any]:
    """Raw per-day rollup docs for a signal over the last N days."""
    sig = _history_signal(signal_raw)
    if sig is None:
        raise AgentRouteError(400, {"ok": False, "error": "unknown_or_unhistorized_signal",
                                    "available": sorted(_HISTORY_SIGNAL_TO_CATALOG)})
    days = _parse_days(days_raw, "14")
    rows = perception_store.list_perception_daily(store.user_id, sig, days)
    return {"ok": True, "signal": sig, "days": days, "daily": rows}


def perception_digest_payload(store, *, days_raw: str | None) -> dict[str, Any]:
    """Balanced cross-domain wake digest: one compact entry per life-context
    domain, plus legacy top-N numeric deltas (``changes``)."""
    days = _parse_days(days_raw, "30")
    uid = store.user_id
    # History rows for the numeric/health fold (comparable) plus the two
    # non-comparable shapes the board reads directly: playback tally + place dwell.
    history_signals = set(perception_history.comparable_signals()) | {"playback", "location_signal"}
    rows_by_signal = {
        signal: perception_store.list_perception_daily(uid, signal, days)
        for signal in history_signals
    }
    notable_max = _digest_notable_max()
    changes = perception_history.notable_changes(rows_by_signal, max_changes=notable_max)
    try:
        snapshot = perception_service.snapshot(uid)
        pull = perception_service.pull_snapshot(uid)
    except Exception:
        snapshot, pull = {}, {}
    try:
        photos = (perception_service.photos_recent(uid, limit=10)[0] or {}).get("photos") or []
    except Exception:
        photos = []
    domains = perception_history.cross_domain_recent(
        snapshot=snapshot,
        pull_snapshot=pull,
        rows_by_signal=rows_by_signal,
        photos=photos,
        max_health_notable=notable_max,
    )
    return {"ok": True, "days": days, "changes": changes, "domains": domains}
