"""Central v1 memory prompt snippets.

Seven can replace this file's text blocks without changing the memory
execution code. Keep route A skills and route B hosted prompts semantically
aligned with these rules.
"""

# Canonical common buckets (A9) — ONE shared bilingual vocabulary so onboarding +
# capture + migration converge instead of each card minting a fresh near-synonym
# bucket (工作/职业/事业) or, with a weak model, all landing in 未分类. Tuned for a
# COMPANION app (not a notes tool): emotion / relationship / preferences / pets /
# values matter more than tidy productivity folders. NOT a hard taxonomy — the model
# still creates a specific bucket (妈妈 / 某个朋友) when none of these fit. Each pair
# is the SAME bucket in the two garden languages; never let 工作 and Work coexist —
# use the side matching the user's language. Seven can edit this list (and the
# guidance below) without touching execution code; everything downstream derives
# from it, so there's a single source of truth.
COMMON_BUCKETS_V1 = [
    ("工作", "Work"),
    ("目标与成长", "Goals & growth"),
    ("家庭", "Family"),
    ("朋友", "Friends"),
    ("宠物", "Pets"),
    ("我们的关系", "Our relationship"),
    ("情绪与安抚", "Feelings & comfort"),
    ("偏好与边界", "Preferences & boundaries"),
    ("个性与价值观", "Personality & values"),
    ("健康", "Health"),
    ("爱好", "Interests"),
    ("金钱", "Money"),
    ("饮食", "Food"),
    ("地点与旅行", "Places & travel"),
]

# Ready-to-inject bilingual line: 工作/Work、目标与成长/Goals & growth、… — short enough to fit every prompt.
# English-only list for the route-B guidance block below (kept in sync automatically).
_COMMON_BUCKETS_EN = " / ".join(en for _zh, en in COMMON_BUCKETS_V1)
# Chinese-only list — the guidance presents zh and en as SEPARATE lists (not 工作/Work
# pairs), because the joined-pair format made the model copy "健康/Health" verbatim as
# the bucket name instead of picking one side.
_COMMON_BUCKETS_ZH = "、".join(zh for zh, _en in COMMON_BUCKETS_V1)

# Deterministic bucket-language backstop. The model still mislabels a Chinese memory
# with an English common bucket (~1/3 of the time — e.g. "Pets" for 用户十年前养过一只狗),
# despite the guidance. Since the common buckets are a fixed zh<->en pair map, we can
# map a wrong-language COMMON bucket back to the card's own language IN CODE — a backstop
# that catches EVERY write path (genesis / capture / agent inline / io_cli all funnel
# through _memory_inner_from_action) regardless of prompt drift, and is unit-testable
# without a real model. Custom buckets (妈妈 / the house) pass through unchanged.
_BUCKET_EN_TO_ZH = {en: zh for zh, en in COMMON_BUCKETS_V1}
_BUCKET_ZH_TO_EN = {zh: en for zh, en in COMMON_BUCKETS_V1}

# Short forms the model actually emits instead of the canonical multi-word bucket.
# Observed 2026-08-10 (usr_7001b1df80e2024d, V2): a Chinese card came back with the
# bucket "preferences" — not a key in the pair map above, so the backstop passed it
# straight through and an English bucket landed in a Chinese garden. The exact-match
# map only ever caught models that wrote "Preferences & boundaries" in full.
#
# Keys are casefolded; the lookup below casefolds too, so a lowercase "work" is
# also caught. Only unambiguous short forms of the common buckets belong here — a
# genuinely custom bucket must still pass through untouched.
_BUCKET_ALIASES = {
    "preferences": "偏好与边界",
    "boundaries": "偏好与边界",
    "goals": "目标与成长",
    "growth": "目标与成长",
    "relationship": "我们的关系",
    "feelings": "情绪与安抚",
    "emotions": "情绪与安抚",
    "comfort": "情绪与安抚",
    "personality": "个性与价值观",
    "values": "个性与价值观",
    "places": "地点与旅行",
    "travel": "地点与旅行",
}
# Canonical-name lookup keyed by casefolded form, so case-only drift converges too.
_BUCKET_EN_FOLDED = {en.casefold(): zh for zh, en in COMMON_BUCKETS_V1}
_BUCKET_ALIAS_TO_ZH = {**_BUCKET_EN_FOLDED, **_BUCKET_ALIASES}
_BUCKET_ALIAS_TO_EN = {
    folded: _BUCKET_ZH_TO_EN[zh]
    for folded, zh in _BUCKET_ALIAS_TO_ZH.items()
    if zh in _BUCKET_ZH_TO_EN
}


