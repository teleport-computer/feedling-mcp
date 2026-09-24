from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from tools.e2e import perception_probe, proactive_probe

from conftest import capture_sleeps


class _Response:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self) -> dict:
        return self._body


def test_proactive_model_cases_always_run_and_invariants_run_once(monkeypatch):
    monkeypatch.setattr(proactive_probe, "_case_user_turn_priority", lambda _c: "priority")
    monkeypatch.setattr(proactive_probe, "_case_proactive_message_quality", lambda _c: "quality")
    monkeypatch.setattr(proactive_probe, "_case_scheduled_must_deliver", lambda _c: "scheduled")
    monkeypatch.setattr(proactive_probe, "_case_wake_coalescing", lambda _c: "coalesced")
    monkeypatch.setattr(proactive_probe, "_case_stale_wake_expiry", lambda _c: "expired")
    monkeypatch.setattr(proactive_probe, "_case_dream_latest_only", lambda _c: "latest")
    monkeypatch.setattr(proactive_probe, "_case_self_wake_min_lead", lambda _c: "clamped")

    provider_only = proactive_probe.run_proactive_probe(object(), {"run_invariants": False})
    with_invariants = proactive_probe.run_proactive_probe(object(), {"run_invariants": True})

    assert provider_only == {
        "area": "proactive",
        "cases": [
            {"name": "proactive_message_quality", "result": "PASS", "detail": "quality"},
            {"name": "user_turn_priority", "result": "PASS", "detail": "priority"},
            {"name": "scheduled_must_deliver", "result": "PASS", "detail": "scheduled"},
        ],
    }
    assert len(with_invariants["cases"]) == 9
    assert {case["result"] for case in with_invariants["cases"]} == {"PASS", "BLOCKED_EVIDENCE"}


def test_perception_model_case_always_runs_and_invariants_run_once(monkeypatch):
    monkeypatch.setattr(perception_probe, "_case_permission_honesty", lambda _c: "honest")
    monkeypatch.setattr(perception_probe, "_case_fast_slow_snapshot", lambda _c: "snapshot")
    monkeypatch.setattr(perception_probe, "_case_timezone_boundary", lambda _c: "timezone")
    monkeypatch.setattr(perception_probe, "_case_grounding", lambda _c: "grounded")

    provider_only = perception_probe.run_perception_probe(object(), {"run_invariants": False})
    with_invariants = perception_probe.run_perception_probe(object(), {"run_invariants": True})

    assert provider_only == {
        "area": "perception",
        "cases": [{"name": "perception_grounding", "result": "PASS", "detail": "grounded"}],
    }
    assert [case["name"] for case in with_invariants["cases"]] == [
        "permission_honesty",
        "fast_slow_signal_snapshot",
        "timezone_boundary",
        "perception_grounding",
    ]
    assert all(case["result"] == "PASS" for case in with_invariants["cases"])


def test_existing_identity_is_replaced_with_untampered_envelope():
    envelope = {"id": "aad-bound-id", "body_ct": "ciphertext"}

    class Client:
        def __init__(self):
            self.calls = []

        def _seal(self, plaintext):
            self.plaintext = plaintext
            return dict(envelope)

        def post(self, path, *, json):
            self.calls.append((path, json))
            if path == "/v1/identity/init":
                return _Response(409, {"error": "already_initialized"})
            return _Response(200, {"status": "replaced"})

    client = Client()
    proactive_probe._install_identity(client, {"agent_name": "probe"}, action="test")

    assert [path for path, _body in client.calls] == [
        "/v1/identity/init",
        "/v1/identity/replace",
    ]
    assert client.calls[1][1]["envelope"] == envelope
    assert client.calls[1][1]["envelope"]["id"] == "aad-bound-id"


def test_identity_init_retries_server_confirmed_earliest_memory_days():
    class Client:
        def __init__(self):
            self.payloads = []

        def post(self, path, *, json):
            assert path == "/v1/identity/init"
            self.payloads.append(dict(json))
            if len(self.payloads) == 1:
                return _Response(400, {
                    "error": "days_with_user_mismatch",
                    "computed_from_earliest_memory": 17,
                    "earliest_memory_date": "2026-07-04",
                })
            return _Response(201, {"status": "created"})

    client = Client()
    proactive_probe._install_identity(client, {"agent_name": "probe"}, action="test")

    assert [payload["days_with_user"] for payload in client.payloads] == [0, 17]
    assert client.payloads[1]["relationship_anchor_evidence"].endswith("2026-07-04")


def test_transport_failure_uses_explicit_result_enum():
    result = proactive_probe._case(
        "transport",
        lambda: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )

    assert result == {
        "name": "transport",
        "result": "BLOCKED_DEPLOYMENT",
        "detail": "transport failure: ConnectError",
    }


