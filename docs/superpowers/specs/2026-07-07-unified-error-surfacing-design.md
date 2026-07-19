# 全后端统一错误透出 — 后端设计 spec

> **RETIRED / DO NOT DEPLOY.** Historical design; managed hosted supervisor
> instructions describe retired source.

日期：2026-07-07
状态：设计定稿，待实现（Phase A → B → C 三个独立实现计划）
对外契约：`docs/FRONTEND_ERROR_CONTRACT.md`（iOS 消费面，字段形状以它为准；本文是后端实现面）
前置特性：聊天回合错误透出（`feat/upstream-error-surfacing` 分支，spec
`2026-07-06-upstream-error-surfacing-design.md`）——本设计的分类器/blame 纪律/
双写模式都以它为参考实现，Phase B 依赖其 `/v1/model_api/runtime_error` 路由与
consumer 分类器已合入。

## 背景

聊天回合的错误透出已解决（system 气泡 + 设置页 last_runtime_error），但全局
盘点（2026-07-07 Explore 报告）显示其余错误面仍有大缺口：

- **runner/supervisor 层完全盲区**：spawn 失败、provider key 解不开只有服务端
  log；`agent_runtime_instances.status/error` 列与 `leases.mark_error()` 存在但
  **零调用零读取**。用户表现为「永远没回复」。
- **记忆退避不可见**：capture/dream 连续失败进指数退避，用户只觉得「记忆不更新」。
- **蒸馏/导入失败只在进度页可见**：用户离开页面就不知道半夜失败了。
- **同步 HTTP 错误可读性差**：57 个 slug 无归责无话术约定；无兜底 500 处理器
  （未捕获异常走 FastAPI 默认输出）；FastAPI 422 校验错误是另一套
  `{"detail":[...]}` 形状；无 request_id 可对账。
- **没有统一的用户通知面**：iOS 想全面感知要轮询七八个各自形状的端点。

## 目标（三大件）

1. **同步 HTTP 错误信封**：全后端非 2xx 响应统一形状，增量不破坏。
2. **统一通知中心**：`GET /v1/notices`，跨子系统的用户可见错误投影层。
3. **场景内字段补齐**：`last_test_error` 等已持久化但未暴露的字段。

## 非目标

- **不做推送**（APNs）：通知只在 App 打开时拉取；severity 升级推送留后续。
- **不迁移存量 117 处错误返回**：`error` slug 字段语义不变，新字段增量可选；
  只有「自由文本 error」（约 10 处）收敛成 slug + detail。
- **iOS 渲染**：独立仓任务，输入是 FRONTEND_ERROR_CONTRACT.md。
- **不动内部排查流**：proactive_jobs 等日志流保持原样；notices 是投影层不是
  日志转发（各子系统失败点主动 emit，不做日志扫描/翻译任务）。
- **同步错误不进通知中心**：用户在场当场看到即可，不重复记录。

## 决策记录（brainstorm 结论，2026-07-07）

| 决策点 | 结论 |
|---|---|
| 全局通知承载 | 新统一端点 `GET /v1/notices`（否决：全走聊天气泡=污染对话；whoami 捎带=热路径不宜变重且无历史） |
| 通知生命周期 | 状态型：同 dedupe_key upsert（occurrences+1），恢复点写 resolved（否决：事件型追加=重试风暴刷屏） |
| 拉取契约 | **快照式无游标**：状态型原地更新与 seq 游标语义矛盾（resolved 不动 seq，增量拉取会漏），量级小（per-user ≤200）全量快照可行 |
| 存储 | **不建新表**：user_logs 新 stream `user_notices`，`item_key=dedupe_key` 复用唯一索引与 `log_patch_item` upsert；零 alembic、FK CASCADE/log_trim 免费 |
| 首批生产者 | genesis、history_import、memory 退避、runner/supervisor、chat 双写（全选） |
| HTTP 话术归属 | iOS 按 slug 本地映射（后端只保证 slug 稳定）；通知的 user_text 服务端下发作兜底（含动态内容） |
| blame 纪律 | 沿用前置特性：user_provider / provider_transient / system；system 侧话术绝不引导用户改配置 |

