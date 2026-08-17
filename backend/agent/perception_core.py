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
from perception import permissions as perception_permissions
from perception import store as perception_store
from perception.agent_fields import (
    AGENT_PERCEPTION_SIGNALS,
    AGENT_SIGNAL_FIELDS as _SIGNAL_FIELDS,
    FAST_AGENT_PERCEPTION_SIGNALS,
    project_signal,
)
from perception.glance import V1_PRESENCE_HINT_FIELDS


class AgentRouteError(Exception):
    """Typed 4xx failure carrying the exact JSON body + status the route returns.

    Both the Flask route and the ASGI route catch this and render
    ``(body, status_code)`` — keeping the error shape byte-identical across
    frameworks."""

    def __init__(self, status_code: int, body: dict):
        super().__init__(str(body.get("error", "error")))
        self.status_code = status_code
        self.body = body


# Authorization judgment now lives in perception.permissions so the dedicated
# recent_apps route and the V2 executor enforce the SAME rule (they used to
# bypass it entirely). Thin aliases keep this module's call sites unchanged.
_SIGNAL_PERMISSION_KEYS = perception_permissions.SIGNAL_PERMISSION_KEYS
_permission_states_reason = perception_permissions.permission_states_reason


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


def _history_field_permission_signal(
    catalog_signal: str,
    field: str,
) -> str | None:
    """Map one stored history field back to its authoritative signal doc.

    History uses canonical catalog signals while authorization is represented
    by the disabled documents returned from ``agent_perception_payload``.  The
    shared ``health_vitals`` row is the only ambiguous catalog record:
    ``step_count`` follows the dedicated ``steps`` permission, while every
    other canonical vital follows ``vitals``. Unknown fields fail closed.
    """
    if catalog_signal == "health_vitals" and field == "step_count":
        return "steps"
    candidates = [
        agent_signal
        for agent_signal, mapped_catalog in _HISTORY_SIGNAL_TO_CATALOG.items()
        if mapped_catalog == catalog_signal
        and field in _SIGNAL_FIELDS.get(agent_signal, ())
    ]
    return candidates[0] if len(candidates) == 1 else None


def _permission_mask_history_rows(
    rows_by_signal: Mapping[str, list[Mapping[str, Any]]],
    signal_docs: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Drop history fields whose canonical live signal doc is disabled.

    Masking rows before ``notable_changes`` also prevents a denied high-rank
    delta from consuming the top-N cap ahead of an authorized change.
    """
    masked: dict[str, list[dict[str, Any]]] = {}
    for catalog_signal, rows in rows_by_signal.items():
        masked_rows: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            doc = row.get("doc")
            safe_doc: dict[str, Any] = {}
            if isinstance(doc, Mapping):
                for field, value in doc.items():
                    if not isinstance(field, str):
                        continue
                    permission_signal = _history_field_permission_signal(
                        str(catalog_signal), field
                    )
                    permission_doc = signal_docs.get(permission_signal or "")
                    if (
                        permission_signal is not None
                        and isinstance(permission_doc, Mapping)
                        and permission_doc.get("disabled") is not True
                    ):
                        safe_doc[field] = value
            masked_rows.append({**dict(row), "doc": safe_doc})
        masked[str(catalog_signal)] = masked_rows
    return masked


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
        out[signal] = _disabled(reason) if reason else _signal_doc(signal, snapshot, pull_snapshot)
    return {"ok": True, "signals": out}


def recent_apps_payload(store, *, limit_raw: str | None, hours_raw: str | None,
                        now: float | None = None) -> dict[str, Any]:
    """Recent app open/close history (the `perception.recent_apps` tool).

    ``signals=app`` only carries the last observed app event inside its TTL and
    cannot answer a trajectory question. This pull returns both Shortcut open
    and close events with timestamps, including close-only windows.
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


def perception_glance_payload(store, *, days_raw: str | None) -> dict[str, Any]:
    """V1-equivalent factual board for Runtime V2 proactive grounding.

    This capability is runtime-internal (it has no model-facing tool schema).
    V1 preloads the same bounded presence fields and balanced digest board before
    a proactive turn, then leaves the ordinary perception tools available for
    deeper reads.  Keep that hybrid shape here instead of reducing the already
    fetched facts to availability booleans.
    """
    snapshot = perception_service.snapshot(store.user_id)
    digest = perception_digest_payload(store, days_raw=days_raw)
    presence_hints = {
        field: snapshot.get(field)
        for field in V1_PRESENCE_HINT_FIELDS
        if snapshot.get(field) is not None
    }
    return {
        "ok": True,
        "presence_hints": presence_hints,
        "cross_domain_board": digest.get("domains") or {},
    }


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
