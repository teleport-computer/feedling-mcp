"""用户 MCP 工具面必须可观测 —— 这是 usr_1baf(2026-08-09)那次查不动的直接原因。

用户报「MCP 测试连接通过,AI 却总说搜不到」。查下来**我们根本没有埋点**:

- 桥把工具面只打进自己的 stderr,不进任何 trace;
- MCP 工具调用不经 io_cli,所以 `agent.tool.call` 里**永远**看不到它们
  (我一度差点把"trace 里没有 MCP 调用"当成"模型没调 MCP"的证据 —— 那是错的);
- 于是「这一轮模型到底看得到哪些 MCP 工具」在生产上完全不可观测,只能猜。

这个文件锁住三层静默失败都会留声。
"""
import json
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
    assert kw["detail"]["has_grant_rule"] is False
    assert "需要授权" in kw["explain"]


def test_claude_fully_wired_is_ok():
    kind, kw = _wiring([
        "claude", "--mcp-config=/tmp/x.json",
        "--allowed-tools=mcp__tavily__*,mcp__gaodemap__*", "--print",
    ])[0]
    assert kind == "mcp.surface.wired"
    assert kw["status"] == "ok"
    assert kw["detail"]["has_grant_rule"] is True


def test_claude_partial_grant_names_only_the_ungranted_server():
    """授权是逐台的:漏掉一台,那台的工具就调不了,不能整体判绿。

    旧判据只问「有没有 --allowed-tools 这个参数」,所以两台里只授权一台也报
    authorized=true —— 恰好把「某个工具用不了」这种局部失败盖掉。
    """
    kind, kw = _wiring([
        "claude", "--mcp-config=/tmp/x.json",
        "--allowed-tools=mcp__tavily__*", "--print",
    ], env={"CLAUDE_CONFIG_DIR": ""})[0]
    assert kind == "mcp.surface.wired"
    assert kw["status"] == "error"
    assert kw["detail"]["has_grant_rule"] is False
    assert kw["detail"]["ungranted"] == ["gaodemap"]


def test_hosted_claude_is_authorized_by_settings_json_not_by_a_flag(tmp_path):
    """托管路线没有 MCP 的 allowlist 参数也算授权 —— 规则在 settings.json 里。

    2.1.217 的四格矩阵证明 settings.json 单独就够(两条来源是并集)。不认这条的
    话,托管用户每轮都会被误报成「未授权」,把真问题淹掉。
    """
    (tmp_path / "settings.json").write_text(json.dumps({"permissions": {"allow": [
        "mcp__tavily__*", "mcp__gaodemap__*", "Bash(io_cli:*)",
    ]}}))
    kind, kw = _wiring(["claude", "--mcp-config=/tmp/x.json", "--print"],
                       env={"CLAUDE_CONFIG_DIR": str(tmp_path)})[0]
    assert kind == "mcp.surface.wired"
    assert kw["status"] == "ok"
    assert kw["detail"]["has_grant_rule"] is True


def test_hosted_shaped_grant_without_mcp_rules_is_reported_unauthorized(tmp_path):
    """托管形状:模板恒带 --allowed-tools、环境恒设 CLAUDE_CONFIG_DIR,
    而两者都**不含任何 mcp 规则**。

    旧判据(`有 --allowed-tools` OR `CLAUDE_CONFIG_DIR 非空`)对这个形状永远
    返回 authorized=true —— 它唯一该报的状态,恰恰是它报不出来的。判据必须看
    规则内容,不是看 flag / 环境变量存不存在。
    """
    (tmp_path / "settings.json").write_text(json.dumps({"permissions": {"allow": [
        "Bash(io_cli:*)", "Read(//home/agent/images/**)",
    ]}}))
    kind, kw = _wiring([
        "claude", "--mcp-config=/tmp/x.json",
        "--allowed-tools=Bash(io_cli:*),Read(//home/agent/files/**)", "--print",
    ], env={"CLAUDE_CONFIG_DIR": str(tmp_path)})[0]
    assert kind == "mcp.surface.wired", "接线本身是好的 —— 坏的是授权"
    assert kw["status"] == "error"
    assert kw["detail"]["has_grant_rule"] is False
    # 排序后再比:名单顺序跟的是启用顺序,不是这条断言想锁的东西。
    assert sorted(kw["detail"]["ungranted"]) == ["gaodemap", "tavily"]


