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


def test_speaker_label_falls_back_to_ta_not_user():
    # 名字未知也绝不退回 "user" —— 退回去就等于把禁词喂回给模型
    assert transcript_speaker_label("user", user_name="", ai_name="") == "TA"
    assert transcript_speaker_label("assistant", user_name="", ai_name="") == "我"


def test_reserved_name_cannot_smuggle_itself_into_the_label():
    """有人把「用户」当名字存进来,也不能变成 "用户: …" 那一行(纵深防御)。"""
    assert transcript_speaker_label("user", user_name="用户", ai_name="小柒") == "TA"
    assert transcript_speaker_label("user", user_name="USER", ai_name="小柒") == "TA"


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
