"""Batch 3 A4: fact_write 支持可选 terms_note(现有桶/线索快照);默认空 = cloud 输出逐字节等价。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from genesis import prompts


def test_default_output_unchanged():
    base = prompts.fact_write_messages([{"summary": "s"}])
    assert prompts.fact_write_messages([{"summary": "s"}], terms_note="") == base
    assert prompts.fact_write_messages([{"summary": "s"}], terms_note="   ") == base


def test_terms_note_inserted_before_firewall():
    note = "现有的桶:工作 / 协作方式 / IO项目(先复用,别造近义或中英重复桶)"
    msgs = prompts.fact_write_messages([{"summary": "s"}], terms_note=note)
    c = msgs[0]["content"]
    assert note in c
    assert c.index(note) < c.index("防火墙")


def test_terms_note_composes_with_floor_note_and_keep_all():
    terms = "现有的桶:工作"
    floor = "参考下限 15"
    both = prompts.fact_write_messages([{"summary": "s"}], keep_all=True,
                                       floor_note=floor, terms_note=terms)
    c = both[0]["content"]
    assert terms in c and floor in c and "长期档案" in c
    # terms 在 floor 之前;keep_all 后缀仍锚在尾部
    assert c.index(terms) < c.index(floor)

    ka_only = prompts.fact_write_messages([{"summary": "s"}], keep_all=True)
    # strip both insertions -> byte-identical to keep_all-only (same rigor as
    # test_genesis_floor_note.py's _floor_insert invariant, extended to the pair)
    stripped = c.replace(_terms_insert(terms), "").replace(_floor_insert(floor), "")
    assert stripped == ka_only[0]["content"]


def _terms_insert(note: str) -> str:
    """Return the exact text inserted before the firewall marker for terms_note."""
    return (("\n\n★ " + str(note).strip()) if str(note or "").strip() else "")


def _floor_insert(note: str) -> str:
    """Return the exact text inserted before the firewall marker for floor_note."""
    return (("\n\n★ " + str(note).strip()) if str(note or "").strip() else "")
