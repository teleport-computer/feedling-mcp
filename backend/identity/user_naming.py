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


# ---------------------------------------------------------------------------
# 主语位判据:「用户X…」里的 X 是产品词,还是本人做的事?
#
# 演化历史(两轮 review 打出来的,别退回去):
#   v1 谓词白名单 —— 开放集,必漏。线上真实泄漏「用户承诺这周末去看医生」就落榜
#      (中文任何动词都能当谓语)。
#   v2 名词头白名单 —— 方向对了,但一张表不够:「登录/注册/支持/测试/付费/授权」
#      **同时是谓词**,于是「用户登录了新设备」被当成产品词放过(codex round-2)。
#      英文对任意 token 做 s/es 归一更糟 —— tests/supports/services 是第三人称谓词,
#      被误判成产品名词。
#   v3(现在)分三类:只作名词的头一律保留;名词/谓词两用的头看**后一个词**;
#      其余按本人处理。判据全是封闭小表,且两个方向都有回归锁。
# ---------------------------------------------------------------------------

# ① 只可能是产品名词的头 —— 无条件保留。
_NOUN_ONLY_HEADS = (
    "增长率", "增长", "画像", "留存", "满意度", "满意", "粘性", "活跃度", "活跃",
    "流失", "转化率", "转化", "反馈", "需求", "体验", "调研", "访谈", "问卷",
    "数量", "规模", "基数", "数据", "行为", "路径", "漏斗", "分层", "旅程",
    "场景", "故事", "生命周期", "报告", "界面", "接口", "功能", "流程", "账户",
    "账号", "权限", "标签", "属性", "特征", "权益", "消息", "订单", "社区",
    "渠道", "来源", "群体", "客服", "隐私", "协议", "列表", "名单", "中心",
    "平台", "端", "侧", "群", "池", "量", "数", "id", "ID",
    # ⚠️ 刻意**不含**「偏好」:codex 建议收进来,但已落地的
    # tests/test_genesis_worker.py 断言 threads ["用户偏好"] → ["小雨偏好"]
    # (身份卡语境下那就是本人的偏好),且「偏好」本来就在下面的句中谓词锚点表里,
    # 收进来也会被第二遍改掉。两边直接冲突,保留有测试背书的那一侧,
    # 并把这条残留歧义写进 test_known_residual_ambiguity_user_preference。
)

# ② 名词/谓词两用的头 —— 「用户登录了新设备」(谓词,本人)vs
#    「用户登录流程需要优化」(名词,产品)。靠后一个词判。
_AMBIGUOUS_HEADS = (
    "登录", "注册", "支持", "测试", "付费", "授权", "认证", "服务", "管理",
    "引导", "通知", "活动", "内容", "状态", "角色", "教育", "分析", "研究",
    "招募", "运营",
)

# 紧跟在头后面的**体标记/宾语起头** → 这个头在做谓语 → 主语是本人。
_ZH_VERB_MARKERS = (
    "了", "过", "着", "到", "完", "成", "上", "下", "起", "掉",
    "这", "那", "该", "此", "我", "你", "他", "她", "它", "自己", "一", "两",
    "三", "四", "五", "几", "全部", "所有",
)
# 紧跟在头后面的**名词尾** → 头和它组成更长的产品名词。
_ZH_NOUN_TAILS = (
    "流程", "体系", "页面", "入口", "模块", "功能", "权限", "记录", "信息",
    "设置", "中心", "机制", "策略", "方式", "渠道", "链路", "接口", "服务",
    "率", "量", "数", "值", "态", "时长", "占比", "比例", "转化", "漏斗",
)
# 紧跟在头后面的**判断/修饰词** → 头是主语名词短语的一部分。
_ZH_NOUN_MARKERS = (
    "是", "的", "由", "在", "有", "没有", "需要", "要", "会", "也", "都", "很",
    "太", "被", "和", "与", "及", "比", "更", "还", "已经", "尚未", "不",
    "，", ",", "。", "、", "；", ";", "：", ":", "(", "（",
)


