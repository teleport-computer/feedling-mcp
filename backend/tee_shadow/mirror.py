"""TEE 影子库尽力而为镜像（spec §5.1）。

影子期铁律：任何失败只 log+计数，绝不传染主路径；漏写由 reconciler 补偿。
"""
from __future__ import annotations

import logging
import os
import threading
import time

from plaintext_shadow import config as plaintext_shadow_config

log = logging.getLogger("feedling.tee_shadow")
_pool = None
_pool_dsn: str | None = None
_pool_lock = threading.Lock()
_failures = 0
_failures_lock = threading.Lock()


def enabled() -> bool:
    target = plaintext_shadow_config.load_target()
    if target is not None:
        plaintext_shadow_config.validate_startup()
        return True
    # Once DATABASE_URL is the promoted TEE database there is no shadow target.
    # Fail closed even if stale rollout secrets still carry the old dual-write
    # flag/DSN; otherwise a request could write the primary twice or resurrect
    # the Phase-3 scheduler after cutover.
    if os.environ.get("FEEDLING_DATABASE_SCHEMA", "rds").strip().lower() == "tee":
        return False
    return os.environ.get("FEEDLING_TEE_DUAL_WRITE", "") == "1" and bool(
        os.environ.get("TEE_DATABASE_URL"))


def _pool_timeout() -> float:
    # 拿连接的等待上限。影子写是 best-effort(失败被吞、reconciler 后续补齐),所以
    # 它绝不能把用户请求扣在这里——这个上限就是每次主写在 TEE 不可用时白等的时间。
    #
    # 曾放宽到 15s(16320c2),理由是网关 direct-TLS 冷握手可能 >5s,并假设 min_size=2
    # 的热连接让这条尾延迟"很少真正命中"。2026-07-13 test 实测推翻了该假设:13 分钟
    # 内 18 次 "couldn't get a connection after 15.00 sec"——瓶颈不是冷握手而是池容量
    # (max_size=4),因为当时每次 /v1/chat/poll 都驱动一次 consumer_state 影子写。
    # 那个热源已被摘除(db.set_blob 不再镜像 consumer_state),这里再把上限收回 2s 作为
    # 第二道闸:即使池再被打满,主请求最多让路 2s 而不是 15s。
    #
    # 代价(有意接受):冷握手 >2s 时该次影子写会失败而不是阻塞请求——对一个 reconciler
    # 本就会收敛的影子库,这是正确的取舍。
    try:
        return max(1.0, float(os.environ.get("FEEDLING_TEE_POOL_TIMEOUT", "2") or 2))
    except (TypeError, ValueError):
        return 2.0


def _pool_int(env: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(env, "") or default))
    except (TypeError, ValueError):
        return default


def _pool_min() -> int:
    # 热连接数。保持得多一些,突发时更少现场做网关 direct-TLS 冷握手(那条慢链路正是
    # 当年把 pool_timeout 放宽到 15s 的理由)。
    #
    # 但热连接越多、经 Phala 网关闲置越久,越容易被网关静默掐断(2026-07-14:min 从
    # 2 提到 8 后,失败从"couldn't get a connection"变成"SSL unexpected eof",chat/
    # memory 整表挂)。所以这 8 条热连接必须靠下面的 max_lifetime 主动回收 + keepalive
    # 保活,否则 min 越大陈连接暴露面越大。
    return _pool_int("FEEDLING_TEE_POOL_MIN", 8)


def _pool_max() -> int:
    # 影子写并发上限——2026-07-13 实测这是整条影子链路上唯一的约束:TEE PG
    # max_connections=200 而当时只用了 11 条,其中 app 用户恰好 4 条 = 池被自己的
    # max_size=4 顶死;41 次镜像失败全是 "couldn't get a connection",零 SSL/链路错误,
    # TEE CVM healthy。即瓶颈是我们自己设的上限,不是 DB、也不是网关。
    #
    # 定 32 的依据是 WORKERS × max_size(池是 per-worker 的),不是拍脑袋:
    #   TEE PG 200 上限(3 条 superuser 保留),owner/replicator/monitoring 等非 app
    #   角色常驻约 7 条。
    #   - 32 → 单 worker 32;即便日后跟 prod 一样开 4 worker 也才 128,尚余约 70。
    #   - 64 → 4 worker 就是 256 > 200,会把 TEE PG 打满。
    # 故 32 是「在安全前提下尽量大」的取值。内存不是约束:work_mem=4MB → 32 条最坏
    # 约 128MB,而 TEE CVM 尚有 3.2GB 空闲(PG 当前仅用 142MB)。
    #
    # 注意:prod 目前不跑双写(compose 无 TEE_DATABASE_URL/FEEDLING_TEE_DUAL_WRITE,
    # enabled() 为假 → 整条镜像是 no-op),所以这里的默认值当前只作用于 test。
    return _pool_int("FEEDLING_TEE_POOL_MAX", 32)


