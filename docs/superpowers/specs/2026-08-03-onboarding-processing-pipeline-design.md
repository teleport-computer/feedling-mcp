# 入住记忆处理管线重构 — P0 止血 + 新流程后端支持

- 日期:2026-08-03
- 发起:Seven(产品定稿,2026-07-28 ~ 08-03 多轮讨论)
- 执笔:claude;实现:codex(本 spec 全部为 backend);iOS 侧 claude 并行实现
- 范围:仅 test;上 prod 由 Seven 单独决定

## 0. 背景与事故

两起 prod 事故 + 漏斗数据驱动本次重构:

- **usr_e8fe2688**(OpenRouter / opus-4.6):两个 large 导入并行跑(材料改了 →
  `input_hash` 变 → 新 job_id → `(user_id, job_id)` 锁不命中),~$10 烧尽,
  402 中断,`processed_chunks=0`,0 张卡。
- **usr_9601ae78**(中转站 / opus-4.6-thinking):4 个 job 各跑 17–22 分钟,全部死于
  `ReadTimeout`。thinking 模型出 2400–4000 token JSON 超过 90s 上限;每窗
  90s×3 重试(~274s/窗,worker.py:701 自己注释了这个数);map 有产出但归约调用
  无 try 保护,一次超时整单作废。
- 漏斗:710 用户中 458 卡在 identity 阶段;近 14 天 cohort 完成率 29.6%。

产品定稿(要点):

1. 入住 gate 只到「材料上传完成」(本地动作);**处理(蒸馏)全部移入 App 内可见**,
   聊天永不因处理中/处理失败而锁死。
2. 上传框架不动;改「模型推荐页」:按用户 key 推荐快模型,只显示预计 token 消耗 +
   推荐模型(不显示时间/金钱),可自选。
3. 处理模型是 **job 级 override**,聊天模型配置全程不动。
4. 材料优先级:角色卡/个人档案/记忆摘要先处理(秒级 →「TA 认识你了」节点),
   聊天史后台慢跑。
5. 状态横幅三态(处理中/失败/完成即消失),首页+身份卡+记忆花园三处同源;
   重复上传必须拦(前端拦 + 后端 409 硬闸)。
6. **用户可见文案禁用「蒸馏」二字**,统一「处理/读取/整理」。

## 1. P0 止血(第一批 PR,与新流程解耦,先行合入)

### P0-1 金丝雀预检(canary)

正式跑 map 前,用本次 job 的 distill 模型对**第 1 个窗口**真跑一次(真实 prompt、
真实 max_tokens)。失败或超过 `FEEDLING_GENESIS_CANARY_TIMEOUT_SEC`(默认 60)→
立即 fail job,错误类 `distill_model_too_slow`(新增,归入 provider_config 家族,
不重试),friendly copy 指向「换更快的模型」。canary 成功的产出**计入正式结果**
(idempotency key 与窗口 0 相同),不浪费。

验收:mock 一个 sleep>60s 的 provider,job 在 ~60s 内失败且错误类正确;正常
provider 下 canary 产出被复用(窗口 0 不重复调用)。

### P0-2 超时不做耗尽式重试

genesis lane 内,`ReadTimeout` 类失败**最多重试 1 次**(现为 3 次,provider_client
reliable_chat_completion 默认)。canary 已经证明模型能跑,后续超时更可能是慢而非抖。
实现建议:genesis 的 completion_fn 传 `max_attempts=2`(或按错误类区分:timeout=2,
429/5xx 维持 3)。不要在 llm_client 加第二层重试(worker.py:114 注释的老坑)。

验收:单窗超时的最坏耗时从 ~274s 降到 ≤~185s;429 行为不变。

### P0-3 checkpoint 接线 + 归约保护

- `backend/genesis/checkpoint.py` 已实现未接线(文件头自述)。接线:每窗 map 产出
  即落 checkpoint;重试的 job 跳过已完成窗口。
- 归约阶段(`plaintext.py:865` build_memory_output_from_fact_candidates、
  `:881` build_voice_persona_output_from_candidates)包 try:失败时保留已产出的
  fact_candidates 于 checkpoint,job 标 failed 但**重试从归约开始**,不重跑 map。

验收:map 完成后人为让归约抛错 → 重试 job 零 map 调用直达归约;账单侧调用数减半以上。

### P0-4 per-user in-flight 锁(DB 判定)

新建 plaintext job 前查该用户是否存在 `status=processing` 且 `ingest=plaintext`
的 job(走 `db.genesis_list_jobs`,**不要**进程内 set —— 多 worker 下无效,
TESTING.md §2-M2)。存在 → 409 `import_job_active` + `active_job_id`。
照抄 `genesis_core.py:299` `redistill_job_active` 模式。iOS 依赖此 409 做拦截弹窗。

验收:双投第二单 409;第一单 done/failed 后可新建;跨 worker 并发双投只成一单。

### P0-5 失败不再报 progress:100

