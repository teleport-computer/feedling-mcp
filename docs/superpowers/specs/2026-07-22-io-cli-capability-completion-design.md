# io_cli 能力补全与通道收口 — 设计

- 日期:2026-07-22
- 基线:`test`(**双基线策略**,hx 拍板:本分支发 test→main 直接线上验证;
  0727 合 pre 时按 §6 迁移计划搬,不做两套设计)
- 分支:`feat/io-cli-capability-completion`
- 相关:pre 上的 `fix/redistill-merge`(蒸馏保字段,待合)、
  `feat/inject-io-cli-capabilities`(VPS 能力注入,**本 spec 第 7 项接管,原分支作废**)

## 1. 背景:这次要解决什么

一次全面审计(2026-07-22,从「改名后自我介绍留旧名」查起)发现 agent 工具体系
是"稀里糊涂跑通"的:

| # | 问题 | 用户症状 |
|---|---|---|
| P1 | `identity-write` 只有介绍+签名两个参数;七维连 CLI 包装都没有 | "以后叫我老张"/"说话温柔点"全改不动 |
| P2 | 云端命令白名单缺 memory-delete/schedule-wake 等,但唤醒文案在宣传它们 | 模型被教了跑不动的命令,静默失败 |
| P3 | 回复 JSON 夹带通道按**前缀透传**(`memory.*`/`identity.*` 全放),整个绕过白名单;词汇表一半靠模型猜、consumer 归一层追认 | 删记忆"碰巧能用";能力边界没人说得清 |
| P4 | 工具文案四处手写(云端 md/白名单/skill 文档/consumer 内嵌),互不同步 | 能力时灵时不灵;每加能力人肉改四处 |
| P5 | `identity.replace`(整卡覆盖)与 `identity.profile_patch`(局部合并)并存,无使用原则 | 历次丢字段事故都出在这条缝上 |
| P6 | 直接对话(终端跟常驻 agent 聊)没有二次蒸馏入口 | 用户说"重新总结你自己"=死路 |
| P7 | VPS agent 的工具清单是 onboarding 时一次性抄进 agent-home/CLAUDE.md 的,**之后永不更新** | io_cli 加了新参数(如 --agent-name),老用户的 agent 永远不知道 → 改名不成功、不报错 |

## 2. 目标 / 非目标

**目标**:每个能力要么被正式支持(声明、文案、放行三者一致),要么明确不存在。
用户口头明确要求改身份卡的任何字段,都能生效。

**非目标**:
- 不做 V2 云端注册表本体(架构层,归 zhihao);
- 不退役夹带通道(只收口,防止"本来能跑的功能改挂了");
- 不引入"清空字段"语义(现契约区分不了 没提到/空值/要清空,另议);
- 不动正常聊天路径的任何现有行为(验收标准:现役功能逐字节不变)。

## 3. 设计

### 3.1 `identity-write` 全字段(解 P1)

命令形态(全部可选,至少传一个;没传的字段不动——底层 `identity.profile_patch`
本来就是局部合并):

    python io_cli.py identity-write \
      [--agent-name X] [--self-introduction X] [--category X] \
      [--user-preferred-name X] [--agent-role X] [--tone-style X] \
      [--custom-persona-prompt X] [--language-preference X] \
      [--relationship-anchor X] \
      [--signature S ...] [--boundaries S ...] [--do-not-say S ...] \
      [--stable-definitions S ...] \
      [--nudge-dimension "幽默:+5" ...]

- **list 字段**用重复 flag(`--signature "得嘞" --signature "包我身上"`),整组替换。
- **七维只微调不设值**:`--nudge-dimension name:±delta`,单次 |delta|≤10,
  底层走既有 `identity.dimension_nudge`(服务端已存在,只缺 CLI 包装)。
  防"你开朗点"一句话把维度拉爆。
- **本地预检**复用 `card_policy`(与服务端同源):runtime 名拒绝、维度结构校验。

**名介联动硬护栏**(与 test 蒸馏侧 303a9439 同口径):
传了 `--agent-name` 且没传 `--self-introduction` 时,CLI 先 `identity-read`,
若现有自我介绍**包含旧名字** → 直接 exit 2,报错要求同一次调用一起更新介绍。
把"该一起改"从 prompt 软规则变成 CLI 硬校验,不靠模型自觉。
(介绍里不含名字时放行——不制造无谓摩擦。)