def test_per_tool_grant_is_reported_as_partial_not_as_full_authorization(tmp_path):
    """逐工具授权是**真的**授权,但只授权了那一个工具。

    把它当成整台已授权,等于把「这台服务器其余工具全被拒」报成一切正常;
    一条指向已不存在的工具名的规则也会同样假绿。所以它单列 partial_grants,
    既不算缺失(不误报),也不冒充完整授权(不假绿)——最终判据是调用时的
    permission_denials(codex 审出)。
    """
    (tmp_path / "settings.json").write_text(json.dumps({"permissions": {"allow": [
        "mcp__tavily__search", "mcp__gaodemap__*",
    ]}}))
    kind, kw = _wiring(["claude", "--mcp-config=/tmp/x.json", "--print"],
                       env={"CLAUDE_CONFIG_DIR": str(tmp_path)})[0]
    assert kw["status"] == "ok", "有规则就不算缺失,不该报错"
    assert kw["detail"]["partial_grants"] == ["tavily"]
    assert "gaodemap" not in kw["detail"].get("partial_grants", [])
    assert "ungranted" not in kw["detail"]
    assert "只授权了具体工具" in kw["explain"]


def test_detail_does_not_claim_authorization_it_cannot_prove(tmp_path):
    """字段名不能叫 authorized —— 这是对我们自己文件做的前置检查。

    它能证明授权**缺失**(要抓的失败),证明不了授权**有效**。名字叫 authorized
    的话,读 trace 的人会拿它当「能调用」的结论,而真正的判据在 permission_denials。
    """
    (tmp_path / "settings.json").write_text(json.dumps({"permissions": {"allow": [
        "mcp__tavily__*", "mcp__gaodemap__*",
    ]}}))
    _kind, kw = _wiring(["claude", "--mcp-config=/tmp/x.json", "--print"],
                        env={"CLAUDE_CONFIG_DIR": str(tmp_path)})[0]
    assert "authorized" not in kw["detail"]
    assert kw["detail"]["has_grant_rule"] is True


def test_variadic_allowlist_reads_every_value_not_just_the_first(tmp_path):
    """`--allowedTools "r1" "r2"` 是官方形状(变参)。

    只读紧跟的那一个 token,第二台就会被假报成 ungranted —— 而这个埋点的整个
    意义就是分辨真缺失(codex 审出)。
    """
    _kind, kw = _wiring([
        "claude", "--mcp-config=/tmp/x.json",
        "--allowedTools", "mcp__tavily__*", "mcp__gaodemap__*", "--print",
    ], env={"CLAUDE_CONFIG_DIR": ""})[0]
    assert kw["status"] == "ok"
    assert kw["detail"]["has_grant_rule"] is True


def test_space_separated_allowlist_value_is_read():
    """`--allowed-tools <值>` 和 `--allowed-tools=<值>` 都得认。

    两种写法在野外都有:本仓模板用 `=` 绑定,官方文档写分开。只解析一种,另一
    种就会被读成「一条规则都没有」,把授权好的用户报成未授权。
    """
    kind, kw = _wiring([
        "claude", "--mcp-config=/tmp/x.json",
        "--allowed-tools", "mcp__tavily__*,mcp__gaodemap__*", "--print",
    ], env={"CLAUDE_CONFIG_DIR": ""})[0]
    assert kw["status"] == "ok"
    assert kw["detail"]["has_grant_rule"] is True


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


# --- postflight:CLI 自报的注册结果 ------------------------------------------
# preflight(wiring)只能证明「我们交过去了」。真正回答「模型这一轮看得到哪几台」
# 的是 claude 自己的 init 事件 —— 生产上一条 `mcp_servers: []` 正是这类问题的
# 第一份硬证据(usr_98947,2026-08-10)。

def _init_line(servers):
    return json.dumps({
        "type": "system", "subtype": "init",
        "tools": ["Bash", "Read"], "mcp_servers": servers,
        "model": "deepseek-v4-pro",
    }) + "\n"


def _registered(stdout, *, lane="chat", enabled=("tavily",), cmd=None,
                attempt="first"):
    captured = []
    applied = {"servers": [{"name": n, "enabled": True} for n in enabled]}
    with patch.object(crc, "_emit_debug_trace",
                      side_effect=lambda ss, t, **kw: captured.append((t, kw))), \
         patch.object(crc, "_user_mcp_applied", applied):
        crc._trace_user_mcp_registered(
            stdout, list(cmd or ["claude", "--print"]), trace_id="t1", lane=lane,
            attempt=attempt)
    return captured


