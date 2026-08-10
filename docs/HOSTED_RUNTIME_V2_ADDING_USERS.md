# 把用户切到 Hosted Runtime V2（Runbook）

如何把一个用户从 V1（resident / agent-runner）切到 V2（hosted serve_worker），
或切回去。含 **API** 和 **DB** 两种方式。

## 背景：fence + allowlist + reconciler

- 每个用户有一个 **fence**（运行时开关）：`v2_runtime_state.hosted_runtime_state`
  ∈ {`resident`（V1）, `v2`（hosted）}，默认 `resident`。
- **`v2_user_allowlist`** 是**期望态**控制表：`(user_id PK, desired ∈ {'v2','resident'},
  updated_at, updated_by, note)`。你不直接改 fence，而是改这张表的 `desired`。
- **runtime-reconciler**（backend 内单例，`FEEDLING_RECONCILE_INTERVAL_SEC` 默认 **15s**
  轮询一次）把每个用户的 fence 推向 allowlist 里的 `desired`。所以改完 allowlist 后
  **约 15–30s** fence 才翻转。

### ⚠️ 前置条件（否则不翻）

- **用户必须有一条 active provider route**（`model_api_routes.is_active = true`，即配好了
  BYOK provider key）。reconciler **只翻有 provider key 的用户**；没配的即使 pin 了也停在
  `resident`，等用户配好 provider 后才翻。
- 查前置：
  ```sql
  SELECT count(*) FROM model_api_routes WHERE user_id='usr_xxx' AND is_active;
  ```

---

## 新注册 Model API 用户的自动 V2 admission

`FEEDLING_V2_NEW_USER_CUTOFF` 是主 CVM **backend** 的环境变量。只接受带时区的 UTC
ISO-8601 时间戳（推荐以 `Z` 结尾，例如 `2026-08-10T00:00:00Z`）；未配置、空值、格式
非法，或用户注册时间不能可靠解析时都 **fail-safe 为 resident**。`FEEDLING_RUNTIME_DEFAULT_DESIRED`
始终保持 `resident`，因此没有 cutoff 的代码部署是零行为变化阶段。

自动判定只在成功测试且已激活的 Model API route 已持久化后执行，且只覆盖
`users.created_at >= cutoff` 的账号。命中后会创建：

```text
desired=v2
updated_by=new-user-cohort
note=registered-at-or-after:<normalized-cutoff>
```

任何 `updated_by != 'new-user-cohort'` 的人工或用户路线记录都是更高优先级的显式 pin，
不会被自动 admission 覆盖。尤其是 `desired=resident` 会持久阻止自动切回 V2；只删除
自动记录不是可靠的 resident pin，因为用户以后再次完成 setup 时仍可能重新满足 cohort
条件并创建该记录。

### 启用、观察与停止 admission

按环境分阶段执行：先以空 cutoff 部署代码并验证健康；再设置一个明确的 UTC cutoff，重部署
measured Compose，随后用 runtime allowlist reconciliation view 观察
`updated_by='new-user-cohort'` 的 `desired`、实际 mode/state/generation 与 `converged`。
同时观察 V2 首轮回复成功率、worker capacity、pending/oldest-job age 和延迟；不得把
provider key、聊天明文或用户内容写入运维记录。

停止新增自动 admission：清空 cutoff，或把它移动到未来。这不会改变已经进入 V2 的账号。
单个账号的持久回滚应写 `desired=resident`；批量回滚只操作自动 cohort，避免影响人工
canary 或其他来源的记录：

```sql
UPDATE v2_user_allowlist
SET desired='resident', updated_at=now()
WHERE updated_by='new-user-cohort' AND desired='v2';
```

---

## 方式 A：Admin API（推荐，单个/少量）

**端点**：`POST /v1/admin/runtime-allowlist`（prod = `https://api.feedling.app`）
**鉴权**：header `X-Admin-Token: <FEEDLING_ADMIN_PASSWORD>`（未配置返回 503，错误返回 401）。
密码来自部署 env `FEEDLING_ADMIN_PASSWORD`（GitHub secret，注入 CVM）。

### 切到 V2
```bash
curl -sX POST https://api.feedling.app/v1/admin/runtime-allowlist \
  -H "X-Admin-Token: $ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"usr_xxx","desired":"v2","note":"manual migration"}'
# → {"user_id":"usr_xxx","desired":"v2"}  (200)
```

