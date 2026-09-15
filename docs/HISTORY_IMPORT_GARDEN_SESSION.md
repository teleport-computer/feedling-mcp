---
document_lifecycle: decision
canonical_owner: self
---
# 历史导入换到 memgarden 导入会话（IO-6）

2026-09-15。产品决定已定：历史导入的「写记忆卡」这一段交给 memgarden 的宿主驱动导入
会话（`GardenComponent.import_session`）。本文记录落点、默认策略的依据、在途 job 的兼容
和还没定的产品问题。代码是事实源，本文只记代码里看不出来的取舍。

## 之前 vs 之后

    之前  上传 → io 切窗 → 每窗 fact_map（io 提示词抽候选）→ 全部候选 fact_write（io 提示词写卡，
          顺带吐身份卡）→ 一次性落库
          「什么值得记 / 怎么归桶 / 怎么去重」在 genesis/prompts.py 里一份、旧上传入口的候选打分里
          一份、日常落卡（memgarden capture 提示词）里又一份
    之后  上传 → io 切窗（不变）→ 按来源开导入会话 → 一批判断、一批写库、commit(record_ids) →
          进度存进已加密的 genesis checkpoint；身份卡另走一次 identity 推导

用户能看到的变化（合成材料实测，见下文对比）：

    之前：上传英文聊天记录 → 记忆卡全是中文，英文专名被翻掉
    之后：上传英文聊天记录 → 卡是英文（语言跟 io 的导入语言判定走）
    之前：同一份聊天记录再导一次 → 花园多出一整套重复卡
    之后：再导一次 → 已有卡进「已有记忆索引」，模型合并，花园不长
    之前：导入跑到一半 provider 超时 → 重试时从头再写一遍卡（前台那段会重复落卡）
    之后：重试从下一批接着跑，已提交的批次不再问模型、不重复写

## 落点（file::symbol）

| 条线 | 入口 | 之前 | 之后 |
|---|---|---|---|
| 托管 plaintext（iOS 新用户 onboarding、花园「补充材料」） | `genesis/plaintext.py::_run_plaintext_genesis_job` | `_run_plaintext_add_memory_job` / `_run_plaintext_genesis_v2` / v1 循环 → `worker.build_foreground_output_from_texts`（fact_map）→ `build_memory_output_from_fact_candidates`（fact_write）→ `service.apply_memory_outputs` | 新 job：`genesis/plaintext_garden.py::run_add_memory` / `run_onboarding` → `memory/garden_import.py::run_import`；老 job 仍走左列 |
| VPS 自托管（V1 resident） | `tools/chat_resident_consumer.py::_resident_distill_advance_memory` | 同一套 fact_map → fact_write（本地 agent 当模型）→ 客户端封信封 memory.add | 同一个 `run_import`（本地 agent 当模型、客户端封信封、`execute_memory_actions` 写）；进度只在内存 |
| 加密分块导入（`POST /v1/genesis/imports`，Runtime V2 serve-worker 里的 genesis 线程） | `genesis/worker.py::_process_job` | `_build_reducer_output`（fact_map/fact_write）→ 输出 POST 给 apply 路由写卡 | 人设材料不变；其余来源 `worker._garden_reducer_output` 在 worker 内逐批写卡（runtime token），apply 路由只收人设/身份卡和 `garden_import.cards_written` |
| 旧上传入口 `POST /v1/history_import/upload` | `hosted/history_import.py::_process_history_import_sync` | 自己的候选抽取 → 打分 → 渲染 → 兜底卡 → 直写 moments | `_GardenMemoryImport` → 同一个 `run_import`，写 memory action（来源仍记 `history_import`），分层张数上限变成 `max_total_cards` |

共用：

- `memory/garden_import.py` —— 引擎：来源→判断尺子映射、进度状态、崩溃后续写（`pending`）、坏批跳过、
  `write_with_executor`（哪些错算「这张卡不合格」、supersede 目标不在了改新增、什么情况整段失败）。
- `genesis/import_engine.py` —— 托管三件事：`llm_complete`（走 `GenesisLLMClient`：并发槽、canary、
  心跳、调用台账）、`store_writer`（`memory.actions` 执行器，归一化复用 `service._memory_action_from_output`）、
  `existing_cards`（读侧索引；读不到降级为空索引并留 `genesis.garden_import.index_unavailable` 轨迹）。

