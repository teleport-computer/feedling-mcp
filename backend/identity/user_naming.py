"""Shared rules for referring to the person in user-visible memory text.

This module is deliberately pure stdlib.  Identity validation, hosted import,
and the standalone resident consumer all need the exact same placeholder and
fallback semantics.
"""
from __future__ import annotations

import re


_RESERVED_USER_NAMES = frozenset({"ta", "user", "用户"})

# 名字未知时,**模型可见文本**里对本人的中性称呼。内部未知标记仍是 "TA"
# (sanitize_user_name 的返回),但「TA」不能出现在模型看得见的地方 ——
# _naming_rule 正是禁止模型这么叫本人的。
UNKNOWN_PERSON_LABEL = "对方"
UNKNOWN_PERSON_LABEL_EN = "The person"


def sanitize_user_name(user_name: str) -> str:
    """Return a real preferred name, or the internal ``TA`` unknown marker."""
    name = " ".join(str(user_name or "").split())
    name = name.strip(" `\"'“”‘’「」『』。，,.;；:：!！?？")
    if not name or name.casefold() in _RESERVED_USER_NAMES:
        return "TA"
    return name


def _naming_rule(user_name: str) -> str:
    """Render the canonical rule for user-visible memory prose."""
    name = sanitize_user_name(user_name)
    if name != "TA":
        return (
            f"提到 {name} 就用「{name}」这个名字。"
            "不要用「用户」/\"user\"、指代本人的「TA」、猜测性别的他/她，"
            "也不要用第二人称「你」来指代本人。"
        )
    return (
        "如果材料里明确出现了本人希望被称呼的名字，就用那个名字；"
        "否则优先省略主语（例如「常在深夜写代码，累了会突然沉默」），"
        "确实需要主语时只用中性的「对方」。"
        "不要用「用户」/\"user\"、指代本人的「TA」、猜测性别的他/她，"
        "也不要用第二人称「你」来指代本人。"
    )


def transcript_speaker_label(role: str, *, user_name: str, ai_name: str = "") -> str:
    """转写里一行的说话人标签。**永远不要**用原始 role 值。

    字面量 ``user:`` 前缀正是教会 capture 模型往用户可见的卡里写「用户」的元凶
    (usr_fee1 投诉,2026-07-17)—— 模型照抄转写里对说话人的称呼。resident 当时
    修了,托管 Runtime V2 一直漏着(``worker.py`` 的 capture/dream 窗口直接插
    ``m.get('role')``),2026-07-26 sonnet-4.6 写出「用户承诺这周末去看医生」
    就是这么来的:prompt 在上面禁这个词,转写在下面把人叫了二十遍 ``user``。

    所以这里是两条运行时**唯一**的标签实现 —— 别再各写一份,那正是它漏掉的原因。
    名字未知时退到中性的「对方」(UNKNOWN_PERSON_LABEL),既不退回 ``user``,
    也不用内部标记 ``TA``——prompt 正是禁止模型这么叫本人的。
    """
    if str(role or "").strip().lower() == "user":
        # 再 sanitize 一次(纵深防御):把 用户/user 当"名字"传进来,
        # 也不能变成 "用户: …" 这一行。
        name = sanitize_user_name(user_name)
        # 名字未知时不能退回内部标记「TA」:_naming_rule 明令禁止模型用「TA」
        # 指代本人,转写里连着出现二十行 "TA:" 就是把 "user:" 的教学问题原样
        # 换了个词(codex 2026-07-26 review P1-1)。退到中性的「对方」。
        return UNKNOWN_PERSON_LABEL if name == "TA" else name
    return " ".join(str(ai_name or "").split()) or "我"


# ---------------------------------------------------------------------------
# 为什么这一层只剩「紧邻谓词锚点」,不再有任何主语位规则
#
# 四轮 review 的收敛过程(别再往回加规则,每一版都被真例打穿过):
#   v1 谓词白名单        —— 开放集,漏掉线上真例「用户承诺这周末去看医生」。
#   v2 名词头白名单      —— 「登录/注册/支持/测试」同时是谓词,本人主语句被放过。
#   v3 名词/谓词两用分类 —— 漏洞只是搬进了「只作名词」那张表。
#   v4 封闭类证据        —— 前提「产品复合词不可能接限定词/副词」直接不成立:
#         用户界面这一版需要重做   →  小雨界面这一版…    (限定词)
#         用户最近流失很多         →  小雨最近流失很多    (时间副词)
#      因为「用户」和标记之间夹的那 1-3 个字**本身就可以是产品名词**。
#   v5(现在)只保留**紧邻**谓词锚点:锚点必须直接贴着「用户」,中间不许夹任何字,
#      所以结构上不可能命中「用户+产品名词+…」。
#
# 更根本的一点:词法层根本无法判定。同一个词、同一个位置,两个方向都自然:
#      User profiles the application / User profile migration starts Monday
#      用户体验了新功能(本人试用)   / 用户体验了新功能(泛用户试用)
# 安全边界不该承担一个 POS parser。真正的完整性靠:
#   ① transcript_speaker_label —— 根因,不再把 "user:" 喂给模型;
#   ② prompt 明令 —— 产品术语去掉「用户」前缀,只有模型知道自己想说哪个意思;
#   ③ memory.card_text.count_user_token_residuals() —— 把残留量出来,不假装覆盖。
# 这一层只做「已观察到的、高置信的个人谓词」这一件小事。
# ---------------------------------------------------------------------------


