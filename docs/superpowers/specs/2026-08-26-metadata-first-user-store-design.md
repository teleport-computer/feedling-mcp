---
document_lifecycle: decision
canonical_owner: self
---
# Metadata-first UserStore 与 Chat 热快照降载设计

**日期：** 2026-08-26  
**目标分支：** `test`  
**状态：** 已确认，待书面 review  

## 1. 背景与生产证据

生产 RDS 的 `NetworkTransmitThroughput` 已从旧基线约 `17.60 MB/s` 降至最近完整 24 小时平均约 `10.10 MB/s`，总降幅约 42.6%，但离“整台 RDS Network TX 降低 85%”对应的 `2.64 MB/s` 目标仍有明显距离。最近 6 小时平均约 `10.21 MB/s`，最近完整小时约 `9.63 MB/s`，说明当前版本仍存在稳定的数据库出站流量。

生产配置和数据库状态同时证明：RDS → TEE 双写与同步任务当前未运行，应用的 selected primary 仍是 RDS。因此剩余流量主要来自应用查询，而不是数据库间复制。

对生产 `pg_stat_user_tables` 做 30 秒只读差分采样后，主要 tuple 活动集中在四张表：

| 表 | 30 秒读取特征 | 平均行大小（采样） | 估算行字节占比 |
| --- | ---: | ---: | ---: |
| `chat_messages` | `idx_tup_fetch=1,346,205` | 约 1,711 B | 约 79% |
| `memory_moments` | `idx_tup_fetch=177,757` | 约 1,844 B | 约 11% |
| `user_logs` | `idx_tup_fetch=409,310` | 约 651 B | 约 9% |
| `v2_runtime_state` | `seq_tup_read=182,549` | 约 69 B | 小于 1% |

这些数字不是 PostgreSQL wire bytes 的精确分解，但足以确定优先级：`chat_messages` 是当前最值得先处理的查询放大源。

代码审计进一步显示，`UserStore(user_id)` 构造时会同步加载 tokens、push state、live activity、Chat hot snapshot、frames metadata 和 world books。大量仅需要用户身份、waiter、历史分页、poll、V2 tail 或单一状态的调用都会先构造完整 Store，从而连带读取最多 256 条 Chat 消息。已有 Chat change event、`LISTEN/NOTIFY` 和增量同步能降低后续刷新成本，但不能消除首次构造时的无条件热快照。

## 2. 目标与非目标

### 2.1 目标

本阶段把 `UserStore` 从“构造即加载全部数据”改为“先创建 metadata shell，用到哪个 section 才加载哪个 section”，重点消除非 Chat 路径触发的 Chat hot snapshot。

上线验收目标：

- Chat hot snapshot 查询次数相对同口径基线降低至少 90%；
- `chat_messages` tuple fetch 相对同口径基线降低至少 80%；
- 首阶段把整台生产 RDS Network TX 从约 `10.10 MB/s` 推进到 `4–6 MB/s` 区间；
- Chat、poll、history、V2 prompt tail、resident redelivery 的结果不缺失、不重复、不乱序；
- 关键接口 p95 延迟相对基线回退不超过 10%；
- 数据库错误率、缓存冲突率和跨 worker 收敛错误不得上升。

`4–6 MB/s` 是本阶段的合理目标，不承诺仅靠此项达到整机 `2.64 MB/s`。上线测量后再依据实际占比决定下一阶段是否处理 Memory、`user_logs` 或其他查询。

### 2.2 非目标

本阶段明确不做：

- RDS → TEE 双写、增量同步、全量导入或 selected-primary 切换；
- `memory_moments` 的增量缓存改造；
- `user_logs` 的 cursor/批量读取改造；
- `v2_runtime_state` 的调度或查询频率调整；
- Chat hot cache 上限从 256 再次调整；
- 数据库 schema 或历史数据迁移；
- 与查询降载无关的 Store 重构。

## 3. 方案选择

### 3.1 方案 A：继续维持 eager Store，仅在部分入口绕开（不采用）

为 history、poll、V2 tail 等高频入口单独增加 DB helper，避免这些入口调用 `get_store()`。

优点是改动小；缺点是漏网调用仍会加载完整 Store，新的调用点也容易重新引入放大。它不能建立可验证的全局不变量，只能逐个修补。

### 3.2 方案 B：metadata-first、按 section 懒加载（采用）

`get_store(user_id)` 默认只返回不执行 SQL 的 shell；调用方通过显式 section 需求加载数据。每个 section 有独立状态、锁、TTL 和刷新语义。

优点是从模型上消除隐式查询，能够对“某条路径允许多少查询”建立测试预算，并给后续 Memory 或其他 section 优化提供稳定边界。代价是调用点迁移较多，需要兼容模式和严格回归。

