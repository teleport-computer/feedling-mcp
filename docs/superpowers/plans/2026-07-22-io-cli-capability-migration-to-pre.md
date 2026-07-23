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

**现状(2026-07-23 实查,R 轮修正)**:
- `test`(本分支)head = `0023_redistill_job_exclusivity`,`down_revision =
  0022_notify_relay`(线性链,无分叉)。
- `pre` head = `0052_dual_runtime_coexistence`。
- `0049_merge_test_pre_heads` 不是"本分支这次合并"的产物,是 pre 历史上
  **更早、且已经解决掉**的一次分叉合并:pre 在 0022 之后曾经分出两条链——
  一条是原样带过去的 `test` 数据侧链(`0020_dau_median_user_sec` ~
  `0022_notify_relay`,内容与本分支的同名文件逐字节相同,已用 `git diff
  origin/pre:...0022_notify_relay.py` 核实),另一条是 pre 自己的 V2 链
  (`0041_v2_mcp_mutation_attempts` ~ `0048_v2_turn_metrics_user_fk`)。
  `0049` 把这两条**都在 pre 分支内部**的链重新接成单链,之后 pre 才继续
  线性长到 `0050 → 0051 → 052`。**merge 之后 pre 并不是"两条独立链"，是
  一条链**——`0023` 与 `0050/0051/0052` 互不认识,不是因为 0049 之后 pre
  自己分叉了,而是因为**本分支的 `0023` 建在 test 自己的 `0022` 之上,
  从未见过 `0049`**。真正待解决的分叉只有一处:`test` 的 `0023` vs `pre`
  的 `052`(或合并时 pre 的实际 head)。

**冲突解法**:合并时用 `alembic merge` 生成一条新的 merge revision,
`down_revision` 同时指向 `0023`(test 侧)和 pre 实际 head(合并时以
`alembic heads` 现查为准,不要硬编码 052——pre 领先 test 上百个提交,
到 0727 可能已经推进过)。

**动作**:
1. `alembic heads` 确认此时 pre 分支上的真实 head(不要硬编码 0052)。
2. `alembic merge -m "merge test io_cli-capability-completion into pre" <test_head> <pre_head>`
   生成合并 revision 文件,落在 `backend/alembic/versions/`。
