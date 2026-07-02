from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import enclave_app  # noqa: E402

def test_internal_resource_ok(monkeypatch):
    import observability.sysread as sysread
    monkeypatch.setattr(sysread, "read_cgroup",
                        lambda *a, **k: {"mem_bytes": 123, "cpu_usage_usec": 456})
    client = enclave_app.app.test_client()
    r = client.get("/internal/resource")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "mem_bytes": 123, "cpu_usage_usec": 456}