`onboarding_validation.py:208` `"progress": 100 if done else (100 if failed else 24)`
→ failed 时报**真实进度**(来自 P0-6 的逐窗计数),`phase_label` 维持 "Genesis failed"。

### P0-6 逐窗进度落盘 + 分材料

`processed_chunks` 现为组级、仅展示。改:每窗 map 完成即递增落库,并新增分材料明细
(见 §2-11 的查询口 schema)。这是节点页/横幅的数据源。

## 2. 新流程支持(第二批 PR,依赖 P0-4/6)

### 2-7 两段提交:estimate → commit

现状:上传即开跑。拆成:

- `POST /v1/genesis/imports/plaintext/estimate`:收与现 upload 相同 payload
  (或先 stage 后引用,codex 定,倾向 stage 免二次传输),**零 LLM 调用**,返回:

```json
{
  "staged_id": "…",
  "materials": [
    {"kind": "ai_persona", "windows": 1, "est_tokens": 4200},
    {"kind": "chat_history", "windows": 19, "est_tokens": 610000}
  ],
  "est_total_tokens": 630000,
  "recommended_model": "anthropic/claude-haiku-4-5" | null
}
```

  token 估算纯算术:窗口字符数 ÷ 3.5(usr_e8fe 的 400 报错实测标定)+ 每窗 prompt
  开销 + max_tokens 上限的输出项。宁高勿低。
- `POST …/plaintext/commit`:`{staged_id, distill_model?}` → 建 job 开跑。
  不带 `distill_model` 则用用户聊天模型(向后兼容)。

### 2-8 distill_model job 级 override

commit 带的 `distill_model` 仅写入该 job metadata,genesis runtime 加载时替换
model 字符串(provider/base_url/key 不变,同 key 换模型)。**绝不触碰用户 runtime
配置**。App 内二次上传同样走 estimate→commit,天然带同一入口。

验收:job 期间与结束后 `/v1/model_api/runtime` 返回的聊天模型不变;job 的 LLM
调用记录(genesis_outputs)model 字段 = override 值。

### 2-9 优先级队列 + identity_ready 节点

组处理顺序固定:`ai_persona → user_profile → memory_summary → history`
(现 `_PLAINTEXT_SOURCE_ORDER` 已是此序,确认并加测试锁定)。前三组全部完成时
置 job 级标志 `identity_ready=true`(落库,进度口暴露)—— 这是 iOS 节点一
(「TA 认识你了」)的判据。节点二 = job done。

### 2-10 推荐模型(服务端)

`estimate` 响应内返回。规则:

- provider=openrouter / anthropic:推荐 `claude-haiku` 最新版。
- provider=openai_compatible(中转站):`GET {base_url}/models`(3s 超时),
  按序匹配 `haiku → flash → mini`(排除含 `thinking` 的 id);拿不到或匹配不上
  → `recommended_model: null`(iOS 显示提示文案,预填现模型)。
- 不做网络探测以外的猜测;推荐失败静默降级,不阻塞 estimate。

### 2-11 进度/状态查询口(横幅数据源)

扩展现有 job status(genesis state / onboarding_validation 均可挂),按材料返回:

```json
{
  "job_id": "…", "status": "processing|failed|done",
  "identity_ready": true,
  "materials": [
    {"kind": "ai_persona", "status": "done", "windows_done": 1, "windows_total": 1, "cards": 4},
    {"kind": "chat_history", "status": "processing", "windows_done": 7, "windows_total": 19, "cards": 11},
    …
  ],
  "error_class": "distill_model_too_slow" | null,
  "friendly_copy": "…(bilingual,禁用「蒸馏」)"
}
```

失败态必须带 `error_class` + friendly_copy;`windows_done` 在失败时保留真实值(P0-5)。

## 3. 文案约束(验收级)

所有用户可见字符串(job friendly_copy、onboarding step required、notices)
**不得出现「蒸馏」**;统一「处理你的文件 / 读取你们的记忆 / 整理记忆」。
验收:grep 后端所有 user-facing 字符串常量,零命中。

## 4. 测试

- 单测按 TESTING.md §2:A(纯后端逻辑)+ B(新路由)+ C(错误 slug)+ Q(409 契约);
  P0-4 需要跨 worker 场景(§2-M2)。
- live(test 环境,复用 test-env recipe):慢模型模拟走 mock provider;
  真跑一遍 estimate→commit→identity_ready→done 全链;409 双投;checkpoint 断点续跑。
- 回归红线:resident sealed lane(`_resident_sealed_import`)与 fresh_start 路径
  行为不变;history_import(hosted lane)不在本 spec 范围,勿动。

## 5. 分批与协作

- PR-1 = P0-1…6(可独立上线,立即止血);PR-2 = 2-7…11。
- codex 写,claude gatekeep;iOS(claude)以 §2-7/2-11 的 schema 为契约并行开发,
  schema 若需调整,mailbox 先对齐再改。