def _stream(servers, calls=()):
    """init 事件 + 若干真实 tool_use/tool_result 对。

    calls 里每一项是 (服务器名, 成功?)。这是判据的第二个来源 —— 模型的散文
    不能当证据,它会声称自己调用了从没碰过的工具(本机实测撞到过)。
    """
    lines = [json.dumps({
        "type": "system", "subtype": "init",
        "tools": ["Bash"], "mcp_servers": servers, "model": "m",
    })]
    for i, (srv, ok) in enumerate(calls):
        tid = f"tu_{i}"
        lines.append(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": tid, "name": f"mcp__{srv}__do", "input": {}}]}}))
        lines.append(json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": tid, "is_error": not ok,
             "content": "pong" if ok else "boom"}]}}))
    return "\n".join(lines) + "\n"


def test_connected_at_init_and_never_called_is_ok():
    kind, kw = _registered(_stream([{"name": "tavily", "status": "connected"}]))[0]
    assert kind == "mcp.surface.registered"
    assert kw["status"] == "ok"
    assert kw["detail"]["verdict"] == {"tavily": "ok"}


def test_pending_at_init_then_a_successful_call_is_recovered_not_a_failure():
    """启动时没就绪、随后调通 —— 这是**实测出来的真实形态**,不是我想象的。

    见 fixture claude_init_pending_tool_recovered.jsonl(本机真实录制)。
    把这种轮次报成失败,正是这条埋点要防的假信号掉了个头:一个专门抓假绿的
    埋点开始产假红,读的人很快就会学会忽略它。
    """
    raw = _stream([{"name": "slow", "status": "pending"}], [("slow", True)])
    kind, kw = _registered(raw, enabled=("slow",))[0]
    assert kw["status"] == "ok", "调通了就不是失败"
    assert kw["detail"]["verdict"] == {"slow": "recovered"}
    assert kw["detail"]["init_status"] == {"slow": "pending"}, (
        "启动快照要原样留着 —— 它是唯一能解释「为什么需要恢复」的东西")
    assert "已恢复" in kw["explain"]


def test_pending_and_never_called_is_inconclusive_not_an_error():
    """「模型没调用」不等于「模型调不了」。

    绝大多数轮次模型根本没有理由用某个工具。把这种判成失败,等于给每个装了
    MCP 的用户每轮刷一条假告警。
    """
    kind, kw = _registered(_stream([{"name": "slow", "status": "pending"}]),
                           enabled=("slow",))[0]
    assert kw["status"] == "ok"
    assert kw["detail"]["verdict"] == {"slow": "inconclusive"}
    assert "无法判定" in kw["explain"]


def test_hard_init_states_without_a_successful_call_are_failures():
    """failed / needs-auth / 压根没出现在名单里 —— 这三种是强证据。

    `needs-auth` 是本机实测真实出现过的状态(claude.ai 系服务器),我原来那版
    白名单会把它判绿。
    """
    for servers, enabled, why in (
        ([{"name": "s", "status": "failed"}], ("s",), "failed"),
        ([{"name": "s", "status": "needs-auth"}], ("s",), "needs-auth"),
        ([], ("s",), "整台没出现"),
    ):
        kind, kw = _registered(_stream(servers), enabled=enabled)[0]
        assert kw["status"] == "error", why
        assert kw["detail"]["verdict"] == {"s": "failed"}, why


def test_a_successful_call_overrides_a_failed_init_state():
    """同一轮里后来真的调通了,终态必须能覆盖启动时的失败。

    否则 dashboard 的 any_error 会因为一个在用户看见之前就自愈了的状态,
    把整轮永久染红(codex 审出)。
    """
    raw = _stream([{"name": "s", "status": "failed"}], [("s", True)])
    _kind, kw = _registered(raw, enabled=("s",))[0]
    assert kw["status"] == "ok"
    assert kw["detail"]["verdict"] == {"s": "recovered"}


def test_an_errored_tool_result_is_a_failure_even_if_init_looked_fine():
    raw = _stream([{"name": "s", "status": "connected"}], [("s", False)])
    _kind, kw = _registered(raw, enabled=("s",))[0]
    assert kw["status"] == "error"
    assert kw["detail"]["verdict"] == {"s": "failed"}
    assert kw["detail"]["called_error"] == ["s"]


def test_mixed_servers_are_judged_independently():
    raw = _stream([{"name": "good", "status": "connected"},
                   {"name": "slow", "status": "pending"},
                   {"name": "dead", "status": "failed"}],
                  [("slow", True)])
    _kind, kw = _registered(raw, enabled=("good", "slow", "dead"))[0]
    assert kw["detail"]["verdict"] == {
        "dead": "failed", "good": "ok", "slow": "recovered"}
    assert kw["status"] == "error", "有一台是硬失败,整条就该报 error"


