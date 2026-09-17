"""落卡请求的唯一构造点（``memory.garden_component.capture_request``）—— 两条 runtime 共用。

## 为什么有这个文件

2026-08-30（fd963bf9）V1 / V2 落卡换成 Garden 组件之后，请求在两个调用点各拼一份，
两份漏了同样的三样东西：

1. **已有记忆索引**：提示词里是 ``(none)``，模型抄不到 target_id，只能 add，
   同一件事说两次就是两张卡（V2 08-03 起有过这份索引，V1 从来没有）。
2. **io 的称呼规则**：没传 ``naming_rule``，英文花园用的是内核默认那版。
3. **洗过的名字**：V2 把身份卡里存成「用户」的名字原样交出去。

这里守的是**生产真实发出去的提示词**（从组件会话里取，不是测试专用的拼装壳），
外加 extract() 把索引规模 / 编造 id 被丢这两件事写进轨迹。V1 / V2 各自的入口
（consumer 的 capture handler、worker 的 ``_run_extraction`` + jobs_store 真提交）
见 test_chat_resident_consumer.py / test_v2_extraction_lanes.py 里的同名用例。

重新生成快照：``python tests/test_capture_request_index_and_naming.py --regen``。
**重新生成是能掩盖真回归的动作** —— 下面的「守意图」断言不随快照走。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from identity.user_naming import _naming_rule  # noqa: E402
from memgarden.naming import naming_rule as kernel_naming_rule  # noqa: E402
from memory import garden_component  # noqa: E402
from memory.garden_component import IO_CONVERSATION_CAPTURE_POLICY  # noqa: E402
from model_api_runtime.v2 import extraction as v2_extraction  # noqa: E402

_FIXTURE = ROOT / "tests" / "fixtures" / "memgarden" / "capture_prompt_production.json"

INDEX_ITEMS = [
    {"id": "mom_job", "summary": "老王在字节跳动做产品经理，负责电商", "bucket": "工作",
     "importance": 0.5, "status": "active", "score": 0.4, "created_at": "2026-08-01T00:00:00Z"},
    {"id": "mom_cat", "summary": "老王养了一只叫年糕的橘猫", "bucket": "宠物",
     "importance": 0.4, "status": "active"},
    {"id": "mom_old_job", "summary": "老王以前在美团", "bucket": "工作",
     "importance": 0.5, "status": "superseded"},
]
INDEX_ITEMS_EN = [
    {"id": "mom_job", "summary": "Alex is a PM at ByteDance working on e-commerce", "bucket": "Work",
     "importance": 0.5},
    {"id": "mom_cat", "summary": "Alex has an orange cat named Mochi", "bucket": "Pets",
     "importance": 0.4},
]

CASES: dict[str, dict] = {
    "zh_with_index": dict(
        window="老王：我上周从字节跳动离职了，下个月去腾讯做产品经理\nio：恭喜！",
        locale="zh-Hans", buckets="工作、宠物", threads="换工作", identity='{"agent_name":"io"}',
        ai_name="io", user_name="老王", items=INDEX_ITEMS,
    ),
    "en_with_index": dict(
        window="Alex: I left ByteDance last week, joining Tencent as a PM next month\nIris: congrats!",
        locale="en", buckets="Work, Pets", threads="job change", identity='{"agent_name":"Iris"}',
        ai_name="Iris", user_name="Alex", items=INDEX_ITEMS_EN,
    ),
    "placeholder_name_raw_from_identity": dict(
        window="对方：我换工作了\n小舟：真好",
        locale="zh-Hans", buckets="", threads="", identity="",
        ai_name="  小舟 \n", user_name=" 用户 ", items=[],
    ),
    "index_unavailable": dict(
        window="Alex: moved to Tencent\nIris: nice",
        locale="en", buckets="", threads="", identity="",
        ai_name="Iris", user_name="user", items=None,
    ),
}


def _request(case: dict):
    items = case["items"]
    return garden_component.capture_request(
        window=case["window"], locale=case["locale"], buckets=case["buckets"],
        threads=case["threads"], identity=case["identity"], ai_name=case["ai_name"],
        user_name=case["user_name"],
        existing_cards=None if items is None else garden_component.capture_existing_cards(items),
    )


def _prompt(case: dict) -> str:
    garden = garden_component.build_garden(garden_component.CallableModel(lambda _p: ""))
    return garden.capture_session(_request(case)).next_prompt()


def _index_section(prompt: str) -> str:
    return prompt.split("target_id from here)]", 1)[1].split("\n[", 1)[0]


# --------------------------------------------------------------------------- #
# 快照：生产提示词逐字节
# --------------------------------------------------------------------------- #

def test_production_capture_prompts_match_snapshot():
    expected = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert set(expected) == set(CASES)
    for name, case in CASES.items():
        assert _prompt(case) == expected[name], name


# --------------------------------------------------------------------------- #
# 守意图（不随快照走）
# --------------------------------------------------------------------------- #

def test_index_lists_active_cards_with_ids_and_skips_superseded():
    index = _index_section(_prompt(CASES["zh_with_index"]))
    assert "- mom_job: [工作] 老王在字节跳动做产品经理，负责电商" in index
    assert "- mom_cat: [宠物] 老王养了一只叫年糕的橘猫" in index
    assert "mom_old_job" not in index
    # 模型只该看到 id / 桶 / 摘要，读侧的分数、时间不进提示词。
    assert "2026-08-01" not in index


def test_empty_garden_and_unavailable_index_both_render_none_but_differ_in_validation():
    assert _index_section(_prompt(CASES["placeholder_name_raw_from_identity"])) == "(none)"
    assert _index_section(_prompt(CASES["index_unavailable"])) == "(none)"
    assert _request(CASES["placeholder_name_raw_from_identity"]).existing_cards == []
    assert _request(CASES["index_unavailable"]).existing_cards is None


def test_names_are_sanitized_and_the_rule_is_ios_not_the_kernel_default():
    en = _request(CASES["en_with_index"])
    assert en.naming_rule == _naming_rule("Alex", locale="en")
    # 两版英文规则确实不同 —— 否则这条断言守不住任何东西。
    assert en.naming_rule != kernel_naming_rule("Alex", locale="en")
    assert en.naming_rule in _prompt(CASES["en_with_index"])

    placeholder = _request(CASES["placeholder_name_raw_from_identity"])
    assert placeholder.user_name == "TA"
    assert placeholder.ai_name == "小舟"
    assert placeholder.naming_rule == _naming_rule("用户", locale="zh-Hans")
    prompt = _prompt(CASES["placeholder_name_raw_from_identity"])
    assert "用户's companion" not in prompt
    assert "提到 用户" not in prompt
    assert "You are 小舟, 这个人's companion" in prompt


def test_request_carries_io_policy_and_explicit_index_budgets():
    req = _request(CASES["zh_with_index"])
    assert req.policy is IO_CONVERSATION_CAPTURE_POLICY
    assert (req.index_cards_limit, req.index_budget_chars, req.index_summary_chars) == (
        garden_component.CAPTURE_INDEX_CARDS_LIMIT,
        garden_component.CAPTURE_INDEX_BUDGET_CHARS,
        garden_component.CAPTURE_INDEX_SUMMARY_CHARS,
    )


def test_index_is_bounded_and_relevant_first_on_a_large_garden():
    filler = [
        {"id": f"mom_core_{i:03d}", "summary": f"老王家里的第{i}件大事，和工作无关", "bucket": "家庭",
         "importance": 0.9}
        for i in range(400)
    ]
    case = dict(CASES["zh_with_index"], items=[*filler, {**INDEX_ITEMS[0], "importance": 0.1}])
    rows = [r for r in _index_section(_prompt(case)).splitlines() if r.startswith("- ")]
    assert len(rows) == garden_component.CAPTURE_INDEX_CARDS_LIMIT
    assert rows[0].startswith("- mom_job: ")
    assert len("\n".join(rows)) <= garden_component.CAPTURE_INDEX_BUDGET_CHARS


def test_old_kernel_degrades_to_no_index_but_keeps_io_naming(monkeypatch):
    """自建 consumer 可能新代码跑在旧 memgarden 上：落卡降级回无索引，不整窗口失败。"""
    assert garden_component.capture_kernel_selects_index()
    monkeypatch.setattr(garden_component, "_CAPTURE_INDEX_FIELDS", ("field_from_the_future",))
    assert not garden_component.capture_kernel_selects_index()
    req = _request(CASES["en_with_index"])
    assert req.existing_cards is None
    assert req.naming_rule == _naming_rule("Alex", locale="en")
    assert _index_section(_prompt(CASES["en_with_index"])) == "(none)"


def test_existing_cards_mapping_is_minimal_and_dedupes():
    cards = garden_component.capture_existing_cards([
        {"id": " m1 ", "title": "旧标题\n字段", "category": "工作", "importance": 0.3, "score": 9},
        {"id": "m1", "summary": "重复"},
        {"id": "m2", "summary": "", "status": "ACTIVE"},
        {"id": "m3", "summary": "归档", "status": "archived"},
        {"summary": "没有 id"},
        "junk",
    ])
    assert cards == [
        {"id": "m1", "summary": "旧标题 字段", "bucket": "工作", "importance": 0.3},
        # 没摘要的真卡保留：不进索引，但它是合法的 target。
        {"id": "m2", "summary": ""},
    ]


# --------------------------------------------------------------------------- #
# V2 extract()：索引规模与「编造 id 被丢」进轨迹
# --------------------------------------------------------------------------- #

def _card(target: str) -> dict:
    return {"action": "supersede", "type": "fact", "target_id": target, "bucket": "工作",
            "threads": ["换工作"], "summary": "老王下个月去腾讯做产品经理",
            "content": "老王上周从字节跳动离职，下个月去腾讯继续做产品经理。"}


def test_extract_records_index_size_and_unknown_target_drop_in_trajectory(monkeypatch):
    replies = [json.dumps({"cards": [_card("mom_made_up")]}, ensure_ascii=False)] * 2

    async def _provider(_cfg, messages, **_kw):
        return {"reply": replies.pop(0), "stop_reason": "stop", "usage": {}}

    monkeypatch.setattr(v2_extraction.provider_client, "reliable_chat_completion_async", _provider)
    sink = garden_component.BounceTracker()
    session = garden_component.build_garden(
        garden_component.CallableModel(lambda _p: ""), on_step=sink,
    ).capture_session(_request(CASES["zh_with_index"]))
    events: list[tuple[str, dict]] = []

    async def _traj(kind, payload):
        events.append((kind, payload))

    items, reason = asyncio.run(v2_extraction.extract(
        provider_config=object(), prompt="", parse=lambda _r: ([], None),
        session=session, step_sink=sink, trajectory_out=_traj,
    ))
    assert (items, reason) == ([], None)
    kinds = [k for k, _ in events]
    index_event = dict(events[kinds.index("capture_index")][1])
    assert index_event == {"index_candidates": 2, "index_cards": 2,
                           "index_chars": index_event["index_chars"]}
    assert index_event["index_chars"] > 0
    assert ("unknown_target_dropped", {"cards": 1}) in events
    assert sink.index_detail() == {**index_event, "dropped_unknown_target": 1}


if __name__ == "__main__" and "--regen" in sys.argv:
    _FIXTURE.write_text(
        json.dumps({name: _prompt(case) for name, case in CASES.items()},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {_FIXTURE}")
