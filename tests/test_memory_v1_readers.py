from __future__ import annotations
import threading

import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from accounts import registry  # noqa: E402
from hosted import history_import as history_import  # noqa: E402
from bootstrap import gates as boot_gates  # noqa: E402
from identity import service as identity_service  # noqa: E402
from memory import memory_core  # noqa: E402
from memory import service as memory_service  # noqa: E402
from proactive import tool_executor_v2  # noqa: E402
from urllib.parse import parse_qs, urlparse  # noqa: E402


# The Flask /v1/memory/* readside routes were deleted in the ASGI cutover; this
# tiny client mimics the old Flask test-client response shape while routing to the
# framework-neutral memory_core the ASGI routes delegate to (buckets/threads fall
# back to _load_moments via a no-op post-enclave, matching the deleted route).
def _fallback_post_enclave(api_key, candidates, *, operation, payload=None):
    return {}


class _Resp:
    def __init__(self, body, status=200):
        self.status_code = status
        self._body = body

    def get_json(self):
        return self._body


class _CoreClient:
    def __init__(self, store, api_key="k_v1"):
        self._store = store
        self._api_key = api_key

    def get(self, path):
        u = urlparse(path)
        p = u.path
        q = parse_qs(u.query)
        one = lambda k: (q.get(k) or [None])[0]  # noqa: E731
        if p == "/v1/memory/buckets":
            return _Resp(*memory_core.buckets(self._store, self._api_key, post_enclave=_fallback_post_enclave))
        if p == "/v1/memory/threads":
            return _Resp(*memory_core.threads(self._store, self._api_key, post_enclave=_fallback_post_enclave))
        if p == "/v1/memory/list":
            return _Resp(*memory_core.list_moments(
                self._store, limit_raw=one("limit"), since=one("since") or "0",
                include_archived_raw=one("include_archived"),
            ))
        if p == "/v1/memory/verify":
            return _Resp(*memory_core.verify(self._store))
        raise AssertionError(f"unrouted path: {p}")


