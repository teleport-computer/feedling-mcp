from __future__ import annotations

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


def test_quality_probe_does_not_create_a_setup_chat_inside_collision_window(monkeypatch):
    client = _PriorityClient()
    reply = {"role": "agent", "id": "quality-reply", "ts": 12.0}
    monkeypatch.setattr(proactive_probe, "_install_quality_identity", lambda _c: None)
    monkeypatch.setattr(proactive_probe, "_save_settings", lambda _c, _patch: {})
    monkeypatch.setattr(proactive_probe, "_wait_out_chat_collision", lambda _c: 0.0)
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 10.0)
    capture_sleeps(monkeypatch, proactive_probe)
    monkeypatch.setattr(
        proactive_probe,
        "_body",
        lambda *_a, **_kw: {"job": {"id": "wake-quality", "lane": "manual_wake"}},
    )
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
        lambda *_a, **_kw: "七七，此刻陪你，也会记得上海时区。",
    )
    monkeypatch.setattr(proactive_probe, "_history", lambda *_a, **_kw: [reply])

    detail = proactive_probe._case_proactive_message_quality(client)

    assert "collision_wait=0.0s" in detail
    assert [path for path, _body in client.posts] == ["/v1/proactive/tick"]


def test_proactive_reply_correlation_has_exact_and_unrelated_job_extremes(monkeypatch):
    exact = {"role": "agent", "id": "reply-1", "proactive_job_id": "pj-1", "ts": 1}
    unrelated = {"role": "agent", "id": "reply-2", "proactive_job_id": "pj-2", "ts": 1}
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 0.0)
    monkeypatch.setattr(proactive_probe, "_history", lambda *_a, **_kw: [exact])
    assert proactive_probe._wait_for_proactive_reply(
        object(), "pj-1", 0.0, timeout=1.0,
    ) == exact

    times = iter((0.0, 0.0, 2.0))
    monkeypatch.setattr(proactive_probe.time, "time", lambda: next(times))
    capture_sleeps(monkeypatch, proactive_probe)
    monkeypatch.setattr(proactive_probe, "_history", lambda *_a, **_kw: [unrelated])
    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._wait_for_proactive_reply(
            object(), "pj-1", 0.0, timeout=1.0,
        )
    assert exc.value.result == "PRODUCT_FAIL"
    assert "no correlated must-deliver reply" in exc.value.detail


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


def test_scheduled_must_deliver_has_queued_green_and_not_queued_red(monkeypatch):
    class Client:
        fire_queued = 1

        def post(self, path, *, json):
            if path == "/v1/proactive/scheduled/actions":
                return _Response(200, {
                    "results": [{"status": "scheduled", "timer_id": "timer-1"}],
                })
            if path == "/v1/proactive/scheduled/fire":
                return _Response(200, {
                    "queued": self.fire_queued,
                    "results": [{
                        "status": "fired",
                        "timer_id": "timer-1",
                        "wake_id": "wake-1",
                    }],
                    "jobs": [{"job_id": "pj-1", "wake_id": "wake-1"}],
                })
            raise AssertionError(path)

    client = Client()
    monkeypatch.setattr(proactive_probe, "_install_quality_identity", lambda _c: None)
    monkeypatch.setattr(proactive_probe, "_save_settings", lambda _c, _patch: {})
    monkeypatch.setattr(proactive_probe.time, "time", lambda: 10.0)
    monkeypatch.setattr(
        proactive_probe,
        "_wait_for_proactive_reply",
        lambda _c, job_id, _since: {
            "role": "agent", "id": "scheduled-reply", "proactive_job_id": job_id,
        },
    )
    monkeypatch.setattr(
        proactive_probe,
        "_decrypt",
        lambda *_a, **_kw: "到时间了，这是你要的提醒。",
    )

    detail = proactive_probe._case_scheduled_must_deliver(client)
    assert "one correlated decryptable reply" in detail

    client.fire_queued = 0
    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._case_scheduled_must_deliver(client)
    assert exc.value.result == "PRODUCT_FAIL"
    assert "did not fire exactly once" in exc.value.detail


class _PriorityClient:
    def __init__(self):
        self.posts = []

    def post(self, _path, **_kwargs):
        self.posts.append((_path, _kwargs.get("json")))
        return _Response(200, {})
