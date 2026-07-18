from __future__ import annotations

import base64
import json
import stat
from pathlib import Path

import pytest

from qa import cot_delivery_probe as probe
from qa import validate_cot_receipt as receipt_validator
from tools.provider_smoke.client import Session, SmokeError


def _trace(**overrides):
    value = {
        "dropped": False,
        "model_call_count": 1,
        "agent_reply_count": 1,
        "chat_response_count": 1,
        "chat_response_match_count": 1,
        "model_thinking_present": True,
        "model_thinking_len": 42,
        "model_thinking_source": "pi_thinking",
        "agent_reply_thinking_kind": "provider_reasoning_summary",
        "model_duration_ms": 800.0,
        "provider_api_duration_ms": 700.0,
    }
    value.update(overrides)
    return value


def _reply(**overrides):
    value = {
        "thinking_present": True,
        "thinking": "safe display summary",
        "thinking_kind": "provider_reasoning_summary",
        "thinking_source": "pi_thinking",
        "thinking_model": "gemini-2.5-flash",
        "thinking_native": True,
    }
    value.update(overrides)
    return value


def test_positive_trace_and_envelope_pass_delivery_only():
    assert probe.classify_delivery(_trace(), _reply()) == ("PASS", "NONE")


def test_positive_trace_and_missing_envelope_catches_delivery_regression():
    assert probe.classify_delivery(
        _trace(), _reply(thinking_present=False, thinking="")
    ) == ("FAIL", "THINKING_ENVELOPE_NOT_DELIVERED")


def test_missing_second_parse_kind_identifies_c6d_regression():
    assert probe.classify_delivery(
        _trace(agent_reply_thinking_kind=""),
        _reply(thinking_present=False, thinking=""),
    ) == ("FAIL", "DOWNSTREAM_PARSE_DROPPED_REASONING")


def test_positive_trace_and_unreadable_envelope_fail():
    assert probe.classify_delivery(
        _trace(), None, decrypt_error=True
    ) == ("FAIL", "THINKING_ENVELOPE_UNREADABLE")


@pytest.mark.parametrize(
    "changes",
    [
        {"model_call_count": 0, "model_thinking_present": False},
        {"model_call_count": 2},
        {"dropped": True},
        {"chat_response_match_count": 0},
    ],
)
def test_missing_or_ambiguous_positive_trace_is_unverified(changes):
    status, code = probe.classify_delivery(_trace(**changes), _reply())
    assert status == "UNVERIFIED"
    assert code in {"TRACE_AMBIGUOUS", "MODEL_REASONING_NOT_OBSERVED"}


def test_correlate_trace_binds_exact_turn_and_reply():
    events = [
        {
            "type": "agent.model.call.done",
            "trace_id": "turn-1",
            "status": "ok",
            "dur_ms": 812,
            "detail": {
                "thinking_present": True,
                "thinking_len": 37,
                "thinking_source": "pi_thinking",
                "api_ms": 701,
            },
        },
        {
            "type": "agent.reply",
            "trace_id": "turn-1",
            "detail": {"thinking_kind": "provider_reasoning_summary"},
        },
        {
            "type": "chat.response",
            "trace_id": "turn-1",
            "detail": {"msg_id": "reply-1"},
        },
        {
            "type": "chat.response",
            "trace_id": "other-turn",
            "detail": {"msg_id": "reply-other"},
        },
    ]

    result = probe.correlate_trace(
        events,
        trace_id="turn-1",
        reply_message_id="reply-1",
        turn_started_at=100,
    )

    assert result["model_call_count"] == 1
    assert result["agent_reply_count"] == 1
    assert result["chat_response_count"] == 1
    assert result["chat_response_match_count"] == 1
    assert result["model_thinking_present"] is True
    assert result["model_thinking_len"] == 37
    assert result["model_duration_ms"] == 812.0
    assert result["provider_api_duration_ms"] == 701.0
    assert result["token_metadata_status"] == "UNVERIFIED"
    assert result["reasoning_token_count"] is None


