"""感知 prompt 的基线快照 —— 守「行为逐字节不变」。

fixture 在 origin/test 基线的 checkout 上跑 scripts/dump_perception_baseline.py 得到。
一旦红，说明某段说明书被改动了 —— 那是行为变更，不能混在重构批次里悄悄发生。

★ 四格验收矩阵（2026-08-20 补，见
  .superpowers/sdd/2026-08-19-perception-extraction-step1/matrix-report.md）：
  上面几个 test_* 只钉了「常量本身」和 V1 的一个纯函数；下面这批钉的是**真实入口**：

  1. V2 hosted worker  —— `worker._wake_system_prompt_for_lane`（按 lane 真实选择
     wake 系统 prompt 的那个函数，覆盖 heartbeat/manual_wake/screen_watch/两种
     scheduled）+ `context.build_turn_messages`（V2 runtime context 真实拼装入口）。
  2/3. hosted resident / self-hosted VPS resident —— 用 subprocess 分别以
     `_HOSTED=True`/`_HOSTED=False` 两种真实模块导入态跑同一入口
     `_native_reachout_perception_context`，证明两态输出相同（该函数全文不读
     `_HOSTED`，origin/test 与本分支的源码都核对过）。
  4. agent mode —— 同一组 subprocess 再交叉 `AGENT_MODE=cli`/`AGENT_MODE=http`，
     证明感知说明文案不随 agent 模式变化（同样是该函数不读 `AGENT_MODE` 的直接结果）。
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest

_FIXTURE = (
    pathlib.Path(__file__).resolve().parent
    / "fixtures" / "perception_kernel" / "prompt_baseline.json"
)

# Self-contained sys.path bootstrap (mirrors other _PURE_UNIT files such as
# tests/test_v2_history_search_unit.py): conftest.py only adds backend/ to
# sys.path inside its DB-provisioning try-block, so on a no-Postgres machine
# this file must add both backend/ and tools/ itself. chat_resident_consumer.py
# also reads FEEDLING_API_URL/FEEDLING_API_KEY at module scope, so those must
# be set before the first `import chat_resident_consumer` (pattern mirrors
# tests/test_chat_resident_consumer_file.py).
os.environ.setdefault("FEEDLING_API_URL", "http://localhost:5001")
os.environ.setdefault("FEEDLING_API_KEY", "test_key_00000000")
_BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
_TOOLS = pathlib.Path(__file__).resolve().parent.parent / "tools"
for _p in (_BACKEND, _TOOLS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _baseline() -> dict[str, str]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_v2_wake_system_prompt_unchanged():
    from model_api_runtime.v2 import worker

    assert worker._WAKE_SYSTEM_PROMPT == _baseline()["v2_wake_system"]


def test_v2_scheduled_wake_system_prompt_unchanged():
    from model_api_runtime.v2 import worker

    assert worker._SCHEDULED_WAKE_SYSTEM_PROMPT == _baseline()["v2_scheduled_wake_system"]


def test_v2_runtime_perception_policy_unchanged():
    from model_api_runtime.v2 import context

    assert context._RUNTIME_PERCEPTION_POLICY == _baseline()["v2_runtime_perception_policy"]


def test_v1_reachout_context_unchanged():
    import chat_resident_consumer as consumer

    got = consumer._native_reachout_perception_context(
        {"place_label": "office", "motion_state": "walking"},
        [{"signal": "health_sleep", "field": "asleep_minutes", "direction": "down"}],
        {"location": {"label": "office"}, "media": {"new_artist": True}},
    )
    assert got == _baseline()["v1_reachout_context"]


def test_v1_reachout_context_empty_unchanged():
    import chat_resident_consumer as consumer

    assert consumer._native_reachout_perception_context({}, [], None) == \
        _baseline()["v1_reachout_context_empty"]


def test_v1_reachout_context_change_only_unchanged():
    """Exercises the `elif change:` back-compat branch (domains falsy, change
    non-empty) — neither of the two tests above ever reaches it: one has
    domains truthy, the other has both change and domains empty."""
    import chat_resident_consumer as consumer

    got = consumer._native_reachout_perception_context(
        {"place_label": "office", "motion_state": "walking"},
        [{"signal": "health_sleep", "field": "asleep_minutes", "direction": "down"}],
        None,
    )
    assert got == _baseline()["v1_reachout_context_change_only"]


def test_v2_tool_schema_perception_descriptions_unchanged():
    """Task 5 will move these out of capabilities.tool_schema.DESCRIPTIONS into
    the kernel package; pin the current text of each one individually so a
    later diff can tell exactly which tool's wording moved/changed."""
    from capabilities import tool_schema

    baseline = _baseline()["v2_tool_schema_perception"]
    for name, expected in baseline.items():
        assert tool_schema.DESCRIPTIONS[name] == expected


