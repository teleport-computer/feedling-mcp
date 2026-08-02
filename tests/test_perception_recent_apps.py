"""Recent-app perception: TTL window + the agent-callable history tool.

Two separate gaps are covered here (Lark t100530):

  1. the CURRENT app signal expired after 5 minutes, so any chat turn that
     happened more than 300s after the last Shortcut ping saw app_name=None;
  2. app events were stored, but the read side did not expose the merged
     open/close trajectory consistently to snapshot, V1, and V2 consumers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from agent import perception_core  # noqa: E402
from perception import agent_fields  # noqa: E402
from perception import catalog  # noqa: E402
from perception import service as perception_service  # noqa: E402
from perception import store as perception_store  # noqa: E402
from proactive import tool_catalog_v2  # noqa: E402


T0 = 1_700_000_000.0


class _Store:
    user_id = "u_apps"

    def load_proactive_settings(self):
        return {}


def _read_app_events(events):
    """Mimics db.log_read: chronological (seq) order, and when limit>0 it
    returns the NEWEST `limit` rows -- not the first `limit` rows."""
    def read(uid, limit=100, since_epoch=0.0):
        rows = [e for e in events if not since_epoch or float(e.get("ts") or 0) > since_epoch]
        return rows[-limit:] if limit and limit > 0 else rows
    return read


def _seed(monkeypatch, *, state=None, opens=None, closes=None):
    monkeypatch.setattr(perception_store, "get_state", lambda uid: dict(state or {}))
    monkeypatch.setattr(perception_store, "read_app_opens", _read_app_events(list(opens or [])))
    monkeypatch.setattr(perception_store, "read_app_closes", _read_app_events(list(closes or [])))


# ---------------------------------------------------------------------------
# 1. TTL window
# ---------------------------------------------------------------------------

def test_app_signal_ttl_is_fifteen_minutes():
    assert catalog.SIGNALS["app"].ttl_sec == 900.0


def test_current_app_survives_past_the_old_five_minute_window(monkeypatch):
    _seed(monkeypatch, state={
        "app_name": {"v": "wechat", "ts": T0},
        "app_category": {"v": "social", "ts": T0},
    })
    # 10 minutes later: used to be nulled out, must now still be readable.
    snap = perception_service.snapshot("u_apps", T0 + 600)
    assert snap["app_name"] == "wechat"
    assert snap["app_category"] == "social"


def test_current_app_still_expires_after_fifteen_minutes(monkeypatch):
    _seed(monkeypatch, state={"app_name": {"v": "wechat", "ts": T0}})
    assert perception_service.snapshot("u_apps", T0 + 899)["app_name"] == "wechat"
    assert perception_service.snapshot("u_apps", T0 + 901)["app_name"] is None


# ---------------------------------------------------------------------------
# 2. The history tool the chat agent can call
# ---------------------------------------------------------------------------

def test_recent_apps_payload_returns_history_with_time_and_category(monkeypatch):
    _seed(monkeypatch, opens=[
        {"app": "xiaohongshu", "category": "social", "ts": T0 + 60},
        {"app": "wechat", "category": "social", "ts": T0},
    ])
    body = perception_core.recent_apps_payload(
        _Store(), limit_raw=None, hours_raw=None, now=T0 + 660
    )
    assert body["ok"] is True
    apps = body["apps"]
    assert [a["app"] for a in apps] == ["xiaohongshu", "wechat"]
    assert apps[0]["category"] == "social"
    assert apps[0]["ts"] == T0 + 60
    assert [app["event"] for app in apps] == ["open", "open"]
    # a usable "how long ago" so the agent doesn't have to do epoch math
    assert apps[0]["minutes_ago"] == 10.0
    assert apps[1]["minutes_ago"] == 11.0
    assert body["count"] == 2


def test_recent_apps_payload_is_newest_first_even_if_store_is_not(monkeypatch):
    _seed(monkeypatch, opens=[
        {"app": "wechat", "ts": T0},
        {"app": "xiaohongshu", "ts": T0 + 300},
        {"app": "safari", "ts": T0 + 100},
    ])
    body = perception_core.recent_apps_payload(_Store(), limit_raw=None, hours_raw=None, now=T0 + 400)
    assert [a["app"] for a in body["apps"]] == ["xiaohongshu", "safari", "wechat"]


def test_recent_apps_payload_honors_limit_and_hours(monkeypatch):
    _seed(monkeypatch, opens=[
        {"app": "wechat", "ts": T0},
        {"app": "xiaohongshu", "ts": T0 + 100},
        {"app": "safari", "ts": T0 + 200},
    ])
    limited = perception_core.recent_apps_payload(_Store(), limit_raw="2", hours_raw=None, now=T0 + 300)
    assert [a["app"] for a in limited["apps"]] == ["safari", "xiaohongshu"]

    # hours window cuts off the oldest entry
    windowed = perception_core.recent_apps_payload(
        _Store(), limit_raw=None, hours_raw="0.05", now=T0 + 200  # 180s window
    )
    assert [a["app"] for a in windowed["apps"]] == ["safari", "xiaohongshu"]
    assert windowed["window_hours"] == 0.05


def test_recent_apps_merges_open_and_close_newest_first_with_stable_ties(monkeypatch):
    _seed(
        monkeypatch,
        opens=[
            {"app": "outside-open", "ts": T0 - 4000},
            {"app": "same-open", "category": "social", "ts": T0 + 100},
        ],
        closes=[
            {"app": "close-only", "category": "work", "ts": T0 + 200},
            {"app": "same-close", "category": "social", "ts": T0 + 100},
        ],
    )

    body = perception_core.recent_apps_payload(
        _Store(), limit_raw=None, hours_raw="1", now=T0 + 300
    )

    assert [(item["app"], item["event"]) for item in body["apps"]] == [
        ("close-only", "close"),
        ("same-open", "open"),
        ("same-close", "close"),
    ]


def test_recent_apps_limit_applies_after_merging_both_streams(monkeypatch):
    _seed(
        monkeypatch,
        opens=[{"app": "open-new", "ts": T0 + 300}],
        closes=[
            {"app": "close-old", "ts": T0 + 100},
            {"app": "close-new", "ts": T0 + 400},
        ],
    )

    body = perception_core.recent_apps_payload(
        _Store(), limit_raw="2", hours_raw=None, now=T0 + 500
    )

    assert [(item["app"], item["event"]) for item in body["apps"]] == [
        ("close-new", "close"),
        ("open-new", "open"),
    ]


def test_recent_apps_payload_empty_is_explicit_not_fabricated(monkeypatch):
    _seed(monkeypatch, opens=[])
    body = perception_core.recent_apps_payload(_Store(), limit_raw=None, hours_raw=None, now=T0)
    assert body == {"ok": True, "apps": [], "count": 0, "window_hours": None}


def test_recent_apps_payload_rejects_bad_limit(monkeypatch):
    _seed(monkeypatch, opens=[])
    try:
        perception_core.recent_apps_payload(_Store(), limit_raw="abc", hours_raw=None, now=T0)
    except perception_core.AgentRouteError as err:
        assert err.status_code == 400
        assert err.body["error"] == "invalid_limit"
    else:
        raise AssertionError("expected AgentRouteError")


def test_recent_apps_payload_never_leaks_credentials(monkeypatch):
    _seed(monkeypatch, opens=[{"app": "wechat", "ts": T0, "api_key": "sk-should-not-appear"}])
    body = perception_core.recent_apps_payload(_Store(), limit_raw=None, hours_raw=None, now=T0)
    assert body["apps"][0] == {
        "app": "wechat",
        "category": None,
        "ts": T0,
        "event": "open",
        "minutes_ago": 0.0,
    }


def test_snapshot_recent_app_event_window_uses_app_signal_ttl(monkeypatch):
    ttl = catalog.SIGNALS["app"].ttl_sec
    now = T0 + 2000
    _seed(
        monkeypatch,
        opens=[
            {"app": "at-boundary", "ts": now - ttl},
            {"app": "outside", "ts": now - ttl - 0.001},
        ],
        closes=[{"app": "closed-inside", "ts": now - ttl + 1}],
    )

    expected = [("closed-inside", "close"), ("at-boundary", "open")]
    assert [
        (item["app"], item["event"])
        for item in perception_service.snapshot("u_apps", now)["recent_app_events"]
    ] == expected
    assert [
        (item["app"], item["event"])
        for item in perception_service.pull_snapshot("u_apps", now)["recent_app_events"]
    ] == expected


# ---------------------------------------------------------------------------
# 3. Discoverability -- chat AND proactive must both see the tool
# ---------------------------------------------------------------------------

def test_recent_apps_is_a_query_tool_not_a_state_signal():
    # it returns a list and takes limit/hours -- project_signal's state-field
    # projection is the wrong shape for it, so there is exactly one entry point
    assert "recent_apps" not in agent_fields.AGENT_PERCEPTION_SIGNALS


# ---------------------------------------------------------------------------
# 4. Authorization -- every entry point, not just the signal path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("permission_state", [
    False,
    "off",
    "denied",
    {"enabled": False},
    {"status": "not_authorized"},
])
def test_history_is_unreadable_once_app_capability_is_off(monkeypatch, permission_state):
    """The bypass this fix closes: `app` switched off used to disable the signal
    path while the dedicated tool happily returned the whole trajectory."""
    _seed(monkeypatch, opens=[{"app": "wechat", "category": "social", "ts": T0}])
    monkeypatch.setattr(
        perception_service, "_app_proactive_settings",
        lambda uid: {"permission_states": {"app": permission_state}},
    )

    body = perception_core.recent_apps_payload(_Store(), limit_raw=None, hours_raw=None, now=T0)

    assert body["disabled"] is True
    assert body["reason"]
    assert body["apps"] == []          # no trajectory leaks
    assert body["count"] == 0


def test_denied_history_is_distinguishable_from_no_data(monkeypatch):
    """"no permission" must not look like "she hasn't used any apps"."""
    _seed(monkeypatch, opens=[{"app": "wechat", "ts": T0}])
    monkeypatch.setattr(perception_service, "_app_proactive_settings",
                        lambda uid: {"permission_states": {"app": False}})
    denied = perception_core.recent_apps_payload(_Store(), limit_raw=None, hours_raw=None, now=T0)

    _seed(monkeypatch, opens=[])
    monkeypatch.setattr(perception_service, "_app_proactive_settings", lambda uid: {})
    empty = perception_core.recent_apps_payload(_Store(), limit_raw=None, hours_raw=None, now=T0)

    assert denied.get("disabled") is True
    assert "disabled" not in empty


def test_history_readable_when_app_capability_untouched_or_on(monkeypatch):
    _seed(monkeypatch, opens=[{"app": "wechat", "ts": T0}])
    for settings in ({}, {"permission_states": {}}, {"permission_states": {"app": True}}):
        monkeypatch.setattr(perception_service, "_app_proactive_settings", lambda uid, s=settings: s)
        body = perception_core.recent_apps_payload(_Store(), limit_raw=None, hours_raw=None, now=T0)
        assert body.get("disabled") is None
        assert [a["app"] for a in body["apps"]] == ["wechat"]


def test_settings_read_failure_fails_closed(monkeypatch):
    """If we can't prove the user still allows it, don't hand over the history."""
    _seed(monkeypatch, opens=[{"app": "wechat", "ts": T0}])

    def boom(uid):
        raise RuntimeError("settings backend down")

    monkeypatch.setattr(perception_service, "_app_proactive_settings", boom)
    body = perception_core.recent_apps_payload(_Store(), limit_raw=None, hours_raw=None, now=T0)
    assert body["disabled"] is True
    assert body["apps"] == []


def test_v2_executor_adapter_enforces_the_same_permission(monkeypatch):
    """The other bypass: the proactive adapter reads the stream directly."""
    from proactive.tool_executor_v2 import default_tool_runtime_adapters_v2

    _seed(monkeypatch, opens=[{"app": "wechat", "ts": T0}])
    monkeypatch.setattr(perception_service, "_app_proactive_settings",
                        lambda uid: {"permission_states": {"app": "denied"}})

    adapter = default_tool_runtime_adapters_v2().perception_recent_apps
    result = adapter("u_apps", {"limit": 10})
    assert result["disabled"] is True
    assert result["apps"] == []


def test_v1_and_v2_recent_apps_delegate_to_the_same_service_function(monkeypatch):
    from proactive.tool_executor_v2 import default_tool_runtime_adapters_v2

    calls = []

    def shared(user_id, *, limit=None, hours=None, now=None):
        calls.append((user_id, limit, hours, now))
        return {"ok": True, "apps": [], "count": 0, "window_hours": hours}

    monkeypatch.setattr(perception_service, "recent_apps", shared)
    v1 = perception_core.recent_apps_payload(
        _Store(), limit_raw="7", hours_raw="2", now=T0
    )
    v2 = default_tool_runtime_adapters_v2().perception_recent_apps(
        "u_apps", {"limit": 7, "hours": 2}
    )

    assert v1 == v2
    assert calls == [
        ("u_apps", 7, 2.0, T0),
        ("u_apps", 7, 2.0, None),
    ]


def test_proactive_catalog_registers_recent_apps_as_a_perception_tool():
    spec = tool_catalog_v2.default_tool_catalog_v2().get("perception.recent_apps")
    assert spec.group == "perception"
    assert spec.cost_class == tool_catalog_v2.FAST


def test_foreground_chat_can_call_recent_apps():
    # the regression this whole ticket is about: normal chat could not reach it
    assert "perception.recent_apps" in tool_catalog_v2.FOREGROUND_CHAT_TOOL_NAMES_V2