def _pool_max_lifetime() -> float:
    # 单条连接的最长存活(秒)后强制回收重建。TEE 走 Phala 网关的 direct-TLS,连接
    # 空闲/存活久了会被网关静默掐断,下次大写(chat 行最大)撞到死连接 → "SSL error:
    # unexpected eof" / "connection is lost"(2026-07-14 test 实测,chat/memory 整表挂)。
    # 主动在网关掐断前回收,配合下面的 keepalive,把陈连接概率压到最低;残余由
    # replicator 的换连接重试兜底。默认 180s,可配。min_size(8)越大,这条越重要。
    try:
        return max(30.0, float(os.environ.get("FEEDLING_TEE_POOL_MAX_LIFETIME", "180") or 180))
    except (TypeError, ValueError):
        return 180.0


def _statement_timeout_ms() -> int:
    # 单条语句在 TEE 服务端的执行上限(毫秒),经 options='-c statement_timeout=' 下发。
    # 补 connect_timeout/keepalives 都盖不住的一种挂死:连接已建好、query 已到达 pg,
    # 但网关把回包黑洞掉——pg 仍在跑或已跑完却送不回来,客户端无限等 recv。有了它,
    # pg 到点自己 cancel → 抛 QueryCanceled,reconcile 这趟干净失败并释放 run-lock,
    # 而不是永久 wedge 住 in-process scheduler(2026-07-14 test 实测:reconcile-first
    # 首次自动跑就卡死 28min,零 [reconcile] 日志、RDS 无 active query、TEE 库健康)。
    #
    # 默认 120s:够 prod user_logs(376MB)reconcile 的 prune 全 PK 扫描 / count(*) 跑完
    # (它们是简单扫描、只取 PK 列),又能兜住真正的服务端黑洞。热镜像写是毫秒级、
    # replicator 批写也远在其下,故单一值同时服务三条路径(mirror/replicator/reconciler)。
    try:
        return max(1000, int(os.environ.get("FEEDLING_TEE_STATEMENT_TIMEOUT_MS", "") or 120000))
    except (TypeError, ValueError):
        return 120000


def _tcp_user_timeout_ms() -> int:
    # 已发出的数据在被强制关连接前允许"未被 ACK"的上限(毫秒,libpq 参数,Linux-only;
    # CVM 是 Linux)。补 statement_timeout 盖不住的反方向挂死:出站(客户端→pg)被网关
    # 黑洞、根本没送达 pg —— 服务端没在跑任何 query,没有 statement_timeout 可 cancel,
    # keepalives 又因网关在 TCP 层回探测而看着 socket 是活的。tcp_user_timeout 让内核在
    # 出站数据 30s 收不到 ACK 时强杀连接 → 抛 OperationalError,同样让这趟干净失败。
    #
    # 默认 30s:健康连接持续被 ACK 时永不触发,只在真卡死时兜底。0 = 交给系统默认(关闭)。
    try:
        return max(0, int(os.environ.get("FEEDLING_TEE_TCP_USER_TIMEOUT_MS", "") or 30000))
    except (TypeError, ValueError):
        return 30000


def _target_dsn(policy=None) -> str:
    if policy is not None:
        return policy.dsn
    target = plaintext_shadow_config.load_target()
    if target is not None:
        plaintext_shadow_config.validate_startup()
        return target.dsn
    dsn = os.environ.get("TEE_DATABASE_URL", "").strip()
    if not dsn:
        raise RuntimeError("shadow database is not configured")
    return dsn


