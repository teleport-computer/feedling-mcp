# Provider 额度状态观测 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户在 iOS 设置页（REST）或聊天里（V2 工具）查看自己 provider key 的余额/用量；现查现回，不落库。

**Architecture:** 底层新模块 `backend/core/provider_usage.py`（无业务依赖，只做三家账单 HTTP + 归一化，紧挨 provider_client.py 这层）。REST 入口 `GET /v1/model_api/usage` 走 hosted 层现有「解一次 key」路径。V2 侧作为 runtime-native 工具进静态目录（同 task/reply），wake lane 显式禁用、subagent 自动落入禁用集，chat lane 经 `_dispatch_mixed_tool_calls` 新分支 + 闭包复用本回合已解密的 `provider_config`，第三方 HTTP 在 enclave 信号量外执行。kill switch 复制 web_halted 双边界模式（目录摘除 + dispatch 活查）。

**Tech Stack:** FastAPI + httpx（async, `trust_env=False`）、alembic、pytest（hand-rolled fake httpx，无 respx）。

## Global Constraints（来自 spec v2，逐条照抄）

- key 只在内存过一遍：不进日志、不进返回体、不进错误信息（第三方错误体截断脱敏）。
- deepseek/openrouter 适配器只在 base_url 为官方 origin（规范化比较）时启用；命名 provider 配自定义 base_url → unsupported。
- 中转站账单请求与配置 base_url 同 origin、保留路径前缀；`follow_redirects=False`；不继承环境代理；响应体上限 256KB 流式读取。
- 总 wall deadline 6s；openrouter 两个接口并发。
- hosted 多租户下出站目标必须过 `net_safety.blocked_url_kind`（公网校验）；self-host 私网 relay 走 env 显式放行 `FEEDLING_PROVIDER_USAGE_ALLOW_PRIVATE=1`（默认关）。
- 工具只在 chat lane 提供；kill switch 默认 ON（= 不 halt，列 DEFAULT false，同 0050 语义）。
- CONTRIBUTING：core 函数框架中立；路由薄壳 `threadpool.run_db`；跨模块 `from pkg import module`；asgi_app.py 零 diff；新公开端点同 PR 更新 OpenAPI/changelog/API_ERRORS。
- 单位枚举：`USD / CNY / tokens / quota / unknown`，不猜单位。
- 金额原样透传 JSON 数值，唯一计算是 openrouter `balance = total_credits - total_usage`（round 6）。

## File Structure

| 文件 | 动作 | 职责 |
|---|---|---|
| `backend/core/provider_usage.py` | Create | 适配器选择、三家账单 HTTP、payload 归一化、出站安全。唯一的领域核心，REST 和 V2 共用 |
| `backend/hosted/usage_core.py` | Create | 框架中立薄胶水：解 key → 调核心（或错误透传） |
| `backend/hosted/setup_routes_asgi.py` | Modify | `GET /v1/model_api/usage` 路由（薄壳） |
| `backend/capabilities/tool_schema.py` | Modify | `PROVIDER_USAGE_TOOL` 常量 + PARAMS/DESCRIPTIONS + build_tool_specs 追加 |
| `backend/alembic/versions/0053_provider_usage_halted.py` | Create | `v2_runtime_control.provider_usage_halted BOOLEAN NOT NULL DEFAULT false` |
| `backend/model_api_runtime/v2/kill_switch.py` | Modify | `provider_usage_halted()` 读 + `set_provider_usage_halted()` 写（独立缓存槽） |
| `backend/model_api_runtime/v2/worker.py` | Modify | dispatch 分支 + 闭包、wake 禁用、_PRIVATE_READ_TOOLS、offer-time halt |
| `tests/test_provider_usage.py` | Create | 核心适配器/归一化/安全（纯单测，进 _PURE_UNIT） |
| `tests/test_model_api_usage_route.py` | Create | REST 路由（需 DB，走 make_client 惯例，不进 _PURE_UNIT） |
| `tests/test_v2_provider_usage_tool.py` | Create | 目录/lane/dispatch/kill switch（纯单测，进 _PURE_UNIT） |
| `tools/public_openapi_contracts.py`、`docs-site/openapi/public.json`、`docs-site/content/docs/changelog.mdx`、`docs/API_ERRORS.md`、`tests/openapi/test_public_openapi.py` | Modify | 公开契约五件套 |
| `tests/conftest.py` | Modify | `_PURE_UNIT` 加两个纯单测文件名 |

---

### Task 1: 查询核心 `backend/core/provider_usage.py` — 适配器选择与归一化（纯逻辑）

**Files:**
- Create: `backend/core/provider_usage.py`
- Test: `tests/test_provider_usage.py`
- Modify: `tests/conftest.py`（`_PURE_UNIT` 集合加 `"test_provider_usage.py"`，插入现有列表字母序处）

**Interfaces:**
- Produces:
  - `select_adapter(provider: str, base_url: str) -> str` — 返回 `"deepseek" | "openrouter" | "relay" | "unsupported"`
  - `_metric(status, **kw) -> dict`、`_empty_metrics() -> dict`（五个 metric 键全出现）
  - `build_payload(provider, adapter, metrics, *, error=None) -> dict` — spec 契约形状；`status` 自动推导：全 ok→`ok`，有 ok 有非 ok→`partial`，无 ok→`error`
  - `PROVIDER_USAGE_TIMEOUT_SEC = 6.0`、`_MAX_RESPONSE_BYTES = 262144`

