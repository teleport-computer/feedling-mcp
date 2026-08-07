# Spec: 30 分钟兜底收割对可续跑的 genesis 导入应重排,不应终局失败

- 日期:2026-08-07
- 发起:Seven(usr_3b73f1cb0a9ec975 用户反馈复盘)
- 实现:codex3(backend)
- Gatekeep:claude3
- 分支:test

## 背景 / 事故

usr_3b73f1cb0a9ec975,6.8MB 聊天记录导入(47,485 条 / 67 窗 / tier=large),蒸馏模型
`anthropic/claude-haiku-4.5`(openclaw 中转,慢)。2026-08-06 09:53 UTC 建 job,
map 阶段 67/67 窗全部完成并落 checkpoint(138 张卡已写入),10:42 UTC 起模型调用
断流(serve-worker 非部署性死亡或单调用挂死,当天 prod 部署在 03:19/14:41 UTC,
均不在窗口内),11:12 UTC 被 30 分钟兜底收割器判 `genesis_stale_timeout:1800s`
**终局失败**。用户被迫从头重导(再一小时、再死一次),BYOK 下 ~140 次模型调用的
成果作废——而这些成果全躺在服务端 checkpoint 里。

慢蒸馏模型(中转 claude)× 长暴露窗口 × 中断即死刑 = 用户"每次导入都导到地老
天荒";换 DeepSeek flash"成功"只是因为跑得快、赶在中断之前完成。

## 现状(两条恢复路径,行为不一致)

1. **快速回收** `db.genesis_reclaim_orphaned_processing_jobs`(worker 死亡检测,
   120s):已区分可续跑——`received_chunks > 0` → 重置 `uploaded` 重排续跑;
   `= 0` → failed。**该案没触发**:单例 serve-worker 全死时 `live_worker_ids`
   为空 fail-closed;worker 活着但调用挂死时 claim 归属仍"活",也不触发。
2. **30 分钟兜底** `db.genesis_reap_stale_processing_jobs`:**无条件**
   `status='failed'`,不看 `received_chunks`,不看 checkpoint。← 本 spec 修的就是它。

checkpoint 续跑机制本身已存在且可用:`_PlaintextCheckpointProgress` 持久化
`map_outputs`,`build_reducer_output_from_texts(resume_map_outputs=…)` 跳过已完成
窗。plaintext 材料也**是**加密分块存服务端的(service.py:408 起强制
"Genesis plaintext must arrive as encrypted chunks";该 job `total_chunks=67`,
`total_bytes≈2MB`)。

## 改动

### 1. `db.genesis_reap_stale_processing_jobs` 增加可续跑分支

镜像快速回收的判别,同一条原子 UPDATE(保留现有 `FOR UPDATE SKIP LOCKED` +
条件内嵌的 TOCTOU 语义,`resident_consumer_id=''` 过滤不变):

- **可续跑** = `received_chunks > 0` 且
  `COALESCE((metadata->>'stale_requeues')::int, 0) < MAX`:
  → `status='uploaded'`,`error=''`,清 `worker_claimed_by`/`worker_claimed_at`,
  `metadata = jsonb_set(metadata, '{stale_requeues}', 计数+1)`,`updated_at=now()`。
- **不可续跑 / 预算耗尽**:维持现行为 `status='failed'`,error 保留
  `genesis_stale_timeout:{sec}s` 形状;预算耗尽可加后缀
  (如 `:requeues_exhausted:{n}`)但 **`stale_timeout` 子串必须保留** ——
  `service.classify_genesis_error` 靠它映射 `worker_restarted` 及用户文案(T16)。
- `MAX` 默认 **2**,env `FEEDLING_GENESIS_STALE_REQUEUE_MAX`。计数走 metadata
  JSONB,**不加列、不开迁移**。
- 返回行带 `_reap_action`(`"requeued" | "failed"`),对齐 reclaim 的
  `_reclaim_action` 惯例。

### 2. `worker.reap_stale_processing_jobs` 调用侧同步

镜像 `reclaim_orphaned_processing_jobs` 的处理:requeued → blob 状态
`"uploaded"`(App 恢复显示"导入中");failed → 现行为。trace 事件
`genesis.worker.stale_reaped` 增加 `action` 与 `stale_requeues` 字段,
requeued 用 `status="ok"`、failed 保持 `status="error"`(对齐 orphan_reclaimed)。

### 3. 过期注释订正

`db.genesis_reclaim_orphaned_processing_jobs` docstring 写着 "plaintext
onboarding, which is never persisted" —— 与 service 层现实(plaintext 也走加密
分块上传)打架。订正为以 `received_chunks` 为准的表述,并注明 plaintext 分块
同样落库。

## 不做(Non-goals)

- 不动 resident lane 的收割(`reap_stale_resident_jobs` 自有 attempts 机制)。
- 不动 1800s 阈值本身(更早探测/更快反馈另立项)。
- 不新增列、不开 alembic 迁移。
- 不持久化 voice_map 产出(resume 时 voice map 会重跑,fact map 不会——已知
  代价,可接受;要省这一半另立项)。
- 不改 iOS/App。

## 测试(真 PG,对照 TESTING.md §2-A/G/M2)

1. stale processing + `received_chunks>0` + 无计数 → `uploaded`,
   `stale_requeues=1`,claim 归属清空,error 空,blob=uploaded,
   trace action=requeued/status=ok。
2. 计数已达 MAX → `failed`,error 含 `stale_timeout` 子串,
   `classify_genesis_error` 仍产 `worker_restarted`(锁用户文案不回归)。
3. `received_chunks=0` → `failed`(现行为不变)。
4. `resident_consumer_id` 非空的行不被选中(现行为不变)。
5. TOCTOU:cutoff 内被 heartbeat 过的行不被选中(现测试若有则保留/若无补上)。
6. 续跑正确性(L1,fake llm 计数):requeued job 被重新 claim 后,已 checkpoint
   的 fact map 窗**零次**模型调用、直接进 reduce;卡不重写(checkpoint 跳过
   已完成窗,断言写卡数不翻倍)。
7. metadata 其他键在 jsonb_set 后不被抹(并发/覆盖自查,§2-M2 精神)。

## 验收 / Gate

- L1 全绿 + 上述真 PG 用例;我 gatekeep 后合 test。
- test 环境 L3:用 `tools/e2e/processing_probe.py` 形状造一个大导入,mid-run
  kill serve-worker,断言 30 分钟收割后 job 回 `uploaded` 并自动续跑完成
  (identity/persona 最终写入),App 侧 genesis_state 状态曲线
  uploaded→processing→done。
- 上线后回访 usr_3b73f1cb0a9ec975 场景:同形状导入在人工中断下能自愈。

## 升级规则(常设)

卡壳 >10 分钟或发现 spec 与代码现实冲突:回报,不猜、不绕、不造工具伪造结果。
