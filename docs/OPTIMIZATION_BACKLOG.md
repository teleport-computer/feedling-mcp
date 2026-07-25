# 优化清单（技术债 backlog）

> 基于 2026-06-10 的代码现状梳理（branch: test）。按"结构性瓶颈 → 性能 →
> 安全 → 运维"分组，每项标注优先级（P0 最高）与改动成本。完成一项就把
> 状态改成 ✅ 并注明日期/commit；项目概览见 `docs/PROJECT_OVERVIEW.md`。

## 推荐启动顺序

1. ~~**#4 memory 写放大改单行 upsert**~~ —— ✅ 已完成（`a94b9aa8`，选择性 upsert/delete）
2. ~~**#2 enclave 换生产 WSGI 服务器**~~ —— ✅ 已完成（gunicorn gthread；后又整体迁 ASGI）
3. ~~**#1 规划 LISTEN/NOTIFY 替代进程内 waiter**~~ —— ✅ 多 worker 已 ship+deploy
   （2026-07-01，backend `-w4`）
4. 复核记录（2026-07-18）：#1/#2/#4/#6/#14 已完成；#3 **部分**完成——
   runtime-token 路径已本地 HMAC 验证，但 api_key 路径仍回环 whoami
   （07-16 prod 仍见 enclave_http_502 = reentrant whoami 超时，见 CHANGELOG）。

---

## 一、结构性瓶颈（影响扩展上限）

### #1 单 worker 天花板 ✅ 已完成（2026-07-01，multi-worker `-w4` + LISTEN/NOTIFY 唤醒总线）

（下文为历史分析，多 worker 已 ship+deploy，见顶部推荐启动顺序的注记。）

- **现状**：生产是 `gunicorn -w 1 --threads 32`
  （`deploy/docker-compose.phala.yaml:154`）。不是随手写的——进程内
  `UserStore` 缓存和 `threading.Event` 长轮询 waiter 都要求全后端共享
  一个进程。
- **后果**：
  - 32 线程是全部并发预算，而 `/v1/chat/poll`、`/v1/proactive/jobs/poll`
    天然挂线程（30s/个）。活跃用户一多，线程池先被等待者吃光，正常请求
    排队——与已观察到的 prod 慢/502 直接相关（另见 enclave 回环因素 #3）。
  - 永远无法加第二个 worker 或第二台实例。
- **方向**：DB 已是唯一真相，写穿缓存可降级为"读缓存 + 跨进程失效/唤醒
  广播"。用 **Postgres LISTEN/NOTIFY**（不引新组件）替代进程内 Event：
  消息落库时 NOTIFY，各 worker 监听后唤醒本进程 poller、顺带失效缓存。
  打通后 `-w 1` 限制解除。
- **时机**：用户量增长前唯一需要"早做"的结构性工作。

### #2 enclave 跑在 Werkzeug 开发服务器上 ✅ 已完成（2026-06，见 CHANGELOG「enclave 改用 gunicorn gthread」）

- **~~现状~~（已过时）**：曾是 `app.run(threaded=True)`（Flask dev server）。
- **已做**：入口已换成 **gunicorn gthread**（`worker_class="gthread"`，
  `_gunicorn_options`/`_enclave_worker_count` @ `backend/enclave_app.py`；
  `FEEDLING_ENCLAVE_WORKERS` compose 默认 2 × 每 worker 32 线程，保留自签 TLS）。
  → **enclave 早已不是「单线程 Werkzeug」**。真正残余瓶颈是 backend 线程饱和 +
  内存墙，见 2026-07-02 longpoll 并发调查稿（`2026-07-02-backend-longpoll-concurrency-investigation.md`，已删，见 git 历史）。

### #3 enclave→backend 回环鉴权耦合 🔶 部分完成（runtime-token 已本地 HMAC；api_key 路径仍回环）P2 · 中等

- **现状**：每个解密请求回头调 backend `/v1/users/whoami` 验 key，缓存
  只是降频；backend 卡顿时解密路径陪着卡。
- **方向**：backend 签发短期 HMAC/JWT 令牌，enclave 用共享派生密钥
  **本地验证**，解密路径与 backend 可用性解耦。

## 二、性能（便宜的赢面）

### #4 memory 写放大 ✅ 已完成（`a94b9aa8` feat(db): optimize memory_replace_all）

- **结果**：`memory_replace_all` 改为选择性 reconcile——只删被移除的行、
  只 upsert doc 变化的行，单卡编辑不再重写整个 garden（full-replace 语义保留）。

### #5 屏幕帧存 PG JSONB ✅ 已完成（帧正文已迁 R2）