- [ ] **Step 1: 写失败测试**（适配器选择 + payload 形状）

```python
# tests/test_provider_usage.py
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
    m = pu._empty_metrics()  # 全 unsupported
    p = pu.build_payload("deepseek", "deepseek_balance", m)
    assert p["status"] == "error" or p["status"] == "unsupported_all"  # 见 Step 3 定案: 无 ok 且无 failed → "error"? 不对——
```

⚠️ 上面最后一断言是引子，Step 3 定案后改成明确语义（见下）：`status` 三值
`ok`（全部请求成功）/ `partial`（部分成功或部分 unsupported 但至少一个 ok）/
`error`（一个 ok 都没有）。全 unsupported（比如 unsupported 适配器）也归 `error`
且带 `error="usage_unsupported_provider"`。测试写成：

```python
def test_build_payload_status_derivation():
    m = pu._empty_metrics()
    p = pu.build_payload("openai", "unsupported", m, error="usage_unsupported_provider")
    assert p["status"] == "error"
    assert p["error"] == "usage_unsupported_provider"
    assert set(p["metrics"].keys()) == {"balance", "remaining", "usage_total", "usage_today", "usage_month"}

    m2 = pu._empty_metrics()
    m2["balance"] = pu._metric("ok", amounts=[{"amount": 25.06, "unit": "CNY"}], scope="account")
    p2 = pu.build_payload("deepseek", "deepseek_balance", m2)
    assert p2["status"] == "partial"          # balance ok，其余 unsupported
    assert p2["provider"] == "deepseek"
    assert "as_of" in p2 and "error" not in p2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/hx/Projects/io/worktrees/feedling-mcp/feat-provider-usage && python3 -m pytest tests/test_provider_usage.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'provider_usage'`

- [ ] **Step 3: 最小实现**

```python
# backend/core/provider_usage.py
"""Provider 账单/额度查询核心（bottom layer，无业务依赖）。

REST（hosted/usage_core.py）与 V2 runtime-native 工具共用。只认已解密的
ProviderConfig；不查库、不解信封、不落日志（key 绝不出现在任何输出）。
"""
from __future__ import annotations

import asyncio
import datetime as _dt

import httpx

import provider_client
from core import net_safety

PROVIDER_USAGE_TIMEOUT_SEC = 6.0
_MAX_RESPONSE_BYTES = 262144  # 256KB

_METRIC_KEYS = ("balance", "remaining", "usage_total", "usage_today", "usage_month")

_OFFICIAL_ORIGINS = {
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai",
}


def _canonical_origin(url: str) -> str:
    from urllib.parse import urlsplit
    parts = urlsplit((url or "").strip())
    if not parts.scheme or not parts.netloc:
        return ""
    host = parts.hostname or ""
    port = parts.port
    default = {"https": 443, "http": 80}.get(parts.scheme)
    suffix = "" if (port is None or port == default) else f":{port}"
    return f"{parts.scheme}://{host}{suffix}"


def select_adapter(provider: str, base_url: str) -> str:
    p = provider_client.normalize_provider(provider)
    base = (base_url or "").strip()
    if p in _OFFICIAL_ORIGINS:
        official = _OFFICIAL_ORIGINS[p]
        if not base or _canonical_origin(base) == official:
            return p
        return "unsupported"  # 命名 provider 配了自定义地址：不把 key 发去非官方 origin
    if p == "openai_compatible" and base:
        return "relay"
    return "unsupported"


def _metric(status: str, **kw) -> dict:
    out = {"status": status}
    out.update(kw)
    return out


def _empty_metrics() -> dict:
    return {k: _metric("unsupported") for k in _METRIC_KEYS}


def build_payload(provider: str, adapter: str, metrics: dict, *, error: str | None = None) -> dict:
    statuses = [m.get("status") for m in metrics.values()]
    if all(s == "ok" for s in statuses):
        status = "ok"
    elif any(s == "ok" for s in statuses):
        status = "partial"
    else:
        status = "error"
    payload = {
        "provider": provider_client.normalize_provider(provider),
        "adapter": adapter,
        "status": status,
        "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "metrics": metrics,
    }
    if error:
        payload["error"] = error
    return payload
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_provider_usage.py -v`
Expected: 2 passed

- [ ] **Step 5: conftest 白名单 + collect-only 验证**

`tests/conftest.py` 的 `_PURE_UNIT` 集合（106–157 行区间）按字母序插入
`"test_provider_usage.py",`。然后：

Run: `python3 -m pytest tests/test_provider_usage.py --collect-only -q | tail -3`
Expected: 列出 2 个测试（不是 0 个）

- [ ] **Step 6: Commit**

```bash
git add backend/core/provider_usage.py tests/test_provider_usage.py tests/conftest.py
git commit -m "feat(provider-usage): adapter selection + payload core"
```

---

### Task 2: 三家账单 HTTP 适配器（fake httpx 单测）

