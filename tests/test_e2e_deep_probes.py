from __future__ import annotations

import httpx
import pytest

from tools.e2e import perception_probe, proactive_probe


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
    monkeypatch.setattr(proactive_probe, "_case_wake_coalescing", lambda _c: "coalesced")
    monkeypatch.setattr(proactive_probe, "_case_stale_wake_expiry", lambda _c: "expired")
    monkeypatch.setattr(proactive_probe, "_case_dream_latest_only", lambda _c: "latest")
    monkeypatch.setattr(proactive_probe, "_case_self_wake_min_lead", lambda _c: "clamped")

    provider_only = proactive_probe.run_proactive_probe(object(), {"run_invariants": False})
    with_invariants = proactive_probe.run_proactive_probe(object(), {"run_invariants": True})

    assert provider_only == {
        "area": "proactive",
        "cases": [
            {"name": "user_turn_priority", "result": "PASS", "detail": "priority"},
            {"name": "proactive_message_quality", "result": "PASS", "detail": "quality"},
        ],
    }
    assert len(with_invariants["cases"]) == 8
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


def test_user_turn_priority_does_not_require_injection_like_echo(monkeypatch):
    rows = [
        {"role": "user", "id": "old-user", "reply_message_id": "old-reply", "ts": 11},
        {"role": "agent", "id": "old-reply", "ts": 12},
        {"role": "user", "id": "current-user", "reply_message_id": "current-reply", "ts": 13},
        {"role": "agent", "id": "current-reply", "reply_to_message_id": "current-user", "ts": 14},
    ]
    sent_text = []

    monkeypatch.setattr(proactive_probe, "_install_quality_identity", lambda _c: None)
    monkeypatch.setattr(proactive_probe, "_save_settings", lambda _c, _patch: {})
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

    detail = proactive_probe._case_user_turn_priority(_PriorityClient())

    assert "correlated user reply arrived" in detail
    assert "只回复" not in sent_text[0]


def test_user_turn_priority_rejects_uncorrelated_wake_before_reply(monkeypatch):
    rows = [
        {"role": "agent", "id": "wake-output", "ts": 12},
        {"role": "user", "id": "current-user", "reply_message_id": "current-reply", "ts": 13},
        {"role": "agent", "id": "current-reply", "reply_to_message_id": "current-user", "ts": 14},
    ]
    monkeypatch.setattr(proactive_probe, "_install_quality_identity", lambda _c: None)
    monkeypatch.setattr(proactive_probe, "_save_settings", lambda _c, _patch: {})
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

    with pytest.raises(proactive_probe._ProbeIssue) as exc:
        proactive_probe._case_user_turn_priority(_PriorityClient())

    assert exc.value.result == "PRODUCT_FAIL"


class _PriorityClient:
    def post(self, _path, **_kwargs):
        return _Response(200, {})
