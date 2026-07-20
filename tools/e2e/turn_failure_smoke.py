"""回合失败可见性的 e2e 冒烟（spec 2026-07-18 §2）。

在真实 test 环境上验服务端契约：

    consumer 写兜底并带上分类
      → 兜底消息携带 turn_failure_* + reply_to_message_id
      → 这些字段能通过 /v1/chat/history 的 `since` 增量过滤（实时性的关键）
      → 用户消息 metadata 上有冗余的 reply_*
      → **归责由服务端按 catalog 定，谎报的 blame 被纠正**（归责红线）
      → detail 绝不下发

单测覆盖了字段形状，但那里 consumer 和引导门都是 mock 的。这个脚本在真服务上
跑，抓的是接线断掉、门拦住、字段被中间层吃掉这类只有真环境才暴露的问题——
实跑时就撞到三处：坏 key 在 setup 阶段即被拒（走的是另一条通道）、托管入口收
明文而非客户端信封、model_api 账号需先进 main_loop 才被引导门放行。

用完即删账号（test-account-hygiene）。只在 setup 时调一次真 key 用于过引导门，
不产生对话调用，几乎不烧额度。

Run:  python3 -m tools.e2e.turn_failure_smoke
退出码 0 = 通过，1 = 失败（发版阻断）。
"""
from __future__ import annotations

import sys
import time
import uuid

from .client import E2EClient

FAKE_KEY = "sk-e2e-turn-failure-" + uuid.uuid4().hex   # 必然 401，不烧额度
POLL_TIMEOUT = 180.0
POLL_INTERVAL = 3.0


def _fail(msg: str) -> None:
    print(f"[turn-failure] FAIL {msg}")
    sys.exit(1)


def _find_carrier(messages: list[dict]) -> dict | None:
    return next((m for m in messages if m.get("turn_failure_error_class")), None)