def test_history_import_persists_v1_memory_body(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_import", memory_lock=threading.RLock())
    saved: list[dict] = []
    monkeypatch.setattr(history_import.memory_service, "_load_moments", lambda _store: [])
    monkeypatch.setattr(history_import.memory_service, "_save_moments", lambda _store, moments: saved.extend(moments))
    monkeypatch.setattr(history_import.boot_gates, "_log_bootstrap_event", lambda *args, **kwargs: None)

    def fake_envelope(store_arg, payload, *, item_id=None):
        return {
            "id": item_id or "mem_import",
            "body_ct": payload.decode("utf-8"),
            "nonce": "nonce",
            "K_user": "ku",
            "K_enclave": "ke",
            "enclave_pk_fpr": "fpr",
            "visibility": "shared",
            "owner_user_id": store_arg.user_id,
        }, ""

    monkeypatch.setattr(history_import.core_envelope, "_build_shared_envelope_for_store", fake_envelope)

    created = history_import._append_import_memory_cards(store, [{
        "summary": "用户喜欢先看地图。",
        "content": "记忆: 用户喜欢先看地图。\n上下文: 导入材料。\n使用提示: 解释时先给结构。",
        "bucket": "协作方式",
        "threads": ["解释偏好"],
        "importance": 0.7,
        "pulse": 0.4,
        "occurred_at": "2026-06-20",
    }])

    assert len(created) == 1
    moment = created[0]
    inner = json.loads(moment["body_ct"])
    assert inner == {
        "summary": "用户喜欢先看地图。",
        "content": "记忆: 用户喜欢先看地图。\n上下文: 导入材料。\n使用提示: 解释时先给结构。",
        "bucket": "协作方式",
        "threads": ["解释偏好"],
    }
    assert "type" not in moment
    assert moment["importance"] == 0.7
    assert moment["pulse"] == 0.4


def test_v1_memory_count_does_not_require_legacy_type_tabs():
    counts = memory_service._count_by_tab([
        {"id": "a", "status": "active", "bucket": "宠物"},
        {"id": "b", "status": "active", "bucket": "工作"},
        {"id": "c", "is_archived": True, "bucket": "旧事"},
    ])

    assert counts == {"story": 2, "about_me": 2, "ta_thinking": 2, "total": 2}


def test_proactive_memory_index_item_shims_v1_card_to_legacy_shape():
    item = tool_executor_v2._memory_index_item({
        "id": "mem_v1",
        "summary": "用户有只猫叫武松。",
        "bucket": "宠物",
        "occurred_at": "2026-06-20",
    })

    assert item["id"] == "mem_v1"
    assert item["title"] == "用户有只猫叫武松。"
    assert item["type"] == "宠物"


def test_memory_bucket_and_thread_endpoints_return_existing_terms(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_terms")
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [
        {"id": "a", "status": "active", "bucket": "宠物", "threads": ["蛋子", "狗狗"]},
        {"id": "b", "status": "active", "bucket": "工作", "threads": ["上线"]},
        {"id": "c", "status": "superseded", "bucket": "旧桶", "threads": ["旧线"]},
    ])
    client = _CoreClient(store)
    buckets = client.get("/v1/memory/buckets").get_json()
    threads = client.get("/v1/memory/threads").get_json()

    assert buckets == {"buckets": ["宠物", "工作"]}
    assert threads == {"threads": ["上线", "狗狗", "蛋子"]}


def test_memory_list_returns_clean_v1_cards_without_legacy_type(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_list")
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [
        {
            "id": "mem_v1",
            "status": "active",
            "summary": "server-visible summary should not be required",
            "occurred_at": "2026-06-20T10:00:00",
            "created_at": "2026-06-20T10:00:00",
            "body_ct": "encrypted",
            "owner_user_id": "usr_list",
        }
    ])
    client = _CoreClient(store)
    response = client.get("/v1/memory/list?limit=20")

    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == 1
    assert body["moments"][0]["id"] == "mem_v1"
    assert "type" not in body["moments"][0]


def test_memory_verify_degrades_for_clean_v1_cards(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_verify")
    monkeypatch.setattr(identity_service, "_relationship_age_days", lambda _store: 1)
    monkeypatch.setattr(memory_service, "_per_tab_floors_for_days", lambda _days: {
        "story": 1,
        "about_me": 1,
        "ta_thinking": 0,
        "total": 2,
    })
    monkeypatch.setattr(registry, "_get_user_archive_language", lambda _uid: "")
    monkeypatch.setattr(memory_service, "_load_moments", lambda _store: [
        {"id": "mem_v1", "status": "active", "bucket": "宠物", "occurred_at": "2026-06-20T10:00:00"}
    ])
    client = _CoreClient(store)
    response = client.get("/v1/memory/verify")

    assert response.status_code == 200
    body = response.get_json()
    assert body["counts"] == {"story": 1, "about_me": 1, "ta_thinking": 1, "total": 1}
    assert body["passing"] is True


def test_bootstrap_gate_uses_v1_count_shim_without_legacy_tabs(monkeypatch):
    store = types.SimpleNamespace(user_id="usr_bootstrap")
    monkeypatch.setattr(boot_gates.memory_service, "_load_moments", lambda _store: [
        {"id": "mem_v1", "status": "active", "bucket": "宠物"},
    ])
    monkeypatch.setattr(boot_gates.memory_service, "_memory_floor_for_days", lambda _days: 2)
    monkeypatch.setattr(boot_gates.identity_service, "_relationship_age_days", lambda _store: 1)
    monkeypatch.setattr(boot_gates.identity_service, "_load_identity", lambda _store: {"id": "identity"})

    state = boot_gates._bootstrap_state(store)

    assert state["stage"] == "main_loop"
    assert state["memory_count"] == 1
    assert state["memory_floor"] == 2
    assert "counts" not in state
    assert "floors" not in state
    assert "missing_tabs" not in state
