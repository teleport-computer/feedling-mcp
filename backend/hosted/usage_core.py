"""GET /v1/model_api/usage 的框架中立核心：解一次 key → 交给 provider_usage。"""
from __future__ import annotations

from hosted import config_store


def resolve_usage_config(store, *, api_key):
    """成功返回 ProviderConfig（api_key 已解密）；失败返回 (None, error_dict)。"""
    return config_store._load_runtime_provider_config(store, api_key)
