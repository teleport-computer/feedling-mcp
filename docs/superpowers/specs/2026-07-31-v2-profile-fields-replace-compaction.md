# Runtime V2:MEMORY/USER 双字段取代 conversation summary,compaction 降到零模型调用

**状态**:设计已与 Seven 五轮对齐并拍板,待实现。
**优先级**:高(M1 独立关掉 prod 三起卡死事故家族)。
**分工**:codex3 实现,claude3 审核 + gatekeep,按里程碑逐段双签。
**iOS**:本批无改动。

---

## 1. 背景(30 秒)

V2 的长期上下文是 conversation summary:`maintenance` lane 用**用户自己的 BYOK key**把聊天记录一批批折成 bullet 摘要,每回合原样注入 prompt。

三个问题:

1. **成本与脆弱** —— 3000 条历史的老用户冷启动,按 prod 实配(`FEEDLING_V2_COMPACTION_BATCH_MSGS=50`)要 ~60 个 job、每个 1 次模型调用,全烧用户的 key。中转站 key 上基本烂尾。prod 已有三起卡死(usr_7f30 三天、usr_90184、usr_81a0);`deploy/docker-compose.phala.yaml:395-411` 那段长注释就是这些事故的疤。
2. **无上限增长** —— frontier 允许到 24 段 / 48,000 字符,每回合全量进 prompt;每压缩一次内容就变,provider 缓存跟着失效。
3. **机制冗余** —— 我们本来就有 Memory Garden。同一批聊天行理论上被 capture 和 compaction 各扫一遍,写进两个互不相通的长期存储;而且**贵的那份(记忆卡)根本不进 prompt**,便宜的那份(摘要)进。优先级是反的。

**替换**:从 Memory Garden 蒸出两个**焊死上限**的字段,每回合注入,取代 summary。

| 字段 | 上限 | 内容 | 一句话 |
|---|---|---|---|
| `MEMORY` | 2,200 字符 | 称呼、关系起点/时长、反复出现的人/宠物/地点、时间线大事、进行中的事 + 承诺过的事、明确雷区 | **事实**:我知道我们之间发生过什么 |
| `USER` | 1,375 字符 | 沟通风格、要陪伴还是要建议、作息节奏、说话方式雷区、称呼偏好 | **方式**:该怎么和 TA 相处 |

**分界线是硬要求:MEMORY 是事实,USER 是方式。** 两边重叠会让同一句话在两个预算里各占一份,等于白扔一半额度。

赢的不是"发得更少",是**发的东西有上限、且稳定到能被 provider 缓存命中**。更深的回忆仍走 `memory_index` / `memory_search` / `memory_fetch` 按需拉。

### 1.1 调查中发现的两件事(改变了地基,必读)

**A. prod 的 capture 和 dream 都没在跑 —— 漏配。**
`deploy/docker-compose.phala.yaml` 的 `serve-worker:`(:350,跑 `serve_worker.py`)整个 `environment:` 块里**没有** `FEEDLING_V2_CAPTURE_ENABLED`,也**没有** `FEEDLING_V2_DREAM_ENABLED`,且无 `env_file:`。两者代码默认都是 `"0"`(`serve_worker.py:269`/`:272`),gate 是 fail-closed(`:282`/`:293`),第一行就 return False。`FEEDLING_V2_CAPTURE_ENABLED` 只声明在 `backend:` 服务(:279)—— 但调度跑在 serve-worker,声明错了地方。

→ **V2 用户的记忆花园目前只由 agent 主动调 `memory_write`(`worker.py:3701`)喂**,没有系统性蒸馏。Seven 决定按漏配处理,与本批一起修。

**B. watermark 不能简单冻结。**
wake lane(`worker.py:6373`、`7277`)用的是**不降级**的 `_ensure_prompt_coverage`,不是 chat 的 `_or_degrade`(:8975);且 wake 的 tail 读**不传** `tail_cap`(:6392/:7295)→ 要求精确覆盖。compaction 一停、watermark 一冻,wake 不是"tail 变长",是直接抛 `prompt_coverage_incomplete`。