def _text_is_chinese(text: str) -> bool:
    """A card counts as Chinese if its text carries any CJK ideograph."""
    return any("一" <= ch <= "鿿" for ch in (text or ""))


def normalize_bucket_language(bucket: str, text: str) -> str:
    """Map a COMMON bucket that's in the wrong language vs the card's content to the
    card's own language via the fixed zh<->en pair map. Custom/unknown buckets pass
    through unchanged. Deterministic — the code backstop behind the bucket prompts."""
    b = (bucket or "").strip()
    if not b:
        return b
    if _text_is_chinese(text):
        # Exact pair map first, then the casefolded short-form table; a bucket in
        # neither is genuinely custom (妈妈 / the house) and must survive as written.
        mapped = _BUCKET_EN_TO_ZH.get(b)
        return mapped if mapped else _BUCKET_ALIAS_TO_ZH.get(b.casefold(), b)
    mapped = _BUCKET_ZH_TO_EN.get(b)
    return mapped if mapped else _BUCKET_ALIAS_TO_EN.get(b.casefold(), b)


# Runtime-agnostic card-writing rules — shared verbatim by the V1 hosted-runtime
# guidance block below and by the Runtime V2 `memory_write` tool description
# (capabilities/tool_schema.py).
#
# ⚠️ Op names are deliberately NOT in here. V1 says `memory.add` / `memory.supersede`;
# V2's schema takes op='add'/'update'/'delete'. V2 ran without ANY of these rules
# until 2026-08-10, and the obvious "just inject MEMORY_WRITE_GUIDANCE_V1 into V2"
# fix would have taught the V2 model two op names its own schema rejects — worse
# than no guidance. Each runtime states its own ops and shares the rules below.
MEMORY_WRITE_RULES_V1 = ("""
- Write only durable user/relationship facts, preferences, boundaries, repeated patterns, or meaningful events.
- Do not write greetings, jokes, one-off task instructions, unconfirmed guesses, roleplay hypotheticals, or the assistant's own inference.
- Refer to the person by the name on their identity card. NEVER call them 「用户」/"the user" in card text, and do not use the placeholder 「TA」 or the second person 「你」 for them. When you do not know a name, prefer dropping the subject entirely (「常在深夜写代码」) over inventing a label.
- Pick one bucket and 1-4 reusable threads. Prefer existing bucket/thread names when provided; converge on the common buckets and only mint a specific new bucket (Mom / 妈妈 / the house) when none fit.
- The bucket name MUST be ONE word in the memory's OWN language: a Chinese memory uses a Chinese bucket (from: """
+ _COMMON_BUCKETS_ZH + """); an English memory uses an English bucket (from: """
+ _COMMON_BUCKETS_EN + """). NEVER write a bilingual slash pair like 「健康/Health」or 「宠物/Pets」, and never let 工作 and Work coexist as two buckets.
- importance means future usefulness for understanding the user. pulse means emotional activation when remembered.
- content must use three Markdown sections: 记忆 / 上下文 / 使用提示.
- Do not claim "saved" or "remembered" before the backend write actually succeeds.
""").strip()


MEMORY_WRITE_GUIDANCE_V1 = ("""
Memory write guidance:
- Use memory.add for new durable events/facts.
- Use memory.supersede when the user corrects or replaces an older memory; do not patch old cards in place.
"""
+ MEMORY_WRITE_RULES_V1).strip()


# Full bucket-convergence guidance injected into every card-creating prompt
# (capture / migrate / genesis) so onboarding and capture steer toward the same set.
COMMON_BUCKETS_GUIDANCE_V1 = (
    "桶名要收敛、可复用,别每张卡都新起一个近义桶。优先从这组通用桶里选并复用——\n"
    "  中文记忆用:" + _COMMON_BUCKETS_ZH + "\n"
    "  英文记忆用:" + _COMMON_BUCKETS_EN + "\n"
    "桶名【只写一个词】、且只用这条记忆本身的语言那一份:中文记忆写「健康」,英文记忆写「Health」。"
    "⚠️ 绝不要把两种语言拼在一起当桶名——别写成「健康/Health」「宠物/Pets」这种带斜杠的双语串。"
    "这些都不贴合,再起一个简短的具体桶(如 妈妈、房子);别造「工作/职业/事业」这种近义重复桶。"
).strip()
