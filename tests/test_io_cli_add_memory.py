"""io_cli add-memory: payload builder + poll helper (pure, no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


def test_add_memory_payload_memory_mode():
    p = io_cli._add_memory_payload("I drink oat milk.", "diet.md", "memory", "vps-add-memory-1")
    assert p["mode"] == "add_memory"
    assert p["format"] == "auto"
    assert p["content"] == ""
    assert p["fresh_start"] is False
    assert p["client_job_id"] == "vps-add-memory-1"
    assert p["memory_summary_content"] == "I drink oat milk."
    assert p["memory_summary_filename"] == "diet.md"
    # identity-only keys must be absent in memory mode
    assert "ai_persona_content" not in p
    assert "character_content" not in p


def test_add_memory_payload_identity_mode():
    p = io_cli._add_memory_payload("Be blunter, use lowercase.", "persona.md", "identity", "vps-update-identity-1")
    assert p["mode"] == "update_identity"
    assert p["ai_persona_content"] == "Be blunter, use lowercase."
    assert p["character_content"] == "Be blunter, use lowercase."
    assert p["ai_persona_filename"] == "persona.md"
    assert p["character_filename"] == "persona.md"
    # memory-only key must be absent in identity mode
    assert "memory_summary_content" not in p


def test_add_memory_payload_no_filename_omits_filename_keys():
    p = io_cli._add_memory_payload("some text", "", "memory", "vps-add-memory-2")
    assert "memory_summary_filename" not in p
    assert p["memory_summary_content"] == "some text"


def test_poll_returns_done_with_memories_created(monkeypatch):
    def fake_http(method, url, auth, **kw):
        assert method == "GET"
        assert url.endswith("/v1/genesis/imports/job-abc")
        return 200, {"job": {"status": "done", "memory_action_count": 5}, "state": {}}
    monkeypatch.setattr(io_cli, "_http_json", fake_http)
    out = io_cli._poll_genesis_job("http://x", {"X-API-Key": "k"}, "job-abc", timeout=1.0, interval=0.0)
    assert out == {"ok": True, "status": "done", "job_id": "job-abc", "memories_created": 5}


def test_poll_returns_failed(monkeypatch):
    def fake_http(method, url, auth, **kw):
        return 200, {"job": {"status": "failed", "error": "add_memory_failed:boom"}, "state": {}}
    monkeypatch.setattr(io_cli, "_http_json", fake_http)
    out = io_cli._poll_genesis_job("http://x", {}, "job-f", timeout=1.0, interval=0.0)
    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "boom" in out["error"]


def test_poll_timeout_returns_pending(monkeypatch):
    def fake_http(method, url, auth, **kw):
        return 200, {"job": {"status": "processing"}, "state": {}}
    monkeypatch.setattr(io_cli, "_http_json", fake_http)
    # timeout=0 -> first deadline check trips immediately, no sleep
    out = io_cli._poll_genesis_job("http://x", {}, "job-p", timeout=0.0, interval=0.0)
    assert out == {"ok": True, "status": "pending", "job_id": "job-p"}


def test_poll_http_error(monkeypatch):
    def fake_http(method, url, auth, **kw):
        return 500, {"error": "boom"}
    monkeypatch.setattr(io_cli, "_http_json", fake_http)
    out = io_cli._poll_genesis_job("http://x", {}, "job-e", timeout=1.0, interval=0.0)
    assert out["ok"] is False
    assert out["status"] == "error"
    assert out["http_status"] == 500
