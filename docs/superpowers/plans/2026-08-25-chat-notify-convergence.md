# Chat Notification Convergence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** 收敛 Chat 跨 worker 通知协议，消除同一聊天变更同时触发 DB v2 增量同步和应用层旧版 256 行快照的问题，并保留非聊天状态即时唤醒 long-poll 的能力。

**Architecture:** `chat_messages` 变更只由数据库 trigger 发出 durable v2 通知；`agent_status_events`、MCP 配置、视觉测试、turn activity 等非聊天状态使用显式 `wake-only` 通知。新 worker 对旧版 chat 通知执行增量校准并无条件唤醒 waiter，不再无条件加载 hot snapshot；老 worker 仍可把 `wake-only` 当旧版 chat 通知处理，因此滚动部署期间安全但暂时较重。

**Tech Stack:** Python 3.13、FastAPI、PostgreSQL LISTEN/NOTIFY、psycopg 3、pytest、CloudWatch、Phala CVM、MDX/Next.js docs-site。

**Spec:** `docs/superpowers/specs/2026-08-25-chat-notify-convergence-design.md`

**Global Constraints:** 保持 `legacy` / `observe` / `incremental` 三种模式可回退；不改变公开 API；日志不得包含 user id、消息 ID、密文或连接串；不改变 hot cache 256；先进入 `test` 并取得环境证据，生产发布继续遵循 `test` / `pre` → `main` 分支流。

---

## Task 1: 固化 typed wake-only 协议和接收路径

**Files:**

- Modify: `backend/core/wake_bus.py`
- Modify: `tests/test_wake_bus.py`

### Step 1: 写发送端 payload 的失败测试

在 `tests/test_wake_bus.py` 增加 exact-shape 测试：

```python
def test_notify_chat_wake_only_emits_exact_typed_payload(monkeypatch):
    sent = []
    monkeypatch.setattr(
        wake_bus.db, "pg_notify",
        lambda channel, payload: sent.append((channel, payload)),
    )
    monkeypatch.setattr(wake_bus, "WORKER_ID", "worker-a")

    wake_bus.notify_chat_wake_only("u7")

    assert sent == [(
        wake_bus.PG_CHANNEL,
        json.dumps(
            {"c": "chat", "u": "u7", "o": "worker-a", "w": 1},
            separators=(",", ":"),
        ),
    )]
```

另加 disabled 场景，确认 `FEEDLING_WAKE_BUS_ENABLED=0` 时不调用 `db.pg_notify`。

### Step 2: 运行发送端测试，确认红灯

Run:

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests/test_wake_bus.py::test_notify_chat_wake_only_emits_exact_typed_payload -q
```

Expected: `FAILED`，因为 `notify_chat_wake_only` 尚不存在。

### Step 3: 实现最小发送函数

在 `backend/core/wake_bus.py` 的 `notify()` 旁增加：

```python
def notify_chat_wake_only(user_id: str) -> None:
    """Wake remote chat pollers without claiming a chat-row mutation."""
    if not _enabled():
        return
    payload = json.dumps(
        {"c": "chat", "u": str(user_id), "o": WORKER_ID, "w": 1},
        separators=(",", ":"),
    )
    db.pg_notify(PG_CHANNEL, payload)
```

不要设置 `v`：`v` 保留给 DB durable chat protocol。与现有 `notify()` 一样，调用点决定 best-effort 异常边界。

### Step 4: 写接收端失败测试

在 `tests/test_wake_bus.py` 添加或改写以下完整测试契约：

- `test_dispatch_wake_only_only_wakes_chat_waiters`: 构造 Store spy，调用 `_dispatch()` 后断言 `calls == ["chat_waiters"]`，`ensure_chat_fresh` 与 `reload_chat_hot_strict` 都用一旦调用就抛错的 stub。
- `test_dispatch_rejects_malformed_wake_only_payload`: 参数化 `w=True`、`w=2`、额外 key、缺少 `o` 四种 payload；Store 的三个方法一旦调用就抛错，dispatch 后无异常即证明 fail-closed。
- `test_dispatch_legacy_chat_in_incremental_mode_syncs_then_wakes`: `ensure_chat_fresh` 记录 kwargs 并返回 True；断言 `[{"force": True}, "chat_waiters"]`，reload stub 不得调用。
- `test_dispatch_legacy_chat_in_incremental_mode_wakes_when_sync_fails`: ensure 返回 False，仍断言 waiter 被调用一次。
- `test_dispatch_legacy_chat_in_legacy_mode_keeps_snapshot_behavior`: 断言调用顺序严格为 `reload_chat_hot_strict`、`notify_chat_waiters`。

拆分现有 `test_dispatch_legacy_chat_refreshes_only_chat`；保留 v2 target-version 测试。

### Step 5: 运行接收端测试，确认红灯

Run:

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests/test_wake_bus.py -q
```