**Files:**
- Modify: `backend/core/provider_usage.py`
- Test: `tests/test_provider_usage.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `select_adapter/_metric/_empty_metrics/build_payload`
- Produces: `async def query_usage_async(config: provider_client.ProviderConfig) -> dict`
  —— 顶层入口；内部 `_query_deepseek/_query_openrouter/_query_relay(client, config, metrics)`；
  `_get_json(client, url, *, api_key) -> tuple[dict | None, str | None]`（(json, err_slug)）；
  `allow_private()`（读 env `FEEDLING_PROVIDER_USAGE_ALLOW_PRIVATE`）

错误 slug 枚举（payload 内，非 HTTP 错误）：`usage_timeout / usage_unreachable /
usage_bad_response / usage_blocked_url / usage_unsupported_provider / usage_http_<code>`。
**任何 slug 不携带第三方响应正文**（防 key 回显）。

- [ ] **Step 1: 写失败测试**（fake AsyncClient，模式照 `tests/test_provider_client.py` 的 FakeResponse/FakeClient + monkeypatch）

```python
# 追加到 tests/test_provider_usage.py
import asyncio, json


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.content = json.dumps(self._body).encode()
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._body


class FakeAsyncClient:
    """按 URL 后缀路由的假 httpx.AsyncClient。routes: dict[包含子串, FakeResponse|Exception]"""
    def __init__(self, routes):
        self.routes = routes
        self.requests = []  # (url, headers)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, timeout=None, follow_redirects=None):
        self.requests.append((url, dict(headers or {})))
        for frag, resp in self.routes.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return FakeResponse(404, {})


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
    assert p["status"] == "partial"
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
    assert p["metrics"]["remaining"] == {"status": "ok", "amount": 97.13, "unit": "USD", "scope": "api_key"}
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
    assert p["metrics"]["remaining"]["status"] == "unsupported"  # limit_remaining null → 无限额
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_provider_usage.py -v`
Expected: 新增测试 FAIL（`query_usage_async` 不存在）

- [ ] **Step 3: 实现**

```python
# 追加到 backend/core/provider_usage.py
import json as _json
import os


def allow_private() -> bool:
    return os.environ.get("FEEDLING_PROVIDER_USAGE_ALLOW_PRIVATE", "0").strip() == "1"


def _make_client() -> httpx.AsyncClient:
    # trust_env=False：不吃环境代理；redirect 一律不跟（同 capabilities/web.py 的姿势）
    return httpx.AsyncClient(trust_env=False, follow_redirects=False,
                             timeout=httpx.Timeout(PROVIDER_USAGE_TIMEOUT_SEC))


def _err_slug(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "usage_timeout"
    if isinstance(exc, httpx.HTTPError):
        return "usage_unreachable"
    return "usage_bad_response"


async def _get_json(client, url: str, *, api_key: str):
    """returns (json_dict | None, err_slug | None)。绝不把响应正文放进 slug。"""
    try:
        resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"},
                                follow_redirects=False)
    except Exception as e:  # noqa: BLE001 — 归一化成 slug，绝不透传异常文本
        return None, _err_slug(e)
    if resp.status_code != 200:
        return None, f"usage_http_{int(resp.status_code)}"
    if len(getattr(resp, "content", b"") or b"") > _MAX_RESPONSE_BYTES:
        return None, "usage_bad_response"
    try:
        data = resp.json()
    except Exception:
        return None, "usage_bad_response"
    if not isinstance(data, dict):
        return None, "usage_bad_response"
    return data, None


def _num(v):
    """JSON 数字原样透传；字符串数字保留字符串（DeepSeek 返回 "25.06"）。非法→None。"""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip():
        try:
            float(v)
            return v
        except ValueError:
            return None
    return None


async def _query_deepseek(client, config, metrics):
    data, err = await _get_json(client, "https://api.deepseek.com/user/balance",
                                api_key=config.api_key)
    if err:
        metrics["balance"] = _metric("failed", reason=err)
        return "deepseek_balance"
    amounts = []
    for info in data.get("balance_infos") or []:
        amt = _num((info or {}).get("total_balance"))
        cur = str((info or {}).get("currency") or "unknown")[:8]
        if amt is not None:
            amounts.append({"amount": amt, "unit": cur})
    if amounts:
        metrics["balance"] = _metric("ok", amounts=amounts, scope="account")
    else:
        metrics["balance"] = _metric("failed", reason="usage_bad_response")
    return "deepseek_balance"


async def _query_openrouter(client, config, metrics):
    credits_task = _get_json(client, "https://openrouter.ai/api/v1/credits",
                             api_key=config.api_key)
    key_task = _get_json(client, "https://openrouter.ai/api/v1/key",
                         api_key=config.api_key)
    (cred, cred_err), (key, key_err) = await asyncio.gather(credits_task, key_task)
    if cred_err:
        metrics["balance"] = _metric("failed", reason=cred_err)
    else:
        d = cred.get("data") or {}
        tc, tu = _num(d.get("total_credits")), _num(d.get("total_usage"))
        if tc is not None and tu is not None:
            bal = round(float(tc) - float(tu), 6)
            metrics["balance"] = _metric("ok", amounts=[{"amount": bal, "unit": "USD"}],
                                         scope="account")
        else:
            metrics["balance"] = _metric("failed", reason="usage_bad_response")
    if key_err:
        for k in ("remaining", "usage_today", "usage_month"):
            metrics[k] = _metric("failed", reason=key_err)
    else:
        d = key.get("data") or {}
        lr = _num(d.get("limit_remaining"))
        metrics["remaining"] = (_metric("ok", amount=lr, unit="USD", scope="api_key")
                                if lr is not None else _metric("unsupported"))
        ud, um = _num(d.get("usage_daily")), _num(d.get("usage_monthly"))
        metrics["usage_today"] = (_metric("ok", amount=ud, unit="USD", timezone="UTC")
                                  if ud is not None else _metric("failed", reason="usage_bad_response"))
        metrics["usage_month"] = (_metric("ok", amount=um, unit="USD", timezone="UTC")
                                  if um is not None else _metric("failed", reason="usage_bad_response"))
    return "openrouter_key"


