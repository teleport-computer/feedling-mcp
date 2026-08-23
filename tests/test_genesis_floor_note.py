"""Batch 2 f(days): fact_write 支持可选 floor_note 尾注;默认空 = cloud 输出逐字节等价。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from genesis import prompts


def test_default_output_unchanged():
    base = prompts.fact_write_messages([{"summary": "s"}])
    with_empty = prompts.fact_write_messages([{"summary": "s"}], floor_note="")
    assert base == with_empty  # 默认/空 note 逐字节等价 → cloud 零变化


def test_floor_note_appended_to_system():
    note = "花园只有 2 张卡,参考下限 38;真实支持的事实尽量都写;绝不编造。"
    msgs = prompts.fact_write_messages([{"summary": "s"}], floor_note=note)
    assert note in msgs[0]["content"]
    # note 在 keep_all 后、STRICT JSON 尾注前
    assert msgs[0]["content"].index(note) < msgs[0]["content"].index("Strict output requirement")


def test_floor_note_composes_with_keep_all():
    note = "参考下限 38"
    ka_only = prompts.fact_write_messages([{"summary": "s"}], keep_all=True)
    both = prompts.fact_write_messages([{"summary": "s"}], keep_all=True, floor_note=note)

    assert "curated for long-term keeping" in both[0]["content"]
    assert note in both[0]["content"]

    # keep_all suffix stays ANCHORED: strip the note insertion → byte-identical to keep_all-only
    floor_insert = _floor_insert(note)
    assert both[0]["content"].replace(floor_insert, "") == ka_only[0]["content"]


def _floor_insert(note: str) -> str:
    """Return the exact text inserted before the firewall marker."""
    return (("\n\n★ " + str(note).strip()) if str(note or "").strip() else "")