# ---------------------------------------------------------------------------
# Matrix cell 1: V2 hosted worker — wake system prompt AS ACTUALLY SELECTED
# per lane, through the real selector `worker._wake_system_prompt_for_lane`.
# ---------------------------------------------------------------------------

# (lane, base_prompt_attr, baseline_key) — base_prompt_attr mirrors exactly what
# the real call site (`_wake_builder` inside `worker._run_wake`, around the
# `_wake_sys = _SCREEN_WATCH_SYSTEM_PROMPT if lane == "screen_watch" else
# wake_system_prompt` line) passes in for each lane, including the
# scheduled-wake variant that only replaces the base prompt when there are due
# reminder notes (`if scheduled_notes: wake_system_prompt = _SCHEDULED_WAKE_SYSTEM_PROMPT`).
_WAKE_LANE_MATRIX = (
    ("heartbeat", "_WAKE_SYSTEM_PROMPT", "v2_wake_prompt_heartbeat"),
    ("manual_wake", "_WAKE_SYSTEM_PROMPT", "v2_wake_prompt_manual_wake"),
    ("screen_watch", "_SCREEN_WATCH_SYSTEM_PROMPT", "v2_wake_prompt_screen_watch"),
    ("scheduled", "_WAKE_SYSTEM_PROMPT", "v2_wake_prompt_scheduled_no_notes"),
    ("scheduled", "_SCHEDULED_WAKE_SYSTEM_PROMPT", "v2_wake_prompt_scheduled_with_notes"),
)


@pytest.mark.parametrize("lane,base_attr,baseline_key", _WAKE_LANE_MATRIX)
def test_v2_wake_prompt_for_lane_unchanged(lane, base_attr, baseline_key, monkeypatch):
    """Every lane `_wake_system_prompt_for_lane` can actually be called with
    (the `_WAKE_LANES` frozenset plus the screen_watch base-prompt swap and the
    scheduled-wake-with-due-notes variant), pinned through the real selector —
    not just the raw constants it wraps."""
    from model_api_runtime.v2 import worker

    # Deterministic regardless of the ambient dev/CI env — the fixture was
    # captured with this forced on (core/self_thinking.py reads it per-call,
    # not at import time, and is untouched by the perception-kernel refactor).
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "1")
    base_prompt = getattr(worker, base_attr)
    got = worker._wake_system_prompt_for_lane(lane, base_prompt)
    assert got == _baseline()[baseline_key]


def test_v2_screen_watch_system_prompt_unchanged():
    from model_api_runtime.v2 import worker

    assert worker._SCREEN_WATCH_SYSTEM_PROMPT == _baseline()["v2_screen_watch_system"]


@pytest.mark.parametrize("lane,base_attr,_key", _WAKE_LANE_MATRIX)
def test_v2_wake_prompt_self_thinking_disabled_is_identity(lane, base_attr, _key, monkeypatch):
    """The selector's other branch: with the self-thinking suffix off, every
    lane must return the base prompt byte-for-byte unchanged (this is the
    ``if not self_thinking.enabled(): return base_prompt`` early return)."""
    from model_api_runtime.v2 import worker

    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "0")
    base_prompt = getattr(worker, base_attr)
    assert worker._wake_system_prompt_for_lane(lane, base_prompt) == base_prompt


# ---------------------------------------------------------------------------
# Matrix cell 1: V2 runtime context policy assembly — the REAL assembly entry
# point `context.build_turn_messages`, not the bare `_RUNTIME_PERCEPTION_POLICY`
# constant already pinned above.
# ---------------------------------------------------------------------------