async def _query_relay(client, config, metrics):
    origin = _canonical_origin(config.base_url)
    # /v1 之类的路径前缀去掉：账单端点挂在站点根（new-api 的部署形态）
    token_url = f"{origin}/api/usage/token"
    data, err = await _get_json(client, token_url, api_key=config.api_key)
    inner = (data or {}).get("data") if isinstance((data or {}).get("data"), dict) else None
    if not err and inner is not None:
        avail, used = _num(inner.get("total_available")), _num(inner.get("total_used"))
        if bool(inner.get("unlimited_quota")):
            metrics["remaining"] = _metric("unsupported")
        elif avail is not None:
            metrics["remaining"] = _metric("ok", amount=avail, unit="quota", scope="api_key")
        if used is not None:
            metrics["usage_total"] = _metric("ok", amount=used, unit="quota")
        if metrics["remaining"].get("status") == "ok" or metrics["usage_total"].get("status") == "ok":
            return "relay_token"
    # 老接口兜底（one-api / 老 new-api）：金额是「显示单位 ×100」，单位站点自定 → unknown
    sub_task = _get_json(client, f"{origin}/dashboard/billing/subscription", api_key=config.api_key)
    use_task = _get_json(client, f"{origin}/dashboard/billing/usage", api_key=config.api_key)
    (sub, sub_err), (use, use_err) = await asyncio.gather(sub_task, use_task)
    limit = _num((sub or {}).get("hard_limit_usd")) if not sub_err else None
    used_raw = _num((use or {}).get("total_usage")) if not use_err else None
    used = round(float(used_raw) / 100.0, 6) if used_raw is not None else None
    if used is not None:
        metrics["usage_total"] = _metric("ok", amount=used, unit="unknown")
    else:
        metrics["usage_total"] = _metric("failed", reason=use_err or "usage_bad_response")
    if limit is not None and used is not None:
        metrics["remaining"] = _metric("ok", amount=round(float(limit) - used, 6),
                                       unit="unknown", scope="api_key")
    elif limit is None:
        metrics["remaining"] = _metric("failed", reason=sub_err or "usage_bad_response")
    return "relay_dashboard"


async def query_usage_async(config) -> dict:
    adapter = select_adapter(config.provider, config.base_url)
    metrics = _empty_metrics()
    if adapter == "unsupported":
        return build_payload(config.provider, "unsupported", metrics,
                             error="usage_unsupported_provider")
    if adapter == "relay" and not allow_private():
        kind = net_safety.blocked_url_kind(config.base_url)
        if kind is not None:
            return build_payload(config.provider, "relay", metrics, error="usage_blocked_url")
    try:
        async with _make_client() as client:
            coro = {"deepseek": _query_deepseek, "openrouter": _query_openrouter,
                    "relay": _query_relay}[adapter](client, config, metrics)
            adapter_used = await asyncio.wait_for(coro, timeout=PROVIDER_USAGE_TIMEOUT_SEC)
    except (asyncio.TimeoutError, TimeoutError):
        return build_payload(config.provider, adapter, metrics, error="usage_timeout")
    payload = build_payload(config.provider, adapter_used, metrics)
    if payload["status"] == "error":
        reasons = [m.get("reason") for m in metrics.values() if m.get("reason")]
        payload["error"] = reasons[0] if reasons else "usage_bad_response"
    return payload
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_provider_usage.py -v`
Expected: 全 passed（`test_relay_dashboard_fallback` 里 remaining 是 16.0）

- [ ] **Step 5: Commit**

```bash
git add backend/core/provider_usage.py tests/test_provider_usage.py
git commit -m "feat(provider-usage): deepseek/openrouter/relay billing adapters"
```

---

### Task 3: REST 端点 `GET /v1/model_api/usage`

**Files:**
- Create: `backend/hosted/usage_core.py`
- Modify: `backend/hosted/setup_routes_asgi.py`（router 上加一条，仿 `/v1/model_api/runtime` 83–88 行的形状）
- Modify: `docs/API_ERRORS.md`（payload 内 slug 说明一节）
- Test: `tests/test_model_api_usage_route.py`（需 DB，**不加**进 _PURE_UNIT）

**Interfaces:**
- Consumes: `provider_usage.query_usage_async(config)`；`config_store._load_runtime_provider_config(store, api_key)`（成功返回 ProviderConfig，失败返回 `(None, error_dict)` tuple）
- Produces: `usage_core.resolve_usage_config(store, *, api_key) -> ProviderConfig | tuple[None, dict]`

- [ ] **Step 1: 写失败测试**（用仓库现有 make_client/route 测试惯例；参考 `tests/` 里现有 setup 路由测试的 fixture 用法，monkeypatch 掉解密与外呼）

```python
# tests/test_model_api_usage_route.py
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))


