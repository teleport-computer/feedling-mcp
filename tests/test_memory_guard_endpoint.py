"""DB 版端到端:脏卡打真实 /v1/memory/actions 端点 → 逐项 memory_card_polluted。

这条补上纯单测覆盖不到的部分:请求真的过了路由 + auth + memory_core + actions 层
(带真实 Postgres)。脏卡在 guard 处就 return 400,在封信封【之前】—— 所以不需要 enclave
stub,是最轻的真实路径验证。需要 Postgres(conftest 会自动 provision);无库则整文件不收集。
"""
from __future__ import annotations

import sys
import base64
import json
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
import db  # noqa: E402
from memory import actions as memory_actions  # noqa: E402


@pytest.fixture()
def api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(b"\x22" * 32).decode(), "archive_language": "zh"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["api_key"]


def _post_actions(api_key: str, memory: dict):
    res = make_client().post(
        "/v1/memory/actions",
        headers={"X-API-Key": api_key},
        json={"actions": [{"type": "memory.add", "memory": memory}]},
    )
    return res.status_code, res.get_json(silent=True)


def test_polluted_summary_rejected_at_endpoint(api_key):
    status, body = _post_actions(api_key, {
        "type": "fact",
        "summary": "analysis to=functions.memory_write",   # 通道前缀+route(强证据)
        "content": "正常的一段正文内容",
        "title": "analysis to=functions.memory_write",
    })
    assert status == 400, body
    assert body["status"] == "failed"
    assert body["error"] == "memory_card_polluted"
    # 全败批次恢复外层 400，同时保留逐项 pollution 拒绝。
    assert "memory_card_polluted" in str(body), body


def test_clean_card_passes_the_guard(api_key):
    # 干净卡不会被 guard 判 memory_card_polluted(它会继续往下走,可能因本测无 enclave 在封
    # 信封处失败 —— 那也证明 guard 放行了)。关键断言:错误不是 memory_card_polluted。
    status, body = _post_actions(api_key, {
        "type": "fact",
        "summary": "用户喜欢先看地图再看路线",
        "content": "记忆: 用户喜欢先看地图再看路线。上下文: 多次提出。",
        "title": "用户喜欢先看地图再看路线",
    })
    assert "memory_card_polluted" not in str(body), (status, body)


def test_write_validation_and_duplicate_skip_at_real_endpoint(api_key, monkeypatch):
    counter = {"n": 0}

    def build_envelope(store, inner, *, item_id=None):
        counter["n"] += 1
        memory_id = item_id or f"mem_endpoint_{counter['n']}"
        return {
            "id": memory_id,
            "body_ct": json.dumps(inner, ensure_ascii=False),
                "nonce": f"nonce_{memory_id}",
                "K_user": f"ku_{memory_id}",
                "K_enclave": f"ke_{memory_id}",
                "visibility": "shared",
            "owner_user_id": store.user_id,
        }, ""

    monkeypatch.setattr(
        memory_actions, "_build_memory_envelope_for_store", build_envelope
    )
    monkeypatch.setattr(
        memory_actions,
        "_memory_plain_from_envelope",
        lambda moment, _api_key, runtime_token="": (
            json.loads(moment["body_ct"]),
            "",
        ),
    )

    client = make_client()
    headers = {"X-API-Key": api_key}

    invalid_source = client.post(
        "/v1/memory/actions",
        headers=headers,
        json={"actions": [{
            "type": "memory.add",
            "memory": {
                "summary": "invalid source",
                "content": "invalid source body",
                "source": "对话",
            },
        }]},
    )
    assert invalid_source.status_code == 400
    assert invalid_source.get_json()["status"] == "failed"
    assert invalid_source.get_json()["error"] == "source_invalid"
    assert invalid_source.get_json()["results"][0]["error"] == "source_invalid"

    invalid_mode = client.post(
        "/v1/memory/actions",
        headers=headers,
        json={"actions": [{
            "type": "memory.add",
            "memory": {
                "summary": "invalid mode",
                "content": "invalid mode body",
                "source": "chat",
            },
            "capture_mode": "conversation_2026",
        }]},
    )
    assert invalid_mode.status_code == 400
    assert invalid_mode.get_json()["status"] == "failed"
    assert invalid_mode.get_json()["error"] == "capture_mode_invalid"
    assert invalid_mode.get_json()["results"][0]["error"] == "capture_mode_invalid"

    def post(summary: str, content: str):
        return client.post(
            "/v1/memory/actions",
            headers=headers,
            json={"actions": [{
                "type": "memory.add",
                "memory": {
                    "summary": summary,
                    "content": content,
                    "source": "chat",
                },
                "capture_mode": "memory_capture",
            }]},
        )

    first = post("Coffee Preference", "Likes oat milk.")
    duplicate = post(" Ｃｏｆｆｅｅ  Preference ", "LIKES   OAT MILK.")
    distinct = post("Coffee Preference", "Likes espresso.")

    assert first.status_code == 200, first.get_data(as_text=True)
    assert duplicate.status_code == 200, duplicate.get_data(as_text=True)
    assert duplicate.get_json()["results"][0]["skipped"] == "duplicate_active"
    assert duplicate.get_json()["effects"] == []
    assert distinct.status_code == 200, distinct.get_data(as_text=True)

    store = core_store.get_store(registry._resolve_user(api_key))
    active = memory_actions.memory_service._active_memory_moments(
        memory_actions.memory_service._load_moments(store)
    )
    assert len(active) == 2


