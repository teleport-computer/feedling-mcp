# Runtime V2 → V1 体验收敛台账 (Parity Program)

> 2026-08-05 立项,Seven 指令:V2 要真实达到 ≥90% 的 V1 体验。
> 方法论:把 V1 的"看不见的产品打磨"当 **spec** 逐项搬,同时把"指望模型自觉"
> 的每一处改成 **harness 确定性保证**。每项走 spec → 实现 → gatekeep →
> 真实模型 live E2E(本地 rig,用最弱模型验收,seeded 测试绿 ≠ 过)。

## 背景诊断(已核实,file:line 见各项)

体感差距 = 三个正交旋钮同时变差:
1. **信息食谱**:人设缺席、工具字段缺失、2k/8k 截断 —— 纯工程,可修。
2. **Harness**:自研 loop 无"先读再说"机制 —— 可修,且 V2 拥有循环控制权,
   能做 V1 做不到的确定性注入。
3. **模型本性**:BYOK 弱模型(Flash/Mini)裸 API 上天生不爱调工具 ——
   prompt 治不了,靠 harness 绕(确定性预取 / tool_choice 强制)。

## 债务清单

### P0-1 人设注入缺失(最大单一因素)ⓘ无隐私增量
- **事实**:`persona` 在整个 `backend/model_api_runtime/v2/` 不存在。V1 spawner
  把 genesis 人设 MD 全文预埋 system prompt(spawners.py:824-890,无截断);
  V2 的 `trusted_system_blocks` 只装 `/skills/*` 文档(serve_worker.py:141-151),
  PROFILE 双字段开关=0。模型只见通用一句 "You are the user's personal
  companion"(context.py:128)。
- **修法**:chat + wake 两个 lane 的 system 侧无条件注入 genesis 人设全文
  (缓存稳定前缀内;人设 V1 时代本就明文进 provider,无新增隐私暴露)。
  无人设(未过 genesis)时保持现状。
- **验收**:live E2E,弱模型,不给它调工具的机会(首轮即答)也能以人设身份
  回答"你是谁";人设改动后下一 turn 生效。

### P0-2 心跳缺"社交自觉信号" + nudge 注入形态错误
- **事实**:V1 wake 有 `attention_facts`(上次用户消息距今、24h 内已主动出现
  次数、recent chat 新鲜度标注,chat_resident_consumer.py:10669-10678)+
  "presence check,说与不说同等有效,按你自己的性格决定" prompt +
  "Never mention this wake or any system wording" 硬禁令。
  V2 wake 三者皆无;且 `_WAKE_NUDGE`(worker.py:864)以 **user 角色**塞进
  tail,被弱模型当"用户提问"回复 → 第三人称/复述系统措辞;缺自觉信号 →
  每个 timer tick 必说话("定时器痕迹")。
- **修法**:①wake 的 runtime/temporal 上下文补 `attention_facts` 等效字段
  (last_user_message_age、visible_proactive_count_24h、tail 新鲜度);
  ②nudge 改非对话形态注入(system 侧或 metadata 块),文案向 V1 的
  "同等有效/按性格决定"语义靠拢;③补 "永不向用户提及本次唤醒或任何系统
  措辞" 禁令;④评估补 self-loop guard(V1: 连续自唤醒≥3 强制静默)与
  90s 撞车门等效物。
- **验收**:live E2E 心跳探针 —— 高"24h 已主动次数"情境下弱模型选择沉默;
  说话时零系统措辞泄漏、零第三人称。

### P1-3 确定性记忆预取(把"自觉"变"保证")
- **事实**:V2 记忆 pull-only,弱模型经常一轮不调工具直接回复 → 长期记忆
  事实上不在场。V1 靠 claude driver 的 agentic 性格兜着,V2 没有。
- **修法**:harness 在首轮 LLM 调用前用用户消息做一次服务端 memory 检索,
  命中卡确定性注入 prompt(计入 turn 预算);模型仍可自主再查。
  可选:按模型档位分策略(弱模型重预取,强模型放自主)。
- **验收**:E2E —— 弱模型 + 涉及旧记忆的提问,回复中体现命中卡内容,
  全程 0 次模型主动工具调用也成立。

### P1-4 文档/MD 读取:分页 + 放宽文档类截断
- **事实**:"V2 不适合读 MD" 不是架构性质,是两个常量:工具结果每条 2000 /
  每轮 8000 字符(worker.py:527-529)。模型侧无文件读取工具
  (`chat_file_read` 被排除,tool_schema.py:29),仅 `workspace_read`。
- **修法**:`workspace_read` 加 offset/limit 分页(对齐 Claude Code Read
  语义);文档类结果单独预算;截断标记必须含 total/returned(已在
  memory_index 做过同款,照搬)。
- **验收**:E2E 读一篇 >20k 字符 MD,模型能分页读完并正确复述尾部内容。

### P1-5 工具面逐项 parity 审计(claude4 自任)
- **事实**:V2 工具面是重写的,V1 的字段/措辞打磨没当 spec 继承。已证实两例:
  memory_index 缺 bucket/thread/total(已修),wake 缺 attention_facts(P0-2)。
- **做法**:io_cli 全动词表(spawners.py:46-103)× tool_schema.py 逐字段对照,
  含 agent_tools_prompt.md 里的使用指引措辞;产出差异表,增补进本台账。

### P2-6 tool_choice 强制首轮 / 分模型策略(P1-3 落地后评估)
### P2-7 撞车门与频控 parity(并入 P0-2 ④评估结果)

### GATED(需 Seven 拍板,不阻塞上述)
- **PROFILE 双字段 rollout**(agent_memory+user_profile):隐私代价 = 花园
  明文进 provider;回滚铁律:先关 DETERMINISTIC 再关 PROFILE。
  P0-1/P1-3 落地后再评估边际收益,拿数据找 Seven 决策。

## 度量
- 每项各自的 live E2E 探针(本地 rig:serve_dev + dev enclave + 本地 PG,
  配方见 /tmp/dream-e2e 与 docs/testing/)。**验收模型 = 可拿到的最弱真实
  模型**(deepseek flash 档),seeded/mock 测试只算回归网不算验收。
- 程序级验收:P0+P1 全落地后,同账号同模型 V1/V2 并排盲评一轮
  (人设一致性 / 记忆命中 / 主动消息自然度)。

## 分工
- codex4:后端实现(逐项 spec 在 mailbox)。
- claude4:spec、gatekeep、P1-5 审计、E2E 探针与验收、本台账维护。