def rewrite_user_reference(text: str, user_name: str, subject: str = "") -> str:
    """Rewrite system-label leaks and user-subject pronouns in visible prose.

    The positive predicate/particle anchors deliberately preserve product terms
    such as ``用户增长`` / ``用户画像`` / ``user growth``.  Prompting is the primary
    guard; this is the deterministic last mile for user-visible memory prose.
    Pronoun rewriting is gated by an explicit ``subject="user"`` so agent and
    relationship prose keeps pronouns that do not refer to the person.  The
    neutral default is intentional for Genesis fact-write output, which no
    longer carries ``about`` and therefore cannot disambiguate ``TA`` safely.

    锚点必须**紧贴**「用户」——中间不许夹字,所以结构上不可能命中
    「用户+产品名词+…」。所有主语位规则都已删除(v4 被真例打穿:
    「用户界面这一版…」「用户最近流失很多」)。因此本函数**必然**有残留,
    那不是 bug:残留由 memory.card_text.count_user_token_residuals() 计数,
    真正的完整性靠转写标签(transcript_speaker_label)和 prompt 明令。
    """
    raw = str(text or "")
    if not raw:
        return raw
    name = sanitize_user_name(user_name)
    zh_referent = name if name != "TA" else UNKNOWN_PERSON_LABEL
    en_referent = name if name != "TA" else UNKNOWN_PERSON_LABEL_EN
    raw = re.sub(
        # 紧邻谓词锚点。第一行是 codex4 原本落地的集合;第二行是 2026-07-26
        # 线上真实泄漏观察到的那批(「用户承诺这周末去看医生」及其同义词)。
        # 收得很紧 —— 只留个人承诺/迁居这类产品语境几乎不会这么说的谓词。
        # 刻意**没有**收进来的(产品语境同样自然,收进来会改坏真内容):
        #   完成/开始/停止/发现/找到/收到/参加/取消/推迟/确认/尝试/忘记/记得
        #   ——「用户开始流失」「用户忘记密码」「用户完成了注册」都是正常产品句。
        # 这张表注定不完整(中文动词是开放集),完整性不靠它。
        r"用户(?=(?:明确|要求|希望|想要?|喜欢|偏好|说|提到|需要|常常?|总是|通常|曾经?|会|能|愿意|拒绝|认为|觉得|正在|已经|仍然|依然|在|有|没有|是|不是|把|对|来自|住在|工作|习惯|倾向|计划|决定|担心|感到|名?叫|养|写|做|使用|选择|爱|讨厌|擅长|关注"
        r"|承诺|答应|报名|搬到|搬去|吐槽"
        r"|的|$|[\s，。！？；;：:、,.!?]))",
        zh_referent,
        raw,
    )
    raw = re.sub(
        # 英文同样只保留紧邻锚点。profiles/accounts/experiences 这类第三人称
        # 谓词与名词短语在词法上无法区分("User profiles the application" vs
        # "User profile migration starts Monday"),猜错的代价是改坏本人真实内容。
        r"(?i)\b(?:the\s+)?user(?=(?:'s|\s+(?:is|has|was|wants|needs|likes|prefers|often|usually|always|never|can|will|works|writes|feels|said|asked|lives|uses|chooses|plans|decided"
        r"|promised|agreed|moved|remembered|complained)\b))",
        en_referent,
        raw,
    )
    if str(subject or "") == "user":
        raw = re.sub(
            r"(^|[。！？.!?]\s*)(?:TA|你|他|她)(?=[\u4e00-\u9fff])",
            lambda match: match.group(1) + zh_referent,
            raw,
        )
        raw = re.sub(
            r"(?i)(^|[.!?]\s+)(?:you|he|she)\b",
            lambda match: match.group(1) + en_referent,
            raw,
        )
    return raw
