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
    assert p2["status"] == "partial"          # balance ok，其余 unsupported
    assert p2["provider"] == "deepseek"
    assert "as_of" in p2 and "error" not in p2
