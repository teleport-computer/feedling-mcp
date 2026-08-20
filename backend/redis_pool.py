"""已废弃的 Redis 连接池（TEE Redis CVM 客户端）。

.. deprecated:: 2026-08-20
   三套 Redis CVM 已暂停，且本模块从未接入业务请求路径。实现暂时保留用于
   审计和未来评估，但 ``redis_configured()`` 固定返回 ``False``，
   ``get_redis()`` 固定拒绝构造客户端。重新启用前必须先完成新的接入 spec、
   恢复基础设施与监控，并移除这里的退役门禁。

低层基础设施客户端，**无业务依赖**——与 object_storage.py / db.py 同层，
任何领域包都可向下 import（依赖方向见 CONTRIBUTING.md §2）。使用规范
（`IO:` 前缀命名、强制 TTL、read-through、缓存故障降级 PG）见
`docs/REDIS_USAGE.md`；开通/运维见 `deploy/DEPLOYMENTS.md`「TEE Redis」。

定位：Redis 是**纯临时层**，Postgres 永远是权威源。故本模块的所有失败都
应被调用方当作 cache miss 处理并降级到 PG，**绝不让 Redis 故障阻塞主流程**。

约束落进本模块的默认值：
- 连接经 dstack gateway passthrough，只有 TLS（明文端口已关）。redis-py 在
  ``ssl=True`` 时用连接主机名做 TLS ``server_hostname``（= SNI），而 host 就是
  完整 gateway 主机名 → SNI 自动发对，无需额外设置（redis-cli 才要显式 --sni）。
- 每 worker 一个有界连接池（``REDIS_MAX_CONNECTIONS``，默认 16，对齐 DB 池），
  池耗尽/超时/连不上都抛异常，由调用方降级。
- 模块 import 阶段不读环境、不建连接（CONTRIBUTING.md §4）——客户端在首次
  ``get_redis()`` 时惰性构造，redis-py 再在首条命令时才真正建 TCP。
"""

from __future__ import annotations

import base64
import os
import tempfile
import threading
from typing import Optional

import redis.asyncio as aioredis

# 进程内单例。归属本模块，别处只经 get_redis()/close_redis() 访问，不复制引用
# （CONTRIBUTING.md §4）。构造受锁保护：ASGI 线程池下可能并发首调。
_client: Optional["aioredis.Redis"] = None
_lock = threading.Lock()
# 解码出的 CA 落一个进程生命周期内稳定的临时文件（redis-py 4.x 的
# ssl_ca_certs 只吃文件路径，不吃 PEM 文本；5.x 才有 ssl_ca_data）。
_ca_file_path: Optional[str] = None

_DEFAULT_PORT = 443
# 每进程池上限。连接预算（接入满载）：backend FEEDLING_BACKEND_WORKERS=6 → 每
# worker 一个进程一个单例池 = 6×16=96；serve-worker 单进程（FEEDLING_V2_MAX_WORKERS
# 是进程内并发、共享同一单例池）= ≤16；主 CVM 合计 ≈112，远低于 Redis 默认
# maxclients=10000（redis.conf 未下调；容器 nofile 默认 1024 时 Redis 会自动把
# maxclients 降到 ~992，仍 ≫112）。池惰性建连（上限非预分配），空闲近 0。不同于
# Postgres 紧绷的 max_connections，这里无调参压力。非阻塞池：单 worker 并发命令
# 超上限时抛错 → 调用方降级 PG（缓存可接受）；某条热路径真需要再调 REDIS_MAX_CONNECTIONS。
_DEFAULT_MAX_CONNECTIONS = 16

_DEPRECATION_MESSAGE = (
    "redis_deprecated: Redis 基础设施已于 2026-08-20 暂停，客户端入口已退役；"
    "重新启用前需先完成新的接入 spec 和基础设施评审"
)


def redis_configured() -> bool:
    """退役期间固定返回 ``False``，即使遗留环境变量仍然存在。"""
    return False


def _ca_certs_path() -> Optional[str]:
    """把 REDIS_CA_B64（base64 的 CA PEM）落成临时文件，返回路径；缺失返回 None。

    只落一次，路径在进程生命周期内复用。REDIS_CA_FILE 若直接给了文件路径则
    优先用它（本地/测试方便）。
    """
    global _ca_file_path
    direct = os.environ.get("REDIS_CA_FILE")
    if direct:
        return direct
    if _ca_file_path is not None:
        return _ca_file_path
    b64 = os.environ.get("REDIS_CA_B64")
    if not b64:
        return None
    pem = base64.b64decode(b64)
    fd = tempfile.NamedTemporaryFile(prefix="redis-ca-", suffix=".crt", delete=False)
    try:
        fd.write(pem)
        fd.flush()
    finally:
        fd.close()
    _ca_file_path = fd.name
    return _ca_file_path


def _build_client() -> "aioredis.Redis":
    host = os.environ.get("REDIS_HOST")
    if not host:
        raise RuntimeError(
            "redis_not_configured: REDIS_HOST 未设置 — 调用方应先 redis_configured() 判断，"
            "未配置时走 Postgres"
        )
    password = os.environ.get("REDIS_PASSWORD")
    if not password:
        # fail-closed：TEE Redis 只有 TLS+AUTH 一层保护，绝不无口令连公网端点。
        raise RuntimeError("redis_password_missing: REDIS_PASSWORD 未设置，拒绝无鉴权连接")
    ca_certs = _ca_certs_path()
    if not ca_certs:
        raise RuntimeError(
            "redis_ca_missing: REDIS_CA_B64/REDIS_CA_FILE 未设置 — 拒绝不校验证书的 TLS 连接"
        )

    port = int(os.environ.get("REDIS_PORT", _DEFAULT_PORT))
    max_conn = int(os.environ.get("REDIS_MAX_CONNECTIONS", _DEFAULT_MAX_CONNECTIONS))

    # redis-py 内部按 max_connections 维护一个连接池；ssl=True 时用 host 做
    # TLS server_hostname(=SNI)，正好等于完整 gateway 主机名。所有 socket 操作
    # 带超时：Redis 卡住不能拖垮请求，超时即异常 → 调用方降级到 PG。
    return aioredis.Redis(
        host=host,
        port=port,
        password=password,
        ssl=True,
        ssl_ca_certs=ca_certs,
        ssl_cert_reqs="required",   # verify-full：校验证书链
        ssl_check_hostname=True,    # 校验主机名（钉住 gateway 域名）
        max_connections=max_conn,
        socket_timeout=3,
        socket_connect_timeout=3,
        health_check_interval=30,
        decode_responses=False,     # 存字节，序列化由调用方控制
    )


def get_redis() -> "aioredis.Redis":
    """拒绝构造客户端；实现仅为未来重新评审保留。"""
    raise RuntimeError(_DEPRECATION_MESSAGE)


async def close_redis() -> None:
    """关闭共享客户端与其连接池。由 lifespan 关停时调用；幂等。"""
    global _client
    client = _client
    _client = None
    if client is not None:
        # redis-py 5.x: aclose()；4.x: close()。两者都释放连接池。
        closer = getattr(client, "aclose", None) or getattr(client, "close", None)
        if closer is not None:
            result = closer()
            if hasattr(result, "__await__"):
                await result


def _reset_for_test() -> None:
    """测试钩子：丢弃单例与缓存的 CA 路径，让下个 get_redis() 按当前 env 重建。"""
    global _client, _ca_file_path
    _client = None
    _ca_file_path = None
