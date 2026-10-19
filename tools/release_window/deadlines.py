"""Absolute phase deadlines. Abort local I/O without taking admission's lock.

Closing local transports cannot cancel provider-side computation. Uncertain
requests remain reserved and cleanup requires its own positive evidence.
"""
import math
import threading
import time


class PhaseExpired(RuntimeError):
    pass


def validate_window(value):
    if not isinstance(value, dict) or set(value) != {
        'latest_start_epoch', 'execution_end_epoch', 'cleanup_end_epoch'
    }:
        raise ValueError('REQUIRED_INPUT:absolute_window')
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in value.values()):
        raise ValueError('REQUIRED_INPUT:absolute_window_numbers')
    if not value['latest_start_epoch'] < value['execution_end_epoch'] < value['cleanup_end_epoch']:
        raise ValueError('REQUIRED_INPUT:absolute_window_order')
    return value


class Deadline:
    def __init__(self, epoch, *, wall=time.time, clock=time.monotonic, schedule=True):
        self.wall, self.clock, self.epoch = wall, clock, epoch
        self.end = clock() + max(0, epoch - wall())
        self.expired = threading.Event()
        self._lock = threading.Lock()
        self._closers = []
        self.timer = threading.Timer(max(0, self.end-clock()), self.expire)
        self.timer.daemon = True
        if schedule:
            self.timer.start()

    def remaining(self):
        left = min(self.epoch-self.wall(), self.end-self.clock())
        if self.expired.is_set() or left <= 0:
            self.expire()
            raise PhaseExpired('absolute phase deadline; preserve recovery evidence')
        return left

    def on_expire(self, callback):
        with self._lock:
            if not self.expired.is_set():
                self._closers.append(callback)
                return
        self._start_close(callback)

    @staticmethod
    def _start_close(callback):
        def close():
            try:
                callback()
            except Exception:
                pass  # Closure attempt is not proof of drained transport.
        threading.Thread(target=close, daemon=True).start()

    def expire(self):
        with self._lock:
            if self.expired.is_set():
                return
            self.expired.set()
            callbacks, self._closers = self._closers, []
        for callback in callbacks:
            self._start_close(callback)

    def bind(self, client):
        self.remaining()
        self.on_expire(client.close)
        def request(req):
            left = self.remaining()
            limits = req.extensions.get('timeout', {})
            req.extensions['timeout'] = {
                key: min(left, limits.get(key) or left)
                for key in ('connect', 'read', 'write', 'pool')
            }
        def response(resp):
            self.on_expire(resp.close)
            self.remaining()
        client.event_hooks.setdefault('request', []).append(request)
        client.event_hooks.setdefault('response', []).append(response)
        return client

    def cancel(self):
        self.timer.cancel()
