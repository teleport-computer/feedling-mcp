# prod `users` 全表重载风暴 —— 真因是常驻心跳的无差别广播

**日期**：调查 2026-07-24，定性+修复 2026-07-25
**环境**：prod（`feedling-enclave-v2` 主 CVM + `feedling-prod-runner-1`）
**优先级**：非 P0。不影响正确性、不涨内存、无用户可见故障；纯资源浪费，但随
**在线常驻用户数**线性放大。
**状态**：已修（本仓，未部署）。

---

## 一句话结论

`/v1/chat/poll` 的常驻存活心跳每 60s 给**每个在线常驻用户**重写一次
`access_bindings[resident].last_seen_at`，而它走的 `registry.persist_user()`
发的是**不带 user_id 的 `users` 广播** —— 每个订阅进程收到后无条件
`load_users()` **全表重载 667 行**。于是"每分钟 N 次单行写"被放大成
"每分钟 N × 进程数 × 667 行读"。

---

## 实测（prod，45s 窗口）

```
users NOTIFY:        29 次 / 45s  = 38.7 次/分钟，全部来自同一个 worker id，payload u=""
users 行变化:        27 行 / 45s  = 36.0 行/分钟
变化字段:            access_bindings × 27（100%）
canonical 语义比较:  semantic 27 / pure-ordering 0
变化的标量叶子:      access_bindings[resident].last_seen_at × 27
                     access_bindings[resident].updated_at   × 27
```

样例（`usr_56e0f55ad93358c1`，仅 resident binding 的两个时间戳变化）：

```
updated_at/last_seen_at: 2026-07-24T15:45:24.442594 → 2026-07-24T15:46:06.865026
```

## ⚠️ 更正前一版交接文档的误诊

`scratchpad/HANDOFF_users_reload_storm_2026-07-24.md` 的结论是
**"access_bindings 纯 JSON key/list 顺序抖动、零标量变化、被 persist_user 误判为
真改动"**，并据此建议给 `persist_user` 加 canonical noop 短路。

**这个结论是错的**，其"深度 diff 显示零标量变化 / canonical 比较
semantically_changed=0"与复测结果直接矛盾：复测 27/27 行都是**真实的时间戳标量
变化**，pure-ordering 行数为 **0**。因此 noop 短路**修不了这个风暴** —— 心跳写
的是真变化，任何语义 noop 检测都不会短路它。

真正的缺陷不在"是否该写"，而在"写完该通知谁重载多少"。

## 根因

- `persist_user()` 是**单行 upsert**，配的却是**全表重载广播**
  （`wake_bus.notify("users")`，无 user_id）。
- `wake_bus` 的 `users` handler（`asgi/lifespan.py` 与 V2
  `serve_worker.py` 各一份）忽略 payload 里的 user_id，一律 `load_users()`
  —— 全表 SELECT + `_normalize_all_users_cas()` 遍历 667 行。
- 该设计对**稀疏**的注册/发钥/改公钥/改偏好是合理的；`_touch_resident_binding_seen`
  这个**频率 ∝ 在线常驻用户数**的存活心跳走同一条路，就把它压垮了。

V2 `serve-worker` 只是把既有 backend bug **暴露**出来（它裸 `python -u` 会打印
`[users] loaded 667 user(s)`，backend 在 gunicorn 下这行被吞了），不是起因。

## 修复

定向广播 + 定向重载，保留跨进程新鲜度（admin `data-track` 的
"在线/掉线"判据、whoami 的 `last_seen_at` 都读内存 registry，不能靠"干脆不广播"
来省事，否则 6h 陈旧阈值会误报掉线）。

- `db.load_user(user_id)` —— 单行读；DB 错误**故意上抛**，让调用方能区分
  "行被删了"和"读失败"。
