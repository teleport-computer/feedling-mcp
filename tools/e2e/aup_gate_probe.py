"""发版哨兵：陪伴提示词是否正在被 Claude Code 的 AUP 闸拦下。

【为什么要它】2026-08-16→08-18 之间上游收紧了一次审查，`self_thinking.INSTRUCTION`
被判 "reverse engineering or duplicating model outputs"。resident + claude-code 用户
从此每一轮都拿兜底话——**而后端一无所知**（driver 不上报、runtime_error 404、
last_runtime_error 单值覆盖）。也就是说：**这条道失效时，用户先撞见，我们后知道。**
本探针把这个顺序倒过来。

【形状：必须是生产同形，不能喂裸 INSTRUCTION】
该闸对文本**非单调**：同一段文案，单独喂会被拒、放进完整提示词里反而放行（反之亦然）。
2026-08-30 实测：裸 INSTRUCTION 形状下连"已修好"的 D2 措辞都仍被拒。所以本探针把
INSTRUCTION 放回一份 consumer 前台回合同形的提示词里再发。

【脚手架怎么来的 —— 会漂的段一个快照都不存】
提示词里每一段随生产演进而变的文本，**都现场从生产件调出来**：

  io_cli 工具目录     `io_cli_catalog.build_catalog` + 生产的 `_strip_web_verbs_from_catalog`
  MEMORY READ 段      consumer 的 `_memory_read_prompt_block()`
  FILE DELIVERY 段    consumer 的 `_outbound_file_prompt_block()`
  时间锚 + 回复语言    consumer 的 `_prepend_time_anchor_foreground()`
                      ——**连同它们之间的胶水一起**

⇒ 这些段**结构上不可能过期**。这条设计是被两次实测逼出来的：
  · 初版把它们存成 fixture，第一次上真检查时**四段里三段已经和分支对不上**
    （目录多两个参数、memory/file 段长度不符、回复语言整段改过措辞）；
  · 二版改成重建、但胶水手抄，结果比生产**多了一个换行**。
⇒ **凡是能从生产调出来的，就不要自己拼。**

⚠️ **唯一还手抄的一处**（写在这里，而不是让它默默过去）：最外层那条
``f"{catalog}\\n{memory}\\n{file}\\n\\n{content}"``，复刻自 consumer
`_prepend_io_cli_capability_catalog` 末尾的 return。
`tests/test_aup_gate_probe.py` 有一条**逐字节**断言钉住它——改生产那条 return 时，
那条测试会红。

【判据】沿用 probe_common 的发版结果分类（无 SKIP）：
  PRODUCT_FAIL     线上文案被拒——这正是我们怕的那件事
  PASS             线上文案通过 **且** canary 仍被拒（判别力当场证明过）
  BLOCKED_EVIDENCE canary 通过了 / 脚手架相对生产已漂 ⇒ 本轮**没量到闸的状态**
  BLOCKED_DEPLOYMENT / AGENT_ERROR   本机没有 claude / 超时 / 无法归类的失败

  退出码沿用 `deep.py` 的 qualification 口径：**默认任一非 PASS ⇒ rc=1**。
  `--diagnostic` 才容忍 BLOCKED_EVIDENCE（但仍对 BLOCKING 类返回 1）。
  ⇒ "只有 OVERALL: PASS 才算放行"这句话在**代码里**成立，不只写在文档里。

⭐ 三条设计约束，每条都对应一次踩过的坑：
  1. 线上文案**从活模块读**（`agent_protocol_core.self_thinking`），本文件里不抄一份——
     抄一份就会和实际发出去的那份漂移，于是探针测的是副本、上线的是原件。
  2. canary 必须与线上文案**不同**。第一版草稿里两者是同一段文本，于是它
     **从未证明过自己能输出 PASS**——一个恒红的量具也能通过那种自测。
  3. 拿不到读数一律说"没量到"，不许落进一个和正常态无法区分的值（没有 SKIP+rc=0）。

⚠️【环境局限，落地时必须知道】
**本探针的灵敏度取决于它跑在什么环境里。** 同一段文案在裸 CI runner 上可能根本不被拒
（账号档位、订阅态、区域、客户端版本都会改变判定）。所以：
  - 必须跑在与 E2E rig 同构的环境（真实登录态的 claude CLI，非 API key 直连）；
  - canary 那一格就是环境自检：**canary 没被拒 ⇒ 这个环境测不了这件事**，
    换环境重跑，不要把它读成"我们没事"。
本探针默认清掉 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，强制走订阅登录态——
API key 直连与订阅态的判定不是同一条通路。

用法：

    python3 tools/e2e/aup_gate_probe.py
    python3 tools/e2e/aup_gate_probe.py --json
    python3 tools/e2e/aup_gate_probe.py --print-prompt     # 只组装并打印，不外发
    python3 tools/e2e/aup_gate_probe.py --write-manifest   # 改了 canary/用户消息后重钉其指纹
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.e2e.probe_common import (  # noqa: E402
    AGENT_ERROR,
    BLOCKED_DEPLOYMENT,
    BLOCKED_EVIDENCE,
    BLOCKING,
    PASS,
    PRODUCT_FAIL,
    Probe,
    worst,
)

REPO = Path(__file__).resolve().parent.parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "aup_gate"
CANARY_PATH = FIXTURES / "canary_instruction_v0.2.0.txt"
USER_MESSAGE_PATH = FIXTURES / "user_message.txt"
MANIFEST_PATH = FIXTURES / "manifest.json"

# 固定件只剩这两个，且都**不是**从生产派生的文本：用户消息是探针自己造的、
# canary 是对照组，本来就该冻在旧版本上。凡是生产会演进的段一律现场重建。
FIXED_FIXTURES = [CANARY_PATH.name, USER_MESSAGE_PATH.name]

# 模板里 INSTRUCTION 的占位。两臂共用一份模板，只有这个位置不同。
INSTRUCTION_SENTINEL = "@@INSTRUCTION@@"

IO_CLI_PATH = REPO / "tools" / "io_cli.py"
CONSUMER_PATH = REPO / "tools" / "chat_resident_consumer.py"

_AUP_MARKER = "Usage Policy"

# 导入 consumer 需要的最小环境。它 import 时读这几个键，缺一个就 KeyError。
# 值全是不可路由的占位，且由 _load_consumer() **强制**写入(不是 setdefault)，
# 导入后还原：探针只调它那几个纯文本函数，不跑任何回合、不带真实凭据。
_CONSUMER_ENV_STUB = {
    "FEEDLING_API_URL": "http://127.0.0.1:1",
    "FEEDLING_API_KEY": "aup-gate-probe-stub",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _load_production_function(path: Path, name: str):
    """按名从生产文件里取出一个自足函数并编译。

    **用生产自己的实现，不在本文件里复制一份判据**——复制的那份会独立漂移，
    于是探针有一天会按一套已经不存在的规则去组装提示词。函数若依赖模块级全局，
    这里 exec 出来的版本会在调用时炸掉，那正确：那说明它不再自足，该报没量到。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    fn = next(
        (n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name), None
    )
    if fn is None:
        raise LookupError(f"{path.name} 里找不到函数 {name}")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), "exec"), ns)
    return ns[name]


