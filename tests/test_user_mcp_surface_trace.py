"""用户 MCP 工具面必须可观测 —— 这是 usr_1baf(2026-08-09)那次查不动的直接原因。

用户报「MCP 测试连接通过,AI 却总说搜不到」。查下来**我们根本没有埋点**:

- 桥把工具面只打进自己的 stderr,不进任何 trace;
- MCP 工具调用不经 io_cli,所以 `agent.tool.call` 里**永远**看不到它们
  (我一度差点把"trace 里没有 MCP 调用"当成"模型没调 MCP"的证据 —— 那是错的);
- 于是「这一轮模型到底看得到哪些 MCP 工具」在生产上完全不可观测,只能猜。

这个文件锁住三层静默失败都会留声。
"""
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

for _k, _v in {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "cli",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_mcp_surface_checkpoint.json",
}.items():
    os.environ.setdefault(_k, _v)

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake = types.ModuleType("content_encryption")
    _fake.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake)

import tools.chat_resident_consumer as crc  # noqa: E402

# 桥真实输出的那一行(index.js)。**这个 fixture 就是契约** —— 改桥的格式必须
# 同步改这里,否则解析静默失效而测试照绿。
_SURFACE = (
    "[user_mcp] surface servers=3 registered=41 dropped=0 cap=100 bytes=7300 "
    "detail=gaodemap:12/12,gardenforum:25/25,tavily:4/4\n"
)
# `服务器:注册数/发现数`。**注册数才回答「它到底进没进去」** —— 第一版只报发现数,
# 于是 tavily 全被丢掉时日志照样写着 tavily:4,恰好把这条埋点要答的问题答错。
_SURFACE_CAPPED = (
    "[user_mcp] surface servers=6 registered=100 dropped=7 cap=100 bytes=9001 "
    "detail=game:8/8,gaodemap:12/12,gardenforum:25/25,"
    "luckin-coffee:26/30,mcdonalds:25/28,tavily:4/4\n"
    "[user_mcp] tool cap 100 reached — dropped 7: luckin-coffee/t28, "
    "mcdonalds/t26\n"
)
# ⚠️ 这是**旧算法**(字母序截断)才可能产生的输出:轮转分配之后两台服务器时
# tavily 不可能是 0/4。保留它只为验**解析器**能如实转述「整台饿死」这种形态
# (万一将来又冒出来)。分配器本身的覆盖在 tests/test_pi_mcp_bridge.py,
# 那边是 harness 驱动的真实调用,不是手写字符串。
_SURFACE_STARVED = (
    "[user_mcp] surface servers=2 registered=100 dropped=4 cap=100 bytes=9500 "
    "detail=gardenforum:100/100,tavily:0/4\n"
    "[user_mcp] tool cap 100 reached — dropped 4: tavily/search\n"
)


def _traces(stderr, *, lane="chat", enabled=("gaodemap",), is_pi=True,
            attempt="first"):
    captured = []

    def fake_emit(subsystem, event_type, **kw):
        captured.append((event_type, kw))

    applied = {"servers": [{"name": n, "enabled": True} for n in enabled]}
    with patch.object(crc, "_emit_debug_trace", side_effect=fake_emit), \
         patch.object(crc, "_user_mcp_applied", applied):
        crc._trace_user_mcp_surface(
            stderr, trace_id="t1", lane=lane, is_pi=is_pi, attempt=attempt
        )
    return captured


def test_resolved_surface_is_traced_with_per_server_counts():
    """正常一轮:必须记下模型实际看到几个工具、每个服务器各贡献几个。

    没有 per-server 明细就答不了「tavily 到底有没有被注册进去」——
    而那正是用户问的问题。
    """
    events = _traces(_SURFACE)
    assert len(events) == 1
    kind, kw = events[0]
    assert kind == "mcp.surface.resolved"
    assert kw["status"] == "ok"
    assert kw["detail"]["registered"] == 41
    assert kw["detail"]["dropped"] == 0
    assert "tavily:4/4" in kw["detail"]["per_server"]
    assert kw["detail"]["schema_bytes"] == 7300
    assert kw["detail"]["driver"] == "pi"


