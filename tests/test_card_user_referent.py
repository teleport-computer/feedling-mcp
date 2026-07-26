"""卡里不许叫本人「用户」—— 三层防线的回归锁。

这个 bug 的历史值得记住:2026-07-17 usr_fee1 投诉后诊断出根因(转写里字面量
``user:`` 教会模型照抄),**只修了 resident**;托管 Runtime V2 自己拼
``f"- {m.get('role')}: …"``,一直漏到 2026-07-26 —— 探针实测 sonnet-4.6 写出
「用户承诺这周末去看医生」。同一条规则两份实现,就一定会漏一份。

所以这里锁三件事:
  ① 标签层:两条运行时**共用**一个 transcript_speaker_label,且它永不吐 "user";
  ② prompt 层:_naming_rule 明令(已有,这里只做在位断言);
  ③ 写入层:scrub_* 的确定性改写接在 capture/dream 上,且不误伤产品词。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity.user_naming import (  # noqa: E402
    _naming_rule,
    transcript_speaker_label,
)
from memory.card_text import (  # noqa: E402
    count_user_token_residuals,
    scrub_card_user_references,
    scrub_dream_consolidations,
)


# --- ① 标签层 --------------------------------------------------------------

def test_speaker_label_never_emits_a_system_role():
    for user_name in ("小雨", "Seven", "", "   ", "用户", "user", "TA"):
        label = transcript_speaker_label("user", user_name=user_name, ai_name="小柒")
        assert label.casefold() not in {"user", "用户"}, (user_name, label)


def test_speaker_label_uses_the_real_name_when_known():
    assert transcript_speaker_label("user", user_name="小雨", ai_name="小柒") == "小雨"
    assert transcript_speaker_label("assistant", user_name="小雨", ai_name="小柒") == "小柒"


def test_speaker_label_falls_back_to_neutral_not_user_and_not_ta():
    """名字未知时退到中性的「对方」。

    既不能退回 "user"(那是原 bug),也**不能**退回内部标记「TA」——
    `_naming_rule` 明令禁止模型用「TA」指代本人,转写里连着二十行 "TA:"
    就是把同一个标签教学问题换了个词(codex 2026-07-26 review P1-1)。
    """
    assert transcript_speaker_label("user", user_name="", ai_name="") == "对方"
    assert transcript_speaker_label("assistant", user_name="", ai_name="") == "我"
    for label in (transcript_speaker_label("user", user_name=n, ai_name="")
                  for n in ("", "   ", "TA", "ta")):
        assert label == "对方"


def test_reserved_name_cannot_smuggle_itself_into_the_label():
    """有人把「用户」当名字存进来,也不能变成 "用户: …" 那一行(纵深防御)。"""
    assert transcript_speaker_label("user", user_name="用户", ai_name="小柒") == "对方"
    assert transcript_speaker_label("user", user_name="USER", ai_name="小柒") == "对方"


def test_v2_capture_window_has_no_literal_user_label():
    """托管路径的窗口渲染必须走共用标签 —— 这是 2026-07-26 的漏点本体。

    直接对源码断言:那一行**不能**再把原始 role 插进转写文本。
    """
    worker_src = (
        Path(__file__).resolve().parents[1]
        / "backend" / "model_api_runtime" / "v2" / "worker.py"
    ).read_text()
    assert 'f"- {m.get(\'role\')}: ' not in worker_src, (
        "V2 又把原始 role 插回转写了 —— 那正是教模型写「用户」的元凶"
    )
    assert "transcript_speaker_label(" in worker_src


# --- ② prompt 层(在位断言)-------------------------------------------------

def test_naming_rule_forbids_the_system_labels():
    known = _naming_rule("小雨")
    unknown = _naming_rule("")
    for rule in (known, unknown):
        assert "用户" in rule and "user" in rule  # 明确点名禁用
    assert "小雨" in known
    assert "对方" in unknown  # 名字未知时的中性替代


# --- ③ 写入层 --------------------------------------------------------------

def test_scrub_replaces_the_system_label_with_the_name():
    """线上真实泄漏那张卡(2026-07-26 sonnet-4.6)。"""
    card = {
        "summary": "用户承诺这周末去看医生",
        "content": "用户答应了自己，这周末一定去看医生。",
        "bucket": "健康",
        "threads": ["加班", "看医生"],
    }
    out = scrub_card_user_references(card, user_name="小雨")
    assert "用户" not in out["summary"] and "小雨承诺" in out["summary"]
    assert "用户" not in out["content"]
    assert card["summary"].startswith("用户")  # 原对象不被就地改写


def test_scrub_uses_neutral_referent_when_name_unknown():
    out = scrub_card_user_references(
        {"summary": "用户希望被提醒吃药", "content": "用户说总忘。"}, user_name=""
    )
    assert "用户" not in out["summary"] and "对方希望" in out["summary"]
    assert "用户" not in out["content"]


def _scrubbed(text: str, user_name: str = "小雨") -> str:
    return scrub_card_user_references(
        {"summary": text, "content": text}, user_name=user_name
    )["summary"]


# 确定性改写这一层**只**做「紧邻高置信个人谓词」这一件小事。
# 两个方向必须成对锁:同一批规则修好一侧就会打坏另一侧,四轮 review 每轮都是
# 这么破的(v1 漏本人主语 → v2/v3 改坏产品词 → v4 又改坏产品词)。
_PERSON_SUBJECT_SENTENCES = (
    # 线上真实泄漏那句 + 同义谓词(2026-07-26 sonnet-4.6 实测)
    "用户承诺这周末去看医生",
    "用户答应了自己要早睡",
    "用户报名了周末的课程",
    "用户搬到了新的城市",
    # codex4 原本就落地的锚点集合
    "用户希望被提醒吃药",
    "用户喜欢燕麦奶",
    "用户的猫叫豆豆",
    "User promised to see a doctor.",
    "User prefers oat milk.",
)
_PRODUCT_SENTENCES = (
    # codex round-4 的复现清单:限定词/副词**不是** product-safe 证据 ——
    # 「用户」和标记之间夹的那几个字本身就可以是产品名词。
    "用户界面这一版需要重做",
    "用户账户这个模块要迁移",
    "用户界面我觉得太复杂",
    "用户最近流失很多",
    "用户最近活跃度下降了",
    "用户昨天的留存率下降了",
    # round-2/3 的清单
    "用户登录流程需要优化",
    "用户注册转化率掉了",
    "用户支持很好",
    "用户测试的结论是什么",
    "用户认证由 OAuth 提供",
    "用户体验变差了",
    "用户调研安排在周四",
    "用户界面需要统一",
    "用户增长了20%",
    "用户满意度和用户增长是研究主题",
    "用户数还在涨",
    "用户画像做得不细",
    # 这批曾被我收进锚点,产品语境同样自然,已撤出
    "用户开始流失",
    "用户忘记密码怎么办",
    "用户完成了注册",
    "用户取消了订阅",
    "User interface needs work",
    "User profile migration starts Monday",
    "User onboarding conversion dropped",
    "User personas need updating",
    "User tests need updating",
    "User profiles the application",
)


def test_person_subject_sentences_are_rewritten():
    """紧邻高置信个人谓词 —— 这是确定性层唯一负责的事。"""
    for sentence in _PERSON_SUBJECT_SENTENCES:
        out = _scrubbed(sentence)
        assert out.startswith("小雨"), (sentence, out)


def test_product_usage_is_preserved():
    """产品语境一律原样保留。

    和上面**成对**:改坏本人真实内容比残留一个系统词严重得多,也更难发现。
    四轮 review 里 codex 每轮都从这一侧找到破绽,所以这张表只会变长不会变短。
    """
    for sentence in _PRODUCT_SENTENCES:
        assert _scrubbed(sentence) == sentence, sentence


def test_deliberately_uncovered_is_documented_and_counted_not_silent():
    """有意不覆盖的残留:摆出来 + 数出来,不假装覆盖。

    可以证明这些无法用词法收敛 —— 同一个词、同一个位置,两个方向都自然:
        用户体验了新功能   本人试用 / 泛用户试用
        用户反馈了一个问题 本人反馈 / 用户们反馈
        User profiles the application / User profile migration starts Monday
    所以确定性层不碰它们。兜底靠 ①转写标签(根因,不再把 "user:" 喂给模型)
    和 ②prompt 明令(产品术语去掉「用户」前缀),效果由残留计数验证。
    """
    for uncovered in (
        "用户体验了新功能",
        "用户反馈了一个问题",
        "用户调研了三家竞品",
        "用户登录了新设备",
        "用户很焦虑",
        "User profiles the application",
        "User accounts for half the traffic",
    ):
        assert _scrubbed(uncovered) == uncovered, (
            f"{uncovered!r} 被改写了 —— 若确认要扩大覆盖,"
            f"必须同时确认 _PRODUCT_SENTENCES 全绿"
        )
        assert count_user_token_residuals({"summary": uncovered}) >= 1, uncovered


def test_residual_counter_counts_tokens_not_leaks():
    """名字要诚实:它数的是 token 出现次数,里面可能全是正当的产品用法。"""
    assert count_user_token_residuals(
        {"summary": "用户留存这个月掉了", "content": "user growth 也不好看",
         "bucket": "工作", "threads": ["用户增长"]}
    ) == 3  # summary 1 + content "user" 1 + threads 1;bucket 里没有
    assert count_user_token_residuals({"summary": "他终于去看医生了"}) == 0


def test_known_residual_ambiguity_user_preference():
    """「用户偏好」是这套判据留下的已知歧义,行为由既有测试定,不是漏改。

    codex 建议把「偏好」收进产品词封闭集,但 tests/test_genesis_worker.py 已断言
    threads ["用户偏好"] → ["小雨偏好"](身份卡语境下那就是本人的偏好),而且
    「偏好」本来就在句中谓词锚点表里 —— 收进封闭集也会被第二遍改掉。
    两边直接冲突,保留有测试背书的那一侧,把歧义摆在这里而不是藏起来。
    """
    out = scrub_card_user_references(
        {"summary": "用户偏好决定推荐结果", "content": "用户偏好决定推荐结果"},
        user_name="小雨",
    )
    assert out["summary"] == "小雨偏好决定推荐结果"


def test_product_terms_survive_even_in_subject_position():
    """主语位的新规则不能吃掉「用户满意度是研究主题」这类真产品词。

    封闭集白名单(名词头)负责这件事 —— 它列错一个的代价是改坏本人真实内容,
    所以这条锁死几个高频头。history_import 已有的用例也在守同一条线。
    """
    for kept in (
        "用户满意度和用户增长是研究主题",
        "用户画像做得不细",
        "用户留存这个月掉了",
        "用户数还在涨",
        "用户调研安排在周四",
    ):
        out = scrub_card_user_references({"summary": kept, "content": kept},
                                        user_name="小雨")
        assert out["summary"] == kept, kept


def test_scrub_leaves_ai_referring_pronouns_alone():
    """卡里指代 AI 的「TA」是本人视角对伴侣的叫法,必须原样保留。

    所以 scrub 刻意用中性 subject —— 只处理明确的系统标签泄漏,不动代词。
    """
    card = {"summary": "TA 陪我熬过了那一晚", "content": "你说会一直在。"}
    out = scrub_card_user_references(card, user_name="小雨")
    assert out["summary"] == card["summary"]
    assert out["content"] == card["content"]


def test_scrub_dream_reaches_the_inner_result():
    rows = [
        {"op": "thicken", "card_ids": ["m_1"],
         "result": {"summary": "用户在加班", "content": "用户说组里走了三个人。",
                    "bucket": "工作", "threads": ["加班"]}},
        {"op": "merge", "card_ids": ["m_2"], "result": {}},
        "not-a-dict",
    ]
    out = scrub_dream_consolidations(rows, user_name="小雨")
    assert "用户" not in out[0]["result"]["summary"]
    assert "小雨" in out[0]["result"]["summary"]
    assert out[0]["card_ids"] == ["m_1"] and out[0]["op"] == "thicken"
    assert out[1]["result"] == {}          # 空 result 不炸
    assert out[2] == "not-a-dict"          # 非 dict 原样透传
    assert rows[0]["result"]["summary"].startswith("用户")  # 不就地改写


def test_scrub_is_wired_into_both_write_paths():
    """判据存在不等于接上了 —— rewrite_user_reference 就是「存在但只接了
    genesis/import」才让日常 capture/dream 漏了这么久。"""
    consumer_src = (
        Path(__file__).resolve().parents[1] / "tools" / "chat_resident_consumer.py"
    ).read_text()
    worker_src = (
        Path(__file__).resolve().parents[1]
        / "backend" / "model_api_runtime" / "v2" / "worker.py"
    ).read_text()
    for src, who in ((consumer_src, "resident"), (worker_src, "V2 worker")):
        assert "scrub_card_user_references(" in src, who
        assert "scrub_dream_consolidations(" in src, who