→ 所以本批**不是删掉 compaction**,而是**保留 coverage 记账、把它的模型调用降到零**。

(GC 不构成理由:`tests/test_v2_gc_coverage_gate.py` 开头明写旧 coverage gate 已故意移除,watermark 冻结不挡数据保留。)

### 1.2 Seven 已拍板

| 项 | 决定 |
|---|---|
| 刷新挂点 | 先在 prod 打开 dream,刷新挂 dream |
| 字符上限 | 先按 2,200 / 1,375 上线,做成**可调参数**,拿真实数据测完再锁 |
| tail 长度 | **保持现状不动**(`_CHAT_TAIL_MAX_TURNS=40` / `_TAIL_HARD_CAP=60`),本批不碰 |
| 漏配修复 | 与 profile **同一批**做 |
| 卡片读取 | **全部的卡,不设覆盖上限**(静默丢卡 = 我们一直在修的那类 bug) |

---

## 2. 范围

### 2.0 补 prod 漏配

`deploy/docker-compose.phala.yaml` 的 **`serve-worker:`** 环境块补:

```yaml
      FEEDLING_V2_CAPTURE_ENABLED: "${FEEDLING_V2_CAPTURE_ENABLED:-1}"
      FEEDLING_V2_DREAM_ENABLED: "${FEEDLING_V2_DREAM_ENABLED:-1}"
```

核对 `.phala.test.yaml` / `.phala.pre.yaml` / `docker-compose.yaml` 的**同一服务**是否有同样的漏(注意:test 的 compose 已声明,但要确认在哪个服务块)。

⚠️ **改 compose 会产生新的 compose_hash → 按 `deploy/DEPLOYMENTS.md` 走链上发布流程。**

### 2.1 零模型调用的 coverage 记账

替掉 compaction 的 provider 调用,保留全部记账语义。

**`compaction.deterministic_fold(*, source_message_count) -> str`**
返回确定性文本(如 `- [N 条更早的消息已由长期记忆覆盖]`),并**过 `_validate_new_bullets(rendered, current_summary="")` 同一道门**(照 `_verbatim_fold` 的写法,compaction.py:240)—— 校验器拒绝的东西绝不能成为不可存储的叶子。

**`compaction.deterministic_checkpoint(child_texts) -> str | None`**
同形状、计数求和;任一子节点不是确定性哨兵时返回 `None`,`_rebalance_summary_frontier` 再退回 `compact_checkpoint`。

> **这个必须做,不是可选。** `_append_summary_segment`(serve_worker.py:1291)每写一片都会重写 head summary 信封,线性增长;唯一收缩 frontier 的是 `_rebalance_summary_frontier` → `compact_checkpoint`,在 `DEFAULT_ROLLUP_FANOUT=8` 触发。不做确定性 checkpoint 的话,成本按 1/8 的速率原样回来。

**不能用 `legacy_opaque` 做逐批叶子。** `SummarySegment.__post_init__` 强制它 `start_seq==0`(summary_frontier.py:71-79),`validate_canonical_frontier` 只允许一个、且必须在队首(:196-198)。逐批叶子必须是 `coverage_kind="exact"`,带真实 `start_seq`/`end_seq`/`source_message_count>0`。文本只要求 strip 后非空(:85),确定性样板能过。

**`_run_compaction` 增加"仅元数据"路径**:profile 生效时**完全不读 tail、不做任何 enclave 解密**。新增:

```python
db.chat_coverage_bounds_after_seq(user_id, after_seq, *, limit, through_seq)
    -> (first_seq, last_seq, count)
```

一条聚合搞定。跳过 `_bounded_compaction_prefix`(worker.py:4499)和 `_COMPACTION_BATCH_CHARS` —— 没有 prompt 要 bound。

⚠️ **该聚合的 `exclude_synthetic_sources` 必须与 `_read_compaction_tail_after_seq`(serve_worker.py:1000-1006)完全一致。** 否则 `verify_ping` / `resident_maintenance` 之类合成行会被冻进不可变覆盖声明、之后又被删,**永久损坏 frontier**。这条是本节最容易踩的坑。

