#!/usr/bin/env python3
"""wake 轮写权限分档 (docs/testing §2 P 行, usr_a40e 2026-08-01 案).

产品不变量:**没有用户在场的那一轮**——
  · 必须**能新增/更新记忆**(capture/dream/主动整理全靠它),
  · **不许删除记忆卡**(Seven 2026-08-14 拍板,不做 pending-intent 例外),
  · **不许改身份卡**(usr_a40e 一次心跳里模型自主改了签名和相处天数)。
本轮改了 wake lane 的 prompt/上下文/沉默语义,必须复验这条分档还在。

L1 单元层:provenance.write_gate 四象限
L2 lane 层:两个 wake 执行调用点确实关闭身份写与记忆删除授权,
           wake 工具面也确实移除 delete；chat 调用点保留默认授权
"""
import ast
import os
import pathlib
import sys

REPO = str(pathlib.Path(__file__).resolve().parents[2])
sys.path.insert(0, os.path.join(REPO, "backend"))
os.environ.setdefault("DATABASE_URL", "postgresql://xiaotingtan@127.0.0.1:5432/feedling_parity_e2e?sslmode=disable")
os.environ.setdefault("FEEDLING_RUNTIME_TOKEN_SECRET", "parity-secret")
os.environ["NO_PROXY"] = "*"

from model_api_runtime.v2 import provenance as prov  # noqa: E402

FAIL = []


def check(name, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}", flush=True)
    if not ok:
        FAIL.append(name)


print("=== L1 write_gate quadrants ===")
# wake turn: memory write allowed, identity write refused
for op in ("add", "update"):
    ok, reason = prov.write_gate(
        "memory_write",
        turn_authorization=True,
        identity_write_authorization=False,
        memory_delete_authorization=False,
        tool_args={"actions": [{"op": op}]},
    )
    check(f"wake: memory_write {op} ALLOWED", ok, reason)
ok, reason = prov.write_gate(
    "memory_write",
    turn_authorization=True,
    identity_write_authorization=False,
    memory_delete_authorization=False,
    tool_args={"actions": [{"op": "delete", "target_id": "m1"}]},
)
check("wake: memory_write delete REFUSED", not ok, reason)
ok, reason = prov.write_gate("identity_patch", turn_authorization=True,
                             identity_write_authorization=False)
check("wake: identity_patch REFUSED", not ok, reason)
ok, reason = prov.write_gate("identity_nudge", turn_authorization=True,
                             identity_write_authorization=False)
check("wake: identity_nudge REFUSED", not ok, reason)
# chat turn: destructive memory write and identity write are both allowed
ok, _ = prov.write_gate(
    "memory_write",
    turn_authorization=True,
    memory_delete_authorization=True,
    tool_args={"actions": [{"op": "delete", "target_id": "m1"}]},
)
check("chat: memory_write delete ALLOWED", ok)
ok, reason = prov.write_gate(
    "memory_write",
    turn_authorization=True,
    tool_args={"actions": [{"op": "delete", "target_id": "m1"}]},
)
check("omitted delete authorization: REFUSED", not ok, reason)
ok, _ = prov.write_gate("identity_patch", turn_authorization=True)
check("chat: identity_patch ALLOWED", ok)
# unauthorized turn: everything refused
ok, _ = prov.write_gate("memory_write", turn_authorization=False)
check("unauthorized: memory_write REFUSED", not ok)
# reads never gated
ok, _ = prov.write_gate("memory_index", turn_authorization=False,
                        identity_write_authorization=False)
check("reads never gated (memory_index)", ok)

print("\n=== L2 dispatch call sites carry the flag ===")
src = open(os.path.join(REPO, "backend/model_api_runtime/v2/worker.py")).read()
tree = ast.parse(src)
sites = []
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    fn = node.func
    name = getattr(fn, "attr", getattr(fn, "id", ""))
    if name != "dispatch_tool_calls":
        continue
    kw = {k.arg for k in node.keywords if k.arg}
    identity_blocked = any(
        k.arg == "identity_write_authorization"
        and isinstance(k.value, ast.Constant) and k.value.value is False
        for k in node.keywords
    )
    memory_delete_blocked = any(
        k.arg == "memory_delete_authorization"
        and isinstance(k.value, ast.Constant) and k.value.value is False
        for k in node.keywords
    )
    memory_delete_allowed = any(
        k.arg == "memory_delete_authorization"
        and isinstance(k.value, ast.Constant) and k.value.value is True
        for k in node.keywords
    )
    sites.append((
        node.lineno,
        identity_blocked,
        memory_delete_blocked,
        memory_delete_allowed,
        "turn_authorization" in kw,
    ))

for lineno, identity_blocked, memory_delete_blocked, memory_delete_allowed, _ in sites:
    print(
        f"   dispatch_tool_calls @ worker.py:{lineno} "
        f"identity_write_False={identity_blocked} "
        f"memory_delete_False={memory_delete_blocked} "
        f"memory_delete_True={memory_delete_allowed}"
    )
wake_sites = [s for s in sites if s[1]]
check("at least 2 wake-side sites pin identity_write_authorization=False",
      len(wake_sites) >= 2, f"found {len(wake_sites)} of {len(sites)} sites")
check(
    "every wake-side site pins memory_delete_authorization=False",
    bool(wake_sites) and all(site[2] for site in wake_sites),
    f"found {sum(site[2] for site in wake_sites)} of {len(wake_sites)} wake sites",
)
check(
    "at least one non-wake site explicitly authorizes chat memory delete",
    any(site[3] for site in sites if not site[1]),
    f"found {sum(site[3] for site in sites if not site[1])} explicit opt-ins",
)

wake_loop_sites = []
for node in ast.walk(tree):
    if not isinstance(node, ast.Call):
        continue
    fn = node.func
    if getattr(fn, "attr", getattr(fn, "id", "")) != "run_tool_loop":
        continue
    memory_delete_hidden = any(
        keyword.arg == "memory_delete_allowed"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in node.keywords
    )
    memory_delete_enabled = any(
        keyword.arg == "memory_delete_allowed"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in node.keywords
    )
    wake_loop_sites.append((node.lineno, memory_delete_hidden, memory_delete_enabled))
check(
    "one wake tool-loop surface pins memory_delete_allowed=False",
    sum(hidden for _, hidden, _ in wake_loop_sites) == 1,
    f"found {sum(hidden for _, hidden, _ in wake_loop_sites)} of "
    f"{len(wake_loop_sites)} tool-loop sites",
)
check(
    "one chat tool-loop surface explicitly enables memory delete",
    sum(enabled for _, _, enabled in wake_loop_sites) == 1,
    f"found {sum(enabled for _, _, enabled in wake_loop_sites)} of "
    f"{len(wake_loop_sites)} tool-loop sites",
)

print(f"\nRESULT: {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
