from __future__ import annotations

import json
import sys
import types
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import hosted_runtime as runtime  # noqa: E402
import memory_readside_core as readside_core  # noqa: E402
from memory import actions as memory_actions  # noqa: E402


def _install_memory_action_fakes(monkeypatch, moments: list[dict]) -> list[dict]:
    saved: list[dict] = []
    envelope_counter = {"value": 0}

    def fake_load(_store):
        return list(moments)

    def fake_save(_store, new_moments):
        saved[:] = [dict(moment) for moment in new_moments]
        moments[:] = [dict(moment) for moment in new_moments]

    def fake_envelope(store, inner, *, item_id=None):
        envelope_counter["value"] += 1
        eid = item_id or f"mem_new_{envelope_counter['value']}"
        return {
            "id": eid,
            "body_ct": json.dumps(inner, ensure_ascii=False),
            "nonce": f"nonce_{eid}",
            "K_user": f"ku_{eid}",
            "K_enclave": f"ke_{eid}",
            "enclave_pk_fpr": "fpr_test",
            "visibility": "shared",
            "owner_user_id": store.user_id,
        }, ""

    monkeypatch.setattr(memory_actions.memory_service, "_load_moments", fake_load)
    monkeypatch.setattr(memory_actions.memory_service, "_save_moments", fake_save)
    monkeypatch.setattr(memory_actions, "_build_memory_envelope_for_store", fake_envelope)
    monkeypatch.setattr(memory_actions.boot_gates, "_log_bootstrap_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        memory_actions.memory_service,
        "_append_memory_change",
        lambda _store, change: {"id": "chg_test", **change},
    )
    return saved


def _inner(moment: dict) -> dict:
    return json.loads(moment["body_ct"])


def test_memory_add_writes_clean_v1_body_fields(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_m2")
    moments: list[dict] = []
    saved = _install_memory_action_fakes(monkeypatch, moments)

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.add",
            "memory": {
                "type": "fact",
                "summary": "用户有只猫叫武松，是狸花猫。",
                "verbatim": "我有只猫叫武松，是狸花猫。",
                "salience": "high",
                "importance": 0.8,
                "occurred_at": "2026-06-21",
                "source": "hosted_runtime_state",
            },
            "reason": "User explicitly stated a durable pet fact.",
        }
    ])

    assert status == 200
    assert body["status"] == "ok"
    assert len(saved) == 1
    moment = saved[0]
    assert moment["status"] == "active"
    assert moment["importance"] == 0.8
    assert moment["pulse"] == 0.3
    assert moment["last_referenced_at"] == moment["occurred_at"]
    for legacy_key in ("card_v", "salience", "source_type", "type"):
        assert legacy_key not in moment
    inner = _inner(moment)
    assert inner["summary"] == "用户有只猫叫武松，是狸花猫。"
    assert inner["content"].startswith("记忆: 用户有只猫叫武松，是狸花猫。")
    assert inner["bucket"] == "未分类"
    assert inner["threads"] == []
    for legacy_key in ("verbatim", "description", "her_quote", "title"):
        assert legacy_key not in inner


