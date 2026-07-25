import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from core import provider_usage as pu


def test_select_adapter_official_origins():
    assert pu.select_adapter("deepseek", "") == "deepseek"
    assert pu.select_adapter("deepseek", "https://api.deepseek.com") == "deepseek"
    assert pu.select_adapter("deepseek", "https://api.deepseek.com/") == "deepseek"
    assert pu.select_adapter("openrouter", "https://openrouter.ai/api/v1") == "openrouter"
    # 命名 provider + 自定义地址 → 不许把 key 发去非官方 origin
    assert pu.select_adapter("deepseek", "https://evil.example.com") == "unsupported"
    assert pu.select_adapter("openrouter", "https://relay.example.com/api/v1") == "unsupported"
    assert pu.select_adapter("openai_compatible", "https://relay.example.com/v1") == "relay"
    assert pu.select_adapter("openai", "") == "unsupported"
    assert pu.select_adapter("anthropic", "") == "unsupported"


def test_build_payload_status_derivation():
    m = pu._empty_metrics()
    p = pu.build_payload("openai", "unsupported", m, error="usage_unsupported_provider")
    assert p["status"] == "error"
    assert p["error"] == "usage_unsupported_provider"
    assert set(p["metrics"].keys()) == {"balance", "remaining", "usage_total", "usage_today", "usage_month"}

    m2 = pu._empty_metrics()
    m2["balance"] = pu._metric("ok", amounts=[{"amount": 25.06, "unit": "CNY"}], scope="account")
    p2 = pu.build_payload("deepseek", "deepseek_balance", m2)
    assert p2["status"] == "ok"          # balance ok，其余 unsupported 不拖累总状态
    assert p2["provider"] == "deepseek"
    assert "as_of" in p2 and "error" not in p2


import asyncio, json


class FakeResponse:
    """既支持旧的 .json()/.content 直接访问，也支持新的 stream() aiter_bytes 协议。"""
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.content = json.dumps(self._body).encode()
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body

    async def aiter_bytes(self):
        # 模拟真实分块传输：切成小块，逼真化 running-cap 的累加路径
        data = self.content
        chunk_size = 7
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeAsyncClient:
    """按 URL 后缀路由的假 httpx.AsyncClient。routes: dict[包含子串, FakeResponse|Exception]"""
    def __init__(self, routes):
        self.routes = routes
        self.requests = []  # (url, headers)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, headers=None, timeout=None, follow_redirects=None):
        self.requests.append((url, dict(headers or {})))
        for frag, resp in self.routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    return _RaisingStreamCtx(resp)
                return resp
        return FakeResponse(404, {})


class _RaisingStreamCtx:
    """`async with client.stream(...)` 时抛异常（连接错误/超时等）的假上下文管理器。"""
    def __init__(self, exc):
        self._exc = exc

    async def __aenter__(self):
        raise self._exc

    async def __aexit__(self, *a):
        return False


def _cfg(provider, base_url=""):
    import provider_client as pc
    return pc.ProviderConfig(provider=provider, model="m", api_key="sk-secret-XYZ", base_url=base_url)


def _run(coro):
    return asyncio.run(coro)


def test_deepseek_balance_multi_currency(monkeypatch):
    fake = FakeAsyncClient({"/user/balance": FakeResponse(200, {
        "is_available": True,
        "balance_infos": [{"currency": "CNY", "total_balance": "25.06"},
                          {"currency": "USD", "total_balance": "1.00"}]})})
    monkeypatch.setattr(pu, "_make_client", lambda: fake)
    p = _run(pu.query_usage_async(_cfg("deepseek")))
    bal = p["metrics"]["balance"]
    assert bal["status"] == "ok"
    assert bal["amounts"] == [{"amount": "25.06", "unit": "CNY"}, {"amount": "1.00", "unit": "USD"}]
    assert p["metrics"]["usage_today"]["status"] == "unsupported"
    assert p["status"] == "ok"
    # key 只进 Authorization header，绝不进 payload
    assert "sk-secret" not in json.dumps(p)
    assert fake.requests[0][1]["Authorization"] == "Bearer sk-secret-XYZ"


