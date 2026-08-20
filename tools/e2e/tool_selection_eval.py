#!/usr/bin/env python3
"""工具选择评测:模型在真实工具目录下,能不能按用户的话挑对工具。

两条轨必须分开解释:
  first-action : 单轮只测模型第一步选了什么,不是 production completion
  completion   : 用确定性 ToolExchange fixture 测前置→目标→最终可见答复

回答四件事:
  1. 今天的第一步工具选择已经坏了吗(完整目录基线)
  2. 折叠(只给名字 + 短描述)的真实代价是多少
  3. 改描述到底有没有用 —— 改完重跑同一套用例逐族对比
  4. 需要 id/revision 的任务能否在多轮里真正完成,而不只选对前置工具

为什么直接打 provider 而不走我们后端:这一阶段只要隔离「描述」这一个变量。
走全链路会把记忆注入、人设、上下文组装混进来,污染结论。

两条臂量的不是同一件事,绝对分数不可互比,只比臂内差值:
  deepseek : 真实 API,带 tools=[...],解析 message.tool_calls[].function.name
  sonnet   : 不买 key,导出 prompt 由调用方起 Claude 子 agent 当被测模型;
             量的是「对描述的理解」,不走 provider 原生 tool-call 通路。

用法:
  python3 tools/e2e/tool_selection_eval.py --selftest
  python3 tools/e2e/tool_selection_eval.py --arm deepseek --mode both
  python3 tools/e2e/tool_selection_eval.py --arm sonnet-export --mode folded > /tmp/x.json
"""
from __future__ import annotations

import argparse
import collections
import copy
import json
import os
import pathlib
import sys
import time

import httpx

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "backend"))

from capabilities import tool_schema as ts  # noqa: E402
from model_api_runtime.v2 import tool_surface as v2_tool_surface  # noqa: E402

_CASES_PATH = pathlib.Path(__file__).with_name("tool_selection_cases.json")
_KEYS_PATH = pathlib.Path.home() / ".feedling-e2e-keys.env"
_DEEPSEEK_BASE = "https://api.deepseek.com/v1"
_DEEPSEEK_MODEL = "deepseek-chat"


def _load_keys() -> dict[str, str]:
    out: dict[str, str] = {}
    if not _KEYS_PATH.exists():
        return out
    for line in _KEYS_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _to_openai_tool(spec) -> dict:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": str(spec.description or ""),
            "parameters": spec.parameters,
        },
    }


# chat 轮不传 on_stay_silent,tool_loop 因此禁用 stay_silent —— 它在 chat 里
# 根本不存在。目录与 coverage 必须用同一个来源,不能一边说"不提供"一边发给模型。
_NOT_OFFERED_IN_CHAT = frozenset({"stay_silent"})


def evaluated_chat_specs():
    return [s for s in ts.build_tool_specs() if s.name not in _NOT_OFFERED_IN_CHAT]


def build_catalog(mode: str) -> list[dict]:
    """完整目录 or 折叠目录。折叠走生产同一个函数,不另写一份。"""
    specs = evaluated_chat_specs()
    if mode == "full":
        return [_to_openai_tool(s) for s in specs]
    if mode == "folded":
        # 生产折叠**保留 RESIDENT_TOOL_NAMES 的完整 schema**,只折其余。
        # 第一版把这 5 个也折了(memory_search 1694→119、memory_write 2500→120…),
        # 于是测出的"折叠代价"比生产激进得多,不是生产的折叠形态。
        return [
            _to_openai_tool(
                s if s.name in v2_tool_surface.RESIDENT_TOOL_NAMES
                else v2_tool_surface.collapsed_tool_spec(s)
            )
            for s in specs
        ]
    raise ValueError(f"unknown mode: {mode}")


# --------------------------------------------------------------------------
# 量具自检:一组「故意写得极清楚」的假工具。评测器是好的 → 这组该接近满分。
# 这组不及格 = 是评测器/模型坏了,不是我们的描述坏了。
# --------------------------------------------------------------------------
_SELFTEST_TOOLS = [
    ("st_get_battery", "Return the current battery percentage of the phone."),
    ("st_send_email", "Send an email to a named recipient with a subject and body."),
    ("st_book_flight", "Book a flight ticket between two airports on a given date."),
    ("st_translate", "Translate a piece of text from one language into another."),
    ("st_play_music", "Start playing a named song or album on the speaker."),
]
_SELFTEST_CASES = [
    ("st_get_battery", "我手机还剩多少电?"),
    ("st_send_email", "给张伟发封邮件说我明天到"),
    ("st_book_flight", "订一张下周一北京飞上海的机票"),
    # 原来写的是「把这句话翻译成英文」——没给出要翻的句子,模型合理地先反问,
    # 判成错是我的题坏了不是工具描述坏了。自检抓到的第一个真问题。
    ("st_translate", "把「今天天气很好」翻译成英文"),
    ("st_play_music", "放一首周杰伦的歌"),
]


def _selftest_catalog() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": d,
                "parameters": {
                    "type": "object", "properties": {}, "additionalProperties": False
                },
            },
        }
        for n, d in _SELFTEST_TOOLS
    ]


def load_cases() -> dict:
    return json.loads(_CASES_PATH.read_text(encoding="utf-8"))


_RUNTIME_FIXTURE_NAMES = frozenset({"scheduled_wakes", "screen_share_active"})


