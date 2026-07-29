"""DB 版端到端:脏卡打真实 /v1/memory/actions 端点 → 400 memory_card_polluted。

这条补上纯单测覆盖不到的部分:请求真的过了路由 + auth + memory_core + actions 层
(带真实 Postgres)。脏卡在 guard 处就 return 400,在封信封【之前】—— 所以不需要 enclave
stub,是最轻的真实路径验证。需要 Postgres(conftest 会自动 provision);无库则整文件不收集。
"""
from __future__ import annotations

import sys
import base64
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


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
    # 端点把 action 级 error 聚合返回;断言拿到的是 pollution 拒绝而非别的 400。
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