def test_usage_route_not_configured(make_client):
    client, _ = make_client()
    r = client.get("/v1/model_api/usage", headers={"X-API-Key": client.api_key})
    assert r.status_code == 400
    assert r.json()["error"] == "model_api_not_configured"


def test_usage_route_happy_path(make_client, monkeypatch):
    import provider_client as pc
    from hosted import usage_core
    import provider_usage as pu

    client, _ = make_client()
    cfg = pc.ProviderConfig(provider="deepseek", model="deepseek-chat", api_key="sk-x")
    monkeypatch.setattr(usage_core, "resolve_usage_config", lambda store, api_key: cfg)

    async def fake_query(config):
        m = pu._empty_metrics()
        m["balance"] = pu._metric("ok", amounts=[{"amount": "25.06", "unit": "CNY"}], scope="account")
        return pu.build_payload("deepseek", "deepseek_balance", m)

    monkeypatch.setattr(pu, "query_usage_async", fake_query)
    r = client.get("/v1/model_api/usage", headers={"X-API-Key": client.api_key})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "partial"
    assert body["metrics"]["balance"]["amounts"][0]["unit"] == "CNY"
    assert "api_key" not in str(body)
```

（`make_client` 若在本仓惯例里叫别的名字/形状，**照 `tests/` 现有 setup 路由测试改**，
不要自造 fixture。执行者先 `grep -l "model_api/get" tests/` 找参考文件。）

- [ ] **Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_model_api_usage_route.py -v`（需要本地 Postgres：`FEEDLING_TEST_PG` 默认 127.0.0.1:55432；没起的话先按 tests/conftest.py 头部注释起库）
Expected: 404 route not found → FAIL

- [ ] **Step 3: 实现**

```python
# backend/hosted/usage_core.py
"""GET /v1/model_api/usage 的框架中立核心：解一次 key → 交给 provider_usage。"""
from __future__ import annotations

from hosted import config_store


def resolve_usage_config(store, *, api_key):
    """成功返回 ProviderConfig（api_key 已解密）；失败返回 (None, error_dict)。"""
    return config_store._load_runtime_provider_config(store, api_key)
```

```python
# backend/hosted/setup_routes_asgi.py — 在 /v1/model_api/runtime 那条后面加：
@router.get("/v1/model_api/usage")
async def model_api_usage(request: Request, auth: AuthResult = Depends(require_auth)):
    api_key = auth_core.extract_api_key(request.headers, request.query_params)
    result = await threadpool.run_db(usage_core.resolve_usage_config, auth.store, api_key=api_key)
    if isinstance(result, tuple):
        return JSONResponse(result[1], status_code=400)
    payload = await provider_usage.query_usage_async(result)
    return JSONResponse(payload, status_code=200)
```

顶部 import 区补 `from hosted import usage_core` 和 `from core import provider_usage`
（模块级导入，遵守 `from pkg import module` 风格）。

`docs/API_ERRORS.md` 追加一节：`/v1/model_api/usage` 复用 loader 的 4xx slug
（`model_api_not_configured / model_api_not_tested / model_api_key_envelope_missing /
model_api_key_decrypt_failed / model_api_config_invalid`）；200 payload 内的
`error` / `reason` 取值 `usage_timeout / usage_unreachable / usage_bad_response /
usage_blocked_url / usage_unsupported_provider / usage_http_<code>`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_model_api_usage_route.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add backend/hosted/usage_core.py backend/hosted/setup_routes_asgi.py docs/API_ERRORS.md tests/test_model_api_usage_route.py
git commit -m "feat(provider-usage): GET /v1/model_api/usage"
```

---

### Task 4: kill switch（migration + 读写器）

**Files:**
- Create: `backend/alembic/versions/0053_provider_usage_halted.py`
- Modify: `backend/model_api_runtime/v2/kill_switch.py`
- Test: `tests/test_v2_kill_switch.py`（追加，照该文件现有 fake-db 模式）

**Interfaces:**
- Produces: `kill_switch.provider_usage_halted() -> bool`（fail-CLOSED：DB 错/缺行 → True=halt）、
  `kill_switch.set_provider_usage_halted(value: bool)`
- 列默认 `false` = 不 halt = 功能 ON（同 0050 语义；这就是「默认 ON 的 kill switch」）

- [ ] **Step 1: 写失败测试**（照 `tests/test_v2_kill_switch_web.py` 对 `_fetch_web_halted_row` 的驱动方式，给 `provider_usage_halted` 写 正常 / DB 异常 fail-closed / 缺行 fail-closed 三条）

```python
def test_provider_usage_halted_reads_row(monkeypatch):
    from model_api_runtime.v2 import kill_switch as ks
    ks._provider_usage_cache_clear()
    monkeypatch.setattr(ks, "_fetch_provider_usage_halted_row", lambda: False)
    assert ks.provider_usage_halted() is False


def test_provider_usage_halted_fail_closed(monkeypatch):
    from model_api_runtime.v2 import kill_switch as ks
    ks._provider_usage_cache_clear()
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(ks, "_fetch_provider_usage_halted_row", boom)
    assert ks.provider_usage_halted() is True
