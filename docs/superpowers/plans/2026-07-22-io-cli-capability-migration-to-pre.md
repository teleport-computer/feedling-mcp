# io_cli 能力补全 → pre(Runtime V2)迁移手册

- 写于:2026-07-23,基于 `feat/io-cli-capability-completion` 分支实际落地代码
  (HEAD `f5a21e21`,相对 `origin/test` merge-base `c79352c3`,40 commits)。
- 用途:2026-07-27 22:00 `test → pre` 合并时,逐项照此执行。**每一条都已对照
  pre 实际代码核实**(`git fetch origin pre` 后直接读取 `origin/pre` 上的真实
  文件),不是从 spec 抄的猜测。
- 配套:spec `docs/superpowers/specs/2026-07-22-io-cli-capability-completion-design.md`
  §7 是本文档的骨架来源;本文档是它的展开+核实版,以本文档为准(spec 若与
  本文档冲突,以本文档实查结果为准,因为 spec 写于 pre 代码 2026-07-22 那天的
  快照,pre 之后可能又变了——合并前务必重新 `git fetch origin pre` 复核一遍
  下面每条的"pre 实查"栏)。
- 前提:合并节奏(何时合、要不要走 PR)由 hx 拍板,本文档只管"合的时候怎么
  解决冲突、怎么验",不代替 hx 的合并决策。

## 如何使用本文档

1. 先做 §0(DB migration 的 merge revision)——它是唯一会让服务**起不来**的
   一项,必须在别的代码冲突解决之前先跑通。
2. 再按 ①~⑦ 顺序处理业务代码冲突(顺序不严格,但 ②的两阶段发布**内部顺序
   不能乱**,见该节)。
3. 每项做完立刻跑该项的验证命令,不要攒到最后一次性验。
4. 全部做完后跑一遍"收尾验证"整节。

---

## §0. Alembic:合并 head + TEE 镜像 + 排他索引清理

**现状(2026-07-23 实查)**:
- `test`(本分支)head = `0023_redistill_job_exclusivity`(线性链,无分叉)。
- `pre` head = `0052_dual_runtime_coexistence`(`0049` 是 test/pre 上一次
  合流的 merge revision,之后 pre 单独又加到了 0052)。
- 两条链在 `0049` 之后完全独立,`0023` 与 `0050/0051/0052` 互不认识对方。

**冲突解法**:合并时用 `alembic merge` 生成一条新的 merge revision,
`down_revision` 同时指向 `0023`(test 侧)和 `0052`(pre 侧,合并时以 pre
实际 head 为准,可能已经不是 052 了,先跑 `alembic heads` 确认)。

**动作**:
1. `alembic heads` 确认此时 pre 分支上的真实 head(可能已经推进,不要硬编码 0052)。
2. `alembic merge -m "merge test io_cli-capability-completion into pre" <test_head> <pre_head>`
   生成合并 revision 文件,落在 `backend/alembic/versions/`。
3. **上线前清理重复 active redistill job**:`0023_redistill_job_exclusivity.py`
   的 `upgrade()` 里已经带了一段防御性清理(把同用户下除最新一条外的所有
   `active`(`awaiting_resident`/`processing`)状态的 `source_kind=
   'resident_redistill'` job 标记为 `failed`),**但它假设的前提是这个
   job kind 在 pre 上此前从未存在过**(本分支 T10/T11 才引入
   `resident_redistill` 这个 source_kind)。如果 pre 在 0727 之前已经有人
   手工造过测试数据、或者 pre 分支自己独立造了同名 job kind,先用下面的
   SQL 确认一遍再跑 migration:

       SELECT user_id, count(*) FROM genesis_import_jobs
       WHERE source_kind = 'resident_redistill'
         AND status IN ('awaiting_resident', 'processing')
       GROUP BY user_id HAVING count(*) > 1;

   有结果 → 索引创建会失败,需要先手工清理(参考 `0023` 里的清理 SQL 写法)
   再跑 migration。预期是空结果(这个 job kind 是本分支新引入的)。
4. **TEE 镜像 schema 同步评估**:`backend/alembic_tee` 是否需要对应索引,
   属于本分支未解决的开放问题(`0023` 迁移文件 docstring 和
   `backend/genesis/genesis_core.py` 的 V2 NOTE 都标了这一点,没有代码动作)。
   合并时需要找 TEE 侧负责人确认一遍——如果 TEE 镜像的排他约束不同步,
   redistill 的"数据库层排他"保证在 TEE 影子库里就形同虚设。