def test_wake_terminal_state_separates_legal_sleep_from_unaudited_misfire():
    base = {
        "v2_recent_jobs": {
            "jobs": [{"job_id": 17, "lane": "manual_wake", "status": "completed"}],
        },
        "v2_wake_activity": {"recent_failures": [], "recent_silences": []},
    }
    silent = {
        **base,
        "v2_wake_activity": {
            "recent_failures": [],
            "recent_silences": [{
                "job_id": 17,
                "lane": "manual_wake",
                "reason": "explicit_silence_suppressed",
            }],
        },
    }

    assert proactive_probe._wake_terminal_state(silent, "17") == (
        "silent",
        "explicit_silence_suppressed",
    )
    assert proactive_probe._wake_terminal_state(base, "17") == (
        "completed_without_output",
        "completed without visible reply or explicit sleep",
    )


@pytest.mark.parametrize(
    ("silences", "expected_result", "detail_fragment"),
    [
        ([{"job_id": 17, "reason": "explicit_silence_suppressed"}],
         "BLOCKED_EVIDENCE", "legally slept"),
        ([], "PRODUCT_FAIL", "completed without visible reply"),
    ],
)
def test_wake_delivery_waiter_preserves_silent_and_misfire_extremes(
    monkeypatch,
    silences,
    expected_result,
    detail_fragment,
):
    times = iter((0.0, 0.0, 2.0))
    snapshot = {
        "v2_recent_jobs": {"jobs": [{"job_id": 17, "status": "completed"}]},
        "v2_wake_activity": {
            "recent_failures": [],
            "recent_silences": silences,
        },
    }
    monkeypatch.setattr(proactive_probe.time, "time", lambda: next(times))
    capture_sleeps(monkeypatch, proactive_probe)
    monkeypatch.setattr(proactive_probe, "_history", lambda *_a, **_kw: [])
    monkeypatch.setattr(proactive_probe, "_admin_user", lambda _c: snapshot)

    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._wait_for_wake_delivery(
            object(), 0.0, "17", action="quality", timeout=1.0,
        )

    assert exc.value.result == expected_result
    assert detail_fragment in exc.value.detail


def test_collision_wait_has_recent_and_clear_window_extremes(monkeypatch):
    sleeps = []
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 100.0)
    capture_sleeps(monkeypatch, proactive_probe, sleeps)
    monkeypatch.setattr(
        proactive_probe,
        "_history",
        lambda *_a, **_kw: [{"role": "user", "ts": 50.0}],
    )

    waited = proactive_probe._wait_out_chat_collision(object(), window=90.0)

    assert waited == 46.0
    assert sleeps == [46.0]

    sleeps.clear()
    monkeypatch.setattr(
        proactive_probe,
        "_history",
        lambda *_a, **_kw: [{"role": "user", "ts": 4.0}],
    )
    assert proactive_probe._wait_out_chat_collision(object(), window=90.0) == 0.0
    assert sleeps == []


@pytest.mark.parametrize("text,expected", [
    ("七七，周一中午，此刻陪你，记忆小测验告一段落了吗？", "PASS"),
    ("七七，此刻陪你，也会记得上海时区。", "PRODUCT_FAIL"),
    ("七七，周一晚上，此刻陪你。", "PRODUCT_FAIL"),
    ("七七，周日中午，此刻陪你。", "PRODUCT_FAIL"),
])
def test_quality_probe_does_not_create_a_setup_chat_inside_collision_window(monkeypatch, text, expected):
    client = _PriorityClient()
    reply = {"role": "agent", "id": "quality-reply", "ts": _ts("2026-09-21T12:59:00+08:00")}
    response = httpx.Response(200, headers={"date": "Mon, 21 Sep 2026 04:58:00 GMT"},
                              json={"job": {"id": "wake-quality", "lane": "manual_wake"}})

    def post(path, **kwargs):
        client.posts.append((path, kwargs.get("json")))
        return response

    monkeypatch.setattr(client, "post", post)
    monkeypatch.setattr(proactive_probe, "_install_quality_identity", lambda _c: None)
    monkeypatch.setattr(proactive_probe, "_save_settings", lambda _c, _patch: {})
    monkeypatch.setattr(proactive_probe, "_wait_out_chat_collision", lambda _c: 0.0)
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 10.0)
    capture_sleeps(monkeypatch, proactive_probe)
    monkeypatch.setattr(
        proactive_probe,
        "_wait_for_wake_delivery",
        lambda *_a, **_kw: reply,
    )
    monkeypatch.setattr(
        proactive_probe,
        "_send_hosted",
        lambda *_a, **_kw: pytest.fail("quality probe must not create a setup user turn"),
    )
    monkeypatch.setattr(
        proactive_probe,
        "_decrypt",
        lambda *_a, **_kw: text,
    )
    monkeypatch.setattr(proactive_probe, "_history", lambda *_a, **_kw: [reply])

    result = proactive_probe._case("quality", lambda: proactive_probe._case_proactive_message_quality(client))
    assert result["result"] == expected
    if expected == "PASS":
        assert "collision_wait=0.0s" in result["detail"]
        assert "2026-09-21T12:57:59+08:00" in result["detail"]
    assert [path for path, _body in client.posts] == ["/v1/proactive/tick"]


