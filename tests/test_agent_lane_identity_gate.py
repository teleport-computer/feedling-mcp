"""身份卡写入的 lane 闸（usr_a40e，2026-08-01）。

事故：一次心跳唤醒里模型自己改了身份卡的签名和相处天数（1388 天写成编造的
220 天），用户全程没说话。规则收敛成一条——**后台/唤醒轮次不写身份卡，读不受
影响**——所以这些用例守的是三件事：

1. 有用户在场的轮次照常能写（这条最重要：闸不能把正常改名堵死）；
2. 没有用户在场的轮次一律拒，且拒绝**可见**（不是静默 no-op）；
3. 闸只挂在 identity 写入上，读和记忆写入不受牵连。
"""

import argparse
import os
import sys
from pathlib import Path

import pytest

# chat_resident_consumer 在 import 期就读这些变量，必须先于导入设好
# （与 tests/test_chat_resident_consumer.py 的引导块同款）。
for _k, _v in {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_lane_gate_checkpoint.json",
}.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402
import chat_resident_consumer as crc  # noqa: E402


def _ns(**overrides) -> argparse.Namespace:
    base = dict(
        agent_name=None, self_introduction=None, category=None,
        user_preferred_name=None, agent_role=None, tone_style=None,
        custom_persona_prompt=None, language_preference=None,
        relationship_anchor=None, relationship_days=None,
        signature=[], add_signature=[], remove_signature=[], replace_signatures=[],
        add_boundary=[], remove_boundary=[], replace_boundaries=[],
        add_do_not_say=[], remove_do_not_say=[], replace_do_not_say=[],
        add_stable_definition=[], remove_stable_definition=[], replace_stable_definitions=[],
        nudge_dimension=[],
    )
    base.update(overrides)
    return argparse.Namespace(**base)


# 事故里被改掉的正是这两个字段，所以两个都各测一遍，不是只测 relationship_days。
_SIGNATURE_WRITE = dict(replace_signatures=["🦁🐑💜"])
_DAYS_WRITE = dict(relationship_days=220)


@pytest.mark.parametrize("lane", ["chat", None])
@pytest.mark.parametrize("write", [_SIGNATURE_WRITE, _DAYS_WRITE], ids=["signature", "days"])
def test_identity_write_allowed_when_a_user_is_in_the_turn(monkeypatch, lane, write):
    """chat = 用户在说话；lane 未设置 = 人手动跑 CLI。两者都必须放行。

    这条比"拒绝"那条更重要：闸的最大风险是把用户自己要求的修改也堵死。
    """
    if lane is None:
        monkeypatch.delenv("FEEDLING_AGENT_LANE", raising=False)
    else:
        monkeypatch.setenv("FEEDLING_AGENT_LANE", lane)

    payload = io_cli._identity_write_payload_v2(_ns(**write))

    assert payload["actions"], "有用户在场时身份写入必须照常构造出请求"


@pytest.mark.parametrize("lane", ["heartbeat", "dream", "capture", "background", "CHAT_BUT_NOT"])
@pytest.mark.parametrize("write", [_SIGNATURE_WRITE, _DAYS_WRITE], ids=["signature", "days"])
def test_identity_write_refused_with_a_visible_reason_when_nobody_is_there(monkeypatch, lane, write):
    monkeypatch.setenv("FEEDLING_AGENT_LANE", lane)

    with pytest.raises(io_cli._IdentityWritePrecheckError) as excinfo:
        io_cli._identity_write_payload_v2(_ns(**write))

    obj = excinfo.value.obj
    assert obj["ok"] is False
    assert obj["error"] == "identity_write_not_allowed_in_background"
    # 静默失败会让模型以为自己改成功了，然后像事故里那样向用户宣布"都改好了"。
    # hint 里 lane 是归一化后的小写形态，比对时同样归一化。
    assert lane.lower() in obj["hint"], "拒绝原因必须点明是哪个 lane，模型和排查的人都要看得懂"


def test_lane_matching_ignores_case_and_padding(monkeypatch):
    """lane 由环境变量传递，不该因为大小写或空白就把闸绕过去。"""
    monkeypatch.setenv("FEEDLING_AGENT_LANE", "  CHAT  ")
    assert io_cli._identity_write_payload_v2(_ns(**_DAYS_WRITE))["actions"]

    monkeypatch.setenv("FEEDLING_AGENT_LANE", "  HeartBeat ")
    with pytest.raises(io_cli._IdentityWritePrecheckError):
        io_cli._identity_write_payload_v2(_ns(**_DAYS_WRITE))


