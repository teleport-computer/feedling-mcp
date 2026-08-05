import importlib.util
from datetime import timezone
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
        "retire_churn_cards": 1,
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


def test_window_recovery_uses_created_and_archived_boundaries_symmetrically():
    since = recovery._iso_datetime("2026-07-29")
    docs = [
        {
            "id": "dream-before-active",
            "source": "memory_dream",
            "created_at": "2026-07-28T23:59:59Z",
            "status": "active",
        },
        {
            "id": "dream-at-boundary-active",
            "source": "memory_dream",
            "created_at": "2026-07-29T00:00:00Z",
            "status": "active",
        },
        {
            "id": "capture-superseded-before",
            "source": "memory_capture",
            "status": "superseded",
            "is_archived": True,
            "archived_at": "2026-07-28T23:59:59Z",
        },
        {
            "id": "capture-superseded-at-boundary",
            "source": "memory_capture",
            "status": "superseded",
            "is_archived": True,
            "archived_at": "2026-07-29T00:00:00Z",
        },
        {
            "id": "old-dream-swallowed-in-window",
            "source": "memory_dream",
            "created_at": "2026-07-20T00:00:00Z",
            "status": "superseded",
            "is_archived": True,
            "archived_at": "2026-07-30T00:00:00Z",
            "superseded_by": "dream-churn",
        },
        {
            "id": "new-dream-already-superseded",
            "source": "memory_dream",
            "created_at": "2026-07-30T00:00:00Z",
            "status": "superseded",
            "is_archived": True,
            "archived_at": "2026-07-31T00:00:00Z",
        },
    ]

    updated, plan = recovery.recovery_plan(
        docs,
        now_iso="2026-08-04T00:00:00Z",
        since=since,
    )
    by_id = {doc["id"]: doc for doc in updated}

    assert by_id["dream-before-active"] == docs[0]
    assert by_id["dream-at-boundary-active"]["status"] == "superseded"
    assert by_id["capture-superseded-before"] == docs[2]
    assert by_id["capture-superseded-at-boundary"]["status"] == "active"
    assert by_id["old-dream-swallowed-in-window"]["status"] == "active"
    assert "superseded_by" not in by_id["old-dream-swallowed-in-window"]
    assert by_id["new-dream-already-superseded"]["status"] == "superseded"
    assert plan == {
        "restore_non_dream_superseded": 2,
        "retire_churn_cards": 2,
        "untouched": 2,
        "hard_deletes": 0,
    }


def test_since_date_is_inclusive_utc_and_workflow_forwards_it():
    parsed = recovery._iso_datetime("2026-07-29")
    assert parsed.tzinfo == timezone.utc
    assert parsed.isoformat() == "2026-07-29T00:00:00+00:00"

    workflow = (
        Path(__file__).parent.parent / ".github/workflows/recover-dream-churn.yml"
    ).read_text()
    assert "since:" in workflow
    assert 'recovery_args+=(--since "$RECOVERY_SINCE")' in workflow


# ---------------------------------------------------------------------------
# 只读侦查输出(2026-08-06,usr_a40e 复原调查):硬删流水 / supersede 按天 /
# 逐卡计划清单 / 窗内新建卡按来源。全部纯函数,不改 recovery_plan 行为。
# ---------------------------------------------------------------------------


def test_change_log_report_lists_hard_deletes_and_supersedes_by_day():
    changes = [
        {"action": "delete", "ts": "2026-08-05T01:10:00Z", "memory_id": "tomb-1"},
        {"action": "delete", "ts": "2026-08-05T01:11:00Z", "memory_id": "tomb-2"},
        {"action": "insert", "ts": "2026-08-04T18:00:00Z", "memory_id": "cap-1"},
        {
            "action": "supersede", "ts": "2026-08-04T18:03:00Z", "memory_id": "new-1",
            "supersedes": ["old-1", "old-2"], "capture_mode": "memory_dream",
        },
        {
            "action": "supersede", "ts": "2026-08-05T18:05:00Z", "memory_id": "new-2",
            "supersedes": ["old-3"],
        },
        {"action": "supersede", "ts": "not-a-date", "memory_id": "new-3", "supersedes": []},
    ]

    report = recovery.change_log_report(changes)

    assert [row["memory_id"] for row in report["hard_deletes"]] == ["tomb-1", "tomb-2"]
    assert report["supersede_by_day"]["2026-08-04"] == {
        "count": 1, "retired_cards": 2, "by_capture_mode": {"memory_dream": 1},
    }
    assert report["supersede_by_day"]["2026-08-05"] == {
        "count": 1, "retired_cards": 1, "by_capture_mode": {"unknown": 1},
    }
    assert report["supersede_by_day"]["unknown"]["count"] == 1


def test_change_log_report_never_leaks_content_fields():
    # reason 是模型可写的自由文本(≤500 字)——真实的泄漏入口,必须只降为存在位
    # (codex2 gatekeep P1 2026-08-05:summary/content 之外漏了 reason)。
    changes = [{
        "action": "delete", "ts": "2026-08-05T01:10:00Z", "memory_id": "m1",
        "summary": "secret text", "content": "secret body",
        "reason": "secret user content",
    }]
    report = recovery.change_log_report(changes)
    flat = str(report)
    assert "secret" not in flat
    assert report["hard_deletes"][0]["has_reason"] is True


