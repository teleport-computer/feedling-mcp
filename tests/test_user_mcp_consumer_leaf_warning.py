"""codex 驱动下，某 enabled MCP server 用单张自签名证书(叶子 CA:TRUE)时 consumer 打警告。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

# consumer 在模块作用域读这两个必需 env var（KeyError 若缺失），照既有 consumer
# 测试的姿势（见 test_memory_action_conformance.py / test_chat_resident_consumer_file.py）
# 在 import 前打桩。
os.environ.setdefault("FEEDLING_API_URL", "http://127.0.0.1:9")
os.environ.setdefault("FEEDLING_API_KEY", "test_key")

import chat_resident_consumer as c  # noqa: E402


def _fake_fetch(leaf_ca):
    # 返回 (anchor, leaf_is_ca)——anchor 给个占位 PEM，leaf_ca 由参数控
    return lambda url: ("-----BEGIN CERTIFICATE-----\nX\n-----END CERTIFICATE-----", leaf_ca)


def test_codex_lone_cert_warns(monkeypatch, caplog):
    monkeypatch.setattr(c, "_cli_template_is_codex", lambda: True)
    servers = [{"name": "probe", "enabled": True, "url": "https://h:9443/mcp", "ca_pem": ""}]
    with caplog.at_level("WARNING"):
        c._enrich_with_fetched_ca(servers, fetch=_fake_fetch(True))
    assert any("probe" in r.message and "codex" in r.message.lower()
               and ("chain" in r.message.lower() or "叶子" in r.message or "CA" in r.message)
               for r in caplog.records)


def test_codex_proper_chain_no_warn(monkeypatch, caplog):
    monkeypatch.setattr(c, "_cli_template_is_codex", lambda: True)
    servers = [{"name": "probe", "enabled": True, "url": "https://h:9443/mcp", "ca_pem": ""}]
    with caplog.at_level("WARNING"):
        c._enrich_with_fetched_ca(servers, fetch=_fake_fetch(False))
    assert not any("codex" in r.message.lower() for r in caplog.records)


def test_claude_lone_cert_no_warn(monkeypatch, caplog):
    monkeypatch.setattr(c, "_cli_template_is_codex", lambda: False)
    servers = [{"name": "probe", "enabled": True, "url": "https://h:9443/mcp", "ca_pem": ""}]
    with caplog.at_level("WARNING"):
        c._enrich_with_fetched_ca(servers, fetch=_fake_fetch(True))
    assert not any("codex" in r.message.lower() for r in caplog.records)


def test_manual_ca_pem_skips_fetch(monkeypatch, caplog):
    monkeypatch.setattr(c, "_cli_template_is_codex", lambda: True)
    # 手贴 ca_pem 的 server 不抓取也不判 leaf → 不警告
    called = {"n": 0}
    def fetch(url):
        called["n"] += 1
        return (None, True)
    servers = [{"name": "probe", "enabled": True, "url": "https://h/mcp", "ca_pem": "PINNED"}]
    c._enrich_with_fetched_ca(servers, fetch=fetch)
    assert called["n"] == 0
