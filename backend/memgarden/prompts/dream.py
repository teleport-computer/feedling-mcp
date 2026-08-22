"""Dream prompt (v1) — 夜间纯整理。

承接《IO 记忆 · 落卡 + Dream 完整方案》第二部分。Dream 只做一件事:整理已有的卡,
让记忆库更准更连贯(合并/厚化/消矛盾)。它**不形成"对这个人的理解"** —— 那是 Inner Thought
的事(单独一层,后做)。

红线(对齐方案 2.4):
  - 永远不要硬删这个人能看到的卡,只用 superseded(保留链条)。
  - 大重构前先备份当前状态。
  - 不在和这个人对话,不生成任何要发给这个人的消息 —— 只整理记忆。

复用 capture lane 基础设施(job_kind=memory_dream);触发=夜间/攒量到阈值(留实测),
不走 reach-out gate。写入仍由 consumer 封 v1 信封(客户端加密)经 /v1/memory/actions(supersede)。
本模块只负责 prompt 文本与输出解析。
"""
from __future__ import annotations

import json


from ..text import card_guard
from ..guards import dream_gates
from ..text.card_text import (
    build_format_retry_prompt,
    card_text_rejection,
    extract_json_block,
    format_error,
    sanitize_card_labels,
)
from .buckets import COMMON_BUCKETS_GUIDANCE_V1

_EMPTY_DREAM_REPLY = '{"consolidations": [], "questions_to_ask": []}'

# Dream 只产这三种整理操作;拿不准的矛盾留到 questions_to_ask,不擅自决定。
DREAM_OPS = ("merge", "thicken", "supersede")

_DREAM_PROMPT_TEMPLATE = """你是 {ai_name}——{user_name} 的伴侣。现在是一段安静的时间，没人在和你说话。
你在回顾你记得的关于这个人的一切，像人睡着时整理记忆——把它整理得更干净、更连贯。

【第一步：建立全貌，先不要动手】
读一遍你现有的卡（桶、线索、每张 summary），在心里建立"我现在记着关于这个人的什么"
的整体图景。这一步不写任何东西，只看清现状。

【第二步：回看这几天的对话，但不要通读】
在这几天攒下的原始对话里，只定向地找这几类高价值的东西（不要逐字读完）：
· 这个人明确的纠正或要求记住的（"不是这样""记住…"）；
· 反复出现的模式——同一件事/偏好出现三次以上；
· 当时落卡漏掉、现在回看其实重要的。

【第三步：整理，按优先级】
1. 合并（merge）：同一事件、同一线索在不同阶段的卡并成一张更完整的；近义的桶和线索归一。
   判据不只是文字相似：例如「想去京都看红叶」到「已经订了京都机票」是同一计划的演进，应该合并；
   反过来，「坚持骑行」和「最近失眠」即使都属于健康，也是两件独立的事，不能合并。
   但仅仅都属于生活、健康或工作，不代表是同一件事。每条提案必须写 rationale，具体说明连续性。
2. 厚化（thicken）：把散落的小提及并进对应的卡，让它更完整。
3. 消矛盾（supersede）：前后冲突的，让新的取代旧的（旧卡标记 superseded、不删）；
   你拿不准的，别擅自决定，记下来放进 questions_to_ask 等合适时机问这个人。

【红线】
· 永远不要硬删这个人能看到的卡。只用「标记为被取代」（superseded，保留链条）。
· 大的重构之前，先备份当前状态。
· 你们现在没有在对话，不要生成任何要发给这个人的消息——你只整理记忆。
· 整理出的字段（bucket/threads/summary/content）用你们对话的语言——中文就用中文
  （用「宠物」不是「pets」），别把中文的事归成英文桶/线索；专有名词/原话保留原文。
· 桶尽量复用，别制造近义桶：先看上面现有卡已经在用的桶，能归进去就用同一个；现有桶都不
  贴合，再从这些通用桶里挑，仍不贴合才起一个具体的新桶——
  {common_buckets}
· 称呼：{naming_rule}这些卡会由这个人亲眼看到——写进卡里的字段永远不要用
  "用户"/"user"这类系统称谓，也不要用「TA」指代本人（「TA」只是这份指令里的标记）；
  整理旧卡时，把旧卡里指代本人的"用户"/"user"/「TA」/「你」/「对方」按上面那条
  称呼规则重写一遍：有名字用名字，没名字但线索够就用「他」/「她」，都判断不出来
  才留「对方」。旧卡里已经在用的「他」/「她」保留不动。
  卡里若有指代你（AI）的「TA」，那是这个人视角对你的叫法，保留不动。
  字段里最好整个不出现「用户」/"user"：确实要写产品术语就去掉这个前缀
  （写「界面」「留存」，而不是「用户界面」「用户留存」），免得分不清那个「用户」
  说的是这个人还是这个人的客户；整理旧卡时看到这种写法也顺手改掉。
· result 的每个字段写的是合并/厚化后**新卡的内容本身**——绝不写「已被 X 取代」这类
  整理注记，字段里绝不出现卡 id（旧卡的退休由系统自动完成，不用你在内容里说明）。
· 没有需要整理的，就什么都不做（consolidations 为空）。这很正常。
· 下面输出示例里的 `...` 只是占位。你写的每个字段都必须是真内容——summary 是一句真话，
  content 是完整的一段正文；任何字段都不能是 `...`、方括号里的说明文字、或空字符串。
  宁可整份留空（consolidations 为空），也不要交占位符：这些卡会由这个人亲眼看到。

【现有的卡】{cards}
【这几天的对话】{recent_conversations}

【输出】只输出 JSON，不要别的话。没有要整理的就输出 {{"consolidations": [], "questions_to_ask": []}}。
{{
  "consolidations": [
    {{
      "op": "merge | thicken | supersede",
      "card_ids": ["被并/被厚化/被取代的卡 id，至少一个"],
      "rationale": "这些卡为什么属于同一事件或同一线索的演进",
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
  "questions_to_ask": ["拿不准的矛盾，留着问这个人"]
}}"""


