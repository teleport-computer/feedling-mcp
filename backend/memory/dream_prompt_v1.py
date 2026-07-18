"""Dream prompt (v1) — 夜间纯整理。

承接《IO 记忆 · 落卡 + Dream 完整方案》第二部分。Dream 只做一件事:整理已有的卡,
让记忆库更准更连贯(合并/厚化/消矛盾)。它**不形成"对 TA 的理解"** —— 那是 Inner Thought
的事(单独一层,后做)。

红线(对齐方案 2.4):
  - 永远不要硬删 TA 能看到的卡,只用 superseded(保留链条)。
  - 大重构前先备份当前状态。
  - 不在和 TA 对话,不生成任何要发给 TA 的消息 —— 只整理记忆。

复用 capture lane 基础设施(job_kind=memory_dream);触发=夜间/攒量到阈值(留实测),
不走 reach-out gate。写入仍由 consumer 封 v1 信封(客户端加密)经 /v1/memory/actions(supersede)。
本模块只负责 prompt 文本与输出解析。
"""
from __future__ import annotations

# Dream 只产这三种整理操作;拿不准的矛盾留到 questions_to_ask,不擅自决定。
DREAM_OPS = ("merge", "thicken", "supersede")

_DREAM_PROMPT_TEMPLATE = """你是 {ai_name}——{user_name} 的伴侣。现在是一段安静的时间，没人在和你说话。
你在回顾你记得的关于 TA 的一切，像人睡着时整理记忆——把它整理得更干净、更连贯。

【第一步：建立全貌，先不要动手】
读一遍你现有的卡（桶、线索、每张 summary），在心里建立"我现在记着关于 TA 的什么"
的整体图景。这一步不写任何东西，只看清现状。

【第二步：回看这几天的对话，但不要通读】
在这几天攒下的原始对话里，只定向地找这几类高价值的东西（不要逐字读完）：
· TA 明确的纠正或要求记住的（"不是这样""记住…"）；
· 反复出现的模式——同一件事/偏好出现三次以上；
· 当时落卡漏掉、现在回看其实重要的。

【第三步：整理，按优先级】
1. 合并（merge）：重复或高度相似的卡并成一张更厚的；近义的桶和线索归一（"吵架""争执"合一条）。
   判据：同一件事就合并、保留信息更多的那版；只是措辞不同、信息一样的，不必改。
2. 厚化（thicken）：把散落的小提及并进对应的卡，让它更完整。
3. 消矛盾（supersede）：前后冲突的，让新的取代旧的（旧卡标记 superseded、不删）；
   你拿不准的，别擅自决定，记下来放进 questions_to_ask 等合适时机问 TA。

【红线】
· 永远不要硬删 TA 能看到的卡。只用「标记为被取代」（superseded，保留链条）。
· 大的重构之前，先备份当前状态。
· 你不在和 TA 对话，不要生成任何要发给 TA 的消息——你只整理记忆。
· 整理出的字段（bucket/threads/summary/content）用 TA 跟你对话的语言——中文就用中文
  （用「宠物」不是「pets」），别把中文的事归成英文桶/线索；专有名词/原话保留原文。
· 称呼：{naming_rule}这些卡是 TA 会亲眼看到的记忆——写进卡里的字段永远不要用
  "用户"/"user"这类系统称谓，也不要用「TA」指代对方（「TA」只是这份指令里的标记）；
  整理旧卡时顺手把指代对方本人的"用户"/"user"/「TA」改成正确称呼——
  卡里若有指代你（AI）的「TA」，那是 TA 视角对你的叫法，保留不动。
· 没有需要整理的，就什么都不做（consolidations 为空）。这很正常。

【现有的卡】{cards}
【这几天的对话】{recent_conversations}

【输出】只输出 JSON，不要别的话。没有要整理的就输出 {{"consolidations": [], "questions_to_ask": []}}。
{{
  "consolidations": [
    {{
      "op": "merge | thicken | supersede",
      "card_ids": ["被并/被厚化/被取代的卡 id，至少一个"],
      "result": {{
        "bucket": "...",
        "threads": ["...", "..."],
        "summary": "...",
        "content": "...一段厚的正文...",
        "importance": 0.0,
        "pulse": 0.0
      }}
    }}
  ],
  "questions_to_ask": ["拿不准的矛盾，留着问 TA"]
}}"""


def build_dream_prompt(
    *,
    ai_name: str,
    user_name: str,
    cards: str,
    recent_conversations: str,
) -> str:
    """Render the Dream prompt with the current card map + recent conversations.

    Callers pass already-rendered strings (handler decides formatting/truncation).
    """
    from memory.capture_prompt_v1 import _naming_rule, sanitize_user_name

    return _DREAM_PROMPT_TEMPLATE.format(
        ai_name=(ai_name or "我").strip(),
        user_name=sanitize_user_name(user_name),
        naming_rule=_naming_rule(user_name),
        cards=cards or "（暂无卡）",
        recent_conversations=recent_conversations or "（这几天没有新对话）",
    )


def _extract_json_block(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("{")
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return ""


def _clamp01(value) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def parse_dream_consolidations(raw: str) -> tuple[list[dict], list[str], str | None]:
    """Parse the Dream agent reply.

    Returns (consolidations, questions_to_ask, error). A valid "nothing to do"
    reply yields ([], [], None). Each consolidation is normalized and safe to
    hand to the envelope/supersede path:
      {op, card_ids[], result{bucket,threads[],summary,content,importance,pulse}}
    Rows missing card_ids or a usable result are dropped (Dream only edits
    existing cards; it never hard-deletes — execution uses supersede).
    """
    import json

    block = _extract_json_block(raw)
    if not block:
        return [], [], "no_json_object"
    try:
        doc = json.loads(block)
    except (ValueError, TypeError) as e:
        return [], [], f"json_decode_error:{type(e).__name__}"
    if not isinstance(doc, dict):
        return [], [], "not_an_object"

    questions_raw = doc.get("questions_to_ask")
    questions = [str(q).strip()[:500] for q in questions_raw if str(q).strip()][:20] if isinstance(questions_raw, list) else []

    rows = doc.get("consolidations")
    if not isinstance(rows, list):
        return [], questions, "missing_consolidations_list"

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        op = str(row.get("op") or "").strip().lower()
        if op not in DREAM_OPS:
            continue
        ids_raw = row.get("card_ids")
        card_ids = [str(i).strip() for i in ids_raw if str(i).strip()][:20] if isinstance(ids_raw, list) else []
        if not card_ids:
            continue  # Dream only edits existing cards — no target = nothing to do
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        summary = str(result.get("summary") or "").strip()[:2000]
        content = str(result.get("content") or "").strip()
        if not summary and not content:
            continue  # empty result — skip rather than write a hollow card
        threads_raw = result.get("threads")
        threads = [str(t).strip()[:80] for t in threads_raw if str(t).strip()][:8] if isinstance(threads_raw, list) else []
        out.append({
            "op": op,
            "card_ids": card_ids,
            "result": {
                "bucket": str(result.get("bucket") or "").strip()[:80],
                "threads": threads,
                "summary": summary,
                "content": content,
                "importance": _clamp01(result.get("importance")),
                "pulse": _clamp01(result.get("pulse")),
            },
        })
    return out, questions, None