def test_dropped_tools_are_traced_as_an_error_with_their_names():
    """撞上限必须报 error 并写清丢了谁。

    丢弃是按**服务器名字母序**发生的,所以排在后面的服务器整个消失 ——
    用户看到的是「测试连接通过,但 AI 说搜不到」,而丢弃在此之前只打进
    桥的 stderr,没有任何人看得到。
    """
    events = _traces(_SURFACE_CAPPED)
    kind, kw = events[0]
    assert kind == "mcp.surface.resolved"
    assert kw["status"] == "error", "工具被丢弃是异常,不能记成 ok"
    assert kw["detail"]["dropped"] == 7
    assert "luckin-coffee/t28" in kw["detail"]["dropped_names"]


def test_missing_surface_on_a_chat_turn_with_enabled_servers_is_an_error():
    """有启用的服务器却没有工具面 —— 桥没加载或启动失败,必须留声。

    这是最阴的一层:pi 在 `-e <bridge>` 指向的文件不存在时会静默降级,
    模型一个 MCP 工具都看不到,而整轮看起来完全正常。
    """
    events = _traces("", lane="chat", enabled=("gaodemap", "tavily"))
    assert len(events) == 1
    kind, kw = events[0]
    assert kind == "mcp.surface.missing"
    assert kw["status"] == "error"
    assert "tavily" in kw["detail"]["enabled_servers"]


def test_no_noise_when_mcp_legitimately_does_not_apply():
    """后台轮次、或没有启用任何服务器 —— 这两种情况没有工具面是**正常的**,
    不该报错。否则每次心跳唤醒都刷一条假告警,真问题会被淹掉。"""
    assert _traces("", lane="proactive", enabled=("gaodemap",)) == []
    assert _traces("", lane="chat", enabled=()) == []
    # claude 走 --mcp-config、codex 走 config.toml —— 两者都不经过我们的桥,
    # 本来就不会有 surface 行。不判 driver 的话,这些用户**每一轮**都会刷一条
    # 假 error 把 trace 环冲掉。我自己写出过这个 bug,commit 前验出来的。
    assert _traces("", lane="chat", enabled=("tavily",), is_pi=False) == []


def test_bridge_log_format_matches_what_the_parser_expects():
    """解析器与桥的输出格式必须对齐。

    这条是防「fixture 比生产更完整/更宽松」那类假绿:直接读 index.js 的源码,
    确认它真的会无条件输出 surface 行,而不是只在丢弃时才打。
    """
    bridge = (
        Path(__file__).parent.parent / "tools" / "pi_mcp_bridge" / "index.js"
    ).read_text(encoding="utf-8")
    assert "[user_mcp] surface servers=" in bridge
    # 必须在 if (dropped.length) 之外 —— 只在丢弃时才打就等于没有可观测性
    surface_at = bridge.index("[user_mcp] surface servers=")
    guard_at = bridge.index("if (dropped.length)")
    assert surface_at < guard_at, "surface 行必须无条件输出,不能藏在丢弃分支里"


def test_tool_cap_is_high_enough_for_a_realistic_multi_server_setup():
    """上限要能装下真实用户的配置。

    usr_1baf 装了 6 个服务器共 107 个工具。50 那版会把字母序靠后的 tavily
    整个丢掉;2026-08-10 统一到 **128**(实测得出,见
    tools/e2e/tool_count_ceiling_probe.py),这套配置一个都不用裁。

    这里读的是**当前**常量而不是写死数字 —— 写死的话调上限时测试会静默失配。
    """
    mapping = (
        Path(__file__).parent.parent / "tools" / "pi_mcp_bridge" / "tool_mapping.js"
    ).read_text(encoding="utf-8")
    import re as _re

    match = _re.search(r"export const MAX_TOOLS = (\d+);", mapping)
    assert match, "找不到 MAX_TOOLS"
    assert int(match.group(1)) >= 107, (
        f"上限 {match.group(1)} 装不下本月工具最多的真实用户(107 个)"
    )


