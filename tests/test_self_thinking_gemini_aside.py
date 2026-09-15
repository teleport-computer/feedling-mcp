"""T591: gemini gets the ``aside`` self-thinking rendering on both runtimes.

Measured 2026-09-15 on gemini-3.6-flash only (T586/T588/T591). Tag-only bisect
(just ``<think>``→``<aside>``, wording unchanged), direct V2-shaped calls,
counted among HTTP-200 responses: 7/11 ``MALFORMED_RESPONSE`` with ``<think>``
vs 0/13 with ``<aside>`` (20 attempts per arm; both arms also saw HTTP 503).
Tag-only replay of one captured pi-wire body: 6/10 HTTP 503 with ``<think>`` vs
0/10 with ``<aside>``. The full aside rendering (tag + two wording
substitutions) was confirmed separately on the V2 shape, 8/8 and 8/8. These
tests pin the configured behavior; Gemini's server-side reason is not
observable.

Every other provider keeps the ``think`` rendering byte for byte; the Claude Code
driver rule from T587 is untouched. The V2 tool loop restates/continues the
contract with the same tag: compact delivery rounds render ``instruction(tag)``
and the ``<think>`` assistant prefill is only sent on ``think`` turns.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_checkpoint.json",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

from agent_protocol_core import self_thinking as st  # noqa: E402
from model_api_runtime.v2 import context, worker  # noqa: E402
from model_api_runtime.v2 import tool_loop  # noqa: E402
import provider_client  # noqa: E402
import tools.chat_resident_consumer as crc  # noqa: E402


@pytest.fixture(autouse=True)
def _self_thinking_on(monkeypatch):
    monkeypatch.delenv("FEEDLING_V2_SELF_THINKING", raising=False)


def _pc(provider: str, model: str = "gemini-3.6-flash"):
    return SimpleNamespace(provider=provider, model=model)


# ---------------------------------------------------------------- Runtime V2

def test_aside_providers_is_the_single_source_and_names_gemini():
    # The provider set is the one knob; a change there is a deliberate decision.
    assert st.ASIDE_TAG_PROVIDERS == frozenset({"gemini"})
    assert context._ASIDE_TAG_PROVIDERS is st.ASIDE_TAG_PROVIDERS


@pytest.mark.parametrize("provider,tag", [
    ("gemini", st.TAG_ASIDE), ("GEMINI", st.TAG_ASIDE), (" gemini ", st.TAG_ASIDE),
    ("anthropic", st.TAG_THINK), ("openai_compatible", st.TAG_THINK), ("", st.TAG_THINK), (None, st.TAG_THINK),
])
def test_tag_for_provider(provider, tag):
    assert st.tag_for_provider(provider) == tag


@pytest.mark.parametrize("provider", ["gemini", "Gemini", " gemini "])
def test_gemini_selects_aside(provider):
    assert context.self_thinking_tag(_pc(provider)) == st.TAG_ASIDE


@pytest.mark.parametrize("provider", ["anthropic", "openai", "openai_compatible",
                                      "openrouter", "deepseek", "", None])
def test_other_providers_keep_think(provider):
    assert context.self_thinking_tag(_pc(provider, "gpt-5.2")) == st.TAG_THINK


def test_none_config_keeps_think():
    assert context.self_thinking_tag(None) == st.TAG_THINK


def test_gemini_chat_system_prompt_uses_aside_and_never_think():
    prompt = context.chat_system_prompt(_pc("gemini"))
    assert prompt == context._join_policy_blocks(
        context._CHAT_REPLY_POLICY,
        st.instruction(st.TAG_ASIDE),
        context._CHAT_POLICY_AFTER_THINKING,
    )
    assert "<aside>" in prompt and "</aside>" in prompt
    assert "<think>" not in prompt and "</think>" not in prompt


def test_non_gemini_chat_system_prompt_is_unchanged():
    # ``instruction(think)`` is the untouched INSTRUCTION constant (T587 test),
    # so this equality pins the pre-T591 rendering for every other provider.
    prompt = context.chat_system_prompt(_pc("anthropic", "claude-sonnet-4-6"))
    assert prompt == context._join_policy_blocks(
        context._CHAT_REPLY_POLICY,
        st.INSTRUCTION,
        context._CHAT_POLICY_AFTER_THINKING,
    )
    assert "<aside>" not in prompt


def test_scheduled_wake_prompt_follows_the_tag():
    base = "BASE WAKE PROMPT"
    aside = worker._wake_system_prompt_for_lane("scheduled", base, tag=st.TAG_ASIDE)
    think = worker._wake_system_prompt_for_lane("scheduled", base)
    assert aside == context._join_policy_blocks(base, st.instruction(st.TAG_ASIDE))
    assert think == context._join_policy_blocks(base, st.INSTRUCTION)
    assert "<think>" not in aside


def test_optional_wake_lanes_ignore_the_tag():
    base = "BASE WAKE PROMPT"
    for lane in ("heartbeat", "manual_wake"):
        assert (worker._wake_system_prompt_for_lane(lane, base, tag=st.TAG_ASIDE)
                == worker._wake_system_prompt_for_lane(lane, base))


def test_absent_correction_follows_the_tag():
    aside = worker._self_thinking_absent_correction_instruction(st.TAG_ASIDE)
    assert aside.startswith("上一轮最终回复缺少规定的 <aside>…</aside> 结构。")
    assert aside.endswith(st.instruction(st.TAG_ASIDE).strip())
    assert "<think>" not in aside
    # The historical constant is the ``think`` rendering, unchanged.
    assert (worker._SELF_THINKING_ABSENT_CORRECTION_INSTRUCTION
            == worker._self_thinking_absent_correction_instruction(st.TAG_THINK))
    assert worker._SELF_THINKING_ABSENT_CORRECTION_INSTRUCTION.startswith(
        "上一轮最终回复缺少规定的 <think>…</think> 结构。")


# ------------------------------------------------------------ Runtime V1 (pi)

@pytest.fixture
def _resident_pi(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'pi --mode json --model feedling/gemini-3.6-flash')
    monkeypatch.setitem(crc.AGENT_RUNTIME_METADATA, "model", "gemini-3.6-flash")


def test_resident_pi_gemini_uses_aside(monkeypatch, _resident_pi):
    monkeypatch.setitem(crc.AGENT_RUNTIME_METADATA, "provider", "gemini")
    assert crc._self_thinking_tag() == st.TAG_ASIDE
    assert crc._foreground_self_thinking_instruction() == st.instruction(st.TAG_ASIDE).strip()
    assert "<aside>" in crc._wake_think_permission_line()
    assert "<think>" not in crc._wake_think_permission_line()


@pytest.mark.parametrize("provider", ["openai_compatible", "openrouter", "deepseek", ""])
def test_resident_pi_other_providers_keep_think(monkeypatch, _resident_pi, provider):
    monkeypatch.setitem(crc.AGENT_RUNTIME_METADATA, "provider", provider)
    assert crc._self_thinking_tag() == st.TAG_THINK
    assert crc._foreground_self_thinking_instruction() == st.INSTRUCTION.strip()


def test_resident_claude_driver_rule_still_wins(monkeypatch):
    monkeypatch.setattr(crc, "AGENT_MODE", "cli")
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'claude -p "{message}"')
    monkeypatch.setitem(crc.AGENT_RUNTIME_METADATA, "provider", "anthropic")
    assert crc._self_thinking_tag() == st.TAG_ASIDE


# ------------------------------------------------- call-site wiring guards

def _worker_source() -> str:
    import inspect
    return inspect.getsource(worker)


def test_every_wake_prompt_call_site_passes_the_provider_tag():
    # The function default is ``think``; a call site that forgets ``tag=`` would
    # silently send gemini the failing rendering again on the scheduled lane.
    src = _worker_source()
    calls = [m for m in re.finditer(r"_wake_system_prompt_for_lane\((?!\s*\n?\s*lane: str)", src)]
    assert calls, "call sites not found"
    for m in calls:
        window = src[m.end(): m.end() + 200]
        assert "tag=context.self_thinking_tag(provider_config)" in window, window


def test_every_absent_correction_call_site_passes_the_provider_tag():
    src = _worker_source()
    sites = [m for m in re.finditer(r"_self_thinking_absent_correction_instruction\(", src)
             if not src[max(0, m.start() - 4): m.start()].endswith("def ")]
    # One rendering of the historical constant plus the live correction site.
    assert len(sites) >= 2, len(sites)
    live = [m for m in sites if "self_thinking.TAG_THINK" not in src[m.end(): m.end() + 40]]
    assert live, "live correction call site not found"
    for m in live:
        assert "context.self_thinking_tag(provider_config)" in src[m.end(): m.end() + 120]


# ------------------------------------------ tool loop: prefill / compact rounds
#
# Drives the REAL run_tool_loop with a local fake at the provider boundary
# (reliable_chat_completion_async) and records the ``assistant_prefill`` the loop
# requested plus what provider_client would let through. A mirror of the
# decision would pass with the gate commented out (codex2 round-2 finding), so
# the loop itself is exercised here — terminal text round and a
# FinalReplyCorrectionRequest retry (the missing-block correction path).

import asyncio


def _drive_loop(monkeypatch, provider: str, model: str, *, correction: bool):
    config = provider_client.ProviderConfig(provider=provider, model=model, api_key="offline-placeholder")
    tag = st.tag_for_provider(provider)
    calls: list[dict] = []

    async def fake(cfg, messages, **kwargs):
        requested = kwargs.get("assistant_prefill", "")
        effective = provider_client._effective_assistant_prefill(
            provider=provider, model=model, requested=requested,
            tools=kwargs.get("tools"), tool_choice=kwargs.get("tool_choice"),
        )
        system = "\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "system")
        calls.append({"requested": requested, "effective": effective,
                      "system_has_aside": "<aside>" in system, "system_has_think": "<think>" in system})
        return {"reply": f"<{tag}>thought</{tag}>hello", "tool_calls": [], "usage": {}}

    async def on_reply(text, *, final, reasoning="", correction_outcome=""):
        if correction and len(calls) == 1:
            return tool_loop.FinalReplyCorrectionRequest(
                instruction=worker._self_thinking_absent_correction_instruction(tag),
                original_text=text, original_reasoning=reasoning)
        return None

    async def dispatch(_calls):
        return []

    async def fold():
        return []

    def build(transcript):
        return [{"role": "system", "content": context.chat_system_prompt(config)},
                {"role": "user", "content": "hello"}, *transcript]

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", fake)
    result = asyncio.run(tool_loop.run_tool_loop(
        provider_config=config, build_messages=build, dispatch_tools=dispatch, on_reply=on_reply,
        fold_new_messages=fold, add_usage=lambda usage: None,
        max_calls=2 if correction else 1, suppress_native_reasoning=True,
    ))
    assert result.stop_reason == "final_text", result
    assert len(calls) == (2 if correction else 1), calls
    return calls


@pytest.mark.parametrize("model", ["gemini-2.5-flash", "gemini-3.1-pro", "gemini-3.6-flash"])
@pytest.mark.parametrize("correction", [False, True])
def test_real_loop_sends_no_think_prefill_on_gemini_aside_turns(monkeypatch, model, correction):
    # gemini-2.5-flash / 3.1-pro are on provider_client's continuation allowlist,
    # so an ungated loop would hand them a ``<think>`` opener under an aside
    # system prompt; 3.6-flash is the measured model.
    calls = _drive_loop(monkeypatch, "gemini", model, correction=correction)
    for c in calls:
        assert c["system_has_aside"] and not c["system_has_think"], c
        assert c["requested"] == "" and c["effective"] == "", c


@pytest.mark.parametrize("correction", [False, True])
def test_real_loop_keeps_think_prefill_for_continuation_capable_anthropic(monkeypatch, correction):
    # Positive control: a provider that keeps the think rendering AND accepts the
    # continuation prefix still gets it on the terminal round (unchanged path).
    calls = _drive_loop(monkeypatch, "anthropic", "claude-sonnet-4-5", correction=correction)
    assert calls[-1]["system_has_think"] and not calls[-1]["system_has_aside"], calls
    assert calls[-1]["requested"] == "<think>" and calls[-1]["effective"] == "<think>", calls


def test_compact_delivery_round_renders_the_selected_tag():
    # The compact-delivery prompt is a closure inside run_tool_loop; pin the
    # source it renders from until a delivery-round fixture exists.
    import inspect
    src = inspect.getsource(tool_loop.run_tool_loop)
    compact = src[src.index("def _compact_delivery_system_prompt"):][:600]
    assert "self_thinking.instruction(self_thinking_tag)" in compact
    assert "self_thinking.INSTRUCTION" not in compact