@pytest.mark.parametrize(
    "usage",
    (
        {"output_tokens_details": {"reasoning_tokens": 192}},
        {"completion_tokens_details": {"reasoning_tokens": 193}},
        {"reasoning_tokens": 194},
        {"thinking_tokens": 195},
    ),
)
def test_correlate_trace_extracts_only_explicit_reasoning_tokens(usage):
    events = [
        {
            "type": "agent.model.call.done",
            "trace_id": "turn-1",
            "status": "ok",
            "detail": {
                "thinking_present": True,
                "thinking_len": 20,
                "thinking_source": "provider",
                "usage": usage,
            },
        },
        {
            "type": "agent.reply",
            "trace_id": "turn-1",
            "detail": {"thinking_kind": "reasoning"},
        },
        {
            "type": "chat.response",
            "trace_id": "turn-1",
            "detail": {"msg_id": "reply-1"},
        },
    ]

    result = probe.correlate_trace(
        events,
        trace_id="turn-1",
        reply_message_id="reply-1",
        turn_started_at=100,
    )

    assert result["token_metadata_status"] == "PRESENT"
    assert result["reasoning_token_count"] in {192, 193, 194, 195}


@pytest.mark.parametrize(
    "usage",
    (
        {"input_tokens": 12, "output_tokens": 34, "total_tokens": 46},
        {
            "reasoning_tokens": 10,
            "output_tokens_details": {"reasoning_tokens": 11},
        },
        {"reasoning_tokens": True},
        {"reasoning_tokens": -1},
    ),
)
def test_correlate_trace_does_not_infer_or_accept_ambiguous_tokens(usage):
    detail = {"usage": usage}

    assert probe._explicit_reasoning_token_count(detail) is None


@pytest.mark.parametrize(
    ("detail", "expected"),
    (
        ({"usage": {"thoughtsTokenCount": 196}}, 196),
        ({"usage_metadata": {"thoughts_token_count": 197}}, 197),
        ({"usageMetadata": {"thoughtsTokenCount": 198}}, 198),
    ),
)
def test_explicit_reasoning_tokens_accept_gemini_aliases(detail, expected):
    assert probe._explicit_reasoning_token_count(detail) == expected


def test_gemini_total_tokens_are_not_reasoning_evidence():
    detail = {
        "usageMetadata": {
            "promptTokenCount": 12,
            "candidatesTokenCount": 34,
            "totalTokenCount": 46,
        }
    }

    assert probe._explicit_reasoning_token_count(detail) is None


def _completed_trace_events(*, duplicate_response: bool = False):
    events = [
        {
            "type": "agent.model.call.done",
            "trace_id": "turn-1",
            "status": "ok",
            "detail": {
                "thinking_present": True,
                "thinking_len": 20,
                "thinking_source": "provider",
            },
        },
        {
            "type": "agent.reply",
            "trace_id": "turn-1",
            "detail": {"thinking_kind": "reasoning"},
        },
        {
            "type": "chat.response",
            "trace_id": "turn-1",
            "detail": {"msg_id": "reply-1"},
        },
    ]
    if duplicate_response:
        events.append(
            {
                "type": "chat.response",
                "trace_id": "turn-1",
                "detail": {"msg_id": "reply-2"},
            }
        )
    return events


def test_trace_poll_requires_two_completed_observations(monkeypatch):
    class FakeClient:
        calls = 0

        def read_trace(self, _session, *, limit, attempts, read_timeout):
            assert limit == 500
            assert attempts == 1
            assert 0 < read_timeout <= probe.TRACE_READ_TIMEOUT_CAP_SECONDS
            self.calls += 1
            return {"events": _completed_trace_events()}

    client = FakeClient()
    monkeypatch.setattr(probe, "TRACE_POLL_INTERVAL_SECONDS", 0.0)

    result = probe._poll_trace(
        client,
        object(),
        trace_id="turn-1",
        reply_message_id="reply-1",
        turn_started_at=100.0,
    )

    assert client.calls == 2
    assert result["chat_response_count"] == 1


def test_trace_poll_detects_duplicate_arriving_after_first_completion(monkeypatch):
    class FakeClient:
        calls = 0

        def read_trace(self, _session, *, limit, attempts, read_timeout):
            assert limit == 500
            assert attempts == 1
            assert 0 < read_timeout <= probe.TRACE_READ_TIMEOUT_CAP_SECONDS
            self.calls += 1
            return {
                "events": _completed_trace_events(
                    duplicate_response=self.calls >= 2
                )
            }

    client = FakeClient()
    monkeypatch.setattr(probe, "TRACE_POLL_INTERVAL_SECONDS", 0.0)

    result = probe._poll_trace(
        client,
        object(),
        trace_id="turn-1",
        reply_message_id="reply-1",
        turn_started_at=100.0,
    )

    assert client.calls == 2
    assert result["chat_response_count"] == 2
    assert probe.classify_delivery(result, _reply()) == (
        "UNVERIFIED",
        "TRACE_AMBIGUOUS",
    )


