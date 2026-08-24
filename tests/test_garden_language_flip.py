"""花园语言判定 —— 守的是「记忆不会自己换语言」。

## 两次教训，都写在这里

**第一次（2026-08-24 线上事故）**：判据看的是已有桶名里 CJK 与拉丁**字符**的个数。
中英文桶名长度根本不对等（「工作」2 字符 vs「Our relationship」15 字符），一个英文
桶顶七个中文桶。真实用户的中文花园两天内整个翻成英文。

**第二次（同日，hx 指出）**：改成「按桶投票」只是提高了门槛，没解决根本问题 ——

    工作、健康、James、Sarah、Mike        ← 中文用户,给三个朋友各建了个桶
    工作、James、OpenAI、GitHub、Figma    ← 中文用户,在记几个项目

人名、公司名、项目名全是拉丁字母，跟这个人说什么语言毫无关系。**怎么数都救不了 ——
压根不该数它。**

所以现在桶名完全不参与判定。这个文件的大半篇幅在守这一条。
"""
from __future__ import annotations

import pytest

from chat.reply_language import (
    garden_language_decision,
    infer_garden_language,
    user_written_text,
)

ZH_SAMPLE = "今天面试挂了，算法题第三次没做出来，感觉自己不太适合这行，有点想放弃了"
EN_SAMPLE = (
    "Bombed the interview again today, third time stuck on the same algorithm "
    "problem. Starting to wonder whether this field is really for me."
)


# ---------------------------------------------------------------- 桶名不是证据

@pytest.mark.parametrize(
    "buckets,desc",
    [
        ("工作、健康、James、Sarah、Mike", "🔴 hx 的例子：给三个外国朋友各建一个桶"),
        ("工作、James、OpenAI、GitHub、Figma", "🔴 公司名 / 项目名"),
        ("我们的关系、工作、健康、Our relationship、Feelings & comfort、GitHub",
         "🔴 2026-08-24 事故当天的真实桶构成"),
        ("Work、Health、Pets、Family、Friends", "全英文桶，但这个人说中文"),
    ],
)
def test_bucket_names_can_never_flip_a_chinese_speakers_garden(buckets: str, desc: str) -> None:
    """一个说中文的人，桶名长什么样都不该让他的记忆变成英文。"""
    got = infer_garden_language({}, written=ZH_SAMPLE, existing_buckets=buckets)
    assert got == "zh-Hans", desc


@pytest.mark.parametrize(
    "buckets,desc",
    [
        ("工作、健康、宠物、家庭", "全中文桶，但这个人说英文"),
        ("妈妈、房子、崽崽", "自定义中文桶"),
    ],
)
def test_bucket_names_can_never_flip_an_english_speakers_garden(buckets: str, desc: str) -> None:
    """反方向同样成立。只守一边的话，把判据写死成「永远中文」也能全绿。"""
    got = infer_garden_language({}, written=EN_SAMPLE, existing_buckets=buckets)
    assert got == "en", desc


def test_buckets_do_not_appear_in_the_basis() -> None:
    """依据名里不该再出现 existing_buckets —— 它已经不是一档证据了。

    这条守的是「有人把桶名悄悄加回判据」：行为上可能一时看不出来（碰巧两边指向
    同一种语言），但依据名会立刻暴露。
    """
    d = garden_language_decision({}, written=ZH_SAMPLE, existing_buckets="Work、Health、GitHub")
    assert d["basis"] != "existing_buckets"
    assert d["basis"] == "writing_language"
    # 桶的计数仍然返回，但只作观测 —— 用来发现「判成中文却几乎全是拉丁桶」这类
    # 归一化失效的情况。
    assert d["bucket_en"] == 3


# ---------------------------------------------------------------- 证据的优先级

def test_explicit_preference_beats_everything() -> None:
    """用户明说要中文，就是中文 —— 哪怕他最近在写英文邮件、设备是英文的。

    这是他自己的设置，不是我们可以推翻的推断。
    """
    got = infer_garden_language(
        {"language_preference": "zh-Hans"},
        written=EN_SAMPLE, locale="en", existing_buckets="Work、Health",
    )
    assert got == "zh-Hans"


def test_what_they_write_beats_device_locale() -> None:
    """设备 locale 可能只是买了台水货手机。他实际在写什么才是真信号。"""
    d = garden_language_decision({}, written=ZH_SAMPLE, locale="en")
    assert d["locale"] == "zh-Hans"
    assert d["basis"] == "writing_language"


def test_chinese_speaker_who_peppers_in_english_tech_words_stays_chinese() -> None:
    """中文用户天天夹英文技术词。**按词计不按字符计** —— 一个英文词≈5 个字母，
    比字符数中文永远吃亏，这跟桶名那次是同一个错。"""
    written = (
        "今天 review 了一天的 PR，眼睛快瞎了。那个 data pipeline 的重构改了 4000 多行，"
        "team 里只有我碰过那块老代码，manager 说 sprint 排不下"
    )
    assert infer_garden_language({}, written=written) == "zh-Hans"


def test_a_short_utterance_is_not_enough_to_move_a_garden() -> None:
    """「ok thanks」不该把一个花园推向英文。证据不够就往下走。"""
    d = garden_language_decision({}, written="ok thanks", locale="zh-Hans")
    assert d["locale"] == "zh-Hans"
    assert d["basis"] == "client_locale"


def test_no_evidence_at_all_falls_back_and_says_so() -> None:
    """什么都没有时落到默认，并且**说得出这是默认值** —— 好让宿主知道这个结论有多弱。"""
    d = garden_language_decision(None)
    assert d["basis"] == "default"


# ---------------------------------------------------------------- 取证只取本人

def test_only_the_persons_own_messages_count_as_evidence() -> None:
    """AI 的回复不算证据。

    AI 的回复本来就是用花园语言写的 —— 拿它当证据，就是「上一轮输出决定下一轮
    输入」那个环，只是从桶名换成了消息体重演一遍。
    """
    messages = [
        {"role": "user", "content": ZH_SAMPLE},
        {"role": "assistant", "content": EN_SAMPLE * 3},
        {"role": "user", "content": "而且最近睡不好，一到周二晚上就开始胃疼"},
    ]
    written = user_written_text(messages)
    assert EN_SAMPLE[:20] not in written
    assert infer_garden_language({}, written=written) == "zh-Hans"


def test_lookalike_roles_do_not_count_as_the_person() -> None:
    """role 判断要精确匹配 —— "user_proxy" 之类不是本人。"""
    written = user_written_text([{"role": "user_proxy", "content": EN_SAMPLE}])
    assert written == ""
