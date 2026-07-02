from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from observability import enclave_client  # noqa: E402


class _Resp:
    status_code = 200
    def json(self): return {"ok": True, "mem_bytes": 10, "cpu_usage_usec": 20}


class _FakeClient:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get(self, url): return _Resp()


def test_scrape_ok(monkeypatch):
    monkeypatch.setattr(enclave_client.httpx, "Client", _FakeClient)
    assert enclave_client.scrape_enclave_resource("https://enclave:5003") == {"mem_bytes": 10, "cpu_usage_usec": 20}


def test_scrape_failure_returns_none(monkeypatch):
    class _Boom(_FakeClient):
        def get(self, url): raise OSError("down")
    monkeypatch.setattr(enclave_client.httpx, "Client", _Boom)
    assert enclave_client.scrape_enclave_resource("https://enclave:5003") is None
