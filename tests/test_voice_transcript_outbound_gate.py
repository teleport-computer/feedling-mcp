"""读完通话逐字记录之后,这一轮不能再有出站通道。

通话记录是用户说过的原话 —— 私密程度不低于记忆卡,而记忆读取工具早就在
`_PRIVATE_READ_TOOLS` 里。少登记一个工具的后果不是报错,是**模型读完私密内容
后仍然握着 web_search / web_fetch / MCP**,可以把原话原样带出去。

`voice_transcript_list` 只返回时间/时长/轮数,不含任何说过的话,所以不在集合里。
"""
from model_api_runtime.v2 import worker


def test_transcript_read_blocks_outbound_tools_for_the_rest_of_the_turn():
    assert "voice_transcript_read" in worker._PRIVATE_READ_TOOLS, (
        "voice_transcript_read 返回逐字原话,必须和 memory_fetch 同级:"
        "读过之后本轮出站工具要消失。"
    )


def test_transcript_list_is_metadata_only_and_not_gated():
    assert "voice_transcript_list" not in worker._PRIVATE_READ_TOOLS, (
        "list 只有元信息,把它也阻断会白白掐掉正常的「查一下有哪些通话再决定读哪通」"
    )


def test_every_content_bearing_read_is_gated():
    """防漂移:以后再加返回用户内容的读工具,这里会提醒你想一想。"""
    from capabilities import registry

    content_bearing = {
        name for name in registry.READ_ACTIONS
        if name.startswith(("memory_", "voice_transcript_", "workspace_"))
        and not name.endswith("_list")
        and name != "memory_organize"
    }
    missing = sorted(content_bearing - worker._PRIVATE_READ_TOOLS)
    assert not missing, (
        f"这些读工具会返回用户内容,却不在出站阻断集合里:{missing}\n"
        "要么加进 _PRIVATE_READ_TOOLS,要么说明它为什么不含用户内容。"
    )
