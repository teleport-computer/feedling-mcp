"""Provider 账单/额度查询核心（backend/core/provider_usage.py）。

REST（hosted/usage_core.py）与 V2 runtime-native 工具共用。只认已解密的
ProviderConfig；不查库、不解信封、不落日志（key 绝不出现在任何输出）。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json as _json
import os

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
    considered = [s for s in statuses if s != "unsupported"]
    if considered and all(s == "ok" for s in considered):
        status = "ok"
    elif any(s == "ok" for s in considered):
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
    """returns (json_dict | None, err_slug | None)。绝不把响应正文放进 slug。

    流式读取，运行中累计字节数、超过 _MAX_RESPONSE_BYTES 立刻停止（不把
    整个响应体吃进内存后再判断），防御恶意/超大 relay 响应。
    """
    try:
        async with client.stream("GET", url, headers={"Authorization": f"Bearer {api_key}"},
                                 follow_redirects=False) as resp:
            if resp.status_code != 200:
                return None, f"usage_http_{int(resp.status_code)}"
            chunks = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    return None, "usage_bad_response"
                chunks.append(chunk)
    except Exception as e:  # noqa: BLE001 — 归一化成 slug，绝不透传异常文本
        return None, _err_slug(e)
    try:
        data = _json.loads(b"".join(chunks))
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
