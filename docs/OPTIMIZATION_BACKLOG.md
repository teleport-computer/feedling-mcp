# 优化清单（技术债 backlog）

> 基于 2026-06-10 的代码现状梳理（branch: test）。按"结构性瓶颈 → 性能 →
> 安全 → 运维"分组，每项标注优先级（P0 最高）与改动成本。完成一项就把
> 状态改成 ✅ 并注明日期/commit；项目概览见 `docs/PROJECT_OVERVIEW.md`。

## 推荐启动顺序

1. **#4 memory 写放大改单行 upsert** —— 一天内完成，立刻见效
2. **#2 enclave 换生产 WSGI 服务器** —— 半天，去掉已知脆弱点
3. **#1 规划 LISTEN/NOTIFY 替代进程内 waiter** —— 结构性，解锁多
   worker，是其余扩展性问题的总开关

---

## 一、结构性瓶颈（影响扩展上限）

### #1 单 worker 天花板 ⬜ P0 · 改动大

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

### #2 enclave 跑在 Werkzeug 开发服务器上 ⬜ P1 · 半天

- **现状**：`backend/enclave_app.py:1395` 是 `app.run(threaded=True)`
  ——Flask 自带 dev server，非生产级 WSGI。
- **缓解已有**：whoami 短 TTL 缓存 + in-flight 合并
  （`enclave_app.py:459-478`）已挡住"history import 触发 N 次回环鉴权"
  的线程风暴。
- **方向**：换 gunicorn gthread（注意保留自签 TLS 配置）。低风险小改动。

### #3 enclave→backend 回环鉴权耦合 ⬜ P2 · 中等

- **现状**：每个解密请求回头调 backend `/v1/users/whoami` 验 key，缓存
  只是降频；backend 卡顿时解密路径陪着卡。
- **方向**：backend 签发短期 HMAC/JWT 令牌，enclave 用共享派生密钥
  **本地验证**，解密路径与 backend 可用性解耦。

## 二、性能（便宜的赢面）

### #4 memory 写放大 ⬜ P1 · 一天内 —— 建议第一个修

- **现状**：`backend/db.py:792` `memory_replace_all`——每加/改/归档
  **一张**记忆卡，DELETE 该用户全部行再逐行重插，且在 `memory_lock` 内。
  老用户 floors 87 张起步 → 写一张卡重写近百行。
- **方向**：schema 已有 `(user_id, moment_id)` 主键 + `ON CONFLICT`
  支持，改单行 upsert/delete 即可。改动小、收益明确。

### #5 屏幕帧存 PG JSONB ⬜ P2 · 中期

- **现状**：`frame_envelopes` 单行可 >150KB，走 TOAST，DB 膨胀快、备份重。
- **方向**：密文模型下对象存储是安全的（内容本来就是密文），DB 只存
  元数据 + 指针。

### #6 app.py 巨石化 ✅ 已完成（2026-06-12）

- **结果**：17.6K 行单体拆为 14 个领域包（core/accounts/push/screen/
  proactive/identity/memory/bootstrap/chat/tracking/admin/content/hosted/
  mcpsrv），app.py 降至 ~900 行装配层；url_map 零 diff、部署入口零改动。
  详见 CHANGELOG 2026-06-12。
- **遗留**：app.py 的迁移期 COMPAT re-export 段（含 hosted 兜底回灌循环）
  待收敛为白名单（`app`、`get_store`、`UserStore`、`require_user`、`_users`、
  `_key_to_user`、`_stores`、`_save_users`、`_register_user`、`db`）——
  独立小 PR，删除前 `grep -rn "appmod\.|app\._" tests/ tools/ backend/` 终核。

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

### #14 hosted tick 全量 UserStore 饿加载 ⬜ P2 · 中期

- **来源**：2026-06-11 hosted proactive code review。
- **现状**：`_hosted_tick_loop` 每 60s 对全体用户调 `get_store` + blob 读，
  所有用户的 UserStore 都会被载入进程内存并定期全量 reload。用户量小时无感，
  用户量增长后内存与 DB 读放大显著。
- **方向**：在 `_users`（或专门的 last_seen_api_key 索引）上加
  **access binding 预过滤**——只对进程内已有缓存且持有 api_key 的托管用户
  创建 tick wake，跳过从未在本次进程生命周期出现过的用户，避免 tick 本身
  成为全量饿加载的驱动者。长期可结合 #1 的 LISTEN/NOTIFY 方向在
  多 worker 场景下协调。
