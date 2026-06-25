# Agent Runtime 计划：Claude Agent SDK / Codex

日期：2026-06-24

本文描述 Feedling 如何为 API 用户运行一个真正的 hosted agent loop，同时让
API 用户和 VPS/resident 用户尽量走同一套 runtime、工具和上下文能力。

## 结论

在 Phala compose 里新增一个 `agent-runner` 服务。这个服务里运行
`AgentSupervisor`，由它为活跃用户按需启动一个 consumer 子进程。每个
consumer 驱动一个 Claude Agent SDK session 或 Codex session，并通过同一批
Feedling 工具访问 screen / memory / identity / perception。

默认不要做“每个用户一个 Docker container”。

默认形态：

```text
Phala CVM
  ingress
  backend
  enclave
  agent-runner
    AgentSupervisor
      consumer[u_123] -> Claude/Codex session
      consumer[u_456] -> Claude/Codex session
```

每用户 Docker 或 microVM 隔离可以作为后续高价值/高风险用户的增强方案，
不作为第一版默认路径。

## 目标

- API 用户拥有真实 agent loop，而不是一次 LLM call + 手搓 JSON 协议。
- VPS 用户和 API 用户使用同一批 screen / memory / identity / perception 工具。
- 每个用户拥有独立 runtime home、session state、logs、workspace 和短期
  Feedling runtime token。
- backend 继续作为加密 chat、memory、identity、wake job、delivery policy、
  push 的唯一事实源。
- runtime 按需启动；不活跃用户不常驻 agent 进程。

## 非目标

- 不用第三方框架替换 Feedling 的 `WakeEventV2`、lease、delivery 逻辑。
- 不给 hosted agent 直接访问 backend 数据库或 enclave 的权限。
- 不让多个用户共享同一个 Claude/Codex home 目录。
- 不把长期 provider key 写进 env 文件、runtime 目录或日志。
- 不把 Docker socket 暴露给 Flask backend。

## Runtime 拓扑

新增第四个 Phala 服务：

```yaml
agent-runner:
  image: ghcr.io/teleport-computer/feedling-agent-runner:<digest>
  command: ["python", "-u", "backend/agent_runtime/supervisor.py"]
  restart: unless-stopped
  environment:
    FEEDLING_API_URL: "http://backend:5001"
    FEEDLING_ENCLAVE_URL: "https://enclave:5003"
    DATABASE_URL: "${DATABASE_URL:-}"
  volumes:
    - feedling_agent_runtime:/agent-data
  depends_on:
    backend:
      condition: service_started
    enclave:
      condition: service_healthy
```

`agent-runner` 镜像建议包含：

- consumer 需要的 Feedling backend/client 代码。
- Python 3.11+。
- 使用 TypeScript SDK 或 CLI wrapper 时需要 Node.js。
- Claude driver 所需的 `claude-agent-sdk`。
- Codex driver 所需的 Codex SDK / Codex CLI。
- Feedling MCP server 或 HTTP tool gateway。

每用户 runtime 目录：

```text
/agent-data/users/<user_id>/
  runtime.json
  claude-home/
  codex-home/
  workspace/
  logs/
  tmp/
```

`runtime.json` 只存非秘密状态：

```json
{
  "driver": "claude",
  "status": "idle",
  "session_ref": "session id or thread id",
  "last_active_at": "2026-06-24T12:00:00Z",
  "last_heartbeat_at": "2026-06-24T12:00:00Z"
}
```

## 进程模型

Supervisor 为每个活跃用户启动一个 consumer 进程：

```bash
feedling-agent-consumer \
  --user-id u_123 \
  --driver claude \
  --runtime-home /agent-data/users/u_123 \
  --feedling-url http://backend:5001
```

生命周期：

```text
message/wake 到达
  -> supervisor 抢占该用户 runtime lease
  -> 如果 consumer 不存在，则 spawn
  -> consumer 处理用户 mailbox
  -> consumer 运行时持续 heartbeat
  -> 空闲 10-30 分钟后退出
  -> session_ref 和 status 持久化
```

lease 必须跨 worker 安全，使用 Postgres，不用内存状态：

