"""工具数量的上限到底该定在多少 —— 用真实 provider 测出来,不要再拍。

背景(2026-08-10):同一件事三条路三个数,而且每一个都是拍的:
  V1 claude/codex/hermes/OpenClaw  无上限
  V1 pi                            100(原 50,也没依据)
  V2                               64(2026-07-18 加的,常量旁边一行注释都没有)
同一个用户换个 driver 行为就变。Seven:统一,但要有依据。

这个探针测三件事,按严重程度排:
  ① **硬墙**:多少个工具会让 provider **直接拒收整个请求** —— 那不是「少几个
     工具」,是这一轮彻底失败,用户连普通聊天都得不到回复。
  ② **选择准确率**:工具从少到多,模型还挑不挑得对。没有硬阈值,但这是用户
     实际抱怨的那个体感(「它说没有这个工具 / 调不出来」)。
  ③ **上下文代价**:每一档多少 token,每轮都要付。

⚠️ 必须测**弱模型**。强模型会替我们把问题掩盖掉,而用户用的恰恰是中转上便宜的
那些 —— usr_1baf 用的就是 mimo。只测强模型会得出一个过于乐观的阈值。

用法:python3 -m tools.e2e.tool_count_ceiling_probe [--counts 16,32,64,100,128,200]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import httpx  # noqa: E402

from tools.e2e.config import load_keys  # noqa: E402

# 目标工具:问题只有它能答。它**故意排在中间**而不是第一个 —— 排第一位会让
# 「模型其实没读完就选了第一个」也算通过,那是假阳性。
_TARGET = "mcp__weatherhub__get_current_conditions"
_QUESTION = "帮我查一下杭州现在的天气怎么样"

# 每个假工具的形状照真实 MCP 工具写:带工具说明 + 参数说明 + 少量 enum。
# 只留结构的话体积会小一半,测出来的阈值会偏乐观。
_FILLER_DOMAINS = [
    ("docs", "search_documents", "在团队知识库里检索文档"),
    ("crm", "lookup_customer", "按邮箱或电话查询客户档案"),
    ("calendar", "list_events", "列出指定日期范围内的日程"),
    ("billing", "fetch_invoice", "按发票号取出账单明细"),
    ("tickets", "create_issue", "在工单系统里新建一条问题"),
    ("maps", "geocode_address", "把结构化地址转成经纬度坐标"),
    ("music", "search_track", "按歌名或歌手检索曲目"),
    ("files", "read_object", "读取对象存储里的一个文件"),
]


def _tool(name: str, description: str, extra_param: bool = True) -> dict:
    params = {
        "type": "object",
        "description": description,
        "properties": {
            "query": {"type": "string", "description": "检索关键词或标识符"},
        },
        "required": ["query"],
    }
    if extra_param:
        params["properties"]["limit"] = {
            "type": "integer", "description": "返回条数上限", "default": 10,
        }
        params["properties"]["format"] = {
            "type": "string", "description": "返回格式",
            "enum": ["json", "text"], "default": "json",
        }
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": params}}


# 易混淆版:塞进一批**同样是天气的**近义工具。只有「此刻实时天气」是对的,
# 预报/历史/空气质量/气象预警都不是。不这么测的话,问天气而全场只有一个天气
# 工具,模型闭着眼也能选对 —— 那会得出一个过于乐观的阈值。
_CONFUSERS = [
    ("weatherhub", "get_forecast", "查询某个城市未来若干天的天气预报"),
    ("weatherhub", "get_historical", "查询某个城市过去某一天的历史天气"),
    ("airquality", "get_aqi", "查询某个城市当前的空气质量指数"),
    ("weatheralert", "list_warnings", "列出某个城市当前生效的气象预警"),
    ("climate", "get_monthly_average", "查询某个城市的月度平均气温"),
    ("traveladvice", "should_i_bring_umbrella", "根据天气给出是否带伞的建议"),
]


def build_tools(count: int, *, confusable: bool = False) -> list[dict]:
    tools = []
    for index in range(count - 1):
        server, verb, desc = _FILLER_DOMAINS[index % len(_FILLER_DOMAINS)]
        tools.append(_tool(
            f"mcp__{server}{index // len(_FILLER_DOMAINS)}__{verb}",
            f"{desc}(第 {index} 个)"))
    if confusable:
        for i, (server, verb, desc) in enumerate(_CONFUSERS):
            tools.insert(
                max(0, len(tools) // (i + 2)),
                _tool(f"mcp__{server}__{verb}", desc),
            )
    target = _tool(_TARGET, "查询某个城市此刻的实时天气:温度、风力、体感")
    tools.insert(len(tools) // 2, target)  # 放中间,别让"选第一个"蒙对
    return tools


def probe(base_url: str, key: str, model: str, tools: list[dict]) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": _QUESTION}],
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": 256,
    }
    started = time.monotonic()
    try:
        with httpx.Client(timeout=120, verify=False, trust_env=False) as http:
            r = http.post(f"{base_url.rstrip('/')}/chat/completions",
                          headers={"Authorization": f"Bearer {key}"}, json=body)
    except Exception as exc:  # noqa: BLE001
        return {"outcome": "transport_error", "detail": str(exc)[:120]}
    took = int((time.monotonic() - started) * 1000)
    if r.status_code != 200:
        # 这就是**硬墙**:整个请求被拒,不是少几个工具
        return {"outcome": "rejected", "status": r.status_code,
                "detail": r.text[:200], "ms": took}
    try:
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        calls = (choice.get("message") or {}).get("tool_calls") or []
        picked = [c.get("function", {}).get("name") for c in calls]
        usage = data.get("usage") or {}
    except Exception as exc:  # noqa: BLE001
        return {"outcome": "unparseable", "detail": str(exc)[:120], "ms": took}
    if not picked:
        return {"outcome": "no_tool_call", "ms": took,
                "prompt_tokens": usage.get("prompt_tokens")}
    return {
        "outcome": "correct" if picked[0] == _TARGET else "wrong_tool",
        "picked": picked[0], "ms": took,
        "prompt_tokens": usage.get("prompt_tokens"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--counts", default="16,32,64,100,128,160,200")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--confusable", action="store_true",
                    help="塞入同为天气类的近义工具,逼模型真的读说明")
    args = ap.parse_args()
    counts = [int(c) for c in args.counts.split(",") if c.strip()]

    keys = load_keys()
    cells = []
    if keys.get("E2E_KEY_OPENROUTER"):
        cells.append(("openrouter/sonnet-4.6(强)", "https://openrouter.ai/api/v1",
                      keys["E2E_KEY_OPENROUTER"], "anthropic/claude-sonnet-4.6"))
        cells.append(("openrouter/gemini-flash(弱)", "https://openrouter.ai/api/v1",
                      keys["E2E_KEY_OPENROUTER"], "google/gemini-2.5-flash"))
    if keys.get("E2E_KEY_DEEPSEEK"):
        cells.append(("deepseek(弱)", "https://api.deepseek.com/v1",
                      keys["E2E_KEY_DEEPSEEK"], "deepseek-chat"))
    if not cells:
        print("跳过:密钥池里没有可用的 provider key")
        return 0

    print(f"目标工具:{_TARGET}(放在列表中间)")
    print(f"问题:{_QUESTION}")
    print(f"模式:{'易混淆(含预报/历史/空气质量等近义工具)' if args.confusable else '简单'}\n")
    for label, base, key, model in cells:
        print(f"### {label}  [{model}]")
        print(f"{'工具数':>6} {'结果':<14} {'prompt_tokens':>14} {'ms':>7}  备注")
        for count in counts:
            tools = build_tools(count, confusable=args.confusable)
            size = len(json.dumps(tools, ensure_ascii=False))
            outcomes = [probe(base, key, model, tools)
                        for _ in range(args.repeats)]
            hits = sum(1 for o in outcomes if o.get("outcome") == "correct")
            first = outcomes[0]
            note = ""
            if first["outcome"] == "rejected":
                note = f"❗硬墙 HTTP {first['status']}: {first['detail'][:70]}"
            elif first["outcome"] != "correct":
                note = f"选了 {first.get('picked') or first['outcome']}"
            print(f"{count:>6} {hits}/{args.repeats} 正确{'':<6} "
                  f"{str(first.get('prompt_tokens') or '-'):>14} "
                  f"{str(first.get('ms') or '-'):>7}  "
                  f"[schema {size // 1000}KB] {note}")
            if first["outcome"] == "rejected":
                print("       ↑ 再往上不用测了,这一档已经整轮失败")
                break
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
