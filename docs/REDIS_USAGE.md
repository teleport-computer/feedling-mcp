# Redis 使用文档与规范

> 面向**接入 Redis 的后端开发者**。基础设施开通/运维见
> `deploy/DEPLOYMENTS.md`「TEE Redis」章节；架构决策见
> `docs/superpowers/specs/2026-07-24-tee-redis-cvm-design.md`。
>
> **当前零流量**：三台 CVM（test/pre/prod）已 running、待命，**没有任何业务
> 代码引用 Redis**。任何一类接入（缓存 / 队列 / 锁）都各自另开 spec，并把
> 本文的规范列为前置条件。

---

## 0. 一句话心智模型

**Redis 在本架构里是纯临时层，Postgres 永远是权威源（source of truth）。**

Redis 里的每一个 key 都必须满足：**丢了不影响正确性，只影响性能**。
能满足这条，缓存 / 队列 / 锁才能安全地放进来；不能满足，就不该进 Redis。

这不是风格偏好，是这台机器的物理约束决定的（见下一节）。本文三条硬规范
（`IO:` 前缀命名、强制 TTL、read-through）都是从这个模型直接推出来的。

---

## 1. 你必须知道的硬约束

| 约束 | 后果 | 对你的要求 |
|---|---|---|
| **`noeviction` 驱逐策略** | 内存打满时**写入直接报错**（`OOM command not allowed`），而不是悄悄驱逐旧 key | 每个 key **必须带 TTL** 自然回收，否则内存只涨不落，最终全体写入失败（§3.2） |
| **无离线备份 + 纯临时层** | 实例重启 / 换 CVM = 数据可能清零，只靠 AOF 扛软重启 | 绝不把「唯一副本」放 Redis；所有值都要能从 PG 重建（§3.3） |
| **`CONFIG`/`KEYS`/`FLUSHALL`/`FLUSHDB`/`DEBUG` 被禁用** | 发这些命令返回 `unknown command` | 别用 `KEYS`（用 `SCAN`）；容量只能读 `INFO memory`；别指望运行时改配置 |
| **对外只有 gateway passthrough** | 跨 CVM 无私网，只能走 `<app-id>-6379s.…:443`，TLS + AUTH 保护 | 客户端**必须发 SNI**、必须校验 CA、必须带口令（§2） |

---

## 2. 连接

Redis 经 dstack gateway passthrough 暴露，**只有 TLS**（明文端口已关）。

- **地址**：`<app-id>-6379s.dstack-pha-prod9.phala.network`，端口 **443**
- **每环境的连接材料**都在 GitHub secret（也是 `redis-monitor` 用的那套）：
  - `<PREFIX>_REDIS_HOST` — 完整 gateway 主机名
  - `<PREFIX>_REDIS_PASSWORD` — AUTH 口令
  - `<PREFIX>_REDIS_CA_B64` — 校验用 CA（base64）
  - `<PREFIX>` = `TEST` / `PRE` / `PROD`
- 生产注入进 CVM/容器的方式与其它机密一致（加密 env），**不要把口令拼进
  命令行 / 日志 / argv**。

### ⚠️ 必须发 SNI

gateway 靠 TLS ClientHello 的 **SNI** 把连接路由到后端 CVM。不发 SNI，
gateway 找不到后端、握手时直接关连接，你只会看到 `unexpected eof`——
一个健康的 Redis 被误判成挂了。

- **好消息**：`redis-py` / 多数库在 `ssl=True` 时默认用连接主机名做
  `server_hostname`（= SNI = 完整 gateway 主机名），**开箱即对**。
- **坑**：`redis-cli --tls` 默认**不**发 SNI，必须显式 `--sni <host>`
  （`deploy/verify-redis.sh` 已处理）。任何自研/低层客户端务必确认 SNI 发对。

### Python 连接工厂（接入时新建，放哪见 `CONTRIBUTING.md`）

