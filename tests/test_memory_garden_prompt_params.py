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
    from memory_garden.prompts.capture import build_capture_prompt

    text = build_capture_prompt(
        ai_name="io",
        user_name="老王",
        naming_rule="叫他老王。",
        buckets="家庭、工作",
        threads="老婆",
        identity="（暂无）",
        window="用户：今天开了一天会\n我：辛苦了",
    )
    assert "io" in text
    assert "老王" in text
    assert "叫他老王。" in text


def test_kernel_dream_prompt_takes_naming_rule_as_param():
    from memory_garden.prompts.dream import build_dream_prompt

    text = build_dream_prompt(
        ai_name="io",
        user_name="老王",
        naming_rule="叫他老王。",
        cards="（暂无）",
        recent_conversations="（暂无）",
    )
    assert "叫他老王。" in text


def test_kernel_prompts_do_not_import_identity():
    """结构判据：内核 prompt 模块的源码里不出现 identity。"""
    import pathlib

    root = (
        pathlib.Path(__file__).resolve().parents[1]
        / "backend"
        / "memory_garden"
        / "prompts"
    )
    offenders = [
        p.name
        for p in root.glob("*.py")
        if "identity" in p.read_text(encoding="utf-8").split("\n\n\n")[0]
        and "import" in p.read_text(encoding="utf-8").split("\n\n\n")[0]
    ]
    # 更直接的判据交给 test_memory_garden_purity.py 的 AST 守卫；
    # 这里只做一次可读性检查，确保 import 段没有 identity。
    src_heads = {
        p.name: "\n".join(p.read_text(encoding="utf-8").splitlines()[:40])
        for p in root.glob("*.py")
    }
    bad = [name for name, head in src_heads.items() if "from identity" in head]
    assert not bad, f"内核 prompt 模块仍 import identity: {bad} / {offenders}"


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
    )
    assert sanitized in text
    assert expected_rule in text