参数映射：

| memgarden 字段 | io 取值 |
|---|---|
| `batches` | io 切好的窗口（18k 字、8 行重叠，按来源分组；大档位仍按 tier 采样） |
| `policy` / `material_kind` | 长期记忆档案 → `curated_archive`；聊天记录 / 用户档案 / 人设材料 → `history_import` |
| `locale` | `history_import.import_language_with_archive`（档案语言是中文就中文，否则跟材料走）；第一次运行定下，续跑沿用 |
| `naming_rule` / `user_name` | `identity.user_naming._naming_rule`（io 的称呼规则） |
| `fallback_occurred_at` | 只给长期记忆档案，值是关系开始日（与切换前 `preserve_dates` 口径一致）；聊天记录的卡日期来自材料里的时间戳 |
| `max_total_cards` | 只有旧上传入口用（分层配额）；genesis 以前没有总量上限，现在也不加 |
| `existing_cards` / `owner_key` | 读侧索引（id / 摘要 / 桶，仅 active）/ user_id |
| 身份卡 | `foreground_identity.derive_foreground_identity`（前台本来就用它；后台和分块 worker 也改用它，拿真写进去的卡当证据） |

前台/后台（onboarding，genesis v2 开）：前台会话只跑 `_cap_foreground_history_chunks` 采样出来的窗口
（`fg:` 前缀）+ 全部支持材料，写完卡 → 身份卡 → 问候 → identity_ready；后台会话跑剩下的窗口（`bg:`）。
与切换前一样，前台写的是采样窗口里的全部卡（不是只写 3–5 张核心卡 —— 那个核心选择在切换前只用来
给后台去重，现在后台去重靠已有记忆索引，不再需要）。

## 默认策略：`two_pass`

对比方式：同一份合成材料、同一个模型（`deepseek-chat`）、每种跑 3 次；「旧」是切换前托管 add_memory
的 fact_map → fact_write，「新」直接跑本仓库的 `memory/garden_import.py`（真切窗、真参数映射，
只把模型和存储换成替身）。召回按每个预设事实的关键词算；重复按正文二元组包含度 ≥0.82 算；
语言错配是卡的语言和材料语言不一致的张数。表里是 3 次均值（最小–最大）。

| 材料 | 指标 | 旧 io | single_pass | two_pass |
|---|---|---|---|---|
| T1 中文聊天（1 窗） | 召回 / 卡数 | 1.0 / 10.7 | 1.0 / 8 | 1.0 / 9.3 |
| | 调用 / 输出 token | 2 / 1161 | 1 / 1593 | 2 / 2301 |
| T2 英文聊天 9k 字（1 窗） | 召回 / 语言错配 | **0.43 / 9.3 张** | 1.0 / 0 | 1.0 / 0 |
| | 卡内重复对 | 0 | 0.33 | 0.67 |
| | 输出 token | 1137 | 1888 | 2353 |
| T3 中文长期记忆档案 | 召回 / 带日期 | 1.0 / 12 of 12 | 1.0 / 12 of 12 | 1.0 / 12 of 12 |
| T5 ChatGPT JSON 导出（英文） | 召回 / 语言错配 | **0.25 / 7 张** | 1.0 / 0 | 1.0 / 0 |
| T6 人设 + 用户档案（中文） | 召回 / 人设混进记忆卡 | 1.0 / 1.33 | 1.0 / 1.33 | 1.0 / 1.0 |
| | 输出 token | 1192 | 1335 | 2213 |
| T4 把 T1 再导一次 | 花园增长 / 花园内重复对 | **+11 / 7.7** | 0 / 0 | 0 / 0 |
| T7 中文长聊天 ~70k 字（4 窗，同一件事每窗换说法再提） | 召回 / 同事实多余卡 | 1.0 / 0 | 1.0 / 0 | 1.0 / 0 |
| | 调用 / 输出 token | 5 / 1145 | 4 / **3588** | 5 / **2369** |

结论与取舍：

- 两种策略质量都明显好于旧流水线（英文材料不再被写成中文、再导入不再复制花园），两者之间质量打平。
- 成本：一窗的小材料 two_pass 多一次调用、输出 token 约 1.4 倍；**多窗材料反过来** —— single_pass 每窗
  都把前面写过的卡改写一遍（supersede），T7 上输出 token 比 two_pass 多 51%，还在库里留下一串被取代的
  旧版本。真实导入绝大多数是多窗（tier 上限 8–96 窗），所以默认 `two_pass`。