def check_case_health(cases_doc: dict) -> list[str]:
    """检查真实题库本身,避免 ``--selftest`` 绿却拿污染题面量模型。

    这里刻意只做可机械证明的结构契约；金标语义仍需独立人工复核，不能
    把 structural health 写成“题目一定正确”。
    """
    problems: list[str] = []
    cases = list(cases_doc.get("cases") or [])
    native = list(cases_doc.get("native_image_cases") or [])
    completion = list(cases_doc.get("completion_cases") or [])

    all_ids = [c.get("id") for c in [*cases, *native, *completion]]
    dup = sorted({case_id for case_id in all_ids if all_ids.count(case_id) > 1})
    if dup:
        problems.append(f"跨轨重复的 case id: {dup}")

    for case in cases:
        case_id = case.get("id")
        fixtures = list(case.get("runtime_fixtures") or [])
        unknown = sorted(set(fixtures) - _RUNTIME_FIXTURE_NAMES)
        if unknown:
            problems.append(f"{case_id}: 未知 runtime fixture {unknown}")
        if "screen_share_active" in fixtures and case.get("family") != "screen":
            problems.append(f"{case_id}: screen_share_active 污染了非 screen case")
        if "scheduled_wakes" in fixtures and case.get("expect") != "cancel_wake":
            problems.append(f"{case_id}: scheduled_wakes 污染了非 cancel_wake case")
        if str(case.get("expect") or "").startswith("screen_") and (
            "screen_share_active" not in fixtures
        ):
            problems.append(f"{case_id}: screen case 缺 active screen fixture")
        if case.get("expect") == "cancel_wake" and (
            "scheduled_wakes" not in fixtures
        ):
            problems.append(f"{case_id}: cancel_wake 缺 scheduled_wakes fixture")
        if case.get("expect") in {"photo_recent", "photo_read"} and any(
            marker in str(case.get("utterance") or "")
            for marker in ("刚发的", "这张图", "当前消息")
        ):
            problems.append(f"{case_id}: 当前消息附图被误标成图库工具题")

    if not native:
        problems.append("缺 native_image_cases：当前消息附图场景没有单独建模")
    for case in native:
        if case.get("input_fixture") != "current_message_native_image":
            problems.append(
                f"{case.get('id')}: native image 必须声明当前消息原生图片 fixture"
            )
        if case.get("expected_action") != "answer_from_native_image":
            problems.append(f"{case.get('id')}: native image 必须期望直接回答")
        if case.get("expect") or case.get("also_ok"):
            problems.append(f"{case.get('id')}: native image 不得期望 photo 工具")

    for case in completion:
        steps = list(case.get("tool_steps") or [])
        if len(steps) < 2:
            problems.append(f"{case.get('id')}: completion 至少要有前置与目标两步")
        for index, step in enumerate(steps):
            if not str(step.get("expect") or "").strip():
                problems.append(f"{case.get('id')}: tool_steps[{index}] 缺 expect")
            if not isinstance(step.get("result"), dict):
                problems.append(f"{case.get('id')}: tool_steps[{index}] 缺确定性 result")
        if not case.get("require_visible_reply", False):
            problems.append(f"{case.get('id')}: completion 必须检查最终可见答复")
    return problems


def check_coverage(cases_doc: dict) -> list[str]:
    """覆盖率断言:工具名从模块读,不许用例集里硬编一份。"""
    real = {s.name for s in ts.build_tool_specs()}
    excluded = set(cases_doc.get("excluded_tools") or {})
    expected = {c["expect"] for c in cases_doc["cases"]}

    problems: list[str] = check_case_health(cases_doc)
    ids = [c.get("id") for c in cases_doc["cases"]]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        problems.append(f"重复的 case id: {sorted(dup)}")
    for c in cases_doc["cases"]:
        if not str(c.get("utterance") or "").strip():
            problems.append(f"{c.get('id')}: utterance 为空")
        if not c.get("family"):
            problems.append(f"{c.get('id')}: 缺 family")
        for a in (c.get("also_ok") or []):
            if a not in real:
                problems.append(f"{c.get('id')}: also_ok 指向不存在的工具 {a}")
    # expect 和 also_ok 的**全部**引用都要与 excluded 求交 —— 只查 expect 会漏:
    # 把某条 also_ok 改成已排除但仍在 chat catalog 里的工具(如 mcp_tool_search),
    # 旧版 check_coverage 会返回 [](codex 用突变实测复现)。
    referenced = set(expected)
    for c in cases_doc["cases"]:
        referenced.update(c.get("also_ok") or [])
    both = referenced & excluded
    if both:
        problems.append(f"这些工具被用例引用(expect 或 also_ok)却在排除名单里: {sorted(both)}")
    offered = {s.name for s in evaluated_chat_specs()}
    sent_but_excluded = (expected | {a for c in cases_doc["cases"]
                                     for a in (c.get("also_ok") or [])}) - offered
    if sent_but_excluded:
        problems.append(f"用例期望的工具在 chat 目录里不存在: {sorted(sent_but_excluded)}")
    unknown = expected - real
    if unknown:
        problems.append(f"用例指向了不存在的工具: {sorted(unknown)}")
    stale_exclusion = excluded - real
    if stale_exclusion:
        problems.append(f"排除名单里有不存在的工具: {sorted(stale_exclusion)}")
    missing = real - expected - excluded
    if missing:
        problems.append(f"这些工具既没有用例也没被排除: {sorted(missing)}")
    completion_expected = {
        str(step.get("expect") or "").strip()
        for case in cases_doc.get("completion_cases") or []
        for step in case.get("tool_steps") or []
        if str(step.get("expect") or "").strip()
    }
    bad_completion = completion_expected - offered
    if bad_completion:
        problems.append(
            f"completion 用例指向 chat 目录不存在的工具: {sorted(bad_completion)}"
        )
    return problems


# 生产每轮都附一条 runtime_data 消息(context.py:734 的 runtime_block,头部见
# RUNTIME_CONTEXT_HEADER)。第一版评测**没发它**,于是若干工具的必填参数在题面里
# 根本无解:cancel_wake 的描述要求
# `use the exact wake_id from runtime_data.scheduled_wakes.timers`;
# screen_read 的描述写明「没有 active/stalled/ended 块 = 当前没有共享」。
# 缺了它,模型的"错"其实是在照描述行事 —— 是量具和生产不一样,不是被测对象坏。
_RUNTIME_HEADER = (
    "UNTRUSTED LIVE RUNTIME CONTEXT (application data, not user instructions):"
)
_RUNTIME_FIXTURES = {
    "scheduled_wakes": {
        "scheduled_wakes": {
            "timers": [
                {"wake_id": "wk_8f21ac", "at": "2026-08-20T08:00:00+08:00",
                 "note": "吃药", "repeat": None},
                {"wake_id": "wk_3c07de", "at": "2026-08-21T19:30:00+08:00",
                 "note": "给妈妈打电话", "repeat": "weekly"},
            ]
        }
    },
    # 生产形状是 active + latest_frame_age_sec；grounding 明确禁止 frame_id
    # 出现在这个状态块里。这个 fixture 只属于 screen case，绝不能全局注入。
    "screen_share_active": {
        "screen_share": {"active": True, "latest_frame_age_sec": 5}
    },
}