def test_multiple_surface_lines_do_not_cross_wire_the_dropped_list():
    """多行时取**最后**一条,别把前一条的工具面配上后一条的丢弃名单。

    我第一版用 `search()` 取第一条 surface 行,却又独立 `search()` 丢弃行 ——
    端到端喂真实桥输出时立刻撞出来:报了 `dropped=0` 却附着别人的丢弃名单。
    生产里一轮通常只有一行,所以这个错在单测里永远看不见 —— 必须拿真实输出喂。
    """
    events = _traces(_SURFACE + _SURFACE_CAPPED)
    assert len(events) == 1
    _kind, kw = events[0]
    # 最后一条才是本轮生效的
    assert kw["detail"]["registered"] == 100
    assert kw["detail"]["dropped"] == 7
    assert "tavily" in kw["detail"]["per_server"]


def test_a_starved_server_is_reported_as_zero_registered_not_as_discovered():
    """一台服务器被完全饿死时,detail 必须写 `tavily:0/4`,不能写 `tavily:4`。

    这正是这条埋点存在的理由:用户问的就是「我加的 tavily 到底进没进去」。
    第一版报的是**丢弃前**的发现数 —— 全被丢掉也照样显示 4,
    等于把要回答的那个问题答错了(codex 审出)。
    """
    events = _traces(_SURFACE_STARVED)
    _kind, kw = events[0]
    assert "tavily:0/4" in kw["detail"]["per_server"], (
        f"饿死的服务器没有被如实报告:{kw['detail']['per_server']}"
    )
    assert kw["status"] == "error"


def test_each_attempt_is_labelled_with_which_attempt_it_was():
    """重试要单独记一条,并且**标明是哪一次**。

    我原以为「重试跑同一条命令、同一份配置,工具面相同」所以只记首次 ——
    错的:重试会**新起进程、重做 MCP 握手**,首次可能某台没连上而重试连上了。

    ⚠️ 这条的第一版名叫「每次尝试都单独记」,却两次都用默认的 attempt="first"
    还只断言「键存在」—— 名字声称的东西一个都没测(codex 审出)。
    """
    first = _traces(_SURFACE, attempt="first")[0][1]
    retry = _traces(_SURFACE, attempt="stream_cut_retry")[0][1]
    assert first["detail"]["attempt"] == "first"
    assert retry["detail"]["attempt"] == "stream_cut_retry"


def test_the_missing_event_also_says_which_driver_lane_and_attempt():
    """missing 事件同样要能定位到是哪一次尝试。

    「首次没工具面、重试有」是真实存在的场景;missing 不带 attempt 就没法
    把两条事件对上号(codex 审出)。
    """
    _kind, kw = _traces(
        "", lane="chat", enabled=("tavily",), attempt="stale_resume_retry"
    )[0]
    assert kw["detail"]["driver"] == "pi"
    assert kw["detail"]["lane"] == "chat"
    assert kw["detail"]["attempt"] == "stale_resume_retry"


def test_a_clean_turn_never_carries_someone_elses_dropped_names():
    """没有丢弃时,dropped_names 必须为空 —— 不能从别处捡一份挂上去。"""
    events = _traces(_SURFACE_CAPPED.splitlines(keepends=True)[-1] + _SURFACE)
    _kind, kw = events[0]
    assert kw["detail"]["dropped"] == 0
    assert kw["detail"]["dropped_names"] == ""


# ── 全路径覆盖(2026-08-10)────────────────────────────────────────────
#
# PR#174 暴露的最大缺漏:pi 那条修了、V2 整条被漏掉,而公开 changelog 却按通用
# 措辞宣称修好了。所以这里按**路径矩阵**逐条锁,而不是只锁自己最熟的那条。
#
#   路径                     截断             可观测
#   Hosted V2                轮转 64/65536    mcp.surface.resolved(serve_worker)
#   V1/自托管 + pi           轮转 100         mcp.surface.resolved(桥 → consumer)
#   V1/自托管 + claude       无(全量下发)   mcp.surface.wired / .missing
#   V1/自托管 + codex        无(config.toml) mcp.surface.wired / .missing