### 2.2 存储

**单个 blob,`kind="v2_agent_profile"`**,两字段同一个 CAS(不会错位;turn 路径一次读)。

```jsonc
{
  "v": 1,
  "state": "ok" | "pending" | "degraded" | "empty",
  "memory": {"envelope": {...}, "chars": 2118},
  "user":   {"envelope": {...}, "chars": 1301},
  "source": {"card_count": 137, "max_updated_at": "2026-07-31T...", "generated_at": "..."},
  "last_attempt": {"at": "...", "reject_code": "user_chars_over_budget:1502",
                   "attempts": 2, "retry_not_before": 1785000000.0},
  "disabled": false
}
```

- ⚠️ **`user_blobs.doc` 是明文 JSONB,这一层不加密**(db.py:3100)→ **信封必须我们自己建**:`core/envelope.py:70` `_build_shared_envelope_for_store(store, plaintext_bytes, *, item_id=None) -> (envelope|None, err)`。本地加密,非 enclave 往返。现成用例:`serve_worker._build_memory_envelope`(:1915)。
- `chars` 放在信封**外**(是长度不是内容),让 admin/指标不解密就能看合规。
- 解密读照 `_read_summary_with_seq`(serve_worker.py:1218-1226)的写法。
- **CAS**:`db.set_blob_if_unchanged(user_id, "v2_agent_profile", expected, new, insert_if_missing=True)`(:3039)。注意 `insert_if_missing` 只在 `expected_doc == {}` 时生效(:3078),首写必须传 `{}`。
- ⚠️ **CAS 失败必须重读 + 重算,不能重放**(`docs/testing/TESTING.md` §2-M2 是硬规矩)。具体:失败后重读,若胜出方的 `source.generated_at` 比我们的新(或 `card_count`/`max_updated_at` 覆盖了更新的花园)→ **丢弃我们的结果**(按构造已陈旧);否则拿新 `expected` 重试一次;**第二次失败即失败退出,不写**。绝不逐字段合并。
- ⚠️ **turn 路径读用 `get_blob_strict` 不是 `get_blob`** —— 后者吞异常返回 `None`(:2867),会让一次 DB 抖动伪装成"这个用户没有画像"而静默走回摘要。strict 读异常 → 退回注入摘要 **且**记一个可观测事件 + 计数(§2-Q:降级必须看得见)。
- `set_blob_if_unchanged` 会把胜出写镜像到 TEE 影子库(:3086)。内容是密文,可接受,但**要把新 kind 加进 `tests/test_account_reset_purges_all_tables.py`**。

### 2.3 生成

**新增 `profile` lane**:`jobs_store.LANES`(:32-42)+ `LANE_PRIORITY["profile"]=10`(:59-71,与 maintenance/capture/dream 同档,绝不与 chat/wake 争)+ `worker.process_job` 分支(照 :8654 `maintenance` 的写法)。`agent_jobs.lane` 无 CHECK 约束,**不需要迁移**。

⚠️ `_run_profile` 必须复制 `_run_compaction` 的失败契约:自包含 try/except、静默 `mark_failed`、**绝不** `_surface_terminal_error`、**绝不**冒聊天气泡。

**三个触发器**(都走 `enqueue_job` 的 per-user 单飞合并):

| # | 位置 | 说明 |
|---|---|---|
| 1 | `hosted/config_store.py:536-542` | 切换到 V2 时,种 wake schedule 旁边 enqueue。**不要在锁内做蒸馏**(那段在 `hosted_runtime_config_mutation_lock` 里) |
| 2 | `worker.py:8114-8124` | dream 提交后,`apply_memory_actions` 与 `_complete_extraction` 之间 enqueue,**不内联重算**。**这是 Seven 定的刷新主路** |
| 3 | `worker.py:10359-10365` 同款 | 回合后尽力而为(try/except、只 log、绝不拖垮已写成的回复)。**两种情况入队,见下** |