```

（缓存清理 helper / fetch 函数命名照 kill_switch.py 里 web 那组现有拆分对齐；
执行者先读 `kill_switch.py:74-155` 再落笔，保持同形。）

- [ ] **Step 2: 跑测试确认失败** — `python3 -m pytest tests/test_v2_kill_switch.py -v -k provider_usage` → FAIL

- [ ] **Step 3: 实现**

Migration（revision id 先 `ls backend/alembic/versions/` 确认 0052 的确切 revision 字符串再填 down_revision）：

```python
"""provider_usage_halted kill switch column.

默认 false = 不 halt = 工具在目录里（默认 ON 的回滚闸，同 0050 语义）。
"""
from alembic import op

revision = "0053_provider_usage_halted"
down_revision = "0052_dual_runtime_coexistence"  # ← 以 0052 文件里的 revision 字符串为准
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE v2_runtime_control
  ADD COLUMN IF NOT EXISTS provider_usage_halted BOOLEAN NOT NULL DEFAULT false;
"""

_DOWN = """
ALTER TABLE v2_runtime_control DROP COLUMN IF EXISTS provider_usage_halted;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
```

kill_switch.py：复制 web 那组的结构（独立缓存槽 + ~2s TTL + fail-closed）：
`_fetch_provider_usage_halted_row()`（单列 SELECT）、`provider_usage_halted()`、
`set_provider_usage_halted(value)`、测试用 `_provider_usage_cache_clear()`。

- [ ] **Step 4: 跑测试确认通过** — 同上命令 → passed
- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/0053_provider_usage_halted.py backend/model_api_runtime/v2/kill_switch.py tests/test_v2_kill_switch.py
git commit -m "feat(provider-usage): provider_usage_halted kill switch"
```

---

### Task 5: 工具 schema + lane 范围（静态目录、wake 禁用、subagent 自动排除、private-read）

**Files:**
- Modify: `backend/capabilities/tool_schema.py`
- Modify: `backend/model_api_runtime/v2/worker.py`
- Test: `tests/test_v2_provider_usage_tool.py`（Create，纯单测 → conftest `_PURE_UNIT` 加名）

**Interfaces:**
- Produces: `tool_schema.PROVIDER_USAGE_TOOL = "provider_usage"`；build_tool_specs() 输出包含它；
  worker 侧：wake lane disabled 集合包含它、`_PRIVATE_READ_TOOLS` 包含它

**设计决策（执行者不要改）：**
- 走**静态目录**而不是 extra_tool_specs——extra 名单在 tool_loop 里按 MCP 语义处理
  （跳过参数校验 + 被 MCP 出站封锁连带），不是我们要的。
- subagent：不进 `_SUBAGENT_ALLOWED_TOOLS`（worker.py:331）即自动落入
  `_SUBAGENT_DISABLED_TOOLS`（594）+ `_child_dispatch` 兜底报错，零改动，测试断言即可。
- **不加进 `provenance.EXTERNAL_READS`**：工具结果是我们自己归一化的 JSON
  （数字/枚举/截断 slug），不含第三方自由文本，不应触发外部内容围栏。
- **加进 `_PRIVATE_READ_TOOLS`**（worker.py:342）：余额是敏感读，读完之后本回合
  后续出站（web/MCP/task）应被封，防外传。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_v2_provider_usage_tool.py
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from capabilities import tool_schema


def test_provider_usage_in_catalog_no_params():
    specs = {s.name: s for s in tool_schema.build_tool_specs()}
    assert tool_schema.PROVIDER_USAGE_TOOL in specs
    spec = specs[tool_schema.PROVIDER_USAGE_TOOL]
    assert spec.parameters.get("properties") == {}
    assert "余额" in spec.description or "usage" in spec.description.lower()


def test_provider_usage_args_validate_empty_only():
    assert tool_schema.validate_tool_args(tool_schema.PROVIDER_USAGE_TOOL, {}) is None
    err = tool_schema.validate_tool_args(tool_schema.PROVIDER_USAGE_TOOL, {"x": 1})
    assert err  # 非空参数被拒


def test_provider_usage_excluded_from_subagent_and_private_read():
    from model_api_runtime.v2 import worker
    assert tool_schema.PROVIDER_USAGE_TOOL not in worker._SUBAGENT_ALLOWED_TOOLS
    assert tool_schema.PROVIDER_USAGE_TOOL in worker._SUBAGENT_DISABLED_TOOLS
    assert tool_schema.PROVIDER_USAGE_TOOL in worker._PRIVATE_READ_TOOLS
