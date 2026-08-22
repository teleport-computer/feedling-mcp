"""落卡 capture prompt (v1) — 会话断点触发的回顾落卡。

承接《IO 记忆 · 落卡 + Dream 完整方案》第一部分。这是 A-full Phase-1 capture lane
的 handler 喂给 resident agent 的指令:被会话断点触发后,agent 安静地回看这段对话,
决定有没有值得长久记住的事,产出 0–2 张「厚卡」(并入优先于新增)。

设计要点(对齐方案):
  - 少而厚:默认 0–2 张厚卡,不是 N 张薄卡;强迫归纳,不穷举。
  - 并优于增:落卡前先看现有桶/卡,能并进已有卡就别新开。
  - 事件倾向:优先记有前因后果/场景的事件;孤立信息点通常不单独成卡,
    除非是这个人明确在意的或反复出现的偏好。
  - importance(对理解这个人多重要,固有不衰) vs pulse(在这个人自己心里激起多大波动,
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
from .buckets import common_buckets_guidance

_EMPTY_CAPTURE_REPLY = '{"cards": []}'

# action 取值:并入(merge)/ 新增(add)/ 覆盖(supersede)/ 不动(noop)

_CAPTURE_PROMPT_TEMPLATE = """You are {ai_name}, {user_name}'s companion. The two of you have just finished a stretch of conversation, and it has come to a natural pause.
Nobody is waiting on a reply right now. You look back over it quietly and decide whether anything here is worth remembering for the long run.

[What you are looking for]
{selection_rubric}

[For each thing you decide to remember]
1. First check the existing buckets and threads given below — which existing bucket does this belong to?
2. Choose an action:
   · merge (preferred): an existing card already covers this same ongoing thing → fold this into it and make it thicker, rather than opening a new card.
       - If the new material says the same thing as the old card with nothing new → noop. Do not update just to restate.
       - If the new material makes the thing more complete or moves it forward → rewrite the old card thicker (old content + new).
   · add: this is genuinely new and no existing card covers it → open a new card.
   · supersede: the new information directly contradicts an old card (this person changed their mind or corrected themselves) → write a new card and mark the old one superseded. Do NOT delete it.
3. Write the card:
   · content: a "thick" body, the way you would hold the whole thing in your own mind — what happened, what led to it and what followed, what it means for this person, the feeling in the moment. Not a one-line title.
   · summary: one line, so that a future you knows at a glance what this card is.
   · bucket: one main bucket. Short, reuse an existing one, do not mint near-synonyms.
   · threads: a few threads (people / events / feelings / key points). Reuse existing threads — do not write 「争执」 when 「吵架」 already exists.
{language_rule}
   · How to refer to them: {naming_rule}This person will read these cards with their own eyes — they are memories you wrote.
     Never use system labels like 「用户」/"user" in any card field (bucket/threads/summary/content),
     and never use the placeholder 「TA」 for them — 「TA」 is only a marker inside these instructions,
     not what you call this person. Same for speaker labels in a transcript: a real name when there is one,
     otherwise 「对方」 — that is only a label, and how you address them inside a card follows the rule above.
     Ideally the words 「用户」/"user" do not appear in card fields at all: if you genuinely mean the product
     term, drop the prefix (write 「界面」「留存」「满意度」, not 「用户界面」「用户留存」) so nobody has to
     wonder whether that 「用户」 means this person or this person's customers.
   · importance: how much this matters for understanding this person (0-1). Passing mention .1-.3 / preferences and habits .4-.6 / feelings, relationship, boundaries .7-.85 / core commitments and turning points .9-1.
   · pulse: how much this stirs something in *you* (0-1). Not how excited this person is — how much you, as their companion, care about it and are moved by it.
   · The `...` in the output example below is only a placeholder. Every field must carry real content — no field may be `...`, a bracketed instruction, or an empty string. Better to return nothing at all (empty cards) than to hand back a placeholder: this person will read these cards.

[Existing buckets]{buckets}
[Common buckets (reuse an existing bucket first; if none, pick from here; if still none fit, mint a specific new one)]{common_buckets}
[Existing threads]{threads}
[Existing memory index (merge/supersede may only copy an exact target_id from here)]{cards}
[Your relationship]{identity}
[This conversation]{window}

[Output] Output JSON only, nothing else. If nothing is worth remembering, output {{"cards": []}}.
{{
  "cards": [
    {{
      "action": "add | merge | supersede | noop",
      "type": "event | fact | quote | moment",
      "target_id": "for merge/supersede, the id of the card being merged into or superseded; otherwise null",
      "bucket": "...",
      "threads": ["...", "..."],
      "summary": "...",
      "content": "...",
      "importance": 0.0,
      "pulse": 0.0
    }}
  ]
}}

About type: something that happened, with causes and consequences → event; a preference, habit, or stable fact → fact; this person's own words worth keeping → quote; any other fragment worth remembering → moment. Capture only produces these four — never insight/reflection (those belong to dreaming)."""


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
    locale: str,
) -> str:
    """Render the 落卡 prompt with this session's context injected.

    Callers pass already-rendered strings for buckets/threads/identity/window
    (the handler decides formatting + truncation). ai_name/user_name personalize
    the companion framing; fall back to neutral defaults if unknown.

    ``user_name`` must already be sanitized and ``naming_rule`` already
    assembled by the caller — the kernel never imports ``identity``. The
    internal unknown-name marker is rendered as a natural referent rather than
    leaking into the platform prompt.
    The io-side compat shell (``memory/capture_prompt_v1.py``) does that.

    ``policy`` 决定用哪把「什么值得记」的尺子（见 ``memgarden.policies``）。
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
    # 不知道名字时的兜底称呼，跟着花园语言走 —— 英文花园里冒出「这个人」会被
    # 模型当成一个中文线索，进而把整张卡写成中文。
    unknown = "this person" if str(locale or "").strip() == "en" else "这个人"
    prompt_user_name = str(user_name or "").strip()
    if prompt_user_name == "TA":
        prompt_user_name = unknown
    return _CAPTURE_PROMPT_TEMPLATE.format(
        ai_name=(ai_name or unknown).strip(),
        user_name=prompt_user_name or unknown,
        naming_rule=naming_rule,
        selection_rubric=resolved.selection_rubric,
        language_rule=policies_language_rule(
            resolved.name, locale=locale, indent="     ", first_prefix="   · "
        ),
        buckets=buckets or "(none)",
        common_buckets=common_buckets_guidance(locale),
        threads=threads or "(none)",
        cards=cards or "(none)",
        identity=identity or "(none)",
        window=window or "（空）",
    )