def test_trace_poll_caps_each_read_to_remaining_budget(monkeypatch):
    clock = iter((100.0, 100.0, 115.5, 115.5, 120.0))

    class FakeClient:
        read_timeouts: list[float] = []

        def read_trace(self, _session, *, limit, attempts, read_timeout):
            assert limit == 500
            assert attempts == 1
            self.read_timeouts.append(read_timeout)
            return {"events": []}

    monkeypatch.setattr(probe.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(probe.time, "sleep", lambda _seconds: None)

    client = FakeClient()
    result = probe._poll_trace(
        client,
        object(),
        trace_id="turn-1",
        reply_message_id="reply-1",
        turn_started_at=100.0,
    )

    assert result["model_call_count"] == 0
    assert client.read_timeouts == [10.0, 4.5]


def test_cot_probe_budget_finishes_before_parent_and_agent_deadlines():
    assert probe.COT_PROBE_WORST_CASE_SECONDS == 277.0
    assert (
        probe.COT_PROBE_WORST_CASE_SECONDS
        < probe.PARENT_COT_TIMEOUT_SECONDS
        < probe.AGENT_COT_WAIT_SECONDS
    )


def test_reply_poll_timeout_cannot_exceed_parent_budget(tmp_path):
    with pytest.raises(probe.CotProbeError, match="probe timeout is invalid"):
        probe.run_probe(
            tmp_path / "unused-manifest.json",
            tmp_path / "unused-receipt.json",
            nonce="nonce_123456",
            timeout_seconds=probe.MAX_REPLY_POLL_SECONDS + 0.1,
        )


def _manifest(tmp_path: Path) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "base_url": probe.LOCKED_BASE_URL,
                "profiles": [
                    {
                        "profile_id": "official-gemini",
                        "provision_status": "ready",
                        "provider": "gemini",
                        "configured_model": "gemini-2.5-flash",
                        "configured_base_url": "",
                        "reasoning_effort": "medium",
                        "user_id": "user-1",
                        "api_key": "feedling-user-secret",
                        "secret_key_b64": base64.b64encode(b"s" * 32).decode(),
                        "public_key_b64": base64.b64encode(b"p" * 32).decode(),
                    }
                ],
            }
        )
    )
    path.chmod(0o600)
    return path


def _configured_route(**overrides):
    config = {
        "configured": True,
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "base_url": "",
        "reasoning_effort": "medium",
    }
    config.update(overrides)
    return {"config": config}


def _manifest_profile() -> dict[str, object]:
    return {
        "provider": "gemini",
        "configured_model": "gemini-2.5-flash",
        "configured_base_url": "",
    }


def test_configured_effort_readback_requires_exact_live_route():
    session = Session(
        user_id="user-1",
        api_key="feedling-user-secret",
        sk=b"s" * 32,
        pk=b"p" * 32,
    )

    class FakeClient:
        def _req(self, method, path, *, api_key, attempts, read_timeout):
            assert (method, path) == ("GET", "/v1/model_api/get")
            assert api_key == "feedling-user-secret"
            assert (attempts, read_timeout) == (
                probe.CONFIG_READ_ATTEMPTS,
                probe.CONFIG_READ_TIMEOUT_SECONDS,
            )
            return 200, _configured_route()

    assert probe.read_configured_effort(
        FakeClient(), session, _manifest_profile()
    ) == ("medium", True, True)


@pytest.mark.parametrize(
    ("body", "effort", "route_matches"),
    [
        (_configured_route(reasoning_effort="off"), "off", True),
        (_configured_route(model="another-model"), "medium", False),
        (
            _configured_route(reasoning_effort="provider-default"),
            "unsupported",
            True,
        ),
    ],
)
def test_configured_effort_readback_attests_readable_values_and_route(
    body, effort, route_matches
):
    session = Session(
        user_id="user-1",
        api_key="feedling-user-secret",
        sk=b"s" * 32,
        pk=b"p" * 32,
    )

    class FakeClient:
        def _req(self, *_args, **_kwargs):
            return 200, body

    assert probe.read_configured_effort(
        FakeClient(), session, _manifest_profile()
    ) == (effort, True, route_matches)


