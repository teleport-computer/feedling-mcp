from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from observability import service  # noqa: E402
from observability.cpu import CpuRateTracker  # noqa: E402

def test_collect_sample_assembles_all_sources(monkeypatch):
    monkeypatch.setattr(service.sysread, "read_host_load", lambda *a, **k: (1.5, 1.6, 1.7))
    monkeypatch.setattr(service.sysread, "read_host_mem",
                        lambda *a, **k: {"total_bytes": 100, "avail_bytes": 30, "free_bytes": 20,
                                         "swap_total_bytes": 0, "swap_free_bytes": 0})
    monkeypatch.setattr(service.sysread, "read_cgroup",
                        lambda *a, **k: {"mem_bytes": 1_400_000_000, "cpu_usage_usec": 1_000_000})
    monkeypatch.setattr(service.enclave_client, "scrape_enclave_resource",
                        lambda *a, **k: {"mem_bytes": 350_000_000, "cpu_usage_usec": 500_000})
    monkeypatch.setattr(service.store, "read_service_resource",
                        lambda s: {"mem_bytes": 5_600_000_000, "cpu_usage_usec": 900_000, "age_sec": 5})
    monkeypatch.setattr(service.store, "agent_counts",
                        lambda: {"live": 98, "by_driver": {"codex": 65, "claude": 33},
                                 "orphan": 0, "idle": 0, "errors": 0})
    monkeypatch.setattr(service.store, "db_activity",
                        lambda: {"conns": 26, "by_state": {"idle": 20}, "slow": 0,
                                 "idle_in_tx": 0, "db_size_pretty": "709 MB"})
    monkeypatch.setattr(service.store, "chat_backlog", lambda stale: 1)
    monkeypatch.setattr(service.fivexx, "drain", lambda: 2)

    s = service.collect_sample(CpuRateTracker(), now=1000.0)
    assert s["host_load1"] == 1.5
    assert s["host_mem_avail_bytes"] == 30
    assert s["backend_mem_bytes"] == 1_400_000_000
    assert s["enclave_mem_bytes"] == 350_000_000
    assert s["agentrunner_mem_bytes"] == 5_600_000_000
    assert s["live_agents"] == 98 and s["orphan"] == 0
    assert s["backend_5xx"] == 2
    assert s["backlog"] == 1               # 进 extra
    assert s["by_driver"] == {"codex": 65, "claude": 33}  # 进 extra
    # CPU% 首轮为 None
    assert s["backend_cpu_pct"] is None

def test_run_once_inserts_and_prunes(monkeypatch):
    inserted = {}
    monkeypatch.setattr(service, "collect_sample", lambda t, now: {"ts": "x", "live_agents": 1})
    monkeypatch.setattr(service.store, "insert_sample", lambda s: inserted.update(s))
    monkeypatch.setattr(service.store, "delete_old_samples", lambda h: 0)
    service.run_once(CpuRateTracker(), now=1.0, retention_hours=48)
    assert inserted["live_agents"] == 1

def test_start_sampler_returns_and_spawns_thread(monkeypatch):
    import threading, time as _t
    real_sleep = _t.sleep  # service.time IS the same module object as _t; capture before patching
    monkeypatch.setattr(service, "report_self_resource", lambda *a, **k: None)
    monkeypatch.setattr(service, "run_once", lambda *a, **k: None)
    monkeypatch.setattr(service.time, "sleep", lambda *a, **k: real_sleep(0.01))
    before = {th.name for th in threading.enumerate()}
    service.start_sampler()   # must return promptly, not block
    real_sleep(0.05)
    names = {th.name for th in threading.enumerate()}
    assert "obs-sampler" in names and "obs-sampler" not in before