- **结果**：帧正文进对象存储（R2），DB 只存元数据 + `body_key` 指针
  （alembic `0007_frame_body_to_r2` + `backend/object_storage.py`；存量
  由 `backend/backfill_frames_to_r2.py` 离线迁移，迁后 `doc = NULL`）。
  密文模型下对象存储安全（内容本来就是密文）。

### #6 app.py 巨石化 ✅ 已完成（2026-06-12）

- **结果**：17.6K 行单体拆为 14 个领域包（core/accounts/push/screen/
  proactive/identity/memory/bootstrap/chat/tracking/admin/content/hosted/
  mcpsrv），app.py 降至 ~900 行装配层；url_map 零 diff、部署入口零改动。
  详见 CHANGELOG 2026-06-12。
- **遗留**：~~app.py 的迁移期 COMPAT re-export 段待收敛为白名单~~ ——
  **已了结**：`backend/app.py` 已随 ASGI 迁移收尾整体删除（2026-07-06，
  装配层现为 `asgi_app.py` + `asgi/lifespan.py`），COMPAT re-export 段
  随之消失，原 grep 终核命令已失效。

## 三、安全 / 信任链

### #7 api_key 走 URL query 参数 ⬜ P1 · 小

- **现状**：`?key=<api_key>` 会落 ingress 访问日志、客户端历史。代码已
  支持 `Authorization: Bearer`。
- **方向**：skill.md 引导新接入优先用 header；ingress 日志对 query
  string 脱敏；长期把 `?key=` 降为兼容路径。

### #8 链上侧已知欠账 ⬜ P2 · 排期问题

DEPLOYMENTS / AUDIT 已自我披露，列出来是为了排期：

- 合约 owner key 标注"一次性、需轮换"；
- 还在 Sepolia 测试网，主网迁移在路；
- 基础镜像 apt 包未 hash-pin（可复现构建缺口）。

### #9 解密授权粒度 ⬜ P3 · 中等

- **现状**："持有 api_key = 可经 enclave 拿全部明文"，key 泄露即内容泄露。
- **方向**：register 已有 keypair proof-of-possession 基础，延伸到解密
  路径——高敏读操作要求设备私钥签名，把"key 泄露"与"内容泄露"分开。

## 四、运维 / 收尾

### #10 历史孤儿账号恢复 ⬜ P1 · 一次性操作

register 去重已修（2026-06-02），但 prod 28 条孤儿 lineage 若尚未跑
`tools/recover_orphan_accounts.py --apply`，找窗口跑掉（先 `--dry-run`）。

### #11 确认 verify-loop 修复已部署 ⬜ P1 · 核对

verify 回包 gate 竞态等三层修复曾处于"已修未部署"状态，确认当前线上
版本已包含。

### #12 常红测试 ⬜ P2 · 小

依赖可达 enclave attestation 的 `test_model_api…relationship_days` 长期
红，会让人对"全绿"麻木——加环境标记 skip 或 mock。

### #13 user_logs 增长 ⬜ P2 · 核对 + 小改

`db.py` 有 `log_trim`，但需确认 proactive_decisions、perception_events
等高频 stream 都有 trim 调用点，否则慢性膨胀。

### #14 hosted tick 全量 UserStore 饿加载 ✅ 已完成（2026-06-19, dc4138f）

- **来源**：2026-06-11 hosted proactive code review。
- **现状（旧）**：`_hosted_tick_loop` 每 60s 对全体用户调 `get_store` + blob 读，
  所有用户的 UserStore 都会被载入进程内存并定期全量 reload。用户量小时无感，
  用户量增长后内存与 DB 读放大显著。
- **方向**：在 `_users`（或专门的 last_seen_api_key 索引）上加
  **access binding 预过滤**——只对进程内已有缓存且持有 api_key 的托管用户
  创建 tick wake，跳过从未在本次进程生命周期出现过的用户，避免 tick 本身
  成为全量饿加载的驱动者。长期可结合 #1 的 LISTEN/NOTIFY 方向在
  多 worker 场景下协调。
- **修复**：随 multi-worker 改动（dc4138f）一并落地。`_run_hosted_tick_once`
  现仅遍历 `_hosted_keyholder_user_ids()`——只取进程内 `_stores` 缓存中且持有
  `last_seen_api_key` 的用户，未在本进程生命周期出现过的用户根本不进缓存，
  自然被跳过；跨 worker 的 `try_consume_pending_for_user` 也是 cache-only
  查找（`_stores.get`，不加载）。回归测试见
  `tests/test_hosted_wake_distribution.py`。
- **后记（2026-07-25）**：hosted tick / hosted wake driver 这条线后来整体
  退役（2bdcc809 移除），上述回归测试与 `_hosted_keyholder_user_ids` 已
  不在仓内；本条目仅存为历史记录。

