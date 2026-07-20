# 自托管 hermes / OpenClaw 用户的 user-MCP 接线

日期：2026-07-18
状态：方向已与用户确认，待写实现计划
前置：`docs/superpowers/specs/2026-07-08-user-mcp-servers-design.md`（下称「v2 spec」）、
`docs/superpowers/specs/2026-07-17-pi-user-mcp-bridge-design.md`（下称「pi 桥 spec」）。
本文补上 v2 spec 与 pi 桥 spec 都没覆盖的一条 driver：**hermes**（及其历史别名
openclaw）。

## 0. 一句话

hermes 原生支持 MCP（读 `~/.hermes/config.yaml` 的 `mcp_servers`），但 feedling 的
consumer 从没给它接线——物化只写 claude/codex 目标、CA 只注入 Node。本设计给
`_materialize_user_mcp` 加一个 hermes 目标（pyyaml merge 进 config.yaml），并让
`_user_mcp_child_env` 把 hermes 并进 codex 的 `SSL_CERT_FILE` 分支（同为 python）。
**数据链路一行不改**，唯一实质新代码是一个纯函数 `hermes_config_merged`。

## 1. 背景：一个被三条 driver 分支漏掉的 driver

用户反馈「我的 VPS（hermes）在 app 上加了 MCP 端点，但 agent 用不上」。逐层核对确认
不是配置问题，是**能力缺口**：

- `_user_mcp_cli_value` 第一行 `if "{mcp}" not in template: return ""`，而 hermes 的
  CLI 模板 `hermes chat -Q --source tool --max-turns 60 -q "{message}"` **没有 `{mcp}`
  占位符** → 对 hermes 永远返回空
- `_materialize_user_mcp` 只物化 **claude json + claude settings.json + codex
  config.toml** 三个目标，**没有 hermes 目标**
- `_user_mcp_child_env` 给非 codex、非 pi 的命令（含 hermes）走 `NODE_EXTRA_CA_CERTS`
  分支——那是 **Node.js 的**环境变量，对 python 的 hermes 完全无效

三处叠加的结果：hermes 用户配了 MCP server，**下发链路会把配置同步到机器**（那部分
driver 无关），但落不到 hermes 能读的位置，且 CA 也没接对。pi 桥 spec 只解决了 pi，
hermes 仍在缺口里。

### 1.1 role="openclaw" 是历史标签，但 OpenClaw runtime 是独立的

起草时曾把两个不同的东西混为一谈，误判成"openclaw 是 hermes 别名、给 hermes 接线即
覆盖"——**这是错的，已订正**：

- **role="openclaw"** —— 聊天回复的历史 role 标签。`status_core.py:20` 注释：
  "legacy from when the only supported agent was OpenClaw"。所有 CLI-driver 回复默认
  落这个 role，**和 runtime 无关**。（`tools/README.md` 里"openclaw 用户跑 hermes-agent"
  的样例误导了起草——那只是某个 openclaw**账号**恰好用 hermes 传输，不代表 runtime 相同。）
- **OpenClaw runtime** —— 一个**独立的 Node.js agent**（`npm install -g openclaw`），
  不是 hermes。证据：`deploy/openclaw-plugins/feedling-io-tools/index.js` 依赖
  `openclaw/plugin-sdk`；配置文件是 `~/.openclaw/openclaw.json`（**不是** config.yaml）；
  io-onboarding 明确把 Hermes 与 OpenClaw 当两个 sibling runtime。

**docker 实证**（2026-07-18，`node:24-slim` + `npm i -g openclaw@2026.7.1-2`）：
OpenClaw 读 `~/.openclaw/openclaw.json` 的 `mcp.servers`（JSON 嵌套），格式
`{name:{url,transport:"streamable-http",headers}}`；`openclaw agent --local` 每回合
自动加载并调用；工具命名 `<server>__<tool>`。端到端跑通（deepwiki 的
`read_wiki_structure` 真被模型调用，返回真实仓库文档树）。

结论：**OpenClaw 需要独立接线**（物化 openclaw.json，不是 config.yaml；CA 走 Node 的
`NODE_EXTRA_CA_CERTS`），见 §4a。它当前无真实用户（Seven 2026-07-17），但 docker 环境
可端到端验证，故本期与 hermes 一并做，不再是"没法测的死代码"。

## 2. 已验证的事实（本设计成立的基础）

