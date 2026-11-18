---
document_lifecycle: current
canonical_owner: self
---
# trace_events 分区维护手册（RDS / TEE）

`trace_events` 按北京时间日分区。**维护永久保留所有 trace，不因年龄删除行或
DROP 日分区**；没有 retention 参数。项目整体暂停后的删除需要另行决策，本命令
不提供删除模式。用户显式清除 trace 的现有 API 属另一条产品语义，本次不修改。

应用角色不做 DDL。维护必须在请求服务之外，使用目标库 owner/迁移凭据；先核对
当前 selected primary，不能因为工作流名字含 RDS 就把它当成当前 TEE 活库。
DEFAULT 保证漏建分区时仍可写入，但非空始终是降级信号。

## 凭据和执行入口

| 入口 | 凭据 | 目标 |
|---|---|---|
| **TEE trace partition maintenance** | `TEST_TEE_MIGRATION_DSN` / `PROD_TEE_MIGRATION_DSN`，以及对应 `*_TEE_PG_CA_PEM` | 当前 test / prod TEE |
| **TEE migrate** 的维护步骤 | 对应环境 `*_TEE_MIGRATION_DSN` + CA | 迁移后同一 TEE 库，同一无删除命令 |
| **RDS trace partition maintenance** | `TEST_DATABASE_URL` / `PRE_DATABASE_URL` / `DATABASE_URL` owner DSN | 运维确认仍使用 RDS 的环境；手动入口保留 |

TEE 使用 `sslmode=verify-full&sslnegotiation=direct` 和该环境 CA。缺 DSN/CA
必须失败，不回退到其他环境。不要在日志中打印连接串、CA 或密码。

本地/自部署命令（用 secret manager 注入 DSN，不把值写进历史）：

```sh
PYTHONPATH=backend python -m admin.trace_events_partitions --repair-default \
  --future-days 60 --lock-timeout-ms 2000 --statement-timeout-ms 30000
```

DSN 从 `TRACE_EVENTS_MIGRATION_DATABASE_URL` 读取，兼容原 TEE 迁移调用的
`TEE_MIGRATION_DATABASE_URL`。不使用应用 DSN 作为兜底。未来天数限制为 0..366。**只有显式 `--repair-default` 才搬迁 DEFAULT**；它表示已安排的
手动维护窗，不是生产授权。无该参数（含 TEE 迁移附带维护、RDS 手动工作流）只补未来
空日期，保留积压并报告 degraded。每日工作流显式使用 `--scheduled`，不能同时指定
`--repair-default`。

自动路径以超过10,000行或8 MiB标记大积压（诊断分流阈值，不是安全耗时保证），对
所有积压都采用更保守的人工搬迁策略。遇未来日期在 DEFAULT 中已有数据则跳过该日；
对其余未来日，锁等待上限100ms、单语句上限250ms，不能被 CLI 较大超时覆盖。
因 ATTACH 校验仍可能扫描大 DEFAULT，未来日也可能被延后；必须输出具体 `deferred`
日期/原因并报 degraded，不能把未挂载的空表当成有效分区。这样不会每日自动重试
30秒的排空锁窗；积压和未补齐窗口保留健康告警，交人工窗口处理。

## 每日自动化与发布边界

`.github/workflows/tee-trace-partitions.yml` 每日 UTC 19:37（北京时间次日03:37）
运行。test/prod 分别 checkout `test`/`main`，独立并发组和失败结果；pre 已暂停，
不加入定时矩阵。TEE迁移与手动RDS入口也调用同一无删除实现。

**GitHub schedule 只从默认分支 main 生效。合入 test 仅交付候选，不代表生产
自动维护已经启用。** 先完成 test 实跑和独立验收，再由 Seven 批准生产推广与首跑，
由 Supervisor 安排。三个 workflow 在执行前验证 checkout 的模块具有无删除标记且
maintain 不接受 retention_days；旧 main 删除版会失败，不能仅靠开关顺序保护。
**main 推广前不得 dispatch prod 的旧 tee-migrate/rds 维护 workflow**；旧 main
上尚不存在本 PR 的守卫，本 PR 无法从 test 修补旧 main 的 workflow 文件。
生产 job 另要求仓库变量 `TRACE_PARTITIONS_PROD_ENABLED=true`，
未批准前不要设置；该开关同时约束定时和手动生产入口。代码在 test 时可以从该分支
手动触发 TEE 工作流，仅选 test，确认 `MAINTAIN-TEE-TRACE`。

生产手动确认词为 `MAINTAIN-TEE-TRACE-PROD`，不能替代发布授权。旧 RDS 入口
确认词仍为 `MAINTAIN-RDS-TRACE` / `MAINTAIN-RDS-TRACE-PROD`。

## 事务和锁的实际边界

1. 读取 DEFAULT 中**实际存在**的北京日期，合并今天..今天+N；不补历史空白日，
   不通过延长 retention 预建大量空表。
