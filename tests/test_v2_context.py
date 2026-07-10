import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import context

def test_build_turn_messages_orders_persona_summary_tail():
    tail = [
        {"id":"1","ts":1.0,"role":"user","content":"hi"},
        {"id":"2","ts":2.0,"role":"openclaw","content":"hello"},
        {"id":"3","ts":3.0,"role":"user","content":"how are you"},
    ]
    msgs = context.build_turn_messages(system_prompt="SYS", summary="- talked about cats", tail=tail)
    assert msgs[0] == {"role":"system","content":"SYS"}
    assert msgs[1]["role"] == "system" and "talked about cats" in msgs[1]["content"]
    assert [m["role"] for m in msgs[2:]] == ["user","assistant","user"]
    assert msgs[-1]["content"] == "how are you"

def test_build_turn_messages_no_summary_skips_summary_block():
    msgs = context.build_turn_messages(system_prompt="SYS", summary="", tail=[{"id":"1","ts":1.0,"role":"user","content":"hi"}])
    assert [m["role"] for m in msgs] == ["system","user"]

def test_build_turn_messages_appends_action_context_last():
    msgs = context.build_turn_messages(system_prompt="S", summary="", tail=[{"id":"1","ts":1.0,"role":"user","content":"q"}], action_context="TOOLS: x")
    assert msgs[-1] == {"role":"system","content":"TOOLS: x"}

def test_build_turn_messages_drops_blank_tail_entries():
    tail=[{"id":"1","ts":1.0,"role":"user","content":"  "},{"id":"2","ts":2.0,"role":"user","content":"real"}]
    msgs = context.build_turn_messages(system_prompt="S", summary="", tail=tail)
    assert [m["content"] for m in msgs if m["role"]!="system"] == ["real"]

def test_needs_compaction_counts_nonblank():
    tail = [{"content":"a"}]*21
    assert context.needs_compaction(tail, budget=20) is True
    assert context.needs_compaction([{"content":"a"}]*20, budget=20) is False
    assert context.needs_compaction([{"content":"  "}]*30, budget=20) is False


def test_text_of_handles_str_list_and_none():
    assert context.text_of("  hi  ") == "hi"
    assert context.text_of(None) == ""
    assert context.text_of([
        {"type": "text", "text": "look at this"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ]) == "look at this"
    # image-only block list has no text
    assert context.text_of([
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ]) == ""


def test_build_turn_messages_passes_image_blocks_through_verbatim():
    blocks = [
        {"type": "text", "text": "这个报告哪里有问题"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
    ]
    msgs = context.build_turn_messages(
        system_prompt="sys", summary="", tail=[{"role": "user", "content": blocks}])
    assert msgs[-1]["content"] is blocks       # verbatim, not stringified
    assert msgs[-1]["role"] == "user"


def test_build_turn_messages_keeps_an_image_only_message():
    """A caption-less image must NOT be dropped — it is the entire user turn."""
    blocks = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}}]
    msgs = context.build_turn_messages(
        system_prompt="sys", summary="", tail=[{"role": "user", "content": blocks}])
    assert msgs[-1]["content"] is blocks


def test_build_turn_messages_still_drops_empty_text_rows():
    msgs = context.build_turn_messages(
        system_prompt="sys", summary="", tail=[{"role": "user", "content": "   "}])
    assert [m["role"] for m in msgs] == ["system"]


def test_needs_compaction_counts_image_rows():
    tail = [{"role": "user", "content": [{"type": "image_url",
                                          "image_url": {"url": "data:image/jpeg;base64,A"}}]}]
    assert context.needs_compaction(tail, budget=0) is True