def run() -> int:
    print(f"[turn-failure] 用假 key 建号（key 前缀 {FAKE_KEY[:24]}…）")
    with E2EClient.provision(route="model_api") as c:
        print(f"[turn-failure] user_id={c.user_id}")

        # 先用一个能测活通过的真 key 完成 setup —— 目的不是调模型，而是让账号
        # 进入 main_loop 阶段：/v1/chat/response 的引导门对 model_api 账号放行的
        # 前提正是 stage==main_loop（backend/bootstrap/gates.py:151）。用假 key
        # setup 会 400，账号停在 needs_* 阶段，写回复就被 409 拦下。
        import os
        real_key = ""
        for line in open(os.path.expanduser("~/Projects/io/.env.local"), encoding="utf-8"):
            if line.startswith("OPEN_ROUTER_KEY="):
                real_key = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
        if not real_key:
            _fail("io/.env.local 里没找到 OPEN_ROUTER_KEY，无法完成 setup")
        r = c.post("/v1/model_api/setup", json={
            "provider": "openrouter",
            "model": "openai/gpt-4o-mini",
            "api_key": real_key,
        })
        print(f"[turn-failure] setup(真 key，仅为过引导门) -> {r.status_code}")
        if r.status_code not in (200, 201):
            _fail(f"setup 失败，账号进不了 main_loop: {r.status_code} {r.text[:160]}")

        # 为什么不用坏 key 触发真实回合失败：/v1/model_api/setup 会对 key 做真实
        # 测活，不通就直接 400 —— 坏 key 走的是【同步 HTTP 错误】那条通道，永远
        # 到不了【回合失败】这条。要真触发它需要一个「能测活通过、但对话时失败」
        # 的 key（如余额刚耗尽），不是本脚本能稳定造出来的。
        #
        # 故本脚本扮演 consumer 直调 /v1/chat/response —— 这正是真实 consumer 失败
        # 时走的同一个入口、同一段服务端代码（原子 CAS 写入 + catalog 查表归责 +
        # 增量流下发）。验的是服务端契约在真环境成立；consumer 侧是否真的调用它，
        # 由 tests/test_consumer_error_classify.py 锁住。
        since = time.time()
        parent_ts = c.send_chat("e2e turn-failure probe")
        hist = c.get("/v1/chat/history?limit=20").json().get("messages", [])
        parent = next((m for m in reversed(hist) if m.get("role") == "user"), None)
        if parent is None:
            _fail("刚发的用户消息没出现在 history 里")
        parent_id = str(parent["id"])
        print(f"[turn-failure] 用户消息 id={parent_id}")

        # 模拟 consumer 在回合失败时写兜底：带上分类结果。
        # 故意谎报 blame=system —— 服务端应按 error_class 查 catalog 纠正回
        # user_provider（归责红线：不采信 payload）。
        send = c.post("/v1/chat/response", json={
            "envelope": c._seal("我这会儿有点慢，刚刚没接上。你稍后再发一次，我会继续接。"),
            "source": "chat",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "auth_invalid",
            "turn_failure_blame": "system",
            "turn_failure_user_text": "x" * 900,
        })
        print(f"[turn-failure] 兜底写入 -> {send.status_code}")
        if send.status_code not in (200, 201):
            _fail(f"兜底写入被拒: {send.status_code} {send.text[:200]}")

        print("[turn-failure] 轮询增量流，等 consumer 写回兜底…")
        carrier = None
        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            h = c.get(f"/v1/chat/history?since={since}&limit=50")
            if h.status_code == 200:
                carrier = _find_carrier(h.json().get("messages", []))
                if carrier:
                    break
            time.sleep(POLL_INTERVAL)

        if carrier is None:
            _fail("增量流里始终没出现带 turn_failure_* 的消息 —— 实时链路断了，"
                  "或 consumer 没把分类结果带上（这正是本脚本要抓的）")

        ec = carrier.get("turn_failure_error_class")
        blame = carrier.get("turn_failure_blame")
        text = carrier.get("turn_failure_user_text") or ""
        parent_id = carrier.get("reply_to_message_id") or ""
        print(f"[turn-failure] 增量流命中: error_class={ec} blame={blame}")
        print(f"[turn-failure] user_text={text[:60]}")

        if not parent_id:
            _fail("兜底消息没带 reply_to_message_id —— 客户端无法配对回用户消息")
        if blame not in ("user_provider", "provider_transient", "system"):
            _fail(f"blame 非法: {blame!r}")
        if not text:
            _fail("user_text 为空 —— 用户看不到原因")
        # 归责红线：我们谎报了 blame=system，服务端必须按 catalog 纠正回
        # user_provider（auth_invalid 是用户的 key 问题，要给行动指引）
        if ec != "auth_invalid":
            _fail(f"error_class 被改写: {ec!r}")
        if blame != "user_provider":
            _fail(f"服务端未按 catalog 纠正归责：谎报 system，实得 {blame!r}，"
                  f"期望 user_provider —— payload 被采信了")
        print("[turn-failure] 谎报的 blame 已被服务端纠正 ✅")

        # 我们塞了 900 个 x；服务端应改用 catalog 文案，不含我们塞的内容
        if "xxxxxxxxxxxxxxxxxxxx" in text:
            _fail("user_text 采信了 payload —— 原始内容可进用户可见文案")
        if len(text) > 500:
            _fail(f"user_text 超长: {len(text)}")
        print("[turn-failure] user_text 由服务端组装、未采信 payload ✅")

        # 冗余持久化：全量 history 里，用户消息上应带 reply_*
        full = c.get("/v1/chat/history?limit=50")
        parent = next((m for m in full.json().get("messages", [])
                       if m.get("id") == parent_id), None)
        if parent is None:
            _fail(f"全量 history 里找不到 parent {parent_id}")
        if parent.get("reply_error_class") != ec:
            _fail(f"用户消息 metadata 未镜像失败信息: "
                  f"{parent.get('reply_error_class')!r} != {ec!r}")
        print("[turn-failure] 用户消息 metadata 已镜像 ✅")

        # detail 绝不下发（隐私边界）
        for key in ("turn_failure_detail", "reply_detail"):
            if carrier.get(key) or parent.get(key):
                _fail(f"{key} 被下发了 —— 原始 provider 报错不得进客户端")
        print("[turn-failure] detail 未下发 ✅")

    print("[turn-failure] PASS")
    return 0


if __name__ == "__main__":
    sys.exit(run())
