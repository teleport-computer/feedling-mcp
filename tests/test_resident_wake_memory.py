"""T582: real wake assembly, enclave HTTP and CLI payload/terminal accounting."""

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")
os.environ.setdefault("AGENT_MODE", "http")
os.environ.setdefault("AGENT_HTTP_URL", "http://localhost:8080/chat")

from tools import chat_resident_consumer as consumer  # noqa: E402


CARDS = [
    {"id": "mem_lamp", "summary": "露营灯保修码 NP-4286"},
    {"id": "mem_trip", "summary": "九月去湖边露营"},
]
LINES = [f"- (id={card['id']}) {card['summary']} · 可能相关" for card in CARDS]
HISTORY = [
    {"id": "u_old", "seq": 3, "role": "user", "content": "以前的消息", "ts": 1},
    {"id": "u_latest", "seq": 10, "role": "user", "content": "露营灯的事情",
     "ts": 2, "quoted_memories": [{"id": "mem_lamp", "text": "过去引用的卡"}]},
    {"id": "a_latest", "seq": 11, "role": "assistant", "content": "收到啦", "ts": 3},
]


@pytest.fixture
def wake(monkeypatch, tmp_path):
    """Only external services/processes and unrelated wake inputs are replaced."""
    events, payloads, requests, statuses = [], [], [], []
    config = {"history": HISTORY, "selection": "ok", "cards": CARDS}
    monkeypatch.setattr(consumer, "AGENT_MODE", "cli")
    monkeypatch.setattr(consumer, "_resolve_cli_executable", lambda cmd: cmd)
    monkeypatch.setattr(consumer, "_agent_session_id_cache", {})
    monkeypatch.setattr(consumer, "_agent_session_meta_cache", {})
    monkeypatch.setattr(consumer, "AGENT_SESSION_FILE_TEMPLATE", str(tmp_path / "session.json"))
    monkeypatch.setattr(consumer, "_agent_cli_cwd", lambda: str(tmp_path))
    monkeypatch.setattr(consumer, "_user_mcp_child_env", lambda cmd: {})
    monkeypatch.setattr(consumer, "FEEDLING_ENCLAVE_URL", "https://enclave.test")
    monkeypatch.setattr(consumer, "ENCLAVE_FETCH_MAX_ATTEMPTS", 1)
    monkeypatch.setattr(consumer, "_whoami_cache", {})
    monkeypatch.setattr(consumer, "_RECALL_TURN_STATE", {})
    monkeypatch.setattr(consumer, "_turn_ledger_path", None)
    monkeypatch.setattr(consumer, "_mark_seen", lambda key: True)
    monkeypatch.setattr(consumer, "claim_proactive_job", lambda key: True)
    monkeypatch.setattr(consumer, "_plan_wake_coalescing", lambda jobs: None)
    monkeypatch.setattr(consumer, "_proactive_backing_off", lambda: False)
    monkeypatch.setattr(consumer, "_provider_payment_cooling_down", lambda: False)
    monkeypatch.setattr(consumer, "_screen_context_for_frame_ids", lambda ids: ("", [], []))
    monkeypatch.setattr(consumer, "_proactive_perception_digest", lambda: ({}, [], {}))
    monkeypatch.setattr(consumer, "_worldbook_context_for_wake", lambda job: [])
    monkeypatch.setattr(consumer, "_call_with_resident_busy_poll", lambda fn, **kw: fn())
    monkeypatch.setattr(consumer, "_proactive_chat_collision", lambda: False)
    monkeypatch.setattr(consumer, "post_reply", lambda *a, **kw: {"id": "reply"})
    monkeypatch.setattr(consumer, "update_proactive_job_status",
                        lambda *args, **kw: statuses.append((args, kw)))
    monkeypatch.setattr(consumer, "_emit_debug_trace",
                        lambda subsystem, event, **kw: events.append({"type": event, **kw}))
    for name in ("_note_proactive_turn_ran", "_note_agent_turn_success",
                 "_clear_provider_payment_cooldown", "_clear_proactive_failure"):
        monkeypatch.setattr(consumer, name, lambda *a, **kw: None)

    def enclave(request):
        requests.append(request)
        if "before_seq" not in request.url.params:
            if config.get("history_failed"):
                raise httpx.ReadTimeout("history unavailable", request=request)
            # These broad-page picks must never substitute for per-anchor picks.
            return httpx.Response(200, json={"messages": config["history"],
                "context_memories": [{"id": "poll_only", "summary": "不要复用"}]})
        if config["selection"] == "transport_failed":
            raise httpx.ReadTimeout("selection unavailable", request=request)
        if config["selection"] == "http_failed":
            return httpx.Response(503)
        page = [m for m in config["history"] if m["seq"] < int(request.url.params["before_seq"])]
        return httpx.Response(200, json={
            "messages": [] if config["selection"] == "mismatched" else page,
            "context_memories": config["cards"],
            "context_memory_log": {"mode": "failed" if config["selection"] == "mode_failed" else "ok",
                                   "counts": {"candidate_pool": 17}},
        })

    def run_cli(cmd, kwargs, **extra):
        payloads.append({"text": kwargs.get("input") or " ".join(cmd), "env": kwargs["env"]})
        reply = json.dumps({"messages": ["记得带上露营灯呀。"]}, ensure_ascii=False)
        driver = Path(cmd[0]).name
        if driver == "pi":
            output = {"type": "message_end", "message": {"role": "assistant",
                      "content": [{"type": "text", "text": reply}], "stopReason": "stop"}}
        elif driver == "codex":
            output = {"type": "item.completed", "item": {"type": "agent_message", "text": reply}}
        else:
            output = {"type": "result", "subtype": "success", "result": reply}
        return subprocess.CompletedProcess(cmd, 0, json.dumps(output, ensure_ascii=False), "")

    monkeypatch.setattr(consumer, "_run_cli_subprocess", run_cli)
    with httpx.Client(transport=httpx.MockTransport(enclave)) as client, httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    ) as backend:
        monkeypatch.setattr(consumer, "_ENCLAVE_CLIENT", client)
        monkeypatch.setattr(consumer, "_HTTP", backend)

        def run(driver="pi", trigger="heartbeat"):
            templates = {"pi": "pi --mode json", "claude": 'claude -p "{message}"',
                         "codex": 'codex exec --json "{message}"'}
            monkeypatch.setattr(consumer, "AGENT_CLI_CMD", templates[driver])
            consumer._process_proactive_jobs([{
                "job_id": "wake_test", "trace_id": "trace_wake_test", "ts": 20,
                "source": consumer.PROACTIVE_JOB_SOURCE, "trigger": trigger,
                "job_kind": trigger if trigger in {"screen_watch", "introduction"} else "proactive",
            }])
            return events, payloads, requests, statuses

        yield config, run
        consumer._recall_turn_reset()