```sql
CREATE TABLE agent_runtime_instances (
  user_id text PRIMARY KEY,
  driver text NOT NULL,
  status text NOT NULL,
  pid integer,
  lease_owner text,
  lease_expires_at timestamptz,
  session_ref text,
  runtime_home text NOT NULL,
  last_heartbeat_at timestamptz,
  last_active_at timestamptz,
  error text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

只有当前 lease owner 可以 spawn、heartbeat、stop 或更新该用户的
`session_ref`。

## Consumer Loop

consumer 要尽量小。它不拥有产品策略，只负责把 Feedling event 交给真实
agent，并把 agent 输出写回 Feedling。

```text
while running:
  poll 这个用户的 Feedling event inbox
  如果没有 event:
    idle timeout 后退出
  根据 event 构造 agent input
  driver.run_turn(input)
  把最终可见 agent message 映射成 Feedling chat reply
  持久化 tool/action trace
  保存 session_ref
```

consumer 应通过 backend HTTP API 调用 Feedling，不要 import backend 全局对象。
这样 runner worker 不依赖 Flask request state。

## 工具层

给 resident agent 和 hosted API agent 暴露同一批工具：

```text
feedling_context_snapshot
feedling_screen_latest
feedling_memory_search
feedling_memory_get
feedling_memory_write
feedling_identity_get
feedling_identity_patch
feedling_send_message
feedling_sleep
feedling_schedule_wake
```

优先 transport：

- Claude / Codex 可用 MCP 时优先 MCP。
- 某些 driver 环境里 MCP 不方便时，提供 HTTP fallback。

每次工具调用都必须带一个短期 runtime token，token 只允许访问一个用户：

```text
user_id
runtime_instance_id
allowed_tools
expires_at
```

agent 不应该拿到用户长期 Feedling API key。

## Claude Driver

如果目标是“hosted 版 Claude Code-like agent loop”，第一版推荐先做
Claude Agent SDK driver。

原因：

- agent loop 跑在我们的进程/基础设施里。
- 支持 built-in tools、hooks、MCP、permissions、sessions、subagents。
- session ID 可以保存并恢复。

Claude runtime 启动流程：

```text
consumer[u_123]
  -> 获取用户 runtime token
  -> 只在本次 consumer/turn 中解密用户 Anthropic provider key
  -> 在 child/session 环境里设置 ANTHROPIC_API_KEY
  -> 启动 Claude Agent SDK query/resume
  -> 挂载 Feedling MCP server
  -> 流式处理 agent events
```

Token 处理：

- 用户 Anthropic API key 复用现有 model API config envelope 加密存储。
- 只有启动 consumer turn 时才通过 enclave 解密。
- 只通过 `ANTHROPIC_API_KEY` 传给 Claude child/session。
- 不把 `ANTHROPIC_API_KEY` 写进 `/agent-data`、`.env`、日志、trace 或
  runtime JSON。
- 进程退出时清理 child env。

重要产品/法律边界：

- Claude Agent SDK 官方文档描述的是用 `ANTHROPIC_API_KEY` 做 API key auth。
- 官方文档也说明，除非提前获批，第三方产品不应向自己的用户提供
  claude.ai 登录或 claude.ai rate limit。
- 所以 Feedling hosted runtime 应使用 Anthropic API key 或获批的 provider
  集成，不要设计成复用用户个人 Claude App 登录态。

第一版实现形态：

```python
async for event in query(
    prompt=agent_input,
    options=ClaudeAgentOptions(
        resume=session_ref,
        mcp_servers={
            "feedling": {
                "command": "python",
                "args": ["-m", "feedling_agent_runtime.mcp_server"],
                "env": {
                    "FEEDLING_API_URL": "http://backend:5001",
                    "FEEDLING_RUNTIME_TOKEN": runtime_token
                }
            }
        },
        allowed_tools=[
            "feedling__context_snapshot",
            "feedling__screen_latest",
            "feedling__memory_search",
            "feedling__memory_get",
            "feedling__identity_get",
            "feedling__send_message"
        ],
    ),
):
    handle_event(event)
