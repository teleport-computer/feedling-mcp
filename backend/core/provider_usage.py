"""Provider 账单/额度查询核心（backend/core/provider_usage.py）。

REST（hosted/usage_core.py）与 V2 runtime-native 工具共用。只认已解密的
ProviderConfig；不查库、不解信封、不落日志（key 绝不出现在任何输出）。
"""
from __future__ import annotations

import datetime as _dt

import provider_client

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