Expected: 新增 wake-only 与 incremental legacy 契约失败，已有测试通过。

### Step 6: 实现严格分流和固定枚举 telemetry

在 `_dispatch_chat()` 读取 store 前分类：

```python
is_wake_only = "w" in data
is_v2 = "v" in data
if is_wake_only:
    if (
        set(data) != {"c", "u", "o", "w"}
        or type(data.get("w")) is not int
        or data.get("w") != 1
    ):
        return
    origin = data.get("o")
    if not isinstance(origin, str) or not origin or len(origin) > 128:
        return
elif is_v2:
    # 保留 {v,c,u,r} exact shape 与 positive-int r。
else:
    # 旧版必须 exact {u,c,o} 并校验 origin。
```

处理顺序：

```python
if is_wake_only:
    store.notify_chat_waiters()
    # telemetry reason=wake_only
    return
if not is_v2 and mode == "incremental":
    ok = store.ensure_chat_fresh(force=True)
    store.notify_chat_waiters()
    # legacy_payload 或 sync_failed
    return
```

把 `wake_only` 加入 `_CHAT_SYNC_REASONS`。v2 incremental 的 `already_fresh` 路径保持现状；wake-only 专门负责非 chat 状态。

### Step 7: 验证并提交

Run:

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests/test_wake_bus.py tests/test_chat_poll_cross_worker_staleness.py -q
git diff --check
```

Expected: 全部通过；diff check 无输出。

Commit:

```bash
git add backend/core/wake_bus.py tests/test_wake_bus.py
git commit -m "feat(chat): add typed wake-only notifications"
```

---

## Task 2: 迁移 6 个非聊天状态唤醒点

**Files:**

- Modify: `backend/chat/activity_store.py`
- Modify: `backend/hosted/mcp_core.py`
- Modify: `backend/hosted/setup_core.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `tests/test_chat_turn_activity_v2.py`
- Modify: `tests/test_user_mcp_core.py`
- Modify: `tests/test_vision_model_v2.py`
- Modify: `tests/test_v2_jobs_store.py`

### Step 1: 先把语义测试改成新接口

更新 monkeypatch/断言，使这些行为显式依赖 `notify_chat_wake_only`：resident tool activity 首次插入、MCP 配置/状态、setup main vision test、V2 status event、turn activity。幂等重放不得额外唤醒。

例如修改 `tests/test_v2_jobs_store.py::test_append_status_event_fires_cross_process_chat_wake`：

```python
def test_append_status_event_fires_typed_chat_wake(monkeypatch):
    wakes = []
    monkeypatch.setattr(
        jobs_store.wake_bus, "notify_chat_wake_only",
        lambda user_id: wakes.append(user_id),
    )
    event_id = jobs_store.append_status_event(
        "u_js_9d", "processing", label="starting"
    )
    assert event_id > 0
    assert wakes == ["u_js_9d"]
```

### Step 2: 运行测试，确认红灯

Run:

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_chat_turn_activity_v2.py tests/test_user_mcp_core.py \
  tests/test_vision_model_v2.py tests/test_v2_jobs_store.py -q
