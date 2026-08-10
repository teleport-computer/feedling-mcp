# Fable 5 在 Runtime V1 的 Claude Code 模型适配问题与修复方案

**日期**：2026-08-07  
**测试用户**：`usr_6491814d52abdf99`（test）  
**涉及运行时**：Runtime V1 resident、Claude Code CLI  
**涉及供应商**：Anthropic 直连  
**报告状态**：Claude Code 已回滚至 2.1.195；Claude 家族内 fallback 允许，跨家族错配仍 fail-closed

> **2026-08-08 后续决策**：Claude Code/Anthropic 调用链可能把所请求的
> Claude 型号切换为另一个 Claude 型号。Runtime V1 现在允许这种 Claude
> 家族内 fallback，并记录 configured/actual model；只有实际回执落到非 Claude
> 家族时才产生 `model_mismatch`。本文后续“所有型号错配均 fail-closed”的描述
> 保留为当时排查和首版修复记录，不再代表当前策略。

---

## 1. 执行摘要

Runtime V1 并非无法运行 Anthropic 高级模型。受控测试中，`claude-opus-4-8`、`claude-opus-5`、`claude-sonnet-4-6` 均能完成真实回复和 memory tool call；异常集中在 `claude-fable-5`。

Fable 5 的上游连接测试、V1 回复和 tool call 表面均成功，但 Claude 会话记录显示实际生成回复的是 `claude-opus-4-8`。这是一次**静默模型回退**：配置模型是 Fable 5，实际执行模型是 Opus 4.8，系统仍将回合标记为成功。

测试 Runner 当前安装的是 Claude Code `2.1.195`。该版本的 `--help` 明确支持 `fable` 和完整模型名 `claude-fable-5`，二进制模型表也包含 Fable 5。因此问题不在“Claude Code 版本太旧、不认识 Fable 5”，而在 Runtime V1 的调用方式：系统只设置 `ANTHROPIC_MODEL`，默认命令没有显式传 `--model`，也没有核对实际执行模型。

本报告只保留 Claude Code 首选方案；该方案已于 2026-08-08 合入并部署 test：

1. 默认 Claude 命令显式传入 `--model <route.model>`；
2. provider/model 进入 resident session signature，切模型强制新会话；
3. 从 Claude 输出或 session 事件提取实际模型，配置与实际不一致时返回 `model_mismatch`；
4. 默认非 thinking Claude 命令使用 `--output-format json`，确保每轮都有可校验的结构化模型回执；
5. 通过真实回复、memory tool call 和实际模型字段完成端到端验收。

部署后结论需要拆成两部分：

- **安全问题已修复**：配置模型与实际模型不一致时返回 `model_mismatch`，不再把错误模型的回复伪装成成功；
- **Fable 5 完整适配尚未通过**：最小 Claude CLI tool-call 探针能真实命中 `claude-fable-5`，但完整 Runtime V1 resident 回合仍被 Claude Code 改派到 `claude-opus-4-8`，因此当前必须把 Fable 5 标记为 V1 暂不兼容，而不是自动回退。

本轮不改 pi、不切换 driver，也不把升级 Claude Code 当作修复前提。

---

## 2. 问题与范围

### 2.1 用户现象

- Runtime V1 上 Fable 5 看起来“接不上”或行为不像所选模型；
- Anthropic key 的短连接测试可以成功；
- 普通回复与 tool call 也可能成功，因此仅看 HTTP 状态和回复内容无法发现模型选错；
- Runtime V2 的 Fable 5 空回复属于另一条原生 provider 调用链，不在本报告修复范围内。

### 2.2 本报告解决什么

本报告只解决：

```text
Runtime V1 + Anthropic 直连 + Claude Code + claude-fable-5
```

目标是让 Runtime V1 明确、可验证地运行用户选择的 Fable 5；如果无法加载该模型，则明确失败，禁止换成其他模型后继续返回成功。

### 2.3 明确不做什么

- 不修改 pi；
- 不把 Anthropic Fable 5 路由迁移到其他 driver；
- 不处理 Runtime V2 的 provider empty response；
- 不声称本修复能解决 OpenRouter 独立链路的问题；
- 不把 Claude Code 升级作为首要修复。

