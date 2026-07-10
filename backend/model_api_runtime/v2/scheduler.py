"""V2 proactive scheduler tick (Task 4 of D3 proactive wake lanes plan).

Pure module: every side effect (which users are due, the wake decision,
enqueueing a heartbeat job, advancing the schedule) is injected via a
`deps` object, so this is fully unit-testable with fakes and carries no
IO or wall-clock reads of its own.

Dependency direction: this module must NOT import `hosted`/`agent_runtime`/
`proactive`. The real wake decision + enqueue implementations are wired in
by serve_worker.py (see test_v2_dependency_direction.py).
"""
import logging

logger = logging.getLogger(__name__)

_DEFAULT_WAKE_INTERVAL_SEC = 7200


def run_scheduler_tick(deps, *, now: float) -> dict:
    """One scheduler pass over all users currently due for a heartbeat wake.

    `deps` provides:
      - due_users() -> list[str]           # users whose heartbeat is due & not in payment cooldown
      - wake_decision(user_id) -> dict      # {"should_wake": bool, "wake_interval_sec": int, "block_reason": str}
      - enqueue_heartbeat(user_id) -> None  # enqueue a heartbeat agent_job (single-flight de-dupes)
      - advance_heartbeat(user_id, next_at_epoch: float) -> None  # persist next_heartbeat_at

    For each due user: consult wake_decision. If should_wake -> enqueue_heartbeat
    AND advance. If NOT should_wake (blocked/weak/unactivated) -> advance ONLY,
    do NOT enqueue (zero-burn: no job, no model call). Always advance so a due
    user isn't reconsidered every tick.

    `now` is an injected epoch float (no wall-clock reads here -- keeps this
    deterministic and unit-testable).

    Returns {"considered": N, "enqueued": E, "skipped": S}.
    """
    considered = 0
    enqueued = 0
    skipped = 0

    for user_id in deps.due_users():
        considered += 1
        try:
            decision = deps.wake_decision(user_id)
            interval = int(decision.get("wake_interval_sec") or _DEFAULT_WAKE_INTERVAL_SEC)
            next_at = now + interval

            if decision.get("should_wake"):
                deps.enqueue_heartbeat(user_id)
                enqueued += 1
            else:
                skipped += 1

            deps.advance_heartbeat(user_id, next_at)
        except Exception:
            skipped += 1
            logger.exception("scheduler: error processing wake tick for user %s", user_id)

    # `deps` 是鸭子类型（测试里的 FakeDeps / 生产的 SimpleNamespace），不是 dataclass。
    # 必须 getattr 探测：老的 FakeDeps 根本没有这两个属性，直接取会 AttributeError。
    # 两者缺一即整个 scheduled 扫描跳过 —— 既有 scheduler 逻辑因此零改动。
    due_scheduled = getattr(deps, "due_scheduled_users", None)
    fire_scheduled = getattr(deps, "fire_scheduled", None)
    scheduled_fired = 0
    if due_scheduled is not None and fire_scheduled is not None:
        for user_id in due_scheduled():
            try:
                scheduled_fired += int(fire_scheduled(user_id) or 0)
            except Exception:  # noqa: BLE001 — 单用户失败不能中断整轮扫描
                logger.exception("scheduler: fire_scheduled failed for user %s", user_id)

    # capture/dream 抽取扫描（Task 4）—— 与上面的 scheduled 扫描同构：两个新 dep 同样
    # getattr 探测（既有 FakeDeps 没有这两个属性，直接取会 AttributeError），缺一即整段跳过；
    # 每用户 try/except（logger.exception），单用户失败绝不中断整轮扫描（与 heartbeat/
    # scheduled 的隔离口径一致）。
    extraction_users = getattr(deps, "extraction_users", None)
    tick_extraction = getattr(deps, "tick_extraction", None)
    extraction_enqueued = 0
    if extraction_users is not None and tick_extraction is not None:
        for user_id in extraction_users():
            try:
                extraction_enqueued += int(tick_extraction(user_id) or 0)
            except Exception:  # noqa: BLE001 — 单用户失败不能中断整轮扫描
                logger.exception("scheduler: tick_extraction failed for user %s", user_id)

    # screen_watch 扫描（D-screen_watch Task 4）—— 与上面 scheduled/extraction 扫描同构：
    # 两个新 dep 同样 getattr 探测（既有 FakeDeps 没有这两个属性，直接取会 AttributeError），
    # 缺一即整段跳过；每用户 try/except（logger.exception），单用户失败绝不中断整轮扫描
    # （与 heartbeat/scheduled/extraction 的隔离口径一致）。
    screen_watch_users = getattr(deps, "screen_watch_users", None)
    tick_screen_watch = getattr(deps, "tick_screen_watch", None)
    screen_watch_enqueued = 0
    if screen_watch_users is not None and tick_screen_watch is not None:
        for user_id in screen_watch_users():
            try:
                screen_watch_enqueued += int(tick_screen_watch(user_id) or 0)
            except Exception:  # noqa: BLE001 — 单用户失败不能中断整轮扫描
                logger.exception("scheduler: tick_screen_watch failed for user %s", user_id)

    return {"considered": considered, "enqueued": enqueued, "skipped": skipped,
            "scheduled_fired": scheduled_fired, "extraction_enqueued": extraction_enqueued,
            "screen_watch_enqueued": screen_watch_enqueued}