**验证命令**:
```
alembic upgrade head       # 确认合并后能跑通,不报冲突/循环依赖
alembic current            # 落在新 merge revision 上
alembic downgrade -1 && alembic upgrade head   # 回滚再升级一次,冒烟验证
pytest tests/test_redistill_job_exclusivity.py -q   # 4 passed(索引语义本身不受合并影响)
```

**精确锚点**:
- `backend/alembic/versions/0023_redistill_job_exclusivity.py`(本分支新增,
  docstring 里已经写了"V2 NOTE (2026-07-27 pre-merge)")
- `backend/genesis/genesis_core.py::_resident_sealed_import`(同款 V2 NOTE 注释)

---

## ① `tools/io_cli.py`:identity-write 全字段 vs pre 的 3 参数版

**pre 实查**(`origin/pre:tools/io_cli.py`):`identity-write` 仍是老的
`_identity_write_payload(self_introduction, signature, agent_name=None)`,
只有 3 个 flag(`--agent-name` / `--self-introduction` / `--signature`,
wire 形状 `{"action": {...}}`)。本分支(T5)重写为 13 字段 + 4 个 list 字段
的 add/remove/replace 三操作 + `--nudge-dimension` 七维微调,wire 形状改成
`{"actions": [...]}`(向后兼容,`identity_core.run_actions` 本来就同时接受
两种形状,服务端无需改动)。

**冲突解法**:pre 是子集,**直接取本分支超集**,pre 侧改动(如果有,预期
没有,因为 pre 在这方面基本没动过这个函数)全部丢弃。合并后 pre 上
`identity-write` 的命令行为整体切换成本分支这版。

**精确锚点**:`tools/io_cli.py::_identity_write_payload_v2` (`_STRING_FIELDS`
9 项 + `_LIST_FIELDS` 4 项 + `_parse_nudge_dimension`)、`cmd_identity_write`、
argparse `identity-write` 子命令定义(21 个新 flag)。

**验证命令**:
```
pytest tests/test_io_cli_identity_write_full.py -q      # 36 passed(31+5 I4 补丁)
pytest tests/test_io_cli_identity.py tests/test_io_cli_parser.py -q
FEEDLING_TEST_PG=postgresql://localhost:1/none pytest tests/test_io_cli_identity_write_full.py --collect-only -q
```

---

## ② V2 镜像三件套:`capabilities/identity.py` + `capabilities/tool_schema.py`

这是全部迁移项里**最需要谨慎**的一项,因为 pre 现在的 `identity_patch`
是 V2 原生 tool-calling 唯一入口(agent 走 native tool call,不经过
io_cli/consumer,3.1/3.4 说的"CLI 预检"和"consumer 漏斗"两条防线在 V2
完全不存在)——**成对闸必须落在这里,否则 V2 上改名可以完全不带介绍**。

### pre 实查:三处现状

1. **`backend/capabilities/tool_schema.py::PARAMS["identity_patch"]`**
   (~L48-57)只有 `{patch, agent_name, self_introduction, signature}` 四个
   顶层字段 —— 本分支的 9 个字符串字段(`category`/`user_preferred_name`/
   `agent_role`/`tone_style`/`custom_persona_prompt`/`language_preference`/
   `relationship_anchor`,加上已有的 `agent_name`/`self_introduction`)、
   4 个 list 字段的 add/remove/replace 三操作、以及**七维 nudge 完全没有
   对应的 capability/tool_schema 条目**——pre 上 V2 原生 agent 目前压根
   没有 nudge 能力,这比 spec §7 表格写的"字段对齐"要多一步:**要新增
   一个 `identity_dimension_nudge` capability + tool_schema 条目**,不是
   只改字段列表。
