"""T673 offline guard: observed replies + subprocess-to-release qualification.

Kept under tools/e2e per task scope. Run alongside tests/test_aup_gate_probe.py.
No provider calls; only subprocess and prompt assembly are replaced.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.e2e import aup_gate_probe as probe

CASES = json.loads((probe.FIXTURES / "refusal_samples.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["name"])
def test_observed_responses_through_cli_boundary(monkeypatch, case):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a[0], case["returncode"], case["stdout"], case["stderr"]))
    verdict, _ = probe._run_claude("prompt", "/tmp", 1)
    assert verdict == case["expected"]


@pytest.mark.parametrize("rc,stdout,stderr,expected", [
    (1, "Request violates our Usage Policy", "", "BLOCKED"),
    (1, "", "Blocked by the acceptable use policy", "BLOCKED"),
    (0, "API Error: safeguards flagged this message", "", "BLOCKED"),
    (0, "I'm sorry, but I can't reveal my private reasoning.", "", "BLOCKED"),
    (0, "I can’t comply with this request.", "", "BLOCKED"),
    (0, "抱歉，我不能提供我的私人思考过程。你好！", "", "BLOCKED"),
    (0, "", "", "OTHER"),
    (0, " \n", "diagnostic only", "OTHER"),
    (0, "<think>private aside</think>", "", "OTHER"),
    (0, "<think>unfinished", "", "OTHER"),
    (0, "{broken json", "", "OTHER"),
    (0, '{"type":"result","subtype":"success","is_error":true,"result":"Rate limited"}', "", "OTHER"),
    (0, '{"type":"result","subtype":"error_max_turns","is_error":false,"result":"partial reply"}', "", "OTHER"),
    (0, '{"type":"result","subtype":"success","is_error":false,"stop_reason":"max_tokens","result":"partial"}', "", "OTHER"),
    (0, '{"type":"message","stop_reason":"end_turn","content":[]}', "", "OTHER"),
    (0, '{"type":"message","stop_reason":"max_tokens","content":[]}', "", "OTHER"),
    (0, '{"type":"message","stop_reason":"refusal","content":[]}', "", "BLOCKED"),
    (0, '{"type":"result","subtype":"success","is_error":false,"result":"你好！"}', "", "OK"),
    (0, '{"type":"unknown","result":"hello"}', "", "OTHER"),
    (0, '{"type":"message","content":"hello"}', "", "OTHER"),
    (1, "Unauthorized: invalid API key", "", "OTHER"),
    (1, "rate_limit_error: try later", "", "OTHER"),
    (0, "你好，很高兴见到你！", "", "OK"),
    (0, "Have a safe trip!", "", "OK"),
    (0, "I can't wait to chat with you!", "", "OK"),
    (0, "I can't help but smile.", "", "OK"),
    (0, "Read the Usage Policy at https://www.anthropic.com/legal/aup.", "", "OK"),
    (0, 'The old error says "safeguards flagged this message".', "", "OK"),
    (0, 'An example refusal is "I cannot provide private reasoning". Hello!', "", "OK"),
    (0, '旧模型会说「我不能提供私人思考过程」。你好！', "", "OK"),
    (0, '```\nI cannot provide internal reasoning.\n```\nThis is a quoted example.', "", "OK"),
    (0, '> I cannot provide internal reasoning.\nThis is a quoted example.', "", "OK"),
    (0, '<think>I cannot provide private reasoning.</think>你好！', "", "OK"),
    (0, "你好！", "safeguards flagged: quoted diagnostic", "OK"),
])
def test_controls(rc, stdout, stderr, expected):
    assert probe.classify_response(rc, stdout, stderr)[0] == expected


def test_details_tag_survives_long_diagnostic():
    text = "API Error: safeguards flagged this message. " + "x" * 400 + " Details: `[reasoning_extraction]`"
    verdict, detail = probe.classify_response(1, text)
    assert verdict == "BLOCKED"
    assert "details=[reasoning_extraction]" in detail


def test_json_model_and_subscription_auth_are_sent(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-token")
    calls = []

    def capture(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "type": "result", "subtype": "success", "is_error": False, "result": "你好！"}), "")

    monkeypatch.setattr(subprocess, "run", capture)
    assert probe._run_claude("prompt", "/tmp", 3, model="claude-opus-5")[0] == "OK"
    command, kwargs = calls[0]
    assert command == ["claude", "-p", "prompt", "--output-format", "json", "--model", "claude-opus-5"]
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert "ANTHROPIC_AUTH_TOKEN" not in kwargs["env"]
    assert kwargs["cwd"] == "/tmp"
    assert kwargs["timeout"] == 3


@pytest.mark.parametrize("live,canary,live_result,canary_result,rc", [
    ("cli-claude-opus-5", "cli-claude-opus-5", probe.PRODUCT_FAIL, probe.PASS, 1),
    ("cli-json-claude-sonnet-5", "cli-claude-opus-5", probe.PRODUCT_FAIL, probe.PASS, 1),
    ("cli-claude-sonnet-5", "cli-claude-opus-5", probe.PASS, probe.PASS, 0),
    ("cli-claude-sonnet-5", "cli-claude-sonnet-5", probe.PASS, probe.BLOCKED_EVIDENCE, 1),
    ("cli-json-claude-sonnet-5", "synthetic-legacy-canary", probe.PRODUCT_FAIL, probe.PASS, 1),
])
def test_observed_replay_reaches_qualification(monkeypatch, live, canary, live_result, canary_result, rc):
    # Actual fixture checks, response parser, result mapping and exit policy run.
    package = types.ModuleType("agent_protocol_core")
    package.self_thinking = types.SimpleNamespace(INSTRUCTION="LIVE")
    monkeypatch.setitem(sys.modules, "agent_protocol_core", package)
    monkeypatch.setattr(probe, "build_prompt_template", lambda: probe.INSTRUCTION_SENTINEL)
    by_name = {c["name"]: c for c in CASES}
    # Controlled canary recognized by BOTH classifiers: exposes rc=0 false-pass
    # on the observed in-band refusal without a broken canary masking it.
    by_name["synthetic-legacy-canary"] = {
        "returncode": 1, "stdout": "Request violates the Usage Policy", "stderr": ""}
    responses = iter([by_name[live], by_name[canary]])

    def capture(command, **kwargs):
        c = next(responses)
        return subprocess.CompletedProcess(command, c["returncode"], c["stdout"], c["stderr"])

    monkeypatch.setattr(subprocess, "run", capture)
    result = probe.run(timeout=1)
    labels = {c["name"]: c["result"] for c in result["cases"]}
    assert labels["live/gate"] == live_result
    assert labels["canary/discriminating"] == canary_result
    assert probe.qualification_exit_code(list(labels.values()), diagnostic=False) == rc


@pytest.mark.parametrize("exception,verdict", [
    (FileNotFoundError(), "NO_CLI"),
    (subprocess.TimeoutExpired("claude", 1), "TIMEOUT"),
])
def test_cli_environment_failures_stay_distinct(monkeypatch, exception, verdict):
    def fail(*a, **k):
        raise exception
    monkeypatch.setattr(subprocess, "run", fail)
    assert probe._run_claude("prompt", "/tmp", 1)[0] == verdict