def build_io_cli_catalog_segment() -> str:
    """现场重建 io_cli 目录段——**不存快照**，所以它不可能过期。"""
    sys.path.insert(0, str(REPO / "tools"))
    import io_cli_catalog  # noqa: PLC0415

    catalog = io_cli_catalog.build_catalog(str(IO_CLI_PATH), python=sys.executable)
    if catalog is None:
        raise RuntimeError(
            "build_catalog 返回 None（逐 verb --help 那一步失败）——目录段建不出来"
        )
    strip_web = _load_production_function(CONSUMER_PATH, "_strip_web_verbs_from_catalog")
    return strip_web(catalog)


def _load_consumer():
    """导入 resident consumer（只为拿它那几个纯文本函数）。

    ⚠️ **强制**用占位环境，不是 `setdefault`：调用者机器上很可能有真实的
    `FEEDLING_API_URL` / `FEEDLING_API_KEY`，`setdefault` 会让探针带着真实凭据
    import，还会把真 key 的掩码打进日志。导入完成后原样还原父进程 env。
    导入期的模块级日志一并压掉——那行 "Starting resident consumer" 只是 import
    副作用，没有真的起任何东西，留着会让人误读成探针启动了一个 consumer。
    """
    sys.path.insert(0, str(REPO / "tools"))
    sys.path.insert(0, str(REPO))
    saved = {k: os.environ.get(k) for k in _CONSUMER_ENV_STUB}
    os.environ.update(_CONSUMER_ENV_STUB)
    # ⚠️ 存原值再恢复，**不是** disable(NOTSET)：那会把调用者自己设的 disable level
    # 一并抹掉（`logging.disable` 是进程级全局）。恢复要恢复到"原来那档"，
    # 不是"没有档"——同一族错误：以为自己在还原，其实是在重置。
    saved_disable = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        import chat_resident_consumer  # noqa: PLC0415
    finally:
        logging.disable(saved_disable)
        for key, old in saved.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
    return chat_resident_consumer


