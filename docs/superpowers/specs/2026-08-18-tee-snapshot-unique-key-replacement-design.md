# TEE SNAPSHOT 业务唯一键漂移修复设计

## 背景与根因

TEST 部署 FK-safe staged merge 后，SNAPSHOT lane 不再因外部 FK 或父子顺序失败，
但 `model_api_routes`、`v2_usage_daily_dimensions` 和
`v2_usage_daily_users` 仍会在刷新时触发唯一约束冲突。线上只读核对确认：RDS 与
TEE 对每个冲突业务键各有一行，但两侧主键不同。

当前目标事务的顺序是：临时表 COPY → 按主键 UPSERT → 删除源端已不存在的目标行。
当目标端旧行与源端新行共享业务唯一键但主键不同，UPSERT 会在旧行删除之前插入新
主键，因此先撞上业务唯一约束，整个事务回滚。

## 目标与非目标

目标是让所有 SNAPSHOT 表在“业务唯一键相同、主键不同”时仍能精确收敛到 RDS，
同时保留 staged merge 的事务原子性、列漂移策略和普通 FK 语义。

本次不改变表注册、schema、业务唯一约束、同步频率、Phase 4 cutover 逻辑或
TEE-primary 配置；也不在代码里枚举三张已知失败表的业务键。

## 方案

保留现有临时表和主键集合，将单表目标事务调整为：

1. 从 RDS COPY 公共列到目标端临时表。
2. 按目标表主键，删除目标端存在但临时表不存在的旧行。
3. 用单条 set-based `UPDATE ... FROM stage` 更新主键相同的保留行。
4. 将临时表 UPSERT 到目标表，插入新行并覆盖并发出现的同主键行。
5. 事务提交；任一步失败则全部回滚。

删除步骤只处理源快照中已不存在的主键。主键相同的保留父行不会被删除，因此外部
子表不会因普通更新而丢失；真正 stale 的父行仍按数据库定义的 FK 行为删除或级联。
先删除 stale 行会释放其业务唯一键，使随后插入的新主键成功。该顺序对业务唯一键
完全通用，不需要发现或硬编码每张表的额外唯一索引。

## TEST 部署后的补充发现

首次 prune-before-upsert 部署后，两个 usage 表收敛，但 `model_api_routes` 仍有
一张表失败。只读核对显示：TEE 唯一存量行的主键也存在于 RDS，因此不能 prune；
它在 TEE 仍为 active，但 RDS 已将它改为 inactive，并新增另一主键的 active 行。
单条 UPSERT 的输入行没有顺序保证，可能先插入新 active 行，再更新旧 active 行，
从而撞 partial unique index `model_api_routes_one_active`。

因此新增步骤 3：先批量更新 retained rows，使当前线上旧 active owner 释放业务唯一
键，再允许步骤 4 插入尚不存在于 TEE 的新 active 主键。该操作仍按公共列和主键生成
SQL，不感知 `is_active`、表名或任何具体唯一索引。

该顺序不承诺解决所有 retained-to-retained 唯一键交换：如果新旧 owner 两个主键都已
存在于目标端，PostgreSQL 的 non-deferrable unique index 仍可能在 set-based UPDATE
内部报冲突。此形状不属于当前 TEST 现场；若出现会继续作为 `snapshot_failures` 红灯
保留旧快照并要求单独设计，而不会静默产生错误数据。

## 错误处理与安全边界

- 临时表 COPY、stale prune、retained UPDATE 与 UPSERT 继续位于同一显式事务中。
- COPY、DELETE、UPDATE、UPSERT 或 FK 处理任一步失败，TEE 保留事务开始前的完整快照。
- 没有主键、主键不在公共列、无公共列和行数超限的现有护栏保持不变。
- TEE-only 列在保留行上继续保留，在新行上继续使用目标默认值。
- 单表失败继续只记录报告，不上抛污染主请求路径。

## 测试与验收

先新增一个最小回归测试：源端与目标端各有一行，主键不同但第二个唯一列相同；调用
`snapshot_table` 后应成功，目标端只保留源端主键和内容。该测试在旧实现上必须因
唯一约束失败而 RED。

再新增 partial-unique 回归：目标端保留一条 active 行；源端同主键行已 inactive，
并且在物理 COPY 顺序中先出现另一条新主键 active 行。prune-before-upsert 版本必须
RED；retained pre-update 版本必须得到与源端完全相同的两行状态。

修复后必须验证：

- 新回归测试转 GREEN；
- 现有外部子表保留、stale 父行级联、失败回滚、幂等和列漂移测试继续通过；
- 完整 `tests/test_tee_snapshot.py` 通过，再运行相关 TEE 测试和全量后端测试；
- 部署到 TEST 后，最新同步记录 `snapshot_failures=0`；
- 严格 verify 收敛后重新运行 Phase 4 dry-run。dry-run 只报告 drain blocker，绝不
  自动执行 apply 或切换数据库。

## 发布与回滚

修复从普通 `fix/*` 分支合入 `test`，由现有 GitHub Actions 部署主 CVM 与 runner。
代码回滚只需回退该提交；由于同步事务输出仍是 RDS 的精确快照，本修复不引入新的
持久化格式或不可逆 schema 变化。TEE-primary apply 和部署变量切换继续需要独立、
明确的操作批准。
