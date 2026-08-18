# Task 0.2 取证与细案：tee-sync 的 reconcile/verify 到底怎么了

> 主计划：`docs/superpowers/plans/2026-07-23-tee-promotion-decrypt-removal.md`
> Phase 0 Task 0.2「修 reconcile_ok 慢性 false（表同步前必完成）」。
> 取证时间 2026-07-28 12:0x UTC，prod。**结论推翻了计划里的病症描述与两个疑点方向。**

## 一、计划的假设 vs 实测

| 计划里写的 | 实测（2026-07-28 prod） |
|---|---|
| 「prod 单趟 tick 已 **11 分钟**」 | 近 24h 均值 **306s**，最长 1232s；当前静默期只有 **2–3 分钟** |
| 「`verify_ran=f`」 | ✅ 属实，且更糟：**24h 内 `verify_ran=t` 为 0 次** |
| 「`requeue_backlog` 增长 717→776→3028」 | 当前恒为 **NULL**（verify 从没跑成，该字段根本没被写过） |
| 「`reconcile_ok` 慢性 **false**」 | 24h：`false` 49 次、**`NULL` 89 次**、`true` **1 次** |
| 疑点方向 A：单表独占窗口饥饿 | ❌ 不是 |
| 疑点方向 B：reconcile 在 tick 预算内跑不完只标 false | ❌ 不是（成功那趟 reconcile 拷了 85 万行，没有预算截断） |

近 24h 汇总（`tee_sync_runs`，139 趟 tick）：

```
ticks_24h | did_recon_t | recon_ok_t | recon_ok_null | recon_ok_false | verify_ran_t | avg_dur_s | max_dur_s
      139 |          50 |          1 |            89 |             49 |            0 |     306.3 |    1232.2
```

## 二、两条互相独立的根因

### 根因 1：reconcile 撞 TEE 侧缺表 —— **已自愈，无需修复**

prod backend 日志原文：

```
[tee-sync] reconcile 失败: relation "notify_relay_configs" does not exist
```

`reconcile` 遍历白名单表逐张拷贝，撞到 TEE 库里不存在的表就整趟抛异常
（`tee_sync_scheduler.py:148` 的 `except Exception` → `reconcile_ok` 保持 False）。
这正是计划里记的「alembic_tee 无 CI 钩子，0002/0003 合了从未在实库执行」的下游症状。

**但表已经补上了**——TEE prod 库现在 **54 张表，`notify_relay_configs` 存在**。
证据是 `2026-07-28 02:51:12 UTC` 那趟（`id=1794`）：

```
did_reconcile=t  reconcile_ok=t  reconcile_copied=850445  reconcile_pruned=13
```

85 万行真实拷贝，不是 `AlreadyRunning` 的假成功（那条路径 `reconcile_copied` 会留在 0）。

**因此 Task 0.2 标注的「⚠️ 表同步前必完成」的因果方向是反的**：不是 0.2 挡着表同步，
而是**表同步（Task 0.6 / tee-full-table-alignment 工作流）修好了 0.2 的一半**。
计划里的这条排序约束应当撤销。

### 根因 2：verify 被单条坏信封 fail-closed 卡死 —— **未修，是唯一剩余真问题**

prod backend 日志原文：

```
[tee-sync] verify 失败: enclave_http_403:{"error":"decrypt_failed: envelope missing body_ct"}
```

调用链：

1. `tee_sync_scheduler.py:193` — `if do_reconcile and reconcile_ok:` 条件**是满足的**
   （02:51 那趟 `reconcile_ok=t`），verify 确实被调用了。
2. `tee_shadow/verify.py:306` — `expected = transform(doc, decrypt)` 逐行抽样解密，
   `try` 块**只 catch `transforms.PendingDeviceMigration`**（307 行）。
3. 一条 envelope 缺 `body_ct` → enclave 返回 403 → 这个异常**不在 catch 列表里**
   → 冒泡出 `_sample_ciphertext_content` → 冲垮整趟 verify。