def build_prompt_template() -> str:
    """建一份**只差 INSTRUCTION** 的生产同形提示词模板。

    ⭐ 每一段都现场从生产件来，一个快照都不存：

      io_cli 目录        `io_cli_catalog.build_catalog` + 生产的 `_strip_web_verbs_from_catalog`
      MEMORY READ 段     consumer 的 `_memory_read_prompt_block()`
      FILE DELIVERY 段   consumer 的 `_outbound_file_prompt_block()`
      时间锚 + 回复语言   consumer 的 `_prepend_time_anchor_foreground()` —— **连同它们之间
                         的胶水一起**，所以换行个数不再由本文件手抄

    初版把这些存成 fixture，第一次上真检查时**四段里三段已经和分支对不上**
    （目录多两个参数、memory/file 段长度不符、回复语言整段改过措辞）；
    第二版手抄胶水又比生产多了一个换行。⇒ **凡是能从生产调出来的，就不要自己拼。**

    ⭐ 为什么先建模板、再分别代入两臂：`_prepend_time_anchor_foreground` 里的时间锚
    取的是**真实当前时间**。两臂各调一次的话，跨过一个分钟边界就会让 live 与 canary
    多出一处与 INSTRUCTION 无关的差异——而本探针的全部判别力，正建立在
    **两臂除 INSTRUCTION 外逐字节相同**之上。

    唯一还手抄的是最外层那条
    ``f"{catalog}\\n{memory}\\n{file}\\n\\n{content}"``——复刻自 consumer
    `_prepend_io_cli_capability_catalog` 末尾的 return（本车道 `web_notice` 恒为空）。
    `tests/test_aup_gate_probe.py` 里有一条逐字节断言钉住它。
    """
    consumer = _load_consumer()
    catalog = build_io_cli_catalog_segment()
    memory_block = consumer._memory_read_prompt_block()
    file_block = consumer._outbound_file_prompt_block()
    user_message = USER_MESSAGE_PATH.read_text(encoding="utf-8")

    # msg_unix_ts=0 ⇒ 不大于模块初始的 _last_interaction_unix(0)，since 恒为 None、
    # 全局不被改写 ⇒ 同一进程里重复调用是确定性的。
    content = consumer._prepend_time_anchor_foreground(
        f"{INSTRUCTION_SENTINEL}\n\n{user_message}", 0
    )
    return f"{catalog}\n{memory_block}\n{file_block}\n\n{content}"


def render_prompt(instruction: str, template: str | None = None) -> str:
    """把 INSTRUCTION 代进模板。

    ``instruction`` 传进来的是**未 strip 的原文**；这里按生产的做法 `.strip()`
    （worker.py 与 consumer 侧都是这么拼的），所以探针发出去的字节与生产走同一条变换。
    """
    if template is None:
        template = build_prompt_template()
    return template.replace(INSTRUCTION_SENTINEL, instruction.strip())