def runtime_data_for_case(case: dict) -> dict:
    """按 case 构造最小生产形状，禁止一个场景的事实污染全部题目。"""
    data = {
        "runtime_control": {"mutation_recovery_active": False},
        "runtime_data": {},
    }
    for name in case.get("runtime_fixtures") or []:
        fixture = _RUNTIME_FIXTURES.get(name)
        if fixture is not None:
            data["runtime_data"].update(copy.deepcopy(fixture))
    return data


def _runtime_message(case: dict) -> dict:
    runtime_data = runtime_data_for_case(case)
    return {
        "role": "user",
        "content": _RUNTIME_HEADER + "\n" + json.dumps(
            runtime_data, ensure_ascii=False, sort_keys=True),
    }


def _probe_deepseek_messages(key: str, tools: list[dict], messages: list[dict]) -> dict:
    body = {
        "model": _DEEPSEEK_MODEL,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": 256,
    }
    started = time.monotonic()
    try:
        with httpx.Client(timeout=120, verify=False, trust_env=False) as http:
            r = http.post(
                f"{_DEEPSEEK_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json=body,
            )
    except Exception as exc:  # noqa: BLE001
        return {"outcome": "transport_error", "detail": str(exc)[:120]}
    took = int((time.monotonic() - started) * 1000)
    if r.status_code != 200:
        return {"outcome": "rejected", "status": r.status_code,
                "detail": r.text[:200], "ms": took}
    try:
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        assistant_message = choice.get("message") or {}
        calls = assistant_message.get("tool_calls") or []
        picked = [c.get("function", {}).get("name") for c in calls]
        usage = data.get("usage") or {}
    except Exception as exc:  # noqa: BLE001
        return {"outcome": "unparseable", "detail": str(exc)[:120], "ms": took}
    if not picked:
        return {"outcome": "no_tool_call", "ms": took,
                "text": str(assistant_message.get("content") or ""),
                "_assistant_message": assistant_message,
                "_tool_calls": calls,
                "prompt_tokens": usage.get("prompt_tokens")}
    return {"outcome": "picked" if len(picked) == 1 else "multiple_tool_calls",
            "picked": picked[0] if len(picked) == 1 else None,
            "picked_all": picked, "call_count": len(picked), "ms": took,
            "text": str(assistant_message.get("content") or ""),
            "_assistant_message": assistant_message,
            "_tool_calls": calls,
            "prompt_tokens": usage.get("prompt_tokens")}


def probe_deepseek(key: str, tools: list[dict], case: dict) -> dict:
    return _probe_deepseek_messages(
        key,
        tools,
        [_runtime_message(case), {"role": "user", "content": case["utterance"]}],
    )


# --------------------------------------------------------------------------
# 判分:严格指标(strict exact / strict ok)**不含**多调用,另立集合感知的一轴。
#
# 为什么多调用不能算命中:生产 V2 会并行执行同批多个合法读,所以
# `memory_search + history_search` 是**真实的过度选择** —— 包含目标,但多做了
# 一次不需要的读,增加延迟/成本,还把额外结果混进下一轮。既不是完全错误,
# 也不是完全正确,所以它需要自己的一轴,绝不并入 exact/ok,也不泛称 success。
# --------------------------------------------------------------------------
CLASSES = (
    "singleton_exact", "singleton_also_ok", "singleton_wrong",
    "multi_has_exact_plus_extra", "multi_has_also_ok_plus_extra",
    "multi_no_acceptable", "no_call", "invalid_observation",
)

FIRST_ACTION_METRIC_DEFINITIONS = {
    "strict_exact": (
        "valid observations: exactly one tool call equals expect; excludes also_ok"
    ),
    "strict_ok": (
        "valid observations: exactly one tool call equals expect or also_ok"
    ),
    "strict_exact_all_rows": (
        "all rows: strict_exact numerator; invalid observations remain in denominator"
    ),
    "strict_ok_all_rows": (
        "all rows: strict_ok numerator; invalid observations remain in denominator"
    ),
    "target_recall": "valid observations containing expect anywhere in the call set",
    "acceptable_recall": (
        "valid observations containing expect or also_ok anywhere in the call set"
    ),
    "overcall": "valid observations with more than one tool call",
    "overcall_given_target": (
        "valid observations with expect present that also contain extra tool calls"
    ),
}

COMPLETION_METRIC_DEFINITIONS = {
    "completion_rate": (
        "all completion cases: exact one-tool-per-round sequence, required arguments, "
        "and required visible final reply"
    ),
}


def classify_row(row: dict, case: dict) -> dict:
    """互斥分类 + 逐行布尔诊断。transport/provider 错误单列为 invalid_observation,
    **不进模型选择的分母** —— 那不是模型选错了。"""
    expect = case["expect"]
    allowed = set(case.get("also_ok") or [])
    outcome = row.get("outcome")
    picked_all = list(row.get("picked_all") or [])

    if outcome in {"transport_error", "rejected", "unparseable"}:
        cls = "invalid_observation"
    elif outcome == "no_tool_call":
        cls = "no_call"
    elif len(picked_all) == 1:
        one = picked_all[0]
        cls = ("singleton_exact" if one == expect
               else "singleton_also_ok" if one in allowed
               else "singleton_wrong")
    elif expect in picked_all:
        cls = "multi_has_exact_plus_extra"
    elif allowed & set(picked_all):
        cls = "multi_has_also_ok_plus_extra"
    else:
        cls = "multi_no_acceptable"

    return {
        "klass": cls,
        "target_present": expect in picked_all,
        "acceptable_present": bool(({expect} | allowed) & set(picked_all)),
        "overselected": len(picked_all) > 1,
        "valid": cls != "invalid_observation",
    }


