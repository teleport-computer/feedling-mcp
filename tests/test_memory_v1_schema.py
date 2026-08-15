from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from enclave import readside  # noqa: E402
from memory import actions as memory_actions  # noqa: E402
from memory import service as memory_service  # noqa: E402
from memory.source_policy import MEMORY_SOURCE_VALUES  # noqa: E402


# Known values observed in the 2026-07-29 production 500-user source sample.
# Keep this hard-coded so a policy edit cannot silently remove a live value.
PROD_MEMORY_SOURCE_VALUES_2026_07_29 = {
    "bootstrap",
    "chat",
    "genesis_import",
    "genesis_resident_distill",
    "history_import",
    "hosted_runtime_state",
    "live_conversation",
    "memory_capture",
    "memory_dream",
    "memory_migrate",
    "model_api_capture",
    "model_api_correction",
    "model_api_repair",
    "ombre_brain_sync",
    "resident_absorb",
    "resident_patch",
}


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
        eid = item_id or f"mem_v1_{envelope_counter['value']}"
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


def test_memory_add_writes_clean_v1_schema_without_legacy_fields(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    saved = _install_memory_action_fakes(monkeypatch, [])

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.add",
            "memory": {
                "summary": "用户有只狗叫蛋子，是比熊。",
                "content": "记忆: 用户养了一只狗，叫蛋子，是比熊。\n上下文: 用户明确告诉过我们。\n使用提示: 问到宠物时自然提起，不要反复确认。",
                "bucket": "宠物",
                "threads": ["蛋子", "狗狗"],
                "importance": 0.72,
                "pulse": 0.45,
                "occurred_at": "2026-06-25T12:24:00",
                "source": "chat",
            },
        }
    ])

    assert status == 200
    assert body["status"] == "ok"
    moment = saved[0]
    assert moment["importance"] == 0.72
    assert moment["pulse"] == 0.45
    assert moment["status"] == "active"
    assert moment["last_referenced_at"] == moment["occurred_at"]
    for legacy_key in ("type", "card_v", "salience", "source_type", "anchor_memory_ids"):
        assert legacy_key not in moment

    inner = json.loads(moment["body_ct"])
    assert inner == {
        "summary": "用户有只狗叫蛋子，是比熊。",
        "content": "记忆: 用户养了一只狗，叫蛋子，是比熊。\n上下文: 用户明确告诉过我们。\n使用提示: 问到宠物时自然提起，不要反复确认。",
        "bucket": "宠物",
        "threads": ["蛋子", "狗狗"],
    }


def test_memory_add_truncation_emits_content_free_counts_without_behavior_change(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1_truncation")
    saved = _install_memory_action_fakes(monkeypatch, [])
    events = []
    monkeypatch.setattr(
        memory_actions.debug_trace,
        "trace_event",
        lambda _store, **event: events.append(event),
    )
    secret = "T074_SECRET_MUST_NOT_REACH_TRACE"
    raw_content = ("x" * 5001) + secret

    body, status = memory_actions._execute_memory_actions(store, "api_key", [{
        "type": "memory.add",
        "memory": {
            "summary": "Long card",
            "content": raw_content,
            "source": "chat",
        },
    }])

    assert status == 200
    assert body["status"] == "ok"
    stored = json.loads(saved[0]["body_ct"])["content"]
    assert stored == raw_content[:5000]
    assert events == [{
        "subsystem": "memory",
        "type": "memory.content.truncation",
        "actor": "backend",
        "status": "warning",
        "summary": "",
        "explain": "",
        "detail": {
            "route": "memory_actions",
            "counts": {
                "original_chars": len(raw_content),
                "truncated_chars": len(raw_content) - 5000,
            },
        },
    }]
    assert secret not in json.dumps(events, ensure_ascii=False)


def test_memory_add_preserves_explicit_empty_occurred_at(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    saved = _install_memory_action_fakes(monkeypatch, [])

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.add",
            "memory": {
                "summary": "用户希望导入记忆保持可读。",
                "content": "记忆: 用户希望导入记忆保持可读。\n上下文: 导入材料没有明确事件日期。\n使用提示: 用作长期偏好。",
                "bucket": "协作方式",
                "threads": ["记忆导入"],
                "occurred_at": "",
                "source": "genesis_import",
            },
        }
    ])

    assert status == 200
    assert body["status"] == "ok"
    assert saved[0]["occurred_at"] == ""
    assert "last_referenced_at" not in saved[0]


