"""account_reset (POST /v1/account/reset) 必须清理该用户的 onboarding R2 归档。

行为测试：patch onboarding_archive.storage.delete_user_archives 为记录器，
走真实重置路由，断言它被以正确 user_id 调到。
"""

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
import onboarding_archive.storage as oa_storage  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


_pk_counter = itertools.count(1)


def _register(client) -> tuple[str, str]:
    import base64
    raw = next(_pk_counter).to_bytes(32, "big")
    res = client.post(
        "/v1/users/register",
        json={"public_key": base64.b64encode(raw).decode(), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def test_reset_purges_onboarding_archives_when_enabled(client, monkeypatch):
    uid, api_key = _register(client)
    calls = []
    monkeypatch.setattr(oa_storage, "enabled", lambda: True)
    monkeypatch.setattr(oa_storage, "delete_user_archives", lambda u: calls.append(u))
    res = client.post("/v1/account/reset",
                      json={"confirm": "delete-all-data"},
                      headers={"X-API-Key": api_key})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert calls == [uid]


def test_reset_skips_archive_cleanup_when_disabled(client, monkeypatch):
    uid, api_key = _register(client)
    calls = []
    monkeypatch.setattr(oa_storage, "enabled", lambda: False)
    monkeypatch.setattr(oa_storage, "delete_user_archives", lambda u: calls.append(u))
    res = client.post("/v1/account/reset",
                      json={"confirm": "delete-all-data"},
                      headers={"X-API-Key": api_key})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert calls == []  # enabled() False → 不调用


def test_reset_aborts_when_archive_cleanup_fails_persistently(client, monkeypatch):
    """onboarding 归档是 R2 上的**明文**用户数据 —— 删账号前必须先清干净。持续
    删除失败时重置必须 503 abort 且**不删账号**（此刻尚未删任何东西，状态一致、
    可安全重试），绝不能在报告成功的同时留下无从发现的明文孤儿（无 reaper 兜底）。"""
    uid, api_key = _register(client)
    import content.content_core as routes
    monkeypatch.setattr(routes, "_ARCHIVE_DELETE_BASE_DELAY", 0)  # 测试里不真 sleep

    def _boom(_u):
        raise RuntimeError("r2 delete failed")

    monkeypatch.setattr(oa_storage, "enabled", lambda: True)
    monkeypatch.setattr(oa_storage, "delete_user_archives", _boom)
    res = client.post("/v1/account/reset",
                      json={"confirm": "delete-all-data"},
                      headers={"X-API-Key": api_key})
    assert res.status_code == 503, res.get_data(as_text=True)
    assert res.get_json()["error"] == "archive_cleanup_failed"

    # 账号未被删：修好归档后同一 key 仍能成功重置（证明账号一直在、可重试）。
    calls = []
    monkeypatch.setattr(oa_storage, "delete_user_archives", lambda u: calls.append(u))
    res2 = client.post("/v1/account/reset",
                       json={"confirm": "delete-all-data"},
                       headers={"X-API-Key": api_key})
    assert res2.status_code == 200, res2.get_data(as_text=True)
    assert calls == [uid]


def test_reset_retries_transient_archive_failure_then_succeeds(client, monkeypatch):
    """瞬时 R2 抖动应被有界重试抹平：前两次失败、第三次成功 → 归档删净、账号正常删除。"""
    uid, api_key = _register(client)
    import content.content_core as routes
    monkeypatch.setattr(routes, "_ARCHIVE_DELETE_BASE_DELAY", 0)
    attempts = {"n": 0}

    def _flaky(_u):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient r2")
        # 第三次成功（无异常）

    monkeypatch.setattr(oa_storage, "enabled", lambda: True)
    monkeypatch.setattr(oa_storage, "delete_user_archives", _flaky)
    res = client.post("/v1/account/reset",
                      json={"confirm": "delete-all-data"},
                      headers={"X-API-Key": api_key})
    assert res.status_code == 200, res.get_data(as_text=True)
    assert attempts["n"] == 3  # 重试到第三次成功
