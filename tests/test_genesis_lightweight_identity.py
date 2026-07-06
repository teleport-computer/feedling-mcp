from genesis import lightweight_identity as li


def test_lightweight_pulls_explicit_name_from_character_card():
    text = "# 阿樟 · 角色卡\n- 名字：阿樟\n- 性格：温柔但毒舌"
    p = li.derive_from_support([text], days_with_user=10, language="zh")
    assert p["agent_name"] == "阿樟"
    assert li.has_signal(p) is True


def test_lightweight_no_signal_when_no_name_no_dims():
    p = li.derive_from_support(["随便一段没有名字的话"], days_with_user=0, language="zh")
    assert li.has_signal(p) is False