以下断言均已在 VPS（真机 hermes）或本地实测得出：

| 断言 | 证据 |
|---|---|
| hermes 原生支持 MCP，读 `~/.hermes/config.yaml` 的 `mcp_servers` | `hermes --help` 有 `mcp` 子命令；`native-mcp.md` 明确 |
| HTTP transport 格式：`{name: {url, headers, timeout, connect_timeout}}` | `native-mcp.md` 的 "HTTP Transport (url)" 段 |
| 工具注册命名 `mcp_{server}_{tool}` | `native-mcp.md` "Tool Naming Convention"，与 pi 桥一致 |
| hermes 每回合 spawn 重读 config.yaml（启动 `discover_mcp_tools`） | `native-mcp.md` "Startup Discovery" |
| hermes 用 `mcp` python SDK 的默认 httpx client 连 server | venv 里 `site-packages/mcp/`；`create_mcp_http_client` 不传 verify |
| **httpx 0.28 默认 client 读 `SSL_CERT_FILE`** | 本地实测：不设→自签 `CERTIFICATE_VERIFY_FAILED`；设自签 cert→200 |
| `hermes mcp add` **交互式 + discovery 阻塞**，无人值守不可用 | 真机实测：三处等 TTY 输入（token / auth Y-n / enable Y-n）；喂空输入 `Cancelled`，config 空；耗时 10.6s 连 server |
| 前提：hermes 需 `pip install mcp`，否则静默禁用 MCP | `native-mcp.md` "Prerequisites" |
| pyyaml 6.0.3 已在 `backend/requirements.lock` | consumer 靠 `-r ../backend/requirements.txt` 拿到，零新增依赖 |

**时效性声明**：上表绑定当前 VPS 上的 hermes-agent 版本与 `mcp` SDK / httpx 0.28。
hermes 或 mcp SDK 升级时须重验——尤其 §2 的 `SSL_CERT_FILE` 行为（httpx 未来版本若
改默认 TLS 构造，自签方案要重评）。

## 3. 范围与已定决策

| 决策点 | 结论 | 理由 |
|---|---|---|
| 覆盖 driver | hermes + **OpenClaw**（两个独立 runtime） | §1.1；各读各的配置文件，各需物化目标 |
| OpenClaw 接线 | 物化 `openclaw.json` 的 `mcp.servers`（JSON merge）+ CA 走 Node `NODE_EXTRA_CA_CERTS` | 实证 OpenClaw 是 Node runtime，见 §4a |
| OpenClaw CLI 方案 | 可选（`mcp add --no-probe` 非交互可用），但**选直接 JSON merge** 与 hermes 对称 | `--no-probe` 不阻塞，但 CLI 每 server 一次 shell + 删要 unset；文件 merge 更简单一致 |
| 物化写法 | **pyyaml 解析 merge**，非 managed-block 文本追加 | YAML 的 `mcp_servers:` 是单顶层 map key，追加第二个会重复 key；codex 的 TOML 多 table 可散布故用文本追加，hermes 不行 |
| CLI 方案 | **否决** | §2 实测：`hermes mcp add` 交互式 + discovery 阻塞，违反"MCP 故障不能拖累主链路" |
| CA / 自签 | 复用 **codex 的 `SSL_CERT_FILE=castore`** | hermes 与 codex 同为 python，§2 实测 httpx 读 `SSL_CERT_FILE`；castore（`certifi 系统 CA` + `用户 CA` concat）已由现有 `_write_user_mcp_ca` 生成。**castore 仅在有自签 CA 时生成**：无自签→castore 不存在→hermes 不设 `SSL_CERT_FILE`→回退默认 certifi 连正规 HTTPS；有自签→castore 含系统+用户 CA→正规与自签都 work（与 codex 现有语义完全一致） |
| config.yaml 定位 | `HERMES_CONFIG_DIR`，默认 `~/.hermes` | hermes home 是稳定约定，默认它可让 VPS 用户零配置生效；`is_dir()` 守卫避免误伤非 hermes 部署 |
| 注释保留 | pyyaml round-trip（**会丢注释**）+ **写前备份** `.feedling-bak` | pyyaml 已有依赖；ruamel 保留注释但要加依赖，YAGNI |
| config.yaml 缺失 | 目录在但无 config.yaml → 创建只含 `mcp_servers` 的最小文件 | hermes 其他配置走默认，零配置生效 |
| 传输类型 | 仅 HTTP，不做 stdio | 沿用 v2 spec §1 |
| 数据链路 | **零改动**（存储/下发/物化触发/信封/指纹/castore 全复用） | — |