def _ts(value):
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize("stamp,text", [
    ("2026-09-21T12:59:00+08:00", "周一中午，陪你坐会儿。"),
    ("2026-09-22T02:00:00+08:00", "星期二凌晨，睡不着吗？"),
    ("2026-09-23T06:00:01+08:00", "礼拜三早晨，吃早饭了吗？"),
    ("2026-09-24T11:59:00+08:00", "周四上午，先喝口水。"),
    ("2026-09-25T14:00:01+08:00", "星期五午后，可以歇一会。"),
    ("2026-09-26T18:00:01+08:00", "礼拜六晚间，忙完了吗？"),
    ("2026-09-27T20:00:00+08:00", "星期天晚上，陪你。"),
    ("2026-09-27T20:00:00+08:00", "周日夜晚，陪你。"),
])
def test_quality_grounding_uses_shanghai_weekday_and_period(stamp, text):
    ts = _ts(stamp)
    assert "Asia/Shanghai" in proactive_probe._assert_timezone_grounding(text, ts, ts + 30)


@pytest.mark.parametrize("hour,word", [
    (7, "一早"), (7, "清早"), (7, "一大早"), (7, "大清早"),
    (5, "一早"), (11, "清早"),
    (17, "傍晚"), (18, "傍晚"),
    (18, "今晚"), (20, "今晚"), (20, "今夜"),
    (22, "深夜"), (23, "深夜"), (22, "半夜"), (23, "午夜"),
    (0, "半夜"), (0, "午夜"), (5, "半夜"), (5, "午夜"),
])
def test_quality_grounding_accepts_natural_period_synonyms(hour, word):
    ts = _ts(f"2026-09-24T{hour:02d}:05:00+08:00")
    proactive_probe._assert_timezone_grounding(f"周四{word}，此刻陪你。", ts, ts + 30)


@pytest.mark.parametrize("hour,word", [
    (4, "一早"), (12, "清早"), (12, "一大早"),
    (16, "傍晚"), (19, "傍晚"),
    (17, "今晚"), (12, "今夜"),
    (21, "深夜"), (21, "半夜"), (22, "午夜"),
    (6, "半夜"), (6, "午夜"),
])
def test_quality_grounding_synonyms_do_not_match_other_hours(hour, word):
    ts = _ts(f"2026-09-24T{hour:02d}:05:00+08:00")
    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._assert_timezone_grounding(f"周四{word}", ts, ts + 30)
    assert exc.value.result == "PRODUCT_FAIL"


@pytest.mark.parametrize("text", ["上海时区", "北京时间，周一晚上", "周日中午", "周一", "中午"])
def test_quality_grounding_rejects_place_name_or_wrong_time(text):
    ts = _ts("2026-09-21T12:59:00+08:00")
    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._assert_timezone_grounding(text, ts, ts + 30)
    assert exc.value.result == "PRODUCT_FAIL"


@pytest.mark.parametrize("start,end,text", [
    ("2026-09-21T23:59:30+08:00", "2026-09-22T00:00:30+08:00", "周一晚上"),
    ("2026-09-21T23:59:30+08:00", "2026-09-22T00:00:30+08:00", "周二凌晨"),
    ("2026-09-21T13:59:30+08:00", "2026-09-21T14:00:30+08:00", "周一中午"),
    ("2026-09-21T13:59:30+08:00", "2026-09-21T14:00:30+08:00", "周一下午"),
])
def test_quality_grounding_accepts_generation_boundary(start, end, text):
    proactive_probe._assert_timezone_grounding(text, _ts(start), _ts(end))


def test_quality_grounding_does_not_mix_weekday_and_period_across_midnight():
    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._assert_timezone_grounding(
            "周二晚上", _ts("2026-09-21T23:59:30+08:00"), _ts("2026-09-22T00:00:30+08:00"))
    assert exc.value.result == "PRODUCT_FAIL"


@pytest.mark.parametrize("start,end", [(0, 10), (10, 0), (10, float("nan")),
                                       (float("inf"), 10), (20, 10), (10, 99999), (1e20, 1e20)])
def test_quality_grounding_missing_clock_is_not_a_product_failure(start, end):
    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._assert_timezone_grounding("周一中午", start, end)
    assert exc.value.result == "BLOCKED_EVIDENCE"


@pytest.mark.parametrize("date", ["", "not-a-date", "Mon, 21 Sep 2026 04:58:00"])
def test_quality_grounding_requires_server_date(date):
    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._server_response_time(httpx.Response(200, headers={"date": date}))
    assert exc.value.result == "BLOCKED_EVIDENCE"