**触发器 3 的两个条件**(满足其一即入队):

1. **补生成** —— 画像缺失,或 `state != "ok"` 且已过 `last_attempt.retry_not_before`。
   ⚠️ **必须尊重 `retry_not_before`**,否则一把永久坏掉的 BYOK key 会让用户每回合烧一次蒸馏调用。

2. **陈旧地板(刷新兜底)** —— **两个条件同时成立**才入队:
   - `now - source.generated_at >= PROFILE_MAX_AGE_SEC`(**Seven 定:7 天**,做成可调)
   - **且**花园确实变过

   "花园变过"用一条不解密、不走 enclave 的聚合判断(`memory_moments`,`0001_baseline.py:57`,独立表,PK `(user_id, moment_id)`):

   ```sql
   SELECT count(*) AS n, max(doc->>'updated_at') AS mu
   FROM memory_moments WHERE user_id = %s
   ```

   与画像 blob 的 `source.card_count` / `source.max_updated_at` 比,**任一不同**即视为变过。在一次模型调用旁边这条聚合可以忽略。

   > 加"花园变过"这一条,是为了**花园没变时不白烧调用** —— 重算出来会是同一份东西。

**设计取舍(别退回去)**:

> **不做定期扫描循环。** 早期设计有第四个触发器(定期扫"缺失/失败"的用户),已砍 —— 它能兜的场景触发器 3 全覆盖;唯一它独占的是"切过来后一直不说话的用户",而**不说话的用户根本用不到画像**。等他开口,那一回合走摘要(过渡规则本就兜底),回合后入队,下一回合就有。
>
> **不按"新卡数 ≥ K"触发。** 那是被否掉的一版:爱聊的用户一两天就攒够 K,等于造了个高频刷新。Seven 明确:**画像不需要频繁更新,低频就行**。所以刷新节奏由**一个** `PROFILE_MAX_AGE_SEC` 说了算,不引入需要凭空拍的卡数阈值。N=7 天时每用户最多约 4 次蒸馏/月,且只在花园真变过时发生。
>
> **陈旧地板与做梦开关完全无关。** 刷新主路是 dream(触发器 2),但用户可以在 App 里关做梦(`dream_enabled`),而 Seven 定过「各开关完全独立无连带」。地板挂在**时间 + 花园本身**,不挂任何开关上:做梦开着就更新得更勤,关掉了也保证陈旧不超过 7 天。

**读全部卡,不静默截断** —— 新写 `serve_worker._read_profile_cards(user_id) -> (rendered, card_count)`:

- 用 `_read_memory_context`(:1795-1804)同样的 runtime token 铸造方式,调 `memory_core.index(store, None, {"limit": 0}, post_enclave=_post)`(`effective_readside_limit(0)` → `readside_hard_max()`,默认 1000)。
- ⚠️ **断言 `body["truncated"] is False` 且 `body["user_card_count"] == len(body["items"])`。** 不满足就以 `profile_cards_truncated:{user_card_count}/{len(items)}` 失败,**不写任何东西**。没有帽子,没有半份画像。
- ⚠️ **不要复用 `_read_memory_context`(:1784)和 `_MEMORY_CARDS_LIMIT=60`(:1759)。** 那个函数逐项 `try/except → ""` 降级 —— 对 dream 是对的,对画像是灾难(残缺花园蒸出一个自信的错画像);60 张的帽子正是本功能不能继承的静默截断。
- ⚠️ **自己写渲染器,不用 `_render_card_line`(:1762)。** 它只取 title→summary→content 第一个非空,丢掉了时间线/承诺需要的正文。要带 bucket + `occurred_at` + summary + 有界 content,并保留"第一个非空"兜底(只有 `content` 的卡也要能渲染)。
- 运行上限要在实现里写清楚:`post_enclave_readside` 把所有候选放在**一个** httpx body、20s 超时(memory_readside_core.py:196-220)。>1000 张需要抬 `FEEDLING_MEMORY_READSIDE_HARD_MAX`,抬了那个 POST 就等比变大。**要么全成功,要么带计数响亮失败,绝不悄悄部分。**

