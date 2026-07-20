"""D4 live kill switch —— 单行 `v2_runtime_control` 表驱动的运行时开关，能在不重新部署的
情况下即时停摆 V2 turn 池：新聊天 admission fail-closed 503、`_slot_loop` 不再抢新 job、
活跃写 effect 被 fence 掉。绝不碰 Genesis（它读独立的 `genesis_import_jobs` 表 + 心跳，
本模块从不涉及）。

依赖方向干净：只 import `db`（`tests/test_v2_dependency_direction.py` 守卫），不 import
`hosted`/`agent_runtime`，可被任何 v2 core 模块安全引用。

`turns_halted()` 带一个 ~2s 的模块级 TTL 缓存（控制面表读得极频繁——每个 chat/send 请求
一次、每个 slot 每轮一次——不值得为了一个几乎恒定的布尔值打一次 DB）。`default_on_error`
区分两种调用方：
- admission 读（chat_send_core）传 `default_on_error=True`：控制面读失败时 fail-CLOSED——
  绝不能因为读不到开关状态就放行进一个可能已被暂停的池。
- `_slot_loop`/写 fence 读默认 `default_on_error=False`：控制面故障不该让本已在跑的池
  自己拒绝服务——那等于把一次瞬时 DB 错误放大成整池停摆，而 admission 闸已经在入口挡住
  了新流量的风险。
"""
from __future__ import annotations

import logging
import threading
import time

import db

log = logging.getLogger(__name__)

_CACHE_TTL_SEC = 2.0

_cache_lock = threading.Lock()
_cached_value: bool | None = None
_cached_at: float = 0.0


def _invalidate() -> None:
    """Drop the cached value so the next `turns_halted()` call re-reads the DB.
    Called by `set_turns_halted` after a write, and by tests so they don't have
    to sleep past `_CACHE_TTL_SEC`."""
    global _cached_value, _cached_at
    with _cache_lock:
        _cached_value = None
        _cached_at = 0.0


def _read_turns_halted(*, default_on_error: bool) -> bool:
    """Read the control row and refresh this process's cached observation."""
    global _cached_value, _cached_at
    try:
        with db.get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT turns_halted FROM v2_runtime_control WHERE id=1")
                row = cur.fetchone()
                value = bool(row[0]) if row else False
    except Exception as exc:  # noqa: BLE001 — callers choose fail-open/closed
        log.warning(
            "[v2.kill_switch] turns_halted read failed, default_on_error=%s: %s",
            default_on_error,
            exc,
        )
        return default_on_error
    with _cache_lock:
        _cached_value = value
        _cached_at = time.monotonic()
    return value


def turns_halted(default_on_error: bool = False) -> bool:
    """True iff V2 turns are currently halted. Cached ~`_CACHE_TTL_SEC` seconds.

    On a DB read error, returns `default_on_error` — callers that must fail
    CLOSED (admission) pass `default_on_error=True`; callers on the already-
    running path (slot claim, write fence) keep the default `False` so a
    transient control-plane read failure doesn't itself halt the pool.
    """
    global _cached_value, _cached_at
    now = time.monotonic()
    with _cache_lock:
        if _cached_value is not None and (now - _cached_at) < _CACHE_TTL_SEC:
            return _cached_value
    return _read_turns_halted(default_on_error=default_on_error)


def turns_halted_uncached(default_on_error: bool = False) -> bool:
    """Read the live row without accepting the two-second process cache.

    Long-running Capture work uses this at disclosure and mutation boundaries.
    A cached ``False`` is suitable for high-frequency admission/slot polling,
    but it is not an emergency fence once decrypted conversation content is
    about to leave the enclave or a prepared batch is about to mutate Memory.
    """
    return _read_turns_halted(default_on_error=default_on_error)


def set_turns_halted(halted: bool) -> None:
    """UPDATE the single control row and invalidate the cache so the next read
    (in this process and any other, once its own TTL lapses) observes it."""
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE v2_runtime_control SET turns_halted=%s, updated_at=now() WHERE id=1",
                (bool(halted),),
            )
    _invalidate()