def test_wait_for_scheduled_fire_returns_exact_agent_job_then_times_out(monkeypatch):
    # Green: the REAL V2 scheduler attaches fired_job_id to the due timer.
    fired = {"v2_scheduled_wakes": [
        {"timer_id": "timer-1", "status": "fired", "fired_job_id": 15113},
        {"timer_id": "timer-2", "status": "fired", "fired_job_id": 42},
    ]}
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 0.0)

    class _DebugClient:
        def get(self, _path, **_kw):
            return _Response(200, fired)

    assert proactive_probe._wait_for_scheduled_fire(
        _DebugClient(), "timer-1", timeout=1.0,
    ) == 15113

    # Red: the compat fire path leaves the timer without a real agent_job; the
    # real scheduler never attaches fired_job_id -> bounded PRODUCT_FAIL.
    times = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(proactive_probe.time, "time", lambda: next(times))
    capture_sleeps(monkeypatch, proactive_probe)
    pending = {"v2_scheduled_wakes": [
        {"timer_id": "timer-1", "status": "scheduled", "fired_job_id": 0},
    ]}

    class _PendingClient:
        def get(self, _path, **_kw):
            return _Response(200, pending)

    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._wait_for_scheduled_fire(_PendingClient(), "timer-1", timeout=1.0)
    assert exc.value.result == "PRODUCT_FAIL"
    assert "did not fire due timer" in exc.value.detail


def test_user_turn_priority_runs_no_competition_control_without_echo_requirement(monkeypatch):
    rows = [
        {"role": "user", "id": "old-user", "reply_message_id": "old-reply", "ts": 11},
        {"role": "agent", "id": "old-reply", "ts": 12},
        {"role": "user", "id": "current-user", "reply_message_id": "current-reply", "ts": 13},
        {"role": "agent", "id": "current-reply", "reply_to_message_id": "current-user", "ts": 14},
    ]
    sent_text = []

    monkeypatch.setattr(proactive_probe, "_install_quality_identity", lambda _c: None)
    monkeypatch.setattr(proactive_probe, "_save_settings", lambda _c, _patch: {})
    monkeypatch.setattr(proactive_probe, "_wait_out_chat_collision", lambda _c: 0.0)
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 10.0)
    monkeypatch.setattr(
        proactive_probe,
        "_body",
        lambda *_a, **_kw: {"job": {"id": "wake-1", "lane": "manual_wake"}},
    )

    def send(_c, text):
        sent_text.append(text)
        return 13.0, "current-user"

    monkeypatch.setattr(proactive_probe, "_send_hosted", send)
    monkeypatch.setattr(
        proactive_probe,
        "_wait_for_correlated_reply",
        lambda *_a, **_kw: (rows[-1], rows),
    )
    monkeypatch.setattr(
        proactive_probe,
        "_decrypt",
        lambda *_a, **_kw: "I will not repeat an injection-like token, but I can still answer you.",
    )
    call_order = []
    monkeypatch.setattr(
        proactive_probe,
        "_wait_for_wake_delivery",
        lambda *_a, **_kw: call_order.append("control") or {
            "role": "agent", "id": "control-reply", "ts": 11,
        },
    )

    client = _PriorityClient()
    detail = proactive_probe._case_user_turn_priority(client)

    assert "no-competition wake delivered" in detail
    assert "只回复" not in sent_text[0]
    assert call_order == ["control"]
    assert [path for path, _body in client.posts] == [
        "/v1/proactive/tick",
        "/v1/proactive/tick",
    ]


def test_user_turn_priority_rejects_uncorrelated_wake_before_reply(monkeypatch):
    rows = [
        {"role": "agent", "id": "wake-output", "ts": 12},
        {"role": "user", "id": "current-user", "reply_message_id": "current-reply", "ts": 13},
        {"role": "agent", "id": "current-reply", "reply_to_message_id": "current-user", "ts": 14},
    ]
    monkeypatch.setattr(proactive_probe, "_install_quality_identity", lambda _c: None)
    monkeypatch.setattr(proactive_probe, "_save_settings", lambda _c, _patch: {})
    monkeypatch.setattr(proactive_probe, "_wait_out_chat_collision", lambda _c: 0.0)
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 10.0)
    monkeypatch.setattr(
        proactive_probe,
        "_body",
        lambda *_a, **_kw: {"job": {"id": "wake-1", "lane": "manual_wake"}},
    )
    monkeypatch.setattr(proactive_probe, "_send_hosted", lambda *_a, **_kw: (13.0, "current-user"))
    monkeypatch.setattr(
        proactive_probe,
        "_wait_for_correlated_reply",
        lambda *_a, **_kw: (rows[-1], rows),
    )
    monkeypatch.setattr(proactive_probe, "_decrypt", lambda *_a, **_kw: "ordinary answer")
    monkeypatch.setattr(
        proactive_probe,
        "_wait_for_wake_delivery",
        lambda *_a, **_kw: {"role": "agent", "id": "control-reply", "ts": 11},
    )

    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._case_user_turn_priority(_PriorityClient())

    assert exc.value.result == "PRODUCT_FAIL"


