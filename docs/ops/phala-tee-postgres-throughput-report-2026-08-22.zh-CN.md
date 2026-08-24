# 支持请求：Phala dstack PostgreSQL 网关在大结果集场景下出现严重吞吐退化

**测试日期：** 2026-08-22  
**建议严重级别：** 高（对于数据库主库负载）  
**受影响路径：** 通过 `*-5432s.dstack-pha-prod9.phala.network:443` direct TLS 访问 PostgreSQL  
**未受影响路径：** 在数据库 CVM 内部本地访问 PostgreSQL

## 摘要

当 PostgreSQL 客户端通过 dstack `5432s` 网关，从部署在另一台 Phala CVM 中的 PostgreSQL 服务器读取普通查询结果集时，我们观察到了严重且与结果集大小高度相关的吞吐退化。

在复用连接的情况下，小型 `SELECT 1` 查询只比 AWS RDS 基线慢 3%–15%。但从同一台运维工作站测试时，通过 TEE PostgreSQL 公网网关读取普通 1 MiB 结果集的耗时约为 RDS 的 13–14 倍。把客户端移到另一台 Phala CVM 内部后，问题仍然可以复现，因此可以排除运维工作站 ISP 是问题发生的必要条件。

PostgreSQL 服务器本身看起来不是瓶颈：

- 使用 `EXPLAIN ANALYZE` 测量但不传输完整结果时，合成结果集的生成耗时不到 1 ms。
- 在 PostgreSQL CVM 内部，本地客户端读取普通 1 MiB 结果集约需 0.127 秒。
- 在 PostgreSQL CVM 内部，通过 `COPY TO STDOUT` 读取 8 MiB 约需 0.081 秒，即约 99 MiB/s。
- 同一类普通结果集一旦跨 Phala CVM 传输，吞吐降至约 0.1–0.4 MiB/s。

现有证据把瓶颈定位在远程 PostgreSQL 传输边界：direct TLS、dstack 网关及跨 CVM 数据路径。我们尚未进一步区分具体机制是网关限速、缓冲行为、TCP 流控、重传、TLS 行为，还是与 PostgreSQL 普通行结果协议之间的交互。

## 环境

所有 TEE PostgreSQL 端点均使用：

- 网关：`dstack-pha-prod9.phala.network`
- 外部端口：`443`
- PostgreSQL 服务映射：`5432s`
- TLS：`sslmode=verify-full`
- libpq 协商：`sslnegotiation=direct`
- 客户端工具：PostgreSQL `psql`/`pgbench` 18.1
- RDS 服务端版本：PostgreSQL 17.9
- TEE PostgreSQL 服务端版本：PostgreSQL 17.10

测试没有读取任何客户数据。所有吞吐测试均使用 PostgreSQL 动态生成的合成字符串。

### 相关 CVM

| 环境 | 应用 CVM | PostgreSQL CVM / 网关 app ID | 测试时的数据库角色 |
|---|---|---|---|
| test | `feedling-io-test`（`173c7f49aeb54acb424676b17b17f78e5e2b2938`） | `feedling-io-db-test`（`ca2317945ce4c95f5d5a9df60676a74d59197dcc`） | TEE PostgreSQL 主库 |
| pre | `feedling-io-pre`（`7d18a1f234a0d90e5f643cac8283b6048451b8f7`） | `feedling-io-db-pre`（`ade3cabf133ec3e9ee6220265843c4ac993e1e63`） | TEE PostgreSQL 主库 |
| prod | `feedling-enclave-v2`（`9798850e096d770293c67305c6cfdceed68c1d28`） | `feedling-io-db-prod`（`4903a1288de38b1b5cfccda6aca1cbc3715090bf`） | RDS 主库；TEE DB 从外部及库内测试 |

尽管 test/pre 的客户端和数据库都部署在 Phala 上，应用 CVM 仍通过公网 dstack `5432s:443` 主机名连接 TEE PostgreSQL 主库。

## 测试方法

### 普通结果集查询

测试数据在内存中生成，并以普通 PostgreSQL 文本行返回：

