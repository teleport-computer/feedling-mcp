"""Migration prompt (v1) — 老卡原地升级成 v1(承接老数据迁移方案 §6）。

只做一件事:把每张老卡(title/description/her_quote/context/linked_dimension)
**原地**改写成 v1 形状(bucket/threads/summary/content),保 id,不发明事实,
**不 merge、不 supersede、不删**。复用 capture lane(job_kind=memory_migrate);
写入走 memory.upgrade(原地、保 id)。本模块只负责 prompt 文本与输出解析。

红线:
  - 只升级"喂进来的这几张",绝不新建/合并/取代别的卡。
  - 不发明事实;原文里没有的别补。
  - resolve-before-create:用已有的桶/线索词表,别膨胀出近义新桶。
  - 每条必须带回它的原始 id(原地升级靠 id 对回去)。
"""
from __future__ import annotations

from ..text import card_guard
from ..text.leak_signals import GENERIC_SIGNALS, LeakSignals
from ..text.card_text import extract_json_block, sanitize_card_labels
from .buckets import COMMON_BUCKETS_GUIDANCE_V1

_MIGRATE_PROMPT_TEMPLATE = """你是 {ai_name}——{user_name} 的伴侣。现在是一段安静的时间，没人在和你说话。
你在把一些**旧格式**的记忆卡整理成新格式。这不是重新理解、不是合并、不是新增——
只是把每一张旧卡**原样升级**成新结构，内容尽量保住，别丢、别编。

【你要做什么】
下面每张旧卡有一个 id 和一些旧字段（标题/描述/原话/上下文/关联维度）。
把**每一张**改写成新结构：
  · bucket（归类）：从下面"已有桶/线索"里选最贴的；实在没有再起一个简短的，别造近义重复桶。
  · threads（线索，0-3 个）：优先用已有线索；旧的"关联维度"可作线索候选。
  · summary（一句话主旨）：基于旧的标题/描述。
  · content（正文）：把描述/上下文/原话融进去，写成连贯的一段；旧"原话"作为依据保留。

【字段映射（参考，不是死规则）】
  · 描述/标题 → summary + content 主干
  · 上下文(context) → content 里的背景
  · 原话(her_quote) → content 里的原话/依据
  · 关联维度(linked_dimension) → threads 候选

【红线】
  · 只升级下面这几张，**绝不新建、不合并、不取代别的卡**。
  · **不发明事实**——旧卡里没有的信息不要补。
  · 每条**必须带回原始 id**，一一对应。
  · 语言：bucket/threads/summary/content 用**卡片原文的语言**——中文卡就用中文
    （用「宠物」不是「pets」），别迁成英文桶/线索；专有名词/原话保留原文。
  · 一张都不要漏；实在无法判断的，summary 用旧标题、bucket 用"未归类"也行，别丢卡。

【已有的桶/线索词表】{vocab}
【通用桶（先复用已有桶，没有就从这里选，都不贴合再起具体新桶）】{common_buckets}
【要升级的旧卡】{old_cards}

【输出】只输出 JSON，不要别的话。
{{
  "upgrades": [
    {{
      "id": "这张旧卡的原始 id",
      "bucket": "...",
      "threads": ["...", "..."],
      "summary": "...",
      "content": "...一段连贯正文..."
    }}
  ]
}}"""


def build_migrate_prompt(
    *,
    ai_name: str,
    user_name: str,
    old_cards: str,
    vocab: str,
) -> str:
    """Render the migration prompt. Callers pass already-rendered strings
    (handler decides formatting/truncation of the batch + the bucket/thread vocab)."""
    prompt_user_name = str(user_name or "").strip()
    if prompt_user_name.casefold() == "ta":
        prompt_user_name = "这个人"
    return _MIGRATE_PROMPT_TEMPLATE.format(
        ai_name=(ai_name or "我").strip(),
        user_name=prompt_user_name or "这个人",
        old_cards=old_cards or "（没有要升级的卡）",
        vocab=vocab or "（暂无已有桶/线索）",
        common_buckets=COMMON_BUCKETS_GUIDANCE_V1,
    )


def parse_migrated_cards(
    raw: str,
    *,
    allowed_ids: set[str] | None = None,
    signals: LeakSignals = GENERIC_SIGNALS,
) -> tuple[list[dict], list[str], str | None]:
    """Parse the migration agent reply.

    Returns (upgrades, unmigrated_ids, error). Each upgrade is normalized and safe
    to hand to the memory.upgrade path: {id, bucket, threads[], summary, content}.
    Rows are dropped if they have no id, a duplicate id, no usable content, or (when
    allowed_ids is given) an id that wasn't in the batch — the agent must only
    upgrade cards it was handed, never invent or retarget.

    `unmigrated_ids` = the batch ids (allowed_ids) that did NOT come back as a valid
    upgrade — i.e. the agent omitted them OR they were dropped. The handler MUST
    treat these as not-yet-migrated and retry them next round; it must NOT use
    "error is None" to mean "whole batch done". Empty allowed_ids ⇒ [].
    """
    import json

    all_ids = set(str(i) for i in allowed_ids) if allowed_ids is not None else None

    def _unmigrated(seen: set[str]) -> list[str]:
        return sorted(all_ids - seen) if all_ids is not None else []

    block = extract_json_block(raw)
    if not block:
        return [], _unmigrated(set()), "no_json_object"
    try:
        doc = json.loads(block)
    except (ValueError, TypeError) as e:
        return [], _unmigrated(set()), f"json_decode_error:{type(e).__name__}"
    if not isinstance(doc, dict):
        return [], _unmigrated(set()), "not_an_object"
    rows = doc.get("upgrades")
    if not isinstance(rows, list):
        return [], _unmigrated(set()), "missing_upgrades_list"

    out: list[dict] = []
    seen: set[str] = set()
    _guard_on = card_guard.guard_enabled()
    for row in rows:
        if not isinstance(row, dict):
            continue
        mid = str(row.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        if all_ids is not None and mid not in all_ids:
            continue  # never upgrade a card outside this batch
        summary = str(row.get("summary") or "").strip()[:2000]
        content = str(row.get("content") or "").strip()
        if not summary and not content:
            continue  # empty result — skip rather than write a hollow card
        # 模型原始输出/协议残片:硬字段脏 → 跳过(计入 unmigrated,下轮重试);桶脏 → 清空
        # (走下游的空桶默认);threads 逐项滤脏。此路径提前封信封、绕过 actions 层,故在此接。
        if _guard_on and (
            card_guard.field_pollution_reason(summary, signals)
            or card_guard.field_pollution_reason(content, signals)
        ):
            continue
        threads_raw = row.get("threads")
        threads = [str(t).strip()[:80] for t in threads_raw if str(t).strip()][:8] if isinstance(threads_raw, list) else []
        bucket, threads, label_reasons = sanitize_card_labels(
            bucket=str(row.get("bucket") or "").strip()[:80], threads=threads, guard=_guard_on,
            lang_text=f"{summary}\n{content}", signals=signals,
        )
        if "bucket_protocol_leak" in label_reasons:
            # migrate 走 prebuilt-envelope upgrade,绕过了宿主写入路径上那道会给空桶
            # 兜默认值的闸 —— 必须在此就地降级到按语言的默认桶(codex code_review Important)。
            bucket = card_guard.default_bucket_for_text(f"{summary}\n{content}")
        seen.add(mid)
        out.append({
            "id": mid,
            "bucket": bucket,
            "threads": threads,
            "summary": summary,
            "content": content,
        })
    return out, _unmigrated(seen), None