- `registry.reload_user(user_id)` —— 原地刷新一行 + 重建 key cache；读失败时
  **保留现有行**（把瞬时错误当成"已删除"会把活用户从内存里踢掉 → 该 key 全站
  401，即 `asgi-lifespan-missing-load-users` 那类事故）。不做 normalize-CAS：
  写方已经 normalize 过，这里 normalize 而不落库会把进程本地生成的 id 端出去。
- `registry.reload_users_after_notify(user_id)` —— 带 user_id 走单行，不带的
  仍走全表。两个 handler（`asgi/lifespan.py`、V2 `serve_worker.py`）都接上。
- `persist_user(entry, *, targeted_broadcast=False)` —— 心跳传 `True`；其余
  调用点（注册/发钥/公钥/偏好/access flip）保持全表语义不变。

效果：38.7 次/分钟 × 667 行 × ~7 进程 ≈ **26k 行读/分钟 → ~250 行读/分钟**。

### 收窄必须补回的三件事（code review 抓出，均已修）

第一版收窄丢掉了 `load_users` 原本顺带提供的三个性质，逐条补回：

1. **读取与安装的原子性**。第一版为了不持锁做 I/O，把 `db.load_user()` 放在
   `_users_lock` **外**、装载放在锁内。监听线程拿到旧快照后阻塞在锁上，等请求
   线程提交完编辑再把旧快照装回内存；之后任何一次 `persist_user` 整文档 upsert
   就会把刚签发的 key 永久写没。现在读也在锁内（单行 I/O，比原先锁内读 667 行
   还便宜）。
2. **归一化**。`load_users` 每次都跑 `_normalize_all_users_cas`（归一化 **并**
   CAS 落库），而 admin `data_track` 直接原样快照 `_users` 并注释说明它依赖这一点。
   新增 `_normalize_row_cas_locked()` 做单行版，契约与全量版一致（CAS 失败则端出
   读到的原行，绝不端进程本地生成的 id）。
3. **周期性自愈**。那 38.7 次/分钟的无差别广播，事实上兼任了每 ~1.5s 一次的全量
   修复通道（丢 NOTIFY、handler 抛异常被 `wake_bus._dispatch` 吞掉、`_key_to_user`
   里残留已吊销 key）。收窄后这条通道消失且无替代。新增
   `start_periodic_full_reload()`：每进程一个 daemon 线程，默认 60s
   （`FEEDLING_REGISTRY_FULL_RELOAD_SEC`；下方第二轮修订⑤把初版的 300s 调到 60s）
   跑一次 `load_users()`——全舰队 ~7 次/分钟，对比原先的 ~271 次/分钟。它与 wake-bus
   的 `users` handler **配对无条件启动**（每个注册该 handler、靠 registry 鉴权的
   进程都需要自愈），不 gate 在 `start_background`（下方第二轮修订，[[incident]]）。
   ⚠️ `load_users` 必须能区分"读失败"与"真空表"：`db.load_all_users(raise_on_error
   =True)` 让读失败上抛（保留快照，避免清空 `_users` 全站 401），真空表则正常清空
   （下方第二轮修订②把初版的 `guard_empty` 行数启发式换成 `raise_on_error`）。

另外把一处**注释与行为不符**改掉（CONTRIBUTING §8）：原注释说心跳"只碰该用户的
resident binding"，实际上 `persist_user` 一如既往 upsert 整个文档；targeted 描述的
是**广播范围**，不是写范围。整文档 upsert 可能被陈旧快照覆盖是 `persist_user`
的既有性质，不是本次引入的。

以及一处**收窄没收干净**：单行重载仍在调全量 `_rebuild_key_cache()`（清空 +
667 行重建）。`_resolve_user` 无锁读 `_key_to_user`，每个 clear/rebuild 窗口都会让
有效 key miss、继而取 `_users_lock` 跑 deepcopy 慢路径——正是这个修复想消除的锁
convoy，只是换了个触发点。现在改为 `_reindex_user_keys_locked()` 只动该用户的
hash，全量重建与单行重排共用 `_index_user_keys_locked()` 以免两者对"什么算活 key"
的判断漂移。