help 文本即三护栏文案,与 V2 `tool_schema.py` `identity_patch` 描述口径一致(§6):
① 仅用户显式请求才改;② 改名必须同步介绍里的名字;③ 七维只微调。

### 3.2 云端文案从白名单生成(解 P4 云端半)

spawner 拼 system prompt 时,工具清单段不再手写:从 `_IO_CLI_VERBS` 逐 verb 跑
`io_cli <verb> --help` 现场生成命令+真实 flag 清单(解析逻辑与 3.7 共用),
渲染进 `agent_tools_prompt.md` 的占位符。**文案=白名单,结构性一致**,
"文案说有白名单没给/白名单给了文案没教"两种病根治。

### 3.3 云端白名单补齐(解 P2)

`_IO_CLI_VERBS` 增补:`memory-write`、`memory-patch`、`memory-delete`、
`schedule-wake`、`cancel-wake`(与 VPS 对齐)。

依据:这**不是扩权**——唤醒文案已在宣传这些能力,夹带通道实际已放行等价操作
(前缀透传),补齐命令通道只是让已存在的能力走正门、有权限闸可管。

### 3.4 夹带通道类型白名单(解 P3,只收口不缩能力)

`execute_agent_actions` 从"前缀透传"改为**显式类型白名单**:

    memory.add  memory.create  memory.add_correction  memory.patch
    memory.content_patch  memory.supersede  memory.upgrade  memory.delete
    identity.profile_patch  identity.patch

- 白名单 = 现役全部类型(含归一层认识的所有别名),**行为零变化**——
  用户已依赖的"对话删记忆"继续可用,收口=转正,不是砍掉。
- `identity.replace` 明确不在白名单(见 3.5);未知类型:日志+计数+丢弃,
  不发服务端,消息正常发出(不崩回合)。
- kill switch:`FEEDLING_ACTION_ALLOWLIST=off` 回到前缀透传,默认 ON。
  该关的症状:某个本来能用的夹带动作突然全部静默失效。

### 3.5 写卡原则:只有蒸馏任务可 replace(解 P5)

原则:**`identity.replace` 只许蒸馏任务上下文使用;其余一切写卡走 patch。**
服务端 gate 已存在(无蒸馏上下文 403),本次补**守卫测试**锁死防回归,
并把原则写进 `identity/actions.py` 模块注释——这是三条写卡路径的统一合并语义:
patch=局部合并(没提的不动),replace=蒸馏产完整卡(由代码合并保证完整,见 3.6)。

### 3.6 直接对话蒸馏入口(解 P6)

新 verb:

    python io_cli.py identity-redistill --material-file <path> | --material-text "..."

- 行为:POST 创建 `update_identity` 蒸馏任务(复用现有任务表/蒸馏车道),
  立即返回 job id,不阻塞等结果;蒸馏车道照常:读旧卡 → agent 只产
  「新材料涉及的字段」 → **代码字典合并**(旧卡为底、增量覆盖,同
  pre `fix/redistill-merge` 的形状)→ `identity.replace` 落库。
- **test 侧连同代码合并一起实现**(否则新入口带着已知丢字段 bug 上线);
  0727 与 pre 版本对齐取一(形状相同,见 §6)。
- 护栏:材料上限 64KB;同一用户同时只允许一个未完成 redistill job;
  help 文案写明"仅用户明确要求重新总结时使用"。

### 3.7 VPS 能力实时注入(解 P7,接管自 `feat/inject-io-cli-capabilities`)

**问题缘由**:对话改名功能上线后,VPS 老用户的 agent 不知道有 `--agent-name`
——它的工具清单是 onboarding 时一次性写进 agent-home/CLAUDE.md 的,永不更新。

**方案**(移植原分支三个提交,base 从 pre 换成 test;原实现已过两轮 Codex、
407 测试绿、真模型 A/B 8/8):

- consumer 前台回合注入「当前真实 io_cli 能力清单」,逐 verb 从
  `io_cli <verb> --help` 现场解析真实 flag 生成——**io_cli 变,清单自动变**,
  与 3.1 组合:新参数上线即被所有 VPS 老用户的 agent 看到,零文档工作。