def test_openrouter_full(monkeypatch):
    fake = FakeAsyncClient({
        "/api/v1/credits": FakeResponse(200, {"data": {"total_credits": 220, "total_usage": 167.509}}),
        "/api/v1/key": FakeResponse(200, {"data": {
            "limit_remaining": 97.13, "usage_daily": 0.053, "usage_monthly": 52.907}}),
    })
    monkeypatch.setattr(pu, "_make_client", lambda: fake)
    p = _run(pu.query_usage_async(_cfg("openrouter")))
    assert p["status"] == "ok"
    assert p["metrics"]["balance"]["amounts"] == [{"amount": 52.491, "unit": "USD"}]
    assert p["metrics"]["balance"]["scope"] == "account"
    # OpenRouter 的 limit_remaining 是自设消费上限、非账户余额，会与「余额」并排误导，
    # 故不再展示 → 恒为 unsupported（中转站的 remaining 语义不同，另测保留）。
    assert p["metrics"]["remaining"]["status"] == "unsupported"
    assert p["metrics"]["usage_today"] == {"status": "ok", "amount": 0.053, "unit": "USD", "timezone": "UTC"}
    assert p["metrics"]["usage_month"]["amount"] == 52.907
    assert p["metrics"]["usage_total"]["status"] == "unsupported"


def test_openrouter_partial_failure(monkeypatch):
    fake = FakeAsyncClient({
        "/api/v1/credits": FakeResponse(500, {}),
        "/api/v1/key": FakeResponse(200, {"data": {"limit_remaining": None,
                                                    "usage_daily": 1.0, "usage_monthly": 2.0}}),
    })
    monkeypatch.setattr(pu, "_make_client", lambda: fake)
    p = _run(pu.query_usage_async(_cfg("openrouter")))
    assert p["status"] == "partial"
    assert p["metrics"]["balance"]["status"] == "failed"
    assert p["metrics"]["balance"]["reason"] == "usage_http_500"
    assert p["metrics"]["remaining"]["status"] == "unsupported"  # OpenRouter 不再展示 key 限额
    assert p["metrics"]["usage_today"]["status"] == "ok"


def test_relay_newapi_token_endpoint(monkeypatch):
    fake = FakeAsyncClient({"/api/usage/token": FakeResponse(200, {
        "code": True, "data": {"total_granted": 100, "total_used": 40,
                                "total_available": 60, "unlimited_quota": False}})})
    monkeypatch.setattr(pu, "_make_client", lambda: fake)
    monkeypatch.setattr(pu.net_safety, "blocked_url_kind", lambda url, **k: None)
    p = _run(pu.query_usage_async(_cfg("openai_compatible", "https://relay.example.com/v1")))
    assert p["adapter"] == "relay_token"
    assert p["metrics"]["remaining"] == {"status": "ok", "amount": 60, "unit": "quota", "scope": "api_key"}
    assert p["metrics"]["usage_total"]["amount"] == 40
    assert p["metrics"]["usage_today"]["status"] == "unsupported"
    # 账单 URL 必须同 origin
    assert all(u.startswith("https://relay.example.com/") for u, _ in fake.requests)


def test_relay_dashboard_fallback(monkeypatch):
    fake = FakeAsyncClient({
        "/api/usage/token": FakeResponse(404, {}),
        "/dashboard/billing/subscription": FakeResponse(200, {"hard_limit_usd": 21.35}),
        "/dashboard/billing/usage": FakeResponse(200, {"total_usage": 535.0}),  # 分 ×100
    })
    monkeypatch.setattr(pu, "_make_client", lambda: fake)
    monkeypatch.setattr(pu.net_safety, "blocked_url_kind", lambda url, **k: None)
    p = _run(pu.query_usage_async(_cfg("openai_compatible", "https://relay.example.com/v1")))
    assert p["adapter"] == "relay_dashboard"
    assert p["metrics"]["usage_total"] == {"status": "ok", "amount": 5.35, "unit": "unknown"}
    assert p["metrics"]["remaining"]["amount"] == 15.999999999999998 or \
           p["metrics"]["remaining"]["amount"] == 16.0  # hard_limit - used, round 6 → 16.0
    assert p["metrics"]["remaining"]["unit"] == "unknown"


def test_relay_blocked_private_url(monkeypatch):
    fake = FakeAsyncClient({})
    monkeypatch.setattr(pu, "_make_client", lambda: fake)
    monkeypatch.setattr(pu.net_safety, "blocked_url_kind", lambda url, **k: "blocked_url")
    monkeypatch.delenv("FEEDLING_PROVIDER_USAGE_ALLOW_PRIVATE", raising=False)
    p = _run(pu.query_usage_async(_cfg("openai_compatible", "https://10.0.0.5/v1")))
    assert p["status"] == "error"
    assert p["error"] == "usage_blocked_url"
    assert fake.requests == []  # 一个请求都不许发


def test_timeout_maps_to_slug(monkeypatch):
    import httpx as _httpx
    fake = FakeAsyncClient({"/user/balance": _httpx.TimeoutException("t")})
    monkeypatch.setattr(pu, "_make_client", lambda: fake)
    p = _run(pu.query_usage_async(_cfg("deepseek")))
    assert p["metrics"]["balance"]["reason"] == "usage_timeout"
    assert p["status"] == "error"
