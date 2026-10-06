"""Real extraction + provider retries must reach the plaintext, content-free ledger."""
import asyncio
import json

import httpx
import pytest

import db
import provider_client
from model_api_runtime.v2 import extraction, jobs_store, worker
from test_v2_extraction_lanes import _Recorder, _deps, _seed_v2


@pytest.mark.parametrize("lane", ["capture", "dream"])
@pytest.mark.parametrize("failure", ["http", "read", "wire_deadline", "success", "recovered"])
def test_extraction_wires_reach_ledger_without_private_body(monkeypatch, lane, failure):
    uid = f"u_ledger_{lane}_{failure}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job(f"ledger-{uid}", lanes={lane})
    assert job["id"] == job_id
    cfg = provider_client.ProviderConfig(
        provider="openai_compatible", model="ledger-test", api_key="PRIVATE_API_KEY",
        base_url="https://relay.example/v1", capture_attempt_trace=True)
    calls = []
    timeouts = []

    async def handler(request):
        calls.append(request)
        timeouts.append(request.extensions["timeout"]["read"])
        if failure == "read":
            raise httpx.ReadTimeout("PRIVATE_ERROR_BODY", request=request)
        if failure == "wire_deadline":
            await asyncio.sleep(10)
        if failure == "http" or (failure == "recovered" and len(calls) == 1):
            return httpx.Response(503, json={"error": "PRIVATE_ERROR_BODY"})
        reply = json.dumps({"cards" if lane == "capture" else "consolidations": []})
        return httpx.Response(200, json={
            "choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 123, "completion_tokens": 7}})

    monkeypatch.setattr(provider_client, "_shared_async_client",
                        httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(provider_client, "_reliable_retry_delay_sec", lambda *a, **k: 0)
    monkeypatch.setattr(extraction, "WIRE_DEADLINE_SEC", 0.01)
    monkeypatch.setattr(extraction, "DREAM_WIRE_DEADLINE_SEC", 0.02)
    recorder = _Recorder()
    recorder.user_id = uid
    recorder.job_id = job_id
    setattr(recorder, worker._LEDGER_LANE_ATTR, lane)

    async def run():
        await worker._record_trajectory(recorder, "provider_config_resolved",
                                        worker._safe_provider_metadata(cfg))
        return await worker.process_job(job, _deps(), provider_config=cfg,
                                       api_key=None, runtime_token="rt",
                                       trajectory_recorder=recorder)

    assert asyncio.run(run()) == ("completed" if failure in {"success", "recovered"} else "failed")
    assert timeouts == [0.02 if lane == "dream" else 90.0] * len(calls)
    rows = db.log_read_all(uid, "provider_attempts")
    assert len(rows) == 1
    row = rows[0]
    assert row["parent_message_id"] == f"v2job:{job_id}"
    assert (row["lane"], row["provider"], row["model"]) == (lane, cfg.provider, cfg.model)
    count = 1 if failure == "success" else 2 if failure == "recovered" else 3
    assert len(calls) == row["wire_attempt_count"] == row["outer_attempt_count"] == count
    assert [w["outer_attempt"] for w in row["wire_attempts"]] == list(range(1, count + 1))
    for index, wire in enumerate(row["wire_attempts"]):
        assert wire["status_code"] == (503 if failure == "http" or (failure == "recovered" and index == 0)
                                       else 200 if failure in {"success", "recovered"} else None)
        assert wire["timeout_kind"] == (failure if failure in {"read", "wire_deadline"} else "none")
        assert wire["dur_ms"] >= 0
        assert "wire" not in wire
    text = json.dumps(row, ensure_ascii=False)
    for secret in ("PRIVATE_ERROR_BODY", "PRIVATE_API_KEY", "我换工作了", "想看红叶", "relay.example"):
        assert secret not in text
    assert any(kind == "provider_request" for kind, _ in recorder.events)


def test_wire_metadata_rejects_open_error_and_timeout_text():
    import provider_attempt_metadata
    data = provider_attempt_metadata.project({"attempts": [{
        "kind": "http_attempt", "error_class": "PRIVATE_ERROR_BODY",
        "timeout_kind": "PRIVATE_ERROR_BODY", "duration_ms": float("nan"),
        "status": True, "wire": {"payload": "PRIVATE_PROMPT"},
    }]})
    wire = data["wire_attempts"][0]
    assert wire["error_class"] == wire["timeout_kind"] == "unknown"
    assert wire["dur_ms"] is wire["status_code"] is None
    assert "PRIVATE" not in json.dumps(data)
    assert provider_attempt_metadata.project(None) is None


@pytest.mark.parametrize("kind,cls", [
    ("connect", httpx.ConnectTimeout), ("read", httpx.ReadTimeout),
    ("write", httpx.WriteTimeout), ("pool", httpx.PoolTimeout),
    ("unknown", TimeoutError), ("none", ValueError),
])
def test_timeout_category_uses_exception_type_through_wrapper(kind, cls):
    error = provider_client.ProviderError("PRIVATE_ERROR_BODY")
    error.__cause__ = cls("wire_deadline read timeout arbitrary untrusted text")
    assert provider_client._wire_timeout_kind(error) == kind


@pytest.mark.parametrize("lane,expected", [("capture", 90.0), ("dream", 180.0)])
def test_lane_budgets_reach_real_provider_boundary(monkeypatch, lane, expected):
    """Real worker + extract; catch both phase and wire arguments independently."""
    uid = f"u_budget_wire_{lane}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job(f"budget-{uid}", lanes={lane})
    assert job["id"] == job_id
    seen = []

    async def reliable(*_args, **kwargs):
        seen.append(kwargs)
        return {"reply": json.dumps({"cards" if lane == "capture" else "consolidations": []}),
                "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", reliable)
    cfg = provider_client.ProviderConfig(provider="openai_compatible", model="budget-test",
                                         api_key="test", base_url="https://relay.example/v1")
    assert asyncio.run(worker.process_job(job, _deps(), provider_config=cfg,
                                         api_key=None, runtime_token="rt")) == "completed"
    assert len(seen) == 1
    assert seen[0]["timeout"] == expected
    assert seen[0]["wire_deadline_sec"] == expected
    assert callable(seen[0]["progress_cb"])


@pytest.mark.parametrize("lane,delay,success", [
    ("dream", 0.4, True), ("dream", 0.8, False), ("capture", 0.4, False),
])
def test_lane_wire_deadline_cancels_slow_mock_transport(monkeypatch, lane, delay, success):
    """Scale 90/180 by 1/300; the real wrapper cancels the real MockTransport.

    Constants/forwarding are anchored separately at 90/180. No paid wire or
    minute-long sleep is needed to prove the 90<t<180 and t>180 boundaries.
    """
    uid = f"u_deadline_{lane}_{int(delay * 100)}"
    _seed_v2(uid)
    job_id, _ = jobs_store.enqueue_job(uid, lane)
    job = jobs_store.claim_next_job(f"deadline-{uid}", lanes={lane})
    assert job["id"] == job_id
    monkeypatch.setattr(extraction, "WIRE_DEADLINE_SEC", 0.3)
    monkeypatch.setattr(extraction, "DREAM_WIRE_DEADLINE_SEC", 0.6)
    monkeypatch.setattr(provider_client, "_reliable_retry_delay_sec", lambda *_a, **_k: 0)
    cancelled = []

    async def handler(_request):
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return httpx.Response(200, json={"choices": [{"message": {
            "content": json.dumps({"cards" if lane == "capture" else "consolidations": []})},
            "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    cfg = provider_client.ProviderConfig(provider="openai_compatible", model="deadline-test",
                                         api_key="test", base_url="https://relay.example/v1",
                                         capture_attempt_trace=True)
    recorder = _Recorder()
    recorder.user_id, recorder.job_id = uid, job_id
    setattr(recorder, worker._LEDGER_LANE_ATTR, lane)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setattr(provider_client, "_shared_async_client", client)
            await worker._record_trajectory(recorder, "provider_config", {"provider": cfg.provider})
            return await worker.process_job(job, _deps(), provider_config=cfg, api_key=None,
                                            runtime_token="rt", trajectory_recorder=recorder)

    assert asyncio.run(run()) == ("completed" if success else "failed")
    assert len(cancelled) == (0 if success else 3)
    rows = db.log_read_all(uid, "provider_attempts")
    assert len(rows) == 1
    wires = rows[0]["wire_attempts"]
    assert len(wires) == (1 if success else 3)
    assert {w["timeout_kind"] for w in wires} == ({"none"} if success else {"wire_deadline"})
