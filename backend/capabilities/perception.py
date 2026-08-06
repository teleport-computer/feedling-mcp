"""Perception capabilities — facade over backend/agent/perception_core.py."""
from __future__ import annotations

from agent import perception_core

from capabilities import errors
from capabilities.types import CapabilityResult, ok, err


def _wrap(fn, *, default_msg: str, **kwargs) -> CapabilityResult:
    try:
        body = fn(**kwargs)
    except perception_core.AgentRouteError as e:
        return err(errors.code_for_status(e.status_code),
                   errors.message_for_body(e.body, default_msg),
                   retryable=errors.retryable_for_status(e.status_code))
    return ok(data=errors.cap_data(body if isinstance(body, dict) else {"result": body}))


def _csv(signals):
    if isinstance(signals, (list, tuple)):
        return ",".join(str(s) for s in signals)
    return signals


def snapshot(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    return _wrap(perception_core.agent_perception_payload,
                 default_msg="perception unavailable",
                 store=store, signals_raw=_csv(params.get("signals")))


def recent_apps(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    """Return the permission-gated merged app open/close trajectory."""
    params = params or {}
    return _wrap(
        perception_core.recent_apps_payload,
        default_msg="recent app activity unavailable",
        store=store,
        limit_raw=params.get("limit"),
        hours_raw=params.get("hours"),
    )


def trend(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    return _wrap(perception_core.perception_trend_payload,
                 default_msg="perception trend unavailable",
                 store=store, signal_raw=params.get("signal"),
                 field_raw=params.get("field"), days_raw=params.get("days"))


def history(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    return _wrap(perception_core.perception_history_payload,
                 default_msg="perception history unavailable",
                 store=store, signal_raw=params.get("signal"),
                 days_raw=params.get("days"))


def glance(store, *, api_key=None, runtime_token=None, params=None) -> CapabilityResult:
    params = params or {}
    result = _wrap(perception_core.perception_glance_payload,
                   default_msg="perception glance unavailable",
                   store=store, days_raw=params.get("days"))
    if result.ok:
        result.data.pop("ok", None)
    return result
