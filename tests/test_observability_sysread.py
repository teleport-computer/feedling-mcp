from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from observability import sysread  # noqa: E402

def test_read_host_load(tmp_path):
    p = tmp_path / "loadavg"
    p.write_text("1.60 1.90 2.04 1/807 254\n")
    assert sysread.read_host_load(str(p)) == (1.60, 1.90, 2.04)

def test_read_host_mem(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal: 15396488 kB\nMemFree: 400000 kB\n"
                 "MemAvailable: 900000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
    m = sysread.read_host_mem(str(p))
    assert m["total_bytes"] == 15396488 * 1024
    assert m["avail_bytes"] == 900000 * 1024
    assert m["swap_total_bytes"] == 0

def test_read_cgroup(tmp_path):
    (tmp_path / "memory.current").write_text("1381535744\n")
    (tmp_path / "cpu.stat").write_text("usage_usec 12345\nuser_usec 1\nsystem_usec 2\n")
    c = sysread.read_cgroup(str(tmp_path))
    assert c["mem_bytes"] == 1381535744
    assert c["cpu_usage_usec"] == 12345

def test_read_cgroup_missing(tmp_path):
    c = sysread.read_cgroup(str(tmp_path))
    assert c["mem_bytes"] is None and c["cpu_usage_usec"] is None