def run_deepseek(key: str, mode: str, cases: list[dict],
                 cases_doc: dict) -> dict:
    tools = build_catalog(mode)          # frozen:后续一切都用这一个对象
    prov = provenance(mode, tools, cases_doc)   # 在**发请求之前**生成
    rows = []
    for case in cases:
        res = probe_deepseek(key, tools, case)
        picked = res.get("picked")
        # `also_ok`:合法的前置工具。用户说「读一下我那份方案」时先 workspace_list
        # 找路径是**正确行为**,判它错是题坏了不是模型坏了。
        d = classify_row(res, case)
        exact = d["klass"] == "singleton_exact"
        hit = d["klass"] in ("singleton_exact", "singleton_also_ok")
        public_res = {k: v for k, v in res.items() if not k.startswith("_")}
        rows.append({**case, **public_res, "correct": bool(hit),
                     "exact": bool(exact), "_d": d})
        mark = ("✓" if exact else "~" if hit
                else "+" if d["target_present"] else "!" if not d["valid"] else "✗")
        print(f"  {mark} {case['id']:16s} expect={case['expect']:24s} "
              f"got={picked or res.get('outcome')}", flush=True)
    return {"mode": mode, "arm": "deepseek", "provenance": prov, "rows": rows}


def score_completion_row(row: dict, case: dict) -> dict:
    """按完整调用序列与最终可见答复判 completion，不拿 first action 冒充。"""
    expected = [step["expect"] for step in case.get("tool_steps") or []]
    actual = list(row.get("actual_calls") or [])
    actual_rounds = list(row.get("actual_rounds") or [])
    expected_rounds = [[name] for name in expected]
    sequence_exact = actual == expected and actual_rounds == expected_rounds
    visible_reply = bool(str(row.get("final_text") or "").strip())
    argument_errors = list(row.get("argument_errors") or [])
    return {
        "expected_calls": expected,
        "actual_calls": actual,
        "expected_rounds": expected_rounds,
        "actual_rounds": actual_rounds,
        "sequence_exact": sequence_exact,
        "visible_reply": visible_reply,
        "total_calls": len(actual),
        "overcall": len(actual) > len(expected),
        "argument_errors": argument_errors,
        "completed": sequence_exact and not argument_errors and (
            visible_reply if case.get("require_visible_reply", False) else True
        ),
    }


def _tool_arguments(call: dict) -> tuple[dict, str | None]:
    raw = (call.get("function") or {}).get("arguments")
    if isinstance(raw, dict):
        return raw, None
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}, "arguments_not_json"
    if not isinstance(parsed, dict):
        return {}, "arguments_not_object"
    return parsed, None


def _required_arguments_error(step: dict, args: dict) -> str | None:
    for key, expected in (step.get("args_contains") or {}).items():
        if args.get(key) != expected:
            return f"{key}!={expected!r}"
    return None


def probe_deepseek_completion(key: str, tools: list[dict], case: dict) -> dict:
    """用确定性 ToolExchange fixture 跑 provider 多轮，不伪造 runtime_data id。

    这条轨隔离“目录描述 + provider 选择”；它不声称覆盖生产发布事务。后者由
    Runtime V2 E2E 负责。这里检查的是前置、目标、最终自然语言答复是否连成链。
    """
    messages = [
        _runtime_message(case),
        {"role": "user", "content": case["utterance"]},
    ]
    steps = list(case.get("tool_steps") or [])
    cursor = 0
    actual_calls: list[str] = []
    actual_rounds: list[list[str]] = []
    argument_errors: list[dict] = []
    final_text = ""
    invalid: dict | None = None

    for _round in range(len(steps) + 2):
        res = _probe_deepseek_messages(key, tools, messages)
        if res.get("outcome") in {"transport_error", "rejected", "unparseable"}:
            invalid = {k: v for k, v in res.items() if not k.startswith("_")}
            break
        calls = list(res.get("_tool_calls") or [])
        if not calls:
            final_text = str(res.get("text") or "")
            break

        round_names = [str((call.get("function") or {}).get("name") or "")
                       for call in calls]
        actual_rounds.append(round_names)
        assistant = dict(res.get("_assistant_message") or {})
        assistant.setdefault("role", "assistant")
        messages.append(assistant)
        advanced_this_round = False
        for call_index, call in enumerate(calls):
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            actual_calls.append(name)
            args, parse_error = _tool_arguments(call)
            expected_step = steps[cursor] if cursor < len(steps) else None
            args_error = (
                parse_error
                or (_required_arguments_error(expected_step, args)
                    if expected_step is not None else None)
            )
            if (
                expected_step is not None
                and not advanced_this_round
                and name == expected_step["expect"]
                and args_error is None
            ):
                result = expected_step["result"]
                cursor += 1
                advanced_this_round = True
            else:
                if name == (expected_step or {}).get("expect") and args_error:
                    argument_errors.append({
                        "round": _round,
                        "tool": name,
                        "error": args_error,
                    })
                result = {
                    "ok": False,
                    "error": "unexpected_tool_for_fixture",
                    "expected_next": (
                        steps[cursor]["expect"] if cursor < len(steps) else None
                    ),
                }
            messages.append({
                "role": "tool",
                "tool_call_id": str(call.get("id") or f"fixture-{_round}-{call_index}"),
                "content": json.dumps(result, ensure_ascii=False, sort_keys=True),
            })

    row = {
        "outcome": "invalid_observation" if invalid else "completion_observed",
        "actual_calls": actual_calls,
        "actual_rounds": actual_rounds,
        "argument_errors": argument_errors,
        "final_text": final_text,
        "invalid_detail": invalid,
    }
    return {**row, "_completion": score_completion_row(row, case)}