def _read_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def _check_fixtures(p: Probe, manifest: dict) -> bool:
    """固定件有没有被改坏（内容指纹）。

    这里**只剩两个**固定件，因为凡是从生产派生、会随生产演进的文本都改成
    现场重建了（含时间锚与回复语言规则）。剩下这两个本来就不是生产派生的：
    用户消息是探针自造的、canary 本来就该冻在旧版本上。
    ⇒ 它们只会因为**有人改了它们**而变，不会因为生产演进而过期。
    """
    ok = True
    want = manifest.get("sha256", {})
    for name in FIXED_FIXTURES:
        try:
            got = _sha256((FIXTURES / name).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            p.add(f"fixture/{name}", AGENT_ERROR, f"{type(e).__name__}: {e}")
            ok = False
            continue
        if got == want.get(name):
            p.add(f"fixture/{name}", PASS, f"sha256 {got[:12]}…")
        else:
            p.add(
                f"fixture/{name}",
                AGENT_ERROR,
                f"内容指纹不符：manifest={str(want.get(name))[:12]}… 实测={got[:12]}… "
                "（固定件被改过；确实该改就跑 --write-manifest 并人眼过 diff）",
            )
            ok = False
    return ok


def _run_claude(prompt: str, cwd: str, timeout: int) -> tuple[str, str]:
    """→ (verdict, detail)，verdict ∈ {OK, BLOCKED, NO_CLI, TIMEOUT, OTHER}。"""
    env = dict(os.environ)
    # 订阅登录态与 API key 直连不是同一条判定通路；生产 resident 走的是前者。
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(key, None)
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return "NO_CLI", "claude 不在 PATH"
    except subprocess.TimeoutExpired:
        return "TIMEOUT", f"{timeout}s 内没返回"
    out = (proc.stdout or "") + (proc.stderr or "")
    if _AUP_MARKER in out and proc.returncode != 0:
        return "BLOCKED", out.strip()[:300]
    if proc.returncode != 0:
        return "OTHER", f"rc={proc.returncode} {out.strip()[:300]}"
    return "OK", (proc.stdout or "").strip()[:300]


def run(timeout: int = 180) -> dict:
    p = Probe("aup_gate")

    try:
        manifest = _read_manifest()
    except Exception as e:  # noqa: BLE001
        p.add("fixture/manifest", AGENT_ERROR, f"读不到 manifest: {type(e).__name__}: {e}")
        return p.result()

    if not _check_fixtures(p, manifest):
        return p.result()

    try:
        from agent_protocol_core import self_thinking  # noqa: PLC0415
    except Exception as e:  # noqa: BLE001
        p.add(
            "live/import",
            BLOCKED_DEPLOYMENT,
            f"装不上 agent_protocol_core（{type(e).__name__}: {e}）"
            "——线上文案取不到，本轮没量到任何事",
        )
        return p.result()

    live_instruction = self_thinking.INSTRUCTION
    canary_instruction = CANARY_PATH.read_text(encoding="utf-8")

    # 判别力的前提：对照组必须**不是**被测对象。第一版草稿正是栽在这里。
    if live_instruction.strip() == canary_instruction.strip():
        p.add(
            "control/distinct",
            AGENT_ERROR,
            "canary 与线上文案逐字相同 ⇒ 本探针没有对照组，任何读数都不构成证据。"
            "（多半是装的还是修复前那个版本，或 canary fixture 被换成了当前版本）",
        )
        return p.result()
    p.add(
        "control/distinct",
        PASS,
        f"live sha={_sha256(live_instruction)[:12]}… canary sha={_sha256(canary_instruction)[:12]}…",
    )

    try:
        with tempfile.TemporaryDirectory(prefix="aup-gate-probe-") as scratch:
            # 一份模板、两次代入 ⇒ 两臂除 INSTRUCTION 外逐字节相同（时间锚不会
            # 因为跨分钟边界而在两臂之间引入一处无关差异）。
            template = build_prompt_template()
            live_prompt = render_prompt(live_instruction, template)
            canary_prompt = render_prompt(canary_instruction, template)
            p.add(
                "scaffold/catalog_rebuilt",
                PASS,
                f"io_cli 目录段现场重建成功（组装后 {len(live_prompt)} 字）",
            )
            live_v, live_d = _run_claude(live_prompt, scratch, timeout)
            canary_v, canary_d = _run_claude(canary_prompt, scratch, timeout)
    except Exception as e:  # noqa: BLE001
        p.add("scaffold/catalog_rebuilt", AGENT_ERROR, f"{type(e).__name__}: {e}")
        return p.result()

    if live_v == "NO_CLI":
        p.add("live/gate", BLOCKED_DEPLOYMENT, f"{live_d} ⇒ 本机测不了这件事")
        return p.result()

    if live_v == "BLOCKED":
        p.add(
            "live/gate",
            PRODUCT_FAIL,
            "线上陪伴提示词正在被 AUP 闸拦下：resident + claude-code 用户此刻每一轮"
            f"都会拿到兜底话，且后端无记录。上游原文：{live_d}",
        )
    elif live_v == "OK":
        p.add("live/gate", PASS, f"线上文案通过（{len(live_prompt)} 字提示词）")
    else:
        p.add("live/gate", AGENT_ERROR, f"探针没跑通（{live_v}）：{live_d}")

    if canary_v == "BLOCKED":
        p.add("canary/discriminating", PASS, "已知应被拒的旧文案仍被拒 ⇒ 判别力在")
    elif canary_v == "OK":
        p.add(
            "canary/discriminating",
            BLOCKED_EVIDENCE,
            "canary 没有被拒 ⇒ 闸挪了或本环境判定不同，本探针**已失去判别力**。"
            "上面那个 live 通过不构成证据；换到与 E2E rig 同构的环境重跑，"
            "或重新取一段基线当 canary（不要删掉这一格）。",
        )
    else:
        p.add(
            "canary/discriminating",
            BLOCKED_EVIDENCE,
            f"canary 那次没跑成（{canary_v}）：{canary_d} ⇒ 判别力未经证明",
        )

    return p.result()


def qualification_exit_code(results: list[str], *, diagnostic: bool) -> int:
    """退出码。与 `deep.py` 同口径，并额外修掉一个它没有、而本探针会撞上的洞。

    ⚠️ **不能写成 `1 if worst(results) in BLOCKING else 0`**：probe_common 的
    `SEVERITY` 把 BLOCKED_EVIDENCE 排在 PRODUCT_FAIL **之前**，而 `BLOCKING` 又
    刻意不含 BLOCKED_EVIDENCE ⇒ 一次同时出现两者的运行会返回 0，
    **阻断信号被一个不阻断的信号盖住**。本探针恰好最容易撞上这个组合：
    线上文案被拒时，canary 那一格常常同时失去判别力。2026-08-30 实测到过一次。
    ⇒ 阻断按**集合**判，不按排序后的头一名判。
    """
    if diagnostic:
        return 1 if any(r in BLOCKING for r in results) else 0
    return 0 if all(r == PASS for r in results) else 1


def _write_manifest() -> int:
    manifest = _read_manifest() if MANIFEST_PATH.exists() else {}
    old = dict(manifest.get("sha256", {}))
    new = {name: _sha256((FIXTURES / name).read_text(encoding="utf-8")) for name in FIXED_FIXTURES}
    for key in sorted(set(old) | set(new)):
        if old.get(key) != new.get(key):
            print(f"{key}\n    旧 {str(old.get(key))[:16]}…\n    新 {str(new.get(key))[:16]}…")
    manifest.pop("production_source_sha256", None)
    manifest["sha256"] = new
    MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n已写回 {MANIFEST_PATH}")
    print("⚠️ 重钉指纹**不等于**固定件已经对：上面每一条 diff 都要人眼过一遍。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AUP 闸哨兵探针")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--timeout", type=int, default=180, help="单次 claude 调用超时秒数")
    ap.add_argument(
        "--diagnostic",
        action="store_true",
        help="诊断模式：容忍 BLOCKED_EVIDENCE（默认 qualification 模式下任一非 PASS 都 rc=1）",
    )
    ap.add_argument(
        "--print-prompt",
        action="store_true",
        help="只组装并打印生产同形提示词（live + canary），不外发任何请求",
    )
    ap.add_argument(
        "--write-manifest",
        action="store_true",
        help="重钉两个固定件(canary / 用户消息)的内容指纹——只在确实要改它们时用",
    )
    args = ap.parse_args()

    if args.write_manifest:
        return _write_manifest()

    if args.print_prompt:
        from agent_protocol_core import self_thinking  # noqa: PLC0415

        template = build_prompt_template()
        for name, ins in (
            ("live", self_thinking.INSTRUCTION),
            ("canary", CANARY_PATH.read_text(encoding="utf-8")),
        ):
            text = render_prompt(ins, template)
            print(f"===== {name}: {len(text)} chars sha256={_sha256(text)[:16]}… =====")
            print(text)
        return 0

    result = run(timeout=args.timeout)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"=== {result['area']} ===")
        for case in result["cases"]:
            print(f"{case['result']:<20} {case['name']}")
            if case["detail"]:
                print(f"{'':<20} {case['detail']}")

    results = [c["result"] for c in result["cases"]]
    print(f"\nOVERALL: {worst(results)}")
    blocking = sorted({r for r in results if r in BLOCKING})
    if blocking:
        print(f"BLOCKING: {', '.join(blocking)}")
    if BLOCKED_EVIDENCE in results:
        print("⚠️ BLOCKED_EVIDENCE 不是放行：那一格没有量到闸的状态，别当成绿。")
    rc = qualification_exit_code(results, diagnostic=args.diagnostic)
    if rc:
        print("⇒ 发版阻断" + ("（诊断模式）" if args.diagnostic else "（qualification 模式：只有全 PASS 才放行）"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