## 4. 架构

### 4.1 改动清单（4 处，全在 consumer 侧）

| 文件 | 改动 |
|---|---|
| `tools/user_mcp_materialize.py` | **新增纯函数** `hermes_config_merged(existing_yaml, servers, managed_names) -> str` |
| `tools/chat_resident_consumer.py` `_materialize_user_mcp` | 加 hermes 目标：`HERMES_CONFIG_DIR`（默认 `~/.hermes`）存在就 merge 写 `config.yaml` |
| `tools/chat_resident_consumer.py` `_user_mcp_child_env` | hermes 并进 codex 分支：`_is_codex_cmd(cmd) or _is_hermes_chat_cmd(cmd)` → `SSL_CERT_FILE=castore` |
| `tools/README.md` + `io-onboarding/skill-resident-agent.md` | 文档：hermes MCP 前提（`pip install mcp`）+ 行为 |

`_user_mcp_cli_value` **不改**：hermes 通过 config.yaml 原生发现工具，不需要 CLI 注入
参数，故 hermes 模板保持无 `{mcp}`、该函数对 hermes 继续返回空即正确。

### 4.2 组件：`hermes_config_merged`（唯一实质新代码）

纯函数，无 I/O，与 `codex_config_merged` 对称、可独立单测：

```
existing config.yaml 文本（或 None）
  → yaml.safe_load → dict（顶层其他 key 原样保留）
  → mcp_servers = dict.get("mcp_servers") or {}
  → 删掉 mcp_servers 里 key ∈ managed_names 的条目（prune 我们曾管的）
  → 对每个 enabled server：mcp_servers[name] = {"url": url, **({"headers": h} if h else {})}
  → 若 mcp_servers 非空则写回该 key，否则删除该 key
  → yaml.safe_dump(doc, sort_keys=False, allow_unicode=True) → 返回文本
```

- **prune 语义**与 codex/claude 的 `managed_names` 一致：只动我们管的 server（当前 +
  曾经 applied 的名字），保留用户手动加进 `mcp_servers` 的条目
- `sort_keys=False` 保留顶层 key 顺序，`allow_unicode=True` 避免中文/token 被转义
- **已知代价**：pyyaml 不保留注释与原始格式（缩进风格、空行）。缓解见 §4.3 备份

### 4.3 `_materialize_user_mcp` 的 hermes 目标

对齐现有 codex/claude 目标的结构（`os.environ.get(...)` + 目录守卫）：

```python
hermes_dir = os.environ.get("HERMES_CONFIG_DIR") or str(Path.home() / ".hermes")
if Path(hermes_dir).is_dir():
    cfg_path = Path(hermes_dir) / "config.yaml"
    existing = cfg_path.read_text() if cfg_path.exists() else None
    merged = _m.hermes_config_merged(existing, servers, managed_names)
    _atomic_write_with_backup(cfg_path, merged)   # 备份 + 临时文件 + os.replace
```

- **原子写**：先写 `config.yaml.tmp`，`os.replace` 原子换上；换之前把原 `config.yaml`
  复制成 `config.yaml.feedling-bak`
- **目标隔离**：整段包在自己的 try/except 里，失败只 log 不抛——不影响 claude/codex
  目标，也不 wedge chat（`_maybe_apply_user_mcp` 顶层 try 是最后兜底）

## 4a. OpenClaw 接线（独立 runtime，镜像 hermes）

OpenClaw 是独立 Node runtime，机制与 hermes 平行但文件/格式/CA 都不同。全部 docker 实证（§1.1）。

### 4a.1 改动清单（3 处，与 hermes 并列）

| 文件 | 改动 |
|---|---|
| `tools/user_mcp_materialize.py` | 新增纯函数 `openclaw_config_merged(existing_json, servers, managed_names) -> str` |
| `tools/chat_resident_consumer.py` `_materialize_user_mcp` | 加 OpenClaw 目标：`OPENCLAW_CONFIG_DIR`（默认 `~/.openclaw`）存在就 merge 写 `openclaw.json` |
| `tools/chat_resident_consumer.py` `_user_mcp_child_env` | **无需改代码**：openclaw（非 codex 非 hermes）天然落 `NODE_EXTRA_CA_CERTS` else 分支；只加测试守卫 |