def test_memory_supersede_soft_retires_old_card_and_new_card_is_recallable(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_m2")
    old = {
        "v": 1,
        "id": "mem_old_cat",
        "type": "fact",
        "owner_user_id": "usr_m2",
        "visibility": "shared",
        "body_ct": json.dumps({"summary": "武松是狸花猫", "description": "武松是狸花猫"}),
        "nonce": "nonce_old",
        "K_user": "ku_old",
        "K_enclave": "ke_old",
        "enclave_pk_fpr": "fpr_test",
        "occurred_at": "2026-06-20",
        "created_at": "2026-06-20",
        "updated_at": "2026-06-20",
        "source": "hosted_runtime_state",
        "status": "active",
    }
    moments = [old]
    saved = _install_memory_action_fakes(monkeypatch, moments)
    monkeypatch.setattr(
        memory_actions,
        "_memory_plain_from_envelope",
        lambda moment, _api_key, runtime_token="": (_inner(moment), ""),
    )

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.supersede",
            "supersedes": "mem_old_cat",
            "memory": {
                "type": "fact",
                "summary": "武松其实是橘猫。",
                "verbatim": "我记错了，武松其实是橘猫。",
                "occurred_at": "2026-06-21",
                "source": "hosted_runtime_state",
            },
            "reason": "User corrected the cat breed.",
        }
    ])

    assert status == 200
    assert body["status"] == "ok"
    assert len(saved) == 2
    old_after = next(moment for moment in saved if moment["id"] == "mem_old_cat")
    new_card = next(moment for moment in saved if moment["id"] != "mem_old_cat")
    assert old_after["status"] == "superseded"
    assert old_after["superseded_by"] == new_card["id"]
    assert old_after["is_archived"] is True
    assert old_after["archive_reason"] == f"superseded_by:{new_card['id']}"
    assert new_card["status"] == "active"
    assert new_card["supersedes"] == ["mem_old_cat"]
    assert readside_core.memory_available(old_after, "usr_m2") is False
    assert readside_core.memory_available(old_after, "usr_m2", include_superseded=True, include_archived=True) is True
    assert readside_core.memory_available(new_card, "usr_m2") is True


def test_memory_supersede_prebuilt_envelope_accepts_multiple_old_cards(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_m2")
    old_a = {
        "v": 1,
        "id": "mem_old_a",
        "type": "fact",
        "owner_user_id": "usr_m2",
        "visibility": "shared",
        "body_ct": json.dumps({"summary": "A", "description": "A"}),
        "nonce": "nonce_a",
        "K_user": "ku_a",
        "K_enclave": "ke_a",
        "enclave_pk_fpr": "fpr_test",
        "occurred_at": "2026-06-20",
        "created_at": "2026-06-20",
        "updated_at": "2026-06-20",
        "source": "memory_capture",
        "status": "active",
    }
    old_b = {**old_a, "id": "mem_old_b", "nonce": "nonce_b", "K_user": "ku_b", "K_enclave": "ke_b"}
    moments = [old_a, old_b]
    saved = _install_memory_action_fakes(monkeypatch, moments)
    envelope = {
        "id": "mem_new_dream",
        "body_ct": json.dumps({"summary": "Merged A+B", "content": "Merged dream card."}),
        "nonce": "nonce_new",
        "K_user": "ku_new",
        "K_enclave": "ke_new",
        "enclave_pk_fpr": "fpr_test",
        "visibility": "shared",
        "owner_user_id": "usr_m2",
        "type": "fact",
        "occurred_at": "2026-06-21",
        "source": "memory_dream",
        "importance": 0.8,
        "pulse": 0.4,
    }

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.supersede",
            "supersedes": ["mem_old_a", "mem_old_b"],
            "envelope": envelope,
            "capture_mode": "memory_dream",
            "reason": "Dream merge.",
        }
    ])

    assert status == 200
    assert body["status"] == "ok"
    assert len(saved) == 3
    old_after = {moment["id"]: moment for moment in saved if moment["id"].startswith("mem_old_")}
    new_card = next(moment for moment in saved if moment["id"] == "mem_new_dream")
    assert set(old_after) == {"mem_old_a", "mem_old_b"}
    assert all(moment["status"] == "superseded" for moment in old_after.values())
    assert all(moment["superseded_by"] == "mem_new_dream" for moment in old_after.values())
    assert all(moment["is_archived"] is True for moment in old_after.values())
    assert new_card["status"] == "active"
    assert new_card["source"] == "memory_dream"
    assert new_card["supersedes"] == ["mem_old_a", "mem_old_b"]
    assert body["results"][0]["superseded_ids"] == ["mem_old_a", "mem_old_b"]


def test_memory_supersede_rejects_over_limit_instead_of_retiring_a_prefix():
    store = types.SimpleNamespace(user_id="usr_m2")

    result, effects, status = memory_actions._memory_supersede_envelope_action(
        store,
        {
            "type": "memory.supersede",
            "supersedes": [f"mem_{index}" for index in range(25)],
            "envelope": {},
        },
    )

    assert status == 400
    assert result["error"] == "too_many_supersedes"
    assert result["max_supersedes"] == 20
    assert effects == []


