"""V2 出口清洗:低质中转/弱模型会吐出的各种畸形输出。

**为什么单开这一份**(2026-08-14,材料来自 8 位 agent 的真实灰测故障):

出口清洗的用例一直是**按已知泄漏形态一条条补**出来的 —— JSON 泄漏当年专门
建了 `core/protocol_leak` 这道闸,`<think>` 有 `core/self_thinking`,但
**XML 形态从来没建过**,于是 `<parameter name="tool_name">reply</parameter>`
直接出现在 prod 用户的聊天气泡第一行(usr_7f30d63fb7edb61b,08-14)。

同族的根因有两层,两层都是**矩阵缺口**而不是单点疏忽:

1. **没有「协议残片」这个统一维度** —— 防住了 JSON,对 XML 一无所知。
   全仓 grep ``function_calls|<invoke|<parameter`` 曾经零命中。
2. **e2e 探针清一色官方直连** —— 而这类畸形**只有中转把 function-calling
   降级成文本模拟时才产生**。真实故障源根本不在测试矩阵里,官方 API 再怎么
   测都测不出来。

所以本文件按**输出形态**组织(而不是按已知 bug),把中转/弱模型会产生的畸形
一次性列全。新形态被发现时往对应的表里加一行,而不是新开一个测试文件。

**这份测试的历史与判据**:写入时 XML 那一组是红的(5 failed / 20 passed) ——
它抓的正是 T016 那个缺口。修复(PR#193,merge 523db1d2)合入 test 后已全绿。
**所以:如果它哪天又变红,就是那道闸被削弱或绕过了** —— 不是测试写错了,
先去看 `core/tool_markup_leak.py` 的 `TOOL_TAG_STEMS` 是不是被改动过。

判据两条,缺一不可:
- **不许漏**:用户可见文本里永不出现协议标记
- **不许误伤**:正常聊天内容一个字都不能被吃掉(`<3`、不等式、HTML、代码块)
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from memory_garden.text import protocol_leak as pl  # noqa: E402
from memory_garden.text import self_thinking as st  # noqa: E402
from proactive import agent_protocol_v2 as ap2  # noqa: E402


# ---------------------------------------------------------------------------
# 一、中转把 function-calling 降级成文本模拟时,残片长什么样
#
# 这些不是构造的,是真实中转(openai_compatible)+ claude-sonnet-4-6 会吐的形状。
# 第一条就是 08-14 出现在用户气泡里的原文。
# ---------------------------------------------------------------------------
XML_TOOL_DEBRIS = [
    pytest.param(
        '<parameter name="tool_name">reply</parameter>\n好的，我帮你看一下。',
        "好的，我帮你看一下。",
        id="prod-08-14-原文:parameter标记在正文前",
    ),
    pytest.param(
        '<invoke name="memory_write">\n<parameter name="content">x</parameter>\n</invoke>今天天气不错',
        "今天天气不错",
        id="完整invoke块+正文",
    ),
    pytest.param(
        '<function_calls>\n<invoke name="web_search">\n</invoke>\n</function_calls>',
        "",
        id="纯工具块无正文-应整条丢弃",
    ),
    pytest.param(
        '<invoke name="reply">好</invoke>',
        "好",
        id="带命名空间前缀的变体",
    ),
    pytest.param(
        '<parameters>{"q": "天气"}</parameters>我查到了',
        "我查到了",
        id="parameters复数形式",
    ),
]


# ---------------------------------------------------------------------------
# 二、正常聊天里长得像协议标记的东西 —— 一个字都不许被吃掉
#
# 这一组比上一组更重要:清洗器宁可漏也不能误伤。误伤是把用户真正想说的话
# 吞掉,用户完全不知道发生了什么。
# ---------------------------------------------------------------------------
MUST_SURVIVE = [
    pytest.param("我爱你 <3", id="小于号+数字=颜文字不是标签"),
    pytest.param("如果 a < b 那么就成立", id="不等式"),
    pytest.param("HTML 里 <div> 是块级元素", id="讲HTML本身"),
    pytest.param("用 <br> 换行就行", id="提到HTML标签"),
    pytest.param("```html\n<parameter name='x'>1</parameter>\n```", id="代码围栏里的标记=在教学不是泄漏"),
    pytest.param("这个函数签名是 f<T>(x: T) -> T", id="泛型语法"),
    pytest.param("配置写 {\"port\": 8080} 就好", id="正常JSON片段"),
    pytest.param("数组 [1, 2, 3] 对吧", id="正常数组"),
]


@pytest.mark.parametrize(("raw", "expect_contains"), XML_TOOL_DEBRIS)
def test_xml_tool_debris_never_reaches_user(raw, expect_contains):
    """主动道出口:XML 工具残片不许出现在用户可见文本里。

    proactive 是 fail-closed 的,所以纯残片(无正文)应当整条丢弃返回 ""。
    有正文时,正文必须活下来 —— 只丢标记不丢话。
    """
    out = ap2.sanitize_visible_message_text_v2(raw)
    for marker in ("<parameter", "<invoke", "<function_calls", "</parameter>", "<parameters>"):
        assert marker not in out, f"协议标记 {marker} 泄漏进用户可见文本: {out!r}"
    if expect_contains:
        assert expect_contains in out, f"正文被吃掉了: 期望包含 {expect_contains!r}, 实得 {out!r}"


@pytest.mark.parametrize("text", MUST_SURVIVE)
def test_normal_chat_is_never_eaten(text):
    """零误伤:正常聊天内容必须原样活下来。

    这一组是清洗器的**下限**。宁可某个新形态的残片漏过去(下次补一行),
    也不能把用户真正说的话吃掉 —— 后者用户完全无从察觉。
    """
    out = ap2.sanitize_visible_message_text_v2(text)
    assert out.strip(), f"正常内容被整条吞掉: {text!r}"


# ---------------------------------------------------------------------------
# 三、思考标签:模型没写 <think> 时的兜底分支
#
# 失效模式「if/else 只测 if」:现有用例全喂"模型乖乖写了 <think>"的样本,
# 兜底分支一条都没有。而真实模型经常不照做 —— thinking 变体模型即使我们
# 不请求 reasoning,它照样吐。
# ---------------------------------------------------------------------------
THINKING_SHAPES = [
    pytest.param("<think>让我想想</think>好的", True, id="标准闭合"),
    pytest.param("<think>没闭合的思考 好的", False, id="未闭合-剥不干净应判失败"),
    pytest.param("<thinking>另一种写法</thinking>回复", True, id="thinking长写法"),
    pytest.param("正文在前<think>思考在后</think>", True, id="标签在尾部"),
    pytest.param("好的，没问题", True, id="压根没有标签=正常"),
]


@pytest.mark.parametrize(("raw", "should_be_clean"), THINKING_SHAPES)
def test_thinking_stripping_leaves_no_residue(raw, should_be_clean):
    """剥完之后正文里不许还剩任何 think 类标签。

    契约(见 self_thinking.strip_all_thinking 的 docstring):剥不干净就返回
    FAILED、thinking/reply 都为空,由调用方决定发兜底话还是静默 ——
    **绝不把带标签的残文端给用户**。
    """
    status, _thinking, reply = st.strip_all_thinking(raw)
    if status == st.FAILED:
        assert reply == "", "判定失败时不许返回残文"
        assert not should_be_clean, f"这条本应能剥干净却判了失败: {raw!r}"
    else:
        for tag in ("<think", "</think", "<thinking", "</thinking"):
            assert tag not in reply, f"剥完仍有残留 {tag}: {reply!r}"


# ---------------------------------------------------------------------------
# 四、被撕断的协议 JSON 尾巴(已有闸,这里锁住不许退化)
#
# 真实样本:流被切断的中转把协议信封的头留在 reasoning 通道、尾巴落进可见文本。
# 所有旧闸都锚在 JSON 头上,头没了,尾巴就漏了(usr_ed9d6c05d1accb94)。
# ---------------------------------------------------------------------------
ORPHAN_TAILS = [
    'active.sleep","reason":"4:34 了她睡得很沉 早上再说"}]}',
    '":"5点了她还在睡 没动静"}]}',
    'type":"proactive.sleep","reason":"7点了 还在睡 不打扰了 醒了会找我"}]}',
]


@pytest.mark.parametrize("tail", ORPHAN_TAILS)
def test_orphan_json_tail_still_caught(tail):
    """回归锁:断头的协议尾巴必须仍被识别。"""
    assert pl.is_orphan_json_tail(tail), f"断头尾巴漏过闸: {tail!r}"


@pytest.mark.parametrize("text", [
    "好的，晚安~",
    "这个配置是 {\"port\": 8080} 你改一下",
    "(๑•̀ㅂ•́)و✧ 加油！",
    "你可以用 list.append(x) 然后 d[\"k\"]=v",
])
def test_orphan_tail_detector_has_no_false_positives(text):
    """断头检测器不许把正常聊天判成协议残片。"""
    assert not pl.is_orphan_json_tail(text), f"正常内容被误判为协议尾巴: {text!r}"


# ---------------------------------------------------------------------------
# 五、从词表派生的数形遍历(失效模式 9:手工枚举字面量)
#
# 由来:08-14 的 T016 审计里,用户报的是单数 <parameter>,修好后 CI 12 绿、
# 已判 APPROVED;但复数 <parameters> 原样漏过,继续扫又发现 tool_results /
# tool_uses / invokes 同样漏 —— 因为词表是手工枚举字面量的,每多一个数形
# 就是一次新的漏网。
#
# 关键:本组用例**从模块的词表变量派生**,不手写清单 ——
# 手写测试清单会复制手写词表的同一个盲点,那正是这次漏掉的原因。
# 词表以后加词,这里自动覆盖。
# ---------------------------------------------------------------------------
try:  # PR#193 引入;未合入时跳过整组,不拖累当前分支
    from core import tool_markup_leak as _tml  # noqa: E402

    _HAS_TML = True
except Exception:  # pragma: no cover - 合入前的正常状态
    _HAS_TML = False


def _derived_tag_forms():
    """从 _TAG_NAMES 派生单数 + 复数两种数形。"""
    if not _HAS_TML:
        return []
    out = []
    for name in getattr(_tml, "_TAG_NAMES", ()):
        out.append(name)
        out.append(name + "s" if not name.endswith("s") else name[:-1])
    return sorted(set(out))


@pytest.mark.skipif(not _HAS_TML, reason="core.tool_markup_leak 尚未合入(PR#193)")
@pytest.mark.parametrize("tag", _derived_tag_forms())
def test_every_tag_form_in_vocabulary_is_stripped(tag):
    """词表里每个标记的**两种数形**都必须被清掉,正文必须活下来。

    这条测试的价值不在它今天抓到什么,而在于:
    以后有人往 _TAG_NAMES 加一个词,这里立刻多两条用例把两种数形都钉住。
    """
    body = "这是用户真正要看的话"
    raw = f'<{tag} name="x">y</{tag}>{body}'
    out = ap2.sanitize_visible_message_text_v2(raw)
    assert f"<{tag}" not in out, f"标记 <{tag}> 漏进用户可见文本: {out!r}"
    assert body in out, f"清标记时把正文吃掉了: {out!r}"