---

## 3. 受控测试结果

下表四个模型均使用测试用户的 **Anthropic 直连 credential**，每个模型都要求执行一次真实 memory tool call。

| 配置模型 | 上游测试 | V1 回复 | Tool call | 会话记录中的实际模型 | 判定 |
|---|---:|---:|---:|---|---|
| `claude-opus-4-8` | 成功 | 成功 | 成功 | `claude-opus-4-8` | 通过 |
| `claude-opus-5` | 成功 | 成功 | 成功 | `claude-opus-5` | 通过 |
| `claude-sonnet-4-6` | 成功 | 成功 | 首次参数错误后自动重试成功 | `claude-sonnet-4-6` | 通过 |
| `claude-fable-5` | 成功 | 表面成功 | 成功 | **`claude-opus-4-8`** | **失败：静默回退** |

Fable 5 受控回合的关键证据：

- 消息 ID：`39b68280ceb55ca17d1bfafea1cc4cf0`
- 诊断标记：`TOOLCALL-20260807-FABLE5`
- 回复内容：`TOOLCALL_FABLE5_OK`
- memory-write：执行成功，`applied=1`、`failed=0`
- Claude session：`9df34fc2-55c9-42fc-a0f0-0dcb322e3a04`
- 该 session 内所有 assistant 事件的 `model` 都是 `claude-opus-4-8`
- 该 session 是本轮新建会话，不是从旧 Opus 会话恢复而来

因此，本次 Fable 回退不能归因于恢复了旧会话；问题发生在新会话的模型选择阶段。

测试结束后已完成现场恢复：

- 活动路由恢复为 Anthropic `claude-opus-4-8`
- 临时 Fable 5、Opus 5、Sonnet 4.6 路由全部删除
- 三条诊断记忆全部删除
- 用户原有“喜欢黑色”的偏好记录仍保留

### 3.1 2026-08-08 实施与部署后复测

代码已分两轮合入 `test`：

- `9c4a8a29`：managed Claude 命令显式传 `--model`，provider/model 写入 runtime metadata 和 session signature；
- `86355f84`：从 Claude 结构化事件提取 actual model，不一致时清 session 并返回 `model_mismatch`；
- `d36309f4`：非 thinking Claude 命令增加 `--output-format json`，避免纯文本成功回合缺少可校验的模型回执；
- test 合并提交：`e3b1d3d3`、`b4eebef2`；后端与 runner 最终均运行镜像 `b4eebef`。

部署流水线 `31234914057` 完成且结论为 success，`https://test-api.feedling.app/healthz` 返回 200，release commit 为 `b4eebef241d26a2b0a7580041c4c7a984ceb9832`。

复测统一使用只读真实工具调用，避免污染用户记忆：

```text
python /app/tools/io_cli.py memory-index --query '我喜欢的颜色' --limit 5
```

| 配置模型 | resident 进程模型 | 实际 assistant model | Bash tool | Tool result | 回合结果 |
|---|---|---|---:|---:|---|
| `claude-opus-4-8` | `claude-opus-4-8` | `claude-opus-4-8` | 成功 | `is_error=false` | 通过 |
| `claude-opus-5` | `claude-opus-5` | `claude-opus-5` | 成功 | `is_error=false` | 通过 |
| `claude-sonnet-4-6` | `claude-sonnet-4-6` | `claude-sonnet-4-6` | 成功 | `is_error=false` | 通过 |
| `claude-fable-5` | `claude-fable-5` | `claude-opus-4-8` | 成功 | `is_error=false` | `model_mismatch/system`，回复 fail-closed |

关键诊断结论：

1. 同一 test credential 下，最小 Claude CLI（显式 `--model claude-fable-5`、全新 session、JSON 输出、真实 `memory-index` tool call）连续观测到实际模型为 `claude-fable-5`；这证明 Claude Code `2.1.195` 与上游本身能执行 Fable 5 工具回合。
2. 完整 Runtime V1 resident 命令和上下文下，Claude trace 的所有 assistant 事件仍为 `claude-opus-4-8`。
3. 新增安全门正确识别了该差异：回复消息 `4b81b37a711445438e2c9c577f99c2a8` 标记 `turn_failure_error_class=model_mismatch`、`turn_failure_blame=system`，并清除了错误模型 session。
4. 因此当前剩余问题不是“Runtime V1 没有传模型”，而是 Claude Code 在完整 resident 运行形态下仍发生上游/CLI 级模型改派。没有证据时不得把它伪装为 Fable 成功。

