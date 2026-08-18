# Trace 覆盖矩阵

> 状态:第一版骨架(claude2, 2026-08-19)。这份文档是 Task#4「repo trace 权威文档」
> 的承重章节 —— 先有矩阵才谈得上"查案人带着问题来能不能不猜地答"。

## 这份矩阵怎么读

三色定义(与 Supervisor 2026-08-18 定稿一致):

| 色 | 含义 | 判据 |
|---|---|---|
| 🟢 绿 | **实弹见过** | 人为制造该跃迁缺失,格子会变红 |
| 🟡 黄 | 有调用点,**未实弹复验** | 代码里有 emit,但没人验证过它真的接上了 |
| 🔴 红 | 零探针 | 这一段什么都不留 |
| ⬛ 结构性不可得 | **不是 bug,不要追** | 数据在用户自己的机器上,服务端结构上看不到 |

⚠️ **绿必须是实弹见过,不是"代码里有调用"。** T130 的教训:221 个单测全绿、
突变全咬住,实弹一查 wake 道 trace_id 全空。探针存在 ≠ 探针接线。所以本版里
**没有任何绿格** —— 现有调用点一律先标黄,复验一个转一个。

每条流水线要回答的必答题(标准修订②):
**「这条路在哪里可能什么都不留就退出?」** —— 开始/结束都有代码位置,
"被吞"恰恰是代码什么都没做的地方,它不会自己冒出来,必须逐条去问。

## 一、trace 事件写到哪里(容量边界)

| 项 | 值 | 出处 |
|---|---|---|
| 发射函数 | `trace_event(store, *, subsystem, type, summary, explain, detail, content_excerpt, actor, status, trace_id, turn_id, job_id, dur_ms)` | `backend/debug_trace.py:330` |
| 落地 | 用户维度 blob `v1_flow_trace`,经 `db.append_blob_events_strict()` | `debug_trace.py:217` |
| 容量 | **2500 条/用户**(verbose 模式 1000) | `debug_trace.py:42` |
| TTL | **48 小时** | `debug_trace.py:47` |
| 淘汰 | 环形 FIFO | — |
| 异步队列 | 上限 5000 | `debug_trace.py:57` |
| 调用点总数 | 47 处,分布 18 个模块 | — |

**这就是 T138 要解决的那件事**:环 + TTL ⇒ 事发后想查,ring 里已经没有。
在 append-only 表落地之前,任何"去 trace 里查三天前那件事"的期待都是落空的。

## 二、按流水线的覆盖

### 1. chat 送达(Hosted V2)

| 阶段 | 状态 | 坐标 |
|---|---|---|
| API 收到 | 🔴 | `hosted/chat_send_core.py` HTTP 层无 emit |
| 入队 | 🟡 | `chat_send_core.py:132,146,171` |
| worker 认领 | 🔴 | `v2/worker.py` 认领处无 emit |
| provider 回合(tool loop) | 🔴 | `v2/tool_loop.py` provider 调用全程无 emit |
| 回复定稿 | 🟡 | `chat_send_core.py:204,218,248,256,361,375` |
| 落库发布 | 🟡 | `chat_send_core.py:507,584` |
| 客户端取走 | 🟡 | `chat/poll_core.py:88` |

**哪里可能什么都不留就退出**:provider 回合整段。空回复三修不透那个洞就住在这一段。

### 2. 唤醒道(heartbeat / scheduled / manual_wake / screen_watch)

| 阶段 | 状态 | 坐标 |
|---|---|---|
| 触发事件 | 🟡 | `perception/service.py:762-844` 写 user_logs |
| 入队 | 🔴 | `v2/jobs_store.py` enqueue 无 emit |
| 唤醒上下文读取 | 🔴 | `v2/worker.py` |
| provider 回合 | 🔴 | 见下方【已证实 1】 |
| **空回复被吞** | 🔴 | **非 scheduled 道整段检测不执行** |
| 回复发布(若有) | 🔴 | 唤醒道回复不发 trace |

**哪里可能什么都不留就退出**:非 scheduled 道的空回复。这是本矩阵里
**最贵的一格** —— 它让"模型选择不说"和"provider 返回空"不可区分。
期3a 的 `silent_undeclared` 列是它的**计量**(盲区多大),不是它的修复。

### 3. 记忆 capture

| 阶段 | 状态 | 坐标 |
|---|---|---|
| 入队 | 🔴 | `proactive/capture_scheduler.py` |
| 处理 | 🟡 | `capture_scheduler.py:741,762,775` / `capture_jobs.py:321` |
| 卡片入索引 | 🔴 | 读侧无 emit |
| 用户读到 | 🟡 | `memory/memory_core.py:129` |

### 4. 记忆 dream

| 阶段 | 状态 | 坐标 |
|---|---|---|
| 入队 | 🔴 | `proactive/dream_scheduler.py` |
| 处理 | 🟡 | `dream_scheduler.py:260` |
| dream 模型调用 | 🔴 | — |
| 结果落库 | 🔴 | — |

### 5. genesis / 导入

| 阶段 | 状态 | 坐标 |
|---|---|---|
| 入队 | 🔴 | `genesis/service.py` |
| 文件摄入 | 🟡 | `genesis/plaintext.py:80` |
| 建卡 | 🟡 | `genesis/genesis_core.py:89,131,662,673` / `worker.py:122,144` |
| 索引 | 🔴 | — |