@pytest.mark.parametrize(
    "response",
    [
        (503, {}),
        (200, {}),
        (200, {"config": []}),
        (200, _configured_route(configured=False)),
    ],
)
def test_configured_effort_readback_leaves_unavailable_values_unattested(response):
    session = Session(
        user_id="user-1",
        api_key="feedling-user-secret",
        sk=b"s" * 32,
        pk=b"p" * 32,
    )

    class FakeClient:
        def _req(self, *_args, **_kwargs):
            return response

    assert probe.read_configured_effort(
        FakeClient(), session, _manifest_profile()
    ) == ("unknown", False, False)


def test_configured_effort_readback_reports_transport_unavailable():
    session = Session(
        user_id="user-1",
        api_key="feedling-user-secret",
        sk=b"s" * 32,
        pk=b"p" * 32,
    )

    class FakeClient:
        def _req(self, *_args, **_kwargs):
            raise SmokeError("configuration", "unavailable")

    assert probe.read_configured_effort(
        FakeClient(), session, _manifest_profile()
    ) == ("unknown", False, False)


def test_manifest_loader_requires_private_one_row_assignment(tmp_path):
    manifest = _manifest(tmp_path)
    profile_id, base_url, session = probe.load_profile_session(
        manifest, "official-gemini"
    )
    assert profile_id == "official-gemini"
    assert base_url == probe.LOCKED_BASE_URL
    assert isinstance(session, Session)
    assert session.user_id == "user-1"

    manifest.chmod(0o644)
    with pytest.raises(probe.CotProbeError, match="unsafe"):
        probe.load_profile_session(manifest, "official-gemini")


def test_manifest_loader_requires_locked_requested_effort(tmp_path):
    manifest = _manifest(tmp_path)
    document = json.loads(manifest.read_text())
    document["profiles"][0]["reasoning_effort"] = "low"
    manifest.write_text(json.dumps(document))
    manifest.chmod(0o600)

    with pytest.raises(probe.CotProbeError, match="not ready"):
        probe.load_profile_context(manifest, "official-gemini")


