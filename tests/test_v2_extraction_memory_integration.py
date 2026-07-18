"""Real parser -> V2 mapper -> Memory Garden validator/executor integration.

These tests intentionally do not fake ``memory.actions``. Only crypto and the
physical DB persistence boundary are replaced, so missing plaintext envelope
metadata or a parser/mapper shape mismatch fails in the same validator used in
production.
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from memory import actions as memory_actions  # noqa: E402
from memory.capture_prompt_v1 import parse_capture_cards  # noqa: E402
from memory.dream_prompt_v1 import parse_dream_consolidations  # noqa: E402
from model_api_runtime.v2 import extraction  # noqa: E402


def _install_storage(monkeypatch, moments: list[dict]) -> list[dict]:
    saved: list[dict] = []

    def _load(_store):
        return [dict(moment) for moment in moments]

    def _save(_store, new_moments):
        saved[:] = [dict(moment) for moment in new_moments]
        moments[:] = [dict(moment) for moment in new_moments]

    monkeypatch.setattr(memory_actions.memory_service, "_load_moments", _load)
    monkeypatch.setattr(memory_actions.memory_service, "_save_moments", _save)
    monkeypatch.setattr(
        memory_actions.memory_service,
        "_append_memory_change",
        lambda _store, change: {"id": "change-test", **change},
    )
    monkeypatch.setattr(
        memory_actions.boot_gates,
        "_log_bootstrap_event",
        lambda *_args, **_kwargs: None,
    )
    return saved


def _builder(user_id: str):
    counter = {"value": 0}

    def _build(inner: dict) -> dict:
        counter["value"] += 1
        memory_id = f"mem-generated-{counter['value']}"
        return {
            "id": memory_id,
            "body_ct": json.dumps(inner, ensure_ascii=False),
            "nonce": f"nonce-{memory_id}",
            "K_user": f"user-key-{memory_id}",
            "K_enclave": f"enclave-key-{memory_id}",
            "visibility": "shared",
            "owner_user_id": user_id,
        }

    return _build


def _old_card(user_id: str, memory_id: str) -> dict:
    return {
        "id": memory_id,
        "body_ct": json.dumps({"summary": memory_id, "content": memory_id}),
        "nonce": f"nonce-{memory_id}",
        "K_user": f"user-key-{memory_id}",
        "K_enclave": f"enclave-key-{memory_id}",
        "visibility": "shared",
        "owner_user_id": user_id,
        "type": "fact",
        "occurred_at": "2026-07-17T00:00:00Z",
        "created_at": "2026-07-17T00:00:00Z",
        "updated_at": "2026-07-17T00:00:00Z",
        "source": "memory_capture",
        "status": "active",
    }


def test_capture_parser_mapper_and_real_validator_persist_nonempty_card(monkeypatch):
    user_id = "capture-integration-user"
    store = types.SimpleNamespace(user_id=user_id)
    saved = _install_storage(monkeypatch, [])
    cards, parse_error = parse_capture_cards(json.dumps({
        "cards": [{
            "action": "add",
            "type": "fact",
            "summary": "用户喜欢手冲咖啡。",
            "content": "用户明确说每天早上会做手冲咖啡。",
            "bucket": "生活偏好",
            "threads": ["咖啡"],
            "importance": 0.8,
            "pulse": 0.4,
        }],
    }, ensure_ascii=False))
    assert parse_error is None

    actions, added, superseded = extraction.cards_to_actions(
        cards,
        occurred_at="2026-07-18T09:30:00Z",
        source_ids=["chat-1"],
        build_envelope=_builder(user_id),
    )
    body, status = memory_actions._execute_memory_actions(store, None, actions)

    assert status == 200 and body["status"] == "ok"
    assert (added, superseded) == (1, 0)
    assert len(saved) == 1
    assert saved[0]["type"] == "fact"
    assert saved[0]["occurred_at"] == "2026-07-18T09:30:00Z"
    assert saved[0]["source"] == "memory_capture"
    assert saved[0]["importance"] == 0.8


def test_dream_parser_mapper_and_real_validator_supersede_all_sources(monkeypatch):
    user_id = "dream-integration-user"
    store = types.SimpleNamespace(user_id=user_id)
    moments = [_old_card(user_id, "memory-a"), _old_card(user_id, "memory-b")]
    saved = _install_storage(monkeypatch, moments)
    consolidations, questions, parse_error = parse_dream_consolidations(json.dumps({
        "consolidations": [{
            "op": "merge",
            "card_ids": ["memory-a", "memory-b"],
            "result": {
                "summary": "用户稳定地喜欢自己做咖啡。",
                "content": "多次提到晨间手冲，合并为一个稳定偏好。",
                "bucket": "生活偏好",
                "threads": ["咖啡", "晨间习惯"],
                "importance": 0.9,
                "pulse": 0.5,
            },
        }],
        "questions_to_ask": [],
    }, ensure_ascii=False))
    assert parse_error is None and questions == []

    actions, added, superseded = extraction.consolidations_to_actions(
        consolidations,
        occurred_at="2026-07-18T10:00:00Z",
        source_ids=["chat-1", "chat-2"],
        build_envelope=_builder(user_id),
    )
    body, status = memory_actions._execute_memory_actions(store, None, actions)

    assert status == 200 and body["status"] == "ok"
    assert (added, superseded) == (0, 2)
    assert len(saved) == 3
    old = {item["id"]: item for item in saved if item["id"].startswith("memory-")}
    new = next(item for item in saved if item["id"].startswith("mem-generated-"))
    assert set(old) == {"memory-a", "memory-b"}
    assert all(item["status"] == "superseded" for item in old.values())
    assert all(item["superseded_by"] == new["id"] for item in old.values())
    assert new["supersedes"] == ["memory-a", "memory-b"]
    assert new["source"] == "memory_dream"
    assert new["occurred_at"] == "2026-07-18T10:00:00Z"