现场已恢复并清理：活动路由为原始 Anthropic `claude-opus-4-8`；本轮临时创建的 Fable 5、Opus 5、Sonnet 4.6 路由已删除；探针只读取记忆，没有创建诊断记忆。

---

## 4. 当前调用链

### 4.1 模型如何进入 Claude 进程

`backend/agent_runtime/spawners.py::consumer_env()` 当前读取活动 route 的模型，并写入：

```python
model = (entry.get("model") or "").strip()
if model:
    env["ANTHROPIC_MODEL"] = model
```

但生成默认 Claude CLI 命令时，`consumer_env()` 只给 pi 路径传 `model`；Claude 默认命令没有使用 route model：

```python
env["AGENT_CLI_CMD"] = cli_cmd or _default_cli_cmd(
    driver,
    home,
    model=str(entry.get("model") or "") if driver == "pi" else "",
    reasoning_effort=(
        str(entry.get("reasoning_effort") or "") if driver == "pi" else ""
    ),
)
```

最终 Claude 进程依赖环境变量隐式选模型，命令行中没有：

```text
--model claude-fable-5
```

### 4.2 当前 Claude Code 已经支持 Fable 5

测试 Runner 的实际版本：

```text
2.1.195 (Claude Code)
```

该版本的 CLI 帮助明确说明：

```text
--model <model>
Model for the current session. Provide an alias such as 'fable', 'opus',
or 'sonnet', or a full name such as 'claude-fable-5'.
```

其二进制内置模型表也包含：

```text
claude-fable-5
Fable 5
```

所以“先升级 Claude Code 才能支持 Fable 5”不是当前证据支持的根因。

### 4.3 系统为什么没有发现选错模型

resident 当前主要关心：

- CLI 是否正常退出；
- 是否产出文本；
- tool call 是否执行；
- 是否能写回聊天消息。

它没有把 Claude 事件里的实际模型与 route model 做强一致性比较。因此即使配置 Fable、实际执行 Opus，只要回复和工具成功，整个回合仍会被判为成功。

---

## 5. 根因与独立缺陷

### 5.1 直接故障点：模型选择只靠隐式环境变量

受控测试已经证明：

```text
route.model = claude-fable-5
ANTHROPIC_MODEL = claude-fable-5
新建 Claude session
actual model = claude-opus-4-8
```

这说明当前 managed invocation 中，仅设置 `ANTHROPIC_MODEL` 不能作为可靠的模型选择契约。Claude Code 已提供专用 `--model` 参数，Runtime V1 应使用显式参数选择当前 session 的模型。

在执行显式参数 E2E 前，不能进一步断言是 Claude Code 内部的环境变量优先级、配置覆盖还是其他内部机制导致回退；但这不影响修复边界：调用方必须使用明确、可观测、可校验的模型选择方式。

### 5.2 独立缺陷：session signature 不包含 provider/model

`tools/chat_resident_consumer.py::_agent_entry_signature()` 当前主要基于：

- `AGENT_MODE`
- `AGENT_CLI_CMD`
- `AGENT_HTTP_MODEL`
- runtime metadata

Claude 默认命令不包含 route model，而 runtime metadata 也没有可靠读取 `ANTHROPIC_MODEL`。因此两个不同 Anthropic 模型可能得到同一个 session signature。

虽然本次 Fable 回合使用的是新 session，这不是本次回退的直接原因，但它会导致其他切换场景错误恢复旧模型会话，必须一并修复。

### 5.3 独立缺陷：缺少实际模型一致性安全门

没有 actual-model validation 会把任何 alias 映射、默认模型回退、session 锁模或 CLI 配置错误伪装成成功。该安全门不只服务 Fable 5，也保护所有未来模型升级。

