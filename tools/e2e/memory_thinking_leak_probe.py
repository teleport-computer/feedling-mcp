"""本机全栈探针:记忆抽取会不会被泄漏的思维链噎死。

usr_450ee421e16a3b5a(2026-08-09)报「AI 不往记忆花园存东西了」。真相不是 capture
没被调用 —— 它照常起、模型照常跑、用户的 token 照常花,结果在解析这一步被扔掉。
全 prod 扫描 328 个活跃用户:`json_decode_error` 56 次 / 11 个用户,是 capture 失败
的头号原因。

单测只喂 `parse_capture_cards` 一个字符串;这里跑 consumer 真正的那条路 ——
`_memory_agent_parse_with_bounce` → 解析 → `_capture_build_envelope` →
`execute_memory_actions` → 真后端 → 真 enclave → 真 PG,断言**用户能看见的东西**:
花园里到底有没有多出那张卡。唯一的替身是 provider HTTP 边界(我们不拥有的边界),
回复按线上真实形状带 `<think>`。

两段断言:
  1. 思维链里含伪 JSON 的回复 → 解析出卡 → **卡真的落进花园**,且思维链原文不混进卡片
  2. 第一问真的解析失败(json_decode_error)→ **必须触发第二问并救回来**
     (修复前这个错误码被排除在重问之外,线上连续 6 次 reask_count 全是 0)

台子:serve_dev :5001 + dev-seed enclave :5003 + 固定 PG,起法见
`genesis_resume_probe.py` 的 docstring;**enclave 必须带 NO_PROXY**,否则 macOS
系统代理污染回环。

    NO_PROXY='*' python3 tools/e2e/memory_thinking_leak_probe.py
"""
import base64
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path[:] = [p for p in sys.path if p not in ("", ".")]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:5001"
C = httpx.Client(base_url=BASE, timeout=60.0, trust_env=False)
FAILED = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# ---- 真账号 ---------------------------------------------------------------- #
reg = C.post("/v1/users/register", json={
    "public_key": base64.b64encode(os.urandom(32)).decode("ascii"),
    "archive_language": "zh-Hans-CN",
})
assert reg.status_code == 201, reg.text
uid, key = reg.json()["user_id"], reg.json()["api_key"]
print(f"user={uid}")

os.environ.update({
    "FEEDLING_API_URL": BASE,
    "FEEDLING_API_KEY": key,
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://127.0.0.1:1/chat",
    "CHECKPOINT_FILE": "/tmp/leak_e2e_ckpt.json",
    "NO_PROXY": "*",
})

import tools.chat_resident_consumer as crc  # noqa: E402

crc.FEEDLING_API_URL = BASE
crc.FEEDLING_API_KEY = key
crc._HEADERS = {"X-API-Key": key, "Content-Type": "application/json"}

# 线上真实形状:中转把推理内联进正文,而 capture 的 prompt 要 JSON,
# 所以模型的思维链里天然带大括号。
CARD = ('{"cards":[{"action":"add","type":"event","target_id":null,"bucket":"生活",'
        '"threads":["骑行"],"summary":"周末沿江骑行三十公里",'
        '"content":"他说这个周末沿江骑了三十公里,天气很好。",'
        '"importance":0.6,"pulse":0.3}]}')
REPLY_WITH_THINKING = (
    "<think>用户提到骑行。我打算写一张 {summary: 周末骑行} 这样的卡,"
    "字段照 {\"cards\": [...]} 的结构来。</think>\n" + CARD
)

calls = {"n": 0}


def fake_call_agent(prompt, *_a, **_kw):
    calls["n"] += 1
    return REPLY_WITH_THINKING


crc.call_agent = fake_call_agent
crc._note_agent_turn_success = lambda *a, **k: None


def garden_size():
    r = C.post("/v1/memory/index", headers={"X-API-Key": key}, json={"limit": 100})
    if r.status_code != 200:
        return -1, r.text[:120]
    body = r.json()
    return len(body.get("items") or []), ""


before, err0 = garden_size()
check("garden readable", before >= 0, err0)

# ---- 走真实解析链路 -------------------------------------------------------- #
(cards, parse_err), bounce = crc._memory_agent_parse_with_bounce(
    "PROMPT",
    parse=crc.parse_capture_cards,
    build_retry_prompt=crc.build_capture_retry_prompt,
    lane="capture",
    job_id="e2e_leak",
)
print(f"\nparse_err={parse_err!r} bounce={bounce!r} cards={len(cards)} agent_calls={calls['n']}\n")

check("解析没有被思维链噎死", parse_err is None, f"err={parse_err!r}")
check("拿到了那张卡", len(cards) == 1, f"cards={len(cards)}")

# ---- 真写入:卡必须出现在花园里 --------------------------------------------- #
if not parse_err and cards:
    actions = []
    for card in cards:
        env = crc._capture_build_envelope(
            card, occurred_at="2026-08-09T12:00:00", source="memory_capture")
        actions.append({"type": "memory.add", "envelope": env})
    result = crc.execute_memory_actions(actions)
    check("写入成功", result.get("status") in {"ok", "partial"}, str(result)[:160])
    after, _ = garden_size()
    check("花园真的多了一张卡", after == before + 1, f"{before} -> {after}")
    if after > before:
        r = C.post("/v1/memory/index", headers={"X-API-Key": key}, json={"limit": 5})
        titles = [str(i.get("summary") or i.get("title") or "")[:40]
                  for i in (r.json().get("items") or [])]
        print("garden head:", titles)
        blob = " ".join(titles)
        check("思维链原文没混进卡片", "我打算写一张" not in blob and "结构来" not in blob)

# ---- V1 重问:json_decode_error 现在必须触发第二问 ------------------------- #
# 修复前这个错误码被 is_card_format_error 排除,连续 6 次失败 reask_count 全是 0。
replies = iter(["{这不是合法 JSON 但括号平衡}", CARD])
calls["n"] = 0


def two_shot_agent(prompt, *_a, **_kw):
    calls["n"] += 1
    return next(replies)


crc.call_agent = two_shot_agent
(cards2, err2), bounce2 = crc._memory_agent_parse_with_bounce(
    "PROMPT", parse=crc.parse_capture_cards,
    build_retry_prompt=crc.build_capture_retry_prompt,
    lane="capture", job_id="e2e_reask")
print(f"\nreask: err={err2!r} bounce={bounce2!r} cards={len(cards2)} agent_calls={calls['n']}\n")
check("json_decode_error 触发了第二问", calls["n"] == 2, f"agent_calls={calls['n']}")
check("第二问救回了卡", err2 is None and len(cards2) == 1, f"err={err2!r} cards={len(cards2)}")
check("重问被如实记录", bounce2 == "bounced_ok", f"bounce={bounce2!r}")

C.post("/v1/account/reset", headers={"X-API-Key": key}, json={"confirm": "delete-all-data"})
print()
if FAILED:
    print(f"E2E FAILED: {FAILED}")
    sys.exit(1)
print("E2E ALL PASS")