---

## Phase A：同步 HTTP 错误信封

### A1. 中央 helper

`backend/asgi/responses.py` 新增：

```python
def api_error(status: int, slug: str, *, blame: str = "", detail=None,
              request_id: str = "") -> JSONResponse:
    """统一错误信封。slug 是稳定契约面（见 docs/API_ERRORS.md）；
    blame/detail/request_id 增量可选，缺省不出现在 body。"""
```

存量 `{"error": ...}` 返回**不强制迁移**；新代码与被触碰的路由渐进采用。

### A2. 兜底异常处理器 + request_id

`backend/asgi/middleware.py::register_exception_handlers` 补两个（现状只有
AuthError/ClientDisconnect/HTTPException/PoolTimeout）：

1. **`Exception` 兜底**：生成 `request_id = "req_" + hex8`，服务端
   `log.exception("[req_id=%s] ...")` 带同 id，响应
   `500 {"error":"internal_error","request_id":...}`。杜绝 FastAPI 默认输出。
2. **`RequestValidationError`**：重塑为
   `400 {"error":"invalid_payload","detail":[<精简后的字段错误>]}`，消灭
   `{"detail":[...]}` 双形状。

request_id 同时写响应头 `X-Request-Id`。实现定为：请求入口预生成进
contextvar（access-log 中间件里，行尾带 req_id），错误 handler 从 contextvar
取——错误响应与访问日志天然同 id 对账。

### A3. slug 治理

- 新文档 `docs/API_ERRORS.md`：全量 slug 契约表（slug / 状态码 / 含义 / blame /
  哪些需要 iOS 本地化）。初版从现状 57 个 slug 盘入 + 本设计新增的。
- 自由文本错误收敛（已 grep 定位，约 10 处）：`envelope missing fields: [...]`
  →`envelope_missing_fields`+detail、`thinking_envelope missing fields`→同、
  `f"{new_type}_requires_anchor"`→`anchor_required`+detail。逐处列在实现计划里。
- slug 变更纪律写进 CONTRIBUTING.md 一行：新增错误返回必须用 slug（进
  API_ERRORS.md），禁止自由文本。

### A4. 测试

- 兜底 500：造一个抛异常的测试路由 → 断言信封形状 + request_id 头/体一致 +
  服务端日志含同 id。
- validation 重塑：畸形 body 打真实路由 → `invalid_payload` 形状。
- 全量回归：现有测试对错误 body 的断言不得破坏（`error` 字段语义不变即安全）。

---

## Phase B：通知设施 + chat 双写 + 场景字段

### B1. `backend/notices/` 模块（新包，CONTRIBUTING 分层：core 纯逻辑 + 无路由依赖）

```python
# backend/notices/core.py
VALID_SOURCES = ("genesis", "history_import", "memory", "runner", "chat")
VALID_BLAME = ("user_provider", "provider_transient", "system")
VALID_SEVERITY = ("error", "warning")
NOTICES_STREAM = "user_notices"
NOTICES_MAX = 200          # log_trim 上限
RESOLVED_WINDOW_SEC = 7 * 86400

def emit(store, *, source, error_class, blame, severity, user_text,
         detail="", dedupe_key) -> None:
    """upsert：item_key=dedupe_key 已存在且未 resolved → occurrences+1、
    last_ts/detail/user_text 刷新；已 resolved 或不存在 → 新建（occurrences=1，
    新 notice_id）。绝不抛出（观测性不影响主流程）。"""

def resolve(store, dedupe_key_prefix: str) -> None:
    """把 item_key 以前缀匹配的未 resolved 通知标记 resolved=true +
    resolved_ts。绝不抛出。前缀匹配支持 'chat:'、'runner:' 这类按域清空。"""
```