def test_memory_supersede_aborts_if_any_target_changes_before_locked_commit(monkeypatch):
    """A stale merge must not append a successor after one source disappears."""
    store = types.SimpleNamespace(user_id="usr_m2")

    def old_card(mid: str) -> dict:
        return {
            "v": 1,
            "id": mid,
            "type": "fact",
            "owner_user_id": "usr_m2",
            "visibility": "shared",
            "body_ct": json.dumps({"summary": mid, "description": mid}),
            "nonce": f"nonce_{mid}",
            "K_user": f"ku_{mid}",
            "K_enclave": f"ke_{mid}",
            "enclave_pk_fpr": "fpr_test",
            "occurred_at": "2026-06-20",
            "created_at": "2026-06-20",
            "updated_at": "2026-06-20",
            "source": "memory_capture",
            "status": "active",
        }

    old_a = old_card("mem_old_a")
    old_b = old_card("mem_old_b")
    moments = [old_a, old_b]
    saved = _install_memory_action_fakes(monkeypatch, moments)
    monkeypatch.setattr(
        memory_actions,
        "_memory_plain_from_envelope",
        lambda moment, _api_key, runtime_token="": (_inner(moment), ""),
    )
    loads = {"count": 0}

    def load_with_concurrent_removal(_store):
        loads["count"] += 1
        return [old_a, old_b] if loads["count"] == 1 else [old_a]

    monkeypatch.setattr(
        memory_actions.memory_service,
        "_load_moments",
        load_with_concurrent_removal,
    )

    body, status = memory_actions._execute_memory_actions(store, "api_key", [{
        "type": "memory.supersede",
        "supersedes": ["mem_old_a", "mem_old_b"],
        "memory": {"summary": "merged", "content": "merged content"},
        "capture_mode": "agent_tool",
    }])

    assert status == 400
    assert body["status"] == "failed"
    assert body["results"][0]["http_status"] == 409
    assert body["results"][0]["error"] == "supersede_targets_changed"
    assert body["results"][0]["target_ids"] == ["mem_old_b"]
    assert body["effects"] == []
    assert saved == []
    assert old_a["status"] == "active"


