"""花园语言 live 探针 —— 证「偶尔说英文不会把中文花园翻掉」。

## 为什么要 live 验

2026-08-24 线上事故：一个中文用户的花园两天内整个翻成英文（桶名、卡片摘要、
AI 回复全变了）。判据当时看的是**已有桶名**，而桶名是 AI 自己写的输出 ——
拿输出当输入，形成自我强化的环。

修复把桶名彻底移出判据，改看「这个人实际在用什么语言写」。

**这个修复单测抓不到全貌**：单测喂的是构造好的字符串，而真实风险是
「真模型在真提示词下会不会因为一句英文就换语言」。只有真模型能暴露。

用法（需要 ~/.feedling-e2e-keys.env 的 key）：

    NO_PROXY='*' FEEDLING_E2E_API=http://127.0.0.1:8891 \\
      python3 tools/e2e/garden_language_probe.py --provider deepseek --model deepseek-chat

只打 test / 本地（client.py 硬拒 prod）；账号用完即删。退出码 0 = 全 PASS。
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.e2e.client import E2EClient  # noqa: E402
from tools.e2e.hosted import _hosted_send  # noqa: E402
from tools.e2e.probe_common import force_capture_until_enqueued, mem_index, new_marker  # noqa: E402

CAPTURE_POLL_SEC = 300.0
POLL_EVERY_SEC = 15.0

_CJK = re.compile(r"[一-鿿]")
_LATIN_WORD = re.compile(r"[A-Za-z]+")

_fails: list[str] = []


def check(name: str, ok: bool, detail: str = "", *, pass_detail: str = "") -> bool:
    if ok:
        print(f"[PASS] {name}" + (f" — {pass_detail}" if pass_detail else ""))
        return True
    print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
    _fails.append(name)
    return False


def _load_key_pool() -> dict:
    pool: dict[str, str] = {}
    path = Path.home() / ".feedling-e2e-keys.env"
    if path.exists():
        for line in path.read_text("utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                pool[k.strip()] = v.strip()
    return pool


def _is_chinese(text: str) -> bool:
    """一张卡算中文的判据：**按词计，不按字符计**。

    一个英文词≈5 个字母，中文一个字≈一个词。按字符比的话，中文用户随口夹
    几个技术词就会被判成英文 —— 这正是修复里改掉的那个错。
    """
    zh = len(_CJK.findall(text))
    en = len(_LATIN_WORD.findall(text))
    return zh >= en


def _ids(items: list[dict]) -> set[str]:
    return {str(i.get("id") or "") for i in items if i.get("id")}


def _wait_for_new_cards(c, *, known: set[str], timeout: float) -> list[dict]:
    """等到出现**新 id** 为止。

    ⚠️ 别用「条数变多」或 dict 比对判新增 —— 第一版就是这么写的，结果
    英文那句根本没落新卡，探针却报了 PASS（索引返回的同一张卡，某个字段
    有细微差异，dict 比对就当成了新的）。按 id 比才作数。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        items = mem_index(c, limit=100)
        fresh = [i for i in items if str(i.get("id") or "") not in known]
        if fresh:
            return items
        time.sleep(POLL_EVERY_SEC)
    return mem_index(c, limit=100)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="deepseek")
    ap.add_argument("--model", default="deepseek-chat")
    args = ap.parse_args()

    pool = _load_key_pool()
    key_name = f"E2E_KEY_{args.provider.upper()}"
    key = pool.get(key_name) or os.environ.get(key_name, "")
    if not key:
        print(f"SKIP: no {key_name} in ~/.feedling-e2e-keys.env")
        return 0

    with E2EClient.provision(route="model_api", archive_language="zh-Hans") as c:
        print(f"probe user: {c.user_id} model={args.model}")
        r = c.post("/v1/model_api/setup", json={
            "provider": args.provider, "model": args.model, "api_key": key})
        if not check("model_api setup", r.status_code in (200, 201),
                     f"{r.status_code} {r.text[:120]}"):
            return 1

        # ① 先用中文建立一个中文花园。
        marker = new_marker()
        zh_text = (
            f"帮我记一件事，测试锚点是 {marker}。我不吃辣，一吃就胃疼，"
            "所以点菜的时候都得避开辣的，麻烦你以后帮我盯着点。"
        )
        _sent, err = _hosted_send(c, zh_text)
        if not check("chat send (中文)", not err, err or ""):
            return 1
        forced = force_capture_until_enqueued(c)
        if not check("capture 真的入队了（中文那轮）",
                     bool(forced.get("enqueued")) or forced.get("reason") == "already_captured",
                     f"{forced}", pass_detail=str(forced.get("reason") or "enqueued")):
            return 1
        cards = _wait_for_new_cards(c, known=set(), timeout=CAPTURE_POLL_SEC)
        if not check("中文花园建立了", bool(cards),
                     f"waited {CAPTURE_POLL_SEC:.0f}s, no card",
                     pass_detail=f"{len(cards)} 张"):
            return 1

        first_blob = " ".join(
            f"{i.get('summary') or ''} {i.get('content') or ''} {i.get('bucket') or ''}"
            for i in cards)
        check("第一批卡是中文的", _is_chinese(first_blob),
              repr(first_blob[:100]), pass_detail=repr(first_blob[:60]))

        before_ids = _ids(cards)

        # ② 再用英文说一句 —— **这是事故的触发形状**。
        en_text = (
            "By the way, I had a really rough week at work. "
            "My manager keeps changing the spec and I stayed until 11pm three days in a row."
        )
        _sent, err = _hosted_send(c, en_text)
        if not check("chat send (英文)", not err, err or ""):
            return 1
        forced = force_capture_until_enqueued(c)
        if not check("capture 真的入队了（英文那轮）",
                     bool(forced.get("enqueued")) or forced.get("reason") == "already_captured",
                     f"{forced}", pass_detail=str(forced.get("reason") or "enqueued")):
            return 1
        after = _wait_for_new_cards(c, known=before_ids, timeout=CAPTURE_POLL_SEC)

        new_cards = [i for i in after if str(i.get("id") or "") not in before_ids]
        # 英文那句可能本来就不值得记（模型判它是抱怨、不是事实）—— 那不算失败。
        # 真正要守的是**桶名和已有卡没有被翻语言**，那条在下面。
        if not new_cards:
            print("[INFO] 英文那句没产出新卡（模型判它不值得记）—— "
                  "不算失败，下面照样验花园没被翻语言")

        # 🔴 核心断言：花园没有因为一句英文就翻语言。
        buckets = [str(i.get("bucket") or "") for i in after if i.get("bucket")]
        bucket_blob = " ".join(buckets)
        check("🔴 桶名没有翻成英文", _is_chinese(bucket_blob) if bucket_blob else True,
              f"桶名变成了 {buckets}", pass_detail=f"{buckets}")

        # 整个花园（不只新卡）都要还是中文 —— 事故的表现是**已有卡也被改写**。
        all_blob = " ".join(
            f"{i.get('summary') or ''} {i.get('content') or ''}" for i in after)
        check("🔴 整个花园的卡仍然是中文", _is_chinese(all_blob),
              repr(all_blob[:150]), pass_detail=f"{len(after)} 张，"
                                                f"{repr(all_blob[:60])}")
        if new_cards:
            new_blob = " ".join(
                f"{i.get('summary') or ''} {i.get('content') or ''}" for i in new_cards)
            check("🔴 英文那句产出的新卡也是中文", _is_chinese(new_blob),
                  repr(new_blob[:120]), pass_detail=repr(new_blob[:80]))

    print()
    print("RESULT:", "ALL PASS" if not _fails else f"FAILURES({len(_fails)}): {_fails}")
    return 0 if not _fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