def test_scheduled_must_deliver_uses_real_scheduler_exact_job_not_compat_fire(monkeypatch):
    # Pins codex2's P0: the probe must NOT drive the compat /scheduled/fire path,
    # and delivery must be keyed on the EXACT agent_jobs id the real scheduler
    # attaches (not the legacy pj).
    class Client:
        def post(self, path, *, json):
            if path == "/v1/proactive/scheduled/actions":
                return _Response(200, {
                    "results": [{"status": "scheduled", "timer_id": "timer-1"}],
                })
            raise AssertionError(f"must NOT POST the compat fire path: {path}")

    client = Client()
    monkeypatch.setattr(proactive_probe, "_install_quality_identity", lambda _c: None)
    monkeypatch.setattr(proactive_probe, "_save_settings", lambda _c, _patch: {})
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 10.0)
    monkeypatch.setattr(
        proactive_probe, "_wait_for_scheduled_fire", lambda _c, _timer, **_kw: 15113)
    captured: dict = {}

    def _deliver(_c, _since, fired_job_id, *, timeout=None):
        captured["job_id"] = fired_job_id
        return {"role": "agent", "id": "scheduled-reply", "ts": 11}

    monkeypatch.setattr(proactive_probe, "_wait_for_scheduled_delivery", _deliver)
    monkeypatch.setattr(
        proactive_probe, "_decrypt", lambda *_a, **_kw: "到时间了，这是你要的提醒。")

    detail = proactive_probe._case_scheduled_must_deliver(client)
    assert "exact V2 scheduled agent_job" in detail
    assert "agent_job=15113" in detail
    assert captured["job_id"] == 15113  # exact agent_jobs id, never the pj


def test_scheduled_must_deliver_propagates_when_real_scheduler_never_fires(monkeypatch):
    class Client:
        def post(self, path, *, json):
            if path == "/v1/proactive/scheduled/actions":
                return _Response(200, {
                    "results": [{"status": "scheduled", "timer_id": "timer-1"}],
                })
            raise AssertionError(path)

    monkeypatch.setattr(proactive_probe, "_install_quality_identity", lambda _c: None)
    monkeypatch.setattr(proactive_probe, "_save_settings", lambda _c, _patch: {})
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 10.0)

    def _never(_c, timer_id, **_kw):
        raise proactive_probe._ProbeIssue(
            "PRODUCT_FAIL", f"the V2 scheduler did not fire due timer={timer_id}")

    monkeypatch.setattr(proactive_probe, "_wait_for_scheduled_fire", _never)
    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._case_scheduled_must_deliver(Client())
    assert exc.value.result == "PRODUCT_FAIL"
    assert "did not fire due timer" in exc.value.detail


class _PriorityClient:
    def __init__(self):
        self.posts = []

    def post(self, _path, **_kwargs):
        self.posts.append((_path, _kwargs.get("json")))
        return _Response(200, {})


def test_sanitize_wake_reason_passes_closed_set_and_redacts_everything_else():
    # Closed-set codes pass through verbatim.
    assert proactive_probe._sanitize_wake_reason(
        "wake_failed:v2_summary_frontier_integrity_error"
    ) == "wake_failed:v2_summary_frontier_integrity_error"
    assert proactive_probe._sanitize_wake_reason("failed") == "failed"
    assert proactive_probe._sanitize_wake_reason("") == "unspecified"
    # A relay raw error body appended to a known prefix must NEVER be echoed —
    # last_error is only length-truncated server-side, not reduced to an enum.
    secret = "wake_failed:providererror quota=50000 user=alice@example.com body={...}"
    assert proactive_probe._sanitize_wake_reason(secret) == "redacted"
    assert "alice@example.com" not in proactive_probe._sanitize_wake_reason(secret)
    assert proactive_probe._sanitize_wake_reason("arbitrary free text") == "redacted"


def test_wake_terminal_state_redacts_nonenumerated_failure_reason():
    # Pins the privacy P0 at the shared terminal surface every waiter uses: a
    # last_error carrying a raw body reaches the caller only as "redacted".
    user = {
        "v2_recent_jobs": {"jobs": [{"job_id": 15113, "status": "failed"}]},
        "v2_wake_activity": {
            "recent_failures": [{
                "job_id": 15113,
                "lane": "scheduled",
                "reason": 'wake_failed:providererror 429 {"error":"quota","email":"a@b.co"}',
            }],
            "recent_silences": [],
        },
    }
    state, detail = proactive_probe._wake_terminal_state(user, "15113")
    assert state == "failed"
    assert detail == "redacted"
    assert "quota" not in detail and "a@b.co" not in detail