```sql
-- 总计 1 MiB
SELECT repeat('x', 65536)
FROM generate_series(1, 16);

-- 总计 8 MiB
SELECT repeat('x', 65536)
FROM generate_series(1, 128);

-- 总计 64 MiB
SELECT repeat('x', 65536)
FROM generate_series(1, 1024);
```

在运维工作站测试中，`pgbench` 使用一个持久客户端和自定义查询文件。事务延迟不包含单独报告的初始连接时间：

```bash
pgbench -n -f read-1m.sql -c 1 -j 1 -t 3 '<已脱敏的 DSN>'
```

在应用 CVM 测试中，Python 和 psycopg 使用容器现有的 `DATABASE_URL`，执行相同查询，调用 `fetchall()`，验证接收字节数，并用单调时钟测量耗时。测试过程中没有打印凭据，也没有在 CVM 之间搬运凭据。

### 服务端执行对照

我们对相应查询执行了：

```sql
EXPLAIN (ANALYZE, TIMING OFF, SUMMARY ON)
SELECT repeat('x', 65536)
FROM generate_series(1, 1024);
```

该测试测量结果生成过程，但不会通过客户端连接传输完整载荷。所有被测端点的执行时间均低于 1 ms。这说明与观测到的传输耗时相比，数据生成及数据库执行开销可以忽略。

## 测试结果

### 1. 小查询基线

使用一个持久连接连续执行 50 次 `SELECT 1`：

| 环境 | RDS 平均延迟 | TEE 平均延迟 | TEE 额外开销 |
|---|---:|---:|---:|
| test | 241.6 ms | 259.2 ms | +7% |
| pre | 250.5 ms | 287.1 ms | +15% |
| prod | 260.0 ms | 268.0 ms | +3% |

这说明主要退化与结果集大小相关，并不是所有查询都会承受相同幅度的执行惩罚。

### 2. 运维工作站：普通 1 MiB 结果集

TEE 数值是同一持久连接中三次事务的平均值。RDS 数值是三次单事务测试的中位数；两者的 `pgbench` 事务延迟均不包含初始连接时间。

| 环境 | RDS 延迟 | RDS 吞吐 | TEE 延迟 | TEE 吞吐 | 相对延迟 |
|---|---:|---:|---:|---:|---:|
| test | 1.469 s | 0.681 MiB/s | 20.606 s | 0.049 MiB/s | TEE 慢 14.0 倍 |
| pre | 1.736 s | 0.576 MiB/s | 22.599 s | 0.044 MiB/s | TEE 慢 13.0 倍 |
| prod | 1.717 s | 0.582 MiB/s | 21.777 s | 0.046 MiB/s | TEE 慢 12.7 倍 |

工作站链路存在波动。后续一次 prod TEE 普通 1 MiB `SELECT` 测试包含建连共耗时 10.929 秒，而此前复用连接的事务平均耗时为 21.777 秒。尽管存在波动，TEE 普通结果集传输仍明显慢于 RDS。

作为额外的 RDS 对照，test RDS 在 8 MiB 事务中维持约 4.4 MiB/s，在一次 64 MiB 事务中达到约 5.7 MiB/s，没有出现同样持续的低吞吐现象。

### 3. 从应用 CVM 内部测试

这组测量从数据路径中排除了运维工作站和 ISP。

| 客户端路径 | 三次 1 MiB 耗时 | 1 MiB 中位吞吐 | 8 MiB 耗时 | 8 MiB 吞吐 |
|---|---|---:|---:|---:|
| test 应用 CVM → test TEE DB | 5.264 s、2.390 s、3.766 s | 0.266 MiB/s | 60.312 s | 0.133 MiB/s |
| pre 应用 CVM → pre TEE DB | 10.251 s、11.170 s、3.978 s | 0.098 MiB/s | 37.795 s | 0.212 MiB/s |
| prod 应用 CVM → RDS | 0.509 s、0.531 s、0.846 s | 1.882 MiB/s | 1.534 s | 5.216 MiB/s |

在 8 MiB 测试中，应用 CVM 到 RDS 的链路比应用 CVM 到 TEE 的链路快约 25–39 倍。

没有从 prod 应用 CVM 内部测试 prod TEE 端点，因为 prod 当前以 RDS 为主库，并且 backend 按设计没有注入 TEE DSN。我们没有为了本次测试把生产凭据复制进应用 CVM。