def test_the_real_recorded_transcript_is_classified_as_recovered():
    """回归锁:真实录制的 stdout,不是手写 shape。

    手写 fixture 比生产更规整,正是这条判据第一版栽跟头的原因 —— 它当时把
    init 快照当终态,而真实那一轮 pending 之后是调通的。
    """
    raw = (Path(__file__).parent / "fixtures"
           / "claude_init_pending_tool_recovered.jsonl").read_text()
    _kind, kw = _registered(raw, enabled=("fast", "slow"))[0]
    assert kw["detail"]["init_status"] == {"fast": "connected", "slow": "pending"}
    assert kw["detail"]["verdict"] == {"fast": "ok", "slow": "recovered"}
    assert kw["status"] == "ok"


def test_last_init_wins_when_a_turn_was_retried():
    """重试会新起进程重做握手,前一次的结果不再描述这一轮。"""
    raw = _stream([]) + _stream([{"name": "tavily", "status": "connected"}])
    _kind, kw = _registered(raw)[0]
    assert kw["status"] == "ok"
    assert kw["detail"]["verdict"] == {"tavily": "ok"}


def test_registered_trace_is_silent_where_an_empty_list_is_correct():
    """非 chat 通道 / 没有启用的服务器 / 非 claude —— `[]` 都是**对**的。

    MCP 只在 chat 通道下发,所以蒸馏、心跳这些轮次本来就该是空的。不判这几个
    条件的话,每天每个用户都会刷出一堆假 error,把真信号淹掉 —— 这条埋点的
    目的正好相反。
    """
    assert _registered(_init_line([]), lane="proactive") == []
    assert _registered(_init_line([]), enabled=()) == []
    assert _registered(_init_line([]), cmd=["pi", "run"]) == []


def test_no_structured_init_means_no_observation_not_a_failure():
    """输出里没有 init 事件时保持沉默 —— 我们没有观测,不能编一个出来。

    对着整段 stdout 做正则是另一种编:工具回显里出现一句同形文本就会伪造出
    一条事件(同一个坑在 pi 那条埋点上真发生过)。
    """
    assert _registered("not json at all\n") == []
    assert _registered(json.dumps({"type": "result", "subtype": "success"})) == []


# --- 配置刷新链:每一种失败都必须留声 ----------------------------------------
# 这条链以前**整条静默**:keyless 兜底写 log.error、异常写 log.warning,两条都
# 落在没人看的容器日志里,而用户那边只看到「AI 说用不了我的工具」。轮次级的
# mcp.surface.* 也盖不住 —— 它们在「零台启用」时早退,而那正是静默失败的产物。

def _apply(advertised, *, servers=None, fetch_exc=None, api_key="k",
           paths_pinned=True, prior=None):
    captured = []
    servers = servers if servers is not None else []
    payload = {"fingerprint": advertised,
               "servers": [{"name": s["name"], "enabled": s["enabled"],
                            "config_envelope": {"id": "e"}} for s in servers]}

    def fake_fetch():
        if fetch_exc:
            raise fetch_exc
        return payload

    with patch.object(crc, "_emit_debug_trace",
                      side_effect=lambda ss, t, **kw: captured.append((t, kw))), \
         patch.object(crc, "_user_mcp_advertised", {"fingerprint": advertised}), \
         patch.object(crc, "_user_mcp_applied",
                      prior or {"fingerprint": None, "servers": []}), \
         patch.object(crc, "_fetch_user_mcp_envelopes", fake_fetch), \
         patch.object(crc, "_decrypt_envelope",
                      lambda env: json.dumps({"url": "https://x", "headers": {}})), \
         patch.object(crc, "_materialize_user_mcp", lambda *a, **k: None), \
         patch.object(crc, "FEEDLING_API_KEY", api_key), \
         patch.object(crc, "_USER_MCP_PATHS_PINNED", paths_pinned):
        crc._maybe_apply_user_mcp()
    return captured


def _reset_materialize_dedup():
    crc._user_mcp_trace_last = None


def test_materialize_success_records_configured_and_enabled_counts():
    _reset_materialize_dedup()
    events = _apply("sha256:abc", servers=[
        {"name": "tavily", "enabled": True},
        {"name": "gaodemap", "enabled": False},
    ])
    assert len(events) == 1
    kind, kw = events[0]
    assert kind == "mcp.materialize.applied"
    assert kw["status"] == "ok"
    assert kw["detail"]["configured_count"] == 2
    assert kw["detail"]["enabled_count"] == 1


