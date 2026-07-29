# 撕裂协议 JSON 尾巴泄露 — 根治方案

分支 `fix/json-tail-leak`（基线 origin/test 0eee9a74）。2026-07-28。

## 问题

reasoning 模型 + openai_compatible 中转站。模型输出一段协议信封
`{"messages":[],"actions":[{"type":"proactive.sleep","reason":"..."}]}`，中转站切流把它
在 `reasoning_content`/`content` 通道边界上撕开：头进 reasoning（→「推理过程」折叠区），
尾进 content（→ 聊天气泡）。三套运行时都把两个通道当干净的、从不重拼，且所有旧护栏都
锚在 JSON **头部**特征上 → 头被搬走，尾巴畅通泄露。

实测泄露样本（真实用户 usr_ed9d6c05d1accb94，一夜多条）：

    active.sleep","reason":"4:34 了她睡得很沉 早上再说"}]}
    ":"5点了她还在睡 没动静"}]}
    type":"proactive.sleep","reason":"7点了 还在睡 不打扰了 醒了会找我"}]}

### 覆盖（已查实）

| 运行时 | 现状 | 证据级别 |
|---|---|---|
| V1 `chat_resident_consumer.py` | 三尾巴实测原样 post | 跑真实代码验过 |
| Proactive V2 `agent_protocol_v2.py` | head-anchored，3 漏 2 | 代码走查 |
| Model API V2 `worker.py`/`tool_loop.py` | free-text 兜底无任何协议护栏，最裸 | 代码走查 |

## 设计原则

**不再按「长什么样」堵，改为多个独立信号互相印证。** 共享一个**纯检测器**，返回
证据枚举（不是 `should_drop` 布尔）；各运行时按自己的 lane 语义决定策略。

### 检测器：证据分级（强→弱）

1. `joined_known_protocol` — reasoning 尾 + content 无缝拼接，能完整解析成已知协议信封
   且解析恰好覆盖到文本末尾 → 跨通道撕裂**铁证**（切一刀）。
2. `head_in_reasoning` — 拼不成完整 JSON（切多刀/中段丢失），但 reasoning 通道里独立
   蹲着可识别的协议**头**（`{"actions":[{"type":"proactive.`、`{"messages":`…）→ 几乎
   必然是撕裂（用户绝不会把协议头写进 reasoning 通道，这是指纹）。
3. `transport_cut` — 这一轮传输层报了 stream-cut 签名（`_PI_STREAM_CUT_RE`：ended without
   finish_reason / stream disconnected），配上气泡里的 JSON 残片 → 管道确坏。
4. `orphan_json_tail` — 只有可见尾巴像 JSON 残片（忽略字符串的括号净负 + JSON token），
   无任何上面的佐证 → **弱信号**。

### Lane 策略（关键：检测和策略分离）

- **强证据（1/2/3 任一命中）**：一律吞气泡；**连带清掉配对的 reasoning 头**（绝不把
  协议头贴到兜底气泡）；**绝不执行拼出来的动作**（坏管道上不猜意图）。
  - 前台 → 诚实兜底 `upstream_unavailable`（复用 f2844448 路径）。
  - 主动/wake → 不发气泡 + **记 `protocol_fragment_suppressed` 失败**（复用 6d9600b1 /
    V1 `degenerate_reply_suppressed` 生命周期；不是正常 sleep，别重置退避、别当自主沉默）。
- **弱证据（仅 orphan_json_tail）**：
  - 主动/wake → 仍吞（那场景不可能是正常消息，静默无害）+ 记失败。
  - 前台 → **保留不吞**（分不清是垃圾还是用户在贴 JSON；宁可偶尔漏一小截，不误吃真
    消息）+ 记 observability。**← 待 hx 确认，见「开放决策」**。

## 落地点（三套）

### V1 `tools/chat_resident_consumer.py`
- 检测放在**驱动接缝**（pi：`_pi_turn_from_stream` 拿到 reply+thinking 处；openai：
  `_agent_turn_from_obj` 拿到 content+reasoning_content 处）——此处两通道都在，才能算强证据。
