"""GET /v1/model_api/usage：REST 端点，路由层只做「解一次 key → 转交
provider_usage」的框架中立粘合（Task 3）。

Run: python3 -m pytest tests/test_model_api_usage_route.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from core import provider_usage  # noqa: E402
from hosted import usage_core  # noqa: E402
import provider_client  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _register(client, seed: int = 0x33) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(bytes([seed]) * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def test_usage_route_not_configured(client):
    # 没配置 model_api 时，走 config_store._load_runtime_provider_config 的
    # (None, error_dict) 分支——route 必须原样透传该 error dict，状态码 400。
    _uid, api_key = _register(client)
    r = client.get("/v1/model_api/usage", headers=_headers(api_key))
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error"] == "model_api_not_configured"


def test_usage_route_happy_path(client, monkeypatch):
    # 解密和外呼都被替身：route 只负责把 resolve 出的 ProviderConfig 转交给
    # provider_usage.query_usage_async，再把返回的 payload 原样回给调用方。
    _uid, api_key = _register(client)
    cfg = provider_client.ProviderConfig(
        provider="deepseek", model="deepseek-chat", api_key="sk-x",
    )
    monkeypatch.setattr(usage_core, "resolve_usage_config", lambda store, api_key: cfg)

    async def fake_query(config):
        m = provider_usage._empty_metrics()
        m["balance"] = provider_usage._metric(
            "ok", amounts=[{"amount": "25.06", "unit": "CNY"}], scope="account",
        )
        return provider_usage.build_payload("deepseek", "deepseek_balance", m)

    monkeypatch.setattr(provider_usage, "query_usage_async", fake_query)

    r = client.get("/v1/model_api/usage", headers=_headers(api_key))
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    # unsupported 指标不计入 overall status——只有 balance 一项 ok，整体也是 ok。
    assert body["status"] == "ok"
    assert body["metrics"]["balance"]["amounts"][0]["unit"] == "CNY"
    assert "api_key" not in str(body)  # 明文 provider key 不能出现在响应体