**默认一次调用**;`len(rendered) > PROFILE_SINGLE_CALL_MAX_CHARS`(建议 120,000,对齐 `_COMPACTION_BATCH_CHARS`)才切成有界 map/reduce:

- **复用 `compact_checkpoint` 的形状**(compaction.py:419-455 的 `_fragments`/`_groups`,不丢不乱序 + 显式调用预算 + 专门的耗尽异常),**但不要直接调它**(它的 system prompt 和 `_validate_new_bullets` 输出契约是 bullet 摘要形状,不是画像形状)。中间层出有界 bullet 摘要,最后一层出两个字段。
- ⚠️ **预算 `PROFILE_MAX_PROVIDER_CALLS = 8`,不是 64。** `_MAX_CHECKPOINT_PROVIDER_CALLS=64`(compaction.py:39)正是本批要删掉的那个成本画像;为一个后台字段给用户的 key 记 65 次调用等于把它请回来。超预算 → `profile_source_exceeds_budget:{chars}` 失败。

### 2.4 校验(上限 + 不重叠)

新建**纯**模块 `backend/model_api_runtime/v2/profile.py`,照 `compaction.py` 的契约:`llm` 作为可调用注入,**不 import** DB / 信封 / hosted / provider。落地前先读 `tests/test_v2_dependency_direction.py` 确认允许集。

- **输出契约**:严格 JSON `{"memory": "...", "user": "..."}`(散文,不是 bullet)。抽取照 `memory/dream_prompt_v1.py:120` `_extract_json_block`。
- **`_validate_profile(reply) -> (fields | None, reject_code)`,全有或全无**,理由同 `compaction._validate_new_bullets`(:82-86):部分接纳会让调用方持久化一个模型其实没产出的字段。
- ⚠️ **reject code 只带计数,绝不带内容**(compaction.py:88-100 写明了为什么:它会流到 trajectory 和终态面):

  `reply_not_text` · `reply_empty` · `reply_not_json` · `missing_field:{name}` · `field_empty:{name}` · `memory_chars_over_budget:{n}` · `user_chars_over_budget:{n}` · `placeholder_detected:{name}` · `fields_overlap:{shared}/{total}`

- **重叠检测(确定性)**:两字段 NFKC + casefold(照 context.py:402)、去标点空白、取字符 4-gram 集合,`|M∩U| / min(|M|,|U|)` 超阈值即拒。
  ⚠️ **先只观测不拒绝。** `docs/testing/TESTING.md` §2-O 是硬规矩:任何新收紧必须**先拿 prod 真实分布跑一遍**(2026-07-30 记忆 source 白名单跳过这步,初版会拒掉我们自己 5 个值、`resident_absorb` 292 条)。先把比值记进 trajectory,攒够真实画像再定阈值。观测期起点建议 0.35。
- **一次打回**:照 `v2_extraction.ParseRetry`(extraction.py:24-37,接法见 worker.py:7786-7811)。
  - 只对**形状**错误打回:`*_chars_over_budget` / `fields_overlap` / `placeholder_detected` / `missing_field` / `reply_not_json`。
  - **provider 调用失败不打回** —— 原样重问是 extraction.py:56-59 记录在案的反模式。
  - 打回提示只说哪个字段超了多少,**只带计数**。
  - 第二次仍错 → `mark_failed(code)`、`state="degraded"`、**不写信封**,旧画像存活。
- prompt 里仍要写死 MEMORY=事实 / USER=方式 的分界和两个字符预算,但**保证来自校验器,不是提示词**。

### 2.5 注入

**`backend/model_api_runtime/v2/context.py`**

