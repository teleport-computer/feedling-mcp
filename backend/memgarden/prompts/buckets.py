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
# that catches EVERY write path regardless of prompt drift, and is unit-testable
# without a real model. Custom buckets (妈妈 / the house) pass through unchanged.
_BUCKET_EN_TO_ZH = {en: zh for zh, en in COMMON_BUCKETS_V1}
_BUCKET_ZH_TO_EN = {zh: en for zh, en in COMMON_BUCKETS_V1}

# Short forms the model actually emits instead of the canonical multi-word bucket.
# Observed 2026-08-10 in production: a Chinese card came back with the
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
# Keep this block operation-agnostic: Runtime V2 wake turns reuse it in a narrowed
# add/update-only tool description where mentioning delete would reopen a hidden
# destructive affordance in the prompt even though the executor still refuses it.
MEMORY_CARD_LENGTH_RULE_V1 = (
    "- Keep each card to ONE thing and its content under ~1000 characters. "
    "Split a longer story into several cards instead of writing one long card."
)

MEMORY_WRITE_RULES_V1 = ("""
- Write only durable user/relationship facts, preferences, boundaries, repeated patterns, or meaningful events.
- Do not write greetings, jokes, one-off task instructions, unconfirmed guesses, roleplay hypotheticals, or the assistant's own inference.
"""
+ MEMORY_CARD_LENGTH_RULE_V1
+ """
- Refer to the person by the name on their identity card. NEVER call them 「用户」/"the user" in card text, and do not use the placeholder 「TA」 or the second person 「你」 for them. When you do not know a name, prefer dropping the subject entirely (「常在深夜写代码」) over inventing a label.
- Pick one bucket and 1-4 reusable threads. Prefer existing bucket/thread names when provided; converge on the common buckets and only mint a specific new bucket (Mom / 妈妈 / the house) when none fit.
- The bucket name MUST be ONE word in the memory's OWN language: a Chinese memory uses a Chinese bucket (from: """
+ _COMMON_BUCKETS_ZH + """); an English memory uses an English bucket (from: """
+ _COMMON_BUCKETS_EN + """). NEVER write a bilingual slash pair like 「健康/Health」or 「宠物/Pets」, and never let 工作 and Work coexist as two buckets.
- importance means future usefulness for understanding the user. pulse means emotional activation when remembered.
- Do not claim "saved" or "remembered" before the backend write actually succeeds.
""").strip()


MEMORY_WRITE_GUIDANCE_V1 = ("""
Memory write guidance:
- Use memory.add for new durable events/facts.
- Use memory.supersede when the user corrects or replaces an older memory; do not patch old cards in place.
"""
+ MEMORY_WRITE_RULES_V1).strip()


#: 桶名清单按语言取一份。**只取一份是重点，不是省事**：
#:
#: 旧做法是把中英两套桶一起塞进提示词、再叮嘱模型「只准挑一边」。实测约 1/3 的
#: 中文记忆被贴上英文公共桶（用户十年前养过一只狗 → "Pets"），所以才有
#: ``normalize_bucket_language`` 这个代码兜底。根因就是我们给了模型一个它不该做的
#: 选择题 —— 宿主明明已经知道这个花园是什么语言。
#:
#: 现在只递一套：模型没得挑，也就挑不错。兜底仍然保留（模型自造缩写、旧卡回流），
#: 但它从「常态纠错」退回成「异常兜底」。
BUCKET_SETS = {
    "zh-Hans": _COMMON_BUCKETS_ZH,
    "en": _COMMON_BUCKETS_EN,
}


class UnknownBucketLocaleError(ValueError):
    """宿主没说这个花园是什么语言，或者说了一个我们没有桶清单的语言。"""


def bucket_list(locale: str) -> str:
    """这个语言下的通用桶清单（一行、可直接注入提示词）。

    **故意不给默认值。** 默认成中文，等于把 io 的产品分类法写死进通用内核——
    外部使用者会莫名其妙继承一套中文桶，还不知道为什么（这正是边界文档里
    「在 garden 里写死一个宿主专用的值，比放在宿主里更糟」那一条）。
    对 io 自己也一样：漏传的调用点会当场炸出来，而不是安静地发错语言的桶。
    """
    key = str(locale or "").strip()
    if key not in BUCKET_SETS:
        raise UnknownBucketLocaleError(
            f"没有 {locale!r} 的桶清单；可用：{sorted(BUCKET_SETS)}。"
            "调用方必须显式给出这个花园的语言。"
        )
    return BUCKET_SETS[key]


# Full bucket-convergence guidance injected into every card-creating prompt
# (capture / migrate / genesis) so onboarding and capture steer toward the same set.
#: ⏳ **过渡期遗留：同时列出中英两套桶的旧指引。**
#:
#: 只剩两个调用方，都是在 import 时就把提示词拼死的模块级常量，拿不到「这个用户
#: 是什么语言」：``genesis/prompts.py`` 与 ``capabilities/tool_schema.py``。
#: 把它们改成按调用渲染是独立一批活（见 FEATURE_LOG），不该混进这次。
#:
#: 新代码一律用 ``common_buckets_guidance(locale)``。这个常量清零之后就删掉。
COMMON_BUCKETS_GUIDANCE_V1 = (
    "桶名要收敛、可复用,别每张卡都新起一个近义桶。优先从这组通用桶里选并复用——\n"
    "  中文记忆用:" + _COMMON_BUCKETS_ZH + "\n"
    "  英文记忆用:" + _COMMON_BUCKETS_EN + "\n"
    "桶名【只写一个词】、且只用这条记忆本身的语言那一份:中文记忆写「健康」,英文记忆写「Health」。"
    "⚠️ 绝不要把两种语言拼在一起当桶名——别写成「健康/Health」「宠物/Pets」这种带斜杠的双语串。"
    "这些都不贴合,再起一个简短的具体桶(如 妈妈、房子);别造「工作/职业/事业」这种近义重复桶。"
    "\n" + MEMORY_CARD_LENGTH_RULE_V1
).strip()


def common_buckets_guidance(locale: str) -> str:
    """桶名指引。指令是英文（给模型看的），桶名清单是宿主那个语言的那一套。

    ⚠️ 桶名清单本身**不翻译**：花园里已有的桶就是这些字，模型必须原样复用，
    翻译一下就等于新起了一个桶。
    """
    return (
        "Bucket names must converge and be reusable — do not mint a near-synonym "
        "bucket for every card. Pick and reuse from this common set:\n"
        "  " + bucket_list(locale) + "\n"
        "Write the bucket as ONE short name, copied verbatim from the list above — "
        "do not translate it, and never join two languages with a slash "
        "(no 「健康/Health」, no 「宠物/Pets」).\n"
        "If none of them fit, mint one short specific bucket (e.g. 妈妈 / the house). "
        "Do not create near-duplicates of an existing bucket.\n"
        + MEMORY_CARD_LENGTH_RULE_V1
    ).strip()
