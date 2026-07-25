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

### 回滚
```bash
# 切回 V1：
-d '{"user_id":"usr_xxx","desired":"resident"}'
# 从 allowlist 移除（用户回到默认 resident，不再被 reconciler 管）：
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

### 回滚（DB）
```sql
-- 切回 V1：
UPDATE v2_user_allowlist SET desired='resident', updated_at=now() WHERE user_id='usr_xxx';
-- 移除：
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
