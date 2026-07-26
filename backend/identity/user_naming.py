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
# 主语位判据:「用户X…」里的「用户」指的是本人,还是产品术语的前缀?
#
# 演化史(三轮 review 打出来的。别再往回加词表 —— 那条路已经证明走不通):
#   v1 谓词白名单 —— 开放集,必漏。线上真实泄漏「用户承诺这周末去看医生」落榜。
#   v2 名词头白名单 —— 「登录/注册/支持/测试」同时是谓词,于是本人主语句被放过。
#   v3 名词/谓词两用词分类 —— 同样的漏洞只是搬到了「只作名词」那张表里
#      (用户反馈了一个问题 / 用户体验了新功能 / User profiles the application)。
#   v4(现在)**不再猜词性**。理由是可以证明的:codex round-2 与 round-3 的要求
#      在词法层互斥 —— 同一个词、同一个位置,两个方向:
#          User profiles the application     要改(谓词)
#          User profile migration starts …   要留(名词短语)  ← 差别在第 3 个 token 的词性
#          用户体验了新功能                   要改
#          用户体验变差了                     要留
#      任何「词表 + 后一词」规则都不可能同时满足这两组。安全边界不该承担一个完整
#      POS parser(这是 codex round-3 的架构结论,我同意)。
#
# 所以 v4 只用**封闭类证据**,没有证据就不动:
#   · 体标记 了/过/着 紧跟在 1-2 字动词后 → 「用户」是这个动词的主语 → 本人;
#     例外:指标名词(增长/留存/转化…)—— 「用户增长了 20%」说的是指标不是人。
#   · 限定词/代词 这/那/我/自己… 起头的宾语 → 同理。
#   · 其余仍走下面那套保守的谓词锚点(codex4 落地的原设计,产品词零风险)。
# 英文刻意**只**保留保守锚点:profiles/accounts/experiences 这类第三人称谓词与
# 名词短语在词法上无法区分(见上面的反例),猜错的代价是改坏本人真实内容。
# 覆盖不到的部分由 ①标签层(根因)和 ②prompt 明令兜,并由
# memory.card_text.count_person_referent_leaks() 量真实残留率 —— 不假装覆盖。
# 已知未覆盖(有意):动词-动词复合「用户付费升级了会员」——体标记不在紧邻位。
# 把窗口放宽到 3-4 字就会打坏「用户满意度提升了」,这是同一枚硬币的两面。
# ---------------------------------------------------------------------------

# 指标名词:跟在「用户」后面 + 体标记时,主语是指标而不是人。
# (「用户增长了20%」「用户留存跌过一次」——这是**唯一**需要词表的地方,
#  而且它只用来**豁免**体标记规则,列漏一个的后果是少改一句,不是改坏一句。)
_METRIC_NOUNS = (
    "增长", "留存", "转化", "流失", "活跃", "粘性", "满意", "满意度", "画像",
    "数量", "规模", "占比", "基数", "复购", "渗透",
)

# 封闭类:体标记(动词后)+ 宾语起头的限定词/代词。
_ZH_ASPECT_MARKERS = "了过着"
_ZH_OBJECT_STARTERS = "这那该此我你他她它一两三几每某"

# 主语位:文本开头,或紧跟句末标点/换行/分号之后。逗号刻意不算 ——
# 「他说,用户投诉了」里的「用户」更可能是本人真的在聊自己的用户。
_ZH_SUBJECT_PREFIX = r"(^|[。！？!?；;\n]\s*)"
# 用户 + 1~2 字动词 + 体标记
_ZH_SUBJECT_ASPECT_RE = re.compile(
    _ZH_SUBJECT_PREFIX + r"用户([\u4e00-\u9fff]{1,2})(?=[" + _ZH_ASPECT_MARKERS + r"])"
)
# 用户 + 1~3 字动词 + 限定词/代词起头的宾语
_ZH_SUBJECT_OBJECT_RE = re.compile(
    _ZH_SUBJECT_PREFIX + r"用户([\u4e00-\u9fff]{1,3})(?=[" + _ZH_OBJECT_STARTERS + r"])"
)


