"""AUP 哨兵探针（`tools/e2e/aup_gate_probe.py`）自身的回归。

**为什么量具需要自己的测试**：2026-08-30 手跑红极值时抓到一个真 bug ——
探针在 `live/gate` 已经是 `PRODUCT_FAIL` 的那一轮返回了 `rc=0`，
因为退出码当时从 `worst()` 算，而 `probe_common.SEVERITY` 把 `BLOCKED_EVIDENCE`
排在 `PRODUCT_FAIL` **之前**、`BLOCKING` 又刻意不含它 ⇒ **阻断信号被一个
不阻断的信号盖住**。手测能发现它一次，只有测试能防止它第二次。

这些用例全部不碰网络、不调用 `claude`：外部边界一律 monkeypatch。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PROBE_PATH = REPO / "tools" / "e2e" / "aup_gate_probe.py"

# `_PURE_UNIT` 登记件必须**自带 sys.path 引导**（conftest 的既有约定）：
# `backend/` 只在"provision 到了 Postgres"那个分支里才被加进 sys.path，而 conftest
# 有一个 autouse fixture 会 `from hosted import setup_core`。不自己引导的话，
# 本文件在无 PG 机器上会 14 个 ERROR —— 而那正是它最该跑得起来的时候。
# （2026-08-30 实测过这个失败，不是照着别的文件抄的。）
sys.path.insert(0, str(REPO / "backend"))


def _load_probe():
    """按路径加载探针模块（tools/e2e 不是包的一部分，正常 import 进不来）。"""
    sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location("aup_gate_probe", PROBE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


probe = _load_probe()
pc = probe  # 结果常量从探针模块转出，避免测试自己抄一份 probe_common 的名字

from tools.e2e.probe_common import SEVERITY  # noqa: E402  —— 穷举面从生产词表派生


# --------------------------------------------------------------------------
# 退出码 —— 这一组直接钉住上面那个事故
# --------------------------------------------------------------------------
def test_all_pass_exits_zero():
    assert probe.qualification_exit_code([pc.PASS, pc.PASS], diagnostic=False) == 0


def test_product_fail_with_blocked_evidence_still_exits_nonzero():
    """事故复现格：PRODUCT_FAIL 与 BLOCKED_EVIDENCE 同时出现。

    `worst()` 在这组上返回 BLOCKED_EVIDENCE（它排得更靠前），而 BLOCKING 不含它。
    任何"先 worst 再查 BLOCKING"的写法都会在这里返回 0。
    """
    results = [pc.PASS, pc.PRODUCT_FAIL, pc.BLOCKED_EVIDENCE]
    # 先把这一组的前提钉死：worst 确实落在那个不阻断的词上。
    # 否则哪天 SEVERITY 顺序变了，本用例会在"事故已不可能发生"时继续绿，
    # 让人以为它还在守着什么。
    assert probe.worst(results) == pc.BLOCKED_EVIDENCE
    assert pc.BLOCKED_EVIDENCE not in pc.BLOCKING
    assert probe.qualification_exit_code(results, diagnostic=False) == 1
    # 诊断模式容忍 BLOCKED_EVIDENCE，但绝不容忍 PRODUCT_FAIL
    assert probe.qualification_exit_code(results, diagnostic=True) == 1


def test_blocked_evidence_alone_blocks_in_qualification_mode():
    """"没量到"不是放行 —— 文档里那句"只有 OVERALL: PASS 才算放行"必须在代码里成立。"""
    results = [pc.PASS, pc.BLOCKED_EVIDENCE]
    assert probe.qualification_exit_code(results, diagnostic=False) == 1
    # 只有显式 --diagnostic 才容忍它
    assert probe.qualification_exit_code(results, diagnostic=True) == 0


@pytest.mark.parametrize(
    "status",
    [pc.AGENT_ERROR, pc.BLOCKED_DEPLOYMENT, pc.PRODUCT_FAIL, pc.BLOCKED_EVIDENCE],
)
def test_every_non_pass_blocks_by_default(status):
    """穷举面从结果词表派生，不是我想到哪几个写哪几个。"""
    assert probe.qualification_exit_code([pc.PASS, status], diagnostic=False) == 1


def test_qualification_covers_every_declared_result_word():
    """词表里每一个非 PASS 的词都必须在默认模式下阻断。

    这条防的是"将来 probe_common 新增一个结果词，而退出码逻辑没跟上"——
    穷举集从 `SEVERITY` 派生，新增的词自动进来。
    """
    assert len(SEVERITY) >= 5, "词表意外变空/变短，穷举面已失去意义"
    for status in SEVERITY:
        expected = 0 if status == pc.PASS else 1
        assert probe.qualification_exit_code([status], diagnostic=False) == expected


# --------------------------------------------------------------------------
# 提示词组装 —— 必须真的读生产件，不许中间夹一份快照
# --------------------------------------------------------------------------
def _stub_assembly(monkeypatch, *, catalog="CATALOG", memory="MEM", file_block="FILE",
                   anchor="ANCHOR"):
    """把四个生产贡献源替换成可辨认的标记。

    ``_prepend_time_anchor_foreground`` 是**生产**那一个：它同时产出时间锚、
    回复语言规则**和它们之间的胶水**，所以这里连它一起打桩，签名保持一致。
    """
    class _Consumer:
        @staticmethod
        def _memory_read_prompt_block():
            return memory

        @staticmethod
        def _outbound_file_prompt_block():
            return file_block

        @staticmethod
        def _prepend_time_anchor_foreground(content, msg_unix_ts):
            return f"{anchor}\n\n{content}"

    monkeypatch.setattr(probe, "build_io_cli_catalog_segment", lambda: catalog)
    monkeypatch.setattr(probe, "_load_consumer", lambda: _Consumer)


def test_prompt_is_rebuilt_from_production_not_a_snapshot(monkeypatch):
    """改动任一生产贡献源 ⇒ 组装出的提示词跟着变。

    这是"不存快照"这条设计的可执行判据。初版把这四段存成 fixture，第一次上
    真检查时**三段全和分支对不上**（目录少两个参数、memory/file 段长度不符、
    回复语言整段改过措辞）——快照必漂，而漂了没人会发现。
    """
    _stub_assembly(monkeypatch)
    base = probe.render_prompt("INSTR")
    for label in ("CATALOG", "MEM", "FILE", "ANCHOR", "INSTR"):
        assert label in base, f"{label} 没有进入组装结果"

    # 逐个贡献源单独变动，断言输出确实跟着变（leave-one-out：一次只动一个）
    for kwarg, marker in (
        ("catalog", "CATALOG-MOVED"),
        ("memory", "MEM-MOVED"),
        ("file_block", "FILE-MOVED"),
        ("anchor", "ANCHOR-MOVED"),
    ):
        _stub_assembly(monkeypatch, **{kwarg: marker})
        moved = probe.render_prompt("INSTR")
        assert marker in moved, f"改了 {kwarg}，组装结果没跟上 ⇒ 中间夹了快照"
        assert moved != base


def test_outer_glue_is_byte_exact(monkeypatch):
    """最外层胶水**逐字节**钉死 —— 只断言"各 marker 出现过"是抓不到多一个换行的。

    2026-08-30 二版就是在这里多了一个 `\n`：`content` 当时以 `\n` 开头，而外层
    已经拼了 `\n\n`，于是 FILE 与时间锚之间是三个换行、生产是两个。
    marker-in 型断言对这种错误完全失明。
    """
    _stub_assembly(monkeypatch)
    user_msg = probe.USER_MESSAGE_PATH.read_text(encoding="utf-8")
    # 生产链：catalog\n memory\n file\n\n → _prepend_time_anchor_foreground(...)
    # 而桩里那个返回 f"{anchor}\n\n{content}"，content = SENTINEL\n\n user_msg
    expected = f"CATALOG\nMEM\nFILE\n\nANCHOR\n\nINSTR\n\n{user_msg}"
    assert probe.render_prompt("INSTR") == expected


def test_both_arms_differ_only_by_the_instruction(monkeypatch):
    """两臂共用一份模板 ⇒ 除 INSTRUCTION 外逐字节相同。

    探针的全部判别力建立在这一点上：canary 被拒而 live 通过，必须只能归因于
    那段文案本身。时间锚取真实当前时间，两臂各建一次模板就可能跨分钟边界，
    引入一处与 INSTRUCTION 无关的差异。
    """
    _stub_assembly(monkeypatch)
    template = probe.build_prompt_template()
    live = probe.render_prompt("LIVE_TEXT", template)
    canary = probe.render_prompt("CANARY_TEXT", template)
    assert live.replace("LIVE_TEXT", "@") == canary.replace("CANARY_TEXT", "@")
    assert live != canary


def test_instruction_is_stripped_like_production(monkeypatch):
    """生产两侧都是 `INSTRUCTION.strip()` 拼进去的；探针必须走同一条变换。"""
    _stub_assembly(monkeypatch)
    out = probe.render_prompt("   \n padded \n  ")
    assert "\n\npadded \n\n" not in out  # 只 strip 两端，不动内部
    assert "padded" in out
    assert " padded \n  " not in out


# --------------------------------------------------------------------------
# 两道拒跑闸：固定件被改坏 / 对照组失效
# --------------------------------------------------------------------------
def test_tampered_fixture_refuses_to_run(monkeypatch, tmp_path):
    """固定件内容指纹对不上 ⇒ AGENT_ERROR，且**不发出任何外部请求**。"""
    calls = []
    monkeypatch.setattr(probe, "_run_claude", lambda *a, **k: calls.append(a) or ("OK", ""))
    monkeypatch.setattr(probe, "_read_manifest", lambda: {"sha256": {"canary_instruction_v0.2.0.txt": "deadbeef"}})

    result = probe.run(timeout=1)
    statuses = [c["result"] for c in result["cases"]]
    assert pc.AGENT_ERROR in statuses
    assert calls == [], "指纹已经对不上，还是把提示词发出去了"
    assert probe.qualification_exit_code(statuses, diagnostic=False) == 1


def test_identical_canary_and_live_refuses_to_run(monkeypatch, tmp_path):
    """对照组与被测对象逐字相同 ⇒ 探针没有判别力，必须拒跑而不是报 PASS。

    第一版草稿正是这个形状：live 与 canary 是同一段文本，于是它**从未证明过
    自己能输出 PASS** —— 一个恒红的量具也能通过那种自测。
    """
    calls = []
    monkeypatch.setattr(probe, "_run_claude", lambda *a, **k: calls.append(a) or ("OK", ""))
    monkeypatch.setattr(probe, "_check_fixtures", lambda p, m: True)
    monkeypatch.setattr(probe, "_read_manifest", lambda: {})

    same = "IDENTICAL TEXT"
    fake_st = type(sys)("agent_protocol_core.self_thinking")
    fake_st.INSTRUCTION = same
    fake_apc = type(sys)("agent_protocol_core")
    fake_apc.self_thinking = fake_st
    monkeypatch.setitem(sys.modules, "agent_protocol_core", fake_apc)
    monkeypatch.setitem(sys.modules, "agent_protocol_core.self_thinking", fake_st)
    canary_file = tmp_path / "canary.txt"
    canary_file.write_text(same, encoding="utf-8")
    monkeypatch.setattr(probe, "CANARY_PATH", canary_file)

    result = probe.run(timeout=1)
    statuses = [c["result"] for c in result["cases"]]
    assert pc.AGENT_ERROR in statuses
    assert any(c["name"] == "control/distinct" for c in result["cases"])
    assert calls == [], "没有对照组，还是把提示词发出去了"


def test_run_sends_two_prompts_that_differ_only_by_the_instruction(monkeypatch, tmp_path):
    """`run()` 必须**共用一份模板**，不能两臂各建一次。

    ⚠️ 这一格是补上来的：`test_both_arms_differ_only_by_the_instruction` 只测了
    `build_prompt_template()` + `render_prompt()` 这两个零件，**没测调用点**。
    实测把 `run()` 改回"两臂各调一次 render_prompt"，那 14 条全绿 ——
    守卫的作用域就是它的盲区。判据要钉在**真正发出去的那两段字节**上。

    时间锚在这里被打桩成"每调一次就变"，模拟真实时钟跨过分钟边界；
    共用模板才能让两臂只差 INSTRUCTION。
    """
    ticks = iter(range(100))

    class _Consumer:
        @staticmethod
        def _memory_read_prompt_block():
            return "MEM"

        @staticmethod
        def _outbound_file_prompt_block():
            return "FILE"

        @staticmethod
        def _prepend_time_anchor_foreground(content, msg_unix_ts):
            return f"[time {next(ticks)}]\n\n{content}"

    monkeypatch.setattr(probe, "build_io_cli_catalog_segment", lambda: "CATALOG")
    monkeypatch.setattr(probe, "_load_consumer", lambda: _Consumer)
    monkeypatch.setattr(probe, "_check_fixtures", lambda p, m: True)
    monkeypatch.setattr(probe, "_read_manifest", lambda: {})

    fake_st = type(sys)("agent_protocol_core.self_thinking")
    fake_st.INSTRUCTION = "LIVE_INSTRUCTION_TEXT"
    fake_apc = type(sys)("agent_protocol_core")
    fake_apc.self_thinking = fake_st
    monkeypatch.setitem(sys.modules, "agent_protocol_core", fake_apc)
    monkeypatch.setitem(sys.modules, "agent_protocol_core.self_thinking", fake_st)
    canary_file = tmp_path / "canary.txt"
    canary_file.write_text("CANARY_INSTRUCTION_TEXT", encoding="utf-8")
    monkeypatch.setattr(probe, "CANARY_PATH", canary_file)

    sent = []

    def _capture(prompt, cwd, timeout):
        sent.append(prompt)
        return ("OK" if len(sent) == 1 else "BLOCKED"), ""

    monkeypatch.setattr(probe, "_run_claude", _capture)

    probe.run(timeout=1)
    assert len(sent) == 2, f"应当正好发两次，实际 {len(sent)}"
    live, canary = sent
    assert live.replace("LIVE_INSTRUCTION_TEXT", "@") == canary.replace(
        "CANARY_INSTRUCTION_TEXT", "@"
    ), "两臂除 INSTRUCTION 外不同形 ⇒ 判别力不成立（多半是各建了一次模板）"


def test_load_consumer_restores_caller_logging_disable_level(monkeypatch):
    """`_load_consumer` 压日志之后必须恢复到**调用者原来那档**，不是 NOTSET。

    `logging.disable` 是**进程级全局**。原来写的是 `logging.disable(logging.NOTSET)`，
    那不是"还原"、是"重置"：调用者若自己设过 `disable(ERROR)`，探针跑完会把它抹掉，
    于是调用者以为还压着的日志又开始输出。
    同族：以为自己在还原，其实是在重置。
    """
    import logging

    monkeypatch.setitem(sys.modules, "chat_resident_consumer", type(sys)("stub"))

    logging.disable(logging.ERROR)
    try:
        probe._load_consumer()
        assert logging.root.manager.disable == logging.ERROR, (
            "调用者原有的 disable level 被覆盖了"
        )
    finally:
        logging.disable(logging.NOTSET)

    # 调用者没设过时，跑完也应当回到"没设过"
    assert logging.root.manager.disable == logging.NOTSET
    probe._load_consumer()
    assert logging.root.manager.disable == logging.NOTSET


def test_load_consumer_does_not_leak_stub_credentials(monkeypatch):
    """占位 env 只在 import 期间生效，跑完必须还原调用者的真实值。"""
    monkeypatch.setitem(sys.modules, "chat_resident_consumer", type(sys)("stub"))
    monkeypatch.setenv("FEEDLING_API_KEY", "REAL_CALLER_KEY")
    monkeypatch.delenv("FEEDLING_API_URL", raising=False)

    probe._load_consumer()

    import os

    assert os.environ["FEEDLING_API_KEY"] == "REAL_CALLER_KEY", "调用者的真实值被占位覆盖了"
    assert "FEEDLING_API_URL" not in os.environ, "原本不存在的键被留下了"
