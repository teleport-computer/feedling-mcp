"""Resident decrypt-source health signal tests for chat_resident_consumer.py.

Covers the phase-1 resident side of the onboarding-decrypt-verification work
(2026-07-21): a resident whose only decrypt source is missing/unreachable
otherwise passes verify_ping (which never decrypts) while every real message is
claimed then skipped. The resident now derives a decrypt-health status from
REAL decrypt outcomes and ships it on the poll headers so the backend gate/alert
can tell a live-but-undecrypting resident from a healthy one.

Network + enclave are mocked — no real backend needed.
"""

import os
import sys
import types
from pathlib import Path

import pytest

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "cli",
    "AGENT_CLI_CMD": "claude --allowed-tools 'x' {mcp} -p {message}",
    "CHECKPOINT_FILE": "/tmp/feedling_test_decrypt_health_checkpoint.json",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as c  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_health():
    c._decrypt_health.update({"status": "unknown", "checked_at": 0.0})
    c._decrypt_health_last_refresh["at"] = 0.0
    c._decrypt_read_failures["count"] = 0
    yield


class _FakeResp:
    def raise_for_status(self):
        return None


class _FakeClient:
    def __init__(self, ok=True):
        self._ok = ok

    def get(self, *a, **kw):
        if not self._ok:
            raise RuntimeError("boom")
        return _FakeResp()


def test_headers_empty_until_first_reading():
    # unknown → no header at all; backend treats a missing header as unknown,
    # so shipping a hollow value would be wrong.
    assert c._decrypt_health_headers() == {}


def test_headers_carry_status_and_epoch_once_set(monkeypatch):
    monkeypatch.setattr(c.time, "time", lambda: 1_725_000_000.5)
    c._set_decrypt_health("ok")
    h = c._decrypt_health_headers()
    assert h["X-Feedling-Decrypt-Status"] == "ok"
    assert h["X-Feedling-Decrypt-Checked-At"] == "1725000000.500"


def test_set_health_updates_status_and_checked_at(monkeypatch):
    monkeypatch.setattr(c.time, "time", lambda: 42.0)
    c._set_decrypt_health("degraded")
    assert c._decrypt_health["status"] == "degraded"
    assert c._decrypt_health["checked_at"] == 42.0


def test_probe_unconfigured_when_no_enclave_url(monkeypatch):
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "")
    c._probe_decrypt_reachability()
    assert c._decrypt_health["status"] == "unconfigured"


def test_probe_ok_when_reachable(monkeypatch):
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    monkeypatch.setattr(c, "_client_for", lambda url: _FakeClient(ok=True))
    c._probe_decrypt_reachability()
    assert c._decrypt_health["status"] == "ok"


def test_probe_unreachable_when_source_fails(monkeypatch):
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    monkeypatch.setattr(c, "_client_for", lambda url: _FakeClient(ok=False))
    c._probe_decrypt_reachability()
    assert c._decrypt_health["status"] == "unreachable"


def test_reachability_probe_never_masks_degraded(monkeypatch):
    # A per-message decrypt failure (degraded) must NOT be upgraded to ok merely
    # because the source is dialable — only a real non-empty decrypt clears it.
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    monkeypatch.setattr(c, "_client_for", lambda url: _FakeClient(ok=True))
    monkeypatch.setattr(c.time, "time", lambda: 100.0)
    c._set_decrypt_health("degraded")
    monkeypatch.setattr(c.time, "time", lambda: 200.0)
    c._probe_decrypt_reachability()
    assert c._decrypt_health["status"] == "degraded"     # not upgraded
    assert c._decrypt_health["checked_at"] == 200.0       # heartbeat refreshed


def test_idle_refresh_is_throttled(monkeypatch):
    calls = []
    monkeypatch.setattr(c, "_probe_decrypt_reachability", lambda: calls.append(1))
    monkeypatch.setattr(c, "DECRYPT_HEALTH_REFRESH_SEC", 120.0)
    monkeypatch.setattr(c.time, "time", lambda: 1000.0)
    c._decrypt_health_last_refresh["at"] = 0.0
    c._maybe_refresh_decrypt_health()          # first call fires
    assert len(calls) == 1
    monkeypatch.setattr(c.time, "time", lambda: 1050.0)  # +50s < 120s
    c._maybe_refresh_decrypt_health()          # throttled, no fire
    assert len(calls) == 1
    monkeypatch.setattr(c.time, "time", lambda: 1200.0)  # +200s > 120s
    c._maybe_refresh_decrypt_health()          # fires again
    assert len(calls) == 2


