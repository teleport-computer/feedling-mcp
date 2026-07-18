# pi 用户 MCP 桥：把用户 MCP server 注册成 pi 原生工具

日期：2026-07-17
状态：方向已与用户确认，待实施计划
目标分支：**test**
前置：`docs/superpowers/specs/2026-07-08-user-mcp-servers-design.md`（下称「v2 spec」）。
本文实施 v2 spec **§11 后续项第一条（pi MCP extension）**，并**订正** v2 spec §1
与 `2026-07-16-user-mcp-network-relaxation-design.md` §2 中关于 pi 的过期/错误表述。

## 0. 一句话

pi 官方不支持 MCP，但它的 extension 能注册原生工具——写一个 extension 读取**已经
物化好的** `user-mcp.json`、把每个 MCP 工具注册成 pi 工具，**数据链路一行不改**，
代价是 pi 模板的工具白名单要从 `-t bash` 换成 `-ne -xt read,edit,write`。

## 1. 背景：一个到期的待办被误记成了「路线已放弃」

usr_6f5a 反馈「连了 ombre brain MCP 但 AI 看不到」。逐层核对后确认不是配置问题，
是能力缺口：后端存储/下发正常、consumer 物化正常，断在最后一环——
`_user_mcp_cli_value` 只有 claude（`--mcp-config`）和 codex（`-c mcp_servers.*`）
两个分支，pi 模板根本没有 `{mcp}` 占位符，对 pi 返回空。

影响面是**所有 pi driver 托管用户**，即 gemini / openrouter / openai_compatible 三类
provider 的全部用户——不止她一个。iOS 的 MCP 设置页对任何路线都放行添加，没有能力
提示，用户白配一通。

### 1.1 事实是怎么走偏的（这条链本身值得记录）

| 日期 | 事件 |
|---|---|
| 07-08 / 07-10 | v2 spec 提交（`ca00cfe8`）。§1 写「test 无 pi driver，本期不涉及」——**当时属实**。§11 把 pi MCP extension 列为后续项，措辞是「**等 pi driver 随 pre 合流后**」，即预期它会来。 |
| 07-13 | pi driver 合流进 test（`1e01ef7e`、`3c0a37c3`）。**§1 那句话从这一刻起过期，但没人回头改。** |
| 07-16 | network-relaxation spec §2 决策表读到那句过期事实，写成「pi 不注入（v2 spec §1 已记 test 分支无 pi driver，**路线已放弃**）」。 |
| 07-17 | `8cb9314b` 把该结论固化进代码注释 `# pi: route abandoned (v2 spec §1), no CA surface`，已在 test 与 pre 两个分支。 |

关键的偷换发生在 07-16：v2 spec 说的是「**本期不涉及**」（一个排期决定），被读成了
「**路线已放弃**」（一个战略结论）。后者从来没有人做过这个决定。其后果是 §11 那个
「等 pi driver 合流就做」的待办在 07-13 到期，却在 07-16 被反向**误销案**——这正是
usr_6f5a 撞上的洞至今仍在的原因。

**现实**：pi 是 gemini / openrouter / openai_compatible 在 test 与 prod 上的唯一承载
driver（`db.py:1800`、`agent_runtime_cutover.py:101`、`supervisor.py:714`、
`spawners.py:455`；supervisor 心跳报 `pi_enabled`）。deepseek **不**在此列——它走
claude driver 的 Anthropic-wire（`spawners.py:556`、`:834`）。

## 2. 范围与已定决策

| 决策点 | 结论 |
|---|---|
| 方案 | **每个 MCP 工具独立注册成 pi 工具**；模板 `-t bash` → `-ne -xt read,edit,write` |
| MCP client | **零依赖手写**，理由与协议版本对齐 `backend/hosted/mcp_probe.py` |
| 传输类型 | 仅 streamable HTTP。**不做 stdio**（沿用 v2 spec §1） |
| 工具命名 | `mcp_<server>_<tool>`，sanitize + 确定性去重后缀（gemini 兼容） |
| 工具数上限 | 50，超出丢弃并 log 丢弃内容 |
| 连接超时 | 10s/server，并发连接，失败跳过不阻塞启动 |
| lane gating | chat 注入 `-e`，background 不注入（**结构性保证**，非开关） |
| 配置传递 | env `FEEDLING_USER_MCP_FILE`（per-user 路径，extension 是共享静态文件） |
| extension 语言 | **`.js`，非 `.ts`**（理由见 §4.1）——`loader.js:416` 两者皆收 |
| extension 分发 | `tools/pi_mcp_bridge/`，`COPY tools/` 已覆盖，**Dockerfile 不改** |
| CA | **顺带修**：删掉 `_user_mcp_ca_env` 的 pi early-return，落 `NODE_EXTRA_CA_CERTS` |
| 数据链路 | **零改动**（存储/下发/物化/信封/指纹全部复用） |
| 测试 | pytest + fake MCP server + node harness；CI 显式加 `setup-node` |