### 5.4 相关但独立：provider credential 快速切换缓存

此前还复现过 OpenRouter 与 Anthropic 快速互切后，旧 credential 在约 300 秒缓存窗口内残留，导致新路由收到 401。它会让 Opus 4.8 看起来“接不上”，但与 Fable 5 静默回退不是同一根因。

本报告不扩展到 OpenRouter driver，但建议后续让 provider/model/credential 变化按 route version 立即失效缓存，而不是只等待 TTL。

---

## 6. 首选修复方案：保留 Claude Code driver

### 6.1 P0：默认 Claude 命令显式传 `--model`

修改 `backend/agent_runtime/spawners.py`，让 Claude 默认命令接收 route model，并经过 `shlex.quote()` 写入命令模板：

```text
claude --model claude-fable-5 ...
```

推荐修改方式：

1. `consumer_env()` 调用 `_default_cli_cmd()` 时，对 Claude 也传入 `model`；
2. `_default_cli_cmd()` 的 Claude 分支在 model 非空时加入 `--model <quoted-model>`；
3. 保留 `ANTHROPIC_MODEL` 作为兼容性环境变量，但不再把它当唯一选择机制；
4. 不设置 `--fallback-model`，避免主模型不可用时静默换模；
5. 用户自定义 `cli_cmd` 若已含 `--model`，不得重复注入；
6. managed Claude 命令若没有 `--model`，启动诊断应记录明确 warning。

不要硬编码 Fable 5。参数必须来自活动 route，使 Opus、Sonnet 和后续模型共用同一条正确路径。

### 6.2 P0：模型身份进入 runtime metadata 和 session signature

在 `consumer_env()` 中显式设置：

```python
env["FEEDLING_AGENT_PROVIDER"] = provider
env["FEEDLING_AGENT_MODEL_ID"] = model
```

让 `_agent_entry_signature()` 包含：

```text
driver + provider + model + command + base_url fingerprint
```

其中 base URL 只保留标准化 host 或不可逆 fingerprint，避免把敏感配置写进 header 或日志。

验收行为：

- Opus 4.8 → Fable 5：轮换 session；
- Fable 5 → Opus 5：轮换 session；
- provider 改变：轮换 session；
- route 不变：继续 resume，保持正常对话连续性。

### 6.3 P0：增加 `model_mismatch` 安全门

从 Claude CLI JSON 输出、assistant 事件或 session artifact 中提取实际模型：

```text
configured_model = claude-fable-5
actual_model     = claude-opus-4-8
```

不一致时：

```text
error_class: model_mismatch
blame: runtime_adapter
retryable: false
publish_reply: false
```

用户提示建议：

> 当前运行时没有成功加载所选模型，请重新选择模型或稍后重试。

日志只记录 provider、configured model、actual model、route ID、session ID；不得记录 API key 或用户 prompt。

如果某些 Claude 输出形态没有 actual model 字段，应在上线前确认稳定提取来源；不能用“模型自称”代替结构化元数据。

### 6.4 P1：route-version 驱动配置缓存失效

provider、model、credential、base URL 任一变化时，激活 route 后立即失效旧配置。缓存键应包含 route version 或 credential ID，不能只依赖固定 TTL 收敛。

该项解决快速切换后的旧 key 401，不是 Fable 静默回退的前置条件，可以独立交付。

### 6.5 Claude Code 升级的定位

可以升级 Claude Code以获取后续安全修复和稳定性改进，但执行顺序应是：

1. 先用当前 `2.1.195` 显式 `--model claude-fable-5` 做最小 E2E；
2. 当前版本通过后，实施 managed command 修复和 mismatch gate；
3. 若当前版本显式 `--model` 仍失败，再对照升级版本做单变量 A/B；
4. 只有 A/B 证明升级是必要变量，才把最低版本提升纳入修复。

这能避免把“调用参数缺失”和“依赖版本”同时改变后无法判断真正起效的因素。

---

## 7. 不推荐方案

### 7.1 只升级 Claude Code

当前版本已经声明支持 Fable 5。升级不会自动给 Feedling 的默认命令添加 `--model`，也不会补上 session signature 和实际模型一致性校验。

