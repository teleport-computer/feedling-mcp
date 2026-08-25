# trace_events 分区维护手册（RDS 主库）

写给运维者（Seven）。回答三个问题：要做什么、什么时候做、忘了做会怎样。

背景一句话：`trace_events`（流程诊断事件表）按北京日做了日分区，迁移
0102 只预建了「迁移执行日 ±(29/60) 天」的分区窗口，窗口之外要靠一个
GitHub Actions 手动工作流滚动维护。应用进程刻意没有 DDL 权限，所以
这件事只能由持 owner DSN 的工作流做。

---

## 一、一次性准备（只做一次）

GitHub 仓库 Settings → Secrets and variables → Actions，确认以下
secrets 存在；缺哪个补哪个，值 = 对应环境数据库的 **owner/迁移 DSN**
（不是应用连接串）：

| Secret 名 | 用途 |
|---|---|
| `TEST_DATABASE_URL` | test 环境 RDS owner DSN |
| `PRE_DATABASE_URL`  | pre 环境 RDS owner DSN |
| `DATABASE_URL`      | prod 环境 RDS owner DSN（现有迁移流程已在用，大概率已配好） |

没配的环境跑工作流会直接报
`RDS migration credential is not configured`，不会误连别的库。

## 二、周期性动作（核心就这一件事）

**每月跑一次**下面的工作流，三个环境各跑一遍（建议每月 1 号，低峰时段）：

1. GitHub → Actions → **RDS trace partition maintenance**
2. Run workflow：
   - `environment`：`test` / `pre` / `prod` 选一个
   - `confirm`：输入 `MAINTAIN-RDS-TRACE`（**prod 要输 `MAINTAIN-RDS-TRACE-PROD`**）
3. 点 Run。绿了就完事。

### 硬期限

prod 的分区窗口从「0102 迁移在 prod 生效那天」（= T306 合入 main 后的
那次部署日）起算 **60 天**。第一次 prod 运行必须在那之前完成，此后每月
一次即可。test/pre 同理，各自从部署日起算。

### 工作流一次跑做了什么（`admin/trace_events_partitions.py`）

- **修 DEFAULT**：若兜底分区 `trace_events_default` 里有滞留行，
  在一个事务里 detach → 给滞留行所在的日子补建分区 → 整批搬回 →
  重新 attach。全程一把 `ACCESS EXCLUSIVE` 锁保证原子，失败自动回滚。
- **补窗口**：把「今天−29 天 … 今天+60 天」缺的日分区全部补齐。
- **清过期**：删掉 30 天前的日分区（= trace 数据保留期 30 天），
  以及 DEFAULT 里超过保留期的滞留行。
- 结束时校验 `trace_events_default` 必须为空，非空则工作流红灯。

## 三、判读运行结果

工作流日志最后会打印一行 report，关键字段：

| 字段 | 正常值 | 含义 |
|---|---|---|
| `default_rows_before` | 0 | 非 0 = 维护拖过了窗口，有数据掉进过兜底分区（本次已自动修复） |
| `default_rows_after` | **必须 0** | 非 0 工作流直接红灯 |
| `created` | 一串新分区名 | 本次补建的日分区 |
| `dropped` | 一串旧分区名 | 本次删掉的过期分区 |
| `moved_rows` | 通常 0 | 从 DEFAULT 搬回正式分区的行数 |

## 四、忘了跑会怎样（以及为什么不用慌）

- **数据不会丢**：超过窗口后新写入落进 DEFAULT 兜底分区，写路径不断。
- **但这是降级态**：后端有独立监控盯着 DEFAULT，非空会在 admin 指标里
  告警；DEFAULT 无限膨胀且不能按天清理。
- **修复 = 把工作流跑一遍**：它会自动把滞留行搬回正确的日分区。
  不需要任何手工 SQL。

## 五、注意事项

1. **保留期 30 天是硬删**：跑一次就会删掉 30 天前的 trace。若有正在
   调查的事故需要更久的证据，先导出相关行再跑，或临时用
   `--retention-days` 加大（需要改 workflow 调用处，默认没暴露参数）。
2. **锁**：`ACCESS EXCLUSIVE` 锁 parent 表，正常情况毫秒级，但 DEFAULT
   积压很大时搬运会拉长锁窗——挑低峰跑，尤其 prod。
3. **TEE 侧不用单独管**：TEE 主库沿用原有 tee-migrate 流程
   （`TEE_MIGRATION_DATABASE_URL` 兼容回退保留），本手册只覆盖
   RDS 三环境。
4. 工作流按环境拉对应分支代码（prod→main、pre→pre、test→test），
   有并发保护（同环境同时只跑一个）。
