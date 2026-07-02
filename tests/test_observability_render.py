from __future__ import annotations
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from observability import render  # noqa: E402

def test_sparkline_is_svg():
    svg = render.sparkline([1.0, 2.0, None, 3.0])
    assert svg.startswith("<svg") and "polyline" in svg

def test_sparkline_empty():
    assert "<svg" in render.sparkline([])

def test_render_page_contains_key_metrics():
    latest = {"live_agents": 98, "orphan": 0, "errors": 0, "host_mem_avail_bytes": 1_500_000_000,
              "backend_5xx": 0, "enclave_ok": True, "db_size_pretty": "709 MB",
              "by_driver": {"codex": 65, "claude": 33}}
    html = render.render_page(latest, [latest])
    assert "observability" in html.lower()
    assert "98" in html            # live
    assert "codex" in html

def test_render_page_flags_low_mem_red():
    latest = {"live_agents": 1, "orphan": 0, "errors": 0, "host_mem_avail_bytes": 200_000_000,
              "backend_5xx": 0, "enclave_ok": True, "db_size_pretty": "1 MB", "by_driver": {}}
    html = render.render_page(latest, [latest])
    assert "red" in html.lower() or "#c00" in html.lower()  # 低内存标红
