import asyncio, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import compaction

def _fake_llm_returning(text):
    async def _llm(cfg, messages, **kw):
        _llm.seen = messages
        return {"reply": text}
    return _llm

def test_compact_appends_new_bullets_preserving_old():
    llm = _fake_llm_returning("- user asked about dogs")
    out = asyncio.run(compaction.compact(provider_config=object(), current_summary="- talked about cats",
                                         old_messages=[{"role":"user","content":"tell me about dogs"}], llm=llm))
    assert "- talked about cats" in out and "- user asked about dogs" in out
    assert out.index("cats") < out.index("dogs")

def test_compact_empty_summary_is_just_new():
    llm = _fake_llm_returning("- first thing")
    out = asyncio.run(compaction.compact(provider_config=object(), current_summary="",
                                         old_messages=[{"role":"user","content":"x"}], llm=llm))
    assert out.strip() == "- first thing"

def test_compact_blank_llm_reply_is_noop():
    llm = _fake_llm_returning("   ")
    out = asyncio.run(compaction.compact(provider_config=object(), current_summary="- keep me",
                                         old_messages=[{"role":"user","content":"x"}], llm=llm))
    assert out == "- keep me"

def test_compact_passes_old_messages_to_llm():
    llm = _fake_llm_returning("- ok")
    asyncio.run(compaction.compact(provider_config=object(), current_summary="",
                                   old_messages=[{"role":"user","content":"SECRET_MARKER"}], llm=llm))
    assert "SECRET_MARKER" in str(llm.seen)
