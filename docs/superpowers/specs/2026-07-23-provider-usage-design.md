# Provider 额度状态观测（中转站用量观测）— Design Spec v2

日期：2026-07-23 ｜ 分支：`feat/provider-usage`（基于 `origin/pre`）
状态：v1 经 Codex plan review（BLOCK）+ 逐条核实后修订；接口能力均已实测或读源码确认

## 问题

用户配的 provider key（中转站 / DeepSeek / OpenRouter）余额快用完时没人知道，
消息突然全部失败才发现。

    之前：中转站快没钱了 → 没人知道 → 用户发消息突然全部失败
    之后：打开观测入口 → 看到「余额 / 用量」→ 提前充值
    （各家能查到的字段不同，查不到的显式标「该服务商不提供」，绝不拿错误语义的数字冒充）

## 方案概览

纯现查现回：用户触发时用**已解密的 provider 配置**现场调 provider 账单接口，
统一 payload 返回。不落库、不缓存、不轮询。

```
iOS 设置页打开 ──→ GET /v1/model_api/usage（自行解一次 key）──┐
                                                              ├─→ 查询核心
聊天里问 agent ──→ V2 runtime-native 工具（复用本 turn 已解密   │   query_usage(provider_config)
                   的 provider_config，不二次解密）──────────┘   （只管第三方 HTTP 协议，
                                                                   不查库、不解信封）
                          │
                          ▼
              按「provider + base_url 是否官方 origin」选适配器
              ├─ deepseek 官方 origin: 余额数组（可多币种）；今日/本月 unsupported
              ├─ openrouter 官方 origin: 余额(credits) + 今日/本月(key usage) 全有
              ├─ openai_compatible(中转站): 剩余额度 + 累计已用；今日/本月 unsupported
              │   （new-api/one-api 的 billing 接口忽略日期参数，只有累计值）
              └─ 其他 / 命名 provider 配了自定义 base_url: unsupported
```

## 各 provider 真实能力（已验证，不是假设）

| provider | 余额 | 今日/本月 | 依据 |
|---|---|---|---|
| deepseek | ✅ `GET /user/balance` → `balance_infos[]` 数组（currency+total_balance，可多币种） | ❌ 官方无接口 | 2026-07-23 真 key 实测 |
| openrouter | ✅ `GET /api/v1/credits` → total_credits/total_usage，**普通推理 key 即可**（实测 is_management_key:false 的 key 能查） | ✅ `GET /api/v1/key` → usage_daily / usage_monthly（UTC 语义），另有 limit_remaining（key 级限额剩余，与账户余额分开标注） | 2026-07-23 真 key 实测 |
| openai_compatible | ⚠️ `/dashboard/billing/subscription` + `/dashboard/billing/usage` → 只有**累计**已用与总额度，且单位随中转站设置可能是 USD/CNY/tokens | ❌ new-api/one-api 源码确认忽略 start_date/end_date | 2026-07-23 读 new-api main 分支 billing.go 确认 |
| openai / anthropic / gemini / bedrock | — 首版 unsupported | — | 不适配 |

中转站单位歧义的处理：不猜。接口返回什么给什么，`unit` 字段标注
`usd / cny / tokens / unknown`，iOS 原样展示。

## 两个入口（凭证解析各自负责，核心只收解析结果）

1. **REST** `GET /v1/model_api/usage`（用户 API key 鉴权）：入口自己经现有
   `_load_runtime_provider_config()` 路径解一次 key，调查询核心。V1/V2 用户都可用。
2. **V2 工具**：走 **runtime-native 工具执行路径**（类似 task/reply 的特殊分支，
   不进普通 capability handler）。理由（Codex C2，已核实）：
   - 普通 capability 拿不到本 turn 已解密的 provider_config，只能二次解 key，
     破坏 single-decrypt-per-turn 约定；
   - 普通 read capability 整个 handler 跑在 enclave 信号量里（生产并发=2），
     5s 外网请求会把别人的聊天解密堵在门外。
   - 因此：复用 turn 已有的 provider_config，第三方 HTTP **必须在信号量外**执行。

### 工具的暴露范围与开关（Codex I2，已核实：注册即进全目录）

- **只在 chat lane 提供**；wake/capture/dream/subagent 一律不提供。
- 工具进目录会改变正常 turn 的 schema 与 token 预算 → 这**碰了正常流程**，
  按团队规则加**默认 ON 的 kill switch**（关掉 = 工具从目录消失 + dispatch 拒绝，
  两处都拦，防旧 catalog 快照绕过）。REST 入口不受此开关影响。