2. **`backend/capabilities/tool_schema.py::DESCRIPTIONS["identity_patch"]`**
   (~L237-242)措辞是建议性的("Pass both when the new name should also
   appear..."),不是硬规则。`backend/identity/distill_prompt_v1.py` 里
   已经有一条现成的 V2 NOTE(commit `303a9439`,已在 `origin/test` 上,
   随本次合并自然带过去)明确点名了这一行,要求改成硬规则:
   > "if self_introduction names the companion, a rename MUST update it
   > in the SAME patch, or the card shows one name and introduces itself
   > with another."
3. **`backend/capabilities/identity.py::merge_patch_fields()` /
   `patch()`**(~L29-71)——**这是本条最关键的实查发现**:`merge_patch_fields`
   的 docstring 已经自己写明了一个陷阱,原文摘录:
   > "tool_schema's validator ALSO gates replay of already-persisted
   > effects (serve_worker validates a decrypted effect through it). A
   > new rejection rule there would re-interpret payloads enqueued by a
   > pre-upgrade worker as invalid, and a validation failure becomes a
   > plain RuntimeError, which the outbox treats as retryable — so a
   > legal-when-written effect would retry forever instead of applying.
   > Rejections belong in `patch()` below, where `retryable=False` maps
   > to a terminal discard."

   也就是说:pre 的 V2 runtime 有一个**持久化 effect 重放机制**
   (`backend/model_api_runtime/v2/effect_outbox.py`,`_PENDING_EFFECT_STATUSES
   = {"pending", "pending_fenced_v1"}`)——一个 identity_patch 调用先被
   写入 outbox,再由 `serve_worker` 异步取出重放执行。**重放时会再跑一遍
   `tool_schema.validate_tool_args`**(`tool_schema.py:354` 附近的
   `identity_patch` 分支,复用同一份 `merge_patch_fields`)。如果直接在
   这个校验函数里加"改名必须带介绍"的拒绝规则,部署那一刻 outbox 里所有
   "升级前入队的、只改名不带介绍的旧 effect"重放时会被判定非法——而
   `retryable=False` 意味着**终态丢弃,不是重试**,用户的改名请求会
   静默消失,且没有任何报错(agent 已经在旧一轮回复里说"改好了")。

### 合并动作(两阶段发布,R2-I2)

**不能一步到位加闸。** 按下面顺序:

1. **第一阶段(随本次合并一起做)**:
   - 字段对齐:`tool_schema.py::PARAMS["identity_patch"]` 补齐 9 个字符串
     字段 + 4 个 list 字段的 add/remove/replace 键名(与本分支
     `backend/identity/actions.py::_LIST_OP_FIELDS` 逐字段对齐,那里已经
     留了 V2 migration 注释指向这里——见 `actions.py` ~L811-813)。
   - 新增 `identity_dimension_nudge` capability(`capabilities/identity.py`
     新函数)+ `tool_schema.py` 对应 `PARAMS`/`DESCRIPTIONS` 条目
     (字段:`dimension` + `delta`,单条 |delta|≤10 由服务端
     `backend/identity/actions.py::_identity_dimension_nudge` 已经兜底,
     参见该文件 ~L1420-1426 的 V2 migration 注释)。
   - `DESCRIPTIONS["identity_patch"]` 措辞按 `distill_prompt_v1.py`
     `303a9439` 的注释改硬(改名必须同次带介绍,不再是"建议")。
   - **本阶段先不加任何服务端拒绝逻辑**——只是让 V2 原生 agent 的
     prompt/schema 描述"应该"带介绍,靠模型自觉,暂不服务端强制。这一步
     保证所有**新产生**的 identity_patch 调用(deploy 之后)都是从新
     schema/新 prompt 发出的,自然会带 self_introduction(不保证 100%,
     但 outbox 里不会再有"旧版本模型产生的不带介绍的合法 effect"持续
     堆积)。
2. **等待 drain**:确认 `effect_outbox` 里在部署时间点之前入队的、
   `type` 落在 identity 相关(`_LEGACY_SENSITIVE_EFFECT_TYPES` 包含
   `"identity"`)的 `pending`/`pending_fenced_v1` 状态 effect 已经全部
   被 `serve_worker` 处理完(应用或终态失败),不再有"旧版本产生的
   effect 还在排队"。可以查:

       SELECT count(*) FROM v2_effect_outbox
       WHERE status IN ('pending', 'pending_fenced_v1')
         AND effect_type = 'identity'
         AND created_at < '<部署时间戳>';

   （具体表名/字段名以 pre 上 `backend/alembic/versions/0027_v2_effect_outbox.py`
   实际 schema 为准,上面是示意)。