- 命中强证据 → 产生**显式** `protocol_fragment_rejected`（不是普通空 turn），使
  `call_agent` 设 `_turn_reply_parse_failed`，前台走兜底；同时清掉该 reply 的 reasoning。
  修 Codex Critical 2（否则 thinking 非空 → 6854 判为合法空 turn → 前台静默吞轮）。
- parser 层（`_scan_visible_protocol`）保留弱证据兜底，仅供 proactive drop。

### Model API V2 `backend/model_api_runtime/v2/tool_loop.py` + `worker.py`
- 在 tool_loop **调 `on_reply` 之前**分类（此处 `pr.text` + `pr.raw['reasoning']` 都在）。
- 修投递确认契约（Codex Critical 3）：命中时不得让 `replied_intermediate=True` /
  不得把 `pr.text` 当 delivered。二选一：`on_reply` 返回 `published: bool`，仅 True 才
  标 delivered；或抛受控 `ProtocolFragmentSuppressed`（仿现成的 `FinalReplySuperseded`）。
  final chat → 换兜底并清空 reasoning；wake → 抛 `TurnError("protocol_fragment_suppressed")`
  静默失败，而非假成功。
- worker sink（`_on_reply` 6933/4827）再做一次 defense-in-depth。

### Proactive V2 `backend/proactive/agent_protocol_v2.py`
- 放宽 `_looks_like_protocol_fragment` 超出头部锚定：接入共享检测器的弱证据判据
  （尾巴形状），补 head-anchored 漏掉的 A/C 形状。记 suppressed。

### 共享工具
- 抽一个纯检测器 + 证据枚举（`joined_known_protocol`/`head_in_reasoning`/`transport_cut`/
  `orphan_json_tail`/`none`），三套各调、各定策略。**不共享 `should_drop()` 布尔**。

### 不动
- `provider_client` 不做全局清 reply（会改 extraction/compaction 行为）。
- raw-text/memory lane 不是同级泄露口（capture/dream 的 JSON parser 本就拒尾巴）；只补
  「撕裂尾巴 → 无 memory write / 无 compaction 污染」回归测试。
- 中转站本身不修（第三方）；iOS 渲染不动。

## 测试矩阵
- 对**完整信封的每个切分位置**参数化（不是只放这三个固定尾巴）。
- 误伤语料：JSON diff、报错日志、字符串字面量含 `"}]}`、Markdown 代码围栏、多气泡、
  颜文字、内联 JSON、含冒号的话。**必须逐字送达，零误吞**。
- 生命周期：前台命中 → 兜底且 reasoning 不贴气泡；wake 命中 → 无气泡 + suppressed 失败；
  V2 reply-tool intermediate 不被误标 delivered。
- raw-text lane：撕裂尾巴 → 无写入。
- 真 e2e：V1 resident + openai-compatible relay；Runtime V2。

## 开放决策（待 hx）
1. **弱证据前台**：保留+记（默认，护真消息）还是也吞（零泄露优先）？我倾向前者。
2. **强证据拼出真 `messages`**：一律扔（默认，坏管道不补发）还是前台尝试补发？我倾向扔。

## 分批
- B1：共享检测器 + 单测（纯函数，先 TDD）。
- B2：V1 驱动接缝接入 + 显式 reject + 清 reasoning + 前台/主动生命周期。
- B3：Model API V2 tool_loop 分类 + 投递契约修正 + worker sink 兜底。
- B4：Proactive V2 放宽 + raw-text 回归测试。
- B5：changelog(Unreleased) + reliability doc；真 e2e。

## 部署
- consumer 改动：CI 不管 → 合 test 随 CVM 镜像对 hosted 生效；VPS 自托管用户需手动
  `systemctl restart feedling-chat-resident`。
- backend V2：合 test → CI 自动出镜像 + 部署 CVM。
- 上线状态手工回写 FEATURE_LOG。
