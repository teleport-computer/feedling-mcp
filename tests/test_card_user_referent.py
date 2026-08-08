"""卡里不许叫本人「用户」—— 三层防线的回归锁。

这个 bug 的历史值得记住:2026-07-17 usr_fee1 投诉后诊断出根因(转写里字面量
``user:`` 教会模型照抄),**只修了 resident**;托管 Runtime V2 自己拼
``f"- {m.get('role')}: …"``,一直漏到 2026-07-26 —— 探针实测 sonnet-4.6 写出
「用户承诺这周末去看医生」。同一条规则两份实现,就一定会漏一份。

所以这里锁三件事:
  ① 标签层:两条运行时**共用**一个 transcript_speaker_label,且它永不吐 "user";
  ② prompt 层:_naming_rule 明令 + 要求产品术语去掉「用户」前缀;
  ③ 残留计数:量出①②到底管不管用 —— 并且**刻意不在写入路径上跑确定性改写**,
     原因见文件末尾那条测试(rewrite_user_reference 在产品语境下会改坏真内容)。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from identity.user_naming import (  # noqa: E402
    _naming_rule,
    transcript_speaker_label,
)
from memory.card_text import count_user_token_residuals  # noqa: E402
from identity.user_naming import rewrite_user_reference  # noqa: E402


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
    assert "对方" in unknown  # 名字未知、性别也判断不出来时的最后一档


def test_naming_rule_allows_evidence_backed_pronouns_when_name_is_unknown():
    """判据 = 「没有任何可靠的称呼/性别依据」,不是「user_name 字段为空」。

    usr_144b(2026-08-09)从没在设置里填过名字,于是每张卡都叫她「对方」——
    包括 capture 当初读着对话写对的那些。字段没填 ≠ 不知道这个人是谁。
    """
    unknown = _naming_rule("")
    assert "就用「他」或「她」" in unknown
    assert "有依据的判断不是猜测" in unknown
    assert "已经写对的「他」/「她」也保留不动" in unknown
    # 「对方」必须排在 他/她 之后,是兜底而不是首选。
    assert unknown.index("就用「他」或「她」") < unknown.index("退到中性的「对方」")
    # 松的只有性别代词这一项,三个系统称谓一个都不许松。
    assert "「TA」" in unknown and "第二人称「你」" in unknown


def test_rewriter_upgrades_pronouns_to_a_known_name_but_never_downgrades_them():
    """确定性层看不见身份卡和对话,没有判断性别的证据。

    所以它只有资格把 他/她 **升级**成已知真名,没有资格把它降级成「对方」。
    「TA」/「你」是另一回事:prompt 明令禁这两个词,任何情况下都要改掉。
    """
    # 名字已知 → 代词上调成真名(这条旧行为保留)。
    assert rewrite_user_reference("她终于去看医生了", "小雨", subject="user") == \
        "小雨终于去看医生了"
    # 名字未知 → 代词原样保留,绝不降级成「对方」。
    assert rewrite_user_reference("她终于去看医生了", "", subject="user") == \
        "她终于去看医生了"
    assert rewrite_user_reference("He finally saw a doctor.", "", subject="user") == \
        "He finally saw a doctor."
    # 「TA」/「你」不受影响 —— 名字未知时照样退到「对方」。
    assert rewrite_user_reference("TA终于去看医生了", "", subject="user") == \
        "对方终于去看医生了"
    assert rewrite_user_reference("你终于去看医生了", "", subject="user") == \
        "对方终于去看医生了"


# --- ③ 残留计数 + 「为什么不在写入路径上跑改写」 ----------------------------

def test_residual_counter_counts_tokens_not_leaks():
    """名字要诚实:它数的是 token 出现次数,里面可能全是正当的产品用法。"""
    assert count_user_token_residuals(
        {"summary": "用户留存这个月掉了", "content": "user growth 也不好看",
         "bucket": "工作", "threads": ["用户增长"]}
    ) == 3  # summary 1 + content "user" 1 + threads 1;bucket 里没有
    assert count_user_token_residuals({"summary": "他终于去看医生了"}) == 0


def test_deterministic_rewriter_is_not_wired_into_the_daily_card_path():
    """capture/dream 的写入路径**不得**调用 rewrite_user_reference。

    2026-07-26 对抗性复核实测:这个改写器现有的锚点在产品语境下是确定性内容
    损坏,而记忆卡的主人自己可能就是 PM/工程师,天天在聊「我们的用户」。
    下面每一条都是实测输出,不是假设。接到每天跑的写入路径上,就是把一个潜伏在
    一次性 import 里的 bug 放大成每人每天。

    它仍留在 genesis / history_import 上(那是既有行为,单独处理),
    但日常写卡这条路只做①标签 + ②prompt + 计数。
    """
    corrupted = {
        # 「的」/标点/空白 锚点 —— 这几个恰恰是「用户」作泛指的标志
        "用户的反馈集中在加载速度上": "小雨的反馈集中在加载速度上",
        "我们这周流失了一批用户。": "我们这周流失了一批小雨。",
        "这个功能面向的是付费用户。": "这个功能面向的是付费小雨。",
        # 产品写作的标准词汇
        "用户说这个价格太贵了": "小雨说这个价格太贵了",
        "用户是我们最重要的资产": "小雨是我们最重要的资产",
        # 修饰语被撕开,同一句里还自相矛盾
        "月活用户在下降": "月活小雨在下降",
        "我们要区分新用户、老用户和回流用户。": "我们要区分新小雨、老用户和回流小雨。",
        # 英文限定词悬空 / 中文名 + 's
        "A user is confused by the empty state.": "A 小雨 is confused by the empty state.",
        "The user's retention dropped 12%.": "小雨's retention dropped 12%.",
    }
    for src, broken in corrupted.items():
        assert rewrite_user_reference(src, "小雨") == broken, (
            f"{src!r} 的改写行为变了 —— 如果这是修好了,可以重新考虑把它接回写入路径,"
            f"但必须先补齐产品语境的成对回归"
        )

    consumer_src = (
        Path(__file__).resolve().parents[1] / "tools" / "chat_resident_consumer.py"
    ).read_text()
    worker_src = (
        Path(__file__).resolve().parents[1]
        / "backend" / "model_api_runtime" / "v2" / "worker.py"
    ).read_text()
    for src, who in ((consumer_src, "resident"), (worker_src, "V2 worker")):
        assert "rewrite_user_reference" not in src, who
        assert "scrub_card_user_references" not in src, who
        assert "count_user_token_residuals" in src, f"{who} 必须仍在量残留"


def test_name_is_used_as_a_regex_replacement_template():
    r"""⚠️ 既有 bug(不是本次引入,但本次实证):名字被当成 re.sub 的替换模板。

    这条现在就活在 genesis / history_import 上。含反斜杠的偏好名直接抛异常;
    名字恰好是 `\g<0>` 则静默变成 no-op —— 正好把这个函数存在的意义抵消掉。
    一行可修(改成 lambda 或转义替换串),但那是 identity 域,单独交接。
    留这条测试是为了让它**可见**,而不是躺在某个人的记忆里。
    """
    import pytest

    with pytest.raises(Exception):
        rewrite_user_reference("用户的反馈很多", "N\\A")
    assert rewrite_user_reference("用户的反馈很多", "\\g<0>") == "用户的反馈很多"


# --- 三条路都必须遵守同一条规则 ---------------------------------------------

def test_all_three_write_paths_carry_the_naming_rule():
    """蒸馏 / 落卡 / 做梦 —— 三条会写出用户可见文字的路都要带称呼规则。

    2026-07-26 Seven 问的正是这个:"早期进来第一次走蒸馏"和"做梦整理旧记忆"
    这两条有没有被约束。规则本身三条都有,漏的是**转写标签**(见下一条)。
    """
    from memory.capture_prompt_v1 import build_capture_prompt
    from memory.dream_prompt_v1 import build_dream_prompt

    capture = build_capture_prompt(ai_name="小柒", user_name="", buckets="",
                                   threads="", identity="", window="- 对方: hi")
    dream = build_dream_prompt(ai_name="小柒", user_name="", cards="", recent_conversations="")
    for prompt, who in ((capture, "capture"), (dream, "dream")):
        assert "「用户」" in prompt or '"用户"' in prompt, who   # 明令禁用
        assert "对方" in prompt, who                            # 无名时的中性主语
    # 做梦还必须被要求**回头修**旧卡里的老写法,否则历史包袱永远不会自愈
    assert "整理旧卡" in dream

    import hosted.history_import as hi
    assert "_naming_rule" in Path(
        Path(__file__).resolve().parents[1] / "backend" / "hosted" / "history_import.py"
    ).read_text()
    assert hi.IMPORT_TRANSCRIPT_PERSON_LABEL  # 蒸馏侧的标签是显式常量


def test_import_transcript_never_labels_the_person_user():
    """蒸馏转写的说话人标签不得是字面量 "User"。

    蒸馏是名字**按定义还不存在**的时刻(名字正是它要推导的东西),所以这里用
    稳定中性标签而不是查名字。它是三条路里最后一条还在喂 "User:" 的
    —— 规则在 prompt 里禁这个词,材料在下面每一行都在说它。
    """
    import hosted.history_import as hi

    line = hi._format_import_message_line(
        {"role": "user", "content": "我昨天加班到十一点", "ts": 0}
    )
    assert line.startswith("The person: "), line
    assert not line.startswith("User"), line
    agent_line = hi._format_import_message_line(
        {"role": "assistant", "content": "别熬了", "ts": 0}
    )
    assert agent_line.startswith("Assistant: "), agent_line
    # 来源家族标签(ChatGPT 导出的 "User profile" 素材)是另一回事,不受影响
    profile = hi._format_import_message_line(
        {"role": "user", "source": hi._USER_PROFILE_SOURCE, "content": "喜欢猫", "ts": 0}
    )
    assert profile.startswith("User profile: "), profile