### 查是否翻转成功（converged）
```bash
curl -s https://api.feedling.app/v1/admin/runtime-allowlist \
  -H "X-Admin-Token: $ADMIN_TOKEN" | jq '.allowlist[] | select(.user_id=="usr_xxx")'
# 关注 .converged == true 且 .actual.state == "v2"
```

### 持久回滚到 resident
```bash
# 用同一个 POST 写显式 resident pin；它优先于 automatic cohort admission：
-d '{"user_id":"usr_xxx","desired":"resident"}'
```

如确需清理 allowlist 行，`remove` **不是**持久 resident 回滚。对
`updated_by='new-user-cohort'` 的自动行，后续一次成功的 Model API setup 可能重新创建它；
不要用删除来表达该用户长期留在 resident 的决定。

```bash
# 非持久清理；不能代替 resident pin：
-d '{"user_id":"usr_xxx","desired":"remove"}'
```

`desired` 只接受 `v2` / `resident` / `remove`；缺 `user_id` 或 `desired` 返回 400。

---

## 方式 B：直连 DB（批量，或没有 admin token 时）

DB = prod RDS（`PROD_DATABASE_URL`，见 `.env`）。直接写 `v2_user_allowlist`，reconciler 照常翻。

### 批量切到 V2
```bash
PROD=$(grep -m1 '^PROD_DATABASE_URL=' .env | cut -d= -f2-)
psql "$PROD" -c "INSERT INTO v2_user_allowlist (user_id, desired, updated_by, note) VALUES
  ('usr_a','v2','ops-manual','batch v2 migration'),
  ('usr_b','v2','ops-manual','batch v2 migration')
ON CONFLICT (user_id) DO UPDATE SET desired='v2', updated_at=now();"
```

### 等 reconciler 翻 fence（约 15–30s）
```bash
UIDS="'usr_a','usr_b'"
for i in $(seq 1 15); do
  cnt=$(psql "$PROD" -tAc "SELECT count(*) FROM v2_runtime_state
        WHERE user_id IN ($UIDS) AND hosted_runtime_state='v2';")
  echo "poll $i: v2=$cnt"; [ "$cnt" = "2" ] && break; sleep 6
done
```

### 持久回滚（DB）
```sql
-- 显式 resident pin；不要直接更新 ownership fence：
INSERT INTO v2_user_allowlist (user_id, desired, updated_by, note)
VALUES ('usr_xxx', 'resident', 'ops-manual', 'durable resident rollback')
ON CONFLICT (user_id) DO UPDATE SET
  desired=EXCLUDED.desired, updated_by=EXCLUDED.updated_by,
  note=EXCLUDED.note, updated_at=now();
```

如确需删除 allowlist 行，这只是非持久清理；自动 cohort 行会在后续成功的 Model API setup
中重新创建，不能作为 resident pin：

```sql
DELETE FROM v2_user_allowlist WHERE user_id='usr_xxx';
```

---

## 验证

| 手段 | 判据 |
|---|---|
| DB | `SELECT COALESCE((SELECT hosted_runtime_state FROM v2_runtime_state WHERE user_id='usr_xxx'),'resident');` → `v2` |
| API | `GET /v1/admin/runtime-allowlist` 里该行 `converged=true`、`actual.state=v2` |

---

## 注意事项

- **⚠️ 存量用户切 V2 的冷启动**：有大量 V1 历史的用户切到 V2 后，在 V2 首次为其历史建立
  summary 覆盖之前，头几条 chat turn 可能因 `prompt_coverage_incomplete`（fail-closed 安全
  不变量：不发有 coverage 洞的 prompt）**可见失败几次**（进终态、app 收到错误），summary
  backfill 追上后（通常几分钟内）自愈。批量迁移多个存量用户时预期会有这个过渡窗口。
- **只翻有 provider key 的用户**（见前置条件）。没配 provider 的用户 pin 了会停在 resident。
- reconciler 是 backend 单例（advisory-lock 选主），跑在一个 worker 上；`FEEDLING_RECONCILE_INTERVAL_SEC`
  默认 15s。
- allowlist 的写是幂等的（`ON CONFLICT DO UPDATE` / API 幂等），可安全重复执行。
- 切 V2 后该用户的 chat turn 由主 CVM 内的 serve_worker 池处理，可查 `v2_turn_metrics`
  （`lane`/`status`/`provider`/`model`/`latency_ms`）确认是否走了 V2 且成功。