### 4a.2 `openclaw_config_merged`（JSON merge，非 pyyaml）

openclaw.json 是 JSON，`mcp.servers` 是**嵌套**（`doc["mcp"]["servers"]`），不是顶层：

```
existing openclaw.json 文本（或 None）
  → json.loads → dict（顶层其他 key commands/agents/cron/meta 原样保留）
  → mcp = doc.get("mcp") or {}; servers_map = mcp.get("servers") or {}
  → 删掉 servers_map 里 key ∈ managed_names 的条目
  → 对每个 enabled server：
      servers_map[name] = {"url": url, "transport": "streamable-http",
                           **({"headers": h} if h else {})}
  → 非空 → doc["mcp"] = {**mcp, "servers": servers_map}；空 → 从 mcp 删 servers（mcp 变空则删 mcp）
  → json.dumps(doc, indent=2, ensure_ascii=False) → 返回文本
```

与 hermes 三点差异：① JSON 非 YAML（无注释丢失问题，但仍写前备份 `.feedling-bak` 保险）；
② server 条目多一个 `transport: "streamable-http"`（OpenClaw 必需）；③ 嵌套 `mcp.servers`
而非顶层 `mcp_servers`。prune 语义（只动 managed_names、保留用户手配）与 hermes 一致。

### 4a.3 CA 与 AGENT_CLI_CMD

- **CA**：OpenClaw 是 Node → 走现有 `NODE_EXTRA_CA_CERTS`（ADD 语义，用户 CA only，与
  claude/pi **完全同分支**）。`_user_mcp_child_env` 的 else 分支已是 catch-all（非 codex
  非 hermes），openclaw cmd 天然落进去——**无需改 child_env 代码**，只加一个测试守卫确认
  openclaw cmd → `NODE_EXTRA_CA_CERTS`（防未来回归）。故 openclaw 只有 2 处实质代码改动
  （`openclaw_config_merged` + materialize 目标），比 hermes 还省。
- **AGENT_CLI_CMD**（供 onboarding/运维，非本代码改动）：
  `openclaw agent --local --json --session-id {session_id} -m {message} --model <provider/model>`；
  OpenClaw 自动认标准 provider env（实证 `OPENROUTER_API_KEY` 开箱即用）。
- **生效时机**：`openclaw agent --local` 每回合自动加载 `mcp.servers`（docker 实证，新
  session 直接调用了工具）。OpenClaw 另有 `mcp reload`（gateway 模式清缓存）——纯 `--local`
  路径是否需要 reload **待 §8 的 docker E2E 复核**（物化后紧接下一 turn 是否即可见）。

## 5. 数据流

配置分发段**完全不动**：

```
iOS → POST /v1/mcp/servers → mcp_core 存信封 → poll 广播 fingerprint
  → _maybe_apply_user_mcp 发现变化 → 拉信封 → enclave 解密 → _materialize_user_mcp
      ├ USER_MCP_FILE(claude json)     [现有]
      ├ claude settings.json           [现有]
      ├ codex config.toml              [现有]
      ├ hermes config.yaml  ← 新增
      └ castore bundle(用户CA+系统CA)   [现有]
```

hermes 回合（新增仅 CA 分支 + 依赖 config.yaml 已被物化）：

```
lane 无关（hermes 无 {mcp} 门控）→ _user_mcp_child_env → SSL_CERT_FILE=castore
  → spawn hermes → discover_mcp_tools 读 config.yaml → 并发连各 server（castore 验 TLS）
  → 注册 mcp_{server}_{tool} → 模型看到工具 → 调用
```

**生效时机**：hermes 每回合 spawn 重读 config.yaml，用户在 app 加 server、下一条消息
即生效，与 claude/pi 同构，无需额外 reload。

**门控差异（记录在案）**：claude/pi 用 lane 门控（chat 注入、background 不注入），
以免 proactive 回合动用户第三方 quota。hermes 走 config.yaml 原生发现，**无 per-lane
门控**——一旦物化，proactive 回合的 hermes 也会看到工具。这是 hermes 原生机制的固有
差异，本设计不改 hermes；若未来要给 hermes 做 lane 门控，需在物化侧按 lane 写/清
config.yaml，属独立议题（非本期）。

## 6. 错误处理