```

Expected: 新接口 spy 未被调用；实现仍走 `notify("chat", ...)`。

### Step 3: 迁移精确 6 个调用

把下列调用的 `wake_bus.notify("chat", user_id)` 替换为 `wake_bus.notify_chat_wake_only(user_id)`：

- `backend/chat/activity_store.py`: resident tool activity，1 处。
- `backend/hosted/mcp_core.py`: MCP 状态/配置，2 处。
- `backend/hosted/setup_core.py`: setup main vision test，1 处。
- `backend/model_api_runtime/v2/jobs_store.py`: status event、turn activity，2 处。

保留既有 try/except 和本地 fast path；本任务不处理真正写 `chat_messages` 的 11 处。

### Step 4: 验证并提交

Run Task 2 Step 2 的命令，加 `git diff --check`。Expected: 全部通过。

Commit:

```bash
git add backend/chat/activity_store.py backend/hosted/mcp_core.py \
  backend/hosted/setup_core.py backend/model_api_runtime/v2/jobs_store.py \
  tests/test_chat_turn_activity_v2.py tests/test_user_mcp_core.py \
  tests/test_vision_model_v2.py tests/test_v2_jobs_store.py
git commit -m "refactor(chat): route non-chat state through wake-only"
```

---

## Task 3: 删除 11 个 chat mutation 的重复应用层通知

**Files:**

- Modify: `backend/core/store.py`
- Modify: `backend/chat/chat_core.py`
- Modify: `backend/voice/routes_asgi.py`
- Modify: `backend/hosted/history_import.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`
- Modify: `backend/agent_runtime/supervisor.py`
- Modify: `tests/test_chat_idempotency_unit.py`
- Modify: `tests/test_chat_response_finalize_cas.py`
- Modify: `tests/test_model_api_path.py`
- Modify: `tests/test_agent_runtime_supervisor.py`
- Modify: `tests/test_chat_change_events.py`

### Step 1: 写 DB v2 唯一 mutation 广播契约

扩充 `tests/test_chat_change_events.py`：真实事务提交后从 LISTEN 连接读取通知，覆盖 upsert/delete 并断言 exact shape：

```python
assert payload == {
    "v": 2, "c": "chat", "u": user_id, "r": expected_version,
}
```

确认 rollback 不产生 committed effect。再通过真实 `UserStore.append_chat(strict=True)` 写入一行，并在同一 LISTEN 连接上收集通知：断言只有一条 DB v2 payload，且等待额外 200ms 后没有第二条 legacy payload。这个行为测试会在任一 mutation 路径重新加入应用层 `notify("chat")` 时捕获重复广播。

### Step 2: 更新业务测试预期

把 strict/idempotent append、response finalize、onboarding greeting、supervisor claim release、voice cleanup、terminal failure reply 的测试改为：本地 cache/waiter、事务/CAS、幂等和 failure metadata 不变，但不再期待应用层 legacy notify。supervisor 顺序测试仅删除 `notify:chat` 项，保留 release → kill/spawn。

### Step 3: 运行失败测试

Run:

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_chat_change_events.py \
  tests/test_chat_idempotency_unit.py tests/test_chat_response_finalize_cas.py \
  tests/test_model_api_path.py tests/test_agent_runtime_supervisor.py -q
```

Expected: high-level append 的 exact-one-notification 测试收到 v2 和 legacy 两条通知并失败；更新后的无 legacy notify 断言失败。

### Step 4: 删除精确 11 个调用

- `backend/core/store.py`: append、finalize、sequence finalize、idempotent append，4 处。
- `backend/chat/chat_core.py`: clear，1 处。
- `backend/voice/routes_asgi.py`: cancel/finalize cleanup，2 处。
- `backend/hosted/history_import.py`: onboarding greeting insert，1 处。
- `backend/model_api_runtime/v2/jobs_store.py`: terminal failure reply insert，2 处。
- `backend/agent_runtime/supervisor.py`: claim release update，1 处。

只删跨进程旧通知；保留 `apply_committed_chat_rows()`、`notify_chat_waiters()`、clear 本地更新、DB trigger 和其他 channel。同步修正 `backend/core/store.py` docstring，把 `wake_bus.notify` 改成“DB v2 trigger 广播”。清除未使用 import。

### Step 5: 验证零旧调用并提交

Run:

```bash
rg -n 'wake_bus\.notify\("chat"' backend
```

Expected: 无输出，退出码 1。

