"""花园语言判定 eval —— 确定性，不调模型，毫秒级。

## 为什么住在 io 而不是包里

判定「这个花园是什么语言」要读身份卡、历史记忆、客户端 locale —— 那些都是宿主的
数据，内核碰不到。所以逻辑在 io，eval 也跟着 io 走。

内核那边有自己的 eval（挑卡质量、落卡质量），各守各的。

## 为什么单独立一条

2026-08-24 生产事故：一个 226 张卡的中文花园，两天内新落的卡整个变成英文。

根因不在提示词，在**这个判定函数**：它拿 CJK 与拉丁**字符数**比大小，而中英文
桶名长度根本不对等 —— 「工作」两字符，「Our relationship」十五字符，一个英文桶
顶七个中文桶。于是"8 个中文桶 + 2 个英文桶"判成英文花园。

而那几个英文桶是**更早一个 bug 的残留**（老提示词同时给中英两套让模型挑，
约 1/3 的中文记忆被贴错）。两个 bug 单独看都不致命，叠起来是整个花园翻转：

    旧残留 → 判成英文花园 → 新卡全用英文桶 → 英文桶更多 → 自我强化

这类判定**没有中间态**：一旦判错，用户看到的是记忆突然换了语言。所以它值得一条
独立的、每次发布都跑的 eval，而不是混在单测里。
"""
from __future__ import annotations

import json
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_IO_BACKEND = _HERE.parent / "backend"


def _load_decider():
    """判定逻辑住在宿主 io 里（它要读身份卡和历史，内核碰不到那些）。

    所以这条 eval 需要 io 的源码在手。拿不到就明确 SKIP，**不静默通过** ——
    静默通过的 eval 比没有 eval 更糟：它给人已经测过的错觉。
    """
    if not _IO_BACKEND.exists():
        return None
    sys.path.insert(0, str(_IO_BACKEND))
    try:
        from chat.reply_language import garden_language_decision
        return garden_language_decision
    except Exception:
        return None


def run() -> dict:
    decide = _load_decider()
    if decide is None:
        return {"skipped": True, "reason": f"找不到宿主 io 的 backend：{_IO_BACKEND}"}

    cases = [json.loads(l) for l in
             (_HERE / "corpus" / "gardens.jsonl").read_text(encoding="utf-8").splitlines()
             if l.strip()]
    wrong, results = [], []
    for c in cases:
        got = decide({}, existing_buckets=c["buckets"])["locale"]
        ok = got == c["expect"]
        results.append({"gid": c["gid"], "desc": c["desc"], "expect": c["expect"],
                        "got": got, "ok": ok, "incident": c.get("incident", "")})
        if not ok:
            wrong.append(results[-1])
    return {"skipped": False, "total": len(cases), "wrong": len(wrong),
            "flips": sum(1 for w in wrong if w["expect"] == "zh-Hans" and w["got"] == "en"),
            "results": results}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    r = run()
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0 if (r.get("skipped") or r["wrong"] == 0) else 1

    if r.get("skipped"):
        print(f"SKIP: {r['reason']}")
        return 0
    print(f"花园语言判定 · {r['total']} 个场景\n")
    for x in r["results"]:
        mark = "✅" if x["ok"] else "❌"
        note = "  ← 曾导致线上事故" if x["incident"] and not x["ok"] else ""
        print(f"  {mark} {x['desc']:26} 期望 {x['expect']:8} 实得 {x['got']}{note}")
    if r["wrong"]:
        print(f"\n❌ {r['wrong']} 个判错，其中 {r['flips']} 个是"
              f"**把中文花园判成英文** —— 那是用户能立刻看见的破坏")
        return 1
    print("\n✅ 全部正确")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
