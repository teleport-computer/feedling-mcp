# Runtime V2 三池 test 环境验证记录（2026-08-14）

## 决策状态

- 当前结果：`pending`（部署与基础运行态通过；受控负载、峰值窗口和 previous-image
  恢复演练尚未闭环，不能标记为 `pass`）
- 目标环境：`test`
- CVM：`feedling-io-test`（profile `amiller-users-projects`）
- 已部署提交：`925378905fe2af464298adfed32e3cecb077b2f9`
- 镜像：`ghcr.io/teleport-computer/feedling:9253789`
- 实施 PR：[PR #207](https://github.com/teleport-computer/feedling-mcp/pull/207)
- test CI：[run 31775875835](https://github.com/teleport-computer/feedling-mcp/actions/runs/31775875835)
  全部成功，包括 test 主 CVM、test runner、远程证明和部署后 canary；pre/prod deploy
  jobs 均跳过。
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

部署时间：2026-08-14 14:23--14:28 CST；基础运行态复核至约 14:53 CST。

public `/healthz` 返回 HTTP 200，release commit 为
`925378905fe2af464298adfed32e3cecb077b2f9`。有效环境只包含以下五个三池变量；
`FEEDLING_V2_POOL_MODE` 和 `FEEDLING_V2_MAX_WORKERS` 均不存在。

### 拓扑与资源

- [x] migration `0086_v2_worker_pool_heartbeats` 已应用；未执行数据库降级。
- [x] 观察到 parent、resource tracker 和 4 个不同 child；启动 owner 分别为
  `foreground-0`、`foreground-1`、`wake-0`、`heavy-0`，pool 数量为 `2/1/1`。
- [x] Foreground/Wake/Heavy heartbeat capacity 为 `2/1/1`，configured/healthy
  分别为 `2/2`、`1/1`、`1/1`，restarting 均为 0。
- [ ] Enclave broker 配置 limit=4，当前 wait P95 最高 1.289 ms、无 active/waiting；
  尚未执行 5 caller live contention，因此“第 5 个等待”只有自动化测试证据。
- [ ] Profile 配置并发 1；部署观察窗内没有 Profile batch，尚无 live contention 样本。
- [x] heartbeat 报告 Parent DB pool max 8、4 个 child 各 max 2、waiting=0、
  timeouts=0；采样时 RDS 为 46 idle + 1 active，仍有连接余量。
- [x] serve-worker 容器 RestartCount=0、OOMKilled=false。部署后约 6 分钟 available
  191 MiB，约 25 分钟 available 186 MiB，均高于 128 MiB 门槛。
- [x] 资源采样中 serve-worker 4.06% CPU、backend 12.77% CPU，其余主要容器均低于
  1%；总量低于 85% of 1 core（排除启动峰值）。

serve-worker 部署后采样为 441.5 MiB / 67 PIDs；backend 为 321.6 MiB / 46 PIDs。
相较旧共享 child，新增的单-slot 子进程没有降低 host available memory。

### 故障注入与用户链路

| 场景 | 时间/Job 或 probe ID | 结果与证据 |
| --- | --- | --- |
| 长 Heavy Job + 同用户 Chat | pending | 部署观察窗内无合适的合成账号/受控 Job，不直接写 RDS 注入 |
| 单 Heavy slot watchdog | pending | 尚未受控触发 watchdog；观察到 wake Job 5067 完成后，exact-claim reconcile 只 SIGKILL/拉起 wake-0，foreground/heavy PID 与容器均未重启。这是单槽 fencing 旁证，不冒充 watchdog 注入 |
| 5 个并发 Enclave caller | CI 31775875835 | 自动化并发/故障测试通过；live heartbeat limit=4，但无 5 caller live 样本 |
| Profile concurrency | CI 31775875835 | 自动化并发测试通过；live 配置为 1，但观察窗内 batch_count=0 |
| Chat smoke | pending | 部署 canary round-trip 通过，但它不是 Chat；部署后窗口 Chat 样本数为 0 |

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
| previous image restored | blocked-safe | 尝试重跑旧 test deploy job（run 31768281532 attempt 2）；`pin-runtime-release.sh` 发现 test 已从 `afc7e4a` 前进到新提交，在调用 `phala deploy` 前拒绝，CVM 未变更 |
| new image redeployed | pending | pending |
| repeated smoke/isolation | pending | pending |

不能把该结果记为恢复成功。仓库的“较新 workflow 拥有部署权”保护阻止旧 workflow 覆盖
test，这是正确的并发安全行为。后续恢复演练应增加一个受审计的 test-only workflow 输入，
允许选择 previous known-good image，同时复用当前 test secrets、目标检查、远程证明和新版本恢复
步骤；不应通过本地拼接不完整 secrets 或绕过 branch-head guard 完成。

## 最终判定

本次可以判定“test 配置与基础运行态上线成功”，但完整结果继续保持 `pending`：Chat/Heavy、
live Enclave/Profile contention、峰值窗口和 previous-image 恢复尚缺证据。只有 pool heartbeat、
取消定向、exact recovery、Enclave/DB 上限、延迟阈值、资源阈值和 previous-image 恢复全部
具备证据时，才把结果改为 `pass`。任一关键项失败则标记 `fail`，恢复 previous known-good
image，并记录 follow-up defect。pre/prod 推广不在本记录范围内。