Run:

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_chat_change_events.py \
  tests/test_chat_idempotency_unit.py tests/test_chat_response_finalize_cas.py \
  tests/test_model_api_path.py tests/test_agent_runtime_supervisor.py \
  tests/test_voice_revision_latest_wins.py -q
git diff --check
```

Expected: 全部通过。

Commit:

```bash
git add backend tests
git commit -m "perf(chat): retire duplicate mutation notifications"
```

---

## Task 4: 增加 snapshot fallback 固定原因 telemetry

**Files:**

- Modify: `backend/core/store.py`
- Modify: `tests/test_chat_incremental_sync.py`

### Step 1: 写固定枚举与脱敏失败测试

参数化覆盖 `overflow`、`gap`、`reset`、`missing_row`、`generation_conflict`。用 `caplog` 断言：

```python
assert f"reason={expected_reason}" in caplog.text
assert "user_hash=" in caplog.text
assert store.user_id not in caplog.text
assert "message_ids" not in caplog.text
```

直接调用 helper，未知 reason 必须抛 `ValueError`。

### Step 2: 运行测试，确认红灯

Run:

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests/test_chat_incremental_sync.py -q
```

Expected: 新 telemetry 断言失败。

### Step 3: 实现 content-free helper

在 `backend/core/store.py` 定义：

```python
_CHAT_SNAPSHOT_FALLBACK_REASONS = frozenset({
    "gap", "reset", "overflow", "missing_row", "generation_conflict",
})

def _chat_snapshot_fallback_telemetry(
    *, user_id: str, reason: str, hot_rows: int
) -> None:
    if reason not in _CHAT_SNAPSHOT_FALLBACK_REASONS:
        raise ValueError("invalid chat snapshot fallback reason")
    user_hash = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]
    log.info(
        "chat_sync_snapshot_fallback reason=%s user_hash=%s hot_rows=%d",
        reason, user_hash, max(0, int(hot_rows)),
    )
```

在 `ensure_chat_fresh()` 每个 incremental → snapshot 分支，先记录再 reload。`reset` 优先于 `gap`；空事件、断层、尾版本落后统一为 `gap`。不记录 IDs、版本列表、原始异常或 user id。

### Step 4: 验证并提交

Run:

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests/test_chat_incremental_sync.py tests/test_wake_bus.py -q
git diff --check
```

Expected: 全部通过，日志不含测试 user id。

Commit:

```bash
git add backend/core/store.py tests/test_chat_incremental_sync.py
git commit -m "feat(chat): expose snapshot fallback reasons"
```

---

## Task 5: 同步运维与公开架构文档

**Files:**

- Modify: `docs/ops/chat-incremental-sync-runbook.md`
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`

### Step 1: 更新 runbook

增加协议表：

| Payload | Producer | 新 worker | 老 worker |
|---|---|---|---|
| `{v:2,c:chat,u,r}` | DB trigger | event sync | bounded snapshot |
| `{c:chat,u,o,w:1}` | 非 chat 状态 | waiter only | bounded snapshot |
| `{u,c:chat,o}` | 历史 producer | 校准 + wake | bounded snapshot |

补充上线观测：`wake_only`、`legacy_payload`、`sync_failed`、`chat_sync_snapshot_fallback reason=*`。稳态预期 `legacy_payload=0`、`sync_failed=0`；fallback 低且原因可解释。

### Step 2: 更新架构与 changelog

`architecture.mdx` 说明 durable mutation 与 wake-only 职责、滚动兼容。`changelog.mdx` 的 `Unreleased` 记录：多 worker Chat 同步减少重复 hot-snapshot DB 读取，不改变 Chat/MCP/activity 即时可见性和公开 API。

本变更不修改 OpenAPI schema，但仍跑 contract tests。

### Step 3: 验证并提交

Run:

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests/test_openapi_contract.py -q
cd docs-site
npm run types:check
npm run lint
npm run build
cd ..
git diff --check
```

Expected: 全部通过。

Commit:

```bash
git add docs/ops/chat-incremental-sync-runbook.md \
  docs-site/content/docs/architecture.mdx \
  docs-site/content/docs/changelog.mdx