4. `tee_sync_scheduler.py:211` — `except Exception: log.warning("verify 失败")`
   **静默吞掉**，`summary["verify_ran"]` 保持 `_blank_summary` 给的 `False`。

于是：**一条坏行 = 整个 verify 永久瘫痪**，`verify_ran` / `unconverged_tables` /
`requeue_backlog` 全线失去量测能力，而外部只看到一条 warning 日志。

这与 replicate 侧 2026-07-15 已修的毒行问题是**同一个模式**
（memory `tee-replicate-poison-row-headofline-quarantine`）：replicate 当时加了
quarantine-and-advance，**verify 这条路径没有跟着加**。

## 三、修复方案

**核心**：verify 的逐行抽样解密改为「单行失败不致命」，照抄 replicate 已验证的
quarantine 语义。

1. `verify.py:305-311` 的 `except` 扩成两级：
   - `PendingDeviceMigration` → 维持现状（跳过，不算 mismatch）。
   - **其它任何解密异常** → 记一条 `{"table":…, "user_id":…, "item_id":…,
     "field": "<decrypt-failed>", "error": …}` 进 mismatches 并 `continue`，
     **不中断整趟 verify**。
2. summary 增设 `verify_decrypt_failures` 计数（沿用 `replicate_table_failures`
   的既有风格），让坏行数量成为可观测指标而不是一条 warning。
3. `tee_sync_scheduler.py:211` 的 `except` 保留（兜底），但因为 verify 内部已不再
   因单行崩溃，它应当极少触发；触发即代表真正的系统级故障。

**边界（必须守住）**：
- 解密失败**不得**被当成「两库一致」——必须计入 mismatch，否则 verify 会用
  「跳过坏行」换来虚假的全绿，比现在崩掉更危险。
- 不在 verify 里做任何写操作（隔离/删除）；verify 是只读量测，隔离是 replicate
  侧 quarantine 的职责。

## 四、⚠️ 验收标准本身不可执行，需改

计划写的验收是：**「连续 3 个 tick `reconcile_ok=t` 且 `unconverged_tables` 为空」**。

但 `FEEDLING_TEE_RECONCILE_INTERVAL_SEC` 默认 **86400s（24 小时）**
（`tee_sync_scheduler.py:99`），reconcile 成功后 24 小时内不再触发，
`reconcile_ok` 会是 `NULL` 而非 `t`。**「连续 3 个 tick reconcile_ok=t」需要等 3 天**，
且只有在 reconcile 反复失败重试时才可能连续出现——自相矛盾。

建议改成：

- **验收 A**：临时把 `FEEDLING_TEE_RECONCILE_INTERVAL_SEC` 调小（如 900s）跑 3 趟，
  确认 `reconcile_ok=t` 连续 3 次，之后恢复默认值；**或**
- **验收 B**（推荐，不动线上配置）：改判「连续 3 次**做过** reconcile 的 tick
  （`did_reconcile=t`）全部 `reconcile_ok=t`」，并**新增** verify 侧验收：
  「连续 3 趟 `verify_ran=t` 且 `verify_decrypt_failures=0`、`unconverged_tables=0`」。

verify 侧的验收才是这个 Task 真正要保证的东西——reconcile 那半已经自愈了。

## 五、待办

- [ ] 按 §3 改 `verify.py` 的异常分级 + 加 `verify_decrypt_failures` 指标（含单测：
      造一条缺 `body_ct` 的行，断言 verify 完整跑完且该行计入 mismatch）。
- [ ] 定位那条坏信封的归属表与行（verify 修好后由 mismatch 报告直接给出，
      不必现在手工捞）。
- [ ] 主计划撤销 Task 0.2 的「⚠️ 表同步前必完成」排序约束（因果方向已证反）。
- [ ] 主计划按 §4 改写 Task 0.2 的验收标准。