### 3.3 方案 C：拆分为多个独立 Store 类型（暂不采用）

把 Chat、Frame、World Book、Token 等拆成独立对象，由上层组合。

长期边界最清晰，但会一次性改变大量接口和对象生命周期，显著扩大回归面。当前目标是降低 RDS 查询，不需要同时完成彻底的领域拆分。方案 B 保留未来拆分空间。

## 4. 核心架构

### 4.1 Store shell

`UserStore(user_id)` 初始化只允许创建以下进程内元数据：

- 用户标识；
- section 状态表；
- section 级锁与 singleflight 协调对象；
- Chat waiter/通知协调对象；
- 生命周期和观测元数据。

构造过程不得执行 SQL。普通 `get_store(user_id)` 也只获取或创建 shell。

需要数据的调用方使用显式接口，例如：

```python
store = get_store(user_id, require={StoreSection.CHAT})
store = get_store(
    user_id,
    require={StoreSection.TOKENS, StoreSection.PUSH_STATE},
)
```

`require` 的返回语义是：指定 section 已按该调用路径的严格度完成首次加载或刷新；没有列出的 section 不能被连带加载。

### 4.2 Section 划分

首阶段定义六个独立 section：

- `CHAT`
- `FRAMES`
- `WORLD_BOOKS`
- `TOKENS`
- `PUSH_STATE`
- `LIVE_ACTIVITY`

section 只负责一类缓存数据及其加载/刷新状态。已有底层 DB helper 可继续复用；本阶段不要求把每个 section 物理拆成新文件或新 Store 类。

### 4.3 Section 状态机

每个 section 独立维护：

- `unloaded`：从未读取，内存中没有可声明为完整的数据；
- `loading`：一个调用正在完成首次加载或刷新，其他调用等待同一个结果；
- `fresh`：存在可用缓存，且未超过 TTL、未收到失效信号；
- `stale`：存在 last-good cache，但需要按调用语义刷新。

允许的核心转换为：

```text
unloaded -> loading -> fresh
fresh -> stale -> loading -> fresh
loading -> unloaded   # 首次加载失败
loading -> stale      # 刷新失败但有 last-good cache
```

首次加载采用 per-user、per-section singleflight。同一进程内 100 个并发调用首次需要 Chat 时，只允许一个 hot snapshot 查询；其他调用共享结果或错误。

### 4.4 兼容模式

新增部署配置：

```text
FEEDLING_STORE_LOAD_MODE=legacy|selective|lazy
```

- `legacy`：保持现有 eager 构造行为，用于版本级快速回滚和新版本基线验证；
- `selective`：已迁移调用点按显式 section 加载；暂未迁移的兼容入口仍可请求 legacy eager 行为；
- `lazy`：默认 `get_store()` 只返回 shell，所有数据访问必须显式声明 section。

`selective` 不是依靠运行时猜测调用者。兼容入口必须通过明确的 legacy wrapper 或参数标注，且清单固定、可搜索、可逐项清零。新代码不得新增兼容入口。

## 5. 数据流与一致性

### 5.1 Shell-only 路径

history 分页、Chat poll、V2 prompt tail 以及已有 bounded SQL helper 的调用路径，只获取 Store shell 或完全使用现有 DB helper。它们不得为了 waiter、用户锁或对象复用而触发 Chat hot snapshot。

这类路径的查询预算不要求“所有 SQL 为零”；要求是“Chat snapshot SQL 为零”，并保持原有 bounded 查询的 limit、排序和分页契约。

### 5.2 Chat 首次读取与刷新

首次真正需要内存 Chat 集合的路径调用 `require={CHAT}`：

1. 获取 `CHAT` section singleflight；
2. 读取 version-consistent、最多 256 行的 hot snapshot；
3. 沿用现有 generation fence，避免加载期间的本地已提交写入被旧快照覆盖；
4. 原子安装 rows、索引和版本；
5. 标记 `fresh` 并唤醒同一 singleflight 的等待者。

如果 generation 在加载期间变化，丢弃旧快照并沿用已有有界收敛逻辑重试，不允许把旧快照覆盖到较新的本地状态。

### 5.3 Chat 写入

数据库仍是权威来源。

- `CHAT` 已加载：保持现有 write-through 行为。数据库提交成功后更新本地缓存、generation、索引和 waiter。
- `CHAT` 未加载：写入数据库并唤醒 waiter，但不为了把一条新消息追加进缓存而读取 256 行 snapshot；section 仍保持 `unloaded` 或记录 dirty/version hint，不能仅凭这一条消息宣称 `fresh`。
- `CHAT` 正在加载：数据库提交和本地 generation fence 必须保证加载结果不会覆盖较新的本地提交；写入本身不等待无关 section。

### 5.4 清空历史