| 场景 | 处理 |
|---|---|
| merge / 写 config.yaml 失败 | 原子写（tmp + `os.replace`）+ 写前 `.feedling-bak`；失败只 log 不抛 |
| hermes 目标抛异常 | 独立 try/except，不影响 claude/codex 目标 |
| `~/.hermes` 目录不存在 | 跳过（不误伤非 hermes 部署） |
| 目录在但 config.yaml 缺失 | 创建只含 `mcp_servers` 的最小文件（hermes 其他配置走默认） |
| hermes 未装 `mcp` 包 | 代码保证不了；物化时若探测到未装则 log 提示，文档必写 |
| 顶层兜底 | `_maybe_apply_user_mcp` 已有 "config refresh must never wedge chat" try |

## 7. 前提与非目标

**前提（文档必写）**：
- hermes：VPS 上需 `pip install mcp`，否则静默禁用 MCP。
- OpenClaw：MCP client 内建，无需额外装（实证 `openclaw@2026.7.1-2` 开箱即连）。

**非目标**：
- stdio transport（只做 HTTP，沿用 v2 spec §1）
- ruamel 保留注释（先 pyyaml + 备份，YAGNI）
- per-lane 门控：hermes（§5）与 OpenClaw（`--local` 每 turn 都加载 `mcp.servers`）都无
  per-lane 门控，独立议题
- OAuth / MCP resources / prompts / sampling（沿用 v2 spec §11）

## 8. 测试

- **单测 `hermes_config_merged`**（纯函数）：空/None config、保留其他顶层 key、prune
  managed_names、enabled 过滤、headers 透传（含空 headers）、重复调用幂等、
  `allow_unicode` 不转义中文
- **单测 `_user_mcp_child_env`**：hermes cmd → `SSL_CERT_FILE=castore`；无 castore
  文件 → 不注入；确认 hermes 不再拿 `NODE_EXTRA_CA_CERTS`
- **单测 `_user_mcp_cli_value`**：hermes（无 `{mcp}`）→ 空（守卫，确认 pi 分支不误伤
  hermes）
- **单测 `_materialize_user_mcp`**：给定 servers → 断言写出的 config.yaml 内容与
  `.feedling-bak` 生成；目录不存在时不写；config.yaml 缺失时创建最小文件
- **CI 接线**：把上述测试文件纳入 `ci.yml` 的 pytest 白名单（`test_user_mcp_consumer.py`
  已在，新断言随之跑；若新增测试文件须显式加入——沿用 pi 桥 spec §7 的教训）
- **单测 `openclaw_config_merged`**（纯函数）：空/None、保留其他顶层 key（commands/agents/
  cron/meta）、嵌套 `mcp.servers`、prune managed_names、`transport:"streamable-http"` 字段、
  headers 透传（含空）、幂等
- **单测 `_user_mcp_child_env`**：openclaw cmd → `NODE_EXTRA_CA_CERTS`（用户 CA）；确认它
  **不**进 codex/hermes 的 `SSL_CERT_FILE` 分支
- **单测 `_materialize_user_mcp`**：`OPENCLAW_CONFIG_DIR` 存在 → 写 openclaw.json 的
  `mcp.servers` + 备份；目录不存在不写；openclaw.json 缺失时创建最小文件
- **真机 E2E**（用户 VPS）：app 加公共 server（deepwiki）→ 物化 config.yaml → 发消息
  验证 hermes 调用工具（端到端验证「同步」）；自签（「HTTPS/自签」）本地已确定性验证
  （httpx + `SSL_CERT_FILE`），VPS 端到端仍需一个云可达的自签 MCP server
- **docker E2E（OpenClaw）**：`scratchpad/mcp-verify/` 的 openclaw-lab 容器 →
  consumer `_materialize_user_mcp` 写 openclaw.json → `openclaw agent --local` 发一个
  用工具的 turn，确认调用 `<server>__<tool>`；**复核生效时机**（物化后是否需 `mcp reload`）

## 9. 配套文档

- `docs/CHANGELOG.md`：落地后记 landmark（含 §1 的三分支缺口链 + §2 CLI add 证伪）
- `tools/README.md`：hermes/openclaw 自托管一节补 MCP 前提与行为
- `io-onboarding/skill-resident-agent.md`：user-mcp.json 一节补 hermes 路线说明
- `docs-site`：本改动不涉及公开 API 契约与部署拓扑，无需变更
