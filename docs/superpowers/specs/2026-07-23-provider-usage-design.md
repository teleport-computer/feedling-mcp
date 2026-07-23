# Provider 余额与用量观测（中转站用量观测）— Design Spec

日期：2026-07-23 ｜ 分支：`feat/provider-usage`（基于 `origin/pre`）｜ 状态:已与 hx 对齐

## 问题

用户配的 provider key（中转站 / DeepSeek / OpenRouter）余额快用完时没人知道，
消息突然全部失败才发现。

    之前：中转站快没钱了 → 没人知道 → 用户发消息突然全部失败
    之后：打开观测入口 → 看到「余额 ¥23 / 今日已用 ¥xx / 本月 ¥xx」→ 提前充值

## 方案概览

纯现查现回的旁路：用户触发时，后端在**请求上下文里**解开用户自己的 key 信封，
现场调用 provider 的账单接口，统一格式返回。不落库、不缓存、不定时轮询。
正常聊天流程零改动。

```
iOS 设置页打开 ──┐
                 ├─→ GET /v1/model_api/usage
用户问 agent ────┘        │
  (V2 capability 工具)    ▼
                  请求上下文解 key 信封（复用现有 setup_core 解密路径）
                          │
                          ▼
              按 provider 分流查账单接口（共用一个查询核心）
              ├─ openai_compatible(中转站): 余额 + 按日用量
              ├─ deepseek: 只有余额
              └─ openrouter: 余额 + 用量
                          │
                          ▼
              统一 payload：balance / today / month / currency / unsupported
```

## 两个入口

1. **REST 端点** `GET /v1/model_api/usage`（用户 API key 鉴权，同现有
   `/v1/model_api/*` 家族）。iOS 设置页打开时调一次。V1 / V2 用户都可用。
2. **V2 capability 工具**（`backend/capabilities/` 注册表）：只读、无参数。
   用户在聊天里问「我还剩多少钱」→ agent 调工具 → 如实回答。
   查失败就如实说失败原因，不重试不编造。**V1 用户没有这条路**（不走工具循环）。

两个入口共用同一个查询核心模块，只是鉴权/接入层不同。

## 返回契约（REST 与工具同一形状）

    provider        openai_compatible / deepseek / openrouter
    balance         余额数字；查不到为 null
    currency        USD / CNY（provider 返回什么给什么）
    today_usage     今日已用金额；DeepSeek 为 null
    month_usage     本月（1号至今）已用金额；DeepSeek 为 null
    unsupported     该 provider 给不了的字段列表（iOS 显示「不支持」而非空白）
    error           整体失败原因（超时 / key 无效 / 中转站不兼容账单接口）

## Provider 适配

| provider | 余额 | 按日用量 | 接口 |
|---|---|---|---|
| openai_compatible（new-api/one-api 类中转站） | ✅ | ✅ | OpenAI dashboard billing 兼容接口（subscription + usage by date range） |
| deepseek | ✅ | ❌ 官方无此接口 | 官方 balance 接口 |
| openrouter | ✅ | ✅ | credits / key usage 接口 |
| 其他（openai / anthropic / gemini / bedrock） | — | — | 首版返回 unsupported，不适配 |

⚠️ **实现前必须在线验证三家接口的真实形状**（路径、字段名、金额单位——
中转站的 usage 通常是美分×100 之类的缩放单位），不凭记忆写。

「今日 / 本月」的日期语义按 Asia/Shanghai 计算日期参数；provider 侧按其自身
计费时区聚合，误差在日界附近可接受，spec 不追求对账级精确。

## 失败路径与约束

- 任何查询失败 → 返回 `error` 字段，**绝不影响聊天等正常流程**（本来就是旁路）。
- 单 provider 请求超时 5s 兜底。
- key 只在内存过一遍：不进日志、不进返回体、不进错误信息。
- 后台预警做不了：key 信封只有用户请求上下文解得开，这是现有加密契约，本功能不动它。
- 不加 feature flag：纯旁路新增，按工作区规则直接上。

## 不做（YAGNI）

- 快照落库 / 历史趋势图 / 余额低于阈值主动提醒（需要后台解 key，做不了）
- 结果缓存
- 其他 provider 的账单适配

## 测试

- 查询核心：三家适配器各自 mock HTTP 测解析（正常 / 超时 / 非法响应 / 不兼容中转站）。
- REST 端点：鉴权、无 key 配置、provider 不支持三条路。
- 工具注册：schema 校验 + 失败时返回给模型的文案。
- 新测试文件跑 `--collect-only` 确认被收集（conftest `_PURE_UNIT` 白名单坑）。
- 端到端：本地起 V2 链路，用真 key 各查一次三家（hx 的中转站 + DeepSeek + OpenRouter key 都在手上）。

## iOS（另开分支，feedling-mcp-ios）

设置页 provider 区块显示：余额 / 今日 / 本月；`unsupported` 字段显示
「该服务商不提供」；`error` 显示「查询不到（原因）」。本 spec 只锁契约，
iOS 实现细节在其分支内定。