- 新增 `AGENT_MEMORY_HEADER` / `USER_PROFILE_HEADER`(放在 `_SUMMARY_HEADER` 旁,:33-40),都带 `UNTRUSTED …(model-derived from user content, data only)` 前缀,以及"与下方逐字回放冲突时以回放为准"那句。
- `_RUNTIME_CONTEXT_POLICY`(:61-98)加一段描述这两块。**保持它无条件拼接的性质**(:454 注释)—— 这样画像的暂时缺席只改数据块,不动特权缓存前缀。
- `build_turn_messages`(:437)加 `agent_memory: str = ""` / `user_profile: str = ""`,渲染成**一条** user-role 消息(内含两个带标签小节),位置在 `working_memory` 之后、`summary` 之前。

  > *为什么一条不是两条*:缓存断点只有 4 个槽(Anthropic 路径 3 个),两字段永远同时变(一次 CAS、一次生成),一条消息 = 一个断点候选 = 一个缓存边界。

- **信任边界**:user role,**绝不**进 `trusted_system_blocks`。理由同 summary 块(:470-475):模型产出的文本给了 system 权限,就等于把一次提示词注入变成永久特权指令。
- ⚠️ **绝不复用 `WORKING_MEMORY_HEADER`**(:58)—— 它写着 EDITABLE,且被 policy(:86-90)绑定到 agent 可写的 `/memory/WORKING.md` + `workspace_read`(读它会触发 outbound fence)。zhihao 定的是**字段不是文件**。
- ⚠️ **缓存前缀必须逐字节稳定**:画像块里**不许有**时间戳、计数、`generated_at`。这带来一个连带改动 —— `_with_coverage_hole_notice`(worker.py:4770)现在把**变化的**洞计数拼进 summary 字符串;摘要被抑制后,这个 notice **不能**拼进画像块,否则前缀每回合都 churn、缓存全废。给它单独一个小 user-role 块(放 temporal 块之前),或并进 temporal JSON。

**`backend/provider_client.py`**

- 照 `_WORKING_MEMORY_HEADER` / `_is_working_memory_message`(:1215-1244)加 `_PROFILE_HEADER` / `_is_profile_message`,带同样的跨层契约注释(header 字符串是**复制不是 import**,因为 provider_client 是下层)—— 并写测试钉住两边一致。
- `_mark_openai_chat_cache_breakpoint`(:1246):把画像消息加进 `candidates`,位置在 working-memory 循环之后、`user_candidates[-2:]` 循环之前。
- ⚠️ **`_mark_anthropic_cache_breakpoint`(:1317)委托时只给 `max_breakpoints=3`。** system + 两个最近 user 边界就占满,画像会在 Anthropic 线路上被**静默挤掉**。必须**显式**决定优先级(建议:画像 > 两个最近 user 边界中较旧的那个),不能追加了事。

### 2.6 过渡与降级

**过渡规则放 worker**(保持 `context.py` 纯):`_read_seq_adaptive_prompt_context`(:4789)读完摘要后读画像 blob。

- `state == "ok"` 且未 `disabled` 且开关开 → 解密两字段返回,`summary=""`,并 ⚠️ **跳过 `_bound_materialized_summary`(:4906)**(那是会调模型的路径,profile 模式下绝不能进)。
- 否则 → 原样走今天的行为(含 `_bound_materialized_summary` 和 summary 块)。

**绝不删除既有 summary 数据**(回滚安全);`append_summary_leaf_cas` / `insert_summary_checkpoint` 保持不动。回滚 = 翻开关。

**开关**:`FEEDLING_V2_PROFILE_ENABLED`(默认 `"0"`)+ blob 内 `disabled: true`(单用户回退不用发版)。

**key 挂了** —— 蒸馏只在 `profile` lane 跑,**结构上不可能阻塞回合**:

- provider 失败 → 只 CAS 写元数据(`state="degraded"`、reject code、`retry_not_before = now + backoff`),信封不动。
- 首次就失败 → `state="pending"`、无信封 → 过渡规则继续注入摘要。**这正是摘要不能删的原因。**
- 重试由**触发器 3 的条件 1** 驱动(回合后、且已过 `retry_not_before`),**不走 wake backoff**(`_FAIL_BACKOFF_WAKE_LANES`,jobs_store.py:917,是 heartbeat/scheduled 专用)—— 避免一把永久坏掉的 key 每回合烧一次调用。`retry_not_before` 用指数退避。

