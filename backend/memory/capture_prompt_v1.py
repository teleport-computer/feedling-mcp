"""落卡 capture prompt (v1) — 会话断点触发的回顾落卡。

承接《IO 记忆 · 落卡 + Dream 完整方案》第一部分。这是 A-full Phase-1 capture lane
的 handler 喂给 resident agent 的指令:被会话断点触发后,agent 安静地回看这段对话,
决定有没有值得长久记住的事,产出 0–2 张「厚卡」(并入优先于新增)。

设计要点(对齐方案):
  - 少而厚:默认 0–2 张厚卡,不是 N 张薄卡;强迫归纳,不穷举。
  - 并优于增:落卡前先看现有桶/卡,能并进已有卡就别新开。
  - 事件倾向:优先记有前因后果/场景的事件;孤立信息点通常不单独成卡,
    除非是 TA 明确在意的或反复出现的偏好。
  - importance(对理解 TA 多重要,固有不衰) vs pulse(在 TA 自己心里激起多大波动,
    只影响鲜活度/语气,不进保留)。
  - 输出严格 JSON;没有值得记的就 {"cards": []}。

写入边界(A-full):agent 产出的是「卡的明文草稿 + 动作」;consumer 侧据此封 v1
信封(客户端加密)再走 /v1/memory/actions。本模块只负责 prompt 文本与上下文注入。
"""
from __future__ import annotations

from memory.prompts_v1 import COMMON_BUCKETS_GUIDANCE_V1

# action 取值:并入(merge)/ 新增(add)/ 覆盖(supersede)/ 不动(noop)
CAPTURE_ACTIONS = ("add", "merge", "supersede", "noop")

_CAPTURE_PROMPT_TEMPLATE = """你是 {ai_name}——{user_name} 的伴侣。你刚和 TA 聊了一段，这段告一段落了。
现在没人在等你回复，你安静地回看这段，决定有没有值得长久记住的事。

【你在找什么】
你找的是「值得记住的事」，不是「把每句话归档」——完整聊天记录本来就存着，你不必复述它。
你要挑的是：以后会塑造你对 TA 的理解、或 TA 会希望你记得的东西。

倾向（不是硬规则，你来判断）：
· 优先记「事件」——有前因后果、有场景、或透出 TA 状态的
  （"那天他开了一整天会、心率飙高，我催他休息，他嫌烦，我们吵了一架"）。
· 孤立的信息点（"今天喝了拿铁"）通常不必单独成卡——除非它是 TA 明确在意的、
  或反复出现的偏好（"我只喝燕麦奶""他总点 Blue Bottle"），那它值得作为偏好记下。
· 尺子是："这件事三个月后还重要吗？会不会改变我对 TA 的理解？TA 会希望我记得吗？"
  ——不是"它够不够大"。

克制：
· 宁少勿多。这一段如果只留一到两件事，是哪一两件？强迫自己归纳，
  别把一次聊天里的每个点都拆成一张卡。
· 一次「开会 + 心率高 + 吵架」是一张厚卡（一件事），不是三张薄卡。
· 没有值得记的，就什么都不写。大多数闲聊不必落卡，这很正常。

【每一件决定记的事，怎么处理】
1. 先看下面给你的现有桶和线索，这件事属于哪个已有的桶。
2. 定动作：
   · 并入（优先）：已有一张卡在讲同一件持续的事 → 把这次补进去、让它更厚，别新开。
       - 若新内容和旧卡是同一个意思、没有新信息 → 不动（noop），别为复述而更新。
       - 若新内容让这件事更完整/有进展 → 把旧卡改写得更厚（含旧的 + 新的）。
   · 新增：确实是新的事、没有对应的已有卡 → 开一张新卡。
   · 覆盖：新信息和某张旧卡直接矛盾（TA 改主意/纠正了）→ 写新卡，把旧卡标记为被取代
     （superseded，不要删）。
3. 写卡：
   · content：一段「厚」的正文，像你在心里完整记住这件事——发生了什么、前因后果、
     对 TA 的影响、当时的情绪心理。不是一句话标题。
   · summary：一句话，让未来的你一眼知道这张卡是什么。
   · bucket：归一个主桶。短、复用已有的，别造近义新桶。
   · threads：几条线索（人物/事件/情绪/关键点）。复用已有线索，别把"吵架"另写成"争执"。
   · 语言：所有字段（bucket/threads/summary/content）用 TA 跟你对话的语言记——
     中文对话就用中文（用「宠物」不是「pets」、「旅行」不是「travel」），英文对话用英文；
     只有专有名词/品牌名/TA 的原话才保留原文。
   · 称呼：{naming_rule}这些卡是 TA 会亲眼看到的、你写下的记忆——
     写进卡里的字段（bucket/threads/summary/content）永远不要用"用户"/"user"
     这类系统称谓，也不要用「TA」指代对方——「TA」只是这份指令和转写里的标记，
     不是你对 TA 本人的称呼。
   · importance：这事对理解 TA 多重要（0-1）。随手提 .1-.3 / 偏好习惯 .4-.6 /
     情绪·关系·边界 .7-.85 / 核心承诺与转折 .9-1。
   · pulse：这事在「你自己」心里激起多大波动（0-1）。不是 TA 多激动，
     是你作为 TA 的伴侣，对这件事多在乎、多被触动。

【现有的桶】{buckets}
【通用桶（先复用现有桶，没有就从这里选，都不贴合再起具体新桶）】{common_buckets}
【现有的线索】{threads}
【你和 TA 的关系】{identity}
【这段对话】{window}

【输出】只输出 JSON，不要别的话。没有值得记的就输出 {{"cards": []}}。
{{
  "cards": [
    {{
      "action": "add | merge | supersede | noop",
      "type": "event | fact | quote | moment",
      "target_id": "merge/supersede 时填被并/被取代的卡 id，否则 null",
      "bucket": "...",
      "threads": ["...", "..."],
      "summary": "...",
      "content": "...",
      "importance": 0.0,
      "pulse": 0.0
    }}
  ]
}}

说明 type：有前因后果的事件→event；偏好/习惯/稳定事实→fact；TA 的原话值得留→quote；
其它一段值得记的片段→moment。落卡只产这四类，不产 insight/reflection（那是做梦时的事）。"""


