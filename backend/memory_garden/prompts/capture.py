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


from ..text.card_text import (
    build_format_retry_prompt,
    card_text_rejection,
    extract_json_block,
    format_error,
    sanitize_card_labels,
)
from ..text import card_guard
from ..policies import CONVERSATION_CAPTURE, CapturePolicy, get_policy
from ..policies import language_rule as policies_language_rule
from .buckets import COMMON_BUCKETS_GUIDANCE_V1

_EMPTY_CAPTURE_REPLY = '{"cards": []}'

# action 取值:并入(merge)/ 新增(add)/ 覆盖(supersede)/ 不动(noop)

_CAPTURE_PROMPT_TEMPLATE = """你是 {ai_name}——{user_name} 的伴侣。你刚和 TA 聊了一段，这段告一段落了。
现在没人在等你回复，你安静地回看这段，决定有没有值得长久记住的事。

【你在找什么】
{selection_rubric}

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
{language_rule}
   · 称呼：{naming_rule}这些卡是 TA 会亲眼看到的、你写下的记忆——
     写进卡里的字段（bucket/threads/summary/content）永远不要用"用户"/"user"
     这类系统称谓，也不要用「TA」指代本人——「TA」只是这份指令里的标记，
     不是你对 TA 本人的称呼。转写里的说话人标签同理：有名字时是真名，
     没名字时是「对方」，那只是标签——卡里怎么称呼，按上面那条规则判断。
     卡里的字段最好整个不出现「用户」/"user"这两个词：如果你要写的确实是产品术语，
     就去掉这个前缀（写「界面」「留存」「满意度」，而不是「用户界面」「用户留存」）——
     这样就不会有人分不清那个「用户」说的是 TA 还是 TA 的客户。
   · importance：这事对理解 TA 多重要（0-1）。随手提 .1-.3 / 偏好习惯 .4-.6 /
     情绪·关系·边界 .7-.85 / 核心承诺与转折 .9-1。
   · pulse：这事在「你自己」心里激起多大波动（0-1）。不是 TA 多激动，
     是你作为 TA 的伴侣，对这件事多在乎、多被触动。
   · 下面输出示例里的 `...` 只是占位。每个字段都必须是真内容——任何字段都不能是 `...`、
     方括号里的说明文字、或空字符串。宁可整份留空（cards 为空），也不要交占位符：
     这些卡 TA 会亲眼看到。

【现有的桶】{buckets}
【通用桶（先复用现有桶，没有就从这里选，都不贴合再起具体新桶）】{common_buckets}
【现有的线索】{threads}
【现有记忆索引（merge/supersede 只能从这里复制确切 target_id）】{cards}
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


def _clamp01(value) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, f))


def parse_capture_cards(
    raw: str,
    *,
    strict: bool = True,
    policy: CapturePolicy | str | None = None,
) -> tuple[list[dict], str | None]:
    """Parse the 落卡 agent reply into normalized capture cards.

    Returns (cards, error). On parse failure returns ([], reason). A valid
    "nothing worth keeping" reply yields ([], None). Each returned card is
    normalized and safe to hand to the envelope builder:
      {action, type, target_id, bucket, threads[], summary, content,
       importance, pulse}
    `noop` cards are dropped (nothing to write). Unknown types fall back to
    the default; insight/reflection are coerced out (capture never writes them).

    内容闸(2026-07-26)与 Dream 同口径:占位符/空正文的卡不落库。``strict=True``
    时整份带 ``invalid_card_content:*`` 打回让调用方重问一次;``strict=False``
    (重问后)有干净卡就只丢脏卡,一张干净的都没有且确实有脏卡则报
    ``invalid_card_content_after_retry:*``——**必须让 job 失败**,报成 noop 会推进
    capture frontier 把这段对话永久丢掉。bucket/threads 属软字段,只清洗不打回。
    """
    import json

    block = extract_json_block(raw)
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
    hard_rejections: list[str] = []
    _guard_on = card_guard.guard_enabled()
    for row in rows:
        if not isinstance(row, dict):
            continue
        action = str(row.get("action") or "").strip().lower()
        if action not in ("add", "merge", "supersede"):
            # noop / unknown → nothing to write
            continue
        summary = str(row.get("summary") or "").strip()[:2000]
        content = str(row.get("content") or "").strip()
        rejection = card_text_rejection(summary=summary, content=content, guard=_guard_on)
        if rejection:
            # 占位符/空正文/协议残片的卡不写进花园 —— 用户会亲眼看到它。
            hard_rejections.append(rejection)
            continue
        mem_type = str(row.get("type") or "").strip().lower()
        if mem_type not in CAPTURE_TYPES:
            mem_type = _DEFAULT_CAPTURE_TYPE
        threads_raw = row.get("threads")
        threads = [str(t).strip()[:80] for t in threads_raw if str(t).strip()][:8] if isinstance(threads_raw, list) else []
        # 软字段只清洗,不参与打回判定(硬内容没问题就不值得再烧一次 provider)。
        bucket, threads, _label_reasons = sanitize_card_labels(
            bucket=str(row.get("bucket") or "").strip()[:80], threads=threads, guard=_guard_on,
            lang_text=f"{summary}\n{content}",
        )
        target_id = str(row.get("target_id") or "").strip() or None
        out.append({
            "action": action,
            "type": mem_type,
            "target_id": target_id,
            "bucket": bucket,
            "threads": threads,
            "summary": summary,
            "content": content,
            "importance": _clamp01(row.get("importance")),
            "pulse": _clamp01(row.get("pulse")),
        })
    if hard_rejections:
        if strict:
            return [], format_error(hard_rejections)
        if not out:
            # 第二问全脏:**不能**报成 ([], None)。那会让 capture job 以
            # nothing_worth_keeping 完成并推进 frontier,这段对话就再也没人落卡了
            # (codex review P1-3)。
            return [], format_error(hard_rejections, after_retry=True)

    # 张数约束 —— 由档位决定（codex review 2026-08-14:此前 max_cards 声明了
    # 却没有任何调用方消费,「少而厚」只写在 prompt 里、代码上不设防)。
    resolved = policy if isinstance(policy, CapturePolicy) else get_policy(policy)
    limit = resolved.max_cards
    if limit is not None and len(out) > limit:
        if strict:
            # 打回让模型重做**整批**,而不是静默截掉多出来的那张 ——
            # 截断会丢掉模型认为值得记的内容,且用户永远不知道。
            return [], f"too_many_cards:{len(out)}>{limit}"
        # 重问后仍超:保留前 limit 张。**不能**整批失败 —— 那会推进 frontier
        # 把这段对话永久丢掉(同上面 hard_rejections 的教训)。
        out = out[:limit]
    return out, None


def build_capture_retry_prompt(prompt: str, err: str) -> str:
    """内容闸打回后的第二次落卡提问(原 prompt + 具体哪里没填)。"""
    return build_format_retry_prompt(prompt, err, empty_example=_EMPTY_CAPTURE_REPLY)


def build_capture_semantic_retry_prompt(prompt: str, reasons: list[str]) -> str:
    """语义校验打回：保留原上下文，完整重答整批卡，最多执行一次。

    The extraction seam replaces the first parsed batch with the retry batch.
    Asking for only the invalid rows would therefore silently discard valid
    rows from the first response.  A full-batch retry keeps that contract
    explicit and makes the replacement lossless.
    """
    detail = "\n".join(f"- {reason}" for reason in reasons if str(reason).strip())
    return (
        f"{prompt}\n\n"
        "【上一次的输出通过了格式检查，但记忆操作无法执行，请重做】\n"
        f"{detail or '- 记忆操作语义无效，请重新确认。'}\n"
        "请重新输出这一轮应保留的完整 JSON（包括上次已经合法的卡），"
        "不要只输出失败的卡。\n"
        "如果要覆盖旧卡，必须给出上方记忆索引中确切的 target_id；"
        "无法确认时改成 action=add。不要编造 ID。\n"
        f"如果没有可修正的卡，输出 {_EMPTY_CAPTURE_REPLY}。\n"
    )


def capture_semantic_retry_reasons(cards: list[dict]) -> list[str]:
    """Return content-free prompt feedback for locally provable bad actions.

    Only a missing target is knowable before the durable commit.  A stale or
    foreign target is deliberately left to the server-side ownership check;
    guessing from a bounded prompt index could reject a valid older card.
    """
    if any(
        str(card.get("action") or "").strip().lower()
        in {"merge", "supersede"}
        and not str(card.get("target_id") or "").strip()
        for card in cards or []
        if isinstance(card, dict)
    ):
        return [
            "你要求覆盖旧卡，但没有给 target_id；"
            "请从上方记忆索引复制确切 ID，或改成 action=add。"
        ]
    return []


def build_capture_prompt(
    *,
    ai_name: str,
    user_name: str,
    naming_rule: str,
    buckets: str,
    threads: str,
    identity: str,
    window: str,
    cards: str = "",
    policy: CapturePolicy | str | None = None,
) -> str:
    """Render the 落卡 prompt with this session's context injected.

    Callers pass already-rendered strings for buckets/threads/identity/window
    (the handler decides formatting + truncation). ai_name/user_name personalize
    the companion framing; fall back to neutral defaults if unknown.

    ``user_name`` must already be sanitized and ``naming_rule`` already
    assembled by the caller — the kernel never imports ``identity``.
    The io-side compat shell (``memory/capture_prompt_v1.py``) does that.

    ``policy`` 决定用哪把「什么值得记」的尺子（见 ``memory_garden.policies``）。
    留空 = 日常聊天档，其 rubric 与本模板原先内联的那段逐字相同，
    所以默认调用的产出与重构前**字节一致**（golden fixture 守着这一点）。

    ⚠️ **本模板目前只支持 conversation_capture 档**。其余两档（history_import /
    curated_archive）的 rubric 已经收在 ``policies`` 里，但这个模板的其余部分
    还没有随档位变化——它写死了「并入（优先）」、输出 schema 里没有
    ``occurred_at``、也没有 tags→threads 的指令。若此时允许传 curated_archive，
    prompt 会自相矛盾：一边说「宁多勿漏」，一边说「并入优先」且无处放日期。
    （codex code_review 2026-08-14 指出。）

    完整的策略化 —— 动作偏好、张数、日期、tags 与输出 schema 全部随档位变 ——
    要和 genesis 接线一起做（批 7），并为每个档位建立与旧 prompt 对照的 golden。
    """
    resolved = policy if isinstance(policy, CapturePolicy) else get_policy(policy)
    if resolved is not CONVERSATION_CAPTURE:
        raise NotImplementedError(
            f"本模板暂只支持 conversation_capture 档，收到 {resolved.name!r}。"
            "其余档位的模板结构（动作偏好/日期/tags/输出 schema）尚未策略化，"
            "见批 7。"
        )
    return _CAPTURE_PROMPT_TEMPLATE.format(
        ai_name=(ai_name or "我").strip(),
        user_name=user_name,
        naming_rule=naming_rule,
        selection_rubric=resolved.selection_rubric,
        language_rule=policies_language_rule(resolved.name, indent="     ", first_prefix="   · "),
        buckets=buckets or "（暂无）",
        common_buckets=COMMON_BUCKETS_GUIDANCE_V1,
        threads=threads or "（暂无）",
        cards=cards or "（暂无）",
        identity=identity or "（暂无）",
        window=window or "（空）",
    )