2. 缺失日期逐日独立事务处理：先创建 `LIKE trace_events INCLUDING ALL` 独立表，
   预复制该日行并建索引，准备阶段只持允许普通读写的锁。再锁 parent（ONLY，不递归锁
   所有旧日分区）及 DEFAULT，在锁内移除已经被用户显式清除的副本、核对更新/新增行，
   只插入新增副本并更新改变的副本，再删除该日 DEFAULT 源行，校验副本与删除计数一致，最后
   `ATTACH PARTITION`。副本不会在挂载前对经父表的读者可见，源行在此之前一直保留。
   DELETE 只用于搬迁或丢弃失效副本，不存在过期清理；复制期间的清除不会被恢复。
   直接 CREATE PARTITION OF 在 DEFAULT 有该日数据时会失败，故必须先搬再 ATTACH。
3. ATTACH 校验日表及 DEFAULT；失败恢复该批所有行和 DDL。先前已成功日期保留，
   重跑安全、幂等。没有整次运行的全局回滚承诺。不要在外层事务中调用 `maintain()`，
   它会拒绝，避免 savepoint 将锁延长到整轮结束。
4. parent 的 ACCESS EXCLUSIVE 会阻塞写入和经 parent 的读取；每个批次结束释放。
   DEFAULT 在 ATTACH 时仍可能被扫描，逐日不等于完全在线或毫秒级。遇锁等待超过2s、
   单语句超过30s则该批回滚并失败；语句超时不是整个批次耗时上限。
5. session advisory lock 防止两路维护同时运行；结束/异常释放，断线由PG释放。
   若运行期间有新的未覆盖日期落入 DEFAULT，最终检查非空报错，数据保留，检查后重跑。

## 读数与验收

JSON report 包含 `mode: repair/future_only`、`large_backlog`、`deferred`、`retention: forever`、`default_rows_before/after`、`moved_rows`、
`created` 和逐日 `batches`（准备、锁等待、delta同步、源行删除、ATTACH/commit、持 parent 锁至提交的秒数及搬迁行数）。
最终 DEFAULT 非空或任何日期 deferred，命令均非零退出（degraded），不会伪报已修复。并发写入时 before/moved
不必相等，完整性用固定行集合或静默窗口核对，不能把零 DEFAULT 当成总数守恒证明。

运行前后用只读事务记录 DEFAULT 行数/时间范围、分区边界、总行数、容量和健康字段；
保留去值命令与钥匙名。首次积压修复应先在本地用等量行数/物理字节实测，记录最慢
批次与并发写入等待；本地耗时不能当作生产保证。生产磁盘/IO与数据按日分布不同，
首跑仍应选低峰，监督 statement/lock timeout，禁止盲目放大超时。
搬迁期间同时需要新日表和 DEFAULT 原文件空间；DELETE 后旧文件不会立即缩小，
普通 autovacuum 可复用空间但不保证还给文件系统。先核对磁盘余量，不能为减小文件
顺带执行 VACUUM FULL / DROP / TRUNCATE；这些均不属于本命令。

健康检查保留 `default_partition_nonempty`、`partition_horizon_low` 和容量告警；
旧日分区不再产生 `expired_partition_present`。`retention_days` 及无限期总容量
预测 `projected_retained_bytes` 为 null；明确报 `retention: forever`、
`projection_days: 30` 与未来30日的 `projected_growth_bytes`。容量判断使用现有
字节加该增长预测；无限保留需要持续扩容/备份，不通过删除证据消除容量告警。


## 首次积压修复的选择（待批准，不能把候选当生产已执行）

A. **保持全量读可见的维护窗**：本实现保留源行直到最后日事务，期间取排他锁，
阻塞经父表的读写。修复前用实际等量基准给出最慢批及整轮时间，交 Seven 决定窗口。
`insert_trace_events_strict` 等锁时占用普通连接。源码缺省普通池16/进程，当前候选
prod compose 配置24/进程，borrow超时10秒；这些是配置，未独立测量live值。若24个
请求均等 trace 锁，同池非trace请求也可能排队并10秒超时；不能承诺影响只限trace。

B. **短事务分批搬到未挂载表**：若提前提交移出的行，它们在 ATTACH 前无法由父表
查询到，读路径暂时缺失，影响诊断/计数/时间窗查询。本任务没有实现或测量该方案，
不能给出已测不可见时长；它改变诊断可见性，需要 Seven 做产品取舍。默认不采用。

本任务的 A/B 评估不代替 Seven 的选择。生产首次执行、超时预算及任何暂停写入
安排均待 test 实跑结果和明确批准。用户显式 `clear_trace_events_strict` 及
`delete_trace_events_for_user` 的物理清除语义不在本次维护范围，原样保留；后者的
源码注明账号删除不得调用，本次未把它误归为账号级联删除。
