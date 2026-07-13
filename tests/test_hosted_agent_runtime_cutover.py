"""Pure-unit tests for backend/hosted/agent_runtime_cutover.py.

The hosted /v1/model_api/chat/send endpoint can route a user to the out-of-process
agent runtime instead of the inline LLM call, behind a per-user flag, while
keeping the external contract stable (short turn → synchronous reply, slow turn →
processing). No flask/DB here — the wait helper takes an injected clock/sleep and
a store-like object exposing ``chat_messages``.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import agent_runtime_cutover as cutover


class FakeStore:
    def __init__(self, messages=None):
        self.chat_messages = messages or []


# ---- flag resolution ----

def test_driver_map_pi():
    import backend.hosted.agent_runtime_cutover as c
    assert c.driver_for_provider("openai_compatible") == "pi"
    assert c.driver_for_provider("gemini") == "pi"
    assert c.driver_for_provider("openrouter") == "pi"
    assert c.driver_for_provider("deepseek") == "pi"
    assert c.driver_for_provider("anthropic") == "claude"
    assert c.driver_for_provider("openai") == "codex"


def test_driver_for_provider_is_derived_not_chosen():
    # Claude Code (Anthropic-wire) handles ONLY anthropic; Codex is
    # openai-native only; pi is the catch-all for the rest (incl. deepseek),
    # unconditionally.
    assert cutover.driver_for_provider("anthropic") == "claude"
    assert cutover.driver_for_provider("claude") == "claude"      # alias → anthropic
    assert cutover.driver_for_provider("deepseek") == "pi"        # direct relay, no gateway
    assert cutover.driver_for_provider("openai") == "codex"
    # gemini/openrouter/openai_compatible → pi, unconditionally (no gateway hop)
    for p in ("gemini", "openrouter", "openai_compatible"):
        assert cutover.driver_for_provider(p) == "pi"
    # no provider configured → no hosted agent
    for p in ("", "bogus"):
        assert cutover.driver_for_provider(p) == "legacy"


def test_deepseek_derives_to_pi_anthropic_stays_claude():
    assert cutover.driver_for_provider("deepseek") == "pi"
    assert cutover.driver_for_provider("anthropic") == "claude"
    assert cutover.driver_for_provider("openai") == "codex"


def test_codex_transport_native_only_for_openai():
    # Codex speaks OpenAI Responses natively; it is the ONLY codex-driven
    # provider left (the LiteLLM gateway is retired). Everything else — pi-driven
    # or claude-driven or unconfigured — has no codex transport.
    assert cutover.codex_transport("openai") == "native"
    for p in ("gemini", "openrouter", "openai_compatible", "anthropic", "claude",
              "deepseek", "", "bogus"):
        assert cutover.codex_transport(p) == ""


def test_gemini_and_openrouter_derive_to_pi_unconditionally():
    assert cutover.driver_for_provider("gemini") == "pi"
    assert cutover.driver_for_provider("openrouter") == "pi"
    assert cutover.driver_for_provider("openai_compatible") == "pi"
    assert cutover.driver_for_provider("openai") == "codex"
    assert cutover.driver_for_provider("anthropic") == "claude"
    assert cutover.driver_for_provider("deepseek") == "pi"


def test_codex_transport_only_native_or_empty():
    assert cutover.codex_transport("openai") == "native"
    for p in ("gemini", "openrouter", "openai_compatible", "anthropic"):
        assert cutover.codex_transport(p) == ""


def test_resolve_driver_raises_when_no_provider():
    # No config or no provider → raise (no legacy fallback anymore)
    with pytest.raises(cutover.UnsupportedProviderError):
        cutover.resolve_driver(None)
    with pytest.raises(cutover.UnsupportedProviderError):
        cutover.resolve_driver({})
    with pytest.raises(cutover.UnsupportedProviderError):
        cutover.resolve_driver({"provider": "bogus"})


def test_resolve_driver_derives_agent_from_provider():
    # Provider alone determines the driver — no per-user flag needed
    assert cutover.resolve_driver({"provider": "anthropic"}) == "claude"
    assert cutover.resolve_driver({"provider": "deepseek"}) == "pi"
    assert cutover.resolve_driver({"provider": "openai"}) == "codex"


def test_resolve_driver_routes_all_pi_providers_regardless_of_gateway():
    # Gateway check removed: gemini/openrouter/openai_compatible always → pi
    for p in ("gemini", "openrouter", "openai_compatible"):
        assert cutover.resolve_driver({"provider": p}) == "pi"
    # openai is native (unaffected)
    assert cutover.resolve_driver({"provider": "openai"}) == "codex"


def test_resolve_driver_ignores_stale_per_user_flag():
    # Any stored agent_runtime_driver value is ignored; only provider matters
    assert cutover.resolve_driver({"agent_runtime_driver": "legacy", "provider": "anthropic"}) == "claude"
    assert cutover.resolve_driver({"agent_runtime_driver": "codex", "provider": "anthropic"}) == "claude"
    assert cutover.resolve_driver({"agent_runtime_driver": "auto", "provider": "openai"}) == "codex"


# ---- reply lookup ----

def test_find_reply_uses_reply_message_id_link():
    msgs = [
        {"id": "u1", "role": "user", "ts": 1.0, "reply_message_id": "a1"},
        {"id": "a1", "role": "openclaw", "ts": 2.0, "body_ct": "..."},
    ]
    row = cutover.find_reply_row(FakeStore(msgs), "u1")
    assert row["id"] == "a1"


def test_find_reply_falls_back_to_reply_to_message_id():
    msgs = [
        {"id": "u1", "role": "user", "ts": 1.0},
        {"id": "a1", "role": "openclaw", "ts": 2.0, "reply_to_message_id": "u1"},
    ]
    assert cutover.find_reply_row(FakeStore(msgs), "u1")["id"] == "a1"


def test_find_reply_none_when_not_yet_answered():
    msgs = [{"id": "u1", "role": "user", "ts": 1.0}]
    assert cutover.find_reply_row(FakeStore(msgs), "u1") is None


# ---- wait loop ----

def test_wait_returns_reply_when_it_arrives():
    store = FakeStore([{"id": "u1", "role": "user", "ts": 1.0}])
    ticks = {"n": 0}

    def fake_sleep(_):
        ticks["n"] += 1
        if ticks["n"] == 2:  # reply lands on the 2nd poll
            store.chat_messages.append({"id": "a1", "role": "openclaw", "ts": 2.0,
                                        "reply_to_message_id": "u1"})

    clock = {"t": 0.0}
    row = cutover.wait_for_reply(store, "u1", timeout=10.0, poll_interval=0.5,
                                 sleep=fake_sleep, now=lambda: clock.__setitem__("t", clock["t"] + 0.5) or clock["t"])
    assert row is not None and row["id"] == "a1"


def test_wait_times_out_to_none():
    store = FakeStore([{"id": "u1", "role": "user", "ts": 1.0}])
    clock = {"t": 0.0}

    def advancing_now():
        clock["t"] += 1.0
        return clock["t"]

    row = cutover.wait_for_reply(store, "u1", timeout=2.0, poll_interval=0.5,
                                 sleep=lambda _: None, now=advancing_now)
    assert row is None


# ---- response shaping ----
# Under E2E the server holds no plaintext, so we never fake a legacy `reply`
# field: the agent-runtime path is always async (202) and the client reads the
# (ciphertext) reply via chat poll + enclave decrypt. `reply_ready` +
# `assistant_message` are a latency hint when the reply already landed.

def test_processing_response_is_202_with_no_reply_field():
    body, status = cutover.build_processing_response({"id": "u1", "ts": 1.0}, driver="claude")
    assert status == 202
    assert body["status"] == "processing"
    assert body["reply_ready"] is False
    assert "reply" not in body          # never a fake plaintext reply
    assert body["user_message"]["id"] == "u1"
    assert body["runtime"]["driver"] == "claude"


def test_ready_response_is_202_with_assistant_ref_not_200_ok():
    body, status = cutover.build_ready_response(
        {"id": "u1", "ts": 1.0}, {"id": "a1", "ts": 2.0}, driver="claude")
    assert status == 202                # NOT 200 — there is no synchronous plaintext
    assert body["reply_ready"] is True
    assert body["assistant_message"]["id"] == "a1"
    assert "reply" not in body
    assert body["runtime"]["driver"] == "claude"


def test_handle_send_returns_ready_when_reply_present():
    store = FakeStore([
        {"id": "u1", "role": "user", "ts": 1.0, "reply_message_id": "a1"},
        {"id": "a1", "role": "openclaw", "ts": 2.0},
    ])
    body, status = cutover.handle_send(store, {"id": "u1", "ts": 1.0}, "claude", timeout=0.0)
    assert status == 202 and body["reply_ready"] is True
    assert body["assistant_message"]["id"] == "a1"


def test_handle_send_returns_processing_when_slow():
    store = FakeStore([{"id": "u1", "role": "user", "ts": 1.0}])
    body, status = cutover.handle_send(store, {"id": "u1", "ts": 1.0}, "claude", timeout=0.0)
    assert status == 202 and body["reply_ready"] is False


# ---- assert_hosting_ready ----

def test_hosting_ready_no_litellm(monkeypatch):
    import backend.hosted.agent_runtime_cutover as c
    monkeypatch.setenv("FEEDLING_HOST_ALL", "1")
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "s")
    c.assert_hosting_ready()   # must not raise


def test_assert_hosting_ready_no_longer_requires_litellm(monkeypatch):
    # LiteLLM gateway retired: assert_hosting_ready must not require it at all.
    monkeypatch.setenv("FEEDLING_HOST_ALL", "1")
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "test-secret")
    cutover.assert_hosting_ready()  # must not raise


def test_assert_hosting_ready_raises_when_host_all_disabled(monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "test-secret")
    monkeypatch.delenv("FEEDLING_HOST_ALL", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        cutover.assert_hosting_ready()
    assert "FEEDLING_HOST_ALL" in str(exc_info.value)


def test_assert_hosting_ready_raises_when_token_secret_missing(monkeypatch):
    monkeypatch.setenv("FEEDLING_HOST_ALL", "1")
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_SECRET", raising=False)
    with pytest.raises(RuntimeError) as exc_info:
        cutover.assert_hosting_ready()
    assert "FEEDLING_RUNTIME_TOKEN_SECRET" in str(exc_info.value)


def test_assert_hosting_ready_passes_when_all_set(monkeypatch):
    monkeypatch.setenv("FEEDLING_HOST_ALL", "1")
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "test-secret")
    cutover.assert_hosting_ready()  # 不抛


# ---- supervisor heartbeat wedge guard ----
# 收口后 backend 无条件把 fit-provider 用户路由到 agent-runner。若 supervisor 没在
# 托管（心跳缺失/陈旧，或其 host_all 标志为 false，或 pi 用户要求的 pi 能力位为 false），
# 该 turn 会卡在 processing 无人应答。evaluate_supervisor_heartbeat 是纯判定；
# check_supervisor_live 读 DB 并在 DB 出错时 fail-open（不让守卫自身成为新故障源）。

def test_evaluate_heartbeat_fresh_with_flags_on_is_live():
    hb = {"ts": 1000.0, "owner": "h:1", "host_all": True, "gateway": True}
    assert cutover.evaluate_supervisor_heartbeat(hb, now=1010.0, max_age=90) == (True, "")


def test_evaluate_heartbeat_absent_is_not_live():
    ok, reason = cutover.evaluate_supervisor_heartbeat(None, now=1000.0, max_age=90)
    assert ok is False and reason == "no_supervisor_heartbeat"


def test_evaluate_heartbeat_stale_is_not_live():
    hb = {"ts": 1000.0, "host_all": True, "gateway": True}
    ok, reason = cutover.evaluate_supervisor_heartbeat(hb, now=1200.0, max_age=90)
    assert ok is False and reason.startswith("stale_supervisor_heartbeat")


def test_evaluate_heartbeat_host_all_off_is_not_live():
    hb = {"ts": 1000.0, "host_all": False, "gateway": True}
    ok, reason = cutover.evaluate_supervisor_heartbeat(hb, now=1010.0, max_age=90)
    assert ok is False and reason == "supervisor_host_all_inactive"


def test_evaluate_heartbeat_gateway_flag_is_ignored():
    """gateway 概念已退休：hb 里的 gateway=False 不再影响判定（只剩 host_all + pi）。"""
    hb = {"ts": 1000.0, "host_all": True, "gateway": False}
    ok, reason = cutover.evaluate_supervisor_heartbeat(hb, now=1010.0, max_age=90)
    assert ok is True and reason == ""


def test_evaluate_heartbeat_missing_ts_is_not_live():
    ok, reason = cutover.evaluate_supervisor_heartbeat(
        {"host_all": True, "gateway": True}, now=1000.0, max_age=90)
    assert ok is False and reason == "bad_supervisor_heartbeat"


def test_check_supervisor_live_fails_open_on_db_error(monkeypatch):
    # No multi-instance rows → legacy path; a legacy read error must fail-open.
    monkeypatch.setattr(cutover.db, "list_supervisor_instance_heartbeats",
                        lambda: [], raising=False)
    def boom():
        raise RuntimeError("pg down")
    monkeypatch.setattr(cutover.db, "read_supervisor_heartbeat", boom)
    assert cutover.check_supervisor_live(now=1000.0) == (True, "")


def test_check_supervisor_live_reads_db_and_evaluates(monkeypatch):
    # Empty multi-instance table → fall back to the legacy single-key heartbeat.
    monkeypatch.setattr(cutover.db, "list_supervisor_instance_heartbeats",
                        lambda: [], raising=False)
    monkeypatch.setattr(cutover.db, "read_supervisor_heartbeat",
                        lambda: {"ts": 999.0, "host_all": True, "gateway": True})
    assert cutover.check_supervisor_live(now=1000.0)[0] is True
    monkeypatch.setattr(cutover.db, "read_supervisor_heartbeat", lambda: None)
    ok, reason = cutover.check_supervisor_live(now=1000.0)
    assert ok is False and reason == "no_supervisor_heartbeat"


# ---- multi-instance (per-owner) heartbeat aggregate verdict ----
# Multiple runners each write their own per-owner heartbeat row (no mutual
# clobber like the legacy single key). The cluster is "live" iff at least one
# fresh runner is hosting (host_all, + pi when required). The backend does
# NOT gate on capacity — a full runner still means the cluster is up; the message
# parks in the DB until a runner with room polls it.

def test_evaluate_instances_empty_is_not_live():
    ok, reason = cutover.evaluate_supervisor_instances([], now=1000.0, max_age=90)
    assert ok is False and reason == "no_supervisor_heartbeat"


def test_evaluate_instances_one_fresh_live_is_live():
    insts = [{"ts": 1000.0, "owner": "h:1", "host_all": True, "gateway": True}]
    assert cutover.evaluate_supervisor_instances(insts, now=1010.0, max_age=90) == (True, "")


def test_evaluate_instances_one_stale_one_live_is_live():
    insts = [
        {"ts": 800.0, "owner": "h:1", "host_all": True, "gateway": True},   # stale
        {"ts": 1000.0, "owner": "h:2", "host_all": True, "gateway": True},  # fresh
    ]
    assert cutover.evaluate_supervisor_instances(insts, now=1010.0, max_age=90) == (True, "")


def test_evaluate_instances_all_stale_is_not_live():
    insts = [
        {"ts": 800.0, "owner": "h:1", "host_all": True, "gateway": True},
        {"ts": 700.0, "owner": "h:2", "host_all": True, "gateway": True},
    ]
    ok, reason = cutover.evaluate_supervisor_instances(insts, now=1010.0, max_age=90)
    assert ok is False and reason.startswith("stale_supervisor_heartbeat")


def test_evaluate_instances_reason_from_freshest_when_none_live():
    # No instance is live; the reported reason comes from the freshest row so the
    # operator sees the most current cluster state. gateway is no longer checked,
    # so both rows here are only distinguished by host_all.
    insts = [
        {"ts": 1000.0, "owner": "h:1", "host_all": False, "gateway": True},
        {"ts": 1005.0, "owner": "h:2", "host_all": False, "gateway": True},  # freshest
    ]
    ok, reason = cutover.evaluate_supervisor_instances(insts, now=1010.0, max_age=90)
    assert ok is False and reason == "supervisor_host_all_inactive"


def test_evaluate_instances_gateway_flag_is_ignored():
    insts = [{"ts": 1000.0, "owner": "h:1", "host_all": True, "gateway": False}]
    assert cutover.evaluate_supervisor_instances(insts, now=1010.0, max_age=90) == (True, "")


def test_check_supervisor_live_prefers_multi_instance(monkeypatch):
    monkeypatch.setattr(cutover.db, "list_supervisor_instance_heartbeats",
                        lambda: [{"ts": 999.0, "owner": "h:1", "host_all": True, "gateway": True}],
                        raising=False)
    # Legacy read must NOT be consulted when fresh instance rows exist.
    monkeypatch.setattr(cutover.db, "read_supervisor_heartbeat",
                        lambda: (_ for _ in ()).throw(AssertionError("legacy must not be read")))
    assert cutover.check_supervisor_live(now=1000.0) == (True, "")


def test_check_supervisor_live_falls_back_to_legacy_when_only_stale_instances(monkeypatch):
    # Only STALE instance rows (dead/rolled-back runners) → the new table is not
    # authoritative; fall back to a legacy heartbeat an old runner may still write
    # fresh. (Transitional: a rollback must not 503 every send on orphan rows.)
    monkeypatch.setattr(cutover.db, "list_supervisor_instance_heartbeats",
                        lambda: [{"ts": 800.0, "owner": "h:1", "host_all": True, "gateway": True}],
                        raising=False)
    monkeypatch.setattr(cutover.db, "read_supervisor_heartbeat",
                        lambda: {"ts": 999.0, "host_all": True, "gateway": True})
    assert cutover.check_supervisor_live(now=1000.0) == (True, "")


def test_check_supervisor_live_stale_instances_and_dead_legacy_is_not_live(monkeypatch):
    # All stale + no legacy → genuinely down (legacy fallback consulted, finds none).
    monkeypatch.setattr(cutover.db, "list_supervisor_instance_heartbeats",
                        lambda: [{"ts": 800.0, "owner": "h:1", "host_all": True, "gateway": True}],
                        raising=False)
    monkeypatch.setattr(cutover.db, "read_supervisor_heartbeat", lambda: None)
    ok, reason = cutover.check_supervisor_live(now=1000.0)
    assert ok is False and reason == "no_supervisor_heartbeat"


def test_check_supervisor_live_fresh_instance_not_hosting_does_not_fall_back(monkeypatch):
    # A FRESH instance that says host_all=false means the cluster genuinely isn't
    # hosting — the new table IS authoritative here; do NOT fall back to legacy.
    monkeypatch.setattr(cutover.db, "list_supervisor_instance_heartbeats",
                        lambda: [{"ts": 999.0, "owner": "h:1", "host_all": False, "gateway": True}],
                        raising=False)
    monkeypatch.setattr(cutover.db, "read_supervisor_heartbeat",
                        lambda: (_ for _ in ()).throw(AssertionError("legacy must not be read")))
    ok, reason = cutover.check_supervisor_live(now=1000.0)
    assert ok is False and reason == "supervisor_host_all_inactive"


def test_check_supervisor_live_instance_read_error_falls_back_to_legacy(monkeypatch):
    # New table unreadable (e.g. pre-migration) → try the legacy key, not fail-open.
    def boom():
        raise RuntimeError("relation does not exist")
    monkeypatch.setattr(cutover.db, "list_supervisor_instance_heartbeats", boom, raising=False)
    monkeypatch.setattr(cutover.db, "read_supervisor_heartbeat",
                        lambda: {"ts": 999.0, "host_all": True, "gateway": True})
    assert cutover.check_supervisor_live(now=1000.0) == (True, "")


# ---- pi driver derivation (unconditional) ----

def test_driver_for_provider_pi_is_unconditional():
    # No flag gates pi: openai_compatible/gemini/openrouter/deepseek always → pi.
    assert cutover.driver_for_provider("openai_compatible") == "pi"
    assert cutover.driver_for_provider("gemini") == "pi"
    assert cutover.driver_for_provider("openrouter") == "pi"
    assert cutover.driver_for_provider("anthropic") == "claude"
    assert cutover.driver_for_provider("deepseek") == "pi"
    assert cutover.driver_for_provider("openai") == "codex"


def test_codex_transport_empty_for_pi_providers():
    # pi-driven providers are never codex-driven, so no gateway/native transport.
    assert cutover.codex_transport("openai_compatible") == ""
    assert cutover.codex_transport("gemini") == ""
    assert cutover.codex_transport("openrouter") == ""
    assert cutover.codex_transport("openai") == "native"


def test_resolve_driver_accepts_pi():
    assert cutover.resolve_driver({"provider": "openai_compatible"}) == "pi"
    assert cutover.resolve_driver({"provider": "gemini"}) == "pi"
    assert cutover.resolve_driver({"provider": "openrouter"}) == "pi"


# ---- pi capability in heartbeat (cross-service flag-drift guard) ----

def test_evaluate_heartbeat_require_pi_gates_on_pi_flag():
    now = 1000.0
    # pi 用户: require_pi=True。runner 报 pi=True → live（gateway 字段已退休，不看）。
    fresh = {"ts": 999.0, "host_all": True, "gateway": False, "pi": True}
    assert cutover.evaluate_supervisor_heartbeat(
        fresh, now=now, max_age=90, require_pi=True) == (True, "")
    # runner pi 关 → 判不 live（防 backend-pi-on/runner-pi-off 卡 processing）。
    no_pi = {"ts": 999.0, "host_all": True, "gateway": True, "pi": False}
    live, reason = cutover.evaluate_supervisor_heartbeat(
        no_pi, now=now, max_age=90, require_pi=True)
    assert live is False and reason == "supervisor_pi_disabled"
    # 老 runner 心跳无 pi 字段 → 保守判不 live（安全方向，宁可 503 不卡死）。
    old = {"ts": 999.0, "host_all": True, "gateway": True}
    assert cutover.evaluate_supervisor_heartbeat(
        old, now=now, max_age=90, require_pi=True)[0] is False
    # 非 pi 用户(require_pi=False): pi 字段缺失不受影响。
    assert cutover.evaluate_supervisor_heartbeat(
        old, now=now, max_age=90, require_pi=False) == (True, "")


def test_check_supervisor_live_threads_require_pi(monkeypatch):
    monkeypatch.setattr(cutover.db, "list_supervisor_instance_heartbeats",
                        lambda: [{"ts": 999.0, "owner": "h:1", "host_all": True,
                                  "gateway": True, "pi": False}], raising=False)
    ok, reason = cutover.check_supervisor_live(require_pi=True, now=1000.0)
    assert ok is False and reason == "supervisor_pi_disabled"


def test_send_gate_has_no_require_gateway_param():
    import inspect
    sig = inspect.signature(cutover.check_supervisor_live)
    assert "require_gateway" not in sig.parameters
    assert "require_pi" in sig.parameters


def test_pi_heartbeat_gate_ignores_gateway_flag():
    hb = {"ts": 1_000_000.0, "host_all": True, "gateway": False, "pi": True}
    live, reason = cutover.evaluate_supervisor_heartbeat(
        hb, now=1_000_001.0, max_age=90, require_pi=True)
    assert live is True and reason == ""
