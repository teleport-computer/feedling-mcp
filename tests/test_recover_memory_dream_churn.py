import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parent.parent / "tools" / "recover_memory_dream_churn.py"
SPEC = importlib.util.spec_from_file_location("recover_memory_dream_churn", MODULE_PATH)
recovery = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(recovery)


def test_recovery_restores_originals_and_soft_retires_dream_cards():
    docs = [
        {
            "id": "original-capture",
            "source": "memory_capture",
            "status": "superseded",
            "superseded_by": "dream-1",
            "is_archived": True,
            "archived_at": "old",
            "archive_reason": "superseded_by:dream-1",
        },
        {
            "id": "original-import",
            "source": "genesis_import",
            "status": "superseded",
            "is_archived": True,
        },
        {
            "id": "dream-1",
            "source": "memory_dream",
            "status": "active",
            "is_archived": False,
        },
        {"id": "ordinary-active", "source": "memory_capture", "status": "active"},
    ]

    updated, plan = recovery.recovery_plan(docs, now_iso="2026-08-04T00:00:00Z")
    by_id = {doc["id"]: doc for doc in updated}

    assert plan == {
        "restore_non_dream_superseded": 2,
        "retire_memory_dream": 1,
        "untouched": 1,
        "hard_deletes": 0,
    }
    for memory_id in ("original-capture", "original-import"):
        assert by_id[memory_id]["status"] == "active"
        assert by_id[memory_id]["is_archived"] is False
        assert "superseded_by" not in by_id[memory_id]
        assert "archived_at" not in by_id[memory_id]
    assert by_id["dream-1"]["status"] == "superseded"
    assert by_id["dream-1"]["is_archived"] is True
    assert by_id["dream-1"]["archive_reason"] == recovery.INCIDENT_REASON
    assert len(updated) == len(docs)


def test_recovery_source_falls_back_to_capture_mode_and_reports_distribution():
    docs = [
        {"id": "d", "capture_mode": "memory_dream", "status": "active"},
        {"id": "c", "capture_mode": "memory_capture", "status": "superseded"},
    ]

    updated, _plan = recovery.recovery_plan(docs, now_iso="2026-08-04T00:00:00Z")
    report = recovery.summarize(updated)

    assert report["by_source"] == {"memory_capture": 1, "memory_dream": 1}
    assert report["active_by_source"] == {"memory_capture": 1}