## 验证

- L1：`tests/test_users_reload_targeted.py`（7 条）+
  `tests/test_users_reload_review_fixes.py`（21 条，覆盖各轮 review 的每条修复：
  锁内读、单行 normalize+CAS、周期自愈、DB 错误保留 vs 真空清空、心跳 CAS 不
  覆盖他 worker 的 key、CAS 输重试、跨用户 hash 不串号、env 守卫、线程幂等）+ 全量
  `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
  = 6263 passed，3 条既有失败与干净 HEAD 基线逐条一致。
- 部署后线上复验：重跑 LISTEN 抓包（`LISTEN feedling_wake`，统计 payload
  `c=="users"`），应看到 **users 事件仍在（携带 user_id），但各进程日志里
  `[users] loaded N user(s)` 的频率掉到接近 0**。
  ⚠️ 判据是"全表重载次数"，不是"users NOTIFY 次数" —— 定向广播本身仍然会发。

### 第二轮 review 的修复（收窄的副作用，均已修）

第一轮修复把风暴收窄成定向重载，但它同时移除了那场风暴顺带提供的自愈通道，且
新代码本身引入了几个问题。第二轮 review 抓出 10 条，逐条处理：

1. **心跳整文档 upsert + 陈旧快照（最严重，CONFIRMED 数据丢失）**。心跳仍用
   `persist_user` 整文档 upsert 本进程快照，而快照最差陈旧度从 ~1.5s 涨到自愈
   间隔。一次丢失的 notify 就能让心跳把别的 worker 刚签发的 API key 覆盖掉、永久
   丢失。**这是我上一轮明确判断错的地方**——上一轮注释说"整文档 upsert 被陈旧
   快照覆盖是既有性质，不是本次引入"，事实对但结论错：那个既有隐患一直没炸，正是
   因为 1.5s 全量重载让快照根本陈旧不了；我拆了这个约束又把替代品定到 300s，等于
   把隐患放大 200 倍。修法：心跳改成对**新鲜 DB 行**做 CAS（`compare_and_set_user`，
   由 `normalize_user_cas` 重构共用），只改 resident binding 的两个时间戳，绝不
   回写其他字段；CAS 输给并发写则同步赢家到内存后重试。DB I/O 移出 `_users_lock`，
   不再 convoy 全局锁。
2. **`guard_empty` 只加在周期路径（CONFIRMED）**。非定向 notify / wake-bus 重连
   重放走裸 `load_users()`，重连期间的 DB 错误仍会清空 `_users`。且 `guard_empty`
   的行数启发式还有个反作用：让周期重载永远无法把 registry 收缩到 0（一次
   `TRUNCATE users` 永不被感知，CONFIRMED）。改法：删掉 `guard_empty`，给
   `db.load_all_users(raise_on_error=True)`，`load_users` 用它区分"读失败"（保留
   快照）与"真空表"（正确清空）。所有调用者一致受保护。
3. **env 裸 `float()` import 期崩（CONFIRMED，全站级）**。`FEEDLING_REGISTRY_
   FULL_RELOAD_SEC=5m` 会让 `import accounts.registry` 抛 ValueError、gunicorn
   开机即死、健康探针都起不来。加 `_env_float` 守卫，坏值降级默认。
4. **daemon 线程污染测试（CONFIRMED）**。`start_periodic_full_reload` 塞在
   `wire_assembly` 里（13 处测试调用），线程会在跑到一半时改写 `registry._users`。
   移到运行时入口：backend 侧放进 `_start_wake_bus`（与 `users` handler 配对、无
   条件——见下方第三轮修订①对 gate 的更正），V2 侧移到 `serve_worker.main`
   （`wire_assembly` 保持 inert）。测试不受污染：ASGITransport 不跑 lifespan，唯一
   跑 lifespan 的 `test_asgi_lifespan_loads_users` 又 stub 掉了 `_start_wake_bus`。
5. **自愈间隔 300s 过长（PLAUSIBLE）**。默认从 300s 调到 60s——够快兜底丢失的
   revoke/delete notify，且每进程每 60s 一次全表，全舰队 ~7 次/分钟，远低于原先
   271 次/分钟。
6. **reindex 先删后加窗口（CONFIRMED）**。`_reindex_user_keys_locked` 先删该用户
   的 hash 再加回，`_resolve_user` 无锁读会在窗口内对有效 key miss、跌进 deepcopy
   慢路径。改成**先加后删**（有效 hash 从不消失）。
7. **跨用户共享 hash 串号（PLAUSIBLE，安全）**。若同一 api_key_hash 出现在两行
   （孤儿/合并残留），定向 reindex 会把它从当前 owner 抢给被重载的用户。改成
   **不抢别的用户的 hash**（冲突留给周期全量按确定性顺序解决）。全量 rebuild 与
   单行 reindex 共用 `_live_key_hashes` 判 liveness。
8. **sort 对非 dict 抛错（PLAUSIBLE）**。插入路径的 `_users.sort()` 用
   `(e or {}).get(...)` 对非 dict 元素抛 AttributeError、中断 reload。抽
   `_install_user_row_locked` 时直接去掉 sort（append 到末尾，与
   `_resolve_user_via_db` 先例一致，DB 读序本非硬不变量）。
9. **测试 monkeypatch stdlib threading（PLAUSIBLE）**。改成 patch registry 本地的
   `_spawn_full_reload_thread` 工厂，不碰进程级 `threading.Thread`。

### 第三轮 review 的修复

1. **周期自愈 gate 错了（PLAUSIBLE，鉴权回归）**。第二轮把
   `start_periodic_full_reload` gate 在 `settings.start_background`（默认 False）后，
   但 `users` handler 是**无条件**注册的、且现在只做定向重载。一个
   `FEEDLING_ASGI_BACKGROUND` 未设的 web worker 照样鉴权，却没有任何自愈通道——
   丢一条 delete/revoke notify 就会让已删/已吊销账号在该 worker 上一直鉴权通过。
   改法：周期自愈移进 `_start_wake_bus`、与 handler 配对**无条件**启动（它是
   per-worker 自愈、非 leader 选主，不该跟后台单例共用开关）。
2. **`persist_user` 的 `targeted_broadcast` 是死参数（PLAUSIBLE，footgun）**。心跳
   改 CAS 后没有任何 caller 传它，且是隐患：`reload_user` 只在 INSERT 清负缓存、
   假定发钥/吊销都走非定向广播，若日后有人把发钥动作接到这个死参数上，新钥会被负
   缓存钉住 401。直接删掉参数，`persist_user` 恒定发非定向广播。
3. 两处文档不一致（CONFIRMED）：CHANGELOG 说 16 条测试实为 15；incident 正文的自愈
   段仍写着初版的 300s / `guard_empty`，与第二轮修订自相矛盾——都已更正为最终值。

### 第四轮 review 的修复

1. **turn_child 不起周期自愈（CONFIRMED，用户可见故障）**。第三轮把自愈 daemon 移进
   各运行时入口，但漏了 `turn_child.main`——它经 `wire_assembly` 注册了 `users`
   handler + envelope 公钥 getter、是长命 spawn 子进程，却走自己的入口。少了它，一条
   丢失的 `users` notify 在该子进程永不自愈：用户轮换内容公钥后，turn_child 会一直用
   陈旧公钥封装托管回复 → decrypt-failed，直到子进程被杀。修：`turn_child.main` 补上
   `start_periodic_full_reload()`（对称 `serve_worker.main`）；`start_periodic_full_reload`
   的 docstring 现在列全三个运行时入口（lifespan / serve_worker.main / turn_child.main）
   以防再漏；加回归测试 `test_turn_child_main_starts_the_periodic_full_reload`。
2. **TEE 影子库不再收敛（PLAUSIBLE）**。心跳从 `upsert_user`（无条件 mirror）改
   `compare_and_set_user`（mirror 是条件 CAS UPDATE `WHERE doc=expected`）后，若影子库
   该行已 drift，mirror 匹配 0 行静默 no-op，影子行 stale 到 24h reconcile。修：
   `compare_and_set_user` 的 mirror 改成**无条件 upsert**（`INSERT … ON CONFLICT DO
   UPDATE`，与 `upsert_user` 的 mirror 同款），主库 CAS 成功后无条件把影子行收敛到
   新值。`normalize_user_cas` 路径一并受益。
3. **reindex 删除步扫全 `_key_to_user`（CONFIRMED，性能）**。删除步 `[h for h,uid in
   _key_to_user.items() if uid==user_id …]` 是 O(全舰队 key 数)，抵消了定向重载"每次
   只做一个用户的活"的初衷。`_install_user_row_locked` 本就持有被替换的旧行，改成从旧行
   的 hash 集合算删除集（O(该用户 key 数)）；周期 `_rebuild_key_cache` 仍是兜底，纠正
   任何 mis-diff 残留。
4. **CAS-loss 重试重复读库（CONFIRMED，性能）**。重试轮已持有 `compare_and_set_user`
   返回的赢家行，却又 `db.load_user` 再读一次。改成复用 `authoritative`（deepcopy 隔离，
   避免下轮编辑改到已装进 `_users`、被 `_resolve_user` 无锁读的那行）。竞争路径从
   2 读 + 2 CAS 降到 1 读 + 2 CAS。
5. **revoked/deleted key 鉴权窗口 1.5s→60s（PLAUSIBLE，已文档化权衡）**。丢了 delete/
   reset notify 的 worker，其 `_key_to_user` 里的已删 key 现在最长 60s（原风暴 ~1.5s）
   才被周期自愈纠正。被周期自愈 + wake-bus 重连 catch-up 双重 bounded；是收窄换来的
   已知代价，非缺陷。

**保留不改（第三轮 [1]，已知权衡）**：`_reindex_user_keys_locked` 对"已被别人拥有
的 hash"跳过不抢，代价是孤儿/合并残留的共享 hash 最长滞留一个自愈周期（~60s）才被
周期全量按 DB 顺序解析。这是第二轮 #7 的正面选择的另一面：抢 → 可能即时跨用户串号
（安全问题）；不抢 → 数据损坏时短暂滞留（可用性问题）。api_key_hash 是随机唯一的，
key 不在用户间正常转移，只有孤儿/合并残留才产生共享 hash，故选安全优先；周期已从
300s 缩到 60s 进一步收窄滞留窗口。

## 遗留

- `persist_user` 其余调用点仍是全表广播。它们稀疏，不构成风暴；若以后要统一，
  可给心跳那种行受限的高频写单独走定向路径，但需先逐个确认没有跨行副作用。
- **`last_seen_at` 仍存在账号文档里**，所以这次只把扇出缩小了 667 倍、没有消除
  它——成本依然按（在线常驻用户数 × 进程数）增长，下一个量级台阶会重开同一张单。
  彻底的修法是把常驻存活挪出 registry 文档（自己的小行/列，由
  `admin/data_track.py` 与 whoami 直接读），心跳就完全不走 users 广播了。属架构级
  改动，本次未做。
- `db.load_all_users()` 失败时吞异常返回 `[]` 的老隐患仍在函数本身；这次只在
  `load_users(guard_empty=True)` 这条周期路径上挡住了它（读回 0 行且内存非空 →
  保留快照）。启动路径仍是无守卫的原语义（空表就是空表）。
