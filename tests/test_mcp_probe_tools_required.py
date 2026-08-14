"""控制面「测试连接」必须对「连上了但没有工具」判失败

2026-08-14 真机发现：`https://mcp.amap.com/sse` 少了 `?key=` 时，返回 HTTP 200
但 body 是高德自己的格式 —— 没有 jsonrpc / result / error。旧判据把它读成
result={} → tools=[] → `{"ok": True, "tool_count": 0}`，于是 app 显示「已连接」，
聊天里 AI 说「我没有地图工具」。

这个症状和 2026-08-13 修的握手竞速**一模一样**，成因却完全不同 —— 真正的代价
不是这一台服务器，是以后收到「MCP 用不了」的投诉时，从用户描述里分不出是哪一种。

要 key 的 MCP 服务器是主流（受害用户配的 tavily_ / github_ 都是），所以这不是边界。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from hosted import mcp_probe  # noqa: E402


def test_amap_shaped_vendor_error_is_not_a_successful_connection():
    """HTTP 200 + 完全不是 JSON-RPC 的 body（实测的 amap 原样）。"""
    with pytest.raises(mcp_probe.ProbeError) as e:
        mcp_probe._tools_from_rpc(
            {"status": "0", "info": "INVALID_USER_KEY", "infocode": "10001"})
    assert e.value.kind == "protocol"
    assert "API key" in e.value.detail


def test_valid_envelope_with_an_empty_catalogue_is_not_ok():
    """协议合法但一个工具都没有 —— 鉴权失败降级成空目录的典型形状。"""
    with pytest.raises(mcp_probe.ProbeError) as e:
        mcp_probe._tools_from_rpc({"jsonrpc": "2.0", "id": 2, "result": {"tools": []}})
    assert e.value.kind == "no_tools"


def test_a_catalogue_of_only_nameless_entries_is_also_not_ok():
    """有条目但全是无名的 —— 过滤后等于空，不能因为 raw 非空就放行。"""
    with pytest.raises(mcp_probe.ProbeError) as e:
        mcp_probe._tools_from_rpc(
            {"result": {"tools": [{"description": "x"}, {"name": ""}]}})
    assert e.value.kind == "no_tools"


def test_a_real_catalogue_still_passes():
    out = mcp_probe._tools_from_rpc(
        {"result": {"tools": [{"name": "ask_question"}, {"name": "read_wiki"}]}})
    assert out["ok"] is True
    assert out["tool_count"] == 2
    assert out["tool_names"] == ["ask_question", "read_wiki"]


def test_a_jsonrpc_error_still_reports_protocol_not_no_tools():
    """既有行为不能被新判据抢走：显式 error 仍然是 protocol。"""
    with pytest.raises(mcp_probe.ProbeError) as e:
        mcp_probe._tools_from_rpc({"jsonrpc": "2.0", "error": {"code": -32601}})
    assert e.value.kind == "protocol"