def test_v2_build_turn_messages_perception_policy_unchanged():
    from model_api_runtime.v2 import context

    messages = context.build_turn_messages(
        system_prompt="SENTINEL_SYSTEM_PROMPT", summary="", tail=[]
    )
    assert messages[0]["role"] == "system"
    got = messages[0]["content"]
    assert got == _baseline()["v2_build_turn_messages_system"]
    # Belt-and-braces: the composed system message must actually still contain
    # the perception policy text (not just match the fixture verbatim — a
    # fixture and production code could theoretically drift together if
    # someone edited both by hand instead of running the dump script).
    assert context._RUNTIME_PERCEPTION_POLICY in got


# ---------------------------------------------------------------------------
# Matrix cells 2/3/4: hosted resident vs self-hosted VPS resident (`_HOSTED`
# True/False) crossed with agent mode (`AGENT_MODE` cli/http). Each combo
# imports tools/chat_resident_consumer.py FRESH in its own subprocess (module
# state — `_HOSTED`, `AGENT_MODE` — is fixed at import time from env, so a
# single in-process import can't represent more than one cell) and calls the
# real entry point. All four must equal the SAME already-pinned
# `v1_reachout_context` baseline: `_native_reachout_perception_context` does
# not read `_HOSTED` or `AGENT_MODE` anywhere in its body (verified by reading
# the function, both on this branch and on origin/test — `git diff
# origin/test..HEAD -- tools/chat_resident_consumer.py` touches only the two
# perception-kernel string literals, nothing conditional on either flag), so
# this is a real, exercised proof of that invariant rather than a static claim.
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

_SUBPROCESS_SNIPPET = """
import json, os, sys
sys.path.insert(0, {tools!r})
sys.path.insert(0, {backend!r})
import chat_resident_consumer as consumer
assert consumer._HOSTED is {expect_hosted!r}, (consumer._HOSTED, {expect_hosted!r})
assert consumer.AGENT_MODE == {expect_agent_mode!r}, (consumer.AGENT_MODE, {expect_agent_mode!r})
got = consumer._native_reachout_perception_context(
    {{"place_label": "office", "motion_state": "walking"}},
    [{{"signal": "health_sleep", "field": "asleep_minutes", "direction": "down"}}],
    {{"location": {{"label": "office"}}, "media": {{"new_artist": True}}}},
)
print(json.dumps(got))
"""


def _run_consumer_reachout_in_subprocess(*, hosted: bool, agent_mode: str) -> str:
    env = dict(os.environ)
    env["FEEDLING_API_URL"] = "http://localhost:5001"
    env["FEEDLING_API_KEY"] = "test_key_00000000"
    env["AGENT_MODE"] = agent_mode
    if hosted:
        # Any non-empty path makes `_HOSTED = bool(FEEDLING_RUNTIME_TOKEN_FILE)`
        # True; the file need not exist — `_refresh_auth_header` catches OSError.
        env["FEEDLING_RUNTIME_TOKEN_FILE"] = "/tmp/perc-matrix-nonexistent-token"
    else:
        env.pop("FEEDLING_RUNTIME_TOKEN_FILE", None)
    snippet = _SUBPROCESS_SNIPPET.format(
        tools=str(_REPO_ROOT / "tools"),
        backend=str(_REPO_ROOT / "backend"),
        expect_hosted=hosted,
        expect_agent_mode=agent_mode,
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"subprocess failed (hosted={hosted} agent_mode={agent_mode}):\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    # Last non-empty stdout line is the JSON payload (module import may log to
    # stdout, e.g. chat_resident_consumer's startup banner).
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    return json.loads(lines[-1])


@pytest.mark.parametrize("hosted", [True, False])
@pytest.mark.parametrize("agent_mode", ["cli", "http"])
def test_v1_reachout_context_invariant_across_hosted_and_agent_mode(hosted, agent_mode):
    got = _run_consumer_reachout_in_subprocess(hosted=hosted, agent_mode=agent_mode)
    assert got == _baseline()["v1_reachout_context"]
