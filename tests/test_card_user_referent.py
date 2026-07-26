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
    card = {
        "summary": "用户承诺这周末去看医生",
        "content": "用户昨天连着加班到十一点，事后答应自己这周末一定去看医生。",
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


def test_scrub_catches_the_verb_the_old_anchor_list_missed():
    """线上真实泄漏那句必须被抓到。

    2026-07-26 sonnet-4.6 写的是「用户承诺这周末去看医生」,而当时的谓词锚点表里
    没有「承诺」—— 谓词是**开放集**(中文任何动词都能当谓语),白名单注定漏。
    主语位改成封闭集判据(不是产品词就是本人)之后才抓得住。
    这几个动词都是当时实测落榜的。
    """
    for verb_sentence in (
        "用户承诺这周末去看医生",
        "用户答应了自己要早睡",
        "用户报名了周末的课程",
        "用户取消了周三的会",
        "用户搬到了新的城市",
    ):
        out = scrub_card_user_references(
            {"summary": verb_sentence, "content": verb_sentence}, user_name="小雨"
        )
        assert "用户" not in out["summary"], verb_sentence
        assert out["summary"].startswith("小雨"), verb_sentence


def test_scrub_preserves_product_terms():
    """本人真的在聊「我们的用户」时不能被改掉 —— 这就是不能一刀切拦词的原因。

    (rewrite_user_reference 的预谓词锚点负责这件事;这里锁住它确实接上了。)
    """
    card = {
        "summary": "在做用户增长的复盘",
        "content": "他们这季度盯用户画像和用户留存，说 user growth 的数字不好看。",
        "bucket": "工作",
        "threads": ["用户增长"],
    }
    out = scrub_card_user_references(card, user_name="小雨")
    assert out["summary"] == card["summary"]
    assert out["content"] == card["content"]
    assert out["threads"] == ["用户增长"]


def test_engineering_product_terms_are_not_mistaken_for_the_person():
    """codex 2026-07-26 review P1-2 的复现清单,逐条锁死。

    这些漏掉的代价是**确定性地改坏本人真实内容**(「用户界面需要统一」
    →「小雨界面需要统一」),比残留一次「用户」更严重、也更难发现 ——
    所以封闭集列表宁可长。
    """
    for kept in (
        "用户登录流程需要优化",
        "用户账户体系下周重构",
        "用户界面需要统一",
        "用户认证由 OAuth 提供",
        "用户授权流程太长",
        "用户通知的文案要改",
        "用户订单量掉了",
        "User interface needs work",
        "User profile migration starts Monday",
        "User onboarding conversion dropped",
        "User personas need updating",     # 复数不必逐个列
        "User interviews are scheduled",
        "User stories were rewritten",
    ):
        out = scrub_card_user_references({"summary": kept, "content": kept},
                                        user_name="小雨")
        assert out["summary"] == kept, kept


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