def test_private_receipt_contains_no_raw_evidence(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    output = parent / "receipt.json"
    receipt = {
        "status": "PASS",
        "failure_code": "NONE",
        "release_qualified": False,
        "token_metadata_status": "UNVERIFIED",
        "reasoning_token_count": None,
        "raw_reply_stored": False,
        "raw_thinking_stored": False,
        "raw_trace_stored": False,
    }

    probe._write_receipt(output, receipt)

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    raw = output.read_text()
    assert "safe display summary" not in raw
    assert "thinking_body_ct" not in raw
    assert "raw_trace" not in raw.replace('"raw_trace_stored":false', "")
    assert json.loads(raw)["release_qualified"] is False


def test_run_probe_projects_reasoning_delivery_without_raw_text(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    parent = tmp_path / "receipt-root"
    parent.mkdir(mode=0o700)
    output = parent / "cot.json"

    class FakeClient:
        def _req(self, method, path, *, api_key, attempts, read_timeout):
            assert (method, path) == ("GET", "/v1/model_api/get")
            assert api_key == "feedling-user-secret"
            assert (attempts, read_timeout) == (2, 15)
            return 200, _configured_route()

        def send(self, session, text, *, read_timeout):
            assert session.user_id == "user-1"
            assert "17 multiplied by 19" in text
            assert read_timeout == probe.CHAT_SEND_TIMEOUT_SECONDS
            return {"user_message": {"id": "turn-1", "ts": 100.0}}

        def poll_reply_record(
            self,
            session,
            user_message_ts,
            timeout_seconds,
            *,
            include_thinking,
            user_message_id,
        ):
            assert include_thinking is False
            assert user_message_id == "turn-1"
            return {"reply": "323", "message": {"id": "reply-1"}}

    monkeypatch.setattr(probe, "decrypt_reply_record", lambda *_args: _reply())
    monkeypatch.setattr(probe, "_poll_trace", lambda *_args, **_kwargs: _trace())

    receipt = probe.run_probe(
        manifest,
        output,
        nonce="nonce_123456",
        expected_profile_id="official-gemini",
        client=FakeClient(),
    )

    assert receipt["status"] == "PASS"
    assert receipt["requested_effort"] == "medium"
    assert receipt["configured_effort"] == "medium"
    assert receipt["configured_effort_attested"] is True
    assert receipt["configured_route_matches_manifest"] is True
    assert receipt["effective_effort"] == "unknown"
    assert receipt["effective_effort_attested"] is False
    assert receipt["request_id"] == receipt["turn_id"] == receipt["trace_id"]
    assert receipt["reasoning_event_count"] == 1
    assert receipt["metadata_present"] is True
    assert receipt["user_visible_disclosure_present"] is True
    assert receipt["token_metadata_status"] == "UNVERIFIED"
    raw = output.read_text()
    assert "safe display summary" not in raw
    assert "feedling-user-secret" not in raw


def test_run_probe_preserves_reply_ids_when_trace_is_unavailable(tmp_path, monkeypatch):
    manifest = _manifest(tmp_path)
    parent = tmp_path / "receipt-root"
    parent.mkdir(mode=0o700)
    output = parent / "cot.json"

    class FakeClient:
        def _req(self, *_args, **_kwargs):
            return 200, _configured_route()

        def send(self, _session, _text, *, read_timeout):
            assert read_timeout == probe.CHAT_SEND_TIMEOUT_SECONDS
            return {"user_message": {"id": "turn-1", "ts": 100.0}}

        def poll_reply_record(self, *_args, **_kwargs):
            return {"reply": "323", "message": {"id": "reply-1"}}

    monkeypatch.setattr(probe, "decrypt_reply_record", lambda *_args: _reply())

    def unavailable(*_args, **_kwargs):
        raise SmokeError("trace", "unavailable")

    monkeypatch.setattr(probe, "_poll_trace", unavailable)

    receipt = probe.run_probe(
        manifest,
        output,
        nonce="nonce_123456",
        expected_profile_id="official-gemini",
        client=FakeClient(),
    )

    assert receipt["status"] == "UNVERIFIED"
    assert receipt["failure_code"] == "TRACE_UNAVAILABLE"
    assert receipt["request_id"] == "turn-1"
    assert receipt["reply_message_id"] == "reply-1"
    assert receipt["final_answer_correct"] is True


@pytest.mark.parametrize(
    ("readback", "expected_effort", "attested", "route_matches"),
    [
        (
            (200, _configured_route(reasoning_effort="off")),
            "off",
            True,
            True,
        ),
        (
            (200, _configured_route(model="another-model")),
            "medium",
            True,
            False,
        ),
        ((200, _configured_route(configured=False)), "unknown", False, False),
        ((200, {"config": []}), "unknown", False, False),
        (SmokeError("configuration", "unavailable"), "unknown", False, False),
    ],
)
def test_run_probe_always_tests_delivery_regardless_of_configuration_readback(
    tmp_path,
    monkeypatch,
    readback,
    expected_effort,
    attested,
    route_matches,
):
    manifest = _manifest(tmp_path)
    parent = tmp_path / "receipt-root"
    parent.mkdir(mode=0o700)
    output = parent / "cot.json"

    class FakeClient:
        send_calls = 0

        def _req(self, *_args, **_kwargs):
            if isinstance(readback, Exception):
                raise readback
            return readback

        def send(self, _session, _text, *, read_timeout):
            assert read_timeout == probe.CHAT_SEND_TIMEOUT_SECONDS
            self.send_calls += 1
            return {"user_message": {"id": "turn-1", "ts": 100.0}}

        def poll_reply_record(self, *_args, **_kwargs):
            return {"reply": "323", "message": {"id": "reply-1"}}

    monkeypatch.setattr(probe, "decrypt_reply_record", lambda *_args: _reply())
    monkeypatch.setattr(probe, "_poll_trace", lambda *_args, **_kwargs: _trace())
    client = FakeClient()

    receipt = probe.run_probe(
        manifest,
        output,
        nonce="nonce_123456",
        expected_profile_id="official-gemini",
        client=client,
    )

    assert client.send_calls == 1
    assert receipt["status"] == "PASS"
    assert receipt["failure_code"] == "NONE"
    assert receipt["requested_effort"] == "medium"
    assert receipt["configured_effort"] == expected_effort
    assert receipt["configured_effort_attested"] is attested
    assert receipt["configured_route_matches_manifest"] is route_matches
    assert receipt["effective_effort"] == "unknown"
    assert receipt["effective_effort_attested"] is False
    assert receipt["request_id"] == "turn-1"
    assert receipt["model_call_count"] == 1

    parsed, _ = receipt_validator.validate_cot_receipt(
        output, "official-gemini"
    )
    assert parsed == receipt
