"""记忆卡内容闸 —— 「弱模型抄了模板占位符」的回归锁。

现场(2026-07-26,usr_ed21…,openai_compatible + minimax-M3):记忆花园里出现两类
垃圾卡,两类都能通过当时的解析(JSON 合法、字段非空),被原样封信封写进花园:
  A. 标题很长、正文只有 `...`   —— 叙述塞进 summary,示例里的 `...` 抄进 content;
  B. `[thickened summary]` / `[thickened content …]` —— 方括号占位,一个字没填。
(那两串方括号文本不在本仓任何 prompt 里 —— 是模型自己编的骨架,所以判据必须是
「长什么样」,不能是「等于我们某个已知模板串」。)

这里锁三件事:①这两类必须被拦;②真卡不能被误伤;③打回=严格模式报
`invalid_card_content:*`,放宽模式(第二问)只丢脏行、留干净的。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from memory.capture_prompt_v1 import (  # noqa: E402
    build_capture_retry_prompt,
    parse_capture_cards,
)
from memory.card_text import (  # noqa: E402
    card_text_rejection,
    format_error,
    is_card_format_error,
    placeholder_reason,
    sanitize_card_labels,
    substantive_len,
)
from memory.dream_prompt_v1 import (  # noqa: E402
    build_dream_prompt,
    build_dream_retry_prompt,
    parse_dream_consolidations,
)


# --- 判据本身 ---------------------------------------------------------------

def test_placeholder_shapes_are_rejected():
    for text in (
        "",
        "   ",
        "...",
        "…",
        "。",
        "— —",
        "[thickened summary]",
        "[thickened content combining work info + incident narrative]",
        "【正文】",
        "<summary>",
        "{content}",
        "TBD",
        "n/a",
        "占位",
        "一段厚的正文",
    ):
        assert placeholder_reason(text) is not None, repr(text)


def test_real_text_is_not_rejected():
    for text in (
        "他终于答应去看医生了",
        "开了一整天会，心率飙到 120，我催他休息他嫌烦",
        "She switched to oat milk in March.",
        "（他自己说的：这次真的会早睡）",  # 圆括号是合法写法,不能当占位符
        "Blue Bottle",
        "对话里提到 2026 年的计划……后面再补",  # 有实质内容,尾部省略号不算占位
    ):
        assert placeholder_reason(text) is None, repr(text)


def test_substantive_count_is_language_neutral():
    """判「有没有字」必须语种中立。

    早先那版用字符区间白名单(ASCII/CJK/假名/谚文),阿拉伯文、西里尔文、希伯来文
    实义字数一律算 0 → 整语种的卡全部被判「只有标点」而打回。这是对非拉丁非 CJK
    用户的静默封杀,codex 07-26 review P1-1 抓到。
    """
    for text in ("مرحبا", "Привет", "שלום", "Καλημέρα", "café résumé", "Ngày mai"):
        assert substantive_len(text) >= 2, repr(text)
        assert placeholder_reason(text) is None, repr(text)
    # 反面:标点/省略号/emoji 仍然不算字
    for text in ("...", "…", "。。。", "！？", "🙂🙂"):
        assert substantive_len(text) == 0, repr(text)


def test_card_accepts_non_latin_content():
    assert card_text_rejection(summary="Привет", content="Он наконец пошёл к врачу.") is None
    assert card_text_rejection(summary="مرحبا", content="ذهب إلى الطبيب أخيرا.") is None


def test_known_conservative_rejections_are_a_deliberate_tradeoff():
    """记下这道闸偏保守的已知代价,别让它以后被当成 bug 悄悄改掉。

    prompt 明确要求 summary 是一句话、content 是一段正文,所以整段就是
    `[laughs]` 这类舞台提示、或只有单个字符,判「没写」是可接受的取舍
    (codex review A)。真要放开必须是产品决定,不是顺手改判据。
    """
    assert placeholder_reason("[laughs]") == "bracket_placeholder"
    assert card_text_rejection(summary="A", content="I.") == "summary_too_short"
    # 但夹在句子里的方括号不受影响 —— 只有「整段就是一个方括号」才算占位
    assert placeholder_reason("他说「[原话保留]我不去」，然后挂了电话") is None


def test_card_needs_both_summary_and_content():
    # 旧判据是「两个都空才拦」,于是「有标题、没正文」一路写进花园 —— 用户看到的第 1 类垃圾卡。
    assert card_text_rejection(summary="她今天讲了很多关于工作的事", content="...") == (
        "content_no_substantive_chars"
    )
    assert card_text_rejection(summary="", content="正文写满了") == "summary_empty"
    assert card_text_rejection(summary="正常标题", content="正常的一段正文") is None


def test_soft_labels_are_sanitized_not_fatal():
    bucket, threads, reasons = sanitize_card_labels(
        bucket="...", threads=["...", "工作", "", "[thread]"]
    )
    assert bucket == "" and threads == ["工作"]
    assert reasons  # 记录洗掉了什么(只做观测)


def test_soft_field_placeholder_alone_never_bounces():
    """硬内容正常、只是 bucket/threads 是占位词 → 清洗后照收,不为它多烧一次调用。

    (第一版把软字段 reason 也并进打回条件,codex review P1-2。)
    """
    raw = (
        '{"cards":[{"action":"add","type":"event","bucket":"无","threads":["none","加班"],'
        '"summary":"他连着加了三天班","content":"他说项目要上线，连着三天到十一点才走。"}]}'
    )
    cards, err = parse_capture_cards(raw)
    assert err is None and len(cards) == 1
    assert cards[0]["bucket"] == "" and cards[0]["threads"] == ["加班"]


def test_format_error_and_predicate():
    err = format_error(["summary_empty", "summary_empty", "content_too_short"])
    assert err == "invalid_card_content:summary_empty,content_too_short"
    assert is_card_format_error(err)
    after = format_error(["summary_empty"], after_retry=True)
    assert after == "invalid_card_content_after_retry:summary_empty"
    assert not is_card_format_error(after)  # 终局,不再打回
    # 这些各有自己的失败路径,重问同一段 prompt 没有意义
    for other in (None, "", "no_json_object", "empty_reply", "provider_call_failed:TimeoutError"):
        assert not is_card_format_error(other)


# --- 现场两张卡的回归 --------------------------------------------------------

_CARD_A = (  # 长标题 + 正文只有省略号
    '{"consolidations":[{"op":"thicken","card_ids":["m_1"],'
    '"rationale":"同一张卡补充事实","result":'
    '{"bucket":"工作","threads":["加班"],'
    '"summary":"她提到公司最近在裁员，她的组也受到影响，情绪很低落，还说到了房贷",'
    '"content":"...","importance":0.8,"pulse":0.5}}]}'
)
_CARD_B = (  # 方括号占位,一个字没填
    '{"consolidations":[{"op":"merge","card_ids":["m_2","m_3"],'
    '"rationale":"同一张卡补充事实","result":'
    '{"bucket":"...","threads":["...","..."],"summary":"[thickened summary]",'
    '"content":"[thickened content combining work info + incident narrative]"}}]}'
)


def test_dream_bounces_the_two_real_garbage_cards():
    for raw in (_CARD_A, _CARD_B):
        cons, _qs, err = parse_dream_consolidations(raw)
        assert cons == [], "垃圾卡不能落库"
        assert is_card_format_error(err), err


def test_dream_relaxed_pass_drops_dirty_keeps_clean():
    raw = (
        '{"consolidations":['
        '{"op":"merge","card_ids":["m_2"],"rationale":"同一张卡补充事实",'
        '"result":{"summary":"[thickened summary]",'
        '"content":"[…]"}},'
        '{"op":"thicken","card_ids":["m_9"],"rationale":"同一张卡补充事实",'
        '"result":{"bucket":"工作",'
        '"threads":["裁员"],"summary":"她的组在裁员名单上",'
        '"content":"她说组里已经走了三个人，她担心下一个是自己。"}}]}'
    )
    # 第一问:有脏行 → 整份打回
    cons, _qs, err = parse_dream_consolidations(raw)
    assert cons == [] and is_card_format_error(err)
    # 第二问:放宽 —— 干净的那张照收,不让一行占位符把整晚整理清零
    cons, _qs, err = parse_dream_consolidations(raw, strict=False)
    assert err is None and len(cons) == 1
    assert cons[0]["card_ids"] == ["m_9"]


def test_capture_bounces_placeholder_card():
    raw = (
        '{"cards":[{"action":"add","type":"event","bucket":"...",'
        '"threads":["...","..."],"summary":"...","content":"..."}]}'
    )
    cards, err = parse_capture_cards(raw)
    assert cards == [] and is_card_format_error(err)
    # 第二问还是全脏 → 必须报 after_retry 让 job 失败,不能伪装成「没什么值得记」
    cards, err = parse_capture_cards(raw, strict=False)
    assert cards == []
    assert err == "invalid_card_content_after_retry:summary_no_substantive_chars"
    assert not is_card_format_error(err)  # 不能再打回一次 —— 那是死循环


def test_all_dirty_after_retry_never_looks_like_a_clean_noop():
    """第二问全脏 ≠ 成功空手。

    报成 ([], None) 会让 resident 的 job 以 noop 完成、V2 还会推进 capture
    frontier —— 这段对话/这批卡就永久丢了,而且 data-track 上完全看不见
    (codex review P1-3)。真正的空结果必须是「模型没给脏行」。
    """
    all_dirty = (
        '{"consolidations":[{"op":"merge","card_ids":["m_1"],'
        '"rationale":"同一张卡补充事实",'
        '"result":{"summary":"...","content":"..."}}]}'
    )
    cons, _qs, err = parse_dream_consolidations(all_dirty, strict=False)
    assert cons == [] and err.startswith("invalid_card_content_after_retry:")

    # 对照:模型真的选择了空结果(prompt 给的正当出路)→ 干净的 noop
    genuinely_empty = '{"consolidations": [], "questions_to_ask": []}'
    cons, _qs, err = parse_dream_consolidations(genuinely_empty, strict=False)
    assert cons == [] and err is None


def test_clean_reply_never_bounces():
    raw = (
        '{"cards":[{"action":"add","type":"event","bucket":"工作",'
        '"threads":["裁员","焦虑"],"summary":"她担心自己在裁员名单上",'
        '"content":"她说组里已经走了三个人，晚上睡不着，反复算房贷还能撑几个月。",'
        '"importance":0.8,"pulse":0.6}]}'
    )
    cards, err = parse_capture_cards(raw)
    assert err is None and len(cards) == 1 and cards[0]["bucket"] == "工作"


# --- 打回的提问长什么样 ------------------------------------------------------

def test_retry_prompt_keeps_context_and_names_the_problem():
    prompt = build_dream_prompt(
        ai_name="小柒", user_name="Seven",
        cards="[m_1] 工作: 她在加班", recent_conversations="[user] 今天又加班",
    )
    retry = build_dream_retry_prompt(prompt, "invalid_card_content:content_no_substantive_chars")
    # 模型这一轮是无状态的:上下文必须再给一次,否则它只能凭空编
    assert prompt in retry
    assert "[m_1] 工作: 她在加班" in retry
    assert "content 只有省略号" in retry
    assert '{"consolidations": [], "questions_to_ask": []}' in retry


def test_capture_retry_prompt_offers_the_empty_answer():
    retry = build_capture_retry_prompt("原始 prompt", "invalid_card_content:summary_empty")
    assert '{"cards": []}' in retry  # 空结果永远是可接受的出路,好过再交一份占位符
    assert "summary 是空的" in retry


# --- 模型原始输出泄漏(2026-07-28 加):harmony 标记 / 报错回显 / 撕裂尾巴 -------

def test_hard_field_protocol_leak_rejects_card():
    # summary/content 混进协议残片 → 整卡打回(走同一个 invalid_card_content 重问路)。
    # 硬字段从严:强证据(通道前缀+route)或 ≥2 弱证据共现。
    assert card_text_rejection(
        summary="analysis to=functions.memory_write", content="正常的一段正文内容"
    ) == "summary_protocol_leak"
    assert card_text_rejection(
        summary="用户今天很开心", content='analysis to=functions.memory_write 乱码 output error code: 400'
    ) == "content_protocol_leak"


def test_hard_field_single_weak_not_rejected():
    # 单个弱证据(开发者贴 JSON 尾巴 / 字面提 to=functions)→ 不误杀整卡(codex code_review)。
    assert card_text_rejection(
        summary="用户在调 parser", content='他说要保留 to=functions.memory_write 这个串'
    ) is None


def test_hard_field_guard_off_bypasses():
    # kill switch:guard=False 时不跑泄漏检测(只留原有占位符判据)。
    assert card_text_rejection(
        summary="analysis to=functions.memory_write", content="正常的一段正文内容", guard=False
    ) is None


def test_soft_bucket_protocol_leak_is_cleaned_not_rejected():
    bucket, threads, reasons = sanitize_card_labels(
        bucket="relationship'}]}analysis to=functions.memory_write output error code: 400", threads=[]
    )
    assert bucket == ""                       # 脏桶就地清空(降级默认桶由调用层做)
    assert "bucket_protocol_leak" in reasons


def test_soft_bucket_known_taxonomy_residue_cleaned():
    bucket, _t, reasons = sanitize_card_labels(bucket="long_term_preference_or_event_v1", threads=[])
    assert bucket == ""
    assert "bucket_protocol_leak" in reasons


def test_soft_threads_drop_dirty_keep_clean():
    _b, threads, reasons = sanitize_card_labels(
        bucket="工作", threads=["家庭", "to=functions.memory_write", "健康"]
    )
    assert threads == ["家庭", "健康"]          # 脏条目丢、干净条目留
    assert "threads_protocol_leak" in reasons


def test_legit_bucket_not_cleaned():
    # 合法自定义桶(含 snake_case)不能被误清。
    for b in ("妈妈", "health_diet_log_2026", "project_feedling_v1"):
        bucket, _t, reasons = sanitize_card_labels(bucket=b, threads=[])
        assert bucket == b, b
        assert not reasons, b


# ---------------------------------------------------------------------------
# 墓碑短语闸(2026-08-05 usr_a40e 事故:「已被 <卡id> 取代——原文」写进可见字段)
# ---------------------------------------------------------------------------


def test_tombstone_marker_rejects_hard_fields():
    err = card_text_rejection(
        summary="已被 c42ebb9618ae447df9d52107ea15de85 取代——绿豆汤偏好",
        content="正常正文。",
    )
    assert err == "summary_tombstone_marker"
    err = card_text_rejection(
        summary="正常标题",
        content="superseded by 1a1f94f9fdc9ec86 —— old friendship note.",
    )
    assert err == "content_tombstone_marker"


def test_tombstone_marker_requires_hex_id_not_bare_prose():
    # 正常散文里的「取代」不误伤:短语必须跟着 ≥8 位 hex 才算。
    assert card_text_rejection(
        summary="换了新手机", content="旧手机已被新手机取代,数据迁移顺利。"
    ) is None
    assert card_text_rejection(
        summary="团队调整", content="他的职位已被同事接替,他觉得释然。"
    ) is None


def test_tombstone_marker_cleaned_from_soft_fields():
    bucket, threads, reasons = sanitize_card_labels(
        bucket="已被 c42ebb9618ae447d 取代",
        threads=["饮食", "已被 c42ebb9618ae447d 取代——旧线索"],
    )
    assert bucket == ""
    assert threads == ["饮食"]
    assert "bucket_tombstone_marker" in reasons
    assert "threads_tombstone_marker" in reasons