3. **合并前审计(不是跑 migration 的前置条件)**:`0023_redistill_job_
   exclusivity.py` 的 `upgrade()` 在建索引**之前**已经自带一段
   `UPDATE ... FROM ranked WHERE ranked.rn > 1`,会把同用户下除最新一条外
   的全部 `active`(`awaiting_resident`/`processing`)状态的
   `source_kind='resident_redistill'` job 自动标记为 `failed`——**索引创建
   本身不会因为重复数据失败**,migration 可以直接跑,不需要人工先清理。
   这段 SQL 只是给合并前的人一个可见性:

       SELECT user_id, count(*) FROM genesis_import_jobs
       WHERE source_kind = 'resident_redistill'
         AND status IN ('awaiting_resident', 'processing')
       GROUP BY user_id HAVING count(*) > 1;

   有结果 → 不代表要手工处理,而是提示"migration 会静默把这些用户的
   较旧 in-flight redistill job 判 failed"——预期是空结果(`resident_
   redistill` 这个 source_kind 是本分支新引入的),如果非空,合并前知会
   一下 hx/受影响用户即可,不阻塞 migration 执行。
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
   `patch()`**(`merge_patch_fields` L32-54,`patch()` L57-71)——**这是本条
   最关键的实查发现**:`merge_patch_fields` 的 docstring 已经自己写明了一个
   陷阱,原文摘录:
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
   `identity_patch` 分支,复用同一份 `merge_patch_fields`)。

   **⚠️ 重放失败时的真实后果是"永远重试",不是"终态丢弃"**(这一点
   docstring 里其实已经写清楚了,首版本文档抄错了方向,这里改正):
   `backend/model_api_runtime/v2/effect_outbox.py`(~L818-841)的重放循环
   只认一种异常会落终态——`db.EffectTerminalError`(→
   `status='discarded'`,不重试);**其余任何异常(包括
   `validate_tool_args` 校验失败抛出的普通 `RuntimeError`)都走
   `db._effect_record_error_on_cursor` 那条路,保留 `pending`/
   `pending_fenced_v1` 状态,下一轮 sweep 还会再捞出来重放**。也就是说,
   如果把"改名必须带介绍"的拒绝规则加在 `validate_tool_args` 里,部署那
   一刻 outbox 里"升级前入队的、只改名不带介绍的旧 effect"重放时会不断
   撞上新校验规则、不断以 `RuntimeError` 失败、**永远卡在 pending 重试
   循环里,占用 sweep 资源且永远不会真正应用**——比"静默丢弃"更糟,是
   一个持续吃资源又看不到进展的挂起态。

   `capabilities/identity.py::patch()` 自己已经有一条现成的
   fail-closed 先例可以照抄(L60-66,`patch` 字段类型不对时的拒绝)。
   `retryable=False` → `db.EffectTerminalError` 的映射关系已在
   `serve_worker.py`(~L1956-1957)实查确认:
   `retryable = result.error["retryable"]; return RuntimeError(code) if
   retryable else db.EffectTerminalError(code)`——`CapabilityResult` 的
   `retryable` 位在这里被转成"要不要抛 `EffectTerminalError`"的判断,
   再由 `effect_outbox.py` 的重放循环认领(L818-841 只认
   `db.EffectTerminalError` 才落 `discarded`)。这条映射链路三处都在
   pre 上核实过,合并时如果 `serve_worker.py`/`errors.py` 这段逻辑又变了
   (pre 变动快),要重新走一遍这条链路确认,不能只看本文档结论。

### 合并动作(两阶段发布,R2-I2)

**不能一步到位加闸。** 按下面顺序:

1. **第一阶段(随本次合并一起做)**:
   - 字段对齐:`tool_schema.py::PARAMS["identity_patch"]` 补齐 9 个字符串
     字段 + 4 个 list 字段的 add/remove/replace 键名(与本分支
     `backend/identity/actions.py::_LIST_OP_FIELDS` 逐字段对齐,那里已经
     留了 V2 migration 注释指向这里——见 `actions.py` L59-60)。
   - 新增 `identity_dimension_nudge` capability(`capabilities/identity.py`
     新函数)+ `tool_schema.py` 对应 `PARAMS`/`DESCRIPTIONS` 条目
     (字段:`dimension` + `delta`,单条 |delta|≤10 由服务端
     `backend/identity/actions.py::_identity_dimension_nudge` 已经兜底,
     参见该文件 L619-620 的 V2 migration 注释)。
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
   effect 还在排队"。示意查询(**执行前先对照 pre 上
   `backend/alembic/versions/0027_v2_effect_outbox.py` 实际 schema 核实
   一遍列名**——`effect_type`/`created_at`/`status` 这三个列在
   2026-07-23 实查时确实存在,但 pre 变动快,不要免检直接用):

       SELECT count(*) FROM v2_effect_outbox
       WHERE status IN ('pending', 'pending_fenced_v1')
         AND effect_type = 'identity'
         AND created_at < '<部署时间戳>';

   **⚠️ `pending_fenced_v1` 这一档不要只用 `created_at < 部署时间戳`
   一个条件就认为"清干净了"**:这个状态是由 `0041/0042/0044` 几个
   migration 加的触发器打上的,语义是"排在某个互斥围栏之后、要等前面的
   mutation-attempt 先解围栏才能轮到自己"——它不是单纯的时间序列,一条
   老的 identity 改名 effect 完全可能因为**围栏本身卡住**(而不是时间)
   迟迟不被处理。上面的 SQL 只能告诉你"有没有旧行还挂着",不能告诉你
   "它们会不会自己走完"——如果计数不为零且长时间不下降,需要去查
   `0041_v2_mcp_mutation_attempts.py` 里那条围栏机制本身有没有卡住,而
   不是想当然等它自然清零。
3. **第二阶段(drain 确认干净后,单独一次小改动)**——**拒绝规则落点是
   `capabilities/identity.py::patch()`,不是 `tool_schema.py::
   validate_tool_args`**:在 `patch()` 里,`merge_patch_fields` 算出
   `patch_fields` 之后、构造 `payload` 之前,加一条检查:`agent_name`
   非空但 `self_introduction` 为空 → `return err(errors.INVALID,
   "identity_patch requires self_introduction alongside a rename",
   retryable=False)`(照抄 L60-66 那条 `patch` 类型校验的 fail-closed
   写法,同一个函数、同一种返回形状)。

   **绝对不要把这条拒绝规则加在 `tool_schema.py::validate_tool_args`
   里**——上面"pre 实查"第 3 点已经用 `serve_worker.py` 的真实代码证明了
   这条路径的失败会变成 `RuntimeError`(retryable),而不是
   `EffectTerminalError`(terminal discard)。加在这里的后果不是"旧
   effect 被静默丢弃",而是**旧 effect 永远卡在 pending 状态被反复重放、
   反复因新规则失败、永远不终结**——比丢弃更糟,且更难发现(没有一条
   "丢弃"日志可查,只有持续增长的重试计数)。`validate_tool_args` 只能
   继续保持它现在"从不拒绝、只做形状校验"的性质,新的语义拒绝一律放
   `patch()` 里。

**补一条兼容测试**(第二阶段落地时必须一起加,写在 pre 侧
`tests/test_capabilities_identity.py` 或同类文件)——**测的是重放路径的
终态,不是"返回了个字符串"**:

- 构造一个"部署前入队的旧版 payload"(`{"agent_name": "老六"}`,不带
  `self_introduction`),模拟 `serve_worker` 重放一条 pre-upgrade 时期
  入队的、只改名不带介绍的 in-flight identity effect。
- 断言:走完 `capabilities.identity.patch(...)` → `serve_worker` 那条
  "`retryable` 位判断要不要抛 `EffectTerminalError`"的转换链路之后,
  **最终结果是 `db.EffectTerminalError`(对应 `effect_outbox` 里落
  `status='discarded'`),不是一次普通异常**。只断言
  `validate_tool_args(...)` 或 `patch(...)` "返回了非 None / 返回了错误
  字符串"是不够的——那种断言在"拒绝规则错放在 `validate_tool_args`"的
  错误实现下**同样会通过**(因为那条路径也会返回一个非空的错误字符串,
  只是它转成 `RuntimeError` 之后被 `effect_outbox` 当成可重试,而不是
  终态丢弃),等于把"重放会无限重试"这个真正的 bug 用一条看似通过的
  测试盖起来。测试必须显式跑到 `retryable=False` → `EffectTerminalError`
  这一步,或者至少直接断言 `patch(...)` 返回的 `CapabilityResult`
  的 `retryable` 字段是 `False`。

**验证命令(本分支侧,字段来源不变,回归用)**:
```
pytest tests/test_identity_list_ops.py tests/test_identity_nudge_cap.py \
       tests/test_identity_rename_pairing.py -q