```

（wake 禁用的断言依 worker.py 4909 附近实际结构写：找到 wake lane 组装 disabled
集合的变量/函数，断言它包含本工具名——执行者读 4425–4438 与 4906–4939 后落笔。）

- [ ] **Step 2: 跑测试确认失败** — `python3 -m pytest tests/test_v2_provider_usage_tool.py -v` → FAIL
- [ ] **Step 3: 实现**

tool_schema.py：
- `PROVIDER_USAGE_TOOL = "provider_usage"`（挨着 TASK_TOOL/REPLY_TOOL，19–20 行）
- `PARAMS[PROVIDER_USAGE_TOOL] = {"type": "object", "properties": {}, "additionalProperties": False}`
- `DESCRIPTIONS[PROVIDER_USAGE_TOOL] = "查询当前 AI 服务商账户的余额与用量（只读）。仅在用户明确询问余额、用量、还剩多少钱时调用；结果如实转述，查不到就说查不到。"`
- `build_tool_specs()`（389–401）在 TASK/REPLY 追加处同样追加本工具 ToolSpec。

worker.py：
- `_PRIVATE_READ_TOOLS`（342–351）加 `cap_tool_schema.PROVIDER_USAGE_TOOL`。
- wake lane：在 4909 传入 `disabled_tool_names` 的集合来源处并入本工具名
  （保持该处现有变量命名风格）。

- [ ] **Step 4: 跑测试确认通过**；另跑 `python3 -m pytest tests/test_capabilities_tool_schema.py tests/test_v2_tool_loop.py -x -q` 确认没把现有目录测试打碎（有的目录测试可能断言工具总数——数字 +1）。
- [ ] **Step 5: conftest `_PURE_UNIT` 加 `"test_v2_provider_usage_tool.py"` + `--collect-only` 验证**
- [ ] **Step 6: Commit**

```bash
git add backend/capabilities/tool_schema.py backend/model_api_runtime/v2/worker.py tests/test_v2_provider_usage_tool.py tests/conftest.py
git commit -m "feat(provider-usage): chat-only runtime-native tool in catalog"
```

---

### Task 6: V2 dispatch 分支（闭包复用 provider_config，信号量外执行，halt 双边界）

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`
- Test: `tests/test_v2_provider_usage_tool.py`（追加）

**Interfaces:**
- Consumes: `provider_usage.query_usage_async`、`kill_switch.provider_usage_halted`、Task 5 的工具名
- Produces:
  - `_make_provider_usage_dispatcher(*, provider_config) -> async callable(tool_calls) -> list[ToolResult]`
    （仿 `_make_task_batch_dispatcher` worker.py:2239 的闭包形状）
  - `_dispatch_mixed_tool_calls(..., dispatch_provider_usage=None)` 新 kwarg；
    分类循环（999–1014）新增 `elif tc.name == cap_tool_schema.PROVIDER_USAGE_TOOL`
  - offer-time：chat lane `disabled_tool_names_for_turn`（5918 附近）并入
    `kill_switch.provider_usage_halted()` 为 True 时的工具名
  - dispatch-time：dispatcher 内先活查一次 halt，halted → `ToolResult(call_id, "error: provider_usage_halted")`

- [ ] **Step 1: 写失败测试**

```python
# 追加 tests/test_v2_provider_usage_tool.py
import asyncio, json


def test_dispatcher_returns_normalized_payload(monkeypatch):
    import provider_client as pc
    import provider_usage as pu
    from model_api_runtime.v2 import worker

    cfg = pc.ProviderConfig(provider="deepseek", model="m", api_key="sk-x")

    async def fake_query(config):
        assert config is cfg  # 必须是同一个对象——不许二次解密
        m = pu._empty_metrics()
        m["balance"] = pu._metric("ok", amounts=[{"amount": "25.06", "unit": "CNY"}], scope="account")
        return pu.build_payload("deepseek", "deepseek_balance", m)

    monkeypatch.setattr(pu, "query_usage_async", fake_query)
    monkeypatch.setattr(worker.kill_switch, "provider_usage_halted", lambda: False)
    dispatch = worker._make_provider_usage_dispatcher(provider_config=cfg)
    calls = [worker.ToolCall(id="c1", name="provider_usage", arguments={})]
    results = asyncio.run(dispatch(calls))
    body = json.loads(results[0].content)
    assert body["metrics"]["balance"]["status"] == "ok"
    assert "sk-x" not in results[0].content


def test_dispatcher_live_halt(monkeypatch):
    import provider_client as pc
    from model_api_runtime.v2 import worker
    cfg = pc.ProviderConfig(provider="deepseek", model="m", api_key="sk-x")
    monkeypatch.setattr(worker.kill_switch, "provider_usage_halted", lambda: True)
    dispatch = worker._make_provider_usage_dispatcher(provider_config=cfg)
    calls = [worker.ToolCall(id="c1", name="provider_usage", arguments={})]
    results = asyncio.run(dispatch(calls))
    assert results[0].content == "error: provider_usage_halted"
```

（`ToolCall`/`ToolResult` 的实际导入位置按 `backend/provider_types.py` 与 worker 现有用法对齐。）

- [ ] **Step 2: 跑测试确认失败** → `_make_provider_usage_dispatcher` 不存在
- [ ] **Step 3: 实现**

worker.py：

```python
def _make_provider_usage_dispatcher(*, provider_config):
    """闭包捕获本回合已解密的 provider_config（single-decrypt 不破坏）。
    第三方 HTTP 直接 await——本函数不在 ENCLAVE_SEMAPHORE 内被调用。"""
    async def _dispatch(tool_calls):
        results = []
        halted = True
        try:
            halted = kill_switch.provider_usage_halted()
        except Exception:
            halted = True
        for tc in tool_calls:
            if halted:
                results.append(ToolResult(tc.id, "error: provider_usage_halted"))
                continue
            try:
                payload = await provider_usage.query_usage_async(provider_config)
                results.append(ToolResult(tc.id, _json.dumps(payload, ensure_ascii=False)))
            except Exception:
                results.append(ToolResult(tc.id, "error: provider_usage_failed"))
        return results
    return _dispatch
```

- `_dispatch_mixed_tool_calls`（944）签名加 `dispatch_provider_usage=None`；
  分类循环新增分支收集 `provider_usage_calls`，执行段：callable 为 None（wake/subagent
  路径）→ 每个 call 回 `ToolResult(tc.id, "error: tool_not_allowed")`；否则 await 它。