### 2.1 为什么是 `-xt` denylist 而不是保住 allowlist

评估过三条路：

- **Gateway tool**（只注册 `mcp_list_tools` / `mcp_call_tool` 两个固定名工具，
  `-t` 不动）：安全姿态零改动，但模型看不到每个工具的 schema，参数退化成自由 JSON
  且无校验，且这类 meta-tool 模式模型实际使用率明显偏低——**很可能做完了用户仍然
  觉得「AI 看不到工具」**，即没有达成本设计的目的。
- **两阶段 spawn**（consumer 先 `tools/list` 拿工具名再拼 `-t`）：allowlist 与
  per-tool schema 两全，但每个 chat 回合 spawn 前要多一次同步 MCP 握手——**把一个
  可选功能的故障放大成主链路故障**（用户 server 慢/挂即拖累正常聊天），且工具名
  sanitize 规则要在 Python 与 JS 两处各写一遍，是必然漂移的重复逻辑。
- **`-xt` denylist**（本设计）：一步到位，体验对齐 claude。代价是安全姿态从
  allowlist 滑向 denylist，缓解见 §3.2。

## 3. 架构

### 3.1 模板改动与对称性

`-xt read,edit,write` 与 `-t bash` 在效果上等价（内建工具即
`read/bash/edit/write` 四个，减掉三个正好剩 bash），因此 base 模板一次性换过去，
`{mcp}` 只负责注入 extension：

```
现在：pi --mode json -t bash                  --append-system-prompt ...
改为：pi --mode json -ne -xt read,edit,write {mcp} --append-system-prompt ...
```

于是 `_user_mcp_cli_value` 的 pi 分支与 claude 分支同构：

```python
# claude（现有）
return f"--mcp-config {USER_MCP_FILE}" if lane == "chat" else ""
# pi（新增）
return f"-e {PI_MCP_BRIDGE_FILE}" if lane == "chat" else ""
```

- 无 MCP 的 pi 用户：active 仍为 `["bash"]`，**行为不变**
- chat 回合：active 变为 `["bash", ...MCP 工具]`
- background 回合：不注入 `-e`，MCP 工具**根本不存在**于该回合

### 3.2 `-ne` 是必须的，不是保险

换成 `-xt` 后 `allowedToolNames` 变 `undefined`，`~/.pi/agent/extensions/` 下任何被
自动发现的 extension，其工具都会被 `includeAllExtensionTools` 激活——而 `-t bash`
现在是挡着的。agent 有 bash 权限，能往自己 home 里写文件。这不构成提权（它本就能
执行任意代码），但会让上一回合的残留 extension 影响下一回合。`-ne` 关闭自动发现、
显式 `-e` 照常工作，正好补平这个行为差。

denylist 的残余风险（pi 新增内建工具漏网）由两点缓解：
`defaultActiveToolNames` **硬编码**在 `sdk.js:131`，新增内建工具不会自动进 active；
且 `Dockerfile.agent-runner:42` 对 pi 是精确 pin，注释已明确要求
「bump explicitly after reviewing its output-protocol changes」——升级 review 是既有
流程，本设计只是往该 checklist 里加一条（见 §8）。

### 3.3 改动清单

| 文件 | 改动 |
|---|---|
| `tools/pi_mcp_bridge/` | **新增**（`index.js` / `mcp_client.js` / `tool_mapping.js`），唯一实质代码 |
| `backend/agent_runtime/spawners.py` | pi 模板：`-t bash` → `-ne -xt read,edit,write`，加 `{mcp}` |
| `tools/chat_resident_consumer.py` | `_user_mcp_cli_value` 加 pi 分支；`_user_mcp_ca_env` 删 pi early-return；env 注入 `FEEDLING_USER_MCP_FILE` |
| 文档/注释/测试 | 订正 `route abandoned`（见 §8） |