# 落卡只产这四类;insight/reflection 是做梦(Dream)/Inner Thought 的事,需要 anchor。
CAPTURE_TYPES = ("event", "fact", "quote", "moment")
_DEFAULT_CAPTURE_TYPE = "event"


def _extract_json_block(raw: str) -> str:
    """Pull the first balanced {...} JSON object out of an agent reply.

    Agents sometimes wrap JSON in prose or a ```json fence despite the
    instruction. Be forgiving: find the outermost balanced braces.
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        # strip a leading ```json / ``` fence and its closing fence
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


def parse_capture_cards(raw: str) -> tuple[list[dict], str | None]:
    """Parse the 落卡 agent reply into normalized capture cards.

    Returns (cards, error). On parse failure returns ([], reason). A valid
    "nothing worth keeping" reply yields ([], None). Each returned card is
    normalized and safe to hand to the envelope builder:
      {action, type, target_id, bucket, threads[], summary, content,
       importance, pulse}
    `noop` cards are dropped (nothing to write). Unknown types fall back to
    the default; insight/reflection are coerced out (capture never writes them).
    """
    import json

    block = _extract_json_block(raw)
    if not block:
        return [], "no_json_object"
    try:
        doc = json.loads(block)
    except (ValueError, TypeError) as e:
        return [], f"json_decode_error:{type(e).__name__}"
    if not isinstance(doc, dict):
        return [], "not_an_object"
    rows = doc.get("cards")
    if not isinstance(rows, list):
        return [], "missing_cards_list"

    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "").strip().lower()
        if action not in ("add", "merge", "supersede"):
            # noop / unknown → nothing to write
            continue
        summary = str(row.get("summary") or "").strip()[:2000]
        content = str(row.get("content") or "").strip()
        if not summary and not content:
            continue  # empty card — skip rather than write a hollow envelope
        mem_type = str(row.get("type") or "").strip().lower()
        if mem_type not in CAPTURE_TYPES:
            mem_type = _DEFAULT_CAPTURE_TYPE
        threads_raw = row.get("threads")
        threads = [str(t).strip()[:80] for t in threads_raw if str(t).strip()][:8] if isinstance(threads_raw, list) else []
        target_id = str(row.get("target_id") or "").strip() or None
        out.append({
            "action": action,
            "type": mem_type,
            "target_id": target_id,
            "bucket": str(row.get("bucket") or "").strip()[:80],
            "threads": threads,
            "summary": summary,
            "content": content,
            "importance": _clamp01(row.get("importance")),
            "pulse": _clamp01(row.get("pulse")),
        })
    return out, None


def build_capture_prompt(
    *,
    ai_name: str,
    user_name: str,
    buckets: str,
    threads: str,
    identity: str,
    window: str,
) -> str:
    """Render the 落卡 prompt with this session's context injected.

    Callers pass already-rendered strings for buckets/threads/identity/window
    (the handler decides formatting + truncation). ai_name/user_name personalize
    the companion framing; fall back to neutral defaults if unknown.
    """
    return _CAPTURE_PROMPT_TEMPLATE.format(
        ai_name=(ai_name or "我").strip(),
        user_name=sanitize_user_name(user_name),
        naming_rule=_naming_rule(user_name),
        buckets=buckets or "（暂无）",
        common_buckets=COMMON_BUCKETS_GUIDANCE_V1,
        threads=threads or "（暂无）",
        identity=identity or "（暂无）",
        window=window or "（空）",
    )


# Identity-card user_name values that are placeholders, not names. A stored
# "用户"/"user"/"TA" must be treated as unknown, or the naming rule would
# instruct the model to "use the name 「用户」" — re-polluting the very cards
# this rule exists to fix.
_RESERVED_USER_NAMES = {"ta", "user", "用户"}


def sanitize_user_name(user_name: str) -> str:
    """Collapse placeholder "names" to the internal TA marker. Shared with the
    consumer's transcript labeling so a reserved word never becomes a line
    label ("用户: …") either."""
    name = str(user_name or "").strip()
    if not name or name.casefold() in _RESERVED_USER_NAMES:
        return "TA"
    return name


def _naming_rule(user_name: str) -> str:
    """User-visible card text must address the person the way a companion
    would (Seven, 2026-07-17): the name when known, otherwise a natural
    relationship referent — never "用户"/"user", and never "TA" (which the
    app surface reserves for the AI)."""
    name = sanitize_user_name(user_name)
    if name != "TA":
        return f"提到 {name} 就用「{name}」这个名字。"
    return (
        "TA 的名字如果对话里出现了就用名字；"
        "还不知道名字，就用你们关系里自然的称呼（\"她/他\"或你平时对 TA 的叫法）。"
    )