def get_target_pool(policy=None):
    global _pool, _pool_dsn
    dsn = _target_dsn(policy)
    if _pool is None or _pool_dsn != dsn:
        with _pool_lock:
            if _pool is None or _pool_dsn != dsn:
                from psycopg_pool import ConnectionPool
                if _pool is not None:
                    _pool.close()
                # connect_timeout:单条连接的建立上限,防止一次网关握手无限期挂住占着
                #   pool 的补给名额。
                # keepalives:内核每 30s 发 TCP 探活,尽量别让网关按空闲掐断。
                # tcp_user_timeout + statement_timeout(见上两个 helper):两个方向的
                #   "连接建好之后才挂死"兜底——前者管出站没 ACK、后者管服务端回包被吞。
                #   缺了它们,单条挂死的 TEE 写会永久 wedge 住 in-process scheduler 的
                #   run-lock(reconcile-first 卡死事故)。
                kwargs = {"autocommit": True, "connect_timeout": 10,
                          "keepalives": 1, "keepalives_idle": 30,
                          "keepalives_interval": 10, "keepalives_count": 3,
                          "options": f"-c statement_timeout={_statement_timeout_ms()}"}
                tcp_ut = _tcp_user_timeout_ms()
                if tcp_ut:
                    kwargs["tcp_user_timeout"] = tcp_ut
                _pool = ConnectionPool(
                    dsn,
                    min_size=_pool_min(),
                    max_size=_pool_max(),
                    timeout=_pool_timeout(),
                    max_idle=300,
                    # 主动回收陈连接(见 _pool_max_lifetime):min_size 的热连接不受
                    # max_idle 约束,只有 max_lifetime 能把它们在被网关掐断前换掉。
                    max_lifetime=_pool_max_lifetime(),
                    kwargs=kwargs,
                    open=True,
                )
                _pool_dsn = dsn
    return _pool


def get_tee_pool():
    """Compatibility alias for callers that mean the active shadow target."""
    return get_target_pool()


def failure_count() -> int:
    return _failures


def probe() -> dict:
    """TEE 影子库健康探活（``SELECT 1`` + 往返延迟）。绝不抛：TEE 未接/连不上都
    返回 ``ok=False`` + 简短 error,给可观测端点当结构化 health 字段用（否则连不上
    会一路 500/503,拿不到"TEE 不可达"这个本身就是信号的数据点）。

    走 ``get_tee_pool()`` 的既有池（受 ``_pool_timeout`` 上限约束),所以探活也享受
    2s 短超时——TEE 卡住时探活自己不会把调用方拖住。"""
    try:
        _target_dsn()
    except RuntimeError:
        return {"ok": False, "latency_ms": None, "error": "unconfigured"}
    t0 = time.monotonic()
    try:
        with get_tee_pool().connection() as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "latency_ms": round((time.monotonic() - t0) * 1000, 1), "error": None}
    except Exception as exc:  # noqa: BLE001 — 探活绝不上抛
        return {"ok": False, "latency_ms": None, "error": str(exc)[:200]}


def _record_failure(exc: Exception, sql: str) -> None:
    global _failures
    with _failures_lock:
        _failures += 1
    log.warning("[tee-mirror] shadow write failed (#%d): %s | sql=%.80s",
                _failures, exc, sql)


def execute(sql: str, params: tuple = ()) -> None:
    if not enabled():
        return
    try:
        with get_tee_pool().connection() as conn:
            conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001 — 影子期吞掉一切
        _record_failure(exc, sql)


# tee_pending_device_migration 的 upsert：同时服务两种用途——
#   1. requeue lane（reason LIKE 'requeue%'）：标记「同 PK 原地改写」的行，让
#      cursor 永不回头的 replicator 在下一趟 run_table 开头重新拉取转换（见
#      tee_replicator.worker 的 requeue 消费步）。
#   2. visibility_local_only：内容被 swap 成 local_only 后的终态标记（TEE 明文行
#      已被删，这行占位使 verify 的 rds == tee + pending 仍然平衡）。
# ON CONFLICT 覆盖 reason/marked_at，故一次 requeue 会盖掉旧的 local_only 标记、
# 反之亦然（controller 定案）。与 worker._PENDING_UPSERT 同一套语义。
_PENDING_UPSERT_SQL = (
    "INSERT INTO tee_pending_device_migration "
    "(user_id, table_name, item_id, reason, marked_at) VALUES (%s,%s,%s,%s, now()) "
    "ON CONFLICT (user_id, table_name, item_id) DO UPDATE SET "
    "reason = EXCLUDED.reason, marked_at = now()"
)


def mark_pending(user_id: str, table_name: str, item_id: str, reason: str) -> None:
    """尽力而为地写/覆盖一条 pending_device_migration 行（影子期吞掉失败）。"""
    execute(_PENDING_UPSERT_SQL, (user_id, table_name, item_id, reason))


def execute_many(statements: list[tuple[str, tuple]]) -> None:
    """尽力而为地把一组语句作为单个事务镜像到 TEE 影子库。

    与 `execute` 同样的 enabled() 门禁与失败吞掉语义：整组要么原子生效，
    要么任一语句失败就整组回滚且只计一次失败（不逐条计数），因为它们在
    主路径上本就属于同一次逻辑写入。
    """
    if not enabled():
        return
    try:
        with get_tee_pool().connection() as conn:
            with conn.transaction():
                for sql, params in statements:
                    conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001 — 影子期吞掉一切
        _record_failure(exc, "; ".join(sql for sql, _ in statements))