doc 字段即 FRONTEND_ERROR_CONTRACT.md §四的 JSON（notice_id/source/error_class/
blame/severity/user_text/detail≤300/dedupe_key/occurrences/first_ts/last_ts/
resolved/resolved_ts）。**明文存储**：内容是系统错误信息非用户内容，与
gate_decisions 同级，不走加密信封。

实现注意：`log_patch_item` 按 (user_id, stream, item_key) 定位——「已 resolved
→ 新建」需要先读现状；用 `db.log_read` 单条读或给 log_patch_item 加读回。
resolve 的前缀匹配在 Python 侧过滤（读全量 ≤200 条再逐条 patch）。

### B2. 读端点

```
GET /v1/notices?include_resolved=<bool, 默认 true>
```

新文件 `backend/notices/routes_asgi.py`（`require_auth`，无 scope——与其它用户
面端点一致）。逻辑：读该用户 `user_notices` 全量 → 过滤（未 resolved 全给；
resolved 只给 `resolved_ts >= now - 7d` 且 `include_resolved` 时）→ 按
`last_ts` 倒序 → `{"notices": [...]}`。注册进 `asgi_app.py`（assembly-only）。

### B3. chat 双写（服务端扇出，consumer 零改动）

`backend/hosted/config_store.record_runtime_error`（前置特性已有）内扇出：

- `error` 非空 → `notices.emit(source="chat", error_class=<error_class 参数>,
  blame=<按 error_class 查映射表>, severity="error",
  user_text=<按 error_class 的话术表>, detail=error,
  dedupe_key=f"chat:{error_class}")`
- `error` 为空（清空调用）→ `notices.resolve("chat:")`

error_class→(blame, user_text) 的映射表放 `backend/notices/catalog.py`，与
consumer 分类器的话术保持同一来源纪律（consumer 在 tools/ 不能 import backend，
两处各自维护但用测试锁一致性：catalog 覆盖 consumer `_ERROR_CLASS_RULES` 的
全部 error_class）。

### B4. 场景字段补齐

- `backend/provider_client.py` public_config 加 `last_test_error`（已持久化，
  一行暴露）。
- `backend/hosted/onboarding_validation.py` steps 附 `error` 细节字段（各 step
  已知失败原因时带上；无则省略）。

### B5. 测试

- notices core：emit 新建/upsert/resolved 后再 emit 新建、resolve 前缀匹配、
  never-raise（store 抛错不外溢）、trim。
- 路由：鉴权、快照过滤（活跃全给/resolved 7d 窗口/include_resolved=false）、
  排序。
- chat 扇出：record_runtime_error 带 error → 流里出现 chat:<class>；清空 →
  resolved。catalog 与 consumer 分类器 error_class 全集一致性测试
  （import 两侧枚举比对）。
- last_test_error 暴露 + onboarding steps error 字段。

---

## Phase C：四个生产者接入

统一模式：失败点 emit、恢复点 resolve、severity 按表；全部调用 wrap 在
notices 的 never-raise 保证下，不影响原流程。

### C1. genesis（`backend/genesis/service.py`）

- `mark_failed()` 内：emit(source="genesis", error_class=分类(job.error)，
  dedupe_key=f"genesis:{job_id}"，severity="error")。error 分类用
  catalog 的 `classify_upstream(text)`（backend 侧持有一份与 consumer 分类器
  等价的正则副本——consumer 在 tools/ 不能 import backend，两份由 B3 的一致性
  测试锁住），匹配不上落 `genesis_failed`。
- plaintext 蒸馏部分成功丢卡（现只进 warnings）：emit severity="warning"、
  error_class="genesis_partial"、dedupe_key=f"genesis:{job_id}:partial"。
- 恢复：任一 job 进入 `done` → resolve("genesis:")。

### C2. history_import（`backend/hosted/history_import.py`）

- 顶层失败（:3204 附近 except）、stale reaper、background_error 三个写入点：
  emit(error_class="import_failed" / "import_stale"，
  dedupe_key=f"history_import:{job_id}")。
