#!/usr/bin/env python3
"""wake 轮写权限分档 (docs/testing §2 P 行, usr_a40e 2026-08-01 案).

产品不变量:**没有用户在场的那一轮**——
  · 必须**能写记忆**(capture/dream/主动整理全靠它),
  · **不许改身份卡**(usr_a40e 一次心跳里模型自主改了签名和相处天数)。
本轮改了 wake lane 的 prompt/上下文/沉默语义,必须复验这条分档还在。

L1 单元层:provenance.write_gate 四象限
L2 lane 层:两个 wake 调用点确实传 identity_write_authorization=False,
           而 chat 调用点不传(=默认 True)
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
ok, reason = prov.write_gate("memory_write", turn_authorization=True,
                             identity_write_authorization=False)
check("wake: memory_write ALLOWED", ok, reason)
ok, reason = prov.write_gate("identity_patch", turn_authorization=True,
                             identity_write_authorization=False)
check("wake: identity_patch REFUSED", not ok, reason)
ok, reason = prov.write_gate("identity_nudge", turn_authorization=True,
                             identity_write_authorization=False)
check("wake: identity_nudge REFUSED", not ok, reason)
# chat turn: both allowed
ok, _ = prov.write_gate("memory_write", turn_authorization=True)
check("chat: memory_write ALLOWED", ok)
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
    has_flag = any(
        k.arg == "identity_write_authorization"
        and isinstance(k.value, ast.Constant) and k.value.value is False
        for k in node.keywords
    )
    sites.append((node.lineno, has_flag, "turn_authorization" in kw))

for lineno, has_flag, _ in sites:
    print(f"   dispatch_tool_calls @ worker.py:{lineno} identity_write_False={has_flag}")
wake_sites = [s for s in sites if s[1]]
check("at least 2 wake-side sites pin identity_write_authorization=False",
      len(wake_sites) >= 2, f"found {len(wake_sites)} of {len(sites)} sites")
check("at least one chat-side site leaves it default (True)",
      len(sites) - len(wake_sites) >= 1, f"{len(sites) - len(wake_sites)} default sites")

print(f"\nRESULT: {'ALL PASS' if not FAIL else 'FAILURES: ' + ', '.join(FAIL)}")
sys.exit(1 if FAIL else 0)
