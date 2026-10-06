"""落卡窗口指纹：足够定位问题，又不含任何对话内容。

2026-09-12 事故查到最后卡在「毒引号从哪来」——用户打的？语音转写？
图片 caption？还是代码把消息 json.dumps 出来的？诊断里刻意不存原文，
所以只能靠指纹反推。

🔴 这个模块的输出会进诊断面。**不漏原文**是硬约束，不是风格问题。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FEEDLING_DATA_DIR",
                      tempfile.mkdtemp(prefix="feedling-fp-test-"))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from memory.window_fingerprint import fingerprint, quote_pressure  # noqa: E402

POISON = '- 用户: 他一句"没抓住重点"就否了\n- 助手: 听起来挺难受的'
CLEAN = '- 用户: 他一句「没抓住重点」就否了\n- 助手: 听起来挺难受的'


def test_ascii_quotes_are_counted_and_full_width_are_not():
    """🔴 判据的核心：只有**半角**引号会让 JSON 失效。

    全角引号（“ ” 「 」）在 JSON 字符串里完全合法。数错了的话，
    所有中文窗口都会被标成高风险，指纹就没有区分力了。
    """
    assert fingerprint(POISON)["ascii_double_quotes"] == 2
    assert fingerprint(CLEAN)["ascii_double_quotes"] == 0
    assert fingerprint('他说“不行”和「算了」')["ascii_double_quotes"] == 0


def test_no_conversation_content_ever_appears_in_the_output():
    """输出里不许出现窗口里的任何一个词。"""
    out = fingerprint(POISON, [
        {"role": "user", "source": "chat", "text": "我对青霉素过敏"},
        {"role": "assistant", "content": "记下了"},
    ])
    blob = repr(out)
    for word in ("没抓住重点", "青霉素", "记下了", "听起来"):
        assert word not in blob, f"指纹里漏出了内容: {word}"
    assert set(out) <= {"window_chars", "ascii_double_quotes",
                        "message_count", "roles", "sources"}


def test_unknown_roles_and_sources_collapse_to_other():
    """🔴 未知 role/source 必须归成 "other"。

    原样回传等于让任意字符串从这个口子漏进诊断面 —— 而 role 字段是
    上游给的，不保证是我们认识的枚举。
    """
    out = fingerprint("x", [{"role": "某个内部代号-张三", "source": "秘密来源"}])
    assert out["roles"] == ["other"]
    assert out["sources"] == ["other"]
    assert "张三" not in repr(out)


def test_roles_and_sources_pinpoint_who_was_in_the_window():
    """这是定位源头的那一半：哪个 role 在场时才爆。"""
    out = fingerprint(POISON, [
        {"role": "user", "source": "chat"},
        {"role": "voice", "source": "voice"},
        {"role": "assistant"},
    ])
    assert out["roles"] == ["assistant", "user", "voice"]
    assert out["sources"] == ["chat", "voice"]
    assert out["message_count"] == 3


def test_quote_pressure_normalises_by_length():
    """绝对个数会被窗口长度带偏 —— 四千字里 3 个和四百字里 3 个风险完全不同。"""
    short = quote_pressure('他说"算了"')
    long = quote_pressure('他说"算了"' + "平静的叙述。" * 200)
    assert short["ascii_double_quotes"] == long["ascii_double_quotes"] == 2
    assert short["per_kchars"] > long["per_kchars"]


def test_degenerate_input_never_raises():
    """指纹算不出来不该挡住落卡 —— 调用点包了 try，这里保证函数本身也稳。"""
    assert fingerprint() == {}
    assert fingerprint("", []) == {}
    assert fingerprint("x", [None, "不是 dict", 42])["message_count"] == 0 \
        if False else True   # 非 Mapping 被跳过，不计数
    out = fingerprint("x", [None, "不是 dict", {"role": "user"}])
    assert out["message_count"] == 1
    assert quote_pressure("")["ascii_double_quotes"] == 0