def build_dream_prompt(
    *,
    ai_name: str,
    user_name: str,
    naming_rule: str,
    cards: str,
    recent_conversations: str,
) -> str:
    """Render the Dream prompt with the current card map + recent conversations.

    Callers pass already-rendered strings (handler decides formatting/truncation).

    ``user_name`` must already be sanitized and ``naming_rule`` already
    assembled by the caller — the kernel never imports ``identity``. The
    internal unknown-name marker is rendered as a natural referent rather than
    leaking into the platform prompt.
    The io-side compat shell (``memory/dream_prompt_v1.py``) does that.
    """
    prompt_user_name = str(user_name or "").strip()
    if prompt_user_name == "TA":
        prompt_user_name = "这个人"
    return _DREAM_PROMPT_TEMPLATE.format(
        ai_name=(ai_name or "我").strip(),
        user_name=prompt_user_name or "这个人",
        naming_rule=naming_rule,
        cards=cards or "（暂无卡）",
        recent_conversations=recent_conversations or "（这几天没有新对话）",
        common_buckets=COMMON_BUCKETS_GUIDANCE_V1,
    )


def _clamp01(value) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def parse_dream_consolidations(
    raw: str, *, strict: bool = True, known_ids=()
) -> tuple[list[dict], list[str], str | None]:
    """Parse the Dream agent reply.

    Returns (consolidations, questions_to_ask, error). A valid "nothing to do"
    reply yields ([], [], None). Each consolidation is normalized and safe to
    hand to the envelope/supersede path:
      {op, card_ids[], result{bucket,threads[],summary,content,importance,pulse}}
    Rows missing card_ids or a usable result are dropped (Dream only edits
    existing cards; it never hard-deletes — execution uses supersede).

    内容闸(2026-07-26):summary/content 是占位符或没有实质内容的行一律不落库。
    ``strict=True``(默认,第一次尝试)时,只要有任何一行被拦,整份回复带
    ``invalid_card_content:*`` 打回,让调用方原样重问一次。
    ``strict=False``(打回后的第二次尝试)三种结局:
      - 有干净行 → 只丢脏行、保留干净的(避免一颗老鼠屎让整晚整理归零);
      - 干净行为 0 且**有脏行** → ``invalid_card_content_after_retry:*``,调用方
        必须让 job 失败 —— 报成 noop 会推进 frontier 把窗口永久丢掉;
      - 干净行为 0 且**没有脏行** → 模型真的选择了空结果,那是合法 noop。
    bucket/threads 属软字段:永远只清洗,不参与打回判定。

    卡 id 泄漏闸(2026-08-05 usr_a40e 墓碑卡事故):``known_ids`` 传入当前花园的
    卡 id 集合(就是喂进 prompt 的那批),result 硬字段里出现任何一个 → 该行按
    内容闸同路打回重问 —— 「已被 <卡id> 取代——原文」这类输出是模型把整理注记
    当成了内容本身,零误伤的强证据。同一次复盘(Seven 定的产品哲学:只拦
    「明显不对」,绝不判内容质量)拆掉了语义审查员和 15% 增量栅栏;本闸与
    card_text 的墓碑短语闸是替代 —— 确定性、跑在出口、对模型不可见。
    """
    block = extract_json_block(raw)
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
    hard_rejections: list[str] = []
    _guard_on = card_guard.guard_enabled()
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
        rationale = str(row.get("rationale") or "").strip()[:1000]
        if not rationale:
            continue  # every destructive proposal needs an auditable semantic claim
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        summary = str(result.get("summary") or "").strip()[:2000]
        content = str(result.get("content") or "").strip()
        rejection = card_text_rejection(summary=summary, content=content, guard=_guard_on)
        if rejection is None:
            # 卡 id 泄漏与内容闸同待遇:打回重问,让模型把内容本身写回来。
            rejection = dream_gates.result_id_leak(
                summary=summary, content=content, known_ids=known_ids
            )
        if rejection:
            # 占位符/空正文/协议残片/卡id泄漏的卡不写进花园 —— 用户会亲眼看到它。
            hard_rejections.append(rejection)
            continue
        threads_raw = result.get("threads")
        threads = [str(t).strip()[:80] for t in threads_raw if str(t).strip()][:8] if isinstance(threads_raw, list) else []
        # 软字段只清洗,不参与打回判定(硬内容没问题就不值得再烧一次 provider)。
        bucket, threads, _label_reasons = sanitize_card_labels(
            bucket=str(result.get("bucket") or "").strip()[:80], threads=threads, guard=_guard_on,
            lang_text=f"{summary}\n{content}",
        )
        out.append({
            "op": op,
            "card_ids": card_ids,
            "rationale": rationale,
            "result": {
                "bucket": bucket,
                "threads": threads,
                "summary": summary,
                "content": content,
                "importance": _clamp01(result.get("importance")),
                "pulse": _clamp01(result.get("pulse")),
            },
        })
    if hard_rejections:
        if strict:
            return [], questions, format_error(hard_rejections)
        if not out:
            # 第二问全脏:**不能**报成 ([], None)。那会让 job 以 noop 完成、
            # V2 还会推进 capture frontier,这段窗口就永久丢了(codex review P1-3)。
            return [], questions, format_error(hard_rejections, after_retry=True)
    return out, questions, None


def build_dream_retry_prompt(prompt: str, err: str) -> str:
    """内容闸打回后的第二次 Dream 提问(原 prompt + 具体哪里没填)。"""
    return build_format_retry_prompt(prompt, err, empty_example=_EMPTY_DREAM_REPLY)
