"""Task 1.3 盘点工具的 fail-closed 守卫。"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import backfill_chat_bodies_to_plaintext as tool  # noqa: E402
import object_storage  # noqa: E402


def test_apply_is_rejected_before_any_database_or_r2_access(monkeypatch):
    """缺 rollout 双闸时，--apply 必须在碰数据之前硬失败。"""
    monkeypatch.setattr(sys, "argv", ["backfill", "--apply"])
    monkeypatch.setattr(
        tool, "_rows_to_process",
        lambda *_a, **_kw: pytest.fail("apply guard must run before DB access"),
    )

    with pytest.raises(SystemExit) as exc:
        tool.main()

    assert exc.value.code == 2


def test_apply_double_gate_can_enter_without_touching_rows(monkeypatch):
    monkeypatch.setenv(tool._APPLY_ENV, "1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["backfill", "--apply", "--allow-plaintext-r2-rewrite"],
    )
    monkeypatch.setattr(tool, "_iter_rows", lambda *_a, **_kw: iter(()))

    assert tool.main() == 0


def test_inventory_refuses_foreign_pointer(monkeypatch):
    monkeypatch.setattr(
        object_storage, "get_chat_body",
        lambda *_a, **_kw: pytest.fail("foreign key must not be fetched"),
    )

    status = tool._inspect_one(
        "usr_owner", "msg-1",
        {"body_key": "chatfiles/usr_other/g1/msg-1/version"},
    )

    assert status == "failed_foreign_body_key"


def test_inventory_reports_existing_owned_object(monkeypatch):
    key = "chatfiles/usr_owner/g1/msg-1/version"
    monkeypatch.setattr(object_storage, "get_chat_body", lambda *_a: "Y3Q=")

    status = tool._inspect_one("usr_owner", "msg-1", {"body_key": key})

    assert status == "would_rewrite_after_protocol"