@pytest.mark.parametrize("driver", ["pi", "claude", "codex"])
@pytest.mark.parametrize("trigger", ["heartbeat", "scheduled_wake", "screen_watch"])
def test_wake_memory_reaches_final_driver_and_recall_counts(wake, driver, trigger):
    _, run = wake
    events, payloads, requests, statuses = run(driver, trigger)
    assert len(payloads) == 1
    payload = payloads[0]["text"]
    for line in LINES:
        assert line in payload
    assert "poll_only" not in payload
    if "recent_chat_context:" in payload:
        assert payload.index(LINES[0]) < payload.index("recent_chat_context:")
    bounded = [r for r in requests if "before_seq" in r.url.params]
    assert len(bounded) == 1
    assert dict(bounded[0].url.params) == {
        "before_seq": "11", "limit": str(consumer.AUTO_MEMORY_TURN_PAGE),
        "context_trace": "1", "include_image_body": "false",
    }
    assert len(requests) == 2  # existing history + per-anchor selection, no extra history fetch
    applied = [e for e in events if e["type"] == "memory.context.applied"]
    completed = [e for e in events if e["type"] == "memory.recall.completed"]
    assert len(applied) == len(completed) == 1
    assert applied[0]["detail"]["rendered"] == applied[0]["detail"]["arrived"] == 2
    assert applied[0]["detail"]["driver"] == driver
    assert applied[0]["detail"]["channel"] == "stdin"
    detail = completed[0]["detail"]
    assert detail["runtime"] == "v1" and detail["lane"] == "wake"
    assert detail["counts"]["injected"] == detail["counts"]["selected"] == 2
    assert detail["candidate_pool"] == 17 and detail["quoted_memories"] == 0
    assert detail["injected_ids"] == [c["id"] for c in CARDS]
    assert applied[0]["trace_id"] == completed[0]["trace_id"] == "trace_wake_test"
    assert payloads[0]["env"]["FEEDLING_TRACE_ID"] == "trace_wake_test"
    assert any(e["type"] == "agent.model.call.done" for e in events)
    assert any(args[1] == "posted" for args, _ in statuses)
    # Memory observability stores IDs/counts; never card summaries or query text.
    memory_events = [e for e in events if e["type"] in
                     {"memory.context.applied", "memory.recall.completed", "context.auto_memory"}]
    assert all(c["summary"] not in json.dumps(memory_events, ensure_ascii=False) for c in CARDS)


@pytest.mark.parametrize("case", ["transport_failed", "http_failed", "mode_failed", "mismatched",
                                  "no_user", "history_failed", "introduction", "empty"])
def test_wake_unknown_selection_is_distinct_from_healthy_zero(wake, case):
    config, run = wake
    # Run a successful wake first: the second must not inherit its injected cards.
    events, payloads, requests, statuses = run()
    events.clear()
    payloads.clear()
    requests.clear()
    statuses.clear()
    if case == "no_user":
        config["history"] = [HISTORY[-1]]
    elif case == "history_failed":
        config["history_failed"] = True
    elif case == "empty":
        config["cards"] = []
    else:
        config["selection"] = case
    events, payloads, requests, statuses = run(trigger="introduction" if case == "introduction" else "heartbeat")
    assert len(payloads) == 1
    assert all(line not in payloads[0]["text"] for line in LINES)
    assert not any(e["type"] == "memory.context.applied" for e in events)
    completed = [e for e in events if e["type"] == "memory.recall.completed"]
    assert len(completed) == 1
    detail = completed[0]["detail"]
    assert detail["counts"]["injected"] == 0
    assert detail["counts"]["selected"] == (0 if case == "empty" else None)
    assert detail["candidate_pool"] == (17 if case == "empty" else None)
    assert ("selected" in detail["unknown"]) is (case != "empty")
    if case in {"no_user", "history_failed"}:
        assert not any("before_seq" in r.url.params for r in requests)
    if case == "introduction":
        assert requests == []  # the first-greeting lane deliberately never reads history
    assert any(args[1] == "posted" for args, _ in statuses)


def test_stale_tail_keeps_latest_user_anchor_without_historical_quotes():
    context = consumer._proactive_chat_context_from_history(HISTORY, limit=1, now=100000)
    assert "收到啦" in context.text and "露营灯的事情" not in context.text
    assert context.memory_anchor == {"id": "u_latest", "seq": 10}