## 4. 组件

### 4.1 桥（`tools/pi_mcp_bridge/`）

**语言选 `.js` 而非 `.ts`**，尽管 pi 文档主推 TypeScript（`loader.js:416` 实际
`.ts`/`.js` 两者皆收，`index.ts`/`index.js` 亦然）。三条理由：

1. 同仓已有 JS extension 先例：`deploy/openclaw-plugins/feedling-io-tools/index.js`
2. 测试 harness 可用 node 原生 `import()`，不依赖 node ≥22.6 的 type-stripping，
   CI 对 node 版本不挑
3. **本仓没有任何 TS 工具链**——无 tsconfig、无 tsc、CI 无类型检查。没有 checker
   的 `.ts` 不构成类型安全，只是语法负担

拆三个文件，各自一个职责，便于独立单测：

| 文件 | 职责 |
|---|---|
| `mcp_client.js` | 零依赖 MCP 协议（握手/list/call/SSE/session） |
| `tool_mapping.js` | MCP tool → pi tool 的纯函数（命名 sanitize、去重、上限） |
| `index.js` | extension 入口：async factory、装配、错误隔离 |

零依赖手写 MCP client。理由与 `mcp_probe.py` docstring 所述一致——
「one endpoint doesn't justify the dependency + requirements.lock churn」：我们只需
`initialize` → `notifications/initialized` → `tools/list` → `tools/call` 四个方法，
不值得往 TEE 镜像里拖一棵 node_modules。`mcp_probe.py` 已趟平 SSE 解析、
`Mcp-Session-Id`、协议版本，是现成参考；**两边共用 `_PROTOCOL_VERSION = "2025-03-26"`，
升级时一起动**。

结构：

1. **async factory** 读 `process.env.FEEDLING_USER_MCP_FILE` → 解析
   `{"mcpServers": {...}}` → 并发连每个 server → `tools/list`
2. 逐个 `pi.registerTool({name, label, description, parameters, execute})`
3. `execute` → `tools/call` → MCP content 转 pi 的 `{content: [...], details: {}}`

### 4.2 schema 直接透传（已验证）

MCP 的 `inputSchema` **原样**传给 `parameters`，不做任何转换。pi 显式支持无 typebox
metadata 的裸 JSON Schema——`pi-ai/dist/utils/validation.js:257`：

```js
if (!hasTypeBoxMetadata(tool.parameters) && isJsonSchemaObject(tool.parameters)) {
    const coerced = coerceWithJsonSchema(args, tool.parameters);
```

`hasTypeBoxMetadata` 即检测 `TYPEBOX_KIND` symbol，`coerceWithJsonSchema` 是专为
裸 JSON Schema 写的强制路径。

**残余风险**：`getValidator` 走 typebox `Compile(schema)`，对 `$ref` / `oneOf` 等
构造的支持度未逐一验证。实现时若遇不支持的构造，在桥内清洗 schema（而非放弃校验）。

### 4.3 工具命名

pi driver 承载 gemini，gemini 工具名约束为 `^[a-zA-Z0-9_-]{1,64}$`；MCP 工具名由
用户的 server 决定，字符不可控。故 `mcp_<server>_<tool>`：非法字符替换、超长截断。
server 名已被 `_SAFE_NAME`（`[a-z0-9_-]{1,32}`）约束，tool 名未约束，sanitize 后
可能撞名——**去重后缀必须确定性**（相同输入恒得相同输出），否则模型跨回合看到的
工具名会漂移。单下划线而非 claude 的 `mcp__<server>__<tool>`，是为给 64 字符预算
省空间。

### 4.4 工具数上限

每个注册的工具都进 system prompt。用户接一个有上百工具的 MCP server 会让 system
prompt 膨胀、抬高每回合 token 成本。上限 50（跨所有 server 合计），超出部分丢弃。

**丢弃必须 log 出丢了哪些工具**：否则用户侧表现为「工具时有时无」——本仓最难排查
的一类问题，且与本设计要解决的原始症状（「AI 看不到这个工具」）无法区分。

### 4.5 错误隔离