git commit -m "docs(chat): document notification convergence"
```

---

## Task 6: 全量本地验证与独立复核

**Files:** Review all Task 1–5 changes.

### Step 1: 运行相关回归集合

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_wake_bus.py tests/test_chat_incremental_sync.py \
  tests/test_chat_poll_cross_worker_staleness.py tests/test_chat_change_events.py \
  tests/test_chat_turn_activity_v2.py \
  tests/test_user_mcp_core.py tests/test_vision_model_v2.py \
  tests/test_v2_jobs_store.py tests/test_chat_idempotency_unit.py \
  tests/test_chat_response_finalize_cas.py tests/test_model_api_path.py \
  tests/test_agent_runtime_supervisor.py tests/test_voice_revision_latest_wins.py -q
```

Expected: 全部通过，无新增 warning 类别。

### Step 2: 运行完整后端测试

```bash
DATABASE_URL='postgresql://postgres:test@127.0.0.1:55432/postgres' \
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python \
  -m pytest tests -q
```

Expected: 全部通过。无关的已知失败必须保留完整命令和基线复现证据，不能静默归因于本变更。

### Step 3: 静态复核

```bash
rg -n 'wake_bus\.notify\("chat"' backend
rg -n 'notify_chat_wake_only' backend tests
rg -n '"w"\s*:\s*1|data\.get\("w"\)' backend/core/wake_bus.py tests/test_wake_bus.py
git diff origin/test...HEAD --check
git status --short
```

Expected: 第一条无输出；第二条为 typed sender、6 个 producer 和测试；payload/validator 均被覆盖；diff clean；status 只有预期文件。

### Step 4: verification-before-completion 清单

核对：老 receiver 把 `{c,u,o,w:1}` 当 generic chat，滚动安全；新 receiver 对未知 key fail-closed；legacy 模式仍 snapshot；incremental legacy sync 失败也 wake；v2 trigger 不依赖应用进程存活；telemetry 仅固定枚举/hash/计数；未顺带实现 retention、TTL、cache <256 或生产监控开关。

发现问题时先加失败测试再修，使用新 commit，不 amend 已复核提交。

---

## Task 7: 集成并部署 TEST

**Files:** No source changes expected; deployment evidence goes in handoff/PR.

### Step 1: 基于最新 `origin/test` 验证

```bash
git fetch origin test
git log --oneline --decorate --max-count=8 HEAD origin/test
git rebase origin/test
```

Expected: 无冲突；有冲突则保留 `test` 新行为并重跑 Task 6。不得覆盖主 worktree 用户未提交文件。

### Step 2: 按分支流集成

推送 `opt/chat-notify-convergence-20260825` 并以 `test` 为目标评审/合并；只有用户明确授权时才直接更新 `test`。普通优化分支不得直接向 `main` 开 PR，不得本地拼装后推 `main`。

### Step 3: 确认 TEST 部署

确认 CI/deploy 成功；TEST image 对应 merge commit；backend/runner health 正常；migration head 无漂移；chat v2 trigger enabled；`FEEDLING_CHAT_SYNC_MODE=incremental` 与 `FEEDLING_CHAT_HOT_CACHE_LIMIT=256` 保持。

### Step 4: TEST 功能回归

用可清理账号验证：跨 worker chat/poll；clear 后不返旧消息；V2 status 渐进可见；resident activity；MCP 状态；vision test；voice cancel/finalize；terminal failure reply；onboarding greeting；多 worker 并发无重复、漏消息或顺序倒退。清理测试数据并记录方法。

### Step 5: 观察至少 30 分钟

记录 `legacy_payload`（稳态 0）、`wake_only`、`sync_failed`（0）、各 fallback 原因，以及 RDS `NetworkTransmitThroughput`、`ReadIOPS`、`DatabaseConnections`、CPU/credit 与同星期/同小时窗口对比。

不承诺固定百分比；验收是普通 chat mutation 从“event sync + 256-row snapshot”收敛为“仅 event sync”。`legacy_payload=0` 且普通 mutation 不伴随 snapshot fallback，即证明主要重复流量被切断。

### Step 6: 形成晋级证据

记录 merge commit、TEST image、回归项目、日志计数、CloudWatch 窗口和残余风险。证据通过后才可按 `test`/`pre` → `main` 提议生产晋级；本计划不授权直接推生产。