def report_completion(rows: list[dict]) -> dict:
    """Completion 轨单独报分母、序列、可见答复与 overcall。"""
    total = len(rows)
    completed_n = sum(r["_completion"]["completed"] for r in rows)
    exact_n = sum(r["_completion"]["sequence_exact"] for r in rows)
    visible_n = sum(r["_completion"]["visible_reply"] for r in rows)
    overcall_n = sum(r["_completion"]["overcall"] for r in rows)
    invalid_n = sum(r.get("outcome") == "invalid_observation" for r in rows)
    out = {
        "track": "production_completion_proxy",
        "scope_note": (
            "deterministic ToolExchange fixtures cover tool-chain completion; "
            "they do not cover Runtime V2 reply publication"
        ),
        "n": total,
        "completed_n": completed_n,
        "completion_rate": completed_n / total if total else None,
        "sequence_exact_n": exact_n,
        "visible_reply_n": visible_n,
        "overcall_n": overcall_n,
        "invalid_n": invalid_n,
        "total_calls": sum(r["_completion"]["total_calls"] for r in rows),
        "metric_definitions": COMPLETION_METRIC_DEFINITIONS,
    }
    print("\n=== completion track ===")
    print(f"completed={completed_n}/{total} sequence_exact={exact_n}/{total} "
          f"visible_reply={visible_n}/{total} overcall={overcall_n}/{total} "
          f"invalid={invalid_n}/{total}")
    print("⚠️ completion 使用确定性 ToolExchange fixture；覆盖工具链完成，"
          "不冒充 Runtime V2 气泡发布 E2E。")
    print("指标口径: completion_rate="
          + COMPLETION_METRIC_DEFINITIONS["completion_rate"])
    return out


def run_deepseek_completion(key: str, mode: str, cases: list[dict],
                            cases_doc: dict) -> dict:
    tools = build_catalog(mode)
    prov = {**provenance(mode, tools, cases_doc), "track": "completion"}
    rows = []
    for case in cases:
        observed = probe_deepseek_completion(key, tools, case)
        row = {**case, **observed}
        rows.append(row)
        mark = "✓" if row["_completion"]["completed"] else "✗"
        print(f"  {mark} {case['id']:20s} calls={row['actual_calls']} "
              f"visible={row['_completion']['visible_reply']}", flush=True)
    metrics = report_completion(rows)
    return {"mode": mode, "arm": "deepseek", "track": "completion",
            "provenance": prov, "metrics": metrics, "rows": rows}


def export_sonnet(mode: str, cases: list[dict]) -> tuple[dict, dict[str, str]]:
    """导出给 Claude 子 agent 当被测模型用的题面。

    ⚠️ 题面里**必须用不透明 id**。第一版直接把 `perc_snap_1`/`wake_cancel_1`
    这种可读 id 发了出去 —— 那等于把答案写在题号上,两个模式都跑出 60/60 满分,
    是假的。DeepSeek 臂只收 utterance 所以没被污染。
    返回 (题面, 不透明id → 真实id 的映射),映射留在本地评分用。
    """
    tools = build_catalog(mode)
    catalog = [
        {"name": t["function"]["name"], "description": t["function"]["description"]}
        for t in tools
    ]
    opaque = {f"c{idx:03d}": c["id"] for idx, c in enumerate(cases)}
    reverse = {v: k for k, v in opaque.items()}
    return (
        {
            "arm": "sonnet",
            "mode": mode,
            "instruction": (
                "You are choosing exactly one tool to call for each user utterance. "
                "Answer with ONLY the tool name, one per line, prefixed by the case "
                "id, like `case_id<TAB>tool_name`. Do not explain."
            ),
            "catalog": catalog,
            "cases": [
                {
                    "id": reverse[c["id"]],
                    "utterance": c["utterance"],
                    # runtime context 属于这一题，不再用一块 active screen
                    # 状态污染所有 case。
                    "runtime_context": {
                        "header": _RUNTIME_HEADER,
                        "data": runtime_data_for_case(c),
                    },
                }
                for c in cases
            ],
        },
        opaque,
    )


def _sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def provenance(mode: str, tools: list[dict], cases_doc: dict) -> dict:
    """产物必须能自证**实际发送的**内容。

    四条硬要求(codex 复审指出):
      1. 用**开始前 frozen 的 tools 对象**,不在结尾重建 —— 长跑期间文件可能变
      2. 带 UTC 时间戳,endpoint 记完整路径
      3. 只记**真正发送**的参数;未发送的写进 provider_defaults_used,不许伪装
      4. hash **实际发送的** runtime 消息(含 role/header/content),不是内部 dict
    """
    import subprocess, datetime
    def git(*a):
        try:
            return subprocess.run(["git", *a], cwd=str(_ROOT),
                                  capture_output=True, text=True).stdout.strip()
        except Exception:  # noqa: BLE001
            return ""
    return {
        "run_started_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "model": _DEEPSEEK_MODEL,
        "endpoint": f"{_DEEPSEEK_BASE}/chat/completions",
        "mode": mode,
        "request_params_sent": {"tool_choice": "auto", "max_tokens": 256},
        "provider_defaults_used": ["temperature", "seed"],
        "evaluator_sha256": _sha(pathlib.Path(__file__).read_text(encoding="utf-8")),
        # 从 **frozen 的 cases_doc** 算,不再回磁盘重读(与 catalog 同类竞态)
        "cases_sha256": _sha(json.dumps(cases_doc, ensure_ascii=False, sort_keys=True)),
        "cases_snapshot": cases_doc,
        "catalog_sha256": _sha(json.dumps(tools, ensure_ascii=False, sort_keys=True)),
        "catalog_tool_count": len(tools),
        "catalog_snapshot": tools,          # 嵌入实体,仅 hash 在 dirty 树里重建不出来
        # 每题 runtime message 不同；hash 完整 id→消息映射，防止把某题的 active
        # screen 状态重新扩散成全局 fixture 后产物仍伪装成同一题面。
        "runtime_message_sha256": _sha(json.dumps(
            {c["id"]: _runtime_message(c) for c in cases_doc["cases"]},
            ensure_ascii=False,
            sort_keys=True,
        )),
        "runtime_context_scope": "per_case",
        "backend_commit": git("rev-parse", "HEAD"),
        "backend_dirty": bool(git("status", "--porcelain", "backend")),
    }


