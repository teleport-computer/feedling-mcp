"""内核版 prompt 构建不依赖 identity —— 称呼规则由调用方装配后传入。

这是内核纯度的最后一处硬伤：三个 prompt 模块原本
``from identity.user_naming import _naming_rule, sanitize_user_name``。
改法是把装配挪到 io 侧的兼容壳里，内核只收已经算好的字符串。

同时钉死一个容易在重构中丢掉的细节：原实现里
``naming_rule=_naming_rule(user_name)`` 传的是**未 sanitize** 的原值，
而 ``user_name=sanitize_user_name(user_name)`` 传的是 sanitize 后的值。
两者不同源，壳必须照搬，否则行为就不是逐字节一致。
"""
from __future__ import annotations


def test_kernel_capture_prompt_takes_naming_rule_as_param():
    from memgarden.prompts.capture import build_capture_prompt

    text = build_capture_prompt(
        ai_name="io",
        user_name="老王",
        naming_rule="叫他老王。",
        buckets="家庭、工作",
        threads="老婆",
        identity="（暂无）",
        window="用户：今天开了一天会\n我：辛苦了",
        locale="zh-Hans",
    )
    assert "io" in text
    assert "老王" in text
    assert "叫他老王。" in text


def test_kernel_dream_prompt_takes_naming_rule_as_param():
    from memgarden.prompts.dream import build_dream_prompt

    text = build_dream_prompt(
        ai_name="io",
        user_name="老王",
        naming_rule="叫他老王。",
        cards="（暂无）",
        recent_conversations="（暂无）",
    )
    assert "叫他老王。" in text


def test_kernel_does_not_reach_into_the_host_identity_system():
    """内核不认识 io 的身份体系 —— 称呼规则必须由宿主装配好传进去。

    ⚠️ 2026-08-23 起内核是外部包，**读不到它的源文件了**。原来这条扫
    `backend/memgarden/prompts/*.py` 的源码，删掉本地副本之后目录不存在、
    匹配数为 0、测试照样绿 —— 变成了一个空扫描（codex code_review 抓到）。

    改成行为断言：内核不导出任何 identity 相关的东西，而宿主传进去的称呼规则
    确实出现在产出里。源码级的纯度由包自己的 tests/test_purity.py 负责。
    """
    import memgarden.prompts.capture as cap
    import memgarden.prompts.dream as drm

    for mod in (cap, drm):
        leaked = [n for n in dir(mod) if "identity" in n.lower()]
        assert not leaked, f"{mod.__name__} 导出了 identity 相关符号：{leaked}"
        assert "identity" not in getattr(mod, "__dict__", {}), mod.__name__

    from memory.capture_prompt_v1 import build_capture_prompt

    text = build_capture_prompt(
        ai_name="io", user_name="老王", buckets="", threads="",
        identity="认识三个月", window="hi", cards="", locale="zh-Hans",
    )
    assert "老王" in text, "宿主装配的称呼没进到产出里"

def test_compat_shell_preserves_original_naming_semantics():
    """兼容壳必须保留「naming_rule 用原值、user_name 用 sanitize 后的值」。

    用一个会被 sanitize 改写的名字，验证两个位置拿到的确实不同源。
    """
    from identity.user_naming import _naming_rule, sanitize_user_name

    raw = "  老王  "
    sanitized = sanitize_user_name(raw)
    expected_rule = _naming_rule(raw)

    from memory.capture_prompt_v1 import build_capture_prompt

    text = build_capture_prompt(
        ai_name="io",
        user_name=raw,
        buckets="家庭",
        threads="老婆",
        identity="（暂无）",
        window="（空）",
        locale="zh-Hans",
    )
    assert sanitized in text
    assert expected_rule in text