def test_gate_is_wired_only_into_the_identity_write_path():
    """闸只能挂在身份写入上。

    capture/dream 在后台轮次里要写记忆、也要读身份卡当上下文；任何一个多余的
    调用点都会在用户看不见的地方掐掉记忆整理。
    """
    source = (Path(io_cli.__file__)).read_text(encoding="utf-8")
    # 只数调用，不数 `def _identity_write_lane_error()` 那一行的定义本身。
    call_sites = sum(
        line.count("_identity_write_lane_error()")
        for line in source.splitlines()
        if not line.lstrip().startswith("def ")
    )

    assert call_sites == 1, (
        "身份 lane 闸应当只有一个调用点（identity-write 的前置检查）；"
        f"实际有 {call_sites} 处，请确认没有误挂到读取或记忆路径上"
    )


def test_background_lane_still_reaches_the_reads_it_needs():
    """守住上一条的反面：读取类 verb 不含闸调用。"""
    source = (Path(io_cli.__file__)).read_text(encoding="utf-8")

    for verb in ("cmd_identity_read", "cmd_memory_index", "cmd_memory_fetch"):
        start = source.index(f"def {verb}(")
        body = source[start:start + 2000]
        assert "_identity_write_lane_error" not in body, (
            f"{verb} 是读取路径，不该被身份写入闸波及"
        )


# ---------------------------------------------------------------------------
# consumer 侧：lane 必须真的到达 agent 子进程，否则上面的闸永远读不到值
# ---------------------------------------------------------------------------

def _capture_child_env(monkeypatch, tmp_path):
    """让 call_agent_cli 跑到 subprocess 那一步，把 env 截下来。"""
    seen: dict = {}

    crc._agent_session_id_cache.clear()
    crc._agent_session_meta_cache.clear()
    monkeypatch.setattr(
        crc, "_whoami_cache",
        {"user_id": "usr_lane", "user_pk": None, "enclave_pk": None},
    )
    monkeypatch.setattr(crc, "AGENT_SESSION_FILE_TEMPLATE", str(tmp_path / "s_{user_id}.json"))
    monkeypatch.setattr(crc, "AGENT_CLI_CMD", 'someagent -q "{message}"')
    monkeypatch.setattr(crc, "_resolve_cli_executable", lambda cmd: cmd)

    class _R:
        returncode = 0
        stdout = "在。"
        stderr = ""

    def _run(*_args, **kwargs):
        seen.update(kwargs.get("env") or {})
        return _R()

    monkeypatch.setattr(crc.subprocess, "run", _run)
    return seen


def test_chat_lane_reaches_the_agent_subprocess(monkeypatch, tmp_path):
    seen = _capture_child_env(monkeypatch, tmp_path)

    crc.call_agent_cli("hi", lane="chat")

    assert seen.get("FEEDLING_AGENT_LANE") == "chat", (
        "聊天轮次必须把 chat 传下去，否则用户在场也会被闸拒掉"
    )


def test_background_lane_reaches_the_agent_subprocess(monkeypatch, tmp_path):
    seen = _capture_child_env(monkeypatch, tmp_path)

    crc.call_agent_cli("hi", lane="heartbeat")

    assert seen.get("FEEDLING_AGENT_LANE") == "heartbeat"


def test_a_caller_that_forgets_the_lane_fails_closed(monkeypatch, tmp_path):
    """不传 lane 时必须掉到 background（拒绝侧），不能掉到 chat（放行侧）。

    这是闸的兜底方向：将来新增一条后台车道而忘了传 lane，结果应该是"改不了
    身份卡"，而不是"随便改"。
    """
    seen = _capture_child_env(monkeypatch, tmp_path)

    crc.call_agent_cli("hi")

    lane = seen.get("FEEDLING_AGENT_LANE")
    assert lane == "background"
    # 并且这个兜底值确实落在拒绝侧——不能只断言字符串相等就完事。
    monkeypatch.setenv("FEEDLING_AGENT_LANE", lane)
    with pytest.raises(io_cli._IdentityWritePrecheckError):
        io_cli._identity_write_payload_v2(_ns(relationship_days=220))