### 2.7 顺带修

`context.py:134` 提示词写着 "instead of relying only on the **recent-memory index**",但 prompt 里**没有这个块** —— 在指涉一个不存在的东西,会误导模型。删掉或改写。

---

## 3. 不做

- 不动 tail 长度(Seven 定)。
- 不删任何既有 summary 数据、表、迁移;`summary_frontier.py` 整个保留。
- 不删 `compaction.compact` / `compact_checkpoint`(还要给 profile-off 用户和超大花园兜底)。
- 不主动清理存量脏记忆卡。
- 不碰 `_read_memory_context` 的 60 张截断(同类 bug,但属于 dream,独立处理)。

---

## 4. 里程碑与验收

每段 codex3 交、claude3 审,不过不进下一段。

### M1 记账降本(可独立上线,建议先发)

确定性 fold + 确定性 checkpoint + 仅元数据 compaction 路径,`FEEDLING_V2_PROFILE_COVERAGE_DETERMINISTIC` 默认关。

**验收**:真 PG 大积压 `model_calls == 0`;超大积压下 wake job 成功、不抛 `prompt_coverage_incomplete`;frontier 多轮后仍有界、head 信封停止增长;合成行(`verify_ping`)被排除在覆盖声明外;开关关闭时既有 `test_v2_compaction*.py` 全绿。

> 即使 profile 整体延期也应先发这段 —— 它独立关掉 usr_7f30 / usr_90184 / usr_81a0 那一族事故。

⚠️ **代码可先合先发,但旗子必须锁到 M5 之后**:开关一开,确定性路径会立刻把用户 prompt 里的摘要替换成一句纯计数哨兵(「N 条更早的消息已由长期记忆覆盖」)。在 MEMORY/USER 注入(M5)上线之前打开 = 用户直接失去全部长期上下文而无任何接替。此前置条件必须同时写死在:①开关定义处的代码注释 ②三个 compose 文件的注释(已有测试钉住注释不被误删)。
(审 M1 时另确认:「零模型调用」是**稳态**说法 —— 老用户 frontier 里模型写的既有节点参与 roll-up 时仍会触发 provider 调用,新确定性叶子成组 fanout=8 后才真零;prod 现有 V2 用户全部处于前一种状态,changelog 措辞已限定为 steady-state。)

### M2 存储

blob kind、信封建/读、CAS(失败重读重算)、state 机、strict 读 + 可观测降级。

**验收**:真 PG CAS 竞争(两连接制造陈旧快照,败方重算而非重放);`ok→degraded→ok`;account reset 清表;TEE 影子只带密文。

### M3 纯 `profile.py`

prompt、JSON 契约、校验器、一次打回、有界 map/reduce。无接线。

**验收**:校验矩阵全绿;依赖方向测试通过;重叠比值仅观测不拒绝。

### M4 lane + 触发器 + 漏配修复

LANES/优先级/分发、`_run_profile`、全量卡读取 + 截断断言、三个触发器(含陈旧地板);同批补 compose 环境变量。

**验收**:test 环境 L3 跑通真实账号 —— 两字段存在且未超限、正常花园 `model_calls == 1`;故意坏 BYOK key → `state="degraded"` 且**零**回合失败;确认 capture/dream job 真的开始入队。

### M5 注入

headers、`build_turn_messages` 参数、worker 接线、摘要抑制、coverage-note 搬家、两条 provider 路径的缓存断点。

**验收**:缓存前缀跨回合**字节一致**;L3 多轮对话在**一个强模型 + 一个弱模型**上各跑一遍(§2-P:强模型会掩盖提示词缺陷),确认模型自然用上画像里的称呼和雷区、且不叙述来源。

### M6 放量 + 定阈值

**前置硬条件:M5 已部署且画像注入已在真实用户上验证** —— 见 M1 的警告框,这个开关会立即替换掉摘要。满足后:打开确定性记账,观察 maintenance `model_calls` 收敛到稳态零(老 frontier 混有模型节点期间仍会有少量 roll-up 调用)、每回合 prompt token 下降;攒够真实画像后把重叠校验从观测切成拒绝,并回头锁字符上限。