def test_memory_supersede_rejects_an_already_archived_target_without_new_card(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_m2")
    old = {
        "id": "mem_old",
        "type": "fact",
        "owner_user_id": "usr_m2",
        "body_ct": json.dumps({"summary": "old", "description": "old"}),
        "status": "superseded",
        "is_archived": True,
        "superseded_by": "mem_existing_successor",
    }
    moments = [old]
    saved = _install_memory_action_fakes(monkeypatch, moments)

    body, status = memory_actions._execute_memory_actions(store, "api_key", [{
        "type": "memory.supersede",
        "supersedes": ["mem_old"],
        "memory": {"summary": "duplicate successor", "content": "duplicate successor"},
    }])

    assert status == 400
    assert body["results"][0]["http_status"] == 409
    assert body["results"][0]["error"] == "supersede_targets_unavailable"
    assert body["results"][0]["target_ids"] == ["mem_old"]
    assert saved == []


def test_prebuilt_memory_supersede_aborts_if_all_targets_disappear_before_commit(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_m2")
    old = {
        "id": "mem_old",
        "type": "fact",
        "owner_user_id": "usr_m2",
        "body_ct": json.dumps({"summary": "old", "description": "old"}),
        "status": "active",
    }
    moments = [old]
    saved = _install_memory_action_fakes(monkeypatch, moments)
    loads = {"count": 0}

    def load_with_concurrent_removal(_store):
        loads["count"] += 1
        return [old] if loads["count"] == 1 else []

    monkeypatch.setattr(memory_actions.memory_service, "_load_moments", load_with_concurrent_removal)

    result, effects, status = memory_actions._memory_supersede_envelope_action(store, {
        "type": "memory.supersede",
        "supersedes": ["mem_old"],
        "envelope": {
            "id": "mem_duplicate",
            "type": "fact",
            "owner_user_id": "usr_m2",
            "body_ct": "ciphertext",
            "nonce": "nonce_duplicate",
            "K_user": "ku_duplicate",
            "K_enclave": "ke_duplicate",
            "enclave_pk_fpr": "fpr_test",
            "visibility": "shared",
            "occurred_at": "2026-08-14",
            "source": "memory_dream",
            "status": "active",
        },
    })

    assert status == 409
    assert result["error"] == "supersede_targets_changed"
    assert result["target_ids"] == ["mem_old"]
    assert effects == []
    assert saved == []


def test_coerce_runtime_action_maps_memory_supersede_to_executor_action():
    action = {
        "type": "memory.supersede",
        "confidence": 0.96,
        "target": {"memory_id": "mem_old_cat"},
        "payload": {
            "memory": {
                "type": "fact",
                "summary": "武松其实是橘猫。",
                "verbatim": "我记错了，武松其实是橘猫。",
                "occurred_at": "2026-06-21",
            }
        },
        "reason": "User corrected an old cat breed memory.",
    }

    coerced = runtime.coerce_runtime_action(action, [], direct_confidence=0.9)

    assert coerced is not None
    assert coerced["domain"] == "memory"
    assert coerced["requires_confirmation"] is False
    assert coerced["executor_action"] == {
        "type": "memory.supersede",
        "supersedes": "mem_old_cat",
        "memory": {
            "summary": "武松其实是橘猫。",
            "content": "武松其实是橘猫。",
            "bucket": "",
            "threads": [],
            "importance": 0.5,
            "pulse": 0.3,
            "occurred_at": "2026-06-21",
            "source": "hosted_runtime_state",
        },
        "reason": "User corrected an old cat breed memory.",
        "capture_mode": "state",
    }


def test_background_execution_prompt_advertises_memory_supersede():
    messages = runtime.build_background_execution_messages(
        user_message="我记错了，武松其实是橘猫。",
        identity={},
        memory_candidates=[{"id": "mem_old_cat", "title": "武松是狸花猫"}],
        context_refs=[],
        pending_items=[],
        memory_terms={"buckets": ["宠物"], "threads": ["武松"]},
    )

    system_prompt = messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert "memory.supersede" in system_prompt
    assert "target.memory_id" in system_prompt
    assert "Memory write guidance" in system_prompt
    assert payload["existing_memory_terms"] == {"buckets": ["宠物"], "threads": ["武松"]}


def test_memory_content_patch_supersedes_old_card_with_v1_shape(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_m2")
    existing = {
        "v": 1,
        "id": "mem_patch_cat",
        "type": "fact",
        "owner_user_id": "usr_m2",
        "visibility": "shared",
        "body_ct": json.dumps({"summary": "旧摘要", "description": "旧摘要"}),
        "nonce": "nonce_patch",
        "K_user": "ku_patch",
        "K_enclave": "ke_patch",
        "enclave_pk_fpr": "fpr_test",
        "occurred_at": "2026-06-20",
        "created_at": "2026-06-20",
        "updated_at": "2026-06-20",
        "source": "hosted_runtime_state",
        "status": "active",
    }
    moments = [existing]
    saved = _install_memory_action_fakes(monkeypatch, moments)
    monkeypatch.setattr(
        memory_actions,
        "_memory_plain_from_envelope",
        lambda _moment, _api_key, runtime_token="": (_inner(existing), ""),
    )

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.content_patch",
            "memory_id": "mem_patch_cat",
            "patch": {
                "summary": "用户有只猫叫武松，是橘猫。",
                "verbatim": "我记错了，武松其实是橘猫。",
            },
            "reason": "Patch should keep Garden and readside in sync.",
        }
    ])

    assert status == 200
    assert body["status"] == "ok"
    old_after = next(moment for moment in saved if moment["id"] == "mem_patch_cat")
    new_card = next(moment for moment in saved if moment["id"] != "mem_patch_cat")
    assert old_after["status"] == "superseded"
    inner = _inner(new_card)
    assert inner["summary"] == "用户有只猫叫武松，是橘猫。"
    assert inner["content"].startswith("记忆: 用户有只猫叫武松，是橘猫。")
    assert "description" not in inner
    assert "her_quote" not in inner