```

具体工具名取决于 MCP SDK 的命名规则。

## Codex Driver

Codex 有三种接入层级：

1. 每个 turn 跑一次 `codex exec`。
2. Codex SDK。
3. `codex app-server`，用 JSON-RPC 控制 thread/turn。

建议分两条 track 做：

### Track A：`codex exec` spike

第一版 Codex POC 先用这个，因为 API key 注入最简单。

```text
CODEX_HOME=/agent-data/users/u_123/codex-home
CODEX_API_KEY=<decrypted OpenAI key>
codex exec --json --sandbox read-only "<agent input>"
```

注意：

- Codex 官方手册明确写了 `CODEX_API_KEY` 支持 `codex exec`。
- 只在单次 invocation 里 inline 设置，不写入文件。
- 用 `--json` 消费结构化事件流。
- reply contract 稳定后可以加 `--output-schema`。
- 每个用户使用独立 `CODEX_HOME`，不能共享全局 `~/.codex`。

这个 track 的长期 session 能力可能弱于 SDK/app-server，但最适合验证：
“用户提供 OpenAI API key 后，我们能否在 hosted 环境里安全运行 Codex”。

### Track B：Codex SDK / app-server spike

如果需要 persistent thread 和更完整 streaming，再做这个。

```text
consumer[u_123]
  -> CODEX_HOME=/agent-data/users/u_123/codex-home
  -> 启动 Codex SDK 或 app-server
  -> thread_start 或 thread_resume
  -> turn_start with Feedling input
  -> stream notifications
```

开放问题：

- hosted 多租户、用户自带 OpenAI key 的场景下，Codex SDK/app-server 的生产
  鉴权方式需要单独 spike 确认。官方手册清楚说明了 `CODEX_API_KEY` 可用于
  `codex exec`；SDK/app-server 这条不能拍脑袋假设。

这个 spike 完成前，不要围绕一个共享全局 Codex login 设计生产架构。

## Token 模型

要严格区分三类 token。

### 1. 用户 provider key

例子：

- Claude Agent SDK 使用的 Anthropic API key。
- Codex `exec` 或其它 OpenAI runtime path 使用的 OpenAI API key。

存储：

- 复用现有 model API config envelope 加密存储。
- `GET` 接口永远不返回明文。
- 只在 CVM/enclave-backed runtime path 内解密。

使用：

- just-in-time 注入 child process 环境。
- 尽量限制在一个 process/turn 内。
- 永不持久化到每用户 runtime home。

### 2. Feedling runtime token

用途：

- 让 agent 调 Feedling tools，且只能访问一个用户。

示例属性：

```json
{
  "sub": "runtime_instance_id",
  "user_id": "u_123",
  "scope": ["screen:read", "memory:read", "chat:write"],
  "exp": 1234567890
}
```

规则：

- backend 或 supervisor 在抢到 user runtime lease 后 mint。
- TTL 短，例如 10-30 分钟。
- consumer 退出后 token 过期或被撤销。
- MCP tools 必须校验 token 的 `user_id` 和请求资源一致。

### 3. Internal supervisor token

用途：

- 让 `agent-runner` 抢 lease、mint per-user runtime token。

规则：

- 通过 Phala encrypted env 注入 `agent-runner` 服务。
- 不传给 per-user child process。
- 可以独立轮换，不影响用户 provider key。

## 构建组织

建议新增一个小 package：

```text
backend/agent_runtime/
  __init__.py
  supervisor.py
  consumer.py
  drivers/
    base.py
    claude_agent_sdk.py
    codex_exec.py
    codex_app_server.py
  mcp_server.py
  tokens.py
  leases.py
  events.py
```

职责：

- `supervisor.py`：lease loop、spawn/stop child process、heartbeat monitor。
- `consumer.py`：每用户 event loop 和 driver orchestration。
- `drivers/base.py`：统一 `AgentDriver` interface。
- `drivers/claude_agent_sdk.py`：Claude Agent SDK 实现。
- `drivers/codex_exec.py`：Codex 第一版 spike，使用 `codex exec`。
- `drivers/codex_app_server.py`：后续 persistent Codex 实现。
- `mcp_server.py`：暴露给 Claude/Codex 的 Feedling tools。
- `tokens.py`：runtime token mint/verify。
- `leases.py`：DB-backed runtime lease。
- `events.py`：poll/claim Feedling messages 和 wake events。

Driver interface：

```python
class AgentDriver:
    async def start(self, state: RuntimeState) -> None: ...
    async def run_turn(self, event: AgentEvent) -> AgentTurnResult: ...
    async def stop(self) -> None: ...
