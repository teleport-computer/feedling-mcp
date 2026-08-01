# V2 optional 重放窗口锚定 — 设计

- 日期：2026-07-30
- 状态：设计已确认，待出实施计划
- 前置：`docs/superpowers/specs/2026-07-29-runtime-v2-context-cache-review.md`（尤其 §11 更正）
- 相关：PR #135（`feat/v2-tail-anchor-groundwork`）已交付本设计要复用的地基
- 归档参考：分支 `archive/v2-tail-anchor-wrong-wiring` 保存着上一次接错位置的实现与测试脚手架

## 1. 背景

V2 chat 的 prompt 前缀逐轮断裂。prod 实测（近 30 天，只取 `model_calls=1` 的回合以排除
turn 内复用干扰）：

| 距上一回合间隔 | 回合数 | 完全零命中 | 命中率 |
| --- | --- | --- | --- |
| < 1 min | 21 | 0 | 35.8% |
| 1–5 min | 60 | 2 | 22.5% |
| 5–15 min | 11 | 4 | 16.0% |
| 15–60 min | 3 | 3 | 0.0% |

`< 1 min` 那一档是决定性的：缓存必然还活着（Anthropic ephemeral 默认 5 分钟 TTL，
`provider_client.py:1169` 未设 `ttl` 字段），命中率却只有 35.8%，且
`write/read ≈ 1.03`——每读回 1 个 token 就要重写 1 个。这说明失效原因不是过期，
而是**前缀本身每轮在变**。

### 1.1 上一次的错误（必读）

2026-07-29 的实施把锚点接到了 `compact_through_seq`，那个值**不参与 prompt 组装**，
因此对健康用户零收益。完整更正见前置 spec §11。该实施已回退。

**这次设计必须避免重蹈的两点**：

1. 动手前确认目标变量真的流进 prompt（用 grep 追全部使用点，不靠命名推断）。
2. 验收测试必须直接断言**前缀逐字节稳定**。上次的测试断言的是「锚点保持/前移」和
   「边界查询被跳过」，接错线的情况下这些照样全绿（7188 passed）。

## 2. 真正的根因

`worker.py` 的 chat lane 存在**双重滑动**：

```
read_recent_turns(user_id, target_turns=40, row_cap=512, through_seq=当前最大seq)
        ↓                                    ① 窗口本身每轮前移
_adaptive_replay_parts(window, watermark_seq, required_tail)
        ↓  optional = 完整轮且 max(seq) <= watermark（已被摘要覆盖）
           required = watermark 之后的行（必须逐字，prompt_invariant 守的不变量）
_make_build_messages_fn:
    optional_limit   = max(0, target_turns - required_turn_count)
    eligible_optional = optional_turns[-optional_limit:]
        ↓                                    ② 再从尾部截取
prompt = [system+skills][summary][eligible_optional][required][本轮消息]
```

两层都在滑：

- **①**：`through_seq` 每轮取当前最大 seq，「最近 40 轮」的窗口整体前移。
- **②**：每来一轮新对话 `required_turn_count` +1 → `optional_limit` −1 →
  `optional_turns[-optional_limit:]` 从**头部**收缩一轮。

`eligible_optional` 紧跟在摘要块之后（`context.py` 的 `build_turn_messages` 里
summary 在 tail 之前），所以摘要之后的第一条消息每轮都变，前缀从那里断裂。

**规模**：compaction 会把 tail 折到只剩约 10 行（`_TAIL_KEEP`），因此 required 约占 5 轮、
optional 约占 35 轮——**optional 撑起 prompt 历史部分的绝大多数**，不能删除
（已与产品确认必须保留）。

## 3. 目标与非目标

**目标**

- G1：锚点不变期间，连续两回合的 prompt 前缀**逐字节一致**。前缀的精确定义见 §8.1：
  从 `messages[0]` 起、至 required tail 第一条之前止。
- G2：单轮 turn 的跨回合缓存命中率从 30.4% 提升到 80%+，`write/read` 从 1.03 降到 0.2 量级。
- G3：不改变 `compact_through_seq` 的取值与 watermark 的推进节奏。
- G4：模型可见的历史原文量不减少（optional 重放保留）。

**非目标**

- 不删除 optional 重放，不放宽 compaction 折叠力度。
- 不改 `db.chat_recent_turn_rows` 的 SQL（只改调用处传入的 `max_turns`）。
- 不改 `tail_anchor.py`、锚点表、两条迁移（PR #135 已交付，原样复用）。
- 不为预算二分搜索的削减路径做特殊处理（见 §7）。
- 不动 wake lane。

## 4. 架构