pi 会 await async factory 且**阻塞启动**（`docs/extensions.md`：完成于 `session_start`
之前）。因此 factory 中任何未捕获异常都可能让 pi 起不来，即**用户的 MCP server 挂了
会导致整个聊天挂掉**。硬性要求：

- 每个 server 独立 try/catch，失败跳过 + log，**绝不向上抛**
- 每 server 10s 超时，超时放弃该 server 并继续启动
- `tools/call` 失败返回 `{ok:false, error}` 形状的 content 交给模型，不抛异常
  （与 `feedling-io-tools` 插件的 `toolResult({ok:false, error})` 处理一致）

## 5. 数据流

配置分发段**完全不动**：

```
iOS → POST /v1/mcp/servers → mcp_core 存信封
  → poll 广播 fingerprint → _maybe_apply_user_mcp 发现变化
  → 拉信封 → enclave 解密 → _materialize_user_mcp 写 user-mcp.json (0600)
```

chat 回合（新增仅中间两步）：

```
lane="chat" → _user_mcp_cli_value → "-e /app/tools/pi_mcp_bridge/index.js"
            → child_env: FEEDLING_USER_MCP_FILE + NODE_EXTRA_CA_CERTS
            → spawn pi → async factory 读文件 → 并发连各 server → tools/list
            → registerTool ×N → 启动完成 → 模型看到工具 → 调用 → tools/call
```

**配置变更自动生效**：extension 每回合重读文件，文件由 `_maybe_apply_user_mcp` 在
fingerprint 变化时改写。用户在 iOS 上加 server，下一条消息即生效，无需额外 reload。

**每回合重做 MCP 握手**是既定成本（consumer 每回合 spawn 新 pi 进程，靠
`--session-id` 续接会话）。可接受，因为它**与 claude 完全同构**——claude 的
`--mcp-config` 同样每回合重连，不是 pi 路线特有的退化。

## 6. 已验证的源码事实（pi 0.80.3）

以下断言均直接读 `@earendil-works/pi-coding-agent@0.80.3` 源码/文档得出，是本设计
成立的基础：

| 断言 | 证据 |
|---|---|
| pi 仍无内建 MCP | `README.md:491`「No MCP. ... or build an extension that adds MCP support」 |
| `-t` 对 extension 工具同样生效 | `agent-session.js:1867` `allCustomTools = [...registeredTools, ...].filter(t => isAllowedTool(t.definition.name))`——被滤的工具**不进 registry**，`setActiveTools` 也救不回 |
| 无 `-t` 时 extension 工具自动全 active | `agent-session.js:145` 启动即传 `includeAllExtensionTools: true`；`:1917` 对应分支 push 全部 extension 工具 |
| `defaultActiveToolNames` 硬编码 | `sdk.js:131` `["read","bash","edit","write"]` |
| pi 支持裸 JSON Schema | `pi-ai/dist/utils/validation.js:257` + `coerceWithJsonSchema` |
| async factory 阻塞启动且早于 `session_start` | `docs/extensions.md`「If the factory returns a Promise, pi awaits it before continuing startup」 |
| `-e` 每回合生效；`-ne` 禁发现但保留 `-e` | `pi --help` |

**时效性声明（本设计存在的根本原因）**：上表全部绑定 pi 0.80.3。
`Dockerfile.agent-runner:42` 对 pi 精确 pin，**升级 pi 时必须重验上表**。本文任何
结论都不得在未重验的情况下，被下游文档升级成更强的表述——§1.1 记录的正是这种升级
造成的事故。

## 7. 测试

- **Python 侧（pytest）**：`_user_mcp_cli_value` pi 分支三态（chat/background/无
  server）；pi 模板渲染含 `-ne -xt read,edit,write` 与 `{mcp}` 槽位；
  `_user_mcp_ca_env` 对 pi 返回 `NODE_EXTRA_CA_CERTS`（**现有断言会红，见 §8**）；
  **等价性回归**：未配 MCP 的 pi 用户命令行行为与改动前一致。
- **桥行为**：进程内 fake MCP server（复用 v2 spec §10 的 ASGI 小应用模式）+
  subprocess node harness（mock `ExtensionAPI`）：断言注册的工具名/schema、命名
  冲突去重、上限截断、**server 不可达时安静跳过而非抛异常**。
