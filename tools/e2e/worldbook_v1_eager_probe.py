#!/usr/bin/env python3
"""V1 resident consumer 世界书「确定性预注入」live 验收(T509,2026-09-07).

起**本仓库当前树**的 tools/chat_resident_consumer.py 作为子进程打 test 环境
(与 tools/e2e/vps.py 同一套起法,AGENT_CLI_CMD 用本机 `claude -p`),断言:

  A. 无条目账号:一轮聊天正常,且**零注入**——consumer 日志无
     `worldbook context injected`,trace 无 `worldbook.context.applied`;
     后端 `worldbook.match.completed` 应为 outcome=no_entries。
  B. 有条目账号、消息含触发词:后端 matched>=1 且 messages>=1,consumer 日志出现
     `worldbook context injected names=[…]`,trace 出现 `worldbook.context.applied`
     且 carrier_chars>0。
  C. **跨句触发**(本单要修的形状):第二句**不含**触发词、只指代上一句,后端仍
     matched>=1 且 messages>=2 —— 只有窗口深度>1 才能通过;旧代码(只传当前一句)
     必红。
  D. **注入体不进 trace**:`worldbook.match.completed` / `worldbook.context.applied`
     的事件 JSON 里不得出现条目正文标记词、条目 id、也不得出现用户消息原文。

用法(在仓库根目录):  python3 tools/e2e/worldbook_v1_eager_probe.py
账号用完即 reset 并 admin 回读;失败现场由 E2EClient 保留。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))
sys.path.insert(0, str(REPO / "tools"))
# ⚠️ 这里**不**改 os.environ。代理环境的调整放在 main() 里：模块顶层改环境会污染
# 整个 pytest 进程——tests/ 里 import 本模块时，同一进程内其它模块的环境分支会随收集
# 顺序变化（codex r3 实测：仅 import 后 HTTP_PROXY/HTTPS_PROXY 被 pop、NO_PROXY=*）。

from tools.e2e.client import E2EClient, TEST_API, TEST_ENCLAVE  # noqa: E402
from tools.e2e.unlock import verify_loop, wait_resident_consumer_passing  # noqa: E402
from content_encryption import build_envelope  # noqa: E402

MARK = {"HIT": "观星祭", "ALWAYS": "影月历", "MISS": "潮汐钟"}
ENTRIES = [
    {"name": "青岚学院", "keys": ["青岚学院", "青岚"], "content": f"青岚学院每年秋分举办{MARK['HIT']},院训是知行合一。"},
    {"name": "落日港", "keys": ["落日港"], "content": f"落日港以{MARK['MISS']}报时。"},
    {"name": "历法", "keys": [], "alwaysOn": True, "content": f"本世界用{MARK['ALWAYS']},一年十三个月。"},
]
REPLY_TIMEOUT = 240.0
# 两轮用户原文做成常量：D 的泄漏哨兵从这里**派生**，不手写（手写会漂：第一版漏了
# 第三条条目 id「历法」和 C 那句原文）。
PROMPT_B = "你还记得青岚学院每年秋天有什么活动吗?一句话告诉我。"
PROMPT_C = "那里现在是什么季节?院训是什么?一句话。"
FAIL: list[str] = []
ACTIVE_CLIENT = None   # 进入 with 后指向 E2EClient；check(False) 据此保留失败现场


def leak_sentinels() -> list[str]:
    """D 闸的哨兵：全部条目 id/name、全部关键词、全部正文、全部标记、两轮用户原文。"""
    out: list[str] = []
    for e in ENTRIES:
        out.append(e["name"])
        out.extend(e["keys"])
        out.append(e["content"])
    out.extend(MARK.values())
    out.extend([PROMPT_B, PROMPT_C])
    return sorted({w for w in out if w})


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}  {str(detail)[:220]}", flush=True)
    if not ok:
        FAIL.append(name)
        # 不抛异常（后面的格还要跑），但必须让 E2EClient 在 __exit__ 时保留现场：
        # 光记全局 FAIL，离开 with 时 exc_type=None、preserve_reason 为空，teardown 会把账号
        # 连同现场一起删掉（codex 复审指出）。
        if ACTIVE_CLIENT is not None:
            ACTIVE_CLIENT.preserve_failure(f"{name}: {str(detail)[:120] or 'failed'}")


# 回复读取用 read_reply_strict（与 tools/e2e/vps.py 同款）：它同时认密文信封与明文行。
# 第一版用了只认密文的 decrypt_reply，test 账号现在默认明文回复，A 段直接抛
# "reply carries no inline sealed envelope to decrypt"（2026-09-07 实跑）。
def trace_events(c: E2EClient, since: float) -> list[dict]:
    r = c.get("/v1/debug/trace?limit=200")
    if r.status_code != 200:
        return []
    evs = (r.json() or {}).get("events") or []
    return [e for e in evs if float(e.get("ts") or 0) >= since - 1]


def wb_events(evs: list[dict], typ: str) -> list[dict]:
    return [e for e in evs if e.get("type") == typ]


def tail(path: Path, n: int = 6) -> str:
    try:
        return " | ".join(path.read_text(errors="replace").splitlines()[-n:])[-400:]
    except Exception:
        return "<no log>"


def _direct_connection_env() -> None:
    """只在探针作为独立进程运行时调整代理环境（main 内调用，import 时不生效）。"""
    os.environ["NO_PROXY"] = "*"; os.environ["no_proxy"] = "*"
    os.environ.pop("HTTP_PROXY", None); os.environ.pop("HTTPS_PROXY", None)


def main() -> int:
    _direct_connection_env()
    if subprocess.run(["which", "claude"], capture_output=True).returncode != 0:
        print("claude CLI not on PATH; abort", flush=True); return 2
    workdir = Path(tempfile.mkdtemp(prefix="feedling_e2e_wb_v1_"))
    log_path = workdir / "consumer.log"
    proc: subprocess.Popen | None = None
    print(f"repo={REPO}\nworkdir={workdir}", flush=True)
    try:
        with E2EClient.provision(route="resident") as c:
            global ACTIVE_CLIENT
            ACTIVE_CLIENT = c
            c.configure_failure_evidence(cell="probe:worldbook_v1_eager",
                                         artifacts={"consumer_log": str(log_path)})
            print("user:", c.user_id, flush=True)
            env = os.environ.copy()
            env.update({
                "FEEDLING_API_URL": TEST_API, "FEEDLING_API_KEY": c.api_key,
                "FEEDLING_ENCLAVE_URL": TEST_ENCLAVE, "AGENT_MODE": "cli",
                "AGENT_CLI_CMD": 'claude -p "{message}"',
                "FEEDLING_AGENT_CLI_CWD": str(workdir / "agent_home"),
                "CHECKPOINT_FILE": str(workdir / "checkpoint.json"),
                "AGENT_SESSION_FILE": str(workdir / "agent-session.txt"),
                "IMAGE_TEMP_DIR": str(workdir / "images"),
                "FEEDLING_AUTO_UPDATE": "0", "NO_PROXY": "*", "no_proxy": "*",
            })
            env.pop("FEEDLING_FOREGROUND_WORLDBOOK_CONTEXT", None)   # 测默认值
            (workdir / "agent_home").mkdir(parents=True, exist_ok=True)
            with open(log_path, "w") as log_f:
                proc = subprocess.Popen(
                    [sys.executable, str(REPO / "tools" / "chat_resident_consumer.py")],
                    env=env, stdout=log_f, stderr=subprocess.STDOUT, cwd=str(REPO))
                check("consumer-heartbeat", wait_resident_consumer_passing(c, timeout=90), tail(log_path))
                if proc.poll() is not None:
                    check("consumer-alive", False, f"rc={proc.returncode} {tail(log_path)}"); return 1
                check("verify-loop", verify_loop(c, timeout=120))

                # ── A. 无条目:零注入(反向) ──────────────────────────────
                tA = c.send_chat("先随便聊一句,你好呀。")
                mA = c.wait_reply(tA, timeout=REPLY_TIMEOUT)
                rA = c.read_reply_strict(mA) if mA else ""
                check("A.无条目账号聊天正常", bool(rA.strip()), rA[:80])
                evA = trace_events(c, tA)
                mc = wb_events(evA, "worldbook.match.completed")
                check("A.后端记录了 no_entries 匹配", any((e.get("detail") or {}).get("outcome") == "no_entries" for e in mc),
                      [ (e.get("detail") or {}).get("outcome") for e in mc ])
                check("A.零注入(trace 无 context.applied)", not wb_events(evA, "worldbook.context.applied"))
                check("A.零注入(consumer 日志无 injected)", "worldbook context injected" not in log_path.read_text(errors="replace"))

                # ── 写入 3 条 client-sealed 条目(与 iOS 同形) ───────────────
                for e in ENTRIES:
                    inner = {"id": e["name"], "name": e["name"], "keywords": e["keys"],
                             "content": e["content"], "enabled": True}
                    if e.get("alwaysOn"): inner["alwaysOn"] = True
                    envlp = build_envelope(
                        plaintext=json.dumps(inner, ensure_ascii=False).encode("utf-8"),
                        owner_user_id=c.user_id, user_pk_bytes=bytes(c._sk.public_key),
                        enclave_pk_bytes=c._enclave_pk, visibility="shared", item_id=e["name"])
                    rr = c.post("/v1/worldbook/upsert", json={"envelope": envlp, "id": e["name"]})
                    check(f"upsert {e['name']}", rr.status_code == 200, rr.text[:100])

                # ── B. 含触发词:一轮真实注入 ─────────────────────────────
                log_before = len(log_path.read_text(errors="replace"))
                tB = c.send_chat(PROMPT_B)
                mB = c.wait_reply(tB, timeout=REPLY_TIMEOUT)
                rB = c.read_reply_strict(mB) if mB else ""
                print("reply B:", rB[:200], flush=True)
                evB = trace_events(c, tB)
                mcB = [e for e in wb_events(evB, "worldbook.match.completed")]
                cnt = [ (e.get("detail") or {}).get("counts") or {} for e in mcB ]
                check("B.后端 matched>=1 且 messages>=1",
                      any(x.get("matched", 0) >= 1 and x.get("messages", 0) >= 1 for x in cnt), cnt)
                logB = log_path.read_text(errors="replace")[log_before:]
                check("B.consumer 日志 injected names=", "worldbook context injected names=" in logB,
                      [l for l in logB.splitlines() if "worldbook" in l][-2:])
                apB = wb_events(evB, "worldbook.context.applied")
                check("B.trace context.applied 且 carrier_chars>0",
                      any(((e.get("detail") or {}).get("carrier_chars") or 0) > 0 for e in apB),
                      [ (e.get("detail") or {}) for e in apB ][:1])
                check("B.(软)回复带上了命中标记", MARK["HIT"] in rB or "秋分" in rB, rB[:120])
                check("B.未提及条目未泄漏进回复", MARK["MISS"] not in rB and "落日港" not in rB, rB[:120])

                # ── C. 跨句触发:第二句不含触发词,只指代 ──────────────────
                tC = c.send_chat(PROMPT_C)
                mC = c.wait_reply(tC, timeout=REPLY_TIMEOUT)
                rC = c.read_reply_strict(mC) if mC else ""
                print("reply C:", rC[:200], flush=True)
                evC = trace_events(c, tC)
                cntC = [ (e.get("detail") or {}).get("counts") or {} for e in wb_events(evC, "worldbook.match.completed") ]
                check("C.跨句:本句无触发词仍 matched>=1(靠窗口深度)",
                      any(x.get("matched", 0) >= 1 for x in cntC), cntC)
                check("C.跨句:后端收到的 messages>=2(窗口确实带了历史)",
                      any(x.get("messages", 0) >= 2 for x in cntC), cntC)
                check("C.(软)回复用上了院训", "知行合一" in rC, rC[:120])

                # ── D. 注入体不进 trace ───────────────────────────────────
                blob = json.dumps([e for e in evB + evC if str(e.get("type","")).startswith("worldbook.")], ensure_ascii=False)
                leaks = [w for w in leak_sentinels() if w in blob]
                check("D.trace 里不含条目 id/关键词/正文/标记/两轮用户原文(哨兵全派生)", not leaks, leaks)
                # ⚠️ 这只证明 live 投影里没有；emitter 从未写入正文由单测
                # test_foreground_worldbook_context_applied_trace_is_content_free 钉住。
            if FAIL:
                c.preserve_failure(f"probe failed: {FAIL}")
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try: proc.wait(timeout=10)
            except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=10)
    print(f"\nconsumer log: {log_path}")
    print(f"RESULT: {'PASS' if not FAIL else 'FAIL ' + str(FAIL)}", flush=True)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