锚点的**语义变更**：从「verbatim tail 起点」改为「**optional 重放的起点 seq**」。
存储、策略模块、决策函数全部复用，只是消费方不同——`tail_anchor.decide_anchor`
的入参语义（当前锚点、锚点后累积轮数、目标边界、目标轮数、滞后上限）对新用途同样成立，
**该模块一行都不用改**。

改造后的数据流：

```
read_recent_turns(user_id, max_turns=_CHAT_TAIL_ANCHOR_MAX_TURNS, ...)   ← ① 窗口放大到 60
        ↓ _adaptive_replay_parts（不变）
optional_turns / required_tail
        ↓
eligible_optional = [g for g in optional_turns if min_seq(g) >= anchor]  ← ② 起点由锚点决定
        ↓
prompt = [system+skills][summary][eligible_optional][required][本轮]
                                  ↑ 锚点不动时这一段逐字节不变
```

窗口必须放大到滞后上限，否则锚点可能落在 40 轮窗口之外、过滤后取不到应有的轮次。
`_RECENT_TURN_ROW_CAP` 默认 512 行，60 轮约 120–240 行，无需调整。

## 5. 组件改动

| 文件 | 改动 |
| --- | --- |
| `backend/model_api_runtime/v2/worker.py`（常量区） | **新增** `_CHAT_TAIL_ANCHOR_MAX_TURNS`（环境变量 `FEEDLING_V2_CHAT_TAIL_ANCHOR_MAX_TURNS`，默认 60），并带钳制：若 `<= _CHAT_TAIL_MAX_TURNS` 则抬到 `_CHAT_TAIL_MAX_TURNS + 20`。⚠️ 该常量随上次回退一并消失，当前分支**不存在**，必须重新加（可从 archive 分支 `worker.py:467-471` 取） |
| `backend/model_api_runtime/v2/worker.py`（chat lane） | 读锚点 → 数锚点后真实用户轮数 → `decide_anchor` → 条件写回。**不赋值给 `compact_through_seq`**（该变量维持原有的 `chat_recent_genuine_turn_boundary_seq` 计算） |
| `backend/model_api_runtime/v2/worker.py`（`_read_seq_adaptive_prompt_context`） | 窗口取 `_CHAT_TAIL_ANCHOR_MAX_TURNS`；摘要读取与 `_adaptive_replay_parts` 切分逻辑不变 |
| `backend/model_api_runtime/v2/worker.py`（`_make_build_messages_fn`） | `eligible_optional` 由锚点过滤产生，替换 `optional_turns[-optional_limit:]` |
| `backend/db.py` | 新增 `chat_genuine_turn_count_after_seq`（可从 archive 分支捞回，谓词与 `chat_recent_genuine_turn_boundary_seq` 严格同口径） |
| 迁移与表注释 | 语义描述从「verbatim tail 起点」更正为「optional 重放起点」 |

**零改动**：`tail_anchor.py`、`v2_chat_tail_anchor` 表结构、两条迁移的 DDL、
`jobs_store.get/set_chat_tail_anchor`、wake lane。

## 6. 关键不变量

1. **锚点单调**：由 `set_chat_tail_anchor` 的 `GREATEST` upsert 保证，存储层自身守住。
2. **滞后区内前缀逐字节不变**：`turns_after_anchor < _CHAT_TAIL_ANCHOR_MAX_TURNS` 时
   不查边界、不写锚点、`eligible_optional` 起点不变。
3. **滞后上限严格大于目标轮数**：否则没有滞后区、退化回逐轮滑动（即当前 bug 本身）。
   钳制逻辑随本次改动新增（见 §5），当前分支尚无。
4. **required tail 完整性不受影响**：`tests/test_v2_prompt_invariant.py` 守的
   「watermark 之后每条消息都逐字出现在 tail 中」不得被破坏。锚点只过滤 optional
   （watermark 之前），与该不变量正交。
5. **`compact_through_seq` 取值不变**：与改动前逐字节相同。

## 7. 错误处理与降级

| 情况 | 行为 |
| --- | --- |
| 锚点为 `None`（老用户首次进入新逻辑） | `decide_anchor` 走 bootstrap 分支，取最近 `_CHAT_TAIL_MAX_TURNS` 轮的边界作为锚点；该回合行为与改动前逐字等价 |
| 锚点早于窗口范围 | 不应发生（超过滞后上限即前移）。若发生，过滤后 `eligible_optional` = 窗口内全部 optional，安全降级 |
| `optional_turns` 为空 | prompt 退化为 `[summary][required]`，合法 |
| 边界查询返回 `None` | `decide_anchor` 返回 `no_boundary`，保持原锚点不清空 |
| 预算二分搜索触发削减 | 该回合前缀变化，由 `tail_fallback=True` 记录 |