- **CI 接线（两件事，缺一则前功尽弃）**：
  1. **把测试文件加进 CI 的 pytest 列表**。`ci.yml` 的 pytest 目标是**手工维护的
     白名单**（止于 `tests/test_dream_prompt_v1.py`），而
     `tests/test_user_mcp_consumer.py` 与 `tests/test_agent_runtime_spawners.py`
     **都不在其中**——本设计要改的断言、以及模板改动会打红的 `-t bash` 断言
     （`spawners.py` 测试 `:761`、`:891`），**CI 目前一条都不跑**。
  2. **显式 `setup-node`**，锁定 node 版本供桥的 harness 使用。
  本仓有前科：conftest `collect_ignore` 在无 PG 时静默丢弃整个 DB 模块且**零
  skipped**，「391 passed 全绿」是假象（真实基线 2440）。白名单漏收是同一个坑的
  另一变体——不是 skip，是**根本没跑**。**宁可硬失败，不要假绿。**
- **已知遗留缺口（明写不藏）**：`test_agent_runtime_spawners.py` 含
  `test_pi_models_json_loads_and_enables_reasoning_in_real_pi`，它 `subprocess`
  真跑 `pi --list-models` 且不 skip。将该文件纳入 CI 需先在 CI 装 pi（可仿
  `npm install -g phala@1.1.19  # pinned (§8 supply-chain)` 的既有模式，pin 至
  Dockerfile 同版本 0.80.3）。若本期不装，则该文件**仍不进 CI**，其 pi 模板断言
  只在本地跑——此缺口必须在计划中显式记录，不得默认为「已覆盖」。
- **手工 E2E**（对齐 v2 spec §10）：test 环境 curl 建配置 + 真实公共 MCP server，
  pi 路线聊一轮验证工具可用；proactive 回合验证工具不可见。

## 8. 文档与注释订正

`route abandoned` 的误读已传播到四处，本期一并订正：

| 位置 | 现状 | 订正为 |
|---|---|---|
| v2 spec `:5` `:26` | 「test 无 pi driver，本期不涉及」 | 标注 07-13 已合流、该表述已过期，指向本文 |
| 07-16 spec §2 决策表 | 「pi 不注入（…路线已放弃）」 | pi 有 driver 且在跑；MCP 桥见本文 |
| `chat_resident_consumer.py:5704` | `# pi: route abandoned (v2 spec §1), no CA surface` | 删除该分支（pi 是 Node，走 `NODE_EXTRA_CA_CERTS`） |
| 07-16 plan `:891` | `assert c._user_mcp_ca_env(["pi", "--message"]) == {}` | 改为断言 `NODE_EXTRA_CA_CERTS`——**该测试把错误理由锁成了契约** |
| `Dockerfile.agent-runner:42` | 注释称 pi driver 覆盖「gemini/openrouter/openai_compatible/**deepseek**」 | 删去 deepseek——它 07-14 已改回 claude driver（`spawners.py:556`、`:834`），注释未跟进 |

最后一行是本设计撰写过程中发现的：起草时曾据该注释把 deepseek 写进 pi 的影响面，
经 `db.py:1800` / `agent_runtime_cutover.py:101` / `supervisor.py:714` 三处交叉核对
才纠正。**这与 §1.1 是同一种病**——过期注释被当作现状引用。可见该失效模式在本仓
并非孤例，故订正范围包含它。

另需在 pi 升级 checklist 中追加：**重验 §6 断言表**。

## 9. 非目标

- **stdio MCP server**（沿用 v2 spec §1：不执行任意用户命令）
- **OAuth 流程、MCP resources / prompts / sampling**（只做 tools，沿用 v2 spec §11）
- **gateway tool 模式 / 两阶段 spawn**（见 §2.1 评估，已否）
- **iOS 能力提示**：本设计消除了能力缺口本身，故不再需要「当前路线不支持 MCP」的
  提示。若未来出现新的无桥 driver，再单独评估。

## 10. 配套文档变更

- `docs/CHANGELOG.md`：落地后按惯例记 landmark，**含 §1.1 的误读链**（这是流程教训，
  不只是功能记录）。
- `io-onboarding/skill-resident-agent.md`：user-mcp.json 一节补充 pi 路线说明。
- `docs-site`：本改动不涉及公开 API 契约与部署拓扑，无需变更。
