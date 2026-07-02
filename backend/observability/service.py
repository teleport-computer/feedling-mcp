from __future__ import annotations
import logging
import os
import threading
import time
import datetime as _dt
from observability import sysread, store
from observability import cpu as _cpu, enclave_client, fivexx

log = logging.getLogger("feedling.observability")


def report_self_resource(service_name: str) -> None:
    """各服务把自身 cgroup 资源写进 agent_runtime_resource（best-effort）。"""
    try:
        c = sysread.read_cgroup()
        store.upsert_service_resource(service_name, c["cpu_usage_usec"], c["mem_bytes"])
    except Exception as e:
        log.warning("[obs] report_self_resource(%s) failed: %s", service_name, e)


SAMPLE_INTERVAL_SEC = int(os.environ.get("FEEDLING_OBS_SAMPLE_INTERVAL_SEC") or "60")
RETENTION_HOURS = float(os.environ.get("FEEDLING_OBS_RETENTION_HOURS") or "48")
BACKLOG_STALE_SEC = int(os.environ.get("FEEDLING_OBS_BACKLOG_STALE_SEC") or "120")


def collect_sample(tracker: "_cpu.CpuRateTracker", now: float) -> dict:
    load = sysread.read_host_load()
    mem = sysread.read_host_mem()
    backend_cg = sysread.read_cgroup()
    enc = enclave_client.scrape_enclave_resource() or {}
    ar = store.read_service_resource("agent-runner") or {}
    counts = store.agent_counts()
    dba = store.db_activity()

    backend_cpu = tracker.update("backend", backend_cg.get("cpu_usage_usec"), now)
    enclave_cpu = tracker.update("enclave", enc.get("cpu_usage_usec"), now)
    ar_cpu = tracker.update("agent-runner", ar.get("cpu_usage_usec"), now)

    return {
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "host_load1": load[0],
        "host_mem_avail_bytes": mem["avail_bytes"],
        "backend_mem_bytes": backend_cg.get("mem_bytes"),
        "enclave_mem_bytes": enc.get("mem_bytes"),
        "agentrunner_mem_bytes": ar.get("mem_bytes"),
        "backend_cpu_pct": backend_cpu,
        "enclave_cpu_pct": enclave_cpu,
        "agentrunner_cpu_pct": ar_cpu,
        "live_agents": counts["live"],
        "orphan": counts["orphan"],
        "errors": counts["errors"],
        "db_conns": dba["conns"],
        "backend_5xx": fivexx.drain(),
        # --- 以下进 extra ---
        "by_driver": counts["by_driver"],
        "idle_agents": counts["idle"],
        "host_load5": load[1], "host_load15": load[2],
        "host_mem_total_bytes": mem["total_bytes"], "host_mem_free_bytes": mem["free_bytes"],
        "host_swap_free_bytes": mem["swap_free_bytes"],
        "db_by_state": dba["by_state"], "db_slow": dba["slow"],
        "db_idle_in_tx": dba["idle_in_tx"], "db_size_pretty": dba["db_size_pretty"],
        "backlog": store.chat_backlog(BACKLOG_STALE_SEC),
        "enclave_ok": bool(enc),
        "agentrunner_age_sec": ar.get("age_sec"),
    }


def run_once(tracker, now: float, retention_hours: float = RETENTION_HOURS) -> None:
    try:
        store.insert_sample(collect_sample(tracker, now))
        store.delete_old_samples(retention_hours)
    except Exception as e:
        log.error("[obs] sample run_once failed: %s", e)


def start_sampler() -> None:
    def _loop():
        log.info("[obs] sampler started (interval=%ss retention=%sh)", SAMPLE_INTERVAL_SEC, RETENTION_HOURS)
        tracker = _cpu.CpuRateTracker()
        while True:
            report_self_resource("backend")  # 后端也把自己写进 resource 表（统一口径/供他处复用）
            run_once(tracker, time.monotonic())
            time.sleep(SAMPLE_INTERVAL_SEC)
    threading.Thread(target=_loop, daemon=True, name="obs-sampler").start()