**关于预算二分搜索**：`plan_provider_round` 的 `_candidate(count)` 取
`eligible_optional[-count:]`，预算不足时从最老的开始丢——这本身也是一种滑动。
实测 `tail_fallback = 0`、`effective_tail_turns` 恒 40，说明该路径从未被触发。
锚定后 prompt 涨约 40%，可能开始触碰。**本设计不为它做特殊处理**：
真触发时可由 `tail_fallback` 观测到，且 `FEEDLING_V2_CHAT_TAIL_ANCHOR_MAX_TURNS`
可下调。避免在没有证据的情况下过度设计。

## 8. 测试策略

### 8.1 验收测试（必须有）

**这是本设计的核心交付物，也是上次失败的直接原因。**

连续跑两轮 `process_job`，捕获两次实际送给 provider 的 messages，断言
**从 `messages[0]` 起、至 required tail 第一条之前止的全部消息逐字节相同**
（含 system 块、摘要块、全部 optional 轮；不含 required tail 与本轮消息）。

判定标准：该测试在「锚点被接到不影响 prompt 的值上」这种错误实现下**必须变红**。
上次那套测试（只断言锚点保持/前移、边界查询被跳过）在同样条件下是绿的。

### 8.2 配套测试

| 测试 | 断言 |
| --- | --- |
| 滞后区内保持 | 锚点值不变；`eligible_optional` 首轮的起始 seq 不变 |
| 前缀比对的粒度 | 逐字节比对序列化后的 messages，而非只比条数或角色 |
| 越过阈值前移 | 锚点前移一次；前缀**预期性地**变化（这一轮 miss 是设计内的） |
| 前移后再次稳定 | 前移后的连续两轮前缀又逐字节相同 |
| `compact_through_seq` 不受影响 | 改动前后该变量取值一致（守 §6 不变量 5） |
| `test_v2_prompt_invariant.py` | 保持通过（守 §6 不变量 4） |

### 8.3 脚手架

从 `archive/v2-tail-anchor-wrong-wiring` 捞回并复用：
`_seq_native_chat_deps`（拼 `TurnDeps`）、`_fake_sealed_envelope`（伪造 enclave 信封）、
`_seed_chat_row`、`_reset_anchor_hotpath_user`。新增一个捕获 messages 的 spy。

### 8.4 变异验证

实施完成后，手工做一次变异测试：把锚点过滤改回 `optional_turns[-optional_limit:]`，
确认验收测试变红，再还原。**不确认这一点就等于没有验收测试。**

## 9. 验收标准

1. 上述验收测试通过，且变异验证证明其具备鉴别力。
2. 全量 L1 无新增失败（基线见实施计划）。
3. `compact_through_seq` 取值与改动前一致。
4. wake lane 零改动（`git diff` 中不出现 `_WAKE_TAIL_MAX_TURNS` 的增删行）。
5. 上线后 48h，prod 单轮 turn（`model_calls=1`）在 `<1min` 间隔档的命中率显著高于 35.8%，
   `write/read` 明显低于 1.03。**不得仅凭整体命中率波动判定成功**——须按 §1 的分档口径复核。

## 10. 风险

| 风险 | 处置 |
| --- | --- |
| prompt 涨约 40%，未命中的回合（间隔 > 5 min，约占 22%）纯多付 | 净收益仍为正（cache read 约为 miss 价的 1/10）。滞后上限可通过环境变量下调 |
| 预算二分搜索开始触发削减 | 由 `tail_fallback` 观测，见 §7 |
| 锚点语义变更后，PR #135 里的表注释与迁移 docstring 过时 | 本次一并更正 |
| `_RECENT_TURN_ROW_CAP` 在超长轮次下不足 | 512 行 / 60 轮 = 每轮 8.5 行，正常对话远低于此；不足时 `_adaptive_replay_parts` 会丢弃头部不完整轮，属既有降级 |

§11.4 提到的 coverage hole 放大风险**不适用于本设计**：那条风险源于钉住
`compact_through_seq` 导致 watermark 推进变慢，而本设计不碰该变量。

## 11. 参考

- 根因与更正：`docs/superpowers/specs/2026-07-29-runtime-v2-context-cache-review.md` §11
- 已交付地基：PR #135（`tail_anchor.py`、锚点表、双链迁移、exhaustion 计数兜底）
- 错误实现与可复用脚手架：分支 `archive/v2-tail-anchor-wrong-wiring`
