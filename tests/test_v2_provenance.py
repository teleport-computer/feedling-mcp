import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import provenance as prov


def test_read_provenance_tags_web_and_task_external():
    assert prov.provenance_for_read("web_search") == prov.EXTERNAL
    assert prov.provenance_for_read("web_fetch") == prov.EXTERNAL
    # A parent cannot prove which external/private reads influenced the child
    # summary, so task provenance is deliberately propagated fail-closed.
    assert prov.provenance_for_read("task") == prov.EXTERNAL
    assert prov.provenance_for_read("memory_index") == prov.INTERNAL


def test_turn_authorization():
    assert prov.turn_has_write_authorization(prov.USER) is True
    assert prov.turn_has_write_authorization(prov.WAKE_TRIGGER) is True
    assert prov.turn_has_write_authorization(prov.EXTERNAL) is False


def test_write_gate_allows_writes_with_authorization():
    allowed, reason = prov.write_gate("memory_write", turn_authorization=True)
    assert allowed is True and reason == ""


def test_write_gate_refuses_writes_without_authorization():
    for w in ("memory_write", "identity_patch", "schedule_wake", "cancel_wake"):
        allowed, reason = prov.write_gate(w, turn_authorization=False)
        assert allowed is False and "authorization" in reason


def test_write_gate_ignores_reads():
    allowed, reason = prov.write_gate("web_search", turn_authorization=False)
    assert allowed is True and reason == ""   # reads are never write-gated
