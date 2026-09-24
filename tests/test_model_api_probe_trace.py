"""Persisted setup/manual-test probe diagnostics: HTTP status without raw errors."""
import base64
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import db  # noqa: E402
import debug_trace  # noqa: E402
import provider_client  # noqa: E402
from admin import data_track  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from hosted import config_store, setup_core  # noqa: E402


@pytest.fixture
def probe_user(client, monkeypatch):
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE", "1")
    monkeypatch.setenv("FEEDLING_V1_FLOW_TRACE_DEFAULT", "1")
    monkeypatch.setattr(setup_core, "_kick_setup_main_vision_test", lambda *a, **k: None)
    monkeypatch.setattr(
        core_envelope, "_build_shared_envelope_for_store",
        lambda *a, **k: ({"v": 1, "body_ct": "ct", "nonce": "n"}, None),
    )
    monkeypatch.setattr(
        core_envelope, "decrypt_provider_key_envelope", lambda *a, **k: b"sk-test"
    )
    response = client.post("/v1/users/register", json={
        "public_key": base64.b64encode(os.urandom(32)).decode("ascii"),
        "archive_language": "en",
    })
    assert response.status_code == 201
    user = response.get_json()
    return user["user_id"], {"X-API-Key": user["api_key"]}


def _events(client, headers):
    response = client.get("/v1/debug/trace?subsystem=model_api", headers=headers)
    assert response.status_code == 200
    return [event for event in response.get_json()["events"]
            if event["type"].startswith("model_api.provider_probe.")]


@pytest.mark.parametrize("operation", ["setup", "test"])
@pytest.mark.parametrize("status_code", [401, 402, 403, None])
def test_probe_failure_status_survives_storage_and_readers_without_raw_error(
    client, probe_user, monkeypatch, operation, status_code,
):
    uid, headers = probe_user
    payload = {
        "provider": "anthropic", "model": "claude-sonnet-4-5", "api_key": "sk-test",
    }
    if operation == "test":
        monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {})
        response = client.post("/v1/model_api/setup", headers=headers, json=payload)
        assert response.status_code == 200, response.get_json()
        # Drain the successful setup before isolating the manual-test trace.
        _events(client, headers)
        debug_trace.clear_trace(SimpleNamespace(user_id=uid))
        monkeypatch.setattr(config_store, "prepare_model_api_delete", lambda store: None)

    secrets = ("PRIVATE_EXCEPTION_KEY", "PRIVATE_RESPONSE_DETAIL", "PRIVATE_RAW_BODY")

    def fail(_config):
        raise provider_client.ProviderError(
            secrets[0], status_code=status_code,
            response_detail=secrets[1], raw_response_body=secrets[2],
        )

    monkeypatch.setattr(provider_client, "test_provider_key", fail)
    response = client.post(f"/v1/model_api/{operation}", headers=headers, json=payload)
    assert response.status_code == 400
    assert response.get_json()["error"] == "provider_test_failed"
    events = _events(client, headers)
    expected_phases = {"started", "finished"}
    if operation == "test":
        expected_phases.add("runtime_fenced")
    assert {event["detail"]["phase"] for event in events} == expected_phases
    assert len({event["trace_id"] for event in events}) == 1
    persisted = db.query_trace_events(user_id=uid, subsystem="model_api")
    for event in events:
        detail = event["detail"]
        expected = status_code if detail["phase"] == "finished" else None
        assert detail["status_code"] == expected
        assert data_track._debug_event_public_json(event)["detail"]["status_code"] == expected
        assert detail["operation"] == operation
        assert set(detail) <= {
            "operation", "phase", "provider", "model", "status_code", "error_class",
            db.TRACE_OUTCOME_PROVENANCE_FIELD,
        }
        if detail["phase"] == "finished":
            assert event["status"] == "error"
            assert event["outcome_class"] == "operational_failure"
    for secret in secrets:
        assert secret not in json.dumps(events)
        assert secret not in json.dumps(persisted)


@pytest.mark.parametrize("value,expected", [
    (100, 100), (200, 200), (599, 599), (99, None), (600, None), (-401, None),
    (None, None), (True, None), (False, None), (401.0, None), (float("inf"), None),
    ("401", None), ("PRIVATE_STATUS_KEY", None), ({"status": 401}, None),
])
def test_probe_trace_status_is_a_closed_http_integer_at_persistence_boundary(
    client, probe_user, value, expected,
):
    uid, headers = probe_user
    setup_core._emit_model_api_probe_trace(
        SimpleNamespace(user_id=uid), probe_trace_id="model_api_probe:closed_status",
        operation="setup", provider="anthropic", model="claude-sonnet-4-5",
        phase="finished", status_code=value,
    )
    events = _events(client, headers)
    assert len(events) == 1
    actual = events[0]["detail"]["status_code"]
    assert actual == expected
    assert actual is None or type(actual) is int
    assert "PRIVATE_STATUS_KEY" not in json.dumps(events)


def test_successful_probe_does_not_invent_an_http_status(client, probe_user, monkeypatch):
    uid, headers = probe_user
    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {"usage": {}})
    setup_core._test_provider_key_observed(
        SimpleNamespace(user_id=uid),
        provider_client.ProviderConfig("anthropic", "claude-sonnet-4-5", "sk-test"),
        operation="setup",
    )
    events = _events(client, headers)
    assert {event["detail"]["phase"] for event in events} == {"started", "finished"}
    assert all(event["detail"]["status_code"] is None for event in events)