- two_pass 的代价：候选（用户内容）存在进度里 → 托管侧进加密 checkpoint，VPS 只在内存；写卡要等整组
  材料读完才开始（前台只读采样窗口，影响有限）。
- 回滚闸：`FEEDLING_GARDEN_IMPORT_STRATEGY=single_pass`（默认值即 `two_pass`，不设就是它）。已开始的 job
  沿用第一次定下的策略（改导入语义不能续传）。

放弃的替代：保留 io 的 fact_map、只把 fact_write 换成 memgarden（半拟合，判断标准仍然两份）；
single_pass 当默认（小材料便宜，但真实多窗导入更贵、改写链更长）。

合成语料和脚本：scratchpad（不入库）`import-compare2/`（corpus2.py / corpus3.py / run2.py / run3.py /
compare2.py）。

## 在途 job 与崩溃

- **托管 plaintext**：`_PlaintextCheckpointProgress` 看 checkpoint —— 没有 `import_engine` 标记但已有
  旧流水线进度（`map_outputs` / `tasks` / `voice_outputs` / `material_cards`）的 job 在旧流水线上跑完；
  其余（含刚建好、还没进度的）写入标记后一直走新引擎。update_identity 不写卡，不受影响。
- **崩溃续写**：写库之前先把这批的写卡指令存进进度（`pending`），每写完 20 张存一次拿到的 id；续跑时
  pending 就是当前这批就只补写，不再问模型。最坏情况是「写完一段、id 还没存下」那一瞬，重复上限一段。
- **加密**：进度走既有 `service.write_genesis_checkpoint`（共享信封 + sha256 回读），信封 id / K_enclave /
  AAD 都没动；只是 checkpoint 文档里多了 `garden_import` 一块。
- **VPS**：进度只在内存（和切换前一样不落盘）。consumer 自更新重启 = 丢进度、job 由后端回收后重跑；
  重跑时已写的卡在已有记忆索引里。
- **分块 worker / 旧上传入口**：一个 job 一口气跑完，没有持久进度（和切换前一样）。

## 需要 memgarden 版本

`ImportRequest.batches` / `strategy` / `max_total_cards` / `fallback_occurred_at` / `naming_rule`、
`GardenComponent.import_session`、顶层 `ImportProgress` / `ImportBatchResult` —— 0.20.1 之后的下一个发布
（当前在 memgarden `release/next`）。io 的 pin 升级之前，引擎模块只在函数里取这些名字，加载不受影响，
但新引擎的测试和运行需要新版本。

## 还没定的产品问题

1. **VPS 收口复查（missed facts 第二遍）**：保留了，行为不变（原始材料整份 + 这次写的卡再问一次 agent）。
   它仍是 io 自己的写卡提示词（判断标准的第二份），而且整份材料一次喂进去、大材料会撑爆单次调用。
   two_pass 已经是「先读完全部材料再写卡」，这一遍的边际价值需要产品决定：删掉，还是交给 memgarden 做成
   导入会话的可选收尾。
2. **VPS 的记忆张数引导（floor note）随 fact_write 退役**：以前 VPS 写卡提示词里带「花园现有 N 张、
   建议 floor–aspiration 张」的引导；memgarden 没有这个概念，现在不再注入。
3. **分块导入里长期记忆档案不再顺带推出 TA 的名字**：切换前 memory_summary 的 fact_write 会带出
   `agent_name`；身份推导的守卫在没有人设材料 / 助手发言时本来就不给名字。只影响公开的分块导入 API。
4. **旧上传入口（`/v1/history_import/upload`）**：iOS 只在调试开关关掉新流程时才调用；记忆卡已接到同一个
   引擎。它剩下的候选抽取 / 打分函数只被 `hosted/turn.py` 的记忆修复（`/v1/model_api/memory/repair`，
   iOS 不调用，退役路线图里标了删除）用着 —— 那条是另一个管线，要删还是接引擎待定。
5. 大导入的前台采样（8 窗）和 tier 窗口上限（`_select_evenly` 会跳过中间窗口）沿用切换前，没有改。