def test_admin_user_classifies_transport_failure_not_raw_httpx(monkeypatch):
    # A transport error reading the admin surface must become a classified
    # BLOCKED_EVIDENCE, never a raw httpx.HTTPError that aborts the case verdict.
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", "token")

    def _boom(*_a, **_kw):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(proactive_probe.httpx, "get", _boom)

    class _C:
        api_url = "https://example.invalid"
        user_id = "usr_1"

    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._admin_user(_C())
    assert exc.value.result == "BLOCKED_EVIDENCE"
    assert "transport failed" in exc.value.detail


def test_scheduled_delivery_requires_exact_activity_job_id_over_concurrent_wake(monkeypatch):
    # Green: a reply correlated to THIS scheduled job by activity_job_id passes,
    # and a concurrent heartbeat bubble does not stand in for it.
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 0.0)
    rows = [
        {"role": "agent", "id": "heartbeat", "activity_job_id": "999", "ts": 5},
        {"role": "agent", "id": "scheduled", "activity_job_id": "15113", "ts": 6},
    ]
    monkeypatch.setattr(proactive_probe, "_history", lambda *_a, **_kw: rows)
    monkeypatch.setattr(
        proactive_probe, "_admin_user",
        lambda _c: (_ for _ in ()).throw(AssertionError("exact reply must not consult admin")))

    reply = proactive_probe._wait_for_scheduled_delivery(object(), 0.0, 15113, timeout=1.0)
    assert reply["id"] == "scheduled"


def test_scheduled_delivery_fails_when_exact_job_failed_despite_concurrent_reply(monkeypatch):
    # Red (codex2's counterexample): a concurrent heartbeat reply exists, but THIS
    # scheduled job failed on the backend -> PRODUCT_FAIL, never green off the
    # unrelated bubble.
    times = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(proactive_probe.time, "time", lambda: next(times))
    capture_sleeps(monkeypatch, proactive_probe)
    monkeypatch.setattr(proactive_probe, "_history", lambda *_a, **_kw: [
        {"role": "agent", "id": "heartbeat", "activity_job_id": "999", "ts": 5},
    ])
    monkeypatch.setattr(proactive_probe, "_admin_user", lambda _c: {
        "v2_recent_jobs": {"jobs": [{"job_id": 15113, "status": "failed"}]},
        "v2_wake_activity": {
            "recent_failures": [{"job_id": 15113, "lane": "scheduled",
                                 "reason": "wake_failed:empty_reply"}],
            "recent_silences": [],
        },
    })
    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._wait_for_scheduled_delivery(object(), 0.0, 15113, timeout=1.0)
    assert exc.value.result == "PRODUCT_FAIL"
    assert "wake job=15113 failed" in exc.value.detail
    assert "wake_failed:empty_reply" in exc.value.detail


def test_scheduled_must_deliver_watermark_captured_before_schedule_post(monkeypatch):
    # P1 race regression guard: `started` (the delivery watermark) MUST be taken
    # before the schedule POST — a reply published between the POST returning and
    # a later timestamp would otherwise be hidden. Distinct clock values make the
    # ordering observable; a constant clock (as the other case test uses) cannot,
    # so this guard goes red if `started` is moved back after the POST.
    clock = iter([100.0, 200.0, 300.0, 400.0, 500.0])
    monkeypatch.setattr(proactive_probe.time, "time", lambda: next(clock))
    post_times: list[float] = []

    class Client:
        def post(self, path, *, json):
            if path == "/v1/proactive/scheduled/actions":
                post_times.append(proactive_probe.time.time())
                return _Response(200, {
                    "results": [{"status": "scheduled", "timer_id": "timer-1"}],
                })
            raise AssertionError(path)

    monkeypatch.setattr(proactive_probe, "_install_quality_identity", lambda _c: None)
    monkeypatch.setattr(proactive_probe, "_save_settings", lambda _c, _patch: {})
    monkeypatch.setattr(
        proactive_probe, "_wait_for_scheduled_fire", lambda _c, _timer, **_kw: 15113)
    captured: dict = {}

    def _deliver(_c, since, _fired, *, timeout=None):
        captured["since"] = since
        return {"role": "agent", "id": "scheduled-reply", "ts": 1}

    monkeypatch.setattr(proactive_probe, "_wait_for_scheduled_delivery", _deliver)
    monkeypatch.setattr(proactive_probe, "_decrypt", lambda *_a, **_kw: "到时间了。")

    proactive_probe._case_scheduled_must_deliver(Client())
    assert post_times, "schedule POST was never issued"
    # The watermark must predate the schedule POST boundary.
    assert captured["since"] < post_times[0]


