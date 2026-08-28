---
document_lifecycle: current
canonical_owner: self
---
# 候选：删除 Runtime V2 watchdog 旧测试兼容层

结论：`delete`，排在简单叶子之后；这里的核心是收紧接口，同时保持恢复安全顺序。

## 范围与证据

- production `serve_worker._watchdog_loop` 总是传真实 `ChildSupervisor`、
  `turn_stall_timeout_sec` 和 `turn_absolute_timeout_sec`。
- 当前恢复路径固定为
  `snapshot -> capacity zero -> kill_for_recovery -> exact owner recovery -> start`。
- `ChildSupervisor.kill_and_respawn()` 和 Python kwarg `turn_hard_timeout_sec` 没有生产
  调用者，仅旧测试/test-double 使用。
- `watchdog._watchdog_loop` 仍为缺少新接口的 test-double 保留
  `supervisor.kill`/`kill_and_respawn` fallback。
- `ChildSupervisor.kill()` 仍被 `pool_supervisor.py` 使用，必须保留。

预计 production gross 删除 35–50 行兼容分支；测试改写会同时删除旧 fixture 并增加
当前契约覆盖，最终 net 待实施 diff 复算。

## 兼容与安全门禁

- 保留 capacity-zero 先于 kill；只有确认终止后才能按 exact
  `job_id + claimed_by` recovery，再启动 replacement。
- termination 未确认时不得 recovery/respawn。
- 不删除部署 alias `FEEDLING_V2_TURN_HARD_TIMEOUT_SEC`；runtime 仍读取它，外部环境
  可能仍设置。
- 先把旧 test-double 改成真实 `snapshot/kill_for_recovery/start` 契约，再删除 fallback、
  旧 Python kwarg 和 wrapper。
- 运行 `test_v2_watchdog.py`、`test_v2_p0_pool_safety.py`、
  `test_v2_child_supervisor.py`、`test_v2_pool_supervisor.py`、
  `test_v2_pool_fault_injection.py` 和 `test_v2_serve_worker.py`。
- test 环境验证 per-slot capacity 归零、owner-fenced recovery 和 replacement；不能只看
  compose/进程存活。

回滚方式：回退提交并重新部署 Runtime V2；不涉及 schema 回滚。