def _starts_with_any(text: str, options) -> bool:
    return any(text.startswith(opt) for opt in options)


def _zh_subject_is_product_term(rest: str) -> bool:
    """主语位的「用户X…」是不是产品词。``rest`` 是「用户」之后的全部文本。"""
    if _starts_with_any(rest, _NOUN_ONLY_HEADS):
        return True
    for head in sorted(_AMBIGUOUS_HEADS, key=len, reverse=True):
        if not rest.startswith(head):
            continue
        after = rest[len(head):]
        if _starts_with_any(after, _ZH_VERB_MARKERS):
            return False  # 用户登录了新设备 / 用户支持这个方案 → 本人
        if _starts_with_any(after, _ZH_NOUN_TAILS):
            return True   # 用户登录流程需要优化 → 产品
        if _starts_with_any(after, _ZH_NOUN_MARKERS):
            return True   # 用户认证由 OAuth 提供 / 用户支持很好 → 产品
        # 两用头 + 认不出的后文:默认按谓词(本人)处理。产品用法通常带
        # 「是/的/率/量」这类标记,而漏掉本人主语才是这道闸要防的那个 bug。
        return False
    return False


# 主语位:文本开头,或紧跟句末标点/换行/分号之后。逗号刻意不算 ——
# 「他说,用户投诉了」里的「用户」更可能是本人真的在聊自己的用户。
_ZH_SUBJECT_USER_RE = re.compile(r"(^|[。！？!?；;\n]\s*)用户")
_EN_SUBJECT_USER_RE = re.compile(r"(?i)(^|[.!?;\n]\s+)(?:the\s+)?user\b")

# 英文:只作名词的产品词(复数逐个列 —— **不做** s/es 自动归一,那会把
# tests/supports/services 这些第三人称谓词误判成产品名词,codex round-2)。
_EN_NOUN_ONLY = frozenset({
    "growth", "retention", "research", "feedback", "persona", "personas",
    "journey", "journeys", "experience", "experiences", "base", "count",
    "acquisition", "churn", "segment", "segments", "survey", "surveys",
    "interview", "interviews", "data", "behavior", "behaviour", "funnel",
    "funnels", "analytics", "metrics", "satisfaction", "engagement",
    "activation", "conversion", "lifecycle", "cohort", "cohorts",
    "interface", "interfaces", "account", "accounts", "profile", "profiles",
    "registration", "authentication", "authorization", "permission",
    "permissions", "onboarding", "preference", "preferences", "setting",
    "settings", "feature", "features", "flow", "flows", "path", "paths",
    "story", "stories", "privacy", "consent", "community", "rights",
    "management", "platform", "id", "ids",
})
# 英文两用词:名词或第三人称谓词。"User tests the new feature"(谓词)vs
# "User tests need updating"(名词)—— 靠后一个词是不是限定词/所有格来判。
_EN_AMBIGUOUS = frozenset({
    "test", "tests", "support", "supports", "service", "services",
    "login", "logins", "signup", "signups", "role", "roles",
    "status", "activity", "activities",
})
_EN_OBJECT_STARTERS = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "his", "her",
    "their", "my", "our", "its", "new", "all", "every", "each", "another",
    "some", "it", "them", "him", "us", "me",
})


def _en_subject_is_product_term(rest: str) -> bool:
    words = re.findall(r"[A-Za-z']+", rest[:80])
    if not words:
        return False
    token = words[0].casefold()
    if token in _EN_NOUN_ONLY:
        return True
    if token in _EN_AMBIGUOUS:
        nxt = words[1].casefold() if len(words) > 1 else ""
        # 后面跟宾语起头 → 它是谓词 → 主语是本人
        return nxt not in _EN_OBJECT_STARTERS
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
