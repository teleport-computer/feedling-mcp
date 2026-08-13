# 用户 MCP 握手等待：claude driver 上「工具明明连着，AI 说用不了」

**日期**：2026-08-13
**分支**：`fix/mcp-handshake-hint`（基线 `origin/test`）
**Lark**：[mcp 冷启动连接慢，测试](https://applink.larksuite.com/client/todo/detail?guid=e5f805a1-4899-4fb7-9c82-b56512eafd27)
· [mcp不可用，尽可能完整测试mcp的可用性](https://applink.larksuite.com/client/todo/detail?guid=69f97a91-e12d-41fb-aab2-15c91b9d21bc)

> 本稿是第二版。第一版只有「注入提示词」一件事；经 Codex plan_review 与 hx 讨论后
> 调整为三件事（提示词 / 观测 / V1 补写连接状态），并按实测顶回了 review 里的一条。
> 变更理由逐条记在 §6、§8。

---

## 1. 问题

用户在 app 里加了 MCP server、点「测试连接」通过（后端拨号，10s/30s 预算，一定过），
但在聊天里 AI 说没有这个工具。

根因不是调用失败，是 **claude CLI 只给 MCP 握手约 2.5 秒**，没赶上的服务器
**一个工具都不进这一轮的工具面**——模型根本不知道自己有这个能力，只能如实说没有。

而 consumer **每轮新起一个 `claude --print` 进程**，所以每轮都在冷启动，
claude 自己那套「进程活着、后台连上、下一轮就有了」永远轮不到。

### 1.1 prod 实证（2026-08-13，`/v1/admin/data-track/debug`）

```
usr_2f4d7e65424b717c  5 台启用
  mcp.surface.wired      → wired=true, mechanism=--mcp-config, driver=claude（配置确实送到了）
  mcp.surface.registered → verdict {tavily_: ok, fetch_: failed,
                                    github_/lutopia_1/piaoliuping_: inconclusive}
                           init_status {tavily_: connected, fetch_: failed, 其余 pending}
                           called_ok: []          ← 模型一次都没碰过 MCP 工具

usr_98947da7ebbe5502  1 台启用
  init_status {piaoliuping: pending}  → 唯一一台没赶上 ＝ MCP 全灭
```

两人同为 `route=model_api` / `effective_responder=hosted_v1` / `driver=claude` /
provider `deepseek`（v4-pro 与 v4-flash）。

prod 规模（同日 admin summary）：`runtime_state_counts` = resident 820 / v2 18；
激活用户 340 人里 resident 323、v2 17。**这个问题打在 323 人身上。**

### 1.2 本地实测：预算到底是多少

自建可控延迟的 MCP 服务器，claude 2.1.217 + deepseek。握手是 **3 次串行往返**
（`initialize` → `notifications/initialized` → `tools/list`）：

```
握手在 +0.58s 开始，init 快照在 +2.3~2.7s 发出

  每次往返 0.2 / 0.4 / 0.6s → tools/list 在 2.39s 前返回 → connected ✅
  每次往返 0.8 / 1.0 / 1.5s → tools/list 在 3.00s 后返回 → pending   ❌
```

即**整个握手必须在约 2.5 秒内跑完**，折合每次往返 < ~0.65s。真实公网 MCP 服务器
（含 DNS + TCP + TLS）实测全程 1.3~2.5s，正压在生死线上——这就是用户体感的「时好时坏」。

### 1.3 为什么只有 claude

| driver | 握手行为 | 有问题吗 |
|---|---|---|
| **claude** | 不等，发完 init 就开工，后台继续连 | ✅ **有** |
| codex | `startup_timeout_sec = 20`（`user_mcp_materialize.codex_config_merged` 自己写的），阻塞等 | ❌ 没有 |
| pi | `tools/pi_mcp_bridge/index.js` 的 factory 被 pi await，每台最多 30s | ❌ 没有 |
| hermes / openclaw | **未验证** | ⚠️ 见 §9 |

在 io 里只有 `anthropic` 和 `deepseek` 两个 provider 映射到 claude driver
（`hosted/agent_runtime_cutover._CLAUDE_PROVIDERS`）。

---

## 2. 方案：三件事

### 2.1 提示词——把 claude 自带的 `WaitForMcpServers` 交给模型

claude CLI 有一个内置工具 `WaitForMcpServers`，我们从来没告诉模型它可以用。
注入三行提示：这一轮连了哪几台 MCP（**只给名字**），以及「要用某台却看不到它的工具时，
先调 `WaitForMcpServers` 等它就绪，不要直接跟用户说用不了」。

`--allowed-tools` 不是排他白名单（代码里已按 2.1.217 实测钉死，见
`_warn_if_claude_allowlist_semantics_unverified`），所以该工具本来就可调；
模型只是不知道它存在。

`WaitForMcpServers` 在 **2.1.195**（runner 镜像 `Dockerfile.agent-runner` 钉的版本）
和 2.1.217 上都存在——已用 init 事件的 `tools` 列表直接核实，**不靠 `sdk-tools.d.ts`**
（那份声明文件两个版本都没列它，但工具确实在，只看声明文件会得出相反结论）。

**不设任何前置死等**：等待只发生在模型真的需要某台服务器的那一轮。不需要 MCP 的
对话一秒都不多花——这是 hx 明确的产品约束，也是本方案相对 §6 中「进程先起、
消息后送」那条路的唯一优势。

### 2.2 观测——`wait` 的触发与结果进 trace

新增字段挂在既有的 `mcp.surface.registered` 事件上（不另起事件名，沿用
`serve_worker.py:4454` 那条约定）：`wait_attempted` / `wait_count` /
`wait_outcome`（ready / not_ready / error / absent）。

**这条是决定要不要上 §6 的转发层的唯一依据。** 没有它，「提示词够不够」只能靠猜——
现在的证据只有实验室 6 次（见 §5.2）。

### 2.3 V1 补写用户可见的连接状态

**现状（本次查出来的洞）**：`/v1/mcp/servers` 每台已经会返回一个 `runtime` 字段
（`mcp_core.list_servers:278` → `mcp_status.runtime_summaries_for_store`），
app 可以直接展示「这台最近几轮连没连上」。但 `record_runtime_results` 的调用方
**只有 `v2/serve_worker.py:4602` 一处**——V1 从不写，所以这 323 个用户的那一栏永远是空的。

consumer 每轮已经算出了这个结论（`_trace_user_mcp_registered` 的
per-server verdict：ok / failed / inconclusive），也已经发回后端，只是进了排障 trace
那条道、没进用户看得见的那条。

**做法**：后端接收 consumer 的 `mcp.surface.registered` trace 时，顺手调一次
`mcp_status.record_runtime_results`。接既有管线，不新增数据结构、不新增端点。

> **放弃的做法：让 AI 自己说「你配的 fetch_ 连不上」。**
> 2026-08-13 hx 质疑「兜底话术真的需要吗」，查证后确认不需要：这条信息属于
> 配置状态，后端已有管线、API 已在返回，靠模型转述既不可靠又走错了层。

---

## 3. 落点

### 3.1 提示词

新增 `_prepend_user_mcp_wait_hint(content, cmd)`，`tools/chat_resident_consumer.py`。

**三个闸，全满足才注入**：

1. `lane == "chat"`——背景/主动轮次本来就不挂 MCP（v2 spec §1：不拿用户的第三方额度）
2. `_is_claude_code_cmd(cmd)`——**硬要求**。给 codex / pi 的模型注入会让它去调一个
   不存在的工具，把「少一个功能」变成「多一个故障」
3. `_user_mcp_applied` 里有 enabled server——没有就**返回同一个字符串对象**

服务器名单取 `_user_mcp_applied`（与 `_user_mcp_cli_value` /
`_ensure_claude_user_mcp_flags` 同一真源），排序沿用 `user_mcp_materialize._enabled`，
保证同一配置产出同一段文本。

**每轮注入，不做 once-per-session**：`_prepend_io_cli_capability_catalog` 那套
「resume-capable driver 只注入一次」的优化在这里不成立——每一轮都是新进程、新握手、
新的一次竞速。文本只有三行；也因此**不需要** pending→commit 那套两阶段提交。

**不进 `agent_tools_prompt.md`**：那份是静态能力说明，这段是逐轮变化的运行时状态。

### 3.2 数据流

```
配置阶段（不变）
  用户加 server → 后端存密封信封 → poll 广播 fingerprint
  → consumer 拉信封、解密、materialize 出 --mcp-config 文件

聊天轮次
  组装 content
    ├─ _prepend_time_anchor_foreground
    ├─ _prepend_io_cli_capability_catalog（自托管 only，不变）
    ├─ ★ _prepend_user_mcp_wait_hint          ← 新增
    └─ _foreground_agent_message（transcript header 仍在最顶，不变）
  → spawn claude --mcp-config … --allowed-tools mcp__<name>__*
  → 模型看不到某台的工具 → 自己调 WaitForMcpServers(['<name>']) → ready
  → 工具出现 → 正常调用
  → ★ postflight：verdict + wait 字段进 trace，并写回 runtime status  ← 新增
```

---

## 4. 边界与极端输入

| 情况 | 行为 |
|---|---|
| **用户配了 30 台**（`mcp_core` 上限） | 只出名字，最坏约 30×32 字符 ≈ 1KB。不出 URL、不出请求头、不出工具清单 |
| **服务器名字跟用途不沾边**（`srv1`） | 模型可能想不到该等它。**本方案的天生上限**——只给名字、不给工具说明。§2.2 的观测就是为了量它 |
| **服务器真的连不上**（prod 的 `fetch_`） | Wait 返回 not ready。**不靠模型转述**，走 §2.3 写回状态、由 app 显示 |
| **好服 + 坏服混在一次 Wait 里** | ⚠️ 未验证，见 §9。提示词要求优先只等**明确需要的那一台** |
| **模型不听话、直接回复** | 退化成现状，不比现在差 |
| **模型连续 Wait** | ⚠️ 未验证，见 §9。观测里的 `wait_count` 就是为了抓它 |
| **operator 自己写了 `--allowed-tools`** | 注入不受影响（提示词与 argv 无关）；argv 一侧的既有告警行为不变 |
| **背景/主动轮次 / 非 claude driver** | 不注入，行为逐字节不变 |

**不需要开关**：纯旁路的提示词追加 + 一条 trace 写入，正常路径逐字节不变，
符合工作区「只有会碰正常流程的改动才留闸」的约定。

---

## 5. 验收标准

### 5.1 必须在 test 环境、真实产品路径上拿到

本地 dev harness 的绿灯**一律不算数**（工作区纪律：hx 是开发者不是终端用户）。

`tools/e2e/user_mcp_handshake_probe.py` 对一台**确定性慢**的服务器，必须同时满足：

1. `init_status` 里该服务器确实是 `pending`（**证明竞速真的发生了**）
2. 该轮 trace 里确实出现过一次 `WaitForMcpServers`（`wait_attempted=true`）
3. 之后有该服务器的**真实成功调用**（`called_ok` 含它）
4. 最终 verdict 是 `recovered`
5. **把提示词去掉，同一确定性延迟场景必须失败**

> 第 1 条和第 5 条是 Codex 审出来的：第一版只写「`called_ok` 非空」，
> 而那可能只是服务器**恰好**在 init 前连上了，证明不了修复起过作用。**接受，已改。**

模型必须精确指定 `deepseek-v4-flash` 与 `deepseek-v4-pro`，runner 用 2.1.195，
不能只写 `--model-class non-claude`。

### 5.2 单测

- 三个闸各一条：非 chat lane / 非 claude cmd / 无 enabled server 时
  `_prepend_user_mcp_wait_hint` 返回**同一个字符串对象**
- 提示文本包含全部 enabled server 名、**不含** URL / 请求头值
- **变异验证**：去掉闸 2（driver 判断）→ 至少一条转红；去掉提示注入 → e2e 判据转红

### 5.3 已有的实测证据，以及它的真实强度

复现环境：docker 真跑一台 **Ombre-Brain**（按 `io-onboarding/mcp-ombre-brain.md`
配静态 token，16 工具）＋按 `usr_2f4d` 真实形态复刻的 5 台（名字、工具数、
以及从 admin trace 反推的应答延迟，`fetch_` 与线上一样不应答）。

基线复现，5 轮逐轮一致，与 prod 判据吻合：**直连可用 3/6 台、模型看得见 58 个工具**。

跨模型矩阵（用户**不点名服务器**，只问「今天有什么新的漂流瓶吗？」，各 3 轮）：

| 模型 | 不加提示 | 加提示 | 算不算有效样本 |
|---|---|---|---|
| deepseek-v4-flash | 调不到 | ✅ 3/3，每轮主动 Wait | ✅ 算 |
| deepseek-v4-pro | 调不到 | ✅ 3/3 | ✅ 算 |
| anthropic sonnet-4-6 | **自己就好了** | ✅ 3/3（不需要 Wait） | ❌ 不算——它本来就成功 |
| glm-4.6 | 调不到 | ✅ 3/3 | ❌ 不算——io 里 glm 走 pi，不走 claude |
| kimi 8k/32k/128k | — | ⚠️ 全部 `400 tokenization failed` | ❌ 不算 |

> **真正验证「提示导致恢复」的只有 6 次，不是 12 次。** Codex 审出，**接受**。
> 且这 6 次共享同一意图与服务器形态，不能当独立样本。**不足以支撑全量 prod**，
> 只够支撑「上 test 验证」。补测矩阵见 §9。

另两条要记的：**越强的模型越不需要这句提示**（Sonnet 无提示自愈，flash 必须有）——
而我们用户大多在用便宜模型，恰好是最需要的那批。**Kimi 在 claude CLI 下完全跑不通**
（与本问题无关，且当前不影响任何人：kimi 在 io 里走 `openai_compatible` → pi），
但若将来想把 kimi 划进 claude driver，会当场炸。

---

## 6. 放弃与备选方案（全部实测过，不是纸上推演）

| 方案 | 结论 |
|---|---|
| **调 `MCP_TIMEOUT`** | ❌ **实测无效**。设 5000 / 20000 结果逐轮一致，它是上限不是下限 |
| **claude 进程常驻**（`--input-format stream-json` 持续喂） | ❌ 实测有效（同进程第 1 轮 1 个工具、第 2 轮 6 个）。否决：①用户配好后第一句仍用不了；②hosted 244 个用户＝244 个常驻 node 进程，资源账扛不住 |
| **进程先起、消息后送**（Codex 提出） | ❌ **实测成立**：起进程后不发任何消息，+2s 上游就收到握手、+10s 全部握完，此时才送消息 → 两台全 connected（含握手需 7.5s 的那台）。**是硬保证。** 但要拿到这个保证必须**前置死等**，而 hx 明确否决「不需要 MCP 的对话也被拖慢」。去掉死等后只剩「把 spawn 挪到组装上下文之前、白蹭那段时间」，收益 = 组装耗时，**未量，很可能不值**，故不做，列入 §9 待量 |
| **本地 stdio 转发层**（缓存工具清单秒答握手，`tools/call` 才连上游） | 🅱️ **plan B，保留**。实测有效且是硬保证：同 6 台，直连 3/6、走转发 6/6（58 → 69 工具，5 轮一致），原型代码已跑通。不先做：要新组件 + 缓存失效策略，且它救的是「提示词没救回的那部分」，这部分多大要靠 §2.2 的数据。**且它的「硬」也要说准：保证模型看到一份缓存目录，不保证目录新鲜、schema 未变、调用成功**（Codex 审出，接受） |
| **`ToolSearch` 恢复路径** | ⚪ 同为提示词软方案，且通常要更多模型往返，不取代 Wait。**但必须记在案**：仓库 fixture `tests/fixtures/claude_init_pending_tool_recovered.jsonl` 已记录 pending 服务器经两次 `ToolSearch` 后被发现并成功调用——**Wait 不是唯一的内置恢复机制**（Codex 审出，第一版漏了，接受） |
| **社区 `mcp-remote`** | ❌ 每次仍现连上游、不缓存工具清单，救不了 |

---

## 7. V1 / V2 差距：逐条核实结论

hx 2026-08-13 要求「之前 MCP 只改了 V2 没改 V1 的，你也应该做了」。逐条核完，
**这批 V2 修复在 V1 上几乎全部「不适用」，不是漏做**——V1 那层的工具面由 claude CLI
自己管，我们不经手；很多 V2 的修复是在补「我们自己实现 MCP 客户端」欠下的债。

| V2 修复 | V1 要补吗 | 依据 |
|---|---|---|
| `57b4aedb` 出站围栏不再下架 MCP（ombre 读后写） | ❌ | V1 的 `outbound_fence` 只在 `screen_pixel_turn` 置真；实测一轮里连调两次真 Ombre（breath → hold）成功 |
| `9760ecb5` 读服务器 `instructions` 注入系统提示 | ❌ | **实测**：服务器在 `initialize.result.instructions` 里藏一个只在那里出现的口令，claude CLI 自己就交给了模型，模型按说明先 unlock 再取值 |
| `65967d19` MCP 返回单独一档结果预算 | ❌ | 那是 V2 自己的通用截断（`backend/capabilities/result_budget.py`，env 均为 `FEEDLING_V2_*`），V1 不经手；CLI 对超大结果自己落盘并告知模型路径 |
| `f703ae6a` 折叠工具 schema 按需加载 | ❌ | V2 省 token 的机制，V1 工具表由 CLI 管 |
| `83738bab` 参数说明与 enum 交给模型 | ❌ | 同上，V1 由 CLI 直接从服务器取、原样透传 |
| `309821d4` 工具上限 128 | ❌ | 落在 `mcp_core`（配置层，两边共用）+ pi bridge；claude 那条我们不裁剪 |

**V1 真正缺的只有两条**，都在本方案里：①握手竞速（§2.1）；②连接状态不写回（§2.3）。

---

## 8. V1 / V2 各自怎么落

- **V1（hosted resident_cli + 全部 VPS 自托管）**：本方案全部三件事，落在 consumer +
  后端 trace 接收侧。prod 激活用户 323 人。
- **V2**：**握手部分不需要**——V2 是我们自己的工具循环，`hosted.mcp_tools.load_turn_mcp`
  同步加载工具目录后才发起模型调用，不存在竞速。runtime status 它已经在写。prod 17 人。

---

## 8.5 补测矩阵与代价（2026-08-13 跑完，40 个 turn）

Codex 建议的矩阵已跑：2 个受影响模型 × 5 场景 × 4 次。服务器组按 usr_2f4d 真实
形态搭，另加一台**名字完全不透明**的 `srv7`（提示词方案的假想上限）与一台永不
应答的 `fetch_`。

| 场景 | flash 命中 | pro 命中 | 耗时中位 | 最慢 |
|---|---|---|---|---|
| S1 语义名 + 隐式需求 | 4/4 | 4/4 | 88s / 56s | 187s |
| S2 **不透明名** + 隐式需求 | 4/4 | 4/4 | 134s / 100s | 248s |
| S3 不透明名 + 用户点名 | 4/4 | 4/4 | 86s / 30s | 123s |
| S4 **根本不需要 MCP** | — | — | **10s / 11s** | 13s |
| S5 超慢 + 好服坏服混合 | 4/4 | 4/4 | 40s / 46s | 57s |

**结论一:需要 MCP 的场景 32/32 全中**，两个模型都是。原先担心的「名字不沾边模型
就想不到」（§4、§9.2）**没有复现**——S2 用完全不透明的 `srv7` 仍然 4/4。这条风险
可以降级，但样本仍只有 8 次。

**结论二:不需要 MCP 的对话没有被拖慢，误触发 0/8**（S4 中位 10~11s，vs 需要 MCP
的 53s，且一次 Wait 都没发生）。这是 hx 的硬约束，用数据答了。

**结论三（不好看的那条）:需要 MCP 的轮次很慢，而且有长尾。**
- 每轮 Wait 次数分布 `{0:9, 1:21, 2:9, 3:1}` —— **10/40 等了不止一次**，
  正是 Codex 预警的「弱模型连着 Wait、每次叠加」，不是理论。
- 耗时中位 53s、最慢 248s；输入 token 中位 127k、p95 578k。

⚠️ 读这三个数字要带上限制：本地跑、延迟是注入的、`fetch_` 故意 120s 不应答，
且**没有对照臂**——同样这些轮次在没有提示词时会很快，但答案是错的（模型说
「我没有这个工具」）。所以这不是「我们把它变慢了」，是「用慢换对」。但 248s
对用户仍然是不可接受的，token 增量也没法从这批数据里把提示词自己的贡献剥出来。

**这条直接加强了 plan B 的理由**：转发层在开局就把工具面给全，根本不需要 Wait，
也就没有这条长尾。是否上，仍按 §2.2 的 prod 观测数据定。

---

## 9. 未验证的风险（必须进 plan 的待办）

1. ~~**hermes / openclaw 两种自托管 agent 没测**~~ —— **hermes 已验完并已修**
   （commit `ef30fbb4`）：读 0.18.2 源码确认 `mcp_discovery_timeout` 默认 **1.5s**，
   比 claude 的 ~2.5s 更紧，同一个病更严重。但它是可配的真上限
   （`wait_for_mcp_discovery` = `thread.join(timeout)`，发现完成即返回），故
   `hermes_config_merged` 现在写 10.0（取自 `mcp_probe._CONNECT_TIMEOUT`）。
   代价：一台彻底连不上的服务器会让每轮多等满 10s。**openclaw 仍未测。**
   原文如下————`user_mcp_materialize.hermes_config_merged`
   只写 url/headers，没有超时旋钮，等不等未知。若也不等，本方案覆盖不到。
2. ~~**补测矩阵未跑**~~ —— **已跑完，见 §8.5。** 原计划如下（Codex 建议，接受）：两个受影响模型 × 5 种场景 × 4 次 = 40 个
   真实产品路径 turn，场景为：语义化名字+隐式需求 / 无意义名字+隐式需求 /
   无意义名字+用户点名 / 配了坏服但问题根本不需要 MCP / 目标超 5 秒或好服坏服混合。
   **不要只追加更多同样的「漂流瓶」问题。**
3. **Wait 的失败语义未实测**：Codex 称「单次最多约 5 秒」。**我实测顶回了这一条**——
   一台握手需 7.5 秒的服务器，Wait 仍返回 `ready: true`（原因是握手在进程启动即开始，
   模型第一次往返本身已烧掉数秒，Wait 时只剩残余）。**但它同条里的另两项我没测**：
   好服+坏服混合是否整体报错、弱模型是否连续 Wait 叠加。这两项要在 test 上验。
4. **代价部分量了（§8.5），但缺对照臂**（Codex 审出）：需要 A/B 记录用户端总耗时 p50/p95、
   `agent_ms`/`api_ms`/`num_turns`、Wait 次数与结果、输入 token 增量、
   以及**不需要 MCP 的请求里误触发 Wait 的比例**。上线阈值要先设，不能测完再解释。
5. **「挪 spawn 提前」的收益未量**（见 §6）：等于组装上下文的耗时，逐轮差别很大
   （带引用记忆的轮次要与 enclave 往返，纯文字轮次可能只有几十毫秒）。先量再决定做不做。
6. 本地实测用 2.1.217，runner 是 2.1.195——工具存在性已核实，行为要在 test 上复验。