def test_all_servers_switched_off_is_called_out_in_the_explain():
    """存了但一台都没开 —— 对用户来说和「配置没生效」一模一样。

    这正是最难查的那种:fingerprint 非空、apply 成功、日志一切正常,而模型
    看不到任何工具。不在文案里点破,读 trace 的人会以为没问题。
    """
    _reset_materialize_dedup()
    kind, kw = _apply("sha256:off", servers=[
        {"name": "tavily", "enabled": False},
    ])[0]
    assert kind == "mcp.materialize.applied"
    assert kw["detail"]["enabled_count"] == 0
    assert "没有一台是启用状态" in kw["explain"]


def test_fetch_failure_is_traced_with_the_exception_type_only():
    """失败要留声,但只留异常类型。

    这里的失败是 fetch/decrypt/写盘,消息里可能带用户的 MCP url 或远端返回的
    正文 —— 那些都不该进 trace。
    """
    _reset_materialize_dedup()
    kind, kw = _apply("sha256:bad",
                      fetch_exc=RuntimeError("https://secret.example/mcp 500 body"))[0]
    assert kind == "mcp.materialize.failed"
    assert kw["status"] == "error"
    assert kw["detail"]["failure"] == "RuntimeError"
    dumped = json.dumps(kw, ensure_ascii=False)
    assert "secret.example" not in dumped


def test_keyless_unpinned_paths_failsafe_is_traced():
    """兜底关掉 user MCP 时也必须留声 —— 以前只有一行 log.error。"""
    _reset_materialize_dedup()
    kind, kw = _apply("sha256:x", api_key="", paths_pinned=False)[0]
    assert kind == "mcp.materialize.failed"
    assert kw["detail"]["failure"] == "paths_unpinned"


def test_failure_copy_gives_the_action_that_matches_that_failure():
    """排障文案必须对应各自的下一步动作,不能一律写「会重试」。

    paths_unpinned 这条记下 fingerprint 就 return,下次 poll 因 fingerprint
    相等直接早退 —— **本进程永远不会再试**;而这个标志是进程启动时定的,改完
    env 也得重启。给出相反的动作比没有文案更坏:读的人会干等一个不会发生的重试
    (codex 审出)。
    """
    _reset_materialize_dedup()
    _kind, unpinned = _apply("sha256:u", api_key="", paths_pinned=False)[0]
    assert "下次 poll 会重试" not in unpinned["explain"]
    assert "不会重试" in unpinned["explain"]
    assert "重启" in unpinned["explain"]

    # 反过来:真正会重试的那一族,文案必须保留重试指引。
    _reset_materialize_dedup()
    _kind, transient = _apply("sha256:t", fetch_exc=RuntimeError("boom"))[0]
    assert "下次 poll 会重试" in transient["explain"]
    assert "重启" not in transient["explain"]


def test_repeated_failures_on_the_same_fingerprint_emit_once():
    """apply 每次 poll 都重试。不去重的话,一个持续失败的用户会把 200 条的
    trace 环刷光 —— 恰好冲掉我们要读的那些轮次(一轮蒸馏就有 ~198 条)。"""
    _reset_materialize_dedup()
    first = _apply("sha256:same", fetch_exc=RuntimeError("boom"))
    second = _apply("sha256:same", fetch_exc=RuntimeError("boom"))
    assert len(first) == 1
    assert second == [], "同一份配置的同一种失败只报一次"
    # 换一份配置就是新状态,必须重新报
    assert len(_apply("sha256:other", fetch_exc=RuntimeError("boom"))) == 1


def test_recovery_after_a_failure_is_reported():
    """失败后修好了要能看见,否则读 trace 的人停在最后一条 error 上。"""
    _reset_materialize_dedup()
    _apply("sha256:f", fetch_exc=RuntimeError("boom"))
    kind, _kw = _apply("sha256:f", servers=[{"name": "tavily", "enabled": True}])[0]
    assert kind == "mcp.materialize.applied"


def test_empty_fingerprint_is_not_reported_as_a_fault():
    """后端的 fingerprint 是对**已保存列表**算的,空 = 用户确实一台都没存。

    那是正常状态。把它报成 error,读的人很快就会学会忽略这个事件。
    """
    _reset_materialize_dedup()
    assert _apply("") == []
