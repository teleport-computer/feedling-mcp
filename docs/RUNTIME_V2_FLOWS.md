# Runtime V2 全流程报告 —— 每个触发、上报、唤醒、做梦、注入的完整过程

> 2026-08-05,V1-parity 总攻交付物 #1(Seven 委托)。基于 test 分支
> (含志豪 PR #158、persona/attention_facts/Batch-2 工具面 parity)。
> 姊妹文档:docs/RUNTIME_V2_PARITY.md(债务台账)、
> docs/superpowers/specs/2026-08-04-runtime-v2-user-feedback-remediation-design.md(志豪根因设计)。
> 行号为写作时快照,漂移以符号名检索为准。

---

## 0. 总览:V2 是什么形状

**V1(hosted resident)**:每用户一个长活 CLI agent 进程(claude/pi driver),
supervisor 拉起(backend/agent_runtime/supervisor.py),人设 MD 全文在 spawn 时
预埋 system prompt(spawners.py:824-890),能力经 io_cli(全部是 HTTP 壳)。

**V2(serve_worker)**:单个 worker 进程服务所有用户,in-process 调 provider
chat API。核心文件:
- `backend/model_api_runtime/v2/serve_worker.py` — 服务装配、调度 tick、效果落库
- `backend/model_api_runtime/v2/worker.py` — turn 编排(process_job)、各 lane 处理器
- `backend/model_api_runtime/v2/context.py` — prompt 组装(build_turn_messages)
- `backend/model_api_runtime/v2/tool_loop.py` — 统一工具循环(所有 provider 一个 loop)
- `backend/model_api_runtime/v2/jobs_store.py` — v2_jobs 队列 + v2_wake_schedule
- `backend/capabilities/` — 模型工具面(tool_schema.py 定义,capabilities/*.py 实现)

**Job 模型**:一切皆 job。lane ∈ {chat, heartbeat, scheduled, manual_wake,
screen_watch, capture, dream, maintenance(compaction), profile}。
`_slot_loop` 认领(worker.py:12328 一带)→ `_run_turn` → `process_job`
按 lane 分发(worker.py:9901-9962)。并发:`FEEDLING_V2_MAX_WORKERS=4`,
enclave 解密信号量 2。

**效果模型**:模型的写操作不直接落库——先进 effect outbox(加密),终局事务
`apply_pending_effects` 统一提交;主动消息在此处过撞车门(见 §2)。

---

## 1. 用户发消息 → chat turn

**触发**:iOS 发 `/v1/chat` → 消息落 `chat_messages` → `enqueue_job(user, "chat")`。

**判断链**(process_job chat 路径,worker.py:9737 起):
1. 认领 + 单飞解密 BYOK provider 配置,铸 enclave HMAC token(worker.py:11988-12002);
2. 合并积压输入(coalesce,worker.py:10098);
3. 装载 workspace prompt 上下文 `_load_workspace_prompt_context`(worker.py:10147):
   **genesis 人设全文**(每 turn JIT 解密 `genesis_persona.content_envelope`,
   serve_worker.py:_load_genesis_persona,95bbd545)+ `/skills/*` 政策文档;
4. 读 metadata-only 的确定性历史 coverage + 原文尾巴
   (read_summary_with_seq / read_tail_after_seq;coverage 只证明已覆盖消息数，
   不含语义摘要；tail 超预算时丢最旧并注入 coverage_hole 声明);
5. 画像选择 `_select_profile_prompt_for_turn`(worker.py:5313):PROFILE 开关开 →
   取 `v2_agent_profile` 的 MEMORY/STYLE 双字段（旧 `USER` 仅读侧兜底）;
6. 时间快照 build_temporal_context(worker.py:10377);
7. 组 prompt(见下)→ 校验 token 预算(超限硬失败,不静默降级)→ 进工具循环。

**Prompt 组装次序**(context.py:build_turn_messages,:466 起):

| # | 块 | 角色 | 上限/备注 |
|---|---|---|---|
| 1 | CHAT_SYSTEM_PROMPT(~1400字)+ 运行时政策(~2500字) | system | 字节恒定,缓存前缀 |
| 2 | **genesis 人设全文** + `/skills/*` | system(trusted blocks) | 无截断,不走 profile 开关 |
| 3 | **MEMORY(长期记忆精粹)+ STYLE(相处方式)** | user(application data) | PROFILE=1 时在场;注入抬头仍是 HOW YOU TWO GET ALONG |
| 4 | 确定性历史 coverage(compaction 产物) | user | 仅含覆盖计数，不含对话语义 |
| 5 | 原文 tail | user | ≤60 行;图≤2/文件≤2 |
| 6 | coverage hole 声明(如有丢行) | user | ~200字 |
| 7 | temporal context JSON(本地时间/时间戳;wake 时含 attention_facts) | user | ~1000字 |
| 8 | runtime context JSON(感知 glance/日程/action 数据) | user | 8000字封顶,超丢最旧+_truncated 标 |

**工具循环**(tool_loop.run_tool_loop):每 turn ≤6 次 LLM 调用(带文件 10),
每轮 ≤8 个工具、全 turn ≤24;结果每条 2000 字符/每轮 8000 字符水位分配,
超打 `...[truncated]`(memory 类附 total/returned);最后一轮禁工具逼出终文。
`reply` 工具产生的中间气泡即时外流(worker.py:10158)。

**工具面**(tool_schema.py,Batch-2 后):identity_get/patch/nudge、
memory_index(limit/bucket/thread/ambient/include_sensitive)、memory_search、
memory_fetch(ids/limit/include_archived/include_superseded)、
memory_write(add/update/delete + reason 审计)、memory_organize、
perception_snapshot/**recent_apps**/trend/history、screen_recent/read、
photo_recent/read(include_image 走 native vision observer,返回
visual_observation 而非本地文件)、web_search/fetch、workspace_list/read/write/
delete、schedule_wake(ISO 或相对时间)/cancel_wake、send_file、task(子agent)、
reply、provider_usage(仅 chat)。已知 V2 limitation:chat_image_read 不在
模型工具面(历史聊天图靠 tail 原生多模态注入;超出 tail 窗的旧图不可回看,
P2 提案=复用 vision-observer 模式)。

**安全 fence**:读过带文本的媒体(screen/photo/recent_apps)后,本 turn 禁
后续外发(web/MCP/子agent)——防注入外泄。

**副作用**:回信文本 + 工具写效果全部经加密 effect outbox 事务提交;完成后
capture 调度器记账(见 §7)。

---

## 2. 心跳唤醒(heartbeat)

**触发**:`v2_wake_schedule.next_heartbeat_at` 到期(每用户一个标量),
调度 tick 轮询 `due_heartbeat_users()`(jobs_store.py:10390-10414)→
enqueue heartbeat job → 完成后推进 `next_heartbeat_at = now + wake_interval_sec`
(用户 App 设置,默认 7200s=2h,范围 [900, 43200])。

**准入判断**(全部在 due 查询/enqueue 前,d8b33a26 后):
- `proactive_settings.dnd=true` → 不醒(**勿扰闸**,heartbeat+screen_watch 同);
- `payment_cooldown_until` 未过(BYOK 402/401/403 → 600s 冷却)→ 不醒;
- `proactive_backoff_until` 未过(失败指数退避 60s→3600s)→ 不醒;
- **self-loop guard**:无用户输入连续接受 3 个 AI 自唤醒,第 4 个 suppress
  (jobs_store.reserve_self_wake,原子化,FEEDLING_MAX_CONSECUTIVE_SELF_WAKES=3;
  heartbeat/screen/事件唤醒不消耗计数);
- 无真实用户历史 → 直接静默完成,不调模型(worker.py:7020-7026)。

**上下文**(worker.py:_run_wake,:6870 起):摘要 + wake tail(≤16 turns)+
人设(同 chat)+ perception_glance 预取 + **attention_facts**(a53a2923):
`last_message_age_sec / last_user_message_age_sec / last_visible_proactive_age_sec
/ tail_freshness / tail_included_messages / visible_proactive_count_24h`
(计数跨 V1/V2 口径,db.chat_visible_proactive_stats)。以 temporal 数据块注入,
**不是伪 user 消息**(志豪 6495378f 移除了 _WAKE_NUDGE)。

**Prompt 语义**(worker.py:855-862,V1 对齐版):"说与沉默同等有效,都不是
默认项,不需要强理由;按你自己的性格、真实对话与当下决定;用 attention_facts
避免打断或重复出现;**永不向用户提及本次唤醒或任何系统措辞**"。

**出口**:说话 → 效果 outbox,发布事务内过 `WAKE_REPLY_CHAT_COLLISION`
撞车门(±90s 内有真实聊天则丢弃,effect_outbox.py:87-122);沉默 → 空回复
即成功(“weak wake sleeps”)。

## 3. 定时提醒(scheduled)

**触发**:模型此前调 `schedule_wake`(支持 "in 2 hours"/"+30m"/"两小时后")
→ scheduled_wake_v2 落库 → 到点 enqueue "scheduled" job。
**语义**:必须送达——prompt 明令"deliver every supplied reminder now,不许
沉默、不许寒暄替代"(worker.py:868-872);一次最多批 10 条最早提醒
(worker.py:7252-7289)。撞车门**不**适用(用户授权的送达优先);DND 下
提醒投递由 controls_v2 单独把关。模型/provider 给空结果时,tool loop 用
scheduled 专用纠正语再试一次;仍失败时,job 终态与 terminal-failure outbox
在同一事务落下,重放器写一条独立的加密失败消息(含原提醒 note + 安全错误说明)。
该消息不挂到最近 user turn、不推进 reply cursor,且以 job id 幂等。取消:
cancel_wake(wake_id)。

## 4. manual_wake

App/运营手动触发 → enqueue "manual_wake"。上下文与 prompt 同 heartbeat
(共用 _WAKE_SYSTEM_PROMPT 与 attention_facts),过撞车门与 self-loop guard。

## 5. screen_watch(屏幕共享陪看)

**触发**:用户开启屏幕共享 → `next_screen_watch_at` 节奏轮询
(due_screen_watch_users,jobs_store.py:10417+,DND 同闸)。
**上下文**:screen_recent 帧元数据预取(worker.py:7365-7377);需要像素时
模型调 screen_read(include_image → native vision observer → untrusted
visual_observation)。
**Prompt**(worker.py:878-887):说/不说同等有效;开口选一个连贯念头,
不播报屏幕状态;**永不叙述"我在看你屏幕/我看了帧"**,不提系统措辞。

## 6. 感知上报链 + 感知事件唤醒

**链路**(志豪 PR #158 重造):
```
iOS 上报信号 → perception/ingress_v2.observe_signal_v2 (:55-89)
  → signal_state_v2.observe_signal_state (:140-347)   ← 落库 baseline(按 user+signal)
      判定(原子):baseline_created│duplicate│stale│unchanged│changed│error
  → 仅 changed → differ_v2 事件(arrived_at_anchor / unlock_after_absence / scene_change)
  → enqueue 唤醒 job
```
**关键不变量**:首次观察只建基线**绝不唤醒**(除显式 allow_first_event,如
unlock_after_absence);同 event_id 去重;时间倒流判 stale;跨 worker/重启
判定一致(修掉了"没移动却收到『到家』"的多进程内存态根因)。
事件覆盖:connectivity/wifi/bluetooth 锚点、解锁、screen_phash、photo_added
(differ_v2.py:43-50)。

**喂给模型的形态**:runtime_data 里的 perception_glance(十域:location/
media/app/health/weather/mood/reminders/calendar/photos/screen)——只是
"要不要细看"的提示,prompt 明令不当 checklist 播报;要精确读数用
perception_* 工具(app 域注意:snapshot 只有 15 分钟内最近事件,轨迹问题
用 perception_recent_apps)。

## 7. capture(主动总结写卡)

**触发**(backend/proactive/capture_scheduler.py:46-56):三条件任一——
安静 1200s、24 turns 兜底、距上次 ≥600s 防抖;载体=调度 tick(30s)+
**设备事件直触**(device_events_append → handle_device_event,V1 同源,
已专项验证 7 passed)。
**闸**:`FEEDLING_V2_CAPTURE_ENABLED`(test 硬编码 1,serve-worker 读)+
每用户 `proactive_settings.capture_enabled`(默认 true)+ 失败退避。
**执行**(worker.py:9004-9136):取 capture 窗口(≤60 条,过滤后的原文,
**不是一行摘要**)+ 现有卡索引 → 共享 prompt `memory/capture_prompt_v1.py`
(V1/V2 同一份)→ parse_capture_cards(严格→宽松重试)→ 走
`backend/memory/actions.py` 统一校验栈(title/description 必填、card_guard
防污染、摘要≤2000/正文≤5000、活卡查重)→ 效果落库。

## 8. dream(做梦整理)+ memory_organize

**触发**:dream_scheduler tick(backend/proactive/dream_scheduler.py)——
夜间窗(night_only)+ 距上次 ≥23h + 新卡 ≥3(env 可覆盖);签名用**非 dream
种子卡**(自产卡不进签名,杜绝自反馈循环);或用户在聊天里要求整理 →
`memory_organize` 工具(force 语义,双层开关 fail-closed,关闭态返回
"整理功能暂时关闭")。
**执行**(重设计 97d4adce;2026-08-05 阀门重构=**出口只拦「明显不对」,
不判内容质量**,usr_a40e 墓碑卡复盘,Seven 拍板):
1. 两段式全文 fetch(60k 预算)——模型看到卡**全文**;
2. 提案必须带 rationale;目标卡必须真实存在、不能被两条提案重复退休;
3. **确定性出口闸**(memory/dream_gates.py + card_text.py,V1/V2 共享一份):
   卡 id 泄漏(result 硬字段含花园真实卡 id → 与内容闸同路打回重问)、
   墓碑短语(「已被+hex」/「superseded by+hex」)、爆炸半径保险丝
   (单晚退休 > 活跃卡 80% 且 ≥10 张 → 整个 job 失败,不部分执行);
   **旧的逐提案语义审查员与 15% 增量栅栏已拆**——弱模型自审自查既误放也误杀,
   还每条提案多烧一次 BYOK;内容对不对交还模型自主;
4. 同 run 内不碰自产卡;>20 动作分批;无数量闸、无文本相似闸(Seven 拍板撤除);
5. supersede=软退休(status/superseded_by/is_archived,信封保留可恢复,
   actions.py:833-853);delete=硬删(不可恢复)。
**验收基线**:四类 live E2E(重复对✓合/演进对✓合/相关不同✗/无关✗ +
round2 收敛 0 动作),最弱真实模型过全绿才算数。

## 9. profile(STYLE/MEMORIES 画像)

**触发**:PROFILE 开→ 切入 V2 时 backend 触发；成功 Dream 仍 force 刷新；
每轮 Chat 后只做 freshness 检查：成功画像 7 天内不重建，超过 7 天也只有
Garden 的 row-count/max-updated-at witness 改变才入队。成功更新或切换 provider
会 best-effort 唤醒画像，普通 post-Chat coalesce 不会提前 delayed retry。
**产物**:`v2_agent_profile`(state=ok/degraded)双字段——MEMORY(长期
记忆精粹)、STYLE(交互方式),由 provider 蒸馏(注意:蒸馏时花园内容明文
到 provider,与聊天同信任面)。
2026-08-16 起新蒸馏和存储统一写 `STYLE`;旧 `USER` 仅在读侧兼容，等待下一轮
成功重蒸馏自然迁移。MEMORY=事实、STYLE=方式的分界没有改变。
**失败恢复**:provider transient 使用同一个 Job 持久延迟重试，5 分钟指数退避、
6 小时封顶；模型输出 shape 最多跨 Job 延迟重试 3 次。provider 配置错误等待
显式配置修复，Garden source/data 错误等待 source witness 改变，未知内部错误终止，
都不会靠下一轮 Chat 才恢复。delayed Job 不占 worker slot、不计 watchdog claimable；
Dream force 或成功 provider 修复可把已有 delayed Job 调为 ready-now。
**注入**:context.py:两字段合并成一条 application-data 消息;MEMORY 以第一人称
记忆呈现，STYLE 沿用 `HOW YOU TWO GET ALONG` 抬头，冲突以 tail 原文为准。
**准入闸**(志豪):自动切 V2 要求 profile state=ok 且 memory/style 非空,
否则留 resident,不半切换。
**回滚铁律**:PROFILE 可独立关闭；历史 coverage 始终走 metadata-only 确定性路径。
PR #187 删除了语义 conversation compact，MEMORY/STYLE 是 Runtime V2 唯一的长期
语义层，coverage sentinel 不能替代画像内容。

## 10. compaction(摘要压缩,maintenance lane)

积压超 `_TAIL_BUDGET` 触发；coverage 只读取 seq/count 边界并写入确定性计数哨兵，
不解密历史消息、不调用 provider。该行为没有运行时开关，也不存在语义摘要、字符批次
或毒丸 quarantine 分支。

## 11. 记忆注入全景(模型什么时候看到什么)

| 时机 | 记忆形态 | 保证方式 |
|---|---|---|
| 每 turn 无条件 | genesis 人设全文;可用时注入 MEMORY/STYLE;确定性 coverage+原文 tail | harness 确定性注入 |
| 模型自主 | memory_index(分区浏览,total/returned 对账)→ memory_fetch 全文 | 两步读,工具结果计费截断 |
| 后台写 | capture(原文窗口)/dream(全文 fetch)/agent memory_write | actions.py 统一校验栈 |
| wake 额外 | attention_facts + perception_glance | temporal/runtime 数据块 |

**与 V1 的本质差**:V1 的记忆在场靠 agent 自觉拉取(强模型下好、弱模型下塌);
V2 把"必须在场的"改为确定性注入,"按需的"留给工具——弱模型地板更高,
这是保留的 V2 优势而非缺陷。

## 12. test 开关终态(2026-08-05 起「常态全开」)

| 开关 | 值 | 读取方 |
|---|---|---|
| FEEDLING_V2_CAPTURE_ENABLED | "1" 硬编码(backend+worker) | 两侧 |
| FEEDLING_V2_DREAM_ENABLED | "1" 硬编码 | serve-worker |
| FEEDLING_V2_PROFILE_ENABLED | "1" 硬编码(backend+worker) | 两侧 |
| FEEDLING_V2_PUSH_ENABLED / SELF_THINKING | "1" 显式声明 | serve-worker |
| TRAJECTORY_INSPECT / REVIEW | 0(break-glass/调试,保留 env 形态) | serve-worker |
| 每用户 proactive_settings | 全字段默认 true(dnd=false),新用户无死 lane | — |

Runtime V2 对话 coverage 固定使用本地 seq/count sentinel；maintenance、inline
catch-up 和 checkpoint 都不读取对话明文或调用 provider。需要回退时回滚 worker
镜像版本，不再提供运行时双轨开关。

## 13. V1 差异判定汇总

**已对齐(本轮闭环)**:人设全文在场(95bbd545)│wake 语义+attention_facts+
禁令(PR#158+a53a2923)│感知 baseline 判定(PR#158)│DND 闸(d8b33a26)│
工具面字段与措辞(3f9d375d;memory_index parity 更早)│capture 触发
(设备事件线已通,专项验证)。
**V2 反超 V1,保留**:self-loop guard 原子化│撞车门进发布事务│dream
rationale+独立二审│确定性注入(人设/画像/摘要)│缓存稳定前缀│身份 list
三操作│加密 workspace/task 子agent。
**已知 limitation(挂 P2,等 Seven 排期)**:chat_image_read(超 tail 窗旧图
不可回看;提案=vision-observer 模式)│reminder 列表 API(取消靠工具可用)。