def test_supersede_target_errors_keep_item_level_400_404_and_403(api_key):
    client = make_client()
    headers = {"X-API-Key": api_key}
    memory = {
        "summary": "Corrected fact",
        "content": "Corrected fact body",
        "source": "hosted_runtime_state",
    }

    no_target = client.post(
        "/v1/memory/actions",
        headers=headers,
        json={"actions": [{
            "type": "memory.supersede",
            "memory": memory,
            "capture_mode": "state",
        }]},
    )
    assert no_target.status_code == 400
    assert no_target.get_json()["error"] == "supersedes_required"
    assert no_target.get_json()["results"][0]["http_status"] == 400
    assert no_target.get_json()["results"][0]["error"] == "supersedes_required"

    missing = client.post(
        "/v1/memory/actions",
        headers=headers,
        json={"actions": [{
            "type": "memory.supersede",
            "supersedes": "mem_missing",
            "memory": memory,
            "capture_mode": "state",
        }]},
    )
    assert missing.status_code == 400
    assert missing.get_json()["error"] == "not_found"
    assert missing.get_json()["results"][0]["http_status"] == 404
    assert missing.get_json()["results"][0]["error"] == "not_found"

    user_id = registry._resolve_user(api_key)
    db.memory_upsert(user_id, "mem_other_owner", "2026-07-29T00:00:00Z", {
        "id": "mem_other_owner",
        "type": "fact",
        "status": "active",
        "occurred_at": "2026-07-29T00:00:00Z",
        "owner_user_id": "usr_someone_else",
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "wrapped",
        "visibility": "shared",
    })
    not_owned = client.post(
        "/v1/memory/actions",
        headers=headers,
        json={"actions": [{
            "type": "memory.supersede",
            "supersedes": "mem_other_owner",
            "memory": memory,
            "capture_mode": "state",
        }]},
    )
    assert not_owned.status_code == 400
    assert not_owned.get_json()["error"] == "not_owned"
    assert not_owned.get_json()["results"][0]["http_status"] == 403
    assert not_owned.get_json()["results"][0]["error"] == "not_owned"


def test_mixed_batch_continues_after_bad_card_and_keeps_success_effects(
    api_key, monkeypatch
):
    counter = {"n": 0}

    def build_envelope(store, inner, *, item_id=None):
        counter["n"] += 1
        memory_id = item_id or f"mem_batch_{counter['n']}"
        return {
            "id": memory_id,
            "body_ct": json.dumps(inner, ensure_ascii=False),
            "nonce": f"nonce_{memory_id}",
            "K_user": f"ku_{memory_id}",
            "K_enclave": f"ke_{memory_id}",
            "visibility": "shared",
            "owner_user_id": store.user_id,
        }, ""

    monkeypatch.setattr(
        memory_actions, "_build_memory_envelope_for_store", build_envelope
    )
    response = make_client().post(
        "/v1/memory/actions",
        headers={"X-API-Key": api_key},
        json={"actions": [
            {
                "type": "memory.add",
                "memory": {
                    "summary": "good one",
                    "content": "first valid card",
                    "source": "chat",
                },
            },
            {
                "type": "memory.add",
                "memory": {
                    "summary": "bad source",
                    "content": "this item must fail alone",
                    "source": "conversation",
                },
            },
            {
                "type": "memory.add",
                "memory": {
                    "summary": "good two",
                    "content": "second valid card",
                    "source": "chat",
                },
            },
        ]},
    )

    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "partial"
    assert body["total_count"] == 3
    assert body["applied_count"] == 2
    assert body["skipped_count"] == 0
    assert body["failed_count"] == 1
    assert [row["status"] for row in body["results"]] == ["ok", "error", "ok"]
    assert body["results"][1]["error"] == "source_invalid"
    assert body["results"][1]["http_status"] == 400
    assert len(body["effects"]) == 2
    assert {
        effect["memory_id"] for effect in body["effects"]
    } == {"mem_batch_1", "mem_batch_2"}


def test_tombstone_supersede_rejected_at_endpoint_and_old_card_stays_active(api_key):
    """公开路由契约(2026-08-06 usr_a40e 徒手 patch 潮):明文 supersede 带
    「已被 <卡id> 取代」墓碑注记 → 400 memory_card_tombstone,且旧卡保持 active。"""
    client = make_client()
    headers = {"X-API-Key": api_key}
    user_id = registry._resolve_user(api_key)
    db.memory_upsert(user_id, "mem_tomb_target", "2026-08-01T00:00:00Z", {
        "id": "mem_tomb_target",
        "type": "fact",
        "status": "active",
        "occurred_at": "2026-08-01T00:00:00Z",
        "owner_user_id": user_id,
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "wrapped",
        "visibility": "shared",
    })

    tomb = "已被 c42ebb9618ae447df9d52107ea15de85 取代——绿豆汤偏好"
    res = client.post(
        "/v1/memory/actions",
        headers=headers,
        json={"actions": [{
            "type": "memory.supersede",
            "supersedes": "mem_tomb_target",
            "memory": {"type": "fact", "title": tomb, "summary": tomb, "content": tomb},
            "capture_mode": "state",
        }]},
    )
    assert res.status_code == 400, res.get_json(silent=True)
    body = res.get_json()
    assert body["error"] == "memory_card_tombstone"
    assert body["results"][0]["error"] == "memory_card_tombstone"

    # 旧卡必须还活着 —— 拒收发生在退休之前。
    moments = db.memory_load(user_id)
    target = next(m for m in moments if m.get("id") == "mem_tomb_target")
    assert str(target.get("status") or "active") == "active"
