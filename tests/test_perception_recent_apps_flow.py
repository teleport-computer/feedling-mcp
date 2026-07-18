"""End-to-end app-perception flow, clock-controlled (Lark t100530).

Drives the REAL code path the user's phone drives -- the /v1/perception/app_open
Shortcut ingest, the real service + store write, then the two agent read paths
(chat CLI tools and the hosted proactive executor) -- with a fake clock so the
"more than 5 minutes, less than 15" window is exercised without waiting.

Only Postgres is faked (the FakeStore from test_perception, whose app-open
semantics mirror db.log_read).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402

from agent import perception_core  # noqa: E402
from perception import perception_read_core  # noqa: E402
from perception import service as perception_service  # noqa: E402
from perception import store as perception_store  # noqa: E402
from proactive.tool_executor_v2 import (  # noqa: E402
    ToolBudgetV2,
    ToolCallV2,
    ToolExecutorV2,
    default_tool_runtime_adapters_v2,
)

from test_perception import FakeStore  # noqa: E402


UID = "usr_flow_apps"
T0 = 1_700_000_000.0


class _Clock:
    def __init__(self, t):
        self.t = t

    def advance(self, seconds):
        self.t += seconds

    def __call__(self):
        return self.t


class _AuthStore:
    user_id = UID

    def load_proactive_settings(self):
        return {}


@pytest.fixture()
def flow(monkeypatch):
    fake = FakeStore()
    fake.user_id = UID
    clock = _Clock(T0)
    monkeypatch.setattr(perception_service, "store", fake)
    monkeypatch.setattr(perception_service, "_now", clock)
    # the agent read path resolves state through perception.store
    monkeypatch.setattr(perception_store, "get_state", fake.get_state)
    monkeypatch.setattr(perception_store, "read_app_opens", fake.read_app_opens)
    return fake, clock


def _open_app(fake, app, category=None):
    """The exact iOS Shortcut call: GET /v1/perception/app_open?app=...&category=..."""
    query = {"app": app, "category": category}
    body, status = perception_read_core.app_open(fake, type("Q", (), {"get": query.get})())
    assert status == 200, body
    return body


def test_shortcut_to_chat_answer_across_the_ten_minute_gap(flow):
    fake, clock = flow

    # 1-2. two Shortcut pings, 60s apart
    assert _open_app(fake, "wechat", "social")["app"] == "wechat"
    clock.advance(60)
    assert _open_app(fake, "xiaohongshu", "social")["app"] == "xiaohongshu"

    # 3. ten minutes pass -- past the OLD 300s window, inside the new 900s one
    clock.advance(600)

    # 4. a normal chat turn pulls perception the way io_cli does
    payload = perception_core.agent_perception_payload(_AuthStore(), signals_raw="app")
    assert payload["ok"] is True

    # current app is still there (this is what used to come back None)
    assert payload["signals"]["app"]["app_name"] == "xiaohongshu"
    assert payload["signals"]["app"]["app_category"] == "social"

    # 5-6. and the history tool answers "what have I been using?"
    recent = perception_core.recent_apps_payload(
        _AuthStore(), limit_raw=None, hours_raw=None
    )
    assert [a["app"] for a in recent["apps"]] == ["xiaohongshu", "wechat"]
    assert recent["count"] == 2
    assert recent["apps"][0]["minutes_ago"] == 10.0
    assert recent["apps"][1]["minutes_ago"] == 11.0
    assert recent["apps"][0]["category"] == "social"


def test_current_app_expires_after_fifteen_minutes_but_history_survives(flow):
    fake, clock = flow
    _open_app(fake, "wechat", "social")

    clock.advance(1200)  # 20 minutes
    payload = perception_core.agent_perception_payload(_AuthStore(), signals_raw="app")
    # the last-seen app has aged out, so the agent must not assert it ...
    assert payload["signals"]["app"]["app_name"] is None
    # ... but it can still say what she was doing 20 minutes ago
    apps = perception_core.recent_apps_payload(
        _AuthStore(), limit_raw=None, hours_raw=None
    )["apps"]
    assert [a["app"] for a in apps] == ["wechat"]
    assert apps[0]["minutes_ago"] == 20.0


def test_dedicated_route_and_hours_window(flow):
    fake, clock = flow
    _open_app(fake, "wechat", "social")
    clock.advance(7200)  # 2h
    _open_app(fake, "xiaohongshu", "social")
    clock.advance(60)

    # GET /v1/agent/perception/recent_apps?hours=1 -> only the recent one
    body = perception_core.recent_apps_payload(_AuthStore(), limit_raw=None, hours_raw="1")
    assert [a["app"] for a in body["apps"]] == ["xiaohongshu"]

    # no window -> both
    body_all = perception_core.recent_apps_payload(_AuthStore(), limit_raw=None, hours_raw=None)
    assert [a["app"] for a in body_all["apps"]] == ["xiaohongshu", "wechat"]


def test_proactive_runtime_reads_the_same_history(flow):
    """7. the proactive/hosted-V2 lane must not regress -- same data, same shape."""
    fake, clock = flow
    _open_app(fake, "wechat", "social")
    clock.advance(60)
    _open_app(fake, "xiaohongshu", "social")
    clock.advance(600)

    executor = ToolExecutorV2(
        adapters=default_tool_runtime_adapters_v2(),
        budget=ToolBudgetV2(fast_hard_limit=8, slow_inline_limit=2),
    )
    result = executor.execute(ToolCallV2("perception.recent_apps", user_id=UID, args={"limit": 5}))
    assert result.ok is True, result
    assert [a["app"] for a in result.result["apps"]] == ["xiaohongshu", "wechat"]
    assert result.result["apps"][0]["minutes_ago"] == 10.0

    # and the proactive lane still sees the current app in the cheap snapshot
    snap = executor.execute(ToolCallV2("perception.now", user_id=UID))
    assert snap.result["snapshot"]["app_name"] == "xiaohongshu"


def test_no_data_returns_an_explicit_empty_not_a_guess(flow):
    body = perception_core.recent_apps_payload(_AuthStore(), limit_raw=None, hours_raw=None)
    assert body["apps"] == []
    assert body["count"] == 0