- 工具 description 写明「仅在用户明确询问余额/用量时调用」（引导，不当安全边界）。
- 隐私决策（记录在案）：用户主动问余额 → 精确数字进对话、发给其模型 provider。
  **接受**。理由：数字本来就是该 provider 自己账户的数据；只传必要字段。

## 返回契约（REST 与工具共享领域数据，不共享 HTTP 外壳）

逐指标带状态，支持部分成功与多币种：

    provider     openai_compatible / deepseek / openrouter
    adapter      实际用的适配器（如 openrouter_key）
    status       ok / partial / error
    as_of        查询时刻
    metrics.balance        {status, amounts:[{amount, unit}], scope: account}
    metrics.remaining      {status, amount, unit, scope: api_key}   # openrouter limit_remaining / 中转站剩余额度
    metrics.usage_total    {status, amount, unit}                    # 中转站累计已用
    metrics.usage_today    {status, amount, unit, timezone}          # 仅 openrouter，UTC
    metrics.usage_month    {status, amount, unit, timezone}          # 仅 openrouter，UTC
    每个 metric 的 status ∈ ok / unsupported / failed(原因)

iOS 对 unsupported 显示「该服务商不提供」，failed 显示「查询不到（原因）」。

## 出站安全（Codex C3，已核实：现有 base_url 校验只有 https/127.0.0.1）

- deepseek / openrouter 适配器**只在 base_url 为官方 origin（规范化比较）时启用**；
  命名 provider 配自定义地址 → 按 openai_compatible 处理或 unsupported，
  绝不把 key 按官方路径拼到自定义 origin 上。
- 中转站账单请求与已配置 base_url **同 origin**、保留路径前缀；
  禁跟随跨 origin redirect；不继承环境代理；响应体上限（如 256KB）流式读取。
- 总 wall deadline ~6s，多个子请求并发（openrouter 两个接口一起发）。
- key 只在内存过一遍：不进日志、不进返回体、不进错误信息（含第三方错误体转发时截断脱敏）。
- hosted 多租户下中转站目标为公网地址（复用 net_safety 的公网校验思路）；
  self-host 私网 relay 由部署者显式放行，不与 hosted 默认混用。

## 明确不做（首版）

- 快照落库 / 历史趋势 / 主动预警。注：后台解 key **技术上可行**（wake job 本来就解），
  首版不做是**政策选择**（无用户同意/频率预算/运营控制），不是加密契约不允许——
  此前 v1 spec 的表述有误，已更正。
- 中转站按日用量（接口不存在此语义）。
- 其他 provider 适配、通用账单库（LiteLLM 类方案面向自建网关计量，不适用 BYOK 余额读取）。

## 测试

- 查询核心：三家适配器 mock HTTP（正常 / 超时 / 非法响应 / 单位三态 / DeepSeek 多币种 /
  openrouter 无 limit / 中转站不兼容 / redirect 拒绝 / 大响应截断 / 错误体含 key 脱敏）。
- 适配器选择：官方 provider + 自定义 base_url → 不走官方适配器。
- V2：single-decrypt 不被破坏（解密计数）；第三方 HTTP 不持有 enclave 信号量（断言）。
- 工具范围：仅 chat lane 进目录；kill switch OFF 时目录与 dispatch 双拦截。
- REST：鉴权 / 无 key 配置 / unsupported provider。
- 公开契约：OpenAPI 产物 + docs-site types:check / contract tests + changelog Unreleased。
- 新测试文件 `--collect-only` 确认被收集（conftest `_PURE_UNIT` 白名单坑）。
- e2e：本地起 V2 链路，hx 的真 key 各查一遍（DeepSeek/OpenRouter 已在 spec 阶段实测通过）。

## iOS（另开分支，feedling-mcp-ios）

设置页 provider 区块按 metrics 渲染；本 spec 只锁契约。

## 修订记录

- v2（2026-07-23）：吸收 Codex plan review 六条核实为真的问题（中转站无按日语义、
  DeepSeek 多币种数组、V2 执行边界/信号量、base_url 适配器选择与出站安全、
  工具目录影响正常流程需 kill switch、「后台解不了 key」表述错误）；
  推翻其一条（OpenRouter /credits 需 management key —— 实测普通 key 可查）。