def test_plan_rows_lists_only_changed_docs_with_metadata_only():
    docs = [
        {"id": "orig", "source": "memory_capture", "status": "superseded",
         "is_archived": True, "archived_at": "2026-08-04T18:03:00Z",
         "superseded_by": "dream-1", "created_at": "2026-07-20T00:00:00Z",
         "summary": "secret original"},
        {"id": "dream-1", "source": "memory_dream", "status": "active",
         "created_at": "2026-08-04T18:03:00Z", "summary": "secret tombstone"},
        {"id": "calm", "source": "memory_capture", "status": "active",
         "created_at": "2026-07-01T00:00:00Z"},
    ]
    updated, _plan = recovery.recovery_plan(docs, now_iso="2026-08-06T00:00:00Z")
    rows = recovery.plan_rows(docs, updated)

    assert {row["id"]: row["planned"] for row in rows} == {
        "orig": "restore", "dream-1": "retire",
    }
    assert all("summary" not in row and "content" not in row for row in rows)
    restored = next(row for row in rows if row["id"] == "orig")
    assert restored["was_superseded_by"] == "dream-1"


def test_window_created_by_source_groups_and_respects_since():
    since = recovery._iso_datetime("2026-08-01")
    docs = [
        {"id": "patch-new", "source": "resident_patch",
         "created_at": "2026-08-05T18:05:00Z", "status": "active"},
        {"id": "dream-new", "source": "memory_dream",
         "created_at": "2026-08-04T18:03:00Z", "status": "active"},
        {"id": "old-capture", "source": "memory_capture",
         "created_at": "2026-07-20T00:00:00Z", "status": "active"},
    ]
    grouped = recovery.window_created_by_source(docs, since=since)
    assert set(grouped) == {"resident_patch", "memory_dream"}
    assert grouped["resident_patch"][0]["id"] == "patch-new"
    # 不带窗时全量分组
    grouped_all = recovery.window_created_by_source(docs, since=None)
    assert set(grouped_all) == {"resident_patch", "memory_dream", "memory_capture"}


def test_retire_sources_extends_to_resident_patch_and_absorb():
    """usr_a40e 徒手 patch 潮:窗内新建的 resident_patch 墓碑必须能被退休,
    且被它吃掉的原卡照常恢复;窗前的同源老卡不动。"""
    since = recovery._iso_datetime("2026-08-04")
    docs = [
        # 窗内徒手墓碑(active)→ 退休
        {"id": "patch-tomb", "source": "resident_patch", "status": "active",
         "created_at": "2026-08-04T18:03:15Z"},
        # 窗内 absorb(已被后续墓碑顶掉)→ 保持退休(retire 分支优先于 restore)
        {"id": "absorb-mid", "source": "resident_absorb", "status": "superseded",
         "created_at": "2026-08-04T18:02:44Z", "is_archived": True,
         "archived_at": "2026-08-04T18:03:11Z", "superseded_by": "patch-tomb"},
        # 窗前老卡、窗内被吃 → 恢复
        {"id": "old-dream", "source": "memory_dream", "status": "superseded",
         "created_at": "2026-07-18T00:00:00Z", "is_archived": True,
         "archived_at": "2026-08-04T18:03:15Z", "superseded_by": "patch-tomb"},
        # 窗前 patch 老卡、没被动 → 不动
        {"id": "old-patch", "source": "resident_patch", "status": "active",
         "created_at": "2026-08-01T03:56:09Z"},
    ]
    updated, plan = recovery.recovery_plan(
        docs, now_iso="2026-08-06T00:00:00Z", since=since,
        retire_sources={"memory_dream", "resident_patch", "resident_absorb"},
    )
    by_id = {doc["id"]: doc for doc in updated}

    assert plan["retire_churn_cards"] >= 1
    assert by_id["patch-tomb"]["status"] == "superseded"
    assert by_id["patch-tomb"]["archive_reason"] == recovery.INCIDENT_REASON
    assert by_id["absorb-mid"]["status"] == "superseded"      # 不复活窗内嫌疑卡
    assert by_id["old-dream"]["status"] == "active"           # 被吃原卡恢复
    assert by_id["old-patch"]["status"] == "active"           # 窗前老卡不动
    assert "archive_reason" not in by_id["old-patch"]


def test_default_retire_sources_stay_dream_only():
    docs = [
        {"id": "patch-active", "source": "resident_patch", "status": "active",
         "created_at": "2026-08-05T00:00:00Z"},
    ]
    updated, plan = recovery.recovery_plan(docs, now_iso="2026-08-06T00:00:00Z")
    assert updated[0]["status"] == "active"                   # 默认行为不变
    assert plan["retire_churn_cards"] == 0


def test_retire_matches_capture_mode_field_like_old_is_dream():
    # codex2 P1/P2:{source: memory_capture, capture_mode: memory_dream} 旧 _is_dream
    # 命中 → 泛化后必须仍命中,默认语义不许回退。
    docs = [{
        "id": "mixed-fields", "source": "memory_capture",
        "capture_mode": "memory_dream", "status": "active",
        "created_at": "2026-08-05T00:00:00Z",
    }]
    updated, plan = recovery.recovery_plan(docs, now_iso="2026-08-06T00:00:00Z")
    assert updated[0]["status"] == "superseded"
    assert plan["retire_churn_cards"] == 1