- 继承原分支的全部关键决策(不重新发明):
  - `[setup]`/`[ops]` help 标记过滤装机类 verb,不硬编码清单;
  - 只缓存完整成功的块;残缺(某 verb --help 失败)不入缓存、下轮重试;
  - resume 型 driver(claude/pi/hermes)每会话注一次;codex 不 resume,每轮注;
  - 注入点在前台组装链 `_foreground_agent_message` **之前**
    (顺序不能反:transcript header 必须在最顶,否则 --resume 抑制失效);
  - **hosted 路径逐字节不变**(`_HOSTED` 直接 return);http backend 不注入。
- 原分支处置:三个本地提交 `git bundle` 存档到 `ops/archive/` 后废弃,
  worktree 按删除清单走(hx 拍板后执行)。

## 4. 测试标准(按 ops/TEST_STANDARD.md §2 对号)

- **单测**:io_cli 逐字段 parser;名介联动硬校验(含旧名/不含旧名/两参齐传);
  nudge 上限;夹带白名单(放行/拦截/未知丢弃/开关 off 回退);replace 守卫
  (非蒸馏上下文 403);redistill 建 job 与并发上限;注入块解析(移植原测试)。
- ⚠️ `tests/conftest.py` `_PURE_UNIT` 坑:新测试文件写完必跑 `--collect-only`
  核对;需 DB 的测试**不进**白名单。
- **真模型 e2e**(prompt 行为单测抓不到,本地起服务):
  ① VPS 改名 → 名字与介绍同步变;② "以后叫我老张" → user_preferred_name 生效;
  ③ 对话删记忆走命令通道成功;④ 直接对话丢材料重新总结 → 卡更新且
  手写人设(custom_persona_prompt)保留;⑤ 老会话(旧 CLAUDE.md)注入后能用新参数。
- **回归**:现役聊天路径逐字节不变;夹带通道现役动作全部继续可用。

## 5. 风险与高亮

- **动了共享 consumer**(host+vps 共用):3.4/3.6/3.7 三项。全部为加法+
  显式化现状;唯一收窄面(3.4)有默认 ON 的 kill switch。**合并/推送节奏由 hx 拍板。**
- 3.7 注入会加长前台 prompt(每会话一次);原分支已实测无回归,移植后复测。
- test 侧 3.6 与 pre `fix/redistill-merge` 存在**故意的重复实现**(双基线代价),
  0727 取一,见 §6。

## 6. 迁移计划(0727 test→pre 时照此执行)

| 本分支项 | pre/V2 侧现状 | 合并动作 |
|---|---|---|
| 3.1 identity-write 全字段 | pre 的 io_cli 已有 `--agent-name`(子集) | **冲突取本分支**(超集);V2 云端镜像=`tool_schema.py` `identity_patch`(~L237),字段对齐 |
| 3.1 三护栏文案 | V2 描述是软措辞("Pass both when…") | 照本 spec 3.1 改硬;名介联动硬校验在 V2 侧落 `capabilities/identity.py` patch 前置检查(CLI 校验够不到原生 tool call) |
| 3.2/3.3 云端文案生成+白名单 | pre 自 0722 双运行时,V1 spawners 仍在跑 | **直接合入继续生效**(迁移期 V1 用户仍用);V2 全量切换后自然退役 |
| 3.4 夹带白名单 | pre consumer 保留(VPS 线永续) | 直接合;若 `feat/inject-io-cli-capabilities` 残留在 pre,以本分支移植版为准 |
| 3.5 replace 原则+守卫测试 | 后端共享,两边同代码 | 直接合 |
| 3.6 直接对话蒸馏入口 | pre 有 `fix/redistill-merge`(合并逻辑同形状) | 合并逻辑**二选一**(优先已实测的一方),入口代码直接合 |
| 3.7 VPS 实时注入 | 原分支在 pre(3 提交,未推,将 bundle 存档作废) | 以本分支移植版为准;pre 侧无需再合原分支 |
| (已合 test)蒸馏名介规则 303a9439 | `distill_prompt_v1.py` 注释已标 V2 位置 | 随 test→pre 自然合入,按注释改 `tool_schema.py` |

## 7. 开放问题

1. 3.3 补齐后,云端 agent 首次获得删记忆的**命令**通道(此前只有夹带旁门)——
   要不要顺手在云端工具文档里给删除加"需用户明确要求"的护栏文案?(倾向加)
2. `identity-redistill` 的服务端入口:复用现有建 job 端点 vs 新端点,
   实现阶段按现有蒸馏任务表结构定,不影响本设计。
