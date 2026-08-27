#!/usr/bin/env python3
"""Reusable product sandbox for IO Memory readside.

This does not require a running backend. It uses stable, human-shaped fixtures
to show what /v1/memory/index and /v1/memory/fetch should feel like to a user.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass


BLOCKED_INDEX_FIELDS = {"verbatim", "her_quote", "follow_up"}
SALIENCE_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}


@dataclass(frozen=True)
class MemoryFixture:
    id: str
    summary: str
    verbatim: str
    bucket_refs: list[str]
    status: str = "active"
    salience: str = "medium"
    follow_up: str = ""
    context: str = ""
    source_type: str = "chat"
    is_open_thread: bool = False
    importance: float = 0.5


def fixture_cards() -> list[MemoryFixture]:
    return [
        MemoryFixture(
            id="mem_presence_first",
            summary="用户情绪崩溃时，先需要被陪着和确认感受，不要立刻给解决方案。",
            verbatim="我那时候不是想要建议，我只是想有人在。",
            bucket_refs=["安抚方式", "情绪低谷"],
            salience="high",
            follow_up="下次先说“我在”，再问要不要一起拆问题。",
            context="来自一次加班后情绪崩溃的聊天。",
            is_open_thread=True,
            importance=0.9,
        ),
        MemoryFixture(
            id="mem_private_boundary",
            summary="用户愿意聊亲密和 XP，但前提是语气安全、可退出、不评判。",
            verbatim="可以聊，但不要像审问，也不要突然变得很露骨。",
            bucket_refs=["亲密边界", "隐私偏好"],
            salience="high",
            follow_up="涉及亲密话题时先确认边界，用温和词，不追问细节。",
            context="来自对人机恋聊天边界的讨论。",
            importance=0.85,
        ),
        MemoryFixture(
            id="mem_work_style",
            summary="用户是产品视角的前端开发，更需要先看地图、流程和影响，再进入技术细节。",
            verbatim="你跟我说服务端的时候，要说人话，我不是后端。",
            bucket_refs=["协作方式", "解释偏好"],
            salience="critical",
            follow_up="复杂工程问题默认先讲结论、全景、影响和下一步。",
            context="来自多次工程协作反馈。",
            importance=0.95,
        ),
        MemoryFixture(
            id="mem_reassurance",
            summary="用户在不确定时需要明确判断，不喜欢空泛安慰；要给可执行路径。",
            verbatim="你现在是老大，你来决定，开始做吧。",
            bucket_refs=["决策偏好", "协作方式"],
            salience="medium",
            follow_up="给建议时要明确推荐，而不是只列选项。",
            context="来自工程计划讨论。",
            importance=0.7,
        ),
        MemoryFixture(
            id="mem_lark_workflow",
            summary="用户希望 agent 能帮忙读 Lark 群消息、整理重点，并辅助工作反应。",
            verbatim="我想有时候远程的时候，可以让你帮我看看群里发生了什么。",
            bucket_refs=["工作流", "Lark"],
            salience="medium",
            follow_up="涉及 Lark 时优先总结行动项和需要回应的人。",
            context="来自 Lark CLI 权限和使用场景讨论。",
            importance=0.6,
        ),
        MemoryFixture(
            id="mem_cat_care",
            summary="用户聊猫咪健康问题时，先需要被安抚，再给观察饮水、精神状态和持续拒食时就医的建议。",
            verbatim="猫咪今天不怎么吃饭，我有点慌。",
            bucket_refs=["猫咪", "宠物照顾", "安抚方式"],
            salience="high",
            follow_up="先回应担心，再建议观察精神、饮水、排便；如果持续不吃或精神差，及时联系兽医。",
            context="来自一次关于猫咪不吃饭的担心。",
            is_open_thread=True,
            importance=0.88,
        ),
    ]


def score(card: MemoryFixture) -> float:
    open_bonus = 1.0 if card.is_open_thread else 0.0
    salience = SALIENCE_WEIGHT.get(card.salience, 2)
    return round(open_bonus + salience + card.importance, 4)


def build_index(cards: list[MemoryFixture], *, limit: int) -> list[dict]:
    ordered = sorted(
        cards,
        key=lambda card: (
            1 if card.is_open_thread else 0,
            SALIENCE_WEIGHT.get(card.salience, 2),
            card.importance,
            card.id,
        ),
        reverse=True,
    )
    return [
        {
            "id": card.id,
            "summary": card.summary,
            "bucket_refs": card.bucket_refs,
            "status": card.status,
            "salience": card.salience,
            "is_open_thread": card.is_open_thread,
            "score": score(card),
        }
        for card in ordered[:limit]
    ]


def build_fetch(cards: list[MemoryFixture], ids: list[str]) -> dict:
    by_id = {card.id: card for card in cards}
    items: list[dict] = []
    missing_ids: list[str] = []
    for memory_id in ids:
        card = by_id.get(memory_id)
        if card is None:
            missing_ids.append(memory_id)
            continue
        items.append({
            "id": card.id,
            "summary": card.summary,
            "verbatim": card.verbatim,
            "bucket_refs": card.bucket_refs,
            "status": card.status,
            "salience": card.salience,
            "follow_up": card.follow_up,
            "context": card.context,
            "source_type": card.source_type,
        })
    return {"items": items, "missing_ids": missing_ids, "unavailable_ids": []}


def run_sandbox(limit: int = 10, fetch_count: int = 3) -> dict:
    cards = fixture_cards()
    index_items = build_index(cards, limit=limit)
    ids = [item["id"] for item in index_items[:fetch_count]]
    fetch = build_fetch(cards, ids)
    leaked = [item["id"] for item in index_items if BLOCKED_INDEX_FIELDS.intersection(item.keys())]
    return {
        "index": {"items": index_items},
        "fetch": fetch,
        "acceptance": {
            "index_count": len(index_items),
            "fetch_count": len(fetch["items"]),
            "index_no_raw_quote": "PASS" if not leaked else "FAIL",
            "leaked_index_ids": leaked,
        },
    }


def _clip(text: str, width: int = 92) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= width:
        return clean
    return clean[: width - 1].rstrip() + "…"


def print_report(result: dict) -> None:
    print("=== 1. index: agent 先看到的安全摘要目录 ===")
    for idx, item in enumerate(result["index"]["items"], start=1):
        print(
            f"{idx:02d}. {item['id']} | salience={item['salience']} | "
            f"score={item['score']}"
        )
        print(f"    summary: {_clip(item['summary'])}")
        print(f"    buckets: {', '.join(item['bucket_refs'])}")

    print("\n=== 2. fetch: agent 命中后拿到的完整正文 ===")
    for idx, item in enumerate(result["fetch"]["items"], start=1):
        print(f"{idx:02d}. {item['id']}")
        print(f"    summary : {_clip(item['summary'])}")
        print(f"    verbatim: {_clip(item['verbatim'])}")
        if item["follow_up"]:
            print(f"    follow  : {_clip(item['follow_up'])}")
        if item["context"]:
            print(f"    context : {_clip(item['context'])}")

    acceptance = result["acceptance"]
    print("\n=== 3. 产品验收结论 ===")
    print(f"index_count={acceptance['index_count']}")
    print(f"fetch_count={acceptance['fetch_count']}")
    print(f"index_no_raw_quote={acceptance['index_no_raw_quote']}")
    print("\n人话：")
    print("- index 是 agent 先看的目录：只给摘要和分类。")
    print("- fetch 是 agent 命中后拿的正文：可以看到原话、上下文和 follow_up。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a reusable product sandbox for IO Memory readside.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--fetch", type=int, default=3)
    args = parser.parse_args()

    result = run_sandbox(limit=max(1, args.limit), fetch_count=max(0, args.fetch))
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_report(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