def _rewrite_zh_subject_with_closed_class_evidence(raw: str, zh_referent: str) -> str:
    """只在有封闭类证据时改写主语位的「用户」。没有证据就原样留着。"""
    def _aspect(match: re.Match) -> str:
        verb = match.group(2)
        if verb in _METRIC_NOUNS:
            return match.group(0)  # 用户增长了 20% —— 主语是指标
        return match.group(1) + zh_referent + verb

    def _object(match: re.Match) -> str:
        verb = match.group(2)
        if verb in _METRIC_NOUNS:
            return match.group(0)
        return match.group(1) + zh_referent + verb

    raw = _ZH_SUBJECT_ASPECT_RE.sub(_aspect, raw)
    return _ZH_SUBJECT_OBJECT_RE.sub(_object, raw)


def rewrite_user_reference(text: str, user_name: str, subject: str = "") -> str:
    """Rewrite system-label leaks and user-subject pronouns in visible prose.

    The positive predicate/particle anchors deliberately preserve product terms
    such as ``用户增长`` / ``用户画像`` / ``user growth``.  Prompting is the primary
    guard; this is the deterministic last mile for user-visible memory prose.
    Pronoun rewriting is gated by an explicit ``subject="user"`` so agent and
    relationship prose keeps pronouns that do not refer to the person.  The
    neutral default is intentional for Genesis fact-write output, which no
    longer carries ``about`` and therefore cannot disambiguate ``TA`` safely.

    判据只用**封闭类证据**(体标记/限定词/副词),没有证据就不动 —— 上面那段
    注释里有「为什么不能靠词表猜词性」的反证。所以本函数**必然**有残留,
    那不是 bug:残留由 memory.card_text.count_person_referent_leaks() 计数,
    真正的完整性靠转写标签(transcript_speaker_label)和 prompt 明令。
    """
    raw = str(text or "")
    if not raw:
        return raw
    name = sanitize_user_name(user_name)
    zh_referent = name if name != "TA" else UNKNOWN_PERSON_LABEL
    en_referent = name if name != "TA" else UNKNOWN_PERSON_LABEL_EN
    # 先跑主语位的封闭类证据规则(体标记/限定词),再跑句中的保守谓词锚点。
    raw = _rewrite_zh_subject_with_closed_class_evidence(raw, zh_referent)
    raw = re.sub(
        # 谓词锚点:紧跟在「用户」后面,所以不存在产品复合词误伤。
        # 第二行是 2026-07-26 从线上真实泄漏观察到的那批动词(承诺/答应/报名/…)。
        # 这张表**注定不完整**(中文动词是开放集),它只负责把高频真例收进来;
        # 完整性靠 ①标签层(根因)和 ②prompt 明令,不靠这张表。
        r"用户(?=(?:明确|要求|希望|想要?|喜欢|偏好|说|提到|需要|常常?|总是|通常|曾经?|会|能|愿意|拒绝|认为|觉得|正在|已经|仍然|依然|在|有|没有|是|不是|把|对|来自|住在|工作|习惯|倾向|计划|决定|担心|感到|名?叫|养|写|做|使用|选择|爱|讨厌|擅长|关注"
        r"|承诺|答应|报名|取消|推迟|参加|参与|抱怨|吐槽|强调|补充|澄清|纠正|确认|完成|开始|停止|忘记|记得|尝试|试过|打算|准备|搬到|搬去|遇到|碰到|发现|找到|收到"
        # 时间/程度/语气副词是**封闭类** —— 「用户昨天…」「用户很…」不可能是产品复合词,
        # 所以这批是判据里最硬的证据,不像上面那行动词表那样注定不完整。
        r"|昨天|今天|明天|前天|后天|昨晚|今晚|今早|上周|上个月|这周|这个月|最近|刚才|刚刚|后来|以前|之前|之后|每天|每次"
        r"|终于|居然|竟然|一直|从来|偶尔|很|太|非常|挺|更|又|才|就|也|还"
        r"|的|$|[\s，。！？；;：:、,.!?]))",
        zh_referent,
        raw,
    )
    raw = re.sub(
        # 英文刻意只保留这套保守锚点。profiles/accounts/experiences 这类第三人称
        # 谓词与名词短语在词法上无法区分("User profiles the application" vs
        # "User profile migration starts Monday"),猜错的代价是改坏本人真实内容。
        r"(?i)\b(?:the\s+)?user(?=(?:'s|\s+(?:is|has|was|wants|needs|likes|prefers|often|usually|always|never|can|will|works|writes|feels|said|asked|lives|uses|chooses|plans|decided"
        r"|promised|agreed|signed|cancelled|canceled|moved|started|stopped|forgot|remembered|mentioned|complained|tried|joined|booked)\b))",
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