def test_poll_merges_decrypt_headers(monkeypatch):
    captured = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"messages": []}

    def _fake_get(url, params=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(c._HTTP, "get", _fake_get)
    monkeypatch.setattr(c.time, "time", lambda: 5.0)
    c._set_decrypt_health("unreachable")
    c.poll_chat(0.0, timeout=0)
    assert captured["headers"]["X-Feedling-Decrypt-Status"] == "unreachable"
    assert captured["headers"]["X-Feedling-Decrypt-Checked-At"] == "5.000"
    # static consumer identity headers still ride along
    assert captured["headers"]["X-Feedling-Consumer"] == "feedling-chat-resident"


def _empty_user_msg(msg_id):
    return {"role": "user", "content": "", "content_type": "text",
            "ts": 100.0, "id": msg_id, "source": "chat"}


def test_empty_content_no_url_reports_unconfigured_not_degraded(monkeypatch):
    # The usr_6c1971 case: no decrypt source at all. The claimed-but-unreadable
    # message must report `unconfigured` (actionable: set FEEDLING_ENCLAVE_URL),
    # NOT `degraded` — the loose branch previously clobbered it to degraded.
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "")
    monkeypatch.setattr(c, "_reset_proactive_idle_guard", lambda: None)
    monkeypatch.setattr(c, "_clear_proactive_failure", lambda: None)
    c._process_messages([_empty_user_msg("no_url_empty")])
    assert c._decrypt_health["status"] == "unconfigured"


def test_empty_content_configured_degrades_only_on_streak(monkeypatch):
    # A configured source that yields no plaintext is a read failure, but a
    # SINGLE one is a possible transient blip (claim/history race) and must not
    # flip health — degrading on the first empty claim parked a healthy
    # established resident on sticky degraded overnight and tripped the
    # backend's maintenance alert (usr_98306ae2, 2026-07-22). Only a streak of
    # DECRYPT_DEGRADE_AFTER consecutive failures degrades.
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    monkeypatch.setattr(c, "_reset_proactive_idle_guard", lambda: None)
    monkeypatch.setattr(c, "_clear_proactive_failure", lambda: None)
    assert c.DECRYPT_DEGRADE_AFTER == 2  # default under test
    c._process_messages([_empty_user_msg("configured_empty_1")])
    assert c._decrypt_health["status"] != "degraded"
    c._process_messages([_empty_user_msg("configured_empty_2")])
    assert c._decrypt_health["status"] == "degraded"


def test_single_empty_content_blip_keeps_prior_ok(monkeypatch):
    # An established resident that has proven decryption keeps reporting ok
    # through one unreadable claim; the blip only counts toward the streak.
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    monkeypatch.setattr(c, "_reset_proactive_idle_guard", lambda: None)
    monkeypatch.setattr(c, "_clear_proactive_failure", lambda: None)
    c._note_decrypt_read_success()
    assert c._decrypt_health["status"] == "ok"
    c._process_messages([_empty_user_msg("blip")])
    assert c._decrypt_health["status"] == "ok"


def test_real_success_resets_failure_streak():
    # fail → success → fail must NOT degrade: the success proves the source
    # works, so the streak restarts from zero.
    c._note_decrypt_read_failure()
    c._note_decrypt_read_success()
    c._note_decrypt_read_failure()
    assert c._decrypt_health["status"] == "ok"
    assert c._decrypt_read_failures["count"] == 1
    c._note_decrypt_read_failure()
    assert c._decrypt_health["status"] == "degraded"


def test_degrade_after_floor_one_restores_immediate(monkeypatch):
    # Operators can opt back into degrade-on-first-failure.
    monkeypatch.setattr(c, "DECRYPT_DEGRADE_AFTER", 1)
    c._note_decrypt_read_failure()
    assert c._decrypt_health["status"] == "degraded"