def report(rows: list[dict]) -> dict:
    by_family: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_family[r["family"]].append(r)

    def agg(rs):
        valid = [r for r in rs if r["_d"]["valid"]]
        invalid_by_reason = dict(collections.Counter(
            str(r.get("outcome") or "unknown")
            for r in rs if not r["_d"]["valid"]
        ))
        if not valid:
            return {"n": 0, "all_rows_n": len(rs), "invalid": len(rs),
                    "invalid_by_reason": invalid_by_reason, "na": True}
        n = len(valid)
        strict_exact_n = sum(
            r["_d"]["klass"] == "singleton_exact" for r in valid
        )
        strict_ok_n = sum(
            r["_d"]["klass"] in ("singleton_exact", "singleton_also_ok")
            for r in valid
        )
        return {
            "n": len(valid), "all_rows_n": len(rs), "invalid": len(rs) - len(valid),
            "invalid_by_reason": invalid_by_reason,
            "strict_exact_n": strict_exact_n,
            "strict_ok_n": strict_ok_n,
            "strict_exact": strict_exact_n / n,
            "strict_ok": strict_ok_n / n,
            # 任何 invalid/剔除都会使 valid 分母变小；同时保留未剔除分母，
            # 禁止 278/348 通过静默丢 6 行变成 278/342。
            "strict_exact_all_rows": strict_exact_n / len(rs),
            "strict_ok_all_rows": strict_ok_n / len(rs),
            "target_recall": sum(r["_d"]["target_present"] for r in valid) / n,
            "acceptable_recall": sum(r["_d"]["acceptable_present"] for r in valid) / n,
            "overcall": sum(r["_d"]["overselected"] for r in valid) / n,
            # P(overcall | target_present):目标在里面时,有多大比例还多调了。
            # 分母为 0 时给 None(N/A),不伪造 0%。
            "overcall_given_target": (
                sum(r["_d"]["overselected"] for r in valid if r["_d"]["target_present"])
                / tp if (tp := sum(r["_d"]["target_present"] for r in valid)) else None
            ),
            "target_present_n": sum(r["_d"]["target_present"] for r in valid),
            "classes": dict(collections.Counter(r["_d"]["klass"] for r in rs)),
        }

    hdr = (f"{'family':12s} {'strict_ex':>9s} {'strict_ok':>9s} {'ok/all':>8s} "
           f"{'tgt_recall':>10s} {'acc_recall':>10s} {'overcall':>8s} "
           f"{'over|tgt':>9s} {'tgt_N':>6s} {'invalid':>7s}")
    print("\n" + hdr)
    print("-" * len(hdr))
    def line(label: str, a: dict) -> str:
        if a.get("na"):
            # valid N=0 → 显示 N/A。**绝不能用 `n or 1` 把它变成 0%**,
            # 那会把"没有有效观测"伪装成"全错"。
            return (f"{label:12s} {'N/A':>9s} {'N/A':>9s} {'N/A':>8s} {'N/A':>10s} "
                    f"{'N/A':>10s} {'N/A':>8s} {'N/A':>9s} {'N/A':>6s} "
                    f"{a['invalid']:7d}")
        ogt = a["overcall_given_target"]
        ogt_s = "N/A" if ogt is None else f"{100*ogt:.1f}%"
        return (f"{label:12s} {100*a['strict_exact']:8.1f}% {100*a['strict_ok']:8.1f}% "
                f"{100*a['strict_ok_all_rows']:7.1f}% "
                f"{100*a['target_recall']:9.1f}% {100*a['acceptable_recall']:9.1f}% "
                f"{100*a['overcall']:7.1f}% {ogt_s:>9s} "
                f"{a['target_present_n']:6d} {a['invalid']:7d}")

    for fam in sorted(by_family):
        print(line(fam, agg(by_family[fam])))
    print(line("(group)", agg(rows)))

    dist = collections.Counter(r["_d"]["klass"] for r in rows)
    print("\n互斥分类:", {k: dist[k] for k in CLASSES if dist[k]})
    invalid_reasons = collections.Counter(
        str(r.get("outcome") or "unknown")
        for r in rows if not r["_d"]["valid"]
    )
    if invalid_reasons:
        print("剔除/invalid 原因:", dict(invalid_reasons))
    print("⚠️ first-action 只测第一步,不是 production completion；"
          "strict_exact/strict_ok 只含单调用,多调用另立一轴。")
    print("指标口径:")
    for metric, definition in FIRST_ACTION_METRIC_DEFINITIONS.items():
        print(f"  {metric}: {definition}")
    print("⚠️ 每组 JSON 同时报 valid 分母与 all_rows 未剔除分母；"
          "invalid 不得靠缩分母静默抬分。")
    # 与 stdout **同源**:两边都用这一份 agg,避免两套算法漂移
    return {
        "by_family": {f: agg(rs) for f, rs in by_family.items()},
        "all": agg(rows),
        "classes": {k: dist[k] for k in CLASSES if dist[k]},
        "metric_definitions": FIRST_ACTION_METRIC_DEFINITIONS,
    }


_PROV_REQUIRED = ("run_started_at", "model", "endpoint", "mode",
                  "request_params_sent", "evaluator_sha256", "cases_sha256",
                  "catalog_sha256", "catalog_tool_count", "catalog_snapshot",
                  "cases_snapshot", "runtime_message_sha256")