def _wiring(cmd, *, lane="chat", enabled=("tavily", "gaodemap"), env=None):
    import os

    captured = []
    applied = {"servers": [{"name": n, "enabled": True} for n in enabled]}
    with patch.object(crc, "_emit_debug_trace",
                      side_effect=lambda ss, t, **kw: captured.append((t, kw))), \
         patch.object(crc, "_user_mcp_applied", applied), \
         patch.dict(os.environ, env or {}, clear=False):
        crc._trace_user_mcp_wiring(list(cmd), trace_id="t1", lane=lane)
    return captured


def test_self_hosted_claude_without_mcp_config_is_reported_as_not_wired():
    """PR#174 修的那个洞必须留声。

    自托管 operator 照旧版文档写的 `AGENT_CLI_CMD` 没有 `{mcp}` 占位符,
    `--mcp-config` 一次都没下发 —— App 里配的服务器**一台都到不了 agent**,
    而 App 的连接测试是绿的(那是控制面探针直连服务器,两条路)。
    这种情况以前完全不可观测,只能靠用户报。
    """
    events = _wiring(["claude", "--print", "--output-format", "json"],
                     env={"CLAUDE_CONFIG_DIR": ""})
    assert len(events) == 1
    kind, kw = events[0]
    assert kind == "mcp.surface.missing"
    assert kw["status"] == "error"
    assert kw["detail"]["wired"] is False
    assert kw["detail"]["driver"] == "claude"


def test_claude_wired_but_unauthorized_is_still_an_error():
    """只接线不授权 = 调用进 permission_denials,模型回「这个工具需要授权」。

    和用户原话一致(PR#174 实测)。这两个条件必须分开报,否则「已接线」会被
    当成「能用」。
    """
    kind, kw = _wiring(["claude", "--mcp-config=/tmp/x.json", "--print"],
                       env={"CLAUDE_CONFIG_DIR": ""})[0]
    assert kind == "mcp.surface.wired"
    assert kw["status"] == "error"
    assert kw["detail"]["authorized"] is False
    assert "需要授权" in kw["explain"]


def test_claude_fully_wired_is_ok():
    kind, kw = _wiring([
        "claude", "--mcp-config=/tmp/x.json",
        "--allowed-tools=mcp__tavily__*", "--print",
    ])[0]
    assert kind == "mcp.surface.wired"
    assert kw["status"] == "ok"
    assert kw["detail"]["authorized"] is True


def test_hosted_claude_is_authorized_by_settings_json_not_by_a_flag():
    """托管路线没有 allowlist 参数也算授权 —— 规则在我们生成的 settings.json 里。

    PR#174 的四格矩阵证明 settings.json 单独就够。不认这条的话,托管用户每轮
    都会被误报成「未授权」,把真问题淹掉。
    """
    kind, kw = _wiring(["claude", "--mcp-config=/tmp/x.json", "--print"],
                       env={"CLAUDE_CONFIG_DIR": "/tmp"})[0]
    assert kind == "mcp.surface.wired"
    assert kw["status"] == "ok"


def test_codex_without_config_home_is_reported_as_not_wired():
    kind, kw = _wiring(["codex", "exec"], env={"CODEX_HOME": ""})[0]
    assert kind == "mcp.surface.missing"
    assert kw["detail"]["driver"] == "codex"


def test_wiring_trace_is_silent_when_there_is_nothing_to_report():
    """后台轮次、没有启用的服务器、非 claude/codex driver —— 都不该出声。

    每轮刷一条假告警会把 200 条的 trace 环冲掉,真问题反而看不见。
    """
    assert _wiring(["claude", "--print"], lane="proactive") == []
    assert _wiring(["claude", "--print"], enabled=()) == []
    assert _wiring(["pi", "--mode", "json"]) == []