3. **第二阶段(drain 确认干净后,单独一次小改动)**:在
   `tool_schema.py::validate_tool_args` 的 `identity_patch` 分支(紧跟
   `merge_patch_fields` 调用之后)加真正的拒绝规则:`merged` 里
   `agent_name` 非空但 `self_introduction` 为空 → 返回校验错误字符串
   (走 `retryable=False` 终态丢弃路径,这时可以接受,因为已确认没有
   "合法旧 effect" 会撞上这条新规则了)。

**补一条兼容测试**(第二阶段和第一阶段之间都要跑,写在 pre 侧
`tests/test_capabilities_identity.py` 或同类文件):模拟一个"部署前入队
的旧版 payload"(`{"agent_name": "老六"}`,不带 `self_introduction`)在
**加闸之前**跑 `validate_tool_args("identity_patch", ...)`,断言仍然
`None`(通过);在**加闸之后**跑同一个 payload,断言明确返回错误字符串
且是"预期内拒绝"而不是意外 500——这条测试的真正目的不是测新规则本身
(那是常规单测),而是**留一个显式的、命名清楚的锚点**,证明"旧 payload
在两个阶段分别会发生什么",避免后续有人看到"拒绝旧 payload"就以为是
bug 反手改掉。

**验证命令(本分支侧,字段来源不变,回归用)**:
```
pytest tests/test_identity_list_ops.py tests/test_identity_nudge_cap.py \
       tests/test_identity_rename_pairing.py -q
```
（pre 侧的新增测试要在 pre 分支上跑,不在本分支范围内。）

**精确锚点(pre 侧,均已通过 `git show origin/pre:<path>` 核实存在)**:
- `backend/capabilities/tool_schema.py` L48-57(`PARAMS["identity_patch"]`)、
  L237-242(`DESCRIPTIONS["identity_patch"]`)、L354-365
  (`validate_tool_args` 里 `identity_patch` 分支)
- `backend/capabilities/identity.py` L29-71(`_TOP_LEVEL_PROFILE_FIELDS` /
  `merge_patch_fields` / `patch`)
- `backend/model_api_runtime/v2/effect_outbox.py`
  (`_LEGACY_SENSITIVE_EFFECT_TYPES`、`_PENDING_EFFECT_STATUSES`)

**精确锚点(本分支侧,V2 migration 注释已预留)**:
- `backend/identity/actions.py` ~L811-813(`_LIST_OP_FIELDS` 上方)、
  ~L1423-1424(`_identity_dimension_nudge` 单条限幅)、~L1574-1575(批量
  求和限幅)
- `backend/identity/distill_prompt_v1.py` ~L63-71(`303a9439` 的
  V2/pre 硬规则注释,已在 `origin/test`)
- `tools/io_cli.py` ~L3447-3448(`_identity_write_payload_v2` 上方的
  V2 镜像注释)

---

## ③ consumer:与 pre 侧 consumer 改动的合并次序

**pre 实查**:`tools/chat_resident_consumer.py` 在 pre 上存在(同一份文件,
VPS 自托管线路径,V2 dual-runtime 期间 V1 spawner 仍在跑,这条 consumer
路径与 hosted V2 registry 制完全独立、互不影响)。本分支在这个文件里做了
五处独立改动(T7 夹带白名单+结果真实化、T8 目录注入、T9 自更新卡因传导、
T11 identity-redistill IPC、T12 蒸馏合并 incremental 过滤),全部是 VPS 线
长期资产,pre 合并时原样保留。

**冲突解法**:**本分支改动在 pre 侧 consumer 改动之后合**(如果两边都改了
同一段代码,以本分支版本为准,重新在其基础上手工补 pre 侧那次改动的意图,
而不是反过来)。理由:本分支对 `execute_agent_actions` / 前台/proactive
回复改写逻辑做了 4 轮 review 才收敛(见 task-7-report.md 的 round 1-4),
是这块代码当前最新、最经过验证的版本;倒着合容易把已经堵上的漏洞
(C1 串行短路、I3 漏斗成对校验、I5 语言判断)重新打开。