```
（pre 侧的新增测试要在 pre 分支上跑,不在本分支范围内。）

**精确锚点(pre 侧,均已通过 `git show origin/pre:<path>` 核实存在,
2026-07-23)**:
- `backend/capabilities/tool_schema.py` L48-57(`PARAMS["identity_patch"]`)、
  L237-242(`DESCRIPTIONS["identity_patch"]`)、L354-365
  (`validate_tool_args` 里 `identity_patch` 分支——**保持只读校验,新的
  拒绝规则不落这里**)
- `backend/capabilities/identity.py` L32-54(`merge_patch_fields`,
  docstring 是本条发现的原始证据)、**L57-71(`patch()`——第二阶段的拒绝
  规则落点,照抄 L60-66 的 fail-closed 写法)**
- `backend/model_api_runtime/v2/effect_outbox.py` L818-841(重放循环,
  只认 `db.EffectTerminalError` 才落 `discarded`,其余异常保留
  `pending`/`pending_fenced_v1` 重试)、`_LEGACY_SENSITIVE_EFFECT_TYPES`、
  `_PENDING_EFFECT_STATUSES`
- `backend/model_api_runtime/v2/serve_worker.py` L1956-1957
  (`retryable` → `RuntimeError`/`db.EffectTerminalError` 的转换点)
- `backend/db.py` L8343(`class EffectTerminalError(RuntimeError)`)

**精确锚点(本分支侧,V2 migration 注释已预留,行号已核对当前 HEAD)**:
- `backend/identity/actions.py` L59-60(`_LIST_OP_FIELDS` 上方)、
  L619-620(`_identity_dimension_nudge` 单条限幅)、L874-875(批量
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

## ⑧ T16 蒸馏失败分类(error_code/error_hint/卡点信息)

`backend/genesis/service.py` 新增 `classify_genesis_error` + `GENESIS_ERROR_HINTS`
（固定枚举:`bad_api_key|provider_timeout|provider_quota|model_bad_json|
model_empty_output|worker_restarted|consumer_offline|decrypt_failed|internal`），
`write_genesis_state` 在 `status=failed` 时附加 `error_code`/`error_hint`
（`error` 原始字符串不变),`status=processing` 时按 job 字典里已有的
`resident_consumer_id`/`resident_claimed_at`/`updated_at` 附加
`worker_claimed_by`/`claimed_age_sec`(取不到就不写,不编造)。纯增量——
`mark_failed` 新增可选 `exc` 参数用于更精确分类,不传时退化为纯字符串匹配。

- **pre 侧同名文件同改**:`backend/genesis/worker.py`(pre 版,跑在
  serve-worker 线程池 + `daemon.py` 里)抛的是同一批
  `GenesisWorkerError`/`ProviderError` 字符串,分类逻辑只在
  `service.py` 存一份,pre 只需要保证自己的失败路径也经过同一个
  `write_genesis_state`/`mark_failed` 落点,不需要重复维护映射表。
- `consumer_offline`(VPS resident 离线)本次**只定义枚举值和文案,不接线**——
  没找到会产出可匹配错误串的真实 consumer-offline 抛点;`decrypt_failed`
  则找到了真实抛点(`worker._decrypt_envelope`,enclave 解密调用失败)并接了线。
- iOS 展示是独立后续任务,不阻塞这次合并——`genesis_state` blob 只是多了两个
  字段,老 app 忽略未知字段,行为不变。
- 新测试 `tests/test_genesis_failure_codes.py` 纯字符串/monkeypatch,已加入
  `tests/conftest.py` 的 `_PURE_UNIT` 白名单。

---

## ⑨ Task 17(B2,hx 2026-07-23 拍板):用户层 5 字段补齐两个蒸馏器

**反转 T7/`ef8e393d`(I7)**:`user_preferred_name` / `custom_persona_prompt` /
`language_preference` / `relationship_anchor` / `stable_definitions` 这 5 个
D1 用户层字段,此前全链路无生产入口(onboarding、重新总结/redistill、做梦都
不产,只有对话 `identity-write` 能写)。本条决定让**两个蒸馏器**都能从素材里
GROUNDED 抽出它们——素材没有明确信号就留空,绝不编。

**改动范围**(纯业务层,不碰 `backend/agent_runtime/`、CLI 行为、
`backend/capabilities/`,按 io/CLAUDE.md 的过渡期判断表**不受 V2 影响,走
test**):
- `backend/identity/distill_prompt_v1.py`:`RESIDENT_IDENTITY_FIELDS` 9→14
  (= `card_policy.PROFILE_FIELDS` 全部 13 + `dimensions`),`_FIELDS_SPEC`
  加 5 字段的 GROUNDED 措辞,`_STRING_CAPS`/`_LIST_FIELDS` 补齐,parser 新增
  `user_preferred_name` 的 "TA"/占位符过滤(复用 `identity.user_naming`)。
- `backend/genesis/prompts.py`:`FACT_WRITE_PROMPT` 输出契约 + 防火墙加一条
  **反向例外**——这 5 个字段描述的是用户本人,只能从用户档案/用户自己的话取,
  跟 agent 身份字段(只能来自描述 TA 的素材)规则相反。
- `backend/genesis/worker.py`:`_fact_write` 聚合逻辑、`_identity_only`、
  `_fact_write_output_empty` 三处的"有信号"判断从只看 `agent_name`/
  `dimensions` 扩到也看这 5 个字段(否则素材只给了 persona 指令但没给名字/
  维度时,聚合阶段会把它整个丢掉)。
- `backend/genesis/plaintext.py`:`_plaintext_merge_reducer_outputs` 同样的
  "有信号"判断扩展,并且这 5 个字段**反过来**要从 `source_family=="user_profile"`
  的输出里取(agent_name/dimensions 继续排除 user_profile,规则不变)。
  **顺手修了一个真实回归**:`_run_plaintext_background_enrichment` 里
  persona-baseline 兜底那段原来是 `merged["identity"] = baseline`(整体覆盖),
  一旦素材只给了 5 字段信号又同时存在 persona 文本,会被这个兜底悄悄冲掉——
  改成 `{**existing_identity, **baseline}` 合并,已加回归测试锁住
  (`test_v2_background_baseline_derivation_merges_not_overwrites_user_layer_fields`)。
- `backend/genesis/service.py`:`_identity_payload_from_output` 补齐这 5 个字段
  的抽取(1200/240 cap,同 `card_policy`/本模块既有约定),"有信号"判断同步
  扩展;`init_identity_if_absent` 补上把这 5 个字段从 `payload` 抄进
  `merged_payload` 的那一步——**`user_preferred_name` 此前已经算出来了但从
  没被抄进去,是个已存在的死代码 bug,这次一起修**。
  `_identity_payload_for_replace`(redistill 落地点)本来就是通用遍历
  `card_policy.PROFILE_STRING_FIELDS`/`PROFILE_LIST_FIELDS`,**已经**天然支持
  这 5 个字段(T12 的成果),这条链路只缺蒸馏器本身产出它们,不缺服务端落地。
- 迁移 doc/changelog 措辞:`docs-site/content/docs/changelog.mdx` 补充一句;
  `distill_prompt_v1.py`/`chat_resident_consumer.py` 里 I7 写的"9 个字段/
  刻意不蒸馏"措辞已改成"14 个字段/GROUNDED 蒸馏"。

**V2 镜像(0727)**:**不适用**,已核实——
`backend/identity/distill_prompt_v1.py` 是 VPS 自托管 resident consumer 专用
(`tools/chat_resident_consumer.py`),hosted V2 没有对应实现,不需要镜像;
`backend/genesis/{prompts,service,worker,plaintext}.py` 是 test/pre 共用的
genesis 流水线单一代码源(不是按 runtime 各一份),随正常 `git merge` 带过去
即可,不需要在 pre 上单独打补丁。**当时发现但未处理的口子**(已在 §⑩ 补上,
见下):hosted V2 的"foreground 身份闸"
(`backend/genesis/foreground_identity.py::derive_foreground_identity`)复用的
是另一条更老的独立实现——`backend/hosted/history_import.py::
_derive_identity_with_provider`(`/v1/history_import/*`,非 genesis 流水线)——
当时**没有改这条**,所以 cloud onboarding 实际跑的 genesis v2 foreground 派生
这一步还不会产出这 5 个字段。**此状态已过期,见 §⑩**——这条已经补上了。

**无法在此验证(prompt 行为,单测证不了模型真的会抽好)**:上传含明确 persona
指令的素材 → onboarding 建卡真的带上 `custom_persona_prompt`;重新总结真的
能更新它。**合并前必须真模型 e2e**,同 io/CLAUDE.md 的加密 e2e 铁律并列的
"prompt 行为 bug 单测抓不到"教训。

---

## ⑩ Task 17(B2 review 追加,hx 2026-07-23 拍板):cloud onboarding 实际路径补齐

**背景**:§⑨ 做完后 review 发现一个关键缺口——cloud onboarding 实际跑的不是
§⑨ 改过的 genesis background reduce,而是 genesis v2 **foreground 派生**
(`FEEDLING_GENESIS_V2_ENABLED=true` 部署下的路径):
`backend/genesis/foreground_identity.py::derive_foreground_identity` 原样复用
`backend/hosted/history_import.py::_derive_identity_with_provider`,这条**没
被 §⑨ 碰过**,只产 9 个字段。也就是说 §⑨ 做完之后,cloud 上新用户走的仍然是
老的 9 字段卡——**这次才是真正把 onboarding 半条链路接到 LIVE cloud 路径上**。

**改动**(同样纯业务层,不受 V2 影响,走 test):
- `backend/hosted/history_import.py::_derive_identity_with_provider`:输出契约
  的"Return JSON only with fields"列表补齐 5 个用户层字段,措辞对齐
  `distill_prompt_v1.py` 的 `_FIELDS_SPEC`(同样的 GROUNDED 口径 + 防注入
  框架)。同时加了防火墙的反向例外(这 5 个字段只能从 User Profile 类素材
  取,跟 agent 身份字段规则相反,同 §⑨ 对 `genesis/prompts.py` 的处理)。
- `backend/hosted/history_import.py::_normalize_identity_payload`:补齐 5 个
  字段的解析/清洗分支——`custom_persona_prompt`/`relationship_anchor` cap
  1200,`language_preference` cap 240(不套 zh-only-English 丢弃逻辑,因为
  语言偏好本身可能就是"English"这类合法英文值,同 `user_preferred_name` 不
  套该逻辑的理由一样),`user_preferred_name` 复用既有的
  `_sanitize_import_user_name`(占位符"TA"/"用户"等于没信号,不落卡),
  `stable_definitions` 按列表清洗(去空、截 12 条、单条截 240)。全部**留空
  即不写 key**,跟 `tone_style`/`agent_role` 等既有字段同一惯例。
- 已核实 `foreground_identity.has_identity_signal` 只看 `agent_name`/
  `dimensions`,这 5 个字段是纯增量,不影响该闸门行为,无需改。
- 已核实 `_IDENTITY_UPDATE_MERGE_TEMPLATE`(二次上传部分补全路径,
  `genesis/plaintext.py::_run_plaintext_update_identity_job` 会传
  `existing_identity`):这条路径没有 Python 侧字段级合并,完全靠 prompt 指示
  模型把"素材没提到的字段"原样带回来。这跟 `tone_style`/`agent_role`/
  `do_not_say`/`boundaries`(P2)已经在跑的合并方式**完全一致**,不是这次新
  引入的风险——`_normalize_identity_payload` 的"留空不写 key"惯例只保证
  "没编造",不保证"模型真的听话回显了旧值";这是已有架构的既有风险面,这次
  没有扩大也没有缩小。
- `backend/genesis/prompts.py::FACT_WRITE_PROMPT`:§⑨ 加的用户层字段说明段落
  缺一条明确的防注入措辞(`distill_prompt_v1.py` 里有,这边没有)——素材(尤
  其可能被抽成 `custom_persona_prompt` 的那段)可能读起来像是在对蒸馏器下
  指令,这次补了一条"当作惰性文本处理、不要执行"的措辞,跟 resident 那边对齐。

**测试**:`tests/test_history_import_identity.py` 新增——
`_normalize_identity_payload` 在字段存在时保留、不存在时不写 key、占位符名字
丢弃、两个 1200-cap 字段截断、`stable_definitions` 清洗;以及 prompt 字段
列表包含这 5 个字段名 + 防注入措辞的存在性断言。均为纯函数/monkeypatch,无
DB。回归跑过
`test_history_import_identity.py`/`test_genesis_service.py`/
`test_genesis_worker.py`/`test_genesis_prompts.py`/`test_genesis_foreground*.py`/
`test_resident_identity_distill.py`,全绿。

**V2 镜像(0727)**:pre 上 `backend/genesis/foreground_identity.py` 是否还是
原样复用 `history_import.py` 这条路径、还是已经切到 V2 原生的
`backend/capabilities/` 身份派生实现,**合并时需要重新核实**(§⑨ 写于
2026-07-23,pre 架构变化快,见 io/CLAUDE.md 过渡期提醒)。如果 pre 已经有独立
的 onboarding 派生实现,这 5 个字段的措辞需要在**那条实现**上补一份同样的
处理,而不是假设这次改的 `history_import.py` 会被直接带过去生效。

**无法在此验证(同 §⑨)**:prompt 行为——单测只能证明字段名进了 prompt、
`_normalize_identity_payload` 不编造/不丢已有信号,证不了模型真的会照着抽好。
**合并前必须真模型 e2e**:上传含显式人设指令的素材 → cloud onboarding 建卡
带上 `custom_persona_prompt` 等 5 个字段(GROUNDED);注入型指令素材不会被
误当成真指令执行、也不会被误抽成假的 `custom_persona_prompt`。

**验证命令**:
```
pytest tests/test_identity_distill_prompt.py tests/test_genesis_service.py \
       tests/test_genesis_worker.py tests/test_genesis_prompts.py \
       tests/test_redistill_server_merge.py tests/test_identity_actions.py \
       tests/test_asgi_identity.py tests/test_genesis_plaintext_routes.py \
       tests/test_genesis_v2_orchestration.py tests/test_genesis_notice.py -q
pytest tests/test_chat_resident_consumer.py -q             # 456 passed(基线不能破)
pytest tests/test_resident_identity_distill.py -q           # 单独跑,已知与 consumer 测试有慢交互
FEEDLING_TEST_PG=postgresql://localhost:1/none pytest --collect-only -q \
    tests/test_identity_distill_prompt.py
```

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
       tests/test_spawners_catalog.py tests/test_genesis_failure_codes.py -q

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
    tests/test_spawners_catalog.py tests/test_genesis_failure_codes.py -q

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