def test_memory_action_rejects_invented_source_and_capture_mode(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    saved = _install_memory_action_fakes(monkeypatch, [])
    base_memory = {
        "summary": "用户喜欢咖啡。",
        "content": "记忆: 用户喜欢咖啡。",
    }

    body, status = memory_actions._execute_memory_actions(store, "api_key", [{
        "type": "memory.add",
        "memory": {**base_memory, "source": "对话"},
    }])
    assert status == 400
    assert body["status"] == "failed"
    assert body["error"] == "source_invalid"
    assert body["results"][0]["error"] == "source_invalid"
    assert saved == []

    body, status = memory_actions._execute_memory_actions(store, "api_key", [{
        "type": "memory.add",
        "memory": {**base_memory, "source": "chat"},
        "capture_mode": "conversation_2026",
    }])
    assert status == 400
    assert body["status"] == "failed"
    assert body["error"] == "capture_mode_invalid"
    assert body["results"][0]["error"] == "capture_mode_invalid"
    assert saved == []


def test_memory_action_accepts_all_declared_source_and_capture_mode_values(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    for index, source in enumerate(sorted(memory_actions.MEMORY_SOURCE_VALUES)):
        saved = _install_memory_action_fakes(monkeypatch, [])
        body, status = memory_actions._execute_memory_actions(store, "api_key", [{
            "type": "memory.add",
            "memory": {
                "summary": f"legal source {index}",
                "content": f"legal source body {index}",
                "source": source,
            },
        }])
        assert status == 200, (source, body)
        assert len(saved) == 1

    for index, capture_mode in enumerate(
        sorted(memory_actions.MEMORY_CAPTURE_MODE_VALUES)
    ):
        saved = _install_memory_action_fakes(monkeypatch, [])
        body, status = memory_actions._execute_memory_actions(store, "api_key", [{
            "type": "memory.add",
            "memory": {
                "summary": f"legal capture mode {index}",
                "content": f"legal capture body {index}",
                "source": "chat",
            },
            "capture_mode": capture_mode,
        }])
        assert status == 200, (capture_mode, body)
        assert len(saved) == 1


def test_memory_source_policy_covers_known_production_values():
    assert PROD_MEMORY_SOURCE_VALUES_2026_07_29 <= MEMORY_SOURCE_VALUES


@pytest.mark.parametrize("count", [1, 2])
def test_memory_batch_all_success_stays_200(monkeypatch, count):
    store = types.SimpleNamespace(user_id="usr_v1")
    monkeypatch.setattr(
        memory_actions,
        "_execute_memory_action",
        lambda _store, _api_key, action, **_kwargs: (
            {"status": "ok", "id": f"mem_{action['marker']}"},
            [{"memory_id": f"mem_{action['marker']}"}],
            200,
        ),
    )

    body, status = memory_actions._execute_memory_actions(
        store, "api_key", [{"marker": i} for i in range(count)]
    )

    assert status == 200
    assert body["status"] == "ok"
    assert body["applied_count"] == count
    assert body["skipped_count"] == body["failed_count"] == 0
    assert "error" not in body and "detail" not in body
    assert [row["http_status"] for row in body["results"]] == [200] * count


def test_memory_batch_all_item_failures_return_400_with_complete_results(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    calls = []

    def fail_one(_store, _api_key, action, *, runtime_token=""):
        calls.append(action["marker"])
        return {
            "status": "error",
            "error": f"bad_{action['marker']}",
            "detail": {"marker": action["marker"]},
        }, [{"type": "must_not_escape_failed_item"}], 400

    monkeypatch.setattr(memory_actions, "_execute_memory_action", fail_one)
    body, status = memory_actions._execute_memory_actions(
        store,
        "api_key",
        [{"marker": "one"}, {"marker": "two"}],
    )

    assert status == 400
    assert calls == ["one", "two"]
    assert body["status"] == "failed"
    assert body["error"] == "bad_one"
    assert body["detail"] == {"marker": "one"}
    assert body["total_count"] == 2
    assert body["applied_count"] == 0
    assert body["skipped_count"] == 0
    assert body["failed_count"] == 2
    assert [row["error"] for row in body["results"]] == ["bad_one", "bad_two"]
    assert body["effects"] == []


def test_memory_batch_single_item_failure_returns_400_with_full_result(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    monkeypatch.setattr(
        memory_actions,
        "_execute_memory_action",
        lambda *_args, **_kwargs: (
            {
                "status": "error",
                "error": "anchor_required",
                "detail": {"mem_type": "insight"},
            },
            [],
            400,
        ),
    )

    body, status = memory_actions._execute_memory_actions(
        store, "api_key", [{"marker": "one"}]
    )

    assert status == 400
    assert body["error"] == "anchor_required"
    assert body["detail"] == {"mem_type": "insight"}
    assert body["total_count"] == body["failed_count"] == 1
    assert body["applied_count"] == body["skipped_count"] == 0
    assert body["results"][0]["http_status"] == 400


def test_memory_batch_mixed_failures_and_noop_do_not_affect_neighbors(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    calls = []
    outcomes = {
        "good_1": ({"status": "ok", "id": "mem_1"}, [{"memory_id": "mem_1"}], 200),
        "source": ({"status": "error", "error": "source_invalid"}, [], 400),
        "missing": ({"status": "error", "error": "not_found"}, [], 404),
        "foreign": ({"status": "error", "error": "not_owned"}, [], 403),
        "polluted": ({"status": "error", "error": "memory_card_polluted"}, [], 400),
        "duplicate": (
            {"status": "ok", "noop": True, "skipped": "duplicate_active"},
            [],
            200,
        ),
        "good_2": ({"status": "ok", "id": "mem_2"}, [{"memory_id": "mem_2"}], 200),
    }

    def execute_one(_store, _api_key, action, *, runtime_token=""):
        marker = action["marker"]
        calls.append(marker)
        return outcomes[marker]

    monkeypatch.setattr(memory_actions, "_execute_memory_action", execute_one)
    markers = list(outcomes)
    body, status = memory_actions._execute_memory_actions(
        store, "api_key", [{"marker": marker} for marker in markers]
    )

    assert status == 200
    assert calls == markers
    assert body["status"] == "partial"
    assert body["applied_count"] == 2
    assert body["skipped_count"] == 1
    assert body["failed_count"] == 4
    assert "error" not in body and "detail" not in body
    assert [effect["memory_id"] for effect in body["effects"]] == [
        "mem_1", "mem_2"
    ]
    assert [row["http_status"] for row in body["results"]] == [
        200, 400, 404, 403, 400, 200, 200
    ]


@pytest.mark.parametrize("count", [1, 2])
def test_memory_batch_all_skipped_stays_200(monkeypatch, count):
    store = types.SimpleNamespace(user_id="usr_v1")
    monkeypatch.setattr(
        memory_actions,
        "_execute_memory_action",
        lambda *_args, **_kwargs: (
            {"status": "ok", "noop": True, "skipped": "duplicate_active"},
            [],
            200,
        ),
    )

    body, status = memory_actions._execute_memory_actions(
        store, "api_key", [{"marker": i} for i in range(count)]
    )

    assert status == 200
    assert body["status"] == "ok"
    assert body["applied_count"] == body["failed_count"] == 0
    assert body["skipped_count"] == count
    assert "error" not in body and "detail" not in body


def test_memory_batch_skipped_plus_failed_without_apply_returns_400(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    outcomes = iter([
        ({"status": "ok", "noop": True, "skipped": "duplicate_active"}, [], 200),
        ({"status": "error", "error": "not_found", "detail": {"id": "missing"}}, [], 404),
    ])
    monkeypatch.setattr(
        memory_actions,
        "_execute_memory_action",
        lambda *_args, **_kwargs: next(outcomes),
    )

    body, status = memory_actions._execute_memory_actions(
        store, "api_key", [{"marker": "skip"}, {"marker": "fail"}]
    )

    assert status == 400
    assert body["status"] == "partial"
    assert body["error"] == "not_found"
    assert body["detail"] == {"id": "missing"}
    assert body["applied_count"] == 0
    assert body["skipped_count"] == body["failed_count"] == 1
    assert [row["http_status"] for row in body["results"]] == [200, 404]


def test_memory_add_skips_normalized_duplicate_but_keeps_distinct_content(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    moments: list[dict] = []
    saved = _install_memory_action_fakes(monkeypatch, moments)
    monkeypatch.setattr(
        memory_actions,
        "_memory_plain_from_envelope",
        lambda moment, _api_key, runtime_token="": (
            json.loads(moment["body_ct"]),
            "",
        ),
    )

    def add(summary: str, content: str):
        return memory_actions._execute_memory_actions(store, "api_key", [{
            "type": "memory.add",
            "memory": {
                "summary": summary,
                "content": content,
                "source": "chat",
            },
        }])

    first, first_status = add("Coffee Preference", "Likes oat milk.")
    duplicate, duplicate_status = add(
        "  Ｃｏｆｆｅｅ   Preference ",
        "LIKES   OAT MILK.",
    )
    distinct, distinct_status = add(
        "Coffee Preference",
        "Likes espresso without milk.",
    )

    assert first_status == duplicate_status == distinct_status == 200
    assert first["results"][0]["status"] == "ok"
    assert duplicate["results"][0]["skipped"] == "duplicate_active"
    assert duplicate["results"][0]["noop"] is True
    assert duplicate["effects"] == []
    assert distinct["results"][0].get("skipped") is None
    assert len(saved) == 2


def test_backend_envelope_adapter_normalizes_only_plaintext_fields():
    old_doc = {
        "id": "mem_old",
        "type": "fact",
        "occurred_at": "2026-06-20T10:00:00",
        "source": "hosted_runtime_state",
        "body_ct": "encrypted-inner",
        "nonce": "nonce",
        "K_user": "ku",
        "K_enclave": "ke",
        "visibility": "shared",
        "owner_user_id": "usr_v1",
        "salience": "high",
        "importance": 0.8,
    }

    adapted = memory_service.to_v1_card(old_doc)

    assert adapted["body_ct"] == "encrypted-inner"
    assert adapted["importance"] == 0.8
    assert adapted["pulse"] == 0.3
    assert adapted["last_referenced_at"] == "2026-06-20T10:00:00"
    assert adapted["status"] == "active"
    assert "bucket" not in adapted
    assert "threads" not in adapted


def test_enclave_inner_adapter_maps_old_inner_to_v1_content_bucket_threads():
    adapted = readside.memory_inner_to_v1(
        {
            "summary": "用户有只猫叫武松。",
            "description": "用户有只猫叫武松，是狸花猫。",
            "her_quote": "我有只猫叫武松，是狸花猫。",
            "linked_dimension": "武松",
        },
        {"type": "quote"},
    )

    assert adapted["summary"] == "用户有只猫叫武松。"
    assert adapted["bucket"] == "我们的关系"
    assert adapted["threads"] == ["武松"]
    assert adapted["content"] == (
        "记忆: 用户有只猫叫武松，是狸花猫。\n"
        "上下文: 我有只猫叫武松，是狸花猫。\n"
        "使用提示: 自然使用这条记忆，不要机械复述。"
    )
    for legacy_key in ("title", "description", "her_quote", "verbatim", "linked_dimension"):
        assert legacy_key not in adapted


def test_memory_create_alias_writes_clean_v1_add(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    saved = _install_memory_action_fakes(monkeypatch, [])

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.create",
            "memory": {
                "summary": "用户喜欢先看地图再看路线。",
                "content": "记忆: 用户喜欢先看地图再看路线。\n上下文: 用户多次提出。\n使用提示: 解释复杂系统时先给结构图。",
                "bucket": "协作方式",
                "threads": ["解释偏好"],
            },
        }
    ])

    assert status == 200
    assert body["results"][0]["action"] == "memory.add"
    assert json.loads(saved[0]["body_ct"])["bucket"] == "协作方式"


def test_memory_retype_updates_type_in_v1_actions(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    moments = [{
        "id": "mem_any",
        "owner_user_id": "usr_v1",
        "type": "event",
        "status": "active",
    }]
    saved = _install_memory_action_fakes(monkeypatch, moments)

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {"type": "memory.retype", "memory_id": "mem_any", "new_type": "fact"}
    ])

    assert status == 200
    assert body["status"] == "ok"
    assert body["results"][0]["action"] == "memory.retype"
    assert saved[0]["type"] == "fact"


def test_memory_patch_becomes_supersede_and_inherits_old_bucket_threads(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_v1")
    old = {
        "v": 1,
        "id": "mem_old_dog",
        "owner_user_id": "usr_v1",
        "visibility": "shared",
        "body_ct": json.dumps({
            "summary": "用户有只狗叫蛋子。",
            "content": "记忆: 用户有只狗叫蛋子。\n上下文: 用户明确说过。\n使用提示: 宠物话题自然使用。",
            "bucket": "宠物",
            "threads": ["蛋子", "狗狗"],
        }),
        "nonce": "nonce_old",
        "K_user": "ku_old",
        "K_enclave": "ke_old",
        "enclave_pk_fpr": "fpr_test",
        "occurred_at": "2026-06-20",
        "created_at": "2026-06-20",
        "updated_at": "2026-06-20",
        "source": "chat",
        "status": "active",
        "importance": 0.7,
        "pulse": 0.4,
    }
    moments = [old]
    saved = _install_memory_action_fakes(monkeypatch, moments)
    decrypt_tokens = []

    def fake_plain(moment, _api_key, runtime_token=""):
        decrypt_tokens.append(runtime_token)
        return json.loads(moment["body_ct"]), ""

    monkeypatch.setattr(memory_actions, "_memory_plain_from_envelope", fake_plain)

    body, status = memory_actions._execute_memory_actions(store, "api_key", [
        {
            "type": "memory.patch",
            "memory_id": "mem_old_dog",
            "patch": {
                "summary": "蛋子是一只比熊，屁股上有胎记。",
                "content": "记忆: 蛋子是一只比熊，屁股上有胎记。\n上下文: 用户纠正并补充。\n使用提示: 问到蛋子时用新事实。",
            },
        }
    ], runtime_token="rt-hosted")

    assert status == 200
    assert body["results"][0]["action"] == "memory.supersede"
    old_after = next(moment for moment in saved if moment["id"] == "mem_old_dog")
    new_card = next(moment for moment in saved if moment["id"] != "mem_old_dog")
    assert old_after["status"] == "superseded"
    inner = json.loads(new_card["body_ct"])
    assert inner["bucket"] == "宠物"
    assert inner["threads"] == ["蛋子", "狗狗"]
    assert inner["summary"] == "蛋子是一只比熊，屁股上有胎记。"
    assert decrypt_tokens == ["rt-hosted"]