```python
# 目前仓库还没有 redis 客户端模块——第一个接入方按 CONTRIBUTING.md 的
# 依赖方向新建，例如 backend/cache/redis_client.py。示例用 redis-py(async)，
# 属示意；确切的 TLS 参数名以你用的 redis-py 版本为准。
import base64, os, tempfile
import redis.asyncio as redis

def make_redis() -> redis.Redis:
    # CA 经 base64 注入 → 落一个临时文件给 ssl_ca_certs（跨版本最稳的写法；
    # 新版 redis-py 也可直接用 ssl_ca_data 传 PEM 文本，免落盘）。
    ca_pem = base64.b64decode(os.environ["REDIS_CA_B64"])
    ca_file = tempfile.NamedTemporaryFile(suffix=".crt", delete=False)
    ca_file.write(ca_pem); ca_file.flush()

    host = os.environ["REDIS_HOST"]              # 完整 gateway 主机名
    return redis.Redis(
        host=host, port=443,
        password=os.environ["REDIS_PASSWORD"],
        ssl=True,
        ssl_ca_certs=ca_file.name,               # 校验 CA
        ssl_cert_reqs="required",                # verify-full
        ssl_check_hostname=True,
        # redis-py 在 ssl=True 时默认用 host 做 TLS server_hostname(=SNI)，
        # 而 host 就是完整 gateway 主机名 → SNI 自动对，无需额外设置。
        decode_responses=False,                  # 存字节，序列化由你控制
        socket_timeout=3, socket_connect_timeout=3,
        health_check_interval=30,
    )
```

**连不上就降级到 PG，绝不阻塞主流程。** Redis 是加速层，它挂了业务必须
仍能用（走 PG）——这也是「纯临时层」的另一面。给所有 Redis 调用套超时 +
`try/except`，异常时当作 cache miss 处理。

---

## 3. 三条硬规范

### 3.1 命名：所有 key 加 `IO:` 前缀

**每一个 key 都以 `IO:` 开头**，其后用 `:` 分层组织命名空间：

```
IO:<domain>:<entity>[:<id>][:<field>]
```

- `<domain>` — 用途大类：`cache` / `lock` / `rl`（rate-limit）/ `queue` …
- 之后按业务实体细分，**用户相关的 key 一律带 `usr_` 前缀的 user_id**。

示例：

| key | 含义 |
|---|---|
| `IO:cache:user:usr_ab12:profile` | 某用户 profile 的缓存 |
| `IO:cache:worldbook:usr_ab12:v3` | 世界书条目缓存（带版本号，避免脏读） |
| `IO:lock:redistill:usr_ab12` | 该用户 redistill 的互斥锁 |
| `IO:rl:provider:openai:usr_ab12` | 该用户对 openai 的限流窗口 |

为什么强制前缀：

1. **一眼归属**：`SCAN IO:cache:*` 能安全圈定某类 key（`KEYS` 被禁，只能
   `SCAN`），排查 / 手动清理不会误伤别人。
2. **未来多租户 / 多用途共享同一实例**时，前缀是唯一的软隔离边界——没有
   前缀纪律，缓存、锁、队列的 key 混在一个平坦空间里迟早互相踩。
3. **命名即契约**：key 里带版本号 / user_id，能从根上避免「换了数据结构却
   命中旧缓存」这类脏读。

> 约定：key 用 ASCII，段之间只用 `:`，不要在段内再塞 `:`；user_id 段保持
> 完整（`usr_…`），不要截断。

### 3.2 存进去的数据必须带 TTL

**没有例外。每个 `SET` 都要带过期时间**（`SET key val EX <秒>` / `SETEX`）。

- `noeviction` 下，无 TTL 的 key **永远不会被回收**，内存单调上涨，最终
  触发 `OOM`，**全实例写入开始报错**（不是只影响你的 key，是拖垮所有人）。
- 参考区间（按用途，具体值在各接入 spec 里定）：

| 用途 | TTL 量级 | 说明 |
|---|---|---|
| 热数据缓存 | 分钟 ～ 小时 | 越是「可容忍轻微陈旧」的，TTL 越长 |
| 分布式锁 | 秒 ～ 低分钟 | 必须 ≥ 临界区最坏耗时，且配 owner token（§4）|
| 限流窗口 | = 窗口长度 | 窗口滑动靠 TTL 自然过期 |

- **如果你觉得某个 key「必须永久存在、不能过期」——那它就不该在 Redis 里，
  它属于 Postgres。** 这条判据能帮你在写代码前就发现放错了地方。
- 写缓存统一用「带 TTL 的 set」，不要先 `SET` 再 `EXPIRE`（两步之间进程
  崩溃会留下一个永不过期的 key，正是要避免的）。

### 3.3 读优先：先 GET，miss 再从 PG 取并 SET（read-through）

标准访问模式是 **cache-aside / read-through**：