```

结果结构：

```python
@dataclass
class AgentTurnResult:
    visible_messages: list[str]
    session_ref: str | None
    tool_trace: list[dict]
    usage: dict
    error: str = ""
```

## 分阶段计划

### P0：单用户本地原型

- 实现 `consumer.py`，先 hard-code 一个测试用户。
- 先实现一个 driver，推荐 Claude Agent SDK。
- 通过 MCP 或 direct HTTP 暴露 Feedling tools。
- 本地连 backend 跑通。
- 验收：iOS 发一条 chat message，agent 收到并回复。

### P1：test Phala 上 agent-runner 服务

- 在 compose 里加 `agent-runner`。
- 加 runtime instances DB table。
- 加 supervisor lease 和 process management。
- 加每用户 runtime home。
- 加 runtime token mint/verify。
- 验收：两个用户能并发聊天，且 session/home/logs 不共享。

### P2：Codex spike

- 实现 `codex_exec` driver，per-turn 使用 `CODEX_API_KEY`。
- 评估启动延迟、session 连续性、MCP 兼容性和成本。
- 决定是否需要 SDK/app-server。

### P3：Hosted model_api cutover

- 在 per-user flag 后替换当前 hosted JSON contract：
  `hosted_agent_runtime_driver = claude | codex | legacy`。
- 保持 `/v1/model_api/chat/send` 外部 API 稳定。
- 短 turn 同步等待；慢 turn 返回 processing。
- 保留 legacy hosted path 作为 rollback。

### P4：Proactive wake 统一

- proactive wake events 也进入同一个 consumer。
- agent 通过同一批工具决定 send message、sleep、schedule wake 或继续请求上下文。

### P5：可选强隔离

- 只为需要强隔离的用户增加 per-user container / microVM。
- 这是单独的安全设计，不混入第一版默认方案。

## 安全要求

- child process 使用非 root 用户运行。
- 每用户独立 runtime home 和 workspace。
- runtime token 短期、带 scope、绑定 user_id。
- provider key 不落盘。
- 日志必须 redact provider key、Feedling runtime token、用户 API key，以及非用户可见的明文私密内容。
- tool calls 按 driver/profile allowlist。
- `send_message`、`memory_write`、`identity_patch` 应支持策略门和可选用户确认。
- idle consumer 被 kill 后，进程 env 随之丢弃。

## 运维要求

- 每用户最大并发 turn：1。
- 每节点最大 active consumer 数可配置。
- 每 turn timeout 可按 driver 配置。
- idle timeout：10-30 分钟。
- heartbeat interval：15-30 秒。
- crash recovery：lease 过期后另一个 supervisor 可以重启该用户。
- 观测指标：
  - active consumer count
  - spawn failures
  - turn latency
  - provider errors
  - tool call count
  - token decrypt failures
  - idle exits

## 开放问题

- 只允许用户自带 Anthropic/OpenAI keys，还是也提供 Feedling-managed provider billing？
- Codex SDK/app-server 是否支持 hosted 多租户下用户自带 OpenAI key 的精确鉴权模型？
  如果不确定，Codex 是否先只做 `codex exec`？
- Feedling tools 走 MCP-only、HTTP-only，还是双 transport？
- 哪些 tool calls 必须进入用户确认？
- hosted Claude/Codex 应该拥有多少 filesystem 权限？默认应是 read-only +
  Feedling tools，而不是宽泛 writable workspace。

## 参考

- Claude Agent SDK overview: https://code.claude.com/docs/en/agent-sdk/overview
- Codex SDK: https://developers.openai.com/codex/sdk
- Codex app-server: https://developers.openai.com/codex/app-server
- Codex MCP: https://developers.openai.com/codex/mcp
- Codex non-interactive mode: https://developers.openai.com/codex/noninteractive