### 4. 在 TEE PostgreSQL CVM 内部测试

在 `feedling-io-db-prod` 内使用本地客户端：

| 操作 | 载荷 | 耗时 | 近似吞吐 |
|---|---:|---:|---:|
| 普通 `SELECT` 结果 | 1 MiB | 0.127 s | 7.9 MiB/s |
| `COPY TO STDOUT` | 8 MiB | 0.081 s | 99 MiB/s |

这些命令包含本地客户端进程及建连开销，因此对数据库本地吞吐的估计偏保守。结果证明：在引入远程网关路径之前，PostgreSQL 可以快速生成并交付相同载荷。

### 5. 与协议相关的差异

在 prod TEE 公网端点上：

- 最终复核中，普通 1 MiB `SELECT` 包含建连共耗时 10.929 秒。
- 1 MiB `COPY TO STDOUT` 包含建连共耗时 3.195 秒。

普通行结果和 COPY 之间存在巨大差异，说明问题并非单纯的物理网卡固定带宽上限。PostgreSQL 结果帧、代理缓冲/背压、TLS record 处理或 TCP 流控行为都可能参与其中。

## 已确认事实

1. 性能退化与结果集大小高度相关。
2. 问题可以在相互独立的 test、pre、prod TEE PostgreSQL CVM 上复现。
3. 当客户端位于另一台 Phala CVM 时问题仍能复现，因此运维工作站 ISP 不是触发问题的必要条件。
4. 服务端生成合成载荷的耗时低于 1 ms。
5. 数据库 CVM 内部的本地结果传输速度正常。
6. 严重退化只在 PostgreSQL 结果经过远程 `5432s:443` dstack 路径时出现。
7. 在本次样本中，PostgreSQL 普通行结果的退化明显重于 COPY 流量。

## 当前故障边界

现有证据把问题定位在远程 PostgreSQL 客户端与 PostgreSQL 容器之间，即 direct TLS、dstack 网关及跨 CVM 路径。

目前证据尚不足以区分以下具体原因：

- 网关按连接或服务配置的带宽限制；
- 代理对 PostgreSQL 行结果的缓冲或背压行为；
- TCP 拥塞窗口、接收窗口、MTU 或重传问题；
- TLS record 大小或加密路径行为；
- `5432s` 流代理与 PostgreSQL 协议之间的特定交互。

## 希望 Phala 协助排查

请 Phala 团队协助确认以下事项：

1. `5432s` direct-TLS 端口映射是否存在已记录或实际配置的带宽、消息大小、缓冲或限速约束。
2. 检查 2026-08-22 测试期间三个数据库 app ID 对应的网关及宿主机指标。
3. 在 `dstack-pha-prod9` 的两台 CVM 之间复现 PostgreSQL 普通 `SELECT` 与 `COPY TO STDOUT` 的吞吐差异。
4. 检查 `5432s:443` 路径上的 TCP 重传、拥塞/接收窗口、MTU、流控停顿及流代理缓冲行为。
5. 使用 `iperf3` 或等效工具，在相同 CVM 之间测量原始 TCP 吞吐，并与数据库路径比较。
6. 确认是否可以提供 CVM 间私有地址或私有服务路由，使数据库流量绕过公网网关。
7. 确认当前 dstack ingress/流代理版本是否存在已知的 PostgreSQL 大结果集性能问题。

## 用户影响

test 和 pre 已经以各自的 TEE PostgreSQL 为主库。即使 PostgreSQL 本身执行很快，大段历史记录、JSON 文档、导出、管理查询或返回大量行的查询仍可能耗时数十秒。连接池能够降低建连成本，但无法解决结果集传输吞吐问题。

本报告测试时 prod 仍以 RDS 为主库。在修复传输瓶颈或提供明显更快的私有数据库路径之前，这个问题会阻碍将 TEE PostgreSQL 提升为生产主库。

## 安全说明

- 所有测试均为只读。
- 没有执行 DDL 或写入操作。
- 没有查询或传输客户数据。
- 测试载荷只包含动态生成的 `x` 字符。
- 文档有意省略密码、DSN、token 及证书路径。

