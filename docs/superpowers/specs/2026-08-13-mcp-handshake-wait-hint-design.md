# 用户 MCP 握手等待：claude driver 上「工具明明连着，AI 说用不了」

**日期**：2026-08-13
**分支**：`fix/mcp-handshake-hint`（基线 `origin/test`）
**Lark**：[mcp 冷启动连接慢，测试](https://applink.larksuite.com/client/todo/detail?guid=e5f805a1-4899-4fb7-9c82-b56512eafd27)
· [mcp不可用，尽可能完整测试mcp的可用性](https://applink.larksuite.com/client/todo/detail?guid=69f97a91-e12d-41fb-aab2-15c91b9d21bc)

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
| hermes / openclaw | **未验证** | ⚠️ 见 §7 |

在 io 里只有 `anthropic` 和 `deepseek` 两个 provider 映射到 claude driver
（`hosted/agent_runtime_cutover._CLAUDE_PROVIDERS`）。

---

## 2. 方案

**claude CLI 自带一个内置工具 `WaitForMcpServers`，我们从来没告诉模型它可以用。**

在 claude 的聊天轮次里注入一小段提示，内容是：这一轮连了哪几台 MCP 服务器（名字），
以及「要用某台而看不到它的工具时，先 `WaitForMcpServers` 等它就绪，不要直接跟用户说用不了」。

不需要新组件、不需要新进程、不改任何数据面。

### 2.1 为什么这就够

`--allowed-tools` 不是排他白名单（代码里已按 2.1.217 实测钉死，见
`_warn_if_claude_allowlist_semantics_unverified`），所以 `WaitForMcpServers`
本来就可调；模型只是不知道它的存在与用途。

`WaitForMcpServers` 在 **2.1.195**（runner 镜像 `Dockerfile.agent-runner` 钉的版本）
和 2.1.217 上都存在——已用 init 事件的 `tools` 列表直接核实，不靠 `sdk-tools.d.ts`
（那份声明文件两个版本都没列它，但工具确实在）。

### 2.2 数据流

```
配置阶段（不变）
  用户加 server → 后端存密封信封 → poll 广播 fingerprint
  → consumer 拉信封、解密、materialize 出 --mcp-config 文件

聊天轮次（本次新增的只有「注入提示」这一步）
  组装 content
    ├─ _prepend_time_anchor_foreground
    ├─ _prepend_io_cli_capability_catalog（自托管 only，不变）
    ├─ ★ _prepend_user_mcp_wait_hint  ← 新增
    └─ _foreground_agent_message（transcript header 仍在最顶，不变）
  → spawn claude --mcp-config … --allowed-tools mcp__<name>__*
  → 模型看不到某台的工具 → 自己调 WaitForMcpServers(['<name>']) → ready
  → 工具出现 → 正常调用
```

### 2.3 落点

新增 `_prepend_user_mcp_wait_hint(content, cmd)`，`tools/chat_resident_consumer.py`。

**三个闸，全满足才注入**：

1. `lane == "chat"`——背景/主动轮次本来就不挂 MCP（v2 spec §1：不拿用户的第三方额度）
2. `_is_claude_code_cmd(cmd)`——**这条是硬要求**。给 codex / pi 的模型注入会让它去调
   一个不存在的工具，把「少一个功能」变成「多一个故障」
3. `_user_mcp_applied` 里有 enabled server——没有就逐字节原样返回

服务器名单直接取 `_user_mcp_applied`（和 `_user_mcp_cli_value` /
`_ensure_claude_user_mcp_flags` 同一个真源），排序规则沿用
`user_mcp_materialize._enabled` 的按名排序，保证同一配置产出同一段文本。

**每轮注入，不做 once-per-session**：`_prepend_io_cli_capability_catalog` 那套
「resume-capable driver 只注入一次」的优化在这里不成立——每一轮都是新进程、
新握手、新的一次竞速，「这一轮哪些还没连上」是逐轮变化的事实。文本只有三行，
代价可忽略；也因此**不需要** pending→commit 那套两阶段提交。

**不进 `agent_tools_prompt.md`**：那份是能力说明（这个 agent 有哪些工具），
这段是运行时状态提示（这一轮连了哪几台、可能还没就绪），逐轮变化，
放静态文件里会立刻过期。

---

## 3. 边界与极端输入

| 情况 | 行为 |
|---|---|
| **用户配了 30 台**（`mcp_core` 上限） | 只出名字，一台一个短名，最坏约 30×32 字符 ≈ 1KB。不出 URL、不出请求头、不出工具清单 |
| **服务器名字跟用途完全不沾边**（`srv1`） | 模型可能想不到该等它。**这是本方案的天生上限**——只给名字、不给工具说明。见 §6 备选方案 |
| **服务器是真的连不上**（如 prod 的 `fetch_`） | `WaitForMcpServers` 返回 not ready；提示里要求模型**说出是哪一台连不上**，而不是笼统说「我没这功能」。用户才知道去哪修 |
| **模型不听话、直接回复** | 退化成现状，不比现在差。没有任何新的失败路径 |
| **operator 自己写了 `--allowed-tools`** | 注入不受影响（提示词与 argv 无关）；argv 一侧的既有告警行为不变 |
| **背景/主动轮次** | 不注入（闸 1），行为逐字节不变 |
| **非 claude driver** | 不注入（闸 2），行为逐字节不变 |

**不需要开关**：这是纯旁路的提示词追加，正常路径逐字节不变，符合工作区
「只有会碰正常流程的改动才留闸」的约定。

---

## 4. V1 / V2 各自怎么落

- **V1（hosted resident_cli + 全部 VPS 自托管）**：本方案，落在 consumer。
  prod 上激活用户 323 人在这条线。
- **V2**：**不需要**。V2 是我们自己的工具循环，`hosted.mcp_tools.load_turn_mcp`
  同步加载工具目录后才发起模型调用，不存在竞速。prod 上 17 人。

---

## 5. 验收标准

必须在 **test 环境、真实产品路径**上拿到，不接受本地 dev harness 的绿灯：

1. `tools/e2e/user_mcp_handshake_probe.py` 在 `--runtime resident --model-class non-claude`
   下，对一台**故意慢**的服务器拿到 `called_ok` 非空（现状是空）。
2. 单测锁三个闸：非 chat lane / 非 claude cmd / 无 enabled server 时
   `_prepend_user_mcp_wait_hint` 返回**同一个字符串对象**（逐字节不变）。
3. 单测锁提示文本包含全部 enabled server 名、且**不含** URL / 请求头值。
4. **变异验证**：把闸 2（driver 判断）去掉 → 至少一条测试转红。

### 5.1 已有的实测证据（本方案的立论基础）

复现环境：docker 真跑一台 **Ombre-Brain**（按 `io-onboarding/mcp-ombre-brain.md`
配静态 token，16 工具）＋按 `usr_2f4d` 真实形态复刻的 5 台（名字、工具数、
以及从 admin trace 反推的应答延迟，`fetch_` 与线上一样不应答）。

**基线复现**（5 轮，逐轮一致，与 prod 一致）：

```
直连：可用 3/6 台，模型看得见 58 个工具
      连上 ombre / tavily_ / github_；pending: piaoliuping_ / lutopia_1 / fetch_
```

**跨模型矩阵**（用户不点名服务器，只问「今天有什么新的漂流瓶吗？」，各 3 轮）：

| 模型 | 不加提示 | 加提示 |
|---|---|---|
| **deepseek-v4-flash**（`usr_2f4d` 在用） | 调不到 | ✅ **3/3**，每轮主动 `WaitForMcpServers` |
| deepseek-v4-pro（`usr_9894` 在用） | 调不到 | ✅ 3/3 |
| anthropic claude-sonnet-4-6 | **自己就好了** | ✅ 3/3（不需要等待工具） |
| glm-4.6（旁证，io 里走 pi） | 调不到 | ✅ 3/3 |
| kimi moonshot-v1-8k/32k/128k | — | ⚠️ 全部 `400 tokenization failed` |

两条要记的：**越强的模型越不需要这句提示**（Sonnet 无提示自愈，flash 必须有）——
而我们用户大多在用便宜模型，恰好是最需要的那批。**Kimi 在 claude CLI 下完全跑不通**，
与本问题无关且当前不影响任何人（kimi 在 io 里走 `openai_compatible` → pi），
但若将来想把 kimi 划进 claude driver，会当场炸。

---

## 6. 放弃的替代方案

| 方案 | 为什么放弃 |
|---|---|
| **调 `MCP_TIMEOUT`** | **实测无效**。设 5000 / 20000 结果逐轮一致，它是上限不是下限，claude 不会为它多等 |
| **让 claude 进程常驻**（照上游设计，`--input-format stream-json` 持续喂） | 实测有效——同一进程第 1 轮 1 个工具、第 2 轮 6 个工具。但①**用户配好后的第一句话仍然用不了**；②hosted 上 244 个用户＝244 个常驻 node 进程，runner 资源账扛不住；③进程生命周期/崩溃恢复是全新的一类故障 |
| **本地 stdio 转发层**（缓存工具清单秒答握手，`tools/call` 才连上游） | 实测有效且**是硬保证**：同样 6 台，直连 3/6、走转发 6/6（69 工具，5 轮一致）。但要新写一个组件 + 缓存失效策略。**保留为 plan B**：若 §7 的未验证项翻车、或「名字不沾边」在真实用户身上高频出现，再上 |
| **社区的 `mcp-remote`** | 它每次仍现连上游，不缓存工具清单，救不了这个问题 |

---

## 7. 未验证的风险（必须在 plan 里排成待办）

1. **hermes / openclaw 两种自托管 agent 没测**——它们连 MCP 是等还是不等未知
   （`user_mcp_materialize.hermes_config_merged` 只写 url/headers，没有超时旋钮）。
   若它们也不等，本方案覆盖不到，需各自补。
2. **「名字不沾边」的真实频率未知**——现有 prod 样本里名字都跟用途相关
   （`tavily_`/`github_`/`fetch_`/`piaoliuping_`），但这是小样本。
3. **本地实测用的是 2.1.217，runner 是 2.1.195**——工具存在性已核实，
   但模型在 195 上的实际行为要在 test 上复验。
4. 上面全部实测在**本地 dev harness**上完成，按工作区纪律**不能当作产品路径的验收**。