def validate_provenance(block: dict) -> tuple[bool, list[str]]:
    """有 provenance 就必须**真的能自证**。

    两级绕过都堵死:
      1. 只查 key 存在 → 塞空 dict 就能骗过
      2. 只在类型恰好正确时才校验 hash → 塞 `catalog_snapshot={}` 就跳过校验
    所以 required 值必须做**类型 + 非空**校验,且 snapshot↔hash/count **始终执行**。
    """
    prov = block.get("provenance")
    if prov is None:
        return False, []                       # 真 legacy,另行标记
    errs = [f"missing:{k}" for k in _PROV_REQUIRED if k not in prov]

    def need_str(k):
        v = prov.get(k)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"not_nonempty_str:{k}")
    for k in ("run_started_at", "model", "endpoint", "mode",
              "evaluator_sha256", "cases_sha256", "catalog_sha256",
              "runtime_message_sha256"):
        need_str(k)
    if not isinstance(prov.get("request_params_sent"), dict):
        errs.append("not_dict:request_params_sent")
    n = prov.get("catalog_tool_count")
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        errs.append("not_nonneg_int:catalog_tool_count")

    snap = prov.get("catalog_snapshot")
    if not isinstance(snap, list):
        errs.append("not_list:catalog_snapshot")
    else:
        # 类型对了才谈 hash;但类型不对已经报错,不会被静默跳过
        if _sha(json.dumps(snap, ensure_ascii=False, sort_keys=True)) != prov.get("catalog_sha256"):
            errs.append("catalog_snapshot!=catalog_sha256")
        if snap and len(snap) != n:
            errs.append("catalog_snapshot!=catalog_tool_count")
    csnap = prov.get("cases_snapshot")
    if not isinstance(csnap, dict):
        errs.append("not_dict:cases_snapshot")
    elif _sha(json.dumps(csnap, ensure_ascii=False, sort_keys=True)) != prov.get("cases_sha256"):
        errs.append("cases_snapshot!=cases_sha256")

    if prov.get("mode") != block.get("mode"):
        errs.append("provenance.mode!=block.mode")
    if block.get("mode") not in ("full", "folded"):
        errs.append(f"bad_block_mode:{block.get('mode')}")
    if not str(block.get("arm") or "").strip():
        errs.append("empty_block_arm")
    if not str(prov.get("endpoint") or "").endswith("/chat/completions"):
        errs.append("endpoint_not_full_path")
    return True, errs


def report_grouped(rows: list[dict]) -> dict:
    """按 run identity 分组出报告。**绝不出 pooled ALL** ——
    full/folded、不同 catalog 的 ctrl/appr,绝对分数不可混(文件头已声明)。
    """
    groups: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in rows:
        groups[tuple(r.get("_group") or ("?", "?", None, None, None))].append(r)
    out = {}
    for g, rs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        arm, mode, model, csha, xsha = g
        label = (f"arm={arm} mode={mode} model={model or 'legacy'} "
                 f"catalog={(csha or 'legacy')[:8]} cases={(xsha or 'legacy')[:8]}")
        print(f"\n=== run group: {label}  (n={len(rs)}) ===")
        out[label] = report(rs)
    if len(groups) > 1:
        print(f"\n⚠️ 共 {len(groups)} 个 run group,**不做合并统计** —— "
              f"不同 mode/catalog 的绝对分数不可混。")
    return out