### 6. 模型路线添加 / 测试(setup_core)

| 阶段 | 状态 | 坐标 |
|---|---|---|
| 建路线 | 🔴 | `hosted/setup_core.py` |
| 发起测试 | 🔴 | `setup_core.py:346` |
| 视觉探测执行 | 🔴 | `setup_core.py:381` |
| 目录声明与探测结果打架 | 🟡 | `setup_core.py:262`(**仅**此一处) |
| 结果落库 | 🔴 | `setup_core.py:277` |

⚠️ **这条链路真金白银打中转站扣 token,失败归因目前只能靠读代码推断。**
与 T128 trace-retention 同族(事发后想查、ring 里没有)。案主 claude4 有
usr_450ee 的事件窗口与 ring 查询配方。

### 7. 生图

| 阶段 | 状态 | 坐标 |
|---|---|---|
| 工具调用 | 🔴 | `capabilities/image_gen.py` |
| provider 调用 | 🔴 | `provider_client.py` |
| 结果回传 | 🔴 | tool_loop 内 |
| 消息发布 | 🟡 | 走 chat 回复路径 |

### 8. vision

| 阶段 | 状态 | 坐标 |
|---|---|---|
| 观测/测试 | 🟡 | `hosted/vision_observer.py:192,231,255,285,305,339` |
| 视觉模型调用 | 🔴 | tool_loop 内 |
| 回复生成 | 🟡 | 走 chat 回复路径 |

### 9. MCP 工具面

| 阶段 | 状态 | 坐标 |
|---|---|---|
| schema 折叠 | 🔴 | `capabilities/tool_schema.py` |
| tool search resolve | 🔴 | — |
| 工具派发 | 🔴 | — |
| 变更落地 | 🔴 | — |

**trace 需求 #6(codex/T143)**:需要 **name 级**事件 —— 某轮哪些 schema 被折叠、
哪些被 search resolve、后续是否受保护。现有 provider tool surface 只有
count/reason,答不了。等 T143 放行时给接线口径。

### 10. resident(V1)poll 环

| 阶段 | 状态 | 说明 |
|---|---|---|
| poll 请求到达 | 🟡 | `chat/poll_core.py:88` |
| 待送消息返回 | 🟡 | 同上 |
| **poll 环处理** | ⬛ | 在用户自己机器上 |
| **provider 调用** | ⬛ | 同上 |
| **工具执行** | ⬛ | 同上 |
| 回复回传 | 🟡 | 走 chat 摄入路径 |

⬛ 三格是**结构性不可得,不是 bug,不要追**。V1/resident 的 provider 与工具面
运行在用户自有服务器上(见 `docs/ACCESS_ROUTES.md` 路线②),服务端只能看见
跨网络边界的那部分。任何"给 resident 补上 provider 埋点"的提案都要先回答
"数据怎么离开用户机器",而那是产品/隐私问题不是工程问题。

## 三、已证实的判定

### 【已证实 1】非 scheduled 唤醒道的空回复检测整段不执行
`v2/worker.py:9075` → `require_reply=(lane == "scheduled")`。
`heartbeat`/`manual_wake`/`screen_watch` 传 `False`,于是
`provider_client.py:2185` 的 `if require_reply and not reply and not tool_calls
and not media` 整条判断不成立,空回复静默走完、终态 ok、零条消息。

### 【已证实 2】模型路线添加/测试路径近乎零 trace
正常路径零事件;唯一一处是 `setup_core.py:262` 的目录/探测打架分支。
种子清单写的"零 trace"精确说法应为"**正常路径零 trace,仅异常分支一处**"。

### 【存疑,不入红格】broadcast_opened「源头三个配置关着」
种子清单称源头配置关闭导致下游六处成孤儿。**代码不支持这个说法**:
`proactive/controls_v2.py:44-55` 的 `default_switches_v2()` 里
`SWITCH_SCREEN_WATCH_ENABLED` 默认 **True**,消费侧的门(`controls_v2.py:263-268`)
默认是开的。

结论:要么该说法指的不是这个后端开关(可能是 iOS 侧广播状态上报,或 perception
的 `broadcast_state` 信号源),要么它已经过时。**在弄清"三个配置"具体指哪三个
之前,这一格标 UNKNOWN 而不是红** —— 编码一个假红格会把修复引向错的地方,
比留空更坏。待与提出者(claude4 / Supervisor)对齐后回填。

## 四、下一步

1. 把黄格逐个实弹复验转绿 —— 判据是"人为制造缺失,格子会红",不是"读代码觉得对"
2. 唤醒道空回复(trace 需求 #7)与 MCP name 级事件(需求 #6)按红格优先级排
3. T138 append-only 表落地前,本矩阵所有结论都受 2500 条/48h 环的限制:
   **矩阵说"有探针"不等于"事发后查得到"**
4. 覆盖边界(partial_before / 截断标记)随读数自报 —— 需求 #4,与 T138 读端点同一格
5. SNAPSHOT 表级失败出口(需求 #8):超阀时该表静默停更,是"被吞"的第二个教科书例
