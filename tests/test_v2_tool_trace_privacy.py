"""agent.tool.call 的 detail 里绝不能出现用户内容 —— 由机制保证,不靠调用方措辞。

V2 补工具调用埋点(2026-08-10)时,失败分支会从工具返回的正文里抠一个错误码:

    _SAFE_TRACE_ERROR_RE = re.compile(r"^error:\\s*([a-z0-9_.-]{1,64})(?:\\s|$)", re.I)

字符类含 `.` 与 `-`,所以「`error: ` + 单个 token」形状会被**原样**写进
`detail.error_code`。审的时候我实测能造出
`error: my_private_notes.txt` -> `error_code: my_private_notes.txt`。

⚠️ 严重度要说准:**当时没有任何调用方能触发它**。所有 `error: ` 产生点要么是
`errors.*` 封闭常量,要么是固定文案(首 token 是 `invalid` 这种词)。所以那是
latent 缺口,不是现网泄漏 —— 但 trace 留存 48 小时且 admin 可读,而
「detail 不含用户内容」是硬线。硬线靠机制,不靠"调用方碰巧没那么写":
以后任何人写一句 `f"error: {filename}"` 就会静默破线,而没有任何东西会报警。

本文件就是那个报警器。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import worker  # noqa: E402


def _detail(tool: str, content: str, *, args=None, metadata=None) -> dict:
    tc = SimpleNamespace(name=tool, args=args or {})
    result = worker.ToolResult(
        call_id="call_1", content=content, metadata=metadata or {},
    )
    return worker._v2_tool_trace_detail(
        tc, event_kind="tool_call_result", result=result, duration_ms=1.0,
    )


def _flat(detail: dict) -> str:
    import json

    return json.dumps(detail, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 失败正文里的 token 不许原样落进 trace
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tool,secret", [
    ("workspace_read", "my_private_notes.txt"),        # 用户文件名
    ("workspace_delete", "2026-tax-return.pdf"),
    ("memory_search", "girlfriend_name_liuyu"),        # 人名
    ("web_fetch", "internal.corp.example.com"),        # 内部主机名
    ("history_search", "project-nightingale"),         # 代号
])
def test_a_token_from_the_error_body_never_reaches_the_trace(tool, secret):
    """工具正文是**不可信输入**:它可能被用户内容插值过。"""
    detail = _detail(tool, f"error: {secret}")

    assert secret not in _flat(detail), (
        f"{tool} 的错误正文里的 {secret!r} 原样进了 trace:{detail}\n"
        "detail 只该有码,不该有内容"
    )


def test_the_failure_is_still_recorded_as_a_failure():
    """收紧不能变成"把失败也吞了" —— 归因信息必须还在。"""
    detail = _detail("workspace_read", "error: my_private_notes.txt")

    assert detail["result_status"] == "err"
    assert detail["result_kind"] == "error"
    assert detail.get("error_code"), "失败必须留下一个码,哪怕是通用的"
    assert detail["tool"] == "workspace_read", "至少要知道是哪个工具失败的"


# --------------------------------------------------------------------------- #
# 成功正文同样不许落盘
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tool,content", [
    ("reply", "今天走了 8432 步,心率 72"),
    ("perception_snapshot", '{"steps": {"step_count": 8432}}'),
    ("memory_fetch", "她妈妈上周做了检查"),
    ("workspace_read", "银行卡号 6222 0000 1111 2222"),
])
def test_a_successful_result_body_never_reaches_the_trace(tool, content):
    flat = _flat(_detail(tool, content))

    for fragment in ("8432", "心率", "妈妈", "6222"):
        if fragment in content:
            assert fragment not in flat, f"{tool} 的返回内容进了 trace:{flat}"


# --------------------------------------------------------------------------- #
# 参数:只有感知的信号名可以留,其余一律不留
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("tool,args", [
    ("workspace_write", {"path": "/workspace/情书.md", "content": "我喜欢你"}),
    ("web_search", {"query": "抑郁症 自测"}),
    ("memory_search", {"query": "她的生日"}),
    ("send_file", {"path": "/workspace/体检报告.pdf"}),
    ("generate_image", {"prompt": "画一张她的画像"}),
])
def test_non_perception_tool_args_are_dropped_entirely(tool, args):
    """搜索词、路径、提示词都是**最敏感**的那一档,一个字都不能留。"""
    detail = _detail(tool, "ok", args=args)

    assert detail["args"] == {}, f"{tool} 的参数泄漏进了 trace:{detail['args']}"
    flat = _flat(detail)
    for value in args.values():
        assert str(value) not in flat


def test_memory_discovery_reuse_is_visible_without_exposing_the_query():
    query = "她的生日"
    tc = SimpleNamespace(name="memory_search", args={"query": query})
    result = worker.ToolResult(
        call_id="call_1",
        content="ok: this memory discovery was already completed",
        metadata={"memory_discovery_reused": True},
    )

    detail = worker._v2_tool_trace_detail(
        tc,
        event_kind="tool_call_result",
        result=result,
        duration_ms=1.0,
    )

    assert detail["memory_discovery_reused"] is True
    assert query not in _flat(detail)


def test_perception_keeps_only_catalog_signal_names():
    """信号名是封闭词表、非用户内容,留着才有诊断价值;夹带的自由串必须掉。"""
    detail = _detail(
        "perception_snapshot", "ok",
        args={"signals": ["steps", "sleep", "我的秘密", "../../etc/passwd"]},
    )

    signals = detail["args"].get("signals") or []
    assert "steps" in signals and "sleep" in signals, "合法信号名不该被丢掉"
    assert "我的秘密" not in signals
    assert "../../etc/passwd" not in signals
    assert "我的秘密" not in _flat(detail)


# --------------------------------------------------------------------------- #
# 精确错误码的闸在**生产者**那一层,不在 worker
#
# worker 读 metadata["perception_error_code"] 时只 sanitize 字符集、不查白名单 ——
# 我 review 时据此以为又漏了,其实是我手工往 metadata 里塞值绕过了生产者。
# 真正的边界在 capabilities/activity_metadata.py:能进 telemetry 的只有精确
# 白名单里的 domain slug,其余一律退回粗粒度 capability code。
# 所以断言钉在那里 —— 钉错层会得到一条永远绿的假测试。
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("dirty", [
    "my_private_notes.txt",
    "girlfriend_liuyu",
    "internal.corp.example.com",
    "2026-tax-return.pdf",
])
def test_only_allowlisted_slugs_become_telemetry(dirty):
    """capability 的 error message 是自由文本,永远不许原样变成错误码。"""
    from capabilities import activity_metadata

    metadata = activity_metadata.perception_result_metadata(
        "perception_snapshot",
        {"ok": False, "error": {"code": "invalid", "message": dirty}},
    )

    assert metadata.get("perception_error_code") != dirty, (
        f"自由文本 {dirty!r} 直接变成了 telemetry:{metadata}"
    )
    assert dirty not in _flat(metadata)


def test_the_allowlisted_domain_slug_still_survives():
    """收紧不能把诊断价值一起收掉 —— unknown_signals 正是这次定位的关键码。"""
    from capabilities import activity_metadata

    metadata = activity_metadata.perception_result_metadata(
        "perception_snapshot",
        {"ok": False, "error": {"code": "invalid", "message": "unknown_signals"}},
    )

    assert metadata["perception_error_code"] == "unknown_signals"


def test_omitted_signals_are_recorded_as_the_default_set():
    """「模型没点名」本身就是这次事故的关键证据,必须看得出来。"""
    detail = _detail("perception_snapshot", "ok", args={})

    assert detail["args"].get("defaulted") is True, (
        "看不出模型用了默认集,就复现不了「问步数却拿到天气」那条链"
    )