**已知残留**:`feat/inject-io-cli-capabilities`(参考分支,VPS 目录注入
的早期尝试)如果在 pre 上还有残留代码,**以本分支 T8 重写版为准**,直接
覆盖/删除那个分支的痕迹——这是 spec 开头就写明的("原分支代码本分支重写,
原分支 bundle 存档后废弃")。

**精确锚点**:
- T7:`canonicalize_action_type` / `_ACTION_ALLOWLIST` / `execute_agent_actions`
  / `rewrite_reply_for_outcomes`(~L2204 起,V2 NOTE:"V2 云端无夹带通道
  (原生 tool loop);本收口属 VPS 线长期资产,0727 合并原样保留")
- T8:`_prepend_io_cli_capability_catalog` / `_commit_io_cli_catalog_injection`
  (~L1987 起,V2 NOTE 同上一条措辞)
- T9:`_self_update_stall` / `_self_update_stall_reason()`(~L1877,注释
  "VPS 线长期资产(自托管专属;hosted 走不到这条路径);pre 合并原样保留")
- T11:`_handle_redistill_ipc` / `_redistill_ipc_serve_forever`(~L3303,
  同一族 V2 NOTE:hosted 无 io_cli.py 子进程,这条 IPC 车道没有 V2 对应物)
- T12:`_resident_incremental_payload`(consumer 侧过滤,配合服务端合并)

**验证命令**:
```
pytest tests/test_chat_resident_consumer.py -q     # 456 passed(基线不能破)
pytest tests/test_consumer_action_admission.py tests/test_consumer_capability_inject.py \
       tests/test_update_stall_reason.py tests/test_identity_redistill_ipc.py -q
```

---

## ④ Alembic(已并入 §0,此处不重复)

---

## ⑤ 蒸馏合并逻辑:服务端版 vs pre 的 `fix/redistill-merge`

**pre 实查**:`fix/redistill-merge` 分支(如果尚未合入 pre 主线)是
**consumer 侧**合并——即让 consumer 自己在拼蒸馏请求之前做"没提字段用旧值
补全"这类逻辑。本分支 T12 把合并逻辑挪到了**服务端**
(`backend/genesis/service.py::_merge_identity_replace_payload` +
`replace_identity_preserving_anchor` 的 CAS 化改造),覆盖 3 个调用方
(redistill 的 `identity.replace`、云端 `update_identity` job、
`_write_back_plaintext_user_name`),不只是 redistill 一条路径。

**冲突解法**:**取本分支服务端版**。如果 `fix/redistill-merge` 已经先
合进了 pre,合并时把它 consumer 侧的合并逻辑**移除**(改回"consumer 只管
产出增量 diff,合并交给服务端"),避免两层合并重复处理导致语义漂移
(例如 consumer 已经把某字段填成旧值,服务端又按"非空字段覆盖"逻辑把这个
"旧值填充"当成"新材料确实提到了这个字段"处理,结果制造出一个死循环式的
自我确认,而不是真正的"没提=不动")。如果 `fix/redistill-merge` 还没合,
直接不合它,以本分支这版为准。

**精确锚点**:
- `backend/genesis/service.py::_merge_identity_replace_payload`(新函数,
  基于 `card_policy.PROFILE_STRING_FIELDS`/`PROFILE_LIST_FIELDS` 做
  key 级覆盖,非空才覆盖)
- `backend/genesis/service.py::replace_identity_preserving_anchor`(CAS化,
  复用 `identity_service.identity_mutation_lock` / `_save_identity_cas` /
  `IdentityWriteConflict`,最多重试 3 次)
- `backend/identity/actions.py::_identity_replace_action` 里被改写的
  "KNOWN RESIDUAL" 注释(从"仍是已知残留"改成"已修复")
- `tools/chat_resident_consumer.py::_resident_incremental_payload`
  (consumer 侧的增量过滤,是本分支唯一保留的 consumer 侧逻辑,与服务端
  合并互补而非重复——它防止把"没变化"的字段也发一遍占用 action 名额,
  不做语义合并)

**验证命令**:
```
pytest tests/test_redistill_server_merge.py -q                 # 6 passed
pytest tests/test_genesis_service.py tests/test_resident_identity_distill.py -q
pytest tests/test_identity_concurrency_baseline.py tests/test_genesis_plaintext_routes.py \
       tests/test_genesis_notice.py tests/test_identity_replace_action.py \
       tests/test_identity_redistill_ipc.py -q
```

---

## ⑥ 云端白名单补齐(T13)+ hosted 目录渲染

**pre 现状**:V1 spawner(`backend/agent_runtime/spawners.py`)在 dual-runtime
期间与 V2 registry 制并存,`_IO_CLI_VERBS` 是这条 V1 路径专属的 Bash 沙箱
白名单,只影响走 claude driver 的 hosted V1 agent。

**合并动作**:直接合入,迁移期(V1 spawner 还在跑)继续生效——新增的
`memory-write` / `memory-patch` / `memory-delete` / `schedule-wake` /
`cancel-wake` 五个 verb 授权、`_hosted_io_cli_catalog_text()` 目录自动渲染、
`_AGENT_PROMPT_FALLBACK_COMMANDS` 兜底文案(18 verb 全覆盖)都是纯 V1 侧
改动。**无需单独动作**:V2 全量切换、V1 spawners 退役时,这整块代码
(`spawners.py` 里的 `_IO_CLI_VERBS`/目录渲染函数、
`agent_runtime/agent_tools_prompt.md` 里的 `<io_cli_catalog>` 占位符)
随 V1 一起自然退役,不需要在 0727 这次合并里做任何特殊处理——只是原样
带过去,等 V1 真正下线那天再删。

**精确锚点**:
- `backend/agent_runtime/spawners.py`:`_IO_CLI_VERBS`(~L41,V2 NOTE:
  "V2 云端注册表制不用本渲染;过渡期 V1 spawner 仍用。0727 后随 V1 退役")、
  `_hosted_io_cli_catalog_text()`、渲染点(~L161)
- `backend/agent_runtime/agent_tools_prompt.md`:`<io_cli_catalog>` 占位符

**验证命令**:
```
pytest tests/test_spawners_catalog.py tests/test_agent_runtime_spawners.py -q   # 112 passed, 1 skipped
```

---

## ⑦ 已合 test 的改名规则(`303a9439`)→ 随合并自然进 pre

`303a9439 fix(identity): 改名时同步自我介绍里的旧名字` 早于本分支单独合过
`origin/test`,本次 test→pre 合并会自然带上。它自己在
`backend/identity/distill_prompt_v1.py` 留了 V2 NOTE(~L63-71),点名要求
把这条规则同步硬化进 `tool_schema.py` 的 `identity_patch` DESCRIPTIONS——
这一步已经并入上面 **②** 的"第一阶段"动作里,不用重复做,只是提醒:
②做完就等于把这条也一起做了,不要漏看成两件独立的事。

---

## 收尾验证(全部动作做完后跑一遍)

```
# DB
alembic upgrade head && alembic current

# 本分支贡献的全部新测试文件(pre 分支上路径相同)
pytest tests/test_identity_rename_pairing.py tests/test_identity_nudge_cap.py \
       tests/test_identity_list_ops.py tests/test_identity_replace_guard.py \
       tests/test_io_cli_identity_write_full.py tests/test_io_cli_catalog.py \
       tests/test_consumer_action_admission.py tests/test_consumer_capability_inject.py \
       tests/test_update_stall_reason.py tests/test_redistill_job_exclusivity.py \
       tests/test_identity_redistill_ipc.py tests/test_redistill_server_merge.py \
       tests/test_spawners_catalog.py -q

# 高风险共享文件的既有回归基线不能破
pytest tests/test_chat_resident_consumer.py -q          # 456 passed
pytest tests/test_identity_actions.py tests/test_asgi_identity.py \
       tests/test_identity_concurrency_baseline.py -q

# --collect-only 核对本分支新增的所有 _PURE_UNIT 白名单文件在无库环境下真的被收集
FEEDLING_TEST_PG=postgresql://localhost:1/none pytest --collect-only \
    tests/test_identity_rename_pairing.py tests/test_identity_nudge_cap.py \
    tests/test_identity_list_ops.py tests/test_io_cli_identity_write_full.py \
    tests/test_io_cli_catalog.py tests/test_consumer_action_admission.py \
    tests/test_consumer_capability_inject.py tests/test_update_stall_reason.py \
    tests/test_identity_redistill_ipc.py tests/test_redistill_server_merge.py \
    tests/test_spawners_catalog.py -q

# docs-site(pre 是否需要重新生成,见下方专节)
cd docs-site && npm run openapi:generate && git status  # 预期无 diff
npm run types:check && npm run lint && npm run build
```

### docs-site:pre 是否需要重新生成

本分支的 T14 已经把公共契约变化写进了 `docs-site/content/docs/changelog.mdx`
的 `Unreleased` 段(identity 全字段/list 操作/nudge 限幅/redistill 排他+
服务端合并)和 `self-hosting.mdx`(redistill 本机封装的信任保证)。这两个
文件本身是**内容合并冲突高发区**——如果 pre 上 `Unreleased` 段在 2026-07-22
之后也有别人新增的条目,合并时是文本冲突,需要手工把两边的 bullet 都保留
(不要选一边丢一边)。`public.json` 的 OpenAPI 契约经 T14 验证是 byte-identical
(这批改动都不涉及公共 HTTP schema 变化),pre 合并后**仍需重新跑一次**
`npm run openapi:generate` 确认无 diff(pre 自己独立的改动可能动了 OpenAPI,
不能假设这次也是 no-op)。

---

## 已知残留 / 待 hx 终审项(摘自 `.superpowers/sdd/progress.md` 台账)

以下是本分支开发过程中标记为"待 hx / 待终审"但未阻塞合并的项,列在这里
方便 hx 在 0727 前后一次性过一遍,不是本次迁移的技术阻塞:

1. **T7 相关**:①`I4` 残留——proactive 失败标记可能被同轮后续
   `completed` 状态覆盖(可观测性问题,非阻塞,建议后续补 flag)。
   ②proactive 轮附带 noop 说明句的文案取舍,值得 hx 过一遍措辞。
2. **T8 相关**:①新会话前两轮可能重复注入一次目录(有界、自愈,已知
   quirk,引入了 pending/commit 两阶段标记后影响已收窄)。②
   `sys.path` 防御性插入的位置,有人建议改到测试层解决更干净,当前留在
   生产代码里也不算错,只是不够优雅。
3. **T10 相关**:极窄竞态窗口下,409 响应体里的 `active_job_id` 可能是
   空串(不会误判排他逻辑本身,只是这种情况下报错信息量少一点)。
4. **T11 相关**:`io_cli_catalog.build_catalog` 对
   `mutually_exclusive_group(required=True)` 的 usage 解析会在目录行里
   留一个装饰性的 `( | )` 残影(不影响功能,`identity-redistill` 命令本身
   和两个真实 flag 名都完整显示),没有为了这一个 verb 去动共享的正则
   解析器。
5. **T12 相关**:蒸馏合并语义的扩张面比最初设想的大——不只是
   `identity.replace`(redistill 专用),还覆盖了云端 `update_identity`
   job 和 `_write_back_plaintext_user_name` 两个既有调用方。这意味着
   "重新上传一份不完整的材料"不再能用来清空某个字段(和"缺失=不变"的
   新原则一致,但这是对既有 `replace` 调用方行为的改变),**需要 hx
   明确背书这个扩大化是否符合预期**(报告里标注为"待 hx 拍板",非阻塞,
   但属于行为语义变化,应该被看到而不是悄悄生效)。
6. **T14 相关**:identity 写字段/错误码的契约目前只写在 changelog 里,
   没有独立的结构化参考页(与仓库现状一致,`/v1/identity/actions` 本来
   就是 `compatibility` 级 generic schema)——如果 identity 的表面积
   继续增长,值得考虑要不要开一个专门的参考页,当前不算问题。
7. **FEATURE_LOG「工具能力补全」模块**(io 工作区级看板,不在本仓库内):
   合并方式(直接 merge / squash / PR)和上线状态(consumer 重启 / 镜像
   重出 / CVM 重部署)需要 hx 在真正执行迁移动作之后手工回写,看板本身
   不会自动感知这次合并——按 io/CLAUDE.md 的规则,这一步任何 agent 都
   不能替 hx 自动做,必须是"动作发生之后"的人工确认。

---

## 分支/commit 参照

- 本分支:`feat/io-cli-capability-completion`,HEAD `f5a21e21`。
- 相对 `origin/test` 的 merge-base:`c79352c3`(40 commits,T1-T14 全部
  complete,详见 `.superpowers/sdd/progress.md` 与各 `task-N-report.md`)。
- 复核 pre 现状用的命令(任何人重新核对本文档时都应该先跑一遍,因为
  pre 变化很快,参见 io/CLAUDE.md 过渡期章节的警告):

      git fetch origin pre
      git show origin/pre:backend/capabilities/tool_schema.py
      git show origin/pre:backend/capabilities/identity.py
      git show origin/pre:tools/io_cli.py
      git ls-tree -r --name-only origin/pre -- backend/alembic/versions/ | tail -5