- chat lane `process_job`（5934 附近，同 task dispatcher 的绑定处）：
  `dispatch_provider_usage=_make_provider_usage_dispatcher(provider_config=provider_config)`。
- offer-time halt：5918 `disabled_tool_names_for_turn` 组装处，
  `kill_switch.provider_usage_halted()` 为 True → 并入工具名（try/except 包住，异常当 halted）。
- 顶部 import：`from core import provider_usage`、`import json as _json`（如已有 json 导入则复用）。

- [ ] **Step 4: 跑测试确认通过**；再全量跑 V2 相关：
  `python3 -m pytest tests/test_v2_provider_usage_tool.py tests/test_v2_worker_tool_loop.py tests/test_v2_tool_loop.py -q` → 全绿
- [ ] **Step 5: Commit**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_provider_usage_tool.py
git commit -m "feat(provider-usage): v2 chat-lane dispatch with turn provider_config"
```

---

### Task 7: 公开契约五件套

**Files:**
- Modify: `tools/public_openapi_contracts.py`（raw-Request handler 需要 override 条目：GET /v1/model_api/usage 的 response schema，形状照 spec 契约写 object；参考文件里现有 GET 条目）
- Regenerate: `docs-site/openapi/public.json`（`cd docs-site && npm run openapi:generate`）
- Modify: `tests/openapi/test_public_openapi.py`（操作数 148→149；若本端点带 query 参数计数也 +0——本端点无 query 参数）
- Modify: `docs-site/content/docs/changelog.mdx`（`## Unreleased` → API 小节加一行）
- Modify: 受影响 docs 页（若 docs-site/content/docs 有 model_api setup 相关页，补一段；没有则跳过）

- [ ] **Step 1: 加 contracts 条目 + 重新生成 public.json**
- [ ] **Step 2: 跑契约测试** — `python3 -m pytest tests/openapi/ -q`，按失败信息把计数断言 +1，再跑到绿
- [ ] **Step 3: docs-site 三连** — `cd docs-site && npm run types:check && npm run lint && npm run build` → 全过
- [ ] **Step 4: changelog Unreleased 加：`GET /v1/model_api/usage` — provider 余额/用量现查现回（deepseek/openrouter/中转站；字段能力随 provider 不同，见接口 schema）`**
- [ ] **Step 5: Commit**

```bash
git add tools/public_openapi_contracts.py docs-site/openapi/public.json tests/openapi/test_public_openapi.py docs-site/content/docs/changelog.mdx
git commit -m "docs(provider-usage): public contract for GET /v1/model_api/usage"
```

---

### Task 8: 全量回归 + e2e 冒烟

- [ ] **Step 1: 全量单测**（本地起 Postgres：按 tests/conftest.py 的 `FEEDLING_TEST_PG` 约定）
  `python3 -m pytest tests/ -q -x --ignore=tests/openapi` 后再 `python3 -m pytest tests/openapi -q`
  Expected: 全绿；任何失败先按 systematic-debugging 处理，不许跳过
- [ ] **Step 2: 真 key 冒烟（不起全链路，直接调核心）**

```bash
cd /Users/hx/Projects/io/worktrees/feedling-mcp/feat-provider-usage/backend
python3 - <<'PY'
import asyncio, json, os, sys
sys.path.insert(0, ".")
import provider_client as pc; from core import provider_usage as pu
# key 从 io/.env.local 读（现场 export），绝不打印 key
for prov, env in (("deepseek", "DEEPSEEK_KEY"), ("openrouter", "OPEN_ROUTER_KEY")):
    cfg = pc.ProviderConfig(provider=prov, model="m", api_key=os.environ[env])
    print(prov, json.dumps(asyncio.run(pu.query_usage_async(cfg)), ensure_ascii=False))
PY
```

  Expected: deepseek 出 CNY 余额、openrouter 出 balance/remaining/今日/本月，
  数值和 2026-07-23 spec 阶段 curl 实测同数量级。
- [ ] **Step 3: 本地 V2 链路 e2e**（照「改名」功能在 FEATURE_LOG 里记的本地起法：backend + enclave + 手动 serve_worker，真 key 配 deepseek）：聊天里问「我的 API 还剩多少钱」→ agent 调 provider_usage → 回复含余额；再把 `set_provider_usage_halted(True)` 打开 → 同问题 → 工具不在目录、agent 如实说查不了。
- [ ] **Step 4: Commit（如有修正）+ 更新 FEATURE_LOG**（跑 `ops/refresh-branch-board.sh`；「上线状态」保持 ❌ 未上线）

---

## Self-Review 结论（写完计划后自查）

- spec 覆盖：REST ✔(T3) 工具 ✔(T5/6) 三家适配 ✔(T2) kill switch 双边界 ✔(T4/6)
  出站安全 ✔(T2 blocked_url + 同 origin) 契约 ✔(T7) e2e ✔(T8)
  conftest 坑 ✔(T1/T5) 单位不猜 ✔(T2)。
- spec「redirect 拒绝」由 `follow_redirects=False` + 非 200 slug 化覆盖（3xx → `usage_http_3xx`）。
- 行号均为探路时点值，执行时以 grep 实际定位为准，不盲改。