def test_maintenance_injection_does_not_reset_failure_streak(monkeypatch):
    # A server-injected maintenance turn (source=resident_maintenance) carries
    # inline plaintext and never exercises the decrypt path — it must not count
    # as recovery evidence. fail → maintenance → fail still degrades at K=2,
    # and the maintenance turn never upgrades health to ok. Otherwise the very
    # alert sent DURING an outage would reset the streak forever.
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    monkeypatch.setattr(c, "_reset_proactive_idle_guard", lambda: None)
    monkeypatch.setattr(c, "_clear_proactive_failure", lambda: None)
    monkeypatch.setattr(c, "_emit_debug_trace", lambda *a, **k: None)
    monkeypatch.setattr(c, "_screen_context_for_message", lambda content: ("", [], []))
    monkeypatch.setattr(c, "_worldbook_context_for_foreground", lambda content: "")
    monkeypatch.setattr(c, "_quoted_memory_context", lambda msg: "")
    monkeypatch.setattr(c, "_prepend_time_anchor_foreground", lambda content, ts: content)
    monkeypatch.setattr(c, "_foreground_agent_message", lambda content, current_ts=0: content)
    monkeypatch.setattr(c, "_resident_chat_runtime_v2_enabled", lambda: False)
    monkeypatch.setattr(c, "_consume_reply_parse_failed", lambda: False)
    monkeypatch.setattr(c, "_note_agent_turn_success", lambda: None)
    monkeypatch.setattr(c, "call_agent", lambda *a, **k: ["on it"])
    monkeypatch.setattr(c, "execute_agent_actions", lambda actions: {"effects": []})
    monkeypatch.setattr(c, "post_reply", lambda *a, **k: {"ok": True})

    c._note_decrypt_read_failure()
    maint = {"role": "user", "content": "【Feedling 维护通知】请检查 consumer",
             "content_type": "text", "ts": 300.0, "id": "maint_1",
             "source": c.RESIDENT_MAINTENANCE_SOURCE}
    c._process_messages([maint])
    assert c._decrypt_health["status"] == "unknown"  # not upgraded to ok
    assert c._decrypt_read_failures["count"] == 1    # streak preserved
    c._process_messages([_empty_user_msg("fail_after_maint")])
    assert c._decrypt_health["status"] == "degraded"


def test_wedge_class_failures_share_the_streak():
    # The wedge path (claimed ids absent from decrypt history) uses the same
    # helper, so wedge cycles and empty-content skips accumulate one streak.
    c._note_decrypt_read_failure()
    assert c._decrypt_health["status"] == "unknown"  # untouched below streak
    c._note_decrypt_read_failure()
    assert c._decrypt_health["status"] == "degraded"


def test_non_empty_real_decrypt_clears_degraded_to_ok(monkeypatch):
    # A real non-empty user message decrypting proves the source works and
    # clears an earlier degraded → ok, before any heavy agent work.
    monkeypatch.setattr(c, "FEEDLING_ENCLAVE_URL", "https://enclave.example/")
    monkeypatch.setattr(c, "_reset_proactive_idle_guard", lambda: None)
    monkeypatch.setattr(c, "_clear_proactive_failure", lambda: None)
    # Neutralize everything downstream of the ok-transition so the test isolates
    # the health flip without running the real agent pipeline.
    monkeypatch.setattr(c, "_emit_debug_trace", lambda *a, **k: None)
    monkeypatch.setattr(c, "_screen_context_for_message", lambda content: ("", [], []))
    monkeypatch.setattr(c, "_worldbook_context_for_foreground", lambda content: "")
    monkeypatch.setattr(c, "_quoted_memory_context", lambda msg: "")
    monkeypatch.setattr(c, "_prepend_time_anchor_foreground", lambda content, ts: content)
    monkeypatch.setattr(c, "_foreground_agent_message", lambda content, current_ts=0: content)
    monkeypatch.setattr(c, "_resident_chat_runtime_v2_enabled", lambda: False)
    monkeypatch.setattr(c, "_consume_reply_parse_failed", lambda: False)
    monkeypatch.setattr(c, "_note_agent_turn_success", lambda: None)
    monkeypatch.setattr(c, "call_agent", lambda *a, **k: ["hi there"])
    monkeypatch.setattr(c, "execute_agent_actions", lambda actions: {"effects": []})
    monkeypatch.setattr(c, "post_reply", lambda *a, **k: {"ok": True})

    c._set_decrypt_health("degraded")
    msg = {"role": "user", "content": "哥哥是你吗", "content_type": "text",
           "ts": 200.0, "id": "real_reply", "source": "chat"}
    c._process_messages([msg])
    assert c._decrypt_health["status"] == "ok"


def test_verify_ping_replies_bind_reply_to_ping_id():
    # Guard the strict-matcher contract: every verify_ping post_reply in the
    # branch passes reply_to_message_id so the backend can bind the hidden ack
    # to THIS ping (source=verify_ping ∧ reply_to=ping id), not "any later agent".
    src = (Path(__file__).parent.parent / "tools" / "chat_resident_consumer.py").read_text()
    posts = [
        ln for ln in src.splitlines()
        if "post_reply(" in ln and 'source="verify_ping"' in ln
    ]
    assert len(posts) == 3, f"expected 3 verify_ping post_reply calls, got {len(posts)}"
    assert all("reply_to_message_id=_ping_id" in ln for ln in posts), posts