### 7.2 继续只依赖 `ANTHROPIC_MODEL`

该路径已经产生配置 Fable、实际 Opus 的受控失败，不能继续作为 managed runtime 的唯一模型选择机制。

### 7.3 只增加 `--model`，不做实际模型校验

显式参数是首要修复，但未来仍可能遇到 alias 映射、服务端回退或 session 异常。缺少 mismatch gate 时，系统仍无法证明实际模型与配置一致。

### 7.4 为 Fable 5 单独硬编码命令

硬编码会让其他模型继续走不可靠路径，也会在后续模型发布时重复产生同类问题。应修复通用 route→Claude command 数据流。

---

## 8. 测试方案

### 8.1 单元测试

`tests/test_agent_runtime_spawners.py`：

- Claude route model 出现在默认 `AGENT_CLI_CMD --model`；
- 模型名经过 shell quoting；
- 空 model 不生成空参数；
- 已含 `--model` 的 operator command 不重复注入；
- `ANTHROPIC_MODEL` 仍与 route model 一致；
- provider/model 写入 runtime metadata env。

`tests/test_chat_resident_consumer.py`：

- provider/model 进入 entry signature；
- provider/model 改变会轮换 session；
- route 不变时正常 resume；
- actual model 一致时发布回复；
- actual model 不一致时产生 `model_mismatch`，不发布回复；
- 缺少结构化 actual model 时按明确策略处理，不读取自然语言自称。

### 8.2 Test 环境最小验证

先不改镜像，直接在隔离的新 session 中做单变量对照：

| 用例 | 模型选择方式 | 预期 |
|---|---|---|
| A | 仅 `ANTHROPIC_MODEL=claude-fable-5` | 复现实际 Opus 4.8 |
| B | `--model claude-fable-5` | 实际模型为 Fable 5 |

两组使用同一 credential、同一 prompt 形状、同一 tool call，唯一变量是是否显式传 `--model`。

如果 B 仍然不是 Fable 5，则停止实施，收集 CLI verbose/JSON 输出，再测试 Claude Code 版本升级；不能把未验证的参数变更直接上线。

### 8.3 Test 环境完整回归

| Provider | Model | Driver | 期望 |
|---|---|---|---|
| Anthropic | `claude-fable-5` | Claude | 回复、tool call 成功；actual model 为 Fable 5 |
| Anthropic | `claude-opus-4-8` | Claude | 回归通过；actual model 为 Opus 4.8 |
| Anthropic | `claude-opus-5` | Claude | 回归通过；actual model 为 Opus 5 |
| Anthropic | `claude-sonnet-4-6` | Claude | 回归通过；actual model 为 Sonnet 4.6 |

每个组合必须执行：

1. 短文本回复；
2. memory-write；
3. 回执与 tool trace 核验；
4. memory-delete；
5. actual model 核验；
6. session signature 核验。

切换顺序：

```text
Opus 4.8 → Fable 5 → Opus 5 → Sonnet 4.6 → Opus 4.8
```

每次激活后立即发消息，不等待缓存 TTL，核对每次切换都创建新 session。

### 8.4 范围隔离验证

- 确认没有修改 pi 配置、pi driver 或 pi 测试基线；
- 确认 Runtime V2 provider loop 未改变；
- OpenRouter Fable 5 继续按独立问题跟踪，不把 Anthropic Claude 路径通过当作 OpenRouter 已修复。

---

## 9. 上线方案

### 阶段一：最小假设验证

1. 在 test Runner 使用当前 Claude Code `2.1.195`；
2. 创建全新 session；
3. 显式传 `--model claude-fable-5`；
4. 执行带 memory tool call 的真实回合；
5. 核对结构化 actual model；
6. 只有实际模型为 Fable 5 才进入实现阶段。

### 阶段二：代码与本地测试

1. 实现 Claude 默认命令显式模型参数；
2. 实现 runtime metadata/session signature；
3. 实现 `model_mismatch`；
4. 跑定向 pytest；
5. 跑带真实 Postgres 的后端测试基线；
6. 若改变公开 runtime/status/error contract，同步公开文档和 changelog。

