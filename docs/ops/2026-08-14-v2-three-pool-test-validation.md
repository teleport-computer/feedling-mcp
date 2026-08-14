# Runtime V2 三池 test 环境验证记录（2026-08-14）

## 决策状态

- 当前结果：`pending`
- 目标环境：`test`
- CVM：`feedling-io-test`（profile `amiller-users-projects`）
- 待部署提交：pending（feature 分支验证完成后填写）
- previous known-good commit/image：
  `afc7e4a915eb0fd34b9031203191e76b3b22c7fd` /
  `ghcr.io/teleport-computer/feedling:afc7e4a`
- 隐私约束：本文只记录 content-free 运维指标，不记录凭证、用户 ID、消息正文、
  Memory 内容或 provider payload。

## 部署前基线

采集时间：2026-08-14 12:58 CST 左右。

### CVM 与容器

| 项目 | 部署前观测 |
| --- | --- |
| CVM 规格 | 1 vCPU / 2 GB / 20 GB |
| Host memory | 1853 MiB total，1591 MiB used，190 MiB available |
| Persistent disk | 18.9 GiB total，1.7 GiB used（9%） |
| serve-worker | 5.37% CPU，256.3 MiB，30 PIDs |
| backend | 16.97% CPU，364.3 MiB，53 PIDs |
| enclave | 0.44% CPU，82.58 MiB |
| enclave-domain | 0.43% CPU，80.01 MiB |
| public health | HTTP 200，release commit `afc7e4a915...` |

旧 serve-worker 进程树只有 parent、multiprocessing resource tracker 和一个共享
`spawn_main` child；child RSS 约 126 MiB。这证明部署前仍是旧的共享 child 拓扑。

test/pre 机器规格保持不变。为控制 1C2G test 主机的资源风险，本次 test 部署使用
`2/1/1 = 4` 个单-slot child；生产 8C16G 的目标容量仍为 `4/2/2 = 8`，但 prod 配置和部署
不在本次范围内。部署时持续观察可用内存、OOM 和容器重启，触发恢复阈值即回退旧 image。

### 数据库与运行时指标

以下为 2026-08-14 部署前同一 test RDS 的 content-free 只读基线：

| 指标 | 基线 | 新版本允许范围 |
| --- | --- | --- |
| migration revision | `0085_v2_wake_shadow_decisions` | 部署后必须为 `0086_v2_worker_pool_heartbeats` |
| Chat claim P95 | 4.557 秒（9 个样本） | 不高于 2 秒；样本不足时延长窗口，不以小样本单独判退化 |
| Chat service P95 | 35.46 秒（9 个完成样本） | 只作对照，不设置绝对回复时延承诺 |
| Enclave P95 | 暂无可比聚合 | 部署后记录绝对值；不得出现新增 timeout 风暴 |
| provider 429/timeout rate | 0（24h） | 不得高于基线 + 1 个百分点 |
| DB connection wait/timeout rate | timeout 0；未观察到 pool wait 告警 | timeout 必须为 0；不得出现持续 wait |
| PostgreSQL activity | 32 idle、2 idle in transaction、2 active ClientWrite、1 active | 低于实例上限，并为约 16 个 Runtime V2 pooled connections 留有余量 |
| queue depth/service time by lane | 队列为空；Chat service P95 35.46 秒 | Heavy/Wake 积压不得导致 Chat 过期 |
| preemption/watchdog count | 0（24h） | 仅允许受控注入或已知故障产生的事件 |

百分比阈值以部署前同一统计窗口的 content-free 聚合为分母；样本不足时不据此判 `pass`，
改用更长观察窗口。

## 预期有效配置

```text
FEEDLING_V2_FOREGROUND_SLOTS=2
FEEDLING_V2_WAKE_SLOTS=1
FEEDLING_V2_HEAVY_SLOTS=1
FEEDLING_V2_PROFILE_INSTANCE_CONCURRENCY=1
FEEDLING_V2_ENCLAVE_INSTANCE_CONCURRENCY=4
```

部署后的 environment、进程参数和启动日志不得出现 `FEEDLING_V2_POOL_MODE`、
`FEEDLING_V2_MAX_WORKERS`、legacy supervisor 或 disabled-isolation 路径。

## 部署后验收记录

### 拓扑与资源

- [ ] migration `0086` 已应用；不执行数据库降级。
- [ ] 观察到 4 个不同 child PID/generation，pool 数量为 `2/1/1`。
- [ ] Foreground/Wake/Heavy heartbeat capacity 为 `2/1/1`。
- [ ] Enclave active grants 始终不超过 4；第 5 个 caller 等待。
- [ ] Profile 实例并发始终不超过 1。
- [ ] Parent DB pool max 8；每个 child max 2；约 16 个 pooled connections 在安全包络内。
- [ ] test 主机无 OOM、无非预期容器重启，稳态 available memory ≥ 128 MiB。
- [ ] test 主机稳态总 CPU < 85% of 1 core（排除短时启动峰值）。

### 故障注入与用户链路

| 场景 | 时间/Job 或 probe ID | 结果与证据 |
| --- | --- | --- |
| 长 Heavy Job + 同用户 Chat | pending | pending |
| 单 Heavy slot watchdog | pending | pending |
| 5 个并发 Enclave caller | pending | pending |
| Profile concurrency | pending | pending |
| Chat smoke | pending | pending |

必须证明：Chat claim P95 ≤ 2 秒；watchdog kill 到 exact claim release P95 ≤ 5 秒；
单 slot watchdog 只改变一个 PID；没有重复 terminal Chat reply。

## Previous-image 恢复演练

1. 记录并重部署 previous known-good `afc7e4a915...`。
2. 确认 public health、旧 image 默认 4-worker 行为和 migration `0086` 向后兼容。
3. 不降级数据库。
4. 重新部署新 image，确认 4 个 child、`2/1/1` heartbeat、Chat smoke 与 Heavy
   隔离场景再次通过。

| 步骤 | 时间 | 证据 |
| --- | --- | --- |
| previous image restored | pending | pending |
| new image redeployed | pending | pending |
| repeated smoke/isolation | pending | pending |

## 最终判定

只有 pool heartbeat、取消定向、exact recovery、Enclave/DB 上限、延迟阈值、资源阈值和
previous-image 恢复全部具备证据时，才把结果改为 `pass`。任一关键项失败则标记 `fail`，恢复
previous known-good image，并记录 follow-up defect。pre/prod 推广不在本记录范围内。
