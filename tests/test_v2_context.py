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