### #15 「生产已死、仅测试供养」符号分诊清单 ⬜ P3 · 需领域判断

2026-07-18 清理第五轮扫描产出：backend 有 ~40 个顶层符号在生产侧
（backend+tools+scripts+deploy）零引用、只被 tests/ 引用。**不是**都该删——
分四类，删错会误伤测试基建或在建功能：

- **在建/flag-gated（勿删）**：proactive V2 全家
  （TurnRunnerV2/ToolExecutorV2/DB*StoreV2/InMemory*V2 等 ~13 个，
  `FEEDLING_RUNTIME_V2_DEFAULT_ON` 门控，test enclave 全量开）。
- **测试钩子/播种工具（勿删）**：`enclave/auth.reset_cache`（25 处测试用）、
  `db.insert_user`、`accounts/runtime_auth._secret` 等。
- **确认已死但测试共享**：hosted model_api 退役家族——**测试手术已于第六轮
  完成**（turn.py 传递可达性分析定位整个死半边 20 函数 + context/config_store/
  history_import 尾巴 + `model_api_runtime` 整包及其专属测试，共删 16 个
  死路径测试）。唯一保留：`_patch_model_api_action_trace`——log_trim 的
  防驱逐回归测试（`test_action_trace_trim_preserves_queued_until_patched`）
  经它验证 `_append` 里活着的 queued-保护语义；且 prod 已无人写 queued
  trace，这条保护本身是否已成死语义待人工裁定后再动。
- **已知覆盖缺口（第六轮记录）**：consumer 侧 worldbook 注入
  （`chat_resident_consumer._worldbook_context_for_foreground`）无直接测试——
  被删的两个 worldbook 注入测试测的是死掉的 hosted 装配路径，不算它的覆盖。
- **灰区终审（2026-07-18 第七轮，全部保留）**：16 个逐一核完，git 取证
  （95decf00）确认生产零调用，但用法定性后没有一个该删——
  ① **oracle 型**（测试用它回读/断言活路径行为）：genesis/checkpoint 四件套
  （genesis v2 e2e 靠它们断言 worker 恢复语义）、perception/store 的
  get_photo_envelope（6 个照片存储测试的回读口）与 merge_state、
  memory/migration.is_capped、ios_contract_v2.missing_expected_keys_v2；
  ② **测试缝/播种**：enclave/auth.reset_cache（20 个 fixture 的缓存隔离）、
  storage_crypto.open_、runtime_auth._secret、db.insert_user；
  ③ **在建预留面**：agent_runtime/leases.set_session_ref/list_active
  （07-17 multi-node runner 工作进行中）、agent_protocol_v2.agent_tool_calls_v2
  （V2 栈）；④ 待议：perception/service.set_manual_user_state（单测试，疑
  debug 缝）。
- **hosted_runtime 模块终审（保留）**：生产 import 已归零（coerce_runtime_action
  等 8 函数），但它是 m2_write_loop / memory_action_conformance /
  test_hosted_runtime 三个测试文件的**输入构造器**——这些测试断言的是活的
  memory/actions 语义（如 patch→supersede 守卫）。删模块=重写活语义测试的
  fixture，回归风险大于收益；若未来重写这批测试改用手工 executor_action
  字典，可随手删掉整个模块。"hosted_runtime_state/_action" source 标签
  在 DB 历史行里存在，读侧当不透明字符串处理。

复现扫描：对 backend 顶层符号统计 prod-corpus 与 test-corpus whole-word
引用数，prod≤1 且 test≥1 即候选（`asgi_test_client.py` 计入 test 侧）。

- **2026-07-25 第四轮清理补充**（db.py 专项扫描；全死的
  `try_stamp_hosted_tick`/`genesis_latest_done_job` 已当轮删除）：新增仅
  测试供养、待领域裁定的候选——`db.effect_mark`（effect 生命周期已走
  effect_sink_claim/complete/release）、`db.frame_prune_to`、
  `db.delete_blob`（疑对称 API 刻意保留）、
  `db.list_hosted_runtime_eligible_user_ids`（controls 变体的瘦包装）、
  `tools/chat_resident_consumer._codex_reply_from_stream`（thin wrapper，
  两个测试断言供养）；`hosted/config_store.set_last_runtime_error`
  （第五轮发现：V2 worker/reaper 实际走 jobs_store 直写 SQL，此包装仅
  测试调用，docstring 的幻影调用方声明已当轮修正）。**oracle 判保留**：`db.chat_newest_ts`
  （selfheal_blocked_by_nonempty_page 用它断言 raw-max-ts 一致性；其在
  fail-open 测试里的僵尸 monkeypatch 已当轮修正为 chat_count_since）。
