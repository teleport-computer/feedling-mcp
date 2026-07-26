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
    名字未知时退到内部标记 ``TA``,也绝不退回 ``user``。
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


# 「用户X」是产品词而不是指代本人的**封闭集**:这一层白名单必须列名词头,不能列动词。
# 理由(2026-07-26 实测):谓词锚点是开放集 —— 中文任何动词都能跟在主语后面,
# 所以线上真实泄漏的「用户承诺这周末去看医生」正好落榜(承诺/答应/报名 都不在表里)。
# 名词头是有限且稳定的(这个领域就是增长/画像/留存/满意度那几个),漏一个的代价也小,
# 所以主语位上改成「除非是产品词,否则就是本人」——把白名单从开放集换成封闭集。
_PRODUCT_TERM_HEADS = (
    # 指标 / 研究
    "增长", "增长率", "画像", "留存", "满意度", "满意", "粘性", "活跃", "活跃度",
    "流失", "转化", "转化率", "反馈", "需求", "体验", "调研", "访谈", "问卷",
    "数量", "规模", "基数", "数据", "行为", "路径", "漏斗", "分层", "旅程",
    "场景", "故事", "运营", "生命周期", "研究", "分析", "报告", "招募", "测试",
    # 产品 / 工程(codex 2026-07-26 review P1-2 的复现清单:这些漏掉会把本人
    # 真实内容改坏,比残留一次「用户」更严重 —— 「用户界面」→「小雨界面」)
    "登录", "注册", "账户", "账号", "界面", "接口", "功能", "流程", "认证",
    "授权", "权限", "标签", "属性", "特征", "权益", "状态", "内容", "消息",
    "通知", "活动", "付费", "订单", "服务", "支持", "社区", "渠道", "来源",
    "群体", "客服", "引导", "隐私", "协议", "教育", "中心", "列表", "名单",
    "角色", "管理", "平台", "端", "侧", "群", "池", "量", "数", "id", "ID",
    # ⚠️ 刻意**不含**「偏好」:codex 建议把它收进来,但已落地的
    # tests/test_genesis_worker.py 断言 threads ["用户偏好"] → ["小雨偏好"]
    # (身份卡语境下「用户偏好」就是本人的偏好),而且「偏好」本来就在下面的
    # 句中谓词锚点表里 —— 收进来也会被第二遍改掉。两边直接冲突,保留有测试
    # 背书的那一侧,并把这条残留歧义记在测试里。
)
_PRODUCT_TERM_HEADS_EN = (
    "growth", "retention", "research", "feedback", "persona", "journey",
    "experience", "base", "count", "acquisition", "churn", "segment",
    "survey", "interview", "data", "behavior", "behaviour", "funnel",
    "analytics", "metrics", "satisfaction", "engagement", "activation",
    "conversion", "lifecycle", "cohort",
    # 同上,codex 的复现清单
    "interface", "account", "profile", "login", "signup", "registration",
    "authentication", "authorization", "permission", "onboarding",
    "preference", "setting", "feature", "flow", "path", "story", "test",
    "privacy", "consent", "support", "service", "community", "rights",
    "activity", "status", "role", "management", "platform", "id",
)

# 主语位:文本开头,或紧跟句末标点/换行/分号之后。
_ZH_SUBJECT_USER_RE = re.compile(r"(^|[。！？!?；;\n]\s*)用户")
_EN_SUBJECT_USER_RE = re.compile(r"(?i)(^|[.!?;\n]\s+)(?:the\s+)?user\b")


def _zh_subject_is_product_term(rest: str) -> bool:
    return any(rest.startswith(head) for head in _PRODUCT_TERM_HEADS)


_PRODUCT_TERM_HEADS_EN_SET = frozenset(h.casefold() for h in _PRODUCT_TERM_HEADS_EN)


def _en_subject_is_product_term(rest: str) -> bool:
    word = re.match(r"\s*([A-Za-z]+)", rest)
    if not word:
        return False
    token = word.group(1).casefold()
    if token in _PRODUCT_TERM_HEADS_EN_SET:
        return True
    # 复数不必逐个列("personas"/"interviews"/"settings"/"stories")
    for suffix, base in (("ies", "y"), ("es", ""), ("s", "")):
        if token.endswith(suffix):
            candidate = token[: -len(suffix)] + base
            if candidate in _PRODUCT_TERM_HEADS_EN_SET:
                return True
    return False


def _rewrite_subject_position(raw: str, zh_referent: str, en_referent: str) -> str:
    """主语位上的「用户」按「不是产品词就是本人」处理。

    只管主语位,句中出现的仍交给下面那套保守的谓词锚点 —— 句中的「用户」更可能
    是本人真的在聊自己的用户(「他说用户投诉了」),不该被动。
    """
    def _zh(match: re.Match) -> str:
        rest = raw[match.end():]
        if _zh_subject_is_product_term(rest):
            return match.group(0)
        return match.group(1) + zh_referent

    def _en(match: re.Match) -> str:
        rest = raw[match.end():]
        if _en_subject_is_product_term(rest):
            return match.group(0)
        return match.group(1) + en_referent

    raw = _ZH_SUBJECT_USER_RE.sub(_zh, raw)
    return _EN_SUBJECT_USER_RE.sub(_en, raw)


def rewrite_user_reference(text: str, user_name: str, subject: str = "") -> str:
    """Rewrite system-label leaks and user-subject pronouns in visible prose.

    The positive predicate/particle anchors deliberately preserve product terms
    such as ``用户增长`` / ``用户画像`` / ``user growth``.  Prompting is the primary
    guard; this is the deterministic last mile for user-visible memory prose.
    Pronoun rewriting is gated by an explicit ``subject="user"`` so agent and
    relationship prose keeps pronouns that do not refer to the person.  The
    neutral default is intentional for Genesis fact-write output, which no
    longer carries ``about`` and therefore cannot disambiguate ``TA`` safely.
    """
    raw = str(text or "")
    if not raw:
        return raw
    name = sanitize_user_name(user_name)
    zh_referent = name if name != "TA" else "对方"
    en_referent = name if name != "TA" else "The person"
    # 先跑主语位的封闭集规则(catches「用户承诺…」),再跑句中的保守谓词锚点。
    raw = _rewrite_subject_position(raw, zh_referent, en_referent)
    raw = re.sub(
        r"用户(?=(?:明确|要求|希望|想要?|喜欢|偏好|说|提到|需要|常常?|总是|通常|曾经?|会|能|愿意|拒绝|认为|觉得|正在|已经|仍然|依然|在|有|没有|是|不是|把|对|来自|住在|工作|习惯|倾向|计划|决定|担心|感到|名?叫|养|写|做|使用|选择|爱|讨厌|擅长|关注|的|$|[\s，。！？；;：:、,.!?]))",
        zh_referent,
        raw,
    )
    raw = re.sub(
        r"(?i)\b(?:the\s+)?user(?=(?:'s|\s+(?:is|has|was|wants|needs|likes|prefers|often|usually|always|never|can|will|works|writes|feels|said|asked|lives|uses|chooses|plans|decided)\b))",
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
