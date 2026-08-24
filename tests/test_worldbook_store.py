from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from core import store as core_store  # noqa: E402
from core.store import UserStore  # noqa: E402


def _uid() -> str:
    return f"usr_worldbook_{uuid.uuid4().hex[:12]}"


def _record(entry_id: str, *, body_ct: str = "ct", updated_at: str = "2026-07-03T00:00:00") -> dict:
    return {
        "id": entry_id,
        "owner_user_id": "usr_owner",
        "v": 1,
        "body_ct": body_ct,
        "nonce": "nonce",
        "K_user": "k-user",
        "K_enclave": "k-enclave",
        "visibility": "shared",
        "enclave_pk_fpr": "fpr",
        "updated_at": updated_at,
    }


def test_world_books_upsert_replaces_by_id_and_persists():
    uid = _uid()
    store = UserStore(uid)

    saved = store.upsert_world_book(_record("wb1", body_ct="one"))
    assert saved["id"] == "wb1"
    assert [item["body_ct"] for item in store.world_books] == ["one"]

    store.upsert_world_book(_record("wb2", body_ct="two"))
    store.upsert_world_book(_record("wb1", body_ct="one-edited"))
    assert {item["id"]: item["body_ct"] for item in store.world_books} == {
        "wb1": "one-edited",
        "wb2": "two",
    }

    reloaded = UserStore(uid)
    assert {item["id"]: item["body_ct"] for item in reloaded.world_books} == {
        "wb1": "one-edited",
        "wb2": "two",
    }


def test_world_books_delete_returns_whether_row_existed_and_persists():
    uid = _uid()
    store = UserStore(uid)
    store.upsert_world_book(_record("wb1"))
    store.upsert_world_book(_record("wb2"))

    assert store.delete_world_book("wb1") is True
    assert [item["id"] for item in store.world_books] == ["wb2"]
    assert store.delete_world_book("missing") is False

    reloaded = UserStore(uid)
    assert [item["id"] for item in reloaded.world_books] == ["wb2"]


def test_world_book_failed_db_upsert_does_not_mutate_local_cache(monkeypatch):
    store = UserStore(_uid())
    before = [dict(item) for item in store.world_books]
    monkeypatch.setattr(core_store.db, "world_book_upsert", lambda *_args: False)

    with pytest.raises(RuntimeError, match="world_book_write_failed"):
        store.upsert_world_book(_record("not-saved"))

    assert store.world_books == before
