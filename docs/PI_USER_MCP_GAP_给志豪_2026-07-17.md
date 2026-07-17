# pi driver 不支持用户 MCP —— 交接给志豪（2026-07-17）

Seven 定：这个归志豪修，Claude/codex 侧不动。以下是已完成的诊断，省一遍排查。

## 用户报告

usr_6f5a125f8c447533（model_api 托管，openai_compatible relay，pi driver）：
在 app 里连了 "ombre brain" MCP server，AI 说看不到这个工具。

## 根因（已核实，非配置错误）

用户 MCP 链路对 pi driver 断在最后一环：

1. 后端存储/下发正常：`backend/hosted/mcp_core.py` 驱动无关，配置已保存、
   fingerprint 随每次 chat poll 广播。✅
2. consumer 物化正常：`tools/chat_resident_consumer.py` 的 user_mcp 区块
   会把配置写成文件。✅
3. **断点**：`_user_mcp_cli_value()`（consumer ~5788）只有 claude
   （`--mcp-config`）和 codex（config.toml + `-c` 覆盖）两个分支；
   **hosted pi 模板没有 `{mcp}` 占位符**（`backend/agent_runtime/
   spawners.py:479-483`）→ 对 pi 永远返回空 → 工具到不了 agent。

## 为什么是空白（设计时已知）

user MCP spec（`docs/superpowers/specs/2026-07-08-user-mcp-servers-design.md`）
明写：当时 test 无 pi driver，"pi 本期不涉及"；§282-285 列了后续方案——
**pi 官方不支持 MCP（README 明示走 extension 路径），但有 `pi.registerTool()`
API，可写 extension 把用户 MCP server 桥接成 pi 工具，数据模型与下发链路
无需改动**。后来 pi driver 上线（gemini/openrouter/openai_compatible 全走
pi），这个后续项没做。

## 已核实不存在现成插件

- 全仓库（含 v1/main 克隆）搜 `registerTool` 只命中
  `deploy/openclaw-plugins/feedling-io-tools`——那是 **OpenClaw** 的
  io_cli 感知工具桥（VPS 自托管场景），不桥接 user MCP、与 pi 无关。
- `deploy/Dockerfile.agent-runner` 只 npm 装三个 CLI，无任何 extension
  安装步骤。
- spawners 给 pi 用户只播 `models.json` + system prompt
  （`PI_CODING_AGENT_DIR={home}/pi-home/agent`），无 extensions 目录。

## 修复要件（若走 extension 路线）

1. 写 pi extension（`pi.registerTool()`），读 consumer 物化好的 user MCP
   配置（`/tmp/feedling_user_mcp_<fingerprint>.json` + CA 文件，格式见
   `tools/user_mcp_materialize.py`），把每个 enabled server 的工具代理出去。
2. `Dockerfile.agent-runner` 装上 extension。
3. `spawners.py` pi 分支 seed extension 配置（参照 models.json 的 files 机制）。
4. 影响面：所有 pi driver 托管用户（= gemini / openrouter /
   openai_compatible 三类 provider 全部）。

## 短期缓解（未做，需产品决定）

iOS MCP 设置页对 pi 路线用户提示"当前模型路线暂不支持 MCP 工具"，
或后端 add 时按 active driver 返回警告——不然每个 pi 用户都会撞一遍。

## 给用户的即时答复口径

pi 路线暂不支持 MCP 工具；换 Anthropic 官方 key（claude driver）或
OpenAI key（codex driver）即可使用 ombre brain。
