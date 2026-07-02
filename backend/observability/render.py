from __future__ import annotations
import html as _html


def sparkline(values, width: int = 160, height: int = 32) -> str:
    pts = [(i, v) for i, v in enumerate(values) if isinstance(v, (int, float))]
    if not pts:
        return f'<svg width="{width}" height="{height}"></svg>'
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    n = (max(xs) or 1)
    coords = " ".join(
        f"{x / n * (width - 2) + 1:.1f},{height - 1 - (y - lo) / span * (height - 2):.1f}"
        for x, y in pts)
    return (f'<svg width="{width}" height="{height}" style="vertical-align:middle">'
            f'<polyline fill="none" stroke="#3a7" stroke-width="1.5" points="{coords}"/></svg>')


def _mem_gb(b):
    return f"{b/1e9:.2f}G" if isinstance(b, (int, float)) else "—"


def render_page(latest: dict, samples: list[dict]) -> str:
    latest = latest or {}
    avail = latest.get("host_mem_avail_bytes")
    low_mem = isinstance(avail, (int, float)) and avail < 500_000_000
    err = latest.get("errors") or 0
    def series(key):
        return sparkline([s.get(key) for s in samples])
    by_driver = latest.get("by_driver") or {}
    driver_str = ", ".join(f"{k}:{v}" for k, v in by_driver.items()) or "—"
    mem_style = "color:#c00;font-weight:bold" if low_mem else ""
    err_style = "color:#c00;font-weight:bold" if err else ""
    rows = f"""
      <tr><td>live 托管</td><td>{latest.get('live_agents','—')} <small>({_html.escape(driver_str)})</small></td><td>{series('live_agents')}</td></tr>
      <tr><td>orphan</td><td>{latest.get('orphan','—')}</td><td></td></tr>
      <tr><td>error</td><td style="{err_style}">{err}</td><td></td></tr>
      <tr><td>可用内存</td><td style="{mem_style}">{_mem_gb(avail)}</td><td>{series('host_mem_avail_bytes')}</td></tr>
      <tr><td>agent-runner 内存</td><td>{_mem_gb(latest.get('agentrunner_mem_bytes'))}</td><td>{series('agentrunner_mem_bytes')}</td></tr>
      <tr><td>host load1</td><td>{latest.get('host_load1','—')}</td><td>{series('host_load1')}</td></tr>
      <tr><td>backend 5xx/样本</td><td>{latest.get('backend_5xx','—')}</td><td>{series('backend_5xx')}</td></tr>
      <tr><td>DB 连接</td><td>{latest.get('db_conns','—')}</td><td>{series('db_conns')}</td></tr>
      <tr><td>enclave</td><td>{'healthy' if latest.get('enclave_ok') else 'DOWN'}</td><td></td></tr>
      <tr><td>库大小</td><td>{_html.escape(str(latest.get('db_size_pretty','—')))}</td><td></td></tr>
      <tr><td>消息堆积</td><td>{latest.get('backlog','—')}</td><td>{series('backlog')}</td></tr>
    """
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="10">
<title>feedling observability</title>
<style>body{{font:13px -apple-system,monospace;margin:24px;color:#222}}
table{{border-collapse:collapse}}td{{padding:4px 12px;border-bottom:1px solid #eee}}
h1{{font-size:16px}}small{{color:#888}}</style></head>
<body><h1>feedling · observability</h1>
<p><small>{_html.escape(str(latest.get('ts','')))} · 每 10s 自动刷新 · 趋势=最近样本</small></p>
<table>{rows}</table></body></html>"""