# --------------------------------------------------------------------------- #
# T635: three probe contracts that drifted behind the batch-actions / V2 dream /
# V2 send behaviour. Each block has the current-contract shape (must accept),
# the pre-T635 shape (must reject — reverting the probe change turns these red),
# and broken shapes that a lenient check would let through.
# --------------------------------------------------------------------------- #
from tools.e2e import experience_probe, memory_probe  # noqa: E402


def _batch_denial(**over):
    body = {
        "status": "failed", "error": "not_found",
        "results": [{"status": "error", "error": "not_found", "http_status": 404,
                     "action": "memory.supersede", "missing": ["x"]}],
        "effects": [], "total_count": 1, "applied_count": 0, "skipped_count": 0, "failed_count": 1,
    }
    body.update(over)
    return body


def test_batch_denied_not_found_accepts_exact_contract():
    assert memory_probe._batch_denied_not_found(400, _batch_denial()) is True


@pytest.mark.parametrize("status,body", [
    (404, {"error": "not_found"}),                                  # pre-T635 top-level shape
    (200, _batch_denial()),                                         # success code with failed body
    (400, _batch_denial(status="partial")),
    (400, _batch_denial(error="memory_action_failed")),
    (400, {**_batch_denial(), "results": []}),
    (400, {**_batch_denial(), "results": [_batch_denial()["results"][0]] * 2}),
    (400, {**_batch_denial(), "results": [{"status": "error", "error": "not_found", "http_status": 404.5}]}),
    (400, {**_batch_denial(), "results": [{"status": "error", "error": "not_found", "http_status": "404"}]}),
    (400, {**_batch_denial(), "results": [{"status": "error", "error": "not_found"}]}),
    (400, _batch_denial(applied_count=None)),
    (400, _batch_denial(applied_count=0.5)),
    (400, _batch_denial(applied_count=False)),
    (400, _batch_denial(failed_count="1")),
    (400, {k: v for k, v in _batch_denial().items() if k != "applied_count"}),
    (400, "not a dict"),
])
def test_batch_denied_not_found_rejects_old_and_broken_shapes(status, body):
    assert memory_probe._batch_denied_not_found(status, body) is False


def _v2_noop(**over):
    # Wire shape from proactive_core._dream_response_doc: no ``job`` key when
    # there is no job (measured on test, T635 r2).
    tick = {"enqueued": False, "reason": "v2_scheduler_owned",
            "state": {"last_dream_completed_at": 0}, "new_cards": 0, "new_turns": 0}
    tick.update(over)
    return tick


def test_v2_dream_noop_shape_accepts_exact_contract():
    assert proactive_probe._v2_dream_noop_shape(_v2_noop()) is True


@pytest.mark.parametrize("tick", [
    _v2_noop(job=None),                                     # job key present at all = off-wire
    _v2_noop(job={"job_id": "1", "job_kind": "memory_dream"}),
    _v2_noop(enqueued=True),
    _v2_noop(reason="dream_already_pending"),
    _v2_noop(state=None),
    {"enqueued": True, "job": {"job_kind": "memory_dream"}},  # pre-T635 V1 shape
    {},
    "not a dict",
])
def test_v2_dream_noop_shape_rejects_broken_and_v1_shapes(tick):
    assert proactive_probe._v2_dream_noop_shape(tick) is False


def _dream_client(ticks, poll):
    class Client:
        def __init__(self):
            self.calls = []

        def _seal(self, plaintext):
            return {"id": "seed", "body_ct": "x"}

        def post(self, path, *, json):
            self.calls.append(path)
            if path == "/v1/memory/add":
                return _Response(201, {"id": "seed"})
            if path == "/v1/dream/tick":
                return _Response(200, ticks.pop(0))
            raise AssertionError(path)

        def get(self, path, **_kw):
            assert path == "/v1/proactive/jobs/poll"
            return _Response(200, poll)
    return Client()


def test_dream_latest_only_v2_scheduler_owned_passes_on_exact_contract():
    detail = proactive_probe._case_dream_latest_only(_dream_client([_v2_noop(), _v2_noop()], {"jobs": []}))
    assert "V2 scheduler-owned" in detail


@pytest.mark.parametrize("ticks,poll", [
    ([_v2_noop(), _v2_noop()], {}),                                        # poll without jobs list
    ([_v2_noop(), _v2_noop()], {"jobs": [{"job_kind": "memory_dream", "job_id": "9"}]}),
    ([_v2_noop(), _v2_noop(job={"job_id": "1", "job_kind": "memory_dream"})], {"jobs": []}),
    ([_v2_noop(), _v2_noop(reason="dream_already_pending")], {"jobs": []}),
])
def test_dream_latest_only_v2_rejects_off_contract(ticks, poll):
    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._case_dream_latest_only(_dream_client(ticks, poll))
    assert exc.value.result == "PRODUCT_FAIL"