def rescore_artifacts(paths: list[str]) -> dict:
    """纯离线重判已有产物 —— **不调用 provider**。

    旧产物没有 provenance,一律标 legacy_unprovenanced=true;**绝不回填伪造出处**。
    同时校验 case 集合:缺失 / 多余 / 重复,都要显式报出来而不是静默跳过。
    """
    doc = load_cases()
    # 坏题库不能被拿来重判 —— rescore 分支之前提前 return 跳过了 coverage
    cov = check_coverage(doc)
    if cov:
        return {"files": [], "rows": [], "cases_problems": cov}
    cases = {c["id"]: c for c in doc["cases"]}
    out = {"files": [], "rows": [], "cases_problems": []}
    for path in paths:
        blob = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        # `--mode both` 的产物含 full + folded 两个 block。之前只取 blob[0],
        # **第二个被静默丢弃**。逐 block 处理,并以 (file, block_index, arm, mode) 记来源。
        blocks = blob if isinstance(blob, list) else [blob]
        for bi, block in enumerate(blocks):
            raw = block.get("rows") or []
            ids = [r.get("id") for r in raw]
            dup = sorted({i for i in ids if ids.count(i) > 1})
            missing = sorted(set(cases) - set(ids))
            extra = sorted(set(ids) - set(cases))
            has_prov, prov_errs = validate_provenance(block)
            prov = block.get("provenance") or {}
            group = (block.get("arm"), block.get("mode"),
                     prov.get("model"), prov.get("catalog_sha256"),
                     prov.get("cases_sha256"))
            out["files"].append({
                "path": path, "block_index": bi,
                "group": list(group),
                "legacy_unprovenanced": not has_prov,
                "provenance_valid": has_prov and not prov_errs,
                "provenance_errors": prov_errs,
                "sha256": _sha(pathlib.Path(path).read_text(encoding="utf-8")),
                "arm": block.get("arm"), "mode": block.get("mode"), "rows": len(raw),
                "duplicate_ids": dup, "missing_cases": missing, "unknown_cases": extra,
            })
            for r in raw:
                case = cases.get(r.get("id"))
                if case is None:
                    continue
            # ⚠️ 金标在后:`{**r, **case}`。之前写成 `{**case, **r}`,
            # **产物里的旧 expect/family/also_ok 会反向覆盖当前金标** ——
            # 那等于拿旧答案去判新题。
                stale = {k: r.get(k) for k in ("expect", "also_ok", "family")
                         if k in r and r.get(k) != case.get(k)}
                out["rows"].append({**r, **case, "_d": classify_row(r, case),
                                    "_src": path, "_block": bi, "_group": group,
                                    "_stale_labels": stale or None})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["deepseek", "sonnet-export"], default="deepseek")
    ap.add_argument("--mode", choices=["full", "folded", "both"], default="both")
    ap.add_argument("--track", choices=["first-action", "completion", "both"],
                    default="first-action",
                    help="first-action 不是完成率；completion 走确定性 ToolExchange")
    ap.add_argument("--check-cases", action="store_true",
                    help="只检查真实题库健康与目录覆盖，不调用 provider")
    ap.add_argument("--selftest", action="store_true",
                    help="真实题库结构健康 + 假工具打分链路；不证明金标语义正确")
    ap.add_argument("--out", default="")
    ap.add_argument("--allow-invalid-artifacts", action="store_true",
                    help="结构有问题时仍输出(标 invalid),默认 fail closed")
    ap.add_argument("--rescore", nargs="+", default=None,
                    help="纯离线重判已有产物,不调用 provider")
    args = ap.parse_args()

    if args.rescore:
        res = rescore_artifacts(args.rescore)
        if res.get("cases_problems"):
            for c in res["cases_problems"]:
                print(f"❌ 当前题库有问题,拒绝重判: {c}", file=sys.stderr)
            return 2
        bad = []
        for f in res["files"]:
            flags = [k for k in ("duplicate_ids", "missing_cases", "unknown_cases")
                     if f[k]]
            # provenance 存在但损坏 = 坏产物,和结构 flag 同等对待
            if f["provenance_errors"]:
                flags.append(f"provenance_errors={f['provenance_errors']}")
            if flags:
                bad.append((f["path"], f["block_index"], flags))
            print(f"  {f['path']}#{f['block_index']}  rows={f['rows']}  "
                  f"arm={f['arm']} mode={f['mode']}  "
                  f"legacy={f['legacy_unprovenanced']} "
                  f"prov_valid={f['provenance_valid']}"
                  + (f"  ⚠️ {{{', '.join(flags)}}}" if flags else ""))
        stale = [r["id"] for r in res["rows"] if r.get("_stale_labels")]
        if stale:
            print(f"  ⚠️ {len(stale)} 行的产物标签与当前金标不符(已按金标判):"
                  f" {sorted(set(stale))[:5]}…")
        if bad and not args.allow_invalid_artifacts:
            # fail closed:结构有问题就**不发布正式 summary**,非零退出。
            print("\n❌ 产物结构有问题,拒绝发布正式 summary。"
                  "确需勘查请加 --allow-invalid-artifacts(输出仍标 invalid)。",
                  file=sys.stderr)
            return 2
        summary = report_grouped(res["rows"])
        if args.out:
            pathlib.Path(args.out).write_text(json.dumps({
                "files": res["files"], "rows_scored": len(res["rows"]),
                "artifacts_invalid": bool(bad),
                "allow_invalid_artifacts": bool(args.allow_invalid_artifacts),
                "stale_label_rows": sorted(set(stale)),
                "metrics_by_group": summary,   # 与 stdout 同源;无 pooled ALL
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0

    doc = load_cases()
    problems = check_coverage(doc)
    if problems:
        for p in problems:
            print(f"题库健康/覆盖问题: {p}", file=sys.stderr)
        return 2
    if args.check_cases:
        print(f"题库健康检查通过: first-action={len(doc['cases'])} "
              f"native-image-contract={len(doc.get('native_image_cases') or [])} "
              f"completion={len(doc.get('completion_cases') or [])}")
        print("⚠️ 这里只证明结构/fixture/覆盖契约，不证明金标语义一定正确。")
        return 0

    keys = _load_keys()
    key = os.environ.get("E2E_KEY_DEEPSEEK") or keys.get("E2E_KEY_DEEPSEEK", "")

    if args.selftest:
        if not key:
            print("selftest 需要 E2E_KEY_DEEPSEEK", file=sys.stderr)
            return 2
        print("量具自检(极清楚的假工具集,应接近满分):")
        tools = _selftest_catalog()
        ok = 0
        for expect, utt in _SELFTEST_CASES:
            res = probe_deepseek(key, tools, {"id": expect, "utterance": utt})
            good = res.get("picked") == expect
            ok += bool(good)
            print(f"  {'✓' if good else '✗'} {expect:18s} got={res.get('picked') or res.get('outcome')}")
        print(f"自检 {ok}/{len(_SELFTEST_CASES)}")
        if ok < len(_SELFTEST_CASES):
            print("⚠️ 自检没满分 —— 后面的结论不可信,先查评测器/模型,不要怪描述",
                  file=sys.stderr)
            return 1
        print("⚠️ selftest 只证明真实题库结构健康与假工具打分链路；"
              "不证明真实金标语义正确，也不覆盖 production completion。")
        return 0

    cases = doc["cases"]
    completion_cases = doc.get("completion_cases") or []
    modes = ["full", "folded"] if args.mode == "both" else [args.mode]

    if args.arm == "sonnet-export":
        if args.track != "first-action":
            print("sonnet-export 当前只导出 first-action；completion 请用 deepseek 轨。",
                  file=sys.stderr)
            return 2
        payloads = []
        keymap: dict[str, str] = {}
        for m in modes:
            payload, opaque = export_sonnet(m, cases)
            payloads.append(payload)
            keymap = opaque
        if args.out:
            pathlib.Path(args.out).write_text(
                json.dumps(payloads, ensure_ascii=False, indent=2), encoding="utf-8")
            pathlib.Path(args.out + ".keymap").write_text(
                json.dumps(keymap, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"题面 → {args.out}\n答案映射(不要给被测模型)→ {args.out}.keymap")
        else:
            print(json.dumps(payloads, ensure_ascii=False, indent=2))
        return 0

    if not key:
        print("缺 E2E_KEY_DEEPSEEK", file=sys.stderr)
        return 2

    results = []
    for m in modes:
        if args.track in {"first-action", "both"}:
            print(f"\n=== deepseek / {m} / first-action ({len(cases)} 条) ===")
            res = run_deepseek(key, m, cases, doc)
            res["track"] = "first-action"
            res["metrics"] = report(res["rows"])
            results.append(res)
        if args.track in {"completion", "both"}:
            print(f"\n=== deepseek / {m} / completion "
                  f"({len(completion_cases)} 条) ===")
            results.append(run_deepseek_completion(
                key, m, completion_cases, doc
            ))

    if args.out:
        pathlib.Path(args.out).write_text(
            json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