- 恢复：job completed → resolve("history_import:")。

### C3. memory 退避（`backend/proactive/capture_scheduler.py`）

- 进入退避且 `fail_streak >= 3` 时：emit(source="memory"，
  error_class="memory_backoff"，severity="warning"，
  dedupe_key=f"memory_backoff:{lane}"，user_text 带 streak 次数与 lane 名)。
  streak 每 +1 重复 emit（=occurrences+1 原地更新，天然不刷屏）。
- 恢复：该 lane 任一 job completed（streak 归零处）→
  resolve(f"memory_backoff:{lane}")。

### C4. runner/supervisor（`backend/agent_runtime/supervisor.py`）

- **顺带接管死代码**：spawn 失败/异常路径调用 `leases.mark_error(...)`（写
  `agent_runtime_instances.status='error'/error`，首次真正接线）。
- emit 点（supervisor 直连 DB，进程内调用 notices）：
  - spawn 失败 → `runner:spawn_failed`（error_class="runner_spawn_failed"）
  - provider key 信封解不开（:375 附近）→ `runner:key_decrypt_failed`
    （error_class="runner_key_decrypt_failed"，blame=system）
  - identity 拉取失败 / runtime-token 刷新失败 → `runner:degraded`
    （error_class="runner_degraded"，severity="warning"）
- 恢复：成功 spawn + 首次心跳 renew 成功 → resolve("runner:")。
- **去抖注意**：supervisor tick 循环（~15s）重试失败会高频 emit——emit 的
  upsert 语义天然吸收（原地 +1），但要避免每 tick 都写 DB：supervisor 侧加
  进程内 per-(user,key) 的 60s 最小写间隔。

### C5. consumer 分类器新增 error_class（`tools/chat_resident_consumer.py`）

`_ERROR_CLASS_RULES` 增补三类（插在 `model_not_found` 与 `rate_limited`
之间——须在宽匹配的 rate/5xx 类之前，否则 422/400 类不兼容错误会被抢走）：

- `provider_incompatible`（user_provider）：`unknown variant|not supported|
  unsupported (parameter|tool)|invalid_request_error.*tool`
- `context_overflow`（user_provider）：`context.{0,20}(length|window)|maximum
  context|too many tokens|prompt is too long`
- `content_filtered`（provider_transient）：`content_filter|content policy|
  safety|blocked by`

catalog.py 同步补齐（B3 的一致性测试会强制）。

### C6. 测试

每个生产者：失败点 emit 落流（字段断言）、恢复点 resolve、原流程行为不变
（emit 抛错被吞）。runner 的 60s 写间隔单测。分类器新类用真实错误串用例
（xAI unknown variant、context length exceeded、content_filter）。

---

## 与既有系统的关系

- **前置依赖**：Phase B 的 chat 扇出改在 `record_runtime_error` 里——需
  `feat/upstream-error-surfacing` 先合入。Phase A 无依赖可并行。
- **聊天 system 气泡不动**：对话内即时性保留；通知中心是可回溯全量面。
- **内部日志流不动**：proactive_jobs 等保持原样（排查面）。
- **`user_notices` 是新造的流名**，代码库无前身；如与团队既有词汇冲突可在
  实现前改名（零成本窗口）。

## 部署顺序与风险

- Phase A/B 纯 backend，无迁移无链上操作，正常 CI 部署；Phase C 含 runner
  镜像（supervisor 改动）——backend 与 runner 镜像同批。
- 风险：emit/resolve 忘记 never-raise → 观测性拖垮主流程（测试强制锁）；
  notices 写放大（runner tick）→ C4 的最小写间隔；slug 治理半途而废 →
  CONTRIBUTING 纪律 + API_ERRORS.md 做 PR 审查依据。
- 回滚：三个 Phase 各自独立可回滚；iOS 未上通知中心前，notices 流只写不读，
  零用户可见风险。