def test_dream_latest_only_v1_single_flight_path_is_unchanged():
    ticks = [
        {"enqueued": True, "job": {"job_kind": "memory_dream", "job_id": "7"}},
        {"enqueued": False, "reason": "dream_already_pending"},
    ]
    poll = {"jobs": [{"job_kind": "memory_dream", "job_id": "7"}]}
    detail = proactive_probe._case_dream_latest_only(_dream_client(ticks, poll))
    assert "duplicate forced dream suppressed" in detail


def _attribution_client(status, body):
    class Client:
        def _request(self, method, path, **_kw):
            assert (method, path) == ("DELETE", "/v1/model_api/delete")
            return _Response(200, {})

        def post(self, path, *, json):
            assert path == "/v1/model_api/chat/send"
            return _Response(status, body)
    return Client()


def test_error_attribution_accepts_400_model_api_not_configured():
    result, detail = experience_probe._error_attribution(
        _attribution_client(400, {"error": "model_api_not_configured"}), {}
    )
    assert result == experience_probe.BLOCKED_EVIDENCE
    assert "400 model_api_not_configured" in detail


@pytest.mark.parametrize("status,body", [
    (503, {"error": "runtime_policy_not_ready"}),   # pre-T635 contract
    (400, {"error": "model_api_not_tested"}),
    (202, {"job_id": "x"}),
    (500, {"error": "boom"}),
    (404, {"error": "not_found"}),
])
def test_error_attribution_rejects_old_and_wrong_shapes(status, body):
    result, _detail = experience_probe._error_attribution(_attribution_client(status, body), {})
    assert result == experience_probe.PRODUCT_FAIL


# --- T635 (codex3 r3): drive the REAL _isolation with a fake pair of clients so
# that reverting only the supersede branch (while keeping the helper) turns red.
class _IsoResponse(_Response):
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=None)  # type: ignore[arg-type]


class _IsoClient:
    """Minimal E2EClient stand-in for memory_probe._isolation.

    ``A`` owns one card whose summary carries the marker; ``B`` sees nothing in
    its index, gets the exact missing shape on fetch, and receives the configured
    (status, body) when it tries to supersede A's card."""

    def __init__(self, *, supersede_status=None, supersede_body=None, a_cards=None):
        self.api_url = "https://test-api.feedling.app"
        self.user_id = "usr_fake"
        self.calls: list[tuple[str, dict]] = []
        self._supersede = (supersede_status, supersede_body)
        self._a_cards = a_cards
        self.torn_down = False

        class _Http:
            def close(self_inner):
                pass
        self._http = _Http()

    def post(self, path, *, json):
        self.calls.append((path, json))
        if path == "/v1/memory/actions":
            action = json["actions"][0]
            if action["type"] == "memory.add":
                self._a_cards = [{"id": "card_A", "summary": action["memory"]["summary"]}]
                return _IsoResponse(200, {"status": "ok", "results": [{"status": "ok", "http_status": 200}]})
            if action["type"] == "memory.supersede":
                return _IsoResponse(*self._supersede)
        if path == "/v1/memory/index":
            return _IsoResponse(200, {"items": list(self._a_cards or [])})
        if path == "/v1/memory/fetch":
            return _IsoResponse(200, {"items": [], "missing_ids": list(json["ids"]), "unavailable_ids": []})
        raise AssertionError(path)

    def teardown(self):
        self.torn_down = True


def _run_isolation(monkeypatch, *, supersede_status, supersede_body):
    a = _IsoClient()
    b = _IsoClient(supersede_status=supersede_status, supersede_body=supersede_body, a_cards=[])
    monkeypatch.setattr(memory_probe.E2EClient, "provision", classmethod(lambda cls, **_kw: b))
    result, detail = memory_probe._isolation(a)
    assert b.torn_down, "account B must always be torn down"
    assert [p for p, _ in b.calls] == ["/v1/memory/index", "/v1/memory/fetch", "/v1/memory/actions"]
    return result, detail


def test_isolation_passes_on_exact_batch_denial(monkeypatch):
    result, detail = _run_isolation(monkeypatch, supersede_status=400, supersede_body=_batch_denial())
    assert result == memory_probe.PASS, detail


@pytest.mark.parametrize("status,body", [
    (404, {"error": "not_found"}),                 # pre-T635 top-level shape must no longer pass
    (200, {"status": "ok"}),
    (201, {"status": "ok"}),
    (400, _batch_denial(applied_count=None)),
    (400, {"status": "failed", "error": "not_found", "results": []}),
    (403, {"error": "forbidden"}),
])
def test_isolation_fails_on_old_or_broken_denial(monkeypatch, status, body):
    result, detail = _run_isolation(monkeypatch, supersede_status=status, supersede_body=body)
    assert result == memory_probe.PRODUCT_FAIL, detail
