from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from observability import service  # noqa: E402


def test_report_self_upserts(monkeypatch):
    monkeypatch.setattr(service.sysread, "read_cgroup",
                        lambda *a, **k: {"mem_bytes": 7, "cpu_usage_usec": 8})
    calls = {}
    monkeypatch.setattr(service.store, "upsert_service_resource",
                        lambda s, cpu, mem: calls.update(service=s, cpu=cpu, mem=mem))
    service.report_self_resource("agent-runner")
    assert calls == {"service": "agent-runner", "cpu": 8, "mem": 7}