`clear_history` 先按现有事务语义清理数据库，再处理进程内状态：

- `CHAT` 已加载：清空 rows、索引并推进 generation/version 状态；
- `CHAT` 未加载：继续保持 `unloaded`，不得为“清空本地缓存”执行首次读取；
- 并发加载：generation fence 或显式失效令加载前快照不能复活已删除内容。

### 5.5 TTL、NOTIFY 与跨 worker

TTL 到期只把已加载 section 标记为 `stale`，不在后台或定时循环中主动重读。下一次真正需要该 section 的调用才刷新。

收到跨 worker 通知时：

- section 已加载：使用现有增量同步或标记 stale，按该 section 的一致性策略收敛；
- section 从未加载：只记录 dirty/version hint，不执行 SQL；
- waiter 唤醒与 section 是否加载相互独立，poll 不能因为 Chat 缓存未加载而失去通知能力。

wake bus 重连、丢通知补偿和 worker 回收不得再对整个 Store 执行无条件 full reload。它们只能按已加载 section 做 catch-up，或保留失效标记供下一次访问处理。

### 5.6 `reload()` 语义

现有无参数 `reload()` 改为只刷新该 Store 已经使用过的 section，不得把 `unloaded` section 变为 loaded。需要完整加载的管理或调试路径必须显式请求 section 集合，不能借 `reload()` 隐式恢复 eager 行为。

## 6. 错误处理

section 之间故障隔离。`CHAT` 加载失败不能使 `TOKENS`、`PUSH_STATE` 或其他 section 不可用。

- 首次严格加载失败：section 回到 `unloaded`，调用方收到结构化 503；不得返回伪造的空列表并标记成功。
- 已有 last-good cache 的刷新失败：保留原缓存并标记 `stale`；严格路径按现有合约报错，明确的 fail-soft 路径可继续使用 last-good cache。
- singleflight 失败：同一批等待者共享同一个结构化错误，下一次调用可以重新尝试；不得在失败后形成永久 poisoned future。
- DB 写入失败：不修改本地缓存、不推进 generation，也不发出成功 waiter 信号。
- 部分 section 加载成功、另一个失败：已经成功的 section 保持 `fresh`；`get_store(require={A, B})` 整体向调用方返回失败，但不回滚 A 的有效缓存。

所有错误日志和指标只记录固定 slug、section、load reason、结果与耗时，不记录用户消息、内容正文、token 或私密配置。

## 7. 调用点迁移

实施时先审计全部 `get_store()` 和直接 `UserStore(...)` 调用，并将其归入：

1. shell-only：只需要 user identity、锁、waiter 或使用 bounded SQL helper；
2. 单 section：只需要 Chat、Frames、Tokens 等某一类状态；
3. 多 section：业务确实同时需要多个 section；
4. 临时 legacy compatibility：尚未迁移但必须在 `selective` 阶段保持旧行为。

重点检查模块包括：

- `backend/core/store.py`
- `backend/core/wake_bus.py`
- Chat response/history/poll 路径
- Runtime V2 prompt tail 与 resident redelivery
- perception、voice、genesis
- agent supervisor/worker 生命周期

迁移原则：调用方声明最小数据需求；不得为了方便请求全部 section。

加入静态或测试型 CI guard：

- 禁止新增直接 `UserStore(...)` 构造；
- 禁止新增没有明确 shell-only 注释/封装或 `require` 声明的数据访问；
- compatibility 清单必须集中维护，能够在切换 `lazy` 前断言为空。

## 8. 观测与查询预算

新增 Store load 指标至少包含：

- `section`
- `reason`：first-use、ttl、notify、reconnect、manual 等固定枚举
- `cache_state`：cold/stale
- `row_count`
- `duration_ms`
- `outcome`

指标不能包含原始 user id；如需判断单用户热点，只使用已有隐私安全的聚合或不可逆 bucket。

为避免只看总流量无法归因，在 TEST 和 PROD 观察窗口记录：

- 每分钟 Chat hot snapshot 次数与返回行数；
- Store shell 创建次数；
- 各 section first-load/refresh 次数；
- `chat_messages` tuple fetch；
- RDS Network TX、ReadIOPS、CPU；
- Chat/history/poll/V2 关键接口吞吐、p50/p95 和错误率；
- generation 冲突、strict snapshot 失败与 wake bus catch-up 结果。

查询预算测试必须证明：

- shell-only 调用产生 0 次 Chat hot snapshot；
- 100 个并发首次 Chat 需求产生 1 次 snapshot；
- reload 未使用的 Store 产生 0 次 section load；
- unloaded section 收到 NOTIFY 产生 0 次 SQL；
- 单一 section 请求不会加载其他 section。

## 9. 测试策略

### 9.1 状态机单元测试

