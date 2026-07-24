"""Regression coverage for the Memory Garden retype mutation fence."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import os
from pathlib import Path
import sys
import threading
import uuid

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

if not os.environ.get("DATABASE_URL"):
    pytest.skip("DATABASE_URL not set — needs a real Postgres", allow_module_level=True)

import conftest  # noqa: E402
import db  # noqa: E402
from core import store as core_store  # noqa: E402
from memory import actions as memory_actions  # noqa: E402
from memory import service as memory_service  # noqa: E402


db.init_schema()


def _card(user_id: str, memory_id: str, *, mem_type: str, body: str) -> dict:
    return {
        "id": memory_id,
        "type": mem_type,
        "occurred_at": "2026-07-20T12:00:00Z",
        "created_at": "2026-07-20T12:00:00Z",
        "updated_at": "2026-07-20T12:00:00Z",
        "source": "memory_capture",
        "owner_user_id": user_id,
        "visibility": "shared",
        "body_ct": body,
        "nonce": f"nonce-{body}",
        "K_user": f"user-key-{body}",
        "K_enclave": f"enclave-key-{body}",
        "enclave_pk_fpr": f"fpr-{body}",
        "status": "active",
        "importance": 0.7,
        "pulse": 0.4,
        "last_referenced_at": "2026-07-20T12:00:00Z",
    }


def test_retype_uses_fresh_target_after_cross_process_whole_garden_write(monkeypatch):
    """A Capture-like update queued before retype's fence is never reverted.

    The two ``UserStore`` objects model different backend processes: their
    Python locks are unrelated.  Retype pauses immediately before taking its
    PostgreSQL mutation fence, after the old implementation had already copied
    its target.  A whole-Garden writer then re-encrypts and supersedes the card.
    Retype must load that fresh row under the fence and change only its requested
    type plus retype audit timestamps.
    """

    user_id = f"u_retype_fence_{uuid.uuid4().hex[:12]}"
    conftest.seed_user(user_id)
    initial = _card(user_id, "target", mem_type="quote", body="stale")
    assert db.memory_upsert(user_id, "target", initial["occurred_at"], initial)

    retype_store = core_store.UserStore(user_id)
    writer_store = core_store.UserStore(user_id)
    real_mutation_lock = memory_service.mutation_lock
    retype_at_fence = threading.Event()
    release_retype = threading.Event()
    gate_guard = threading.Lock()
    gate_used = False

    @contextmanager
    def delayed_retype_lock(store):
        nonlocal gate_used
        should_delay = False
        if store is retype_store:
            with gate_guard:
                if not gate_used:
                    gate_used = True
                    should_delay = True
        if should_delay:
            retype_at_fence.set()
            assert release_retype.wait(timeout=3)
        with real_mutation_lock(store):
            yield

    monkeypatch.setattr(memory_service, "mutation_lock", delayed_retype_lock)
    writer_target: dict = {}

    def _retype():
        return memory_actions._memory_retype_action(
            retype_store,
            {
                "type": "memory.retype",
                "memory_id": "target",
                "new_type": "fact",
            },
        )

    def _capture_like_whole_garden_write() -> None:
        with real_mutation_lock(writer_store):
            moments = memory_service._load_moments(writer_store)
            target_idx = next(i for i, item in enumerate(moments) if item.get("id") == "target")
            fresh_target = dict(moments[target_idx])
            fresh_target.update({
                "type": "event",
                "body_ct": "capture-fresh-ciphertext",
                "nonce": "capture-fresh-nonce",
                "K_user": "capture-fresh-user-key",
                "K_enclave": "capture-fresh-enclave-key",
                "enclave_pk_fpr": "capture-fresh-fpr",
                "status": "superseded",
                "superseded_by": "replacement",
                "is_archived": True,
                "archived_at": "2026-07-20T12:01:00Z",
                "archive_reason": "superseded_by:replacement",
                "updated_at": "2026-07-20T12:01:00Z",
            })
            moments[target_idx] = fresh_target
            moments.append(
                _card(user_id, "replacement", mem_type="fact", body="replacement")
            )
            memory_service._save_moments(writer_store, moments)
            writer_target.update(fresh_target)

    with ThreadPoolExecutor(max_workers=2) as executor:
        retype_future = executor.submit(_retype)
        try:
            assert retype_at_fence.wait(timeout=3)
            writer_future = executor.submit(_capture_like_whole_garden_write)
            writer_future.result(timeout=3)
        finally:
            release_retype.set()
        body, effects, status = retype_future.result(timeout=3)

    assert status == 200, body
    assert body["change"]["old_type"] == "event"
    assert effects[0]["memory_id"] == "target"

    moments = {item["id"]: item for item in db.memory_load(user_id)}
    assert set(moments) == {"target", "replacement"}
    target = moments["target"]
    assert target["type"] == "fact"
    assert target["updated_at"] == target["retyped_at"]

    # Retype may update only its semantic field and audit timestamps.  Every
    # fresh envelope and supersede field from the preceding writer survives.
    ignored = {"type", "updated_at", "retyped_at"}
    assert {key: value for key, value in target.items() if key not in ignored} == {
        key: value for key, value in writer_target.items() if key not in ignored
    }