### 阶段三：部署 test

1. 同步部署 backend 与 runner；
2. 执行第 8 节完整模型矩阵；
3. 保存 route ID、message ID、session ID、configured/actual model 和 tool trace；
4. 观察模型快速切换与正常 resume；
5. 确认无诊断记忆残留。

### 阶段四：生产灰度

1. 先灰度内部 Anthropic Fable 5 route；
2. 监控 `model_mismatch`、CLI non-zero、provider error、tool failure；
3. 确认 Fable 5 稳定后再向存量用户开放；
4. 不扩大到 Runtime V2 或 OpenRouter 链路。

---

## 10. 回滚方案

出现以下任一条件立即停止 Fable 5 上线：

- 显式 `--model` 后 actual model 仍不是 Fable 5；
- tool call 成功率明显低于现有 Claude 模型；
- session 切换后仍恢复旧模型；
- `model_mismatch` 持续出现；
- memory action 错误写入或无法删除。

回滚动作：

1. 将 Fable 5 标记为 Runtime V1 暂不支持；
2. 用户端建议切换 Opus 4.8、Opus 5 或 Sonnet 4.6；
3. 禁止把 Fable 5 自动映射到 Opus；
4. 保留 `model_mismatch` 和 session signature 安全修复；
5. 若升级 Claude Code曾作为独立变量加入，回到已验证版本并保留诊断证据。

---

## 11. 验收标准

只有同时满足以下条件，才能宣告 Runtime V1 的 Anthropic Fable 5 已适配：

- [x] 当前 Claude Code `2.1.195` 的显式 `--model` 最小验证通过；
- [ ] Fable 5 E2E 连续成功至少 3 次；
- [ ] 每条 Fable 回复的 actual model 都是 `claude-fable-5`；
- [ ] memory-write 与 memory-delete 均成功；
- [ ] Opus 4.8 → Fable 5 → Opus 5 → Sonnet 4.6 切换无需等待 TTL；
- [ ] 每次切模型都轮换 resident session；
- [ ] route 不变时正常 resume；
- [x] 不再存在“配置 Fable、实际 Opus、请求成功”的静默回退；错误模型回合被 `model_mismatch` 拦截；
- [x] Opus 4.8、Opus 5、Sonnet 4.6 回归通过；
- [x] pi 与 Runtime V2 均未被本次修复改变。

---

## 12. 建议拆分的开发任务

1. **P0：Claude 显式模型选择**  
   route model 进入默认 CLI `--model`，补 command/env 单元测试。

2. **P0：实际模型一致性安全门**  
   提取结构化 actual model，新增 `model_mismatch` 错误分类和用户提示。

3. **P0：resident session provenance**  
   provider/model/base URL fingerprint 进入 entry signature。

4. **P1：credential/config route-version invalidation**  
   消除 provider 快速切换后的旧 key 窗口。

5. **验证与灰度**  
   运行 Anthropic Claude 模型矩阵并保存脱敏证据。

---

## 13. 最终结论

当前证据仍不支持“只升级 Claude Code即可解决 Fable 5”。测试 Runner 的 Claude Code `2.1.195` 能在最小、真实 tool-call 探针中运行 `claude-fable-5`，但完整 Runtime V1 resident 回合仍改派到 Opus 4.8；需要继续对完整命令中的 resident 上下文/MCP 组合做单变量排查，或用更新 Claude Code 做严格 A/B，才能确定最终兼容条件。

首选安全方案已经落地：managed Claude route 显式传 `--model <route.model>`，provider/model 进入 session signature，非 thinking 路径产出 JSON receipt，并由 `model_mismatch` 安全门核对实际模型。它解决了 Opus 4.8、Opus 5、Sonnet 4.6 的 V1 选择与工具调用，也把 Fable 的静默回退变成可观测、不可伪装的失败。

因此当前产品结论是：Runtime V1 的 Anthropic Opus 4.8、Opus 5、Sonnet 4.6 可用；Fable 5 暂不宣告支持，必须 fail-closed。用户已恢复到 Opus 4.8。本轮未修改 pi，也未通过迁移 driver 规避问题。
