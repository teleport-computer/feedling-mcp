from __future__ import annotations
import threading

_lock = threading.Lock()
_count = 0

def record(status_code: int) -> None:
    global _count
    if 500 <= status_code <= 599:
        with _lock:
            _count += 1

def drain() -> int:
    global _count
    with _lock:
        n = _count
        _count = 0
    return n

def install(app) -> None:
    @app.after_request
    def _obs_count_5xx(resp):
        record(resp.status_code)
        return resp