---

## 5. 测试清单

对照 `docs/testing/TESTING.md` 的 A / D / G / M2 / N / O / P / Q / R 行。

- **`tests/test_v2_profile.py`** —— 校验矩阵:缺字段、正好卡上限、上限+1、非 JSON、占位符、重叠比值三档;**断言每个 reject code 不含输入的任何子串**;断言打回只多花一次调用、且只对形状错误。
- **`tests/test_v2_profile_refresh.py`**(真 PG)—— 陈旧地板的四象限:①未满 7 天 + 花园变了 → **不**入队 ②满 7 天 + 花园没变 → **不**入队 ③满 7 天 + 卡数变了 → 入队 ④满 7 天 + 卡数没变但 `max_updated_at` 前进 → 入队。外加:`state="degraded"` 且未过 `retry_not_before` → **不**入队(坏 key 不得每回合烧调用);`dream_enabled=false` 的用户照样能被地板触发(证明与开关无连带)。
- **`tests/test_v2_profile_storage.py`**(真 PG)—— 首写 `expected={}` + `insert_if_missing`;两连接制造陈旧快照,败方重读**重算**;`ok→degraded→ok`;strict 读失败退回摘要**且**发出可观测事件。
- **`tests/test_v2_profile_cards.py`** —— 0 卡(完成、`state="empty"`、**零**次 provider 调用)、1 卡、1000 卡、只有 `content` 的卡;`user_card_count > len(items)` → 失败且**不写**。
  ⚠️ **必须用 runtime token 而非 api-key 打 readside**(§2-R)—— 现有套件几乎全跑 api-key,而这正是 2026-07-30 事故的形状。
- **`tests/test_v2_context.py`**(扩)—— 画像块是 user-role;画像不变时**两回合字节一致**;摘要仅在画像 ok 时被抑制;coverage-hole notice 不再改动画像块。
- **provider 缓存测试**(扩)—— OpenAI-chat 与 Anthropic(3 槽)两条路径画像都拿到断点;runtime-context 块仍然永不拿断点;tail 之前的前缀跨回合不变。
- **`tests/test_v2_compaction*.py`**(扩)—— 确定性 fold 过 `_validate_new_bullets`;生成的 `SummarySegment` 过 `validate_canonical_frontier`;确定性 checkpoint 对非确定性子节点返回 `None`;profile-on 用户 1500 行积压 `model_calls == 0`。
- **新增 wake 回归** —— profile-on 用户 2000 行积压,`heartbeat` job 完成、tail ≤16 轮、无 `prompt_coverage_incomplete`、enclave 解密次数有界。**这条是能抓住整个问题的那个测试。**
- **`tests/test_account_reset_purges_all_tables.py`** —— 新 kind 被清。
- **`tests/test_v2_dependency_direction.py`** —— `profile.py` 的 import 在允许集内。
- ⚠️ **上线前跑一遍 prod 形状的真实数据**(§2-O,**阻断性**):对线上 V2 用户 dry-run 蒸馏,导出字符长度与重叠比值分布,再定阈值和上限。**不要从这份 spec 里拍阈值** —— 这里写的都是起点,不是结论。

---

## 6. 最容易踩的五个坑(给实现者的速查)

1. `user_blobs.doc` **不加密**,信封要自己建。
2. 逐批叶子必须 `coverage_kind="exact"`,**不能**用 `legacy_opaque`(它被强制 `start_seq==0` 且全局只许一个)。
3. 只做确定性 fold、不做确定性 **checkpoint**,成本会按 1/8 速率原样回来。
4. `chat_coverage_bounds_after_seq` 的合成行排除口径必须与 `_read_compaction_tail_after_seq` **完全一致**,否则永久损坏 frontier。
5. Anthropic 缓存路径只有 **3** 个断点槽,画像会被静默挤掉 —— 必须显式排序。