```
1. 从 Redis GET
2. 命中 → 直接返回
3. 未命中 → 从 Postgres（权威源）读/算 → 带 TTL 写回 Redis → 返回
```

- 这直接体现「PG 是权威源、Redis 是加速层」：**miss 从来不是错误**，
  Redis 空了（重启/换 CVM）业务也只是第一次慢一点，从 PG 自然回暖。
- **写路径要让缓存失效**：更新 PG 后，`DEL` 对应 key（或写一个带新版本号
  的新 key），否则会命中旧值。缓存 key 里带版本号能让这一步更稳。

```python
import json

async def get_or_load(r, key: str, ttl: int, loader):
    """read-through：先 Redis，miss 再走 loader(PG) 并回填。
    Redis 异常一律当 miss，绝不因缓存层故障阻塞主流程。"""
    try:
        cached = await r.get(key)
        if cached is not None:
            return json.loads(cached)
    except Exception:
        pass  # 缓存层故障 → 降级到 PG

    value = await loader()            # 从 Postgres 读/算（权威源）
    try:
        # 带 TTL 的原子写：绝不留无过期的 key（§3.2）
        await r.set(key, json.dumps(value), ex=ttl)
    except Exception:
        pass  # 回填失败无所谓，下次再来
    return value

# 用法
profile = await get_or_load(
    r,
    key=f"IO:cache:user:{user_id}:profile",
    ttl=300,
    loader=lambda: db.load_user_profile(user_id),
)
```

> **惊群（thundering herd）**：某个热 key 过期的瞬间，大量请求会同时 miss、
> 同时打 PG。热点 key 可用一把短 TTL 的锁（§4）做 single-flight，只让一个
> 请求回源、其余短暂等待或返回稍旧值。是否需要在各接入 spec 里评估，别默认
> 上，也别默认不上。

---

## 4. 锁 / 限流的最小安全姿势

锁和队列同样受三条规范约束（`IO:` 前缀、带 TTL、PG 权威）。额外注意：

- **锁必须带 owner token + TTL**：`SET IO:lock:<...> <token> NX EX <秒>`。
  TTL 防死锁（持锁者崩溃后锁自动释放）；释放时用 Lua **比对 token 再删**，
  避免删掉别人续上的锁：

  ```python
  RELEASE = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
  # 加锁
  token = secrets.token_hex(16)
  got = await r.set(f"IO:lock:{name}", token, nx=True, ex=30)
  # 释放（只删自己那把）
  await r.eval(RELEASE, 1, f"IO:lock:{name}", token)
  ```

- **锁不是权威真相**：进程重启 / Redis 清空都会让锁消失。锁只用来「减少
  并发冲突」，正确性仍要靠 PG 侧的幂等 / 唯一约束 / `SELECT … FOR UPDATE`
  兜底。现有 Runtime V2 的抢占就是 PG `SKIP LOCKED`——Redis 锁是优化，不是
  替代。

---

## 5. 别踩的坑（速查）

- ❌ `KEYS IO:cache:*` → 命令被禁 + 会阻塞整个实例。用 `SCAN`（游标分批）。
- ❌ `SET key val`（无 TTL）→ noeviction 下永不回收，拖垮全实例写入。
- ❌ 把唯一数据只写 Redis → 重启即丢。权威数据进 PG。
- ❌ 缓存层故障时让请求 5xx → 必须降级到 PG。Redis 是加速层不是依赖。
- ❌ 口令 / CA 拼进 argv 或日志 → 走加密 env，从机密文件现读。
- ❌ 自研客户端不发 SNI → gateway 恒 `unexpected eof`（§2）。
- ❌ 更新 PG 后不失效缓存 → 命中旧值。DEL 或用带版本号的新 key。

## 6. 接入前 checklist

- [ ] 另开一份接入 spec，把「Redis 冷启动可容忍」写进前置条件
- [ ] 所有 key 走 `IO:<domain>:…` 命名
- [ ] 每个写入都带明确 TTL（并在 spec 里定死每类 key 的 TTL）
- [ ] 访问走 read-through，miss 从 PG 回源
- [ ] Redis 调用全部带超时 + 异常降级到 PG
- [ ] 写路径失效对应缓存
- [ ] 连接发 SNI、校验 CA、口令走加密 env
- [ ] 评估该 key 类是否需要 single-flight（惊群）
