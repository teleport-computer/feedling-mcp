"""内容闸 live 探针(test 环境)——证「真卡不被误伤」+「占位符落不进花园」。

这次改动的主要风险面**不是**漏拦,是误拦:内容闸站在 capture/dream 的写入路径上,
判错一次的后果是用户本该有的记忆卡凭空消失(而且悄无声息)。所以 live 验证的第一顺位
是拿一个正常模型跑真 capture,断言卡确实落地、且 summary/content 都是真内容。

用法(需要 ~/.feedling-e2e-keys.env 里的 E2E_KEY_OPENROUTER):
    NO_PROXY='*' python3 tools/e2e/card_gate_probe.py
    NO_PROXY='*' python3 tools/e2e/card_gate_probe.py --model deepseek/deepseek-chat

只打 test(client.py 硬拒 prod);账号用完即删(test-account-hygiene)。
退出码 0 = 全 PASS。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.e2e.client import E2EClient  # noqa: E402
from tools.e2e.hosted import _hosted_send  # noqa: E402
from tools.e2e.probe_common import mem_fetch, mem_index, new_marker  # noqa: E402

# card_text 的判据要在断言里复用 —— 探针和线上必须是同一把尺子。
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from memory_garden.text.card_text import card_text_rejection  # noqa: E402

CAPTURE_POLL_SEC = 300.0
POLL_EVERY_SEC = 15.0

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "", *, pass_detail: str = "") -> bool:
    """PASS 行只打 ``pass_detail`` —— 把「失败时才成立」的说明打在 PASS 行上
    会让日志读起来自相矛盾(第一版就干了这事:PASS 后面跟着 "no matching card")。"""
    shown = pass_detail if ok else detail
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {shown}" if shown else ""))
    if not ok:
        _fails.append(name)
    return ok


def _load_key_pool() -> dict[str, str]:
    pool: dict[str, str] = {}
    path = Path.home() / ".feedling-e2e-keys.env"
    if not path.exists():
        return pool
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        pool[k.strip()] = v.strip()
    return pool


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="anthropic/claude-sonnet-4.6")
    args = ap.parse_args()

    pool = _load_key_pool()
    key = pool.get("E2E_KEY_OPENROUTER") or os.environ.get("E2E_KEY_OPENROUTER", "")
    if not key:
        print("SKIP: no E2E_KEY_OPENROUTER in ~/.feedling-e2e-keys.env")
        return 0

    with E2EClient.provision(route="model_api") as c:
        print(f"probe user: {c.user_id} model={args.model}")
        r = c.post("/v1/model_api/setup", json={
            "provider": "openrouter", "model": args.model, "api_key": key})
        if not check("model_api setup", r.status_code in (200, 201),
                     f"{r.status_code} {r.text[:120]}"):
            return 1

        # 一条信息量足够、明确要求记住的话 —— capture 该为它落一张卡。
        marker = new_marker()
        text = (
            f"帮我记一件事：我的测试锚点代号是 {marker}。"
            "起因是我昨天连着加班到十一点，答应了自己这周末一定去看医生，"
            "所以想让你以后提醒我这件事。"
        )
        _sent, err = _hosted_send(c, text)
        if not check("chat send", not err, err or ""):
            return 1

        r = c.post("/v1/capture/force", json={})
        if not check("capture/force accepted", r.status_code in (200, 202),
                     f"{r.status_code} {r.text[:120]}"):
            return 1

        card = None
        started = time.time()
        deadline = started + CAPTURE_POLL_SEC
        while time.time() < deadline:
            items = mem_index(c, limit=100)
            for it in items:
                blob = f"{it.get('summary') or ''}{it.get('content') or ''}"
                if marker in blob or "医生" in blob or "加班" in blob:
                    card = it
                    break
            if card:
                break
            time.sleep(POLL_EVERY_SEC)

        # 核心断言:闸没有把一张本该存在的真卡拦掉。
        if not check("capture landed a card (gate did not eat a real memory)",
                     card is not None,
                     f"waited {CAPTURE_POLL_SEC:.0f}s, index had no matching card",
                     pass_detail=f"card {card.get('id') if card else ''} "
                                 f"after {time.time() - started:.0f}s"):
            return 1

        summary = str(card.get("summary") or "")
        content = str(card.get("content") or "")
        if not summary or not content:
            fetched = mem_fetch(c, [str(card.get("id"))])
            if fetched:
                summary = summary or str(fetched[0].get("summary") or "")
                content = content or str(fetched[0].get("content") or "")

        rejection = card_text_rejection(summary=summary, content=content)
        check("landed card passes the very gate that guards writes",
              rejection is None, rejection or "?", pass_detail="clean")
        check("summary is a real sentence, not a placeholder",
              bool(summary.strip()) and summary.strip() not in {"...", "…"},
              repr(summary[:80]), pass_detail=repr(summary[:80]))
        check("content is a real body, not '...'",
              bool(content.strip()) and content.strip() not in {"...", "…"},
              repr(content[:80]), pass_detail=repr(content[:80]))

        # 顺带盯一眼称呼规则:capture prompt 明令卡内字段不得用「用户」/「user」
        # 指代本人(名字未知时省略主语或用「对方」)。这不是内容闸的职责,判 warn
        # 不判 fail —— 但 2026-07-26 这一跑 sonnet-4.6 确实写了「用户承诺…」,
        # 说明规则没兜住,值得留一只眼睛(归属 codex4 的 user-referent-naming)。
        referent_leak = [w for w in ("用户", "user") if w in summary or w in content]
        if referent_leak:
            print(f"[WARN] card calls the person {referent_leak} — naming rule "
                  f"not holding (not this change's scope)")

    print()
    print("RESULT:", "ALL PASS" if not _fails else f"FAILURES({len(_fails)}): {_fails}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