覆盖每个合法转换、首次失败、stale 刷新失败、retry、singleflight 清理和 section 故障隔离。

### 9.2 真实 PostgreSQL 并发测试

使用本地 PostgreSQL 验证：

- 并发首次 Chat load 只执行一次 version-consistent snapshot；
- load 与 append、delete、clear_history 并发时不缺失、不复活；
- DB 提交失败不污染缓存；
- generation fence 在本地并发写入下仍有界收敛。

数据库不可用时不得把这些用例静默 skip 后当作通过。

### 9.3 旧版/新版契约对比

对同一固定数据集分别运行 `legacy` 和 `selective/lazy`，比较：

- Chat history 分页边界和顺序；
- poll 返回与 waiter 唤醒；
- V2 prompt tail；
- resident redelivery；
- clear_history 后结果；
- Tokens、push state、live activity、frames 和 world books 的读取结果。

结果必须等价；允许的区别只有查询数量、缓存加载时机和由此产生的首次访问延迟分布。

### 9.4 多 worker 回归

至少四个 worker，覆盖：

- 本 worker 写、其他 worker poll/read；
- 丢失一次 NOTIFY 后通过 durable state/catch-up 收敛；
- wake bus 重连；
- worker recycle 后冷启动；
- 一个 worker 首次加载失败，其他 worker 正常；
- unloaded worker 收到高频通知仍不产生 snapshot 风暴。

### 9.5 全量验证

除目标测试外，执行完整 DB-backed pytest。由于这是系统架构和部署行为变化，同一实现 PR 还必须审阅并同步：

- `docs-site/content/docs/` 下相关 architecture、workflow 和 self-hosting/trust 内容；
- `docs-site/content/docs/changelog.mdx` 的 `Unreleased`；
- 若公开 API 契约未变化，不需要改 OpenAPI；若实施中发现契约变化，则必须同步 source/override、重新生成 public OpenAPI 并运行 contract tests；
- `docs-site` 的 `npm run types:check`、`npm run lint` 和 `npm run build`。

## 10. 发布、观测与回滚

### 10.1 分阶段发布

1. 将同一个候选版本部署到 TEST，先使用 `legacy`，确认代码版本本身与旧行为等价。
2. TEST 切换 `selective`，执行完整功能回归、DB-backed 测试、多 worker 演练和查询预算验证。
3. 确认 compatibility 清单清零后，在 TEST 切换 `lazy` 并再次回归。
4. 生产先部署同一个版本但保持 `legacy`，建立同版本的短窗口基线。
5. 生产切换 `selective`，观察 1 小时、6 小时、24 小时；达到正确性与延迟门槛后再切 `lazy`。
6. `lazy` 继续观察同样窗口，并与发布前同星期、同小时流量比较。

配置切换应通过现有受控部署流程完成，不在容器内手改临时环境变量。

### 10.2 继续与停止条件

继续扩大流量的条件：

- Chat/history/poll/V2 核心功能回归通过；
- 没有缺失、重复、乱序或 clear 后复活；
- 5xx、DB error、strict snapshot failure 不上升；
- p95 回退不超过 10%；
- Chat snapshot 与 tuple fetch 朝验收目标下降。

出现任一数据正确性问题时立即回到 `legacy`，不等待观察窗口结束。只有流量降幅不足但正确性稳定时，保留新模式收集归因数据，再决定后续优化，不通过扩大本阶段范围补救。

### 10.3 回滚

首选回滚是把 `FEEDLING_STORE_LOAD_MODE` 改回 `legacy` 并按现有发布流程重启 worker。必要时再回滚应用版本。

本阶段没有 schema 和数据迁移，因此回滚不需要数据修复。切换回 legacy 后，所有 Store 恢复 eager 构造；已有数据库内容、Chat version/event 和 waiter 语义不变。

## 11. 实施完成定义

本阶段只有同时满足以下条件才算完成：

- 所有 Store 调用点完成分类，`lazy` 模式不存在未声明的数据依赖；
- 单元、真实 PostgreSQL、四 worker、完整 DB-backed 和文档构建验证通过；
- TEST 的 `legacy -> selective -> lazy` 回归有可复查证据；
- 生产完成至少 24 小时同口径观测；
- Chat snapshot 至少下降 90%，`chat_messages` tuple fetch 至少下降 80%；
- RDS Network TX 达到或接近 `4–6 MB/s`，并记录与 `2.64 MB/s` 最终目标的剩余差距；
- 没有正确性、错误率或关键接口 p95 超门槛回退；
- 架构文档、变更日志和运维回滚说明与实现同步。

如果本阶段达到预期但整机仍高于 `2.64 MB/s`，下一轮只依据上线后的查询归因数据选一个新目标，优先比较 `memory_moments` 与 `user_logs`，重新走独立设计与实施流程。
