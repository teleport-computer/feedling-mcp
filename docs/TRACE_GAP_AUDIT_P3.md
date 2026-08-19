# T154 探针缺漏普查（p3 最终清单）

基线：`origin/test@baa22e89309606e1a9fd47b27f2d11a7e972c188`。直接 `debug_trace.trace_event` 数不能当语义探针数：`_trace_genesis`、`_trace_enclave`、`trace_identity_dimensions_set` 等包装器会扩成多个语义位点。下列“有但没实弹验过”表示代码位点/单测存在，但本轮没有看到真实环境持久化后经 admin 出口读回的证据。所有“零探针”判定还带一个前提：trace 开关确实持久化并对执行该路径的 worker 生效；A28 证明这个前提当前并不可靠，因此未实弹条目不得宣称“已覆盖”。

第二半（chat/唤醒/runtime）基线：`origin/test@b83922473687a59140320fa868c24b20b839c859`。这一半必须按运行时拆开，不能写“V1 同理”：

| 运行时 | 真正执行位置 | 后端本来可以知道什么 | 本轮证据口径 |
|---|---|---|---|
| `runtime_v2` | 我们的 `serve_worker` / `worker` | 入队、provider/tool、effect disposition、回复事务、job 终态都在我们机器 | 结构上本可完全观测；现状缺口应修，不可用“结构性不可得”豁免 |
| `V1-hosted` | pi/claude CLI 与 consumer 都在我们的托管机器 | consumer 内部探针及后端 poll/response 边界均可获得 | 代码位点丰富，但本轮无真实持久化后 admin 读回证据，统一标“未实弹” |
| `V1-resident` | consumer/CLI 在用户 VPS | 后端可靠知道消息入队、poll/claim、`/chat/response` 接收与回复行终态 | VPS 内部 provider/tool 细节结构性不可保证；只追后端边界，不把远端内部不可达当 bug |

这里的“未实弹/黄格”不只表示“看到了调用点、还差一次读回”：A28 证明 trace 开关可能持久化失败却返回 `enabled=true`，所以连执行当时量具是否真的开启都无法确认。黄格必须同时补“开关跨 worker 生效”与“事件从真实路径持久化后经 admin 出口读回”两层证据，才可升为已覆盖。

V2 不是“没有 trace”：`worker.py` + `serve_worker.py` 当前可发 16 种语义事件，桥接链 `_emit_v2_debug_trace → diagnostics_core.emit_trace_event_payload → debug_trace.trace_event` 也真实连到同一 trace 环。真正的问题是三套观测存储没有统一回答“产出最终有没有发生”：`record_*` 主要进指标表，trajectory 是加密轨迹，只有部分语义事件进 admin trace。语义位点不能用朴素文本计数代替逐点读代码：只数 `trace_event(` 会漏掉包装器；只数 `emit_debug_trace` 符号引用会把声明、None 守卫和传参都算成调用，得到虚高的 39；只数 `emit_debug_trace(` 又会漏掉 `asyncio.to_thread(deps.emit_debug_trace, ...)` 这种可调用对象后接逗号的形态，得到虚低的 3。16 是逐个核实事件名与真实调用路径后的闭集。

**本轮判词：runtime_v2 的产出大部分到不了那个用来发现“被吞”的检测视图；并且唯一能配对的 image 生命周期族，会把明确失败误报成卡死。**

> **实施优先级：先修 A28 的 `debug_trace.set_enabled` 假成功，再补其余探针。** 否则后续“补完后能不能查到”的验收仍可能被同一个假 enabled 欺骗。

## A. 探针缺口清单

1. `[memory/service.py:_load_moments → memory_core.index/list/get/verify] [存储读取/最终响应] [零探针且会制造假成功] [答不了“0 张是真的空，还是 DB 读取失败”] [memory.store.load.error；operation,error_class,trace_id]`
2. `[memory_readside_core.py:_partition_memory_candidates/_local_memory_items → memory_core.index] [候选过滤/最终响应] [半探针：memory.index.called 有返回数，无 invalid/unavailable] [答不了“结果少是无匹配，还是卡损坏/本地不可用”] [memory.index.completed；candidate_count,returned_count,invalid_count,unavailable_count,truncated,query_fingerprint,trace_id]`
3. `[memory_core.py:fetch] [最终响应] [半探针：有 requested/fetched，无 missing/unavailable，也无可靠 trace_id] [答不了“没取回的是不存在还是结构性不可用”] [memory.fetch.completed；requested_count,fetched_count,missing_count,unavailable_count,trace_id]`
4. `[memory_core.py:existing_terms] [enclave 读取/回退/最终响应] [零探针] [答不了 buckets/threads 为空是确实无词条，还是 enclave 失败后 envelope 回退也为空] [memory.terms.completed；operation,returned_count,fallback_used,error_class,trace_id]`
5. `[memory actions + direct add/retype/delete] [写入最终出口] [半探针：actions 只有聚合计数；direct routes 零探针] [答不了每项写动作是否真正落地、哪个结果被丢弃] [memory.card.write.completed；action,memory_id,outcome,changed_field_count,trace_id]`
6. `[genesis/plaintext.py:_run_plaintext_genesis_v2/_run_plaintext_genesis_job] [任务终态] [有探针但假成功] [答不了 job 是否真的 done；failed 可被随后 genesis.plaintext.done 覆盖] [沿用 genesis.plaintext.done/failed，但只从经验证的终态结果发；job_id,mode,stage,error_class,dur_ms]`
7. `[genesis/service.py:apply_reducer_output] [蒸馏产物应用] [半探针：genesis.outputs.applied 不含 planned/written/dropped 全貌] [答不了模型产出多少张、实际写成多少张、哪里丢了] [genesis.outputs.applied；job_id,planned_cards,written_cards,dropped_cards,identity_written,persona_written,profile_written]`
8. `[genesis/llm_client.py:GenesisLLMClient.complete] [模型调用最终出口] [零探针；仅成功输出表元数据/外层 job 失败] [答不了哪种蒸馏任务调用失败、耗时和是否空产出] [genesis.llm_call.completed；job_id,task_kind,outcome,error_class,attempt_count,dur_ms,response_chars,stop_reason]`
9. `[genesis/plaintext.py:_append_plaintext_onboarding_greeting] [问候发布] [零探针] [答不了导入成功但 greeting 是否真正 append；异常被吞成空串] [genesis.greeting.publish.completed；job_id,source,published,error_class,message_id]`
10. `[genesis/genesis_core.py:resident_pending/resident_complete] [本地蒸馏网络边界] [零探针且 complete 会假成功] [答不了 claim 是否缺 material、complete 是否真正写终态后才删 chunks] [genesis.resident.claim.completed / genesis.resident.complete.completed；job_id,outcome,claimed_count,missing_material_count,completion_written,chunks_deleted,memory_action_count,identity_output,trace_id]`
11. `[genesis/persona_backfill.py route] [最终响应] [零探针] [答不了 no_signal、enqueued、failed 三种用户结果] [genesis.persona_backfill.completed；outcome,job_id,error_class,trace_id]`
12. `[worldbook/worldbook_core.py:match] [匹配/注入最终出口] [半探针：只有 block 非空才发 worldbook_injected，且无实弹验过] [答不了空 block 是合法无匹配，还是 rejected/unavailable/corrupt] [worldbook.match.completed；candidate_count,matched_count,rejected_count,unavailable_count,block_chars,outcome,trace_id]`
13. `[worldbook/worldbook_core.py:delete] [写入最终出口] [零探针且会假成功] [答不了 entry 是否实际删除；bool 被忽略仍回 ok=true] [worldbook.entry.write.completed；action,entry_id,outcome,trace_id]`
14. `[hosted/vision_observer.py:observe] [观察最终出口] [半探针：provider/image 失败与调用有事件但无实弹验过；早期 400/404/409 零探针] [答不了一次图片观察从 message 到 route 到最终 observation 的完整结果] [vision.observation.completed；trace_id,message_id,route_id,outcome,error_class,retryable,observation_chars,dur_ms]`
15. `[hosted/setup_core.py:model_api setup/test_provider_key] [文本模型零样本测试出口] [零探针] [答不了用户配置测试是否调用、成功、失败及耗时] [model_api.route_test.completed；test_kind=text,route_id,outcome,error_class,status_code,dur_ms,trace_id]`
16. `[hosted/setup_core.py:_run_setup_main_vision_test] [异步视觉测试出口] [半探针：只在 catalog/probe 不一致时有事件；正常、普通失败、stale 丢弃零探针] [答不了异步 probe 是否真正执行和最终采用] [model_api.setup_vision_probe.completed；route_id,outcome,error_class,dur_ms,trace_id]`
17. `[hosted/setup_core.py:_test_route_image_generation_or_error] [图片生成测试出口] [零探针] [答不了 provider 测试成功/失败与 route test 状态是否写成] [model_api.route_test.completed；test_kind=image_generation,route_id,outcome,error_class,status_code,dur_ms,trace_id]`
18. `[identity/service.py:_load_identity → GET/verify/init] [存储读取/最终响应] [零探针且会制造假成功] [答不了 identity=None 是真的未创建，还是 DB 读失败] [identity.store.load.error；operation,error_class,trace_id]`
19. `[identity/identity_core.py:init/replace → service._save_identity] [写入最终出口] [半探针/假成功：identity_changes 是自然审计记录，但共享 db.set_blob 吞异常后仍写审计并回 2xx] [答不了 identity 是否真正落库] [identity.card.write.completed；action,identity_id,outcome,change_id,changed_field_count,trace_id；根因与统一接线见 A27]`
20. `[identity/actions.py] [动作最终出口] [半探针：dimensions_set 有计数事件、其余主要依赖 identity_changes；均无实弹验过] [答不了 no-op/失败/成功动作在同一 trace 中的最终产出] [identity.action.completed；action,outcome,change_id,changed_field_count,trace_id]`
21. `[core/enclave.py:_decrypt_envelope_via_enclave] [调用前置/最终出口] [半探针：start/done/error 有但未实弹；URL/key 缺失在 start 前退出] [答不了 enclave_unavailable/api_key_unavailable 属于哪次输出失败] [enclave.call.error；purpose,path,error_class,trace_id]`
22. `[core/enclave.py:_enclave_get_json_for_gate/_get_enclave_info] [gate/attestation 输出] [零探针] [答不了 gate 或公钥/attestation 获取是否成功及耗时] [enclave.gate.completed / enclave.attestation.fetch.completed；purpose,outcome,error_class,dur_ms,trace_id]`
23. `[admin/tee_sync_scheduler.py + tee_shadow/reconciler.py:reconcile_all] [系统同步最终出口] [半探针：tee_sync_runs.snapshot_failures 一直有写，但全仓无基于它的告警/阈值/展示；外层 snapshot 异常还可能记 0] [答不了失败表、被连带跳过的后续表、每 tick 新增 mirror failure；精确形状是“记下来了没人看”] [系统指标而非用户 debug trace：tee.sync.stage.completed / tee.sync.coverage / tee.mirror.failure.delta；run_id,stage,outcome,failed_table_count,failed_tables,unwired_tables,delta,by_table,duration_ms]`
24. `[admin/data_track.py:_debug_trace_stem/_debug_trace_detect_stall] [trace 消费/卡死检测] [有数据但探测器结构性失明] [只认 .start/.done/.error，答不了 genesis 的 .started/.failed、vision 的 .called/.completed 是否卡死] [不新增 trace；统一生命周期或显式 phase/outcome；验收：genesis start 无 terminal 必须出现在 stalled 视图]`
25. `[admin/data_track.py:_debug_trace_group_turns] [trace 分组/卡死检测] [有数据但会虚假配对] [空 trace_id 全落 ungrouped，A 的 start 可被 B 的 done 配掉，真实卡死被判 ok] [生产者补可靠 trace_id，或消费端隔离未知操作；验收：两个空 trace_id 操作不得跨操作配对]`
26. `[admin/data_track.py:_debug_trace_events_from_blobs] [trace 读取/最终管理视图] [半探针：坏事件、坏时间戳静默丢弃/归零] [答不了管理页结果是否完整] [不是自写 trace；在读侧返回 coverage：invalid_event_count,invalid_timestamp_count,ungrouped_count,oldest_retained_at,partial_before]`
27. `[db.py:set_blob（AST 复算 backend 生产代码 18 个调用点，identity/genesis/memory/proactive/hosted 等）] [共享写原语最终出口] [零探针且吞异常制造跨子系统假成功；严格版 set_blob_strict 仅 3 个生产调用（含 db.py 内部 2 个）] [答不了任一调用者“函数正常返回”是否等于 blob 真正写成] [blob.write.error；kind,error_class,operation,trace_id；原语 except 内统一发，禁止 doc/模型/主机字段]`
28. `[debug_trace.py:set_enabled → db.set_blob] [探针开关最终出口] [零探针且假成功：写失败被共享原语吞掉，仍无条件置本进程缓存并回 enabled=true] [答不了“开启 trace”是否真正持久化、是否对其他进程生效；TTL/重启后当前进程也会悄悄回退] [A27 的 blob.write.error 覆盖 + set_enabled 严格写后读回；验收：注入持久化失败后接口不得回 enabled=true，且不得置缓存]`
29. `[runtime_v2:worker/serve_worker → admin/data_track 卡死检测] [最终管理视图] [到得了 trace 环但大多不参与生命周期配对：16 种事件中只有 agent.image.generate.start/.done 命中 .start/.done/.error 契约；其余语义事件不配对，image 失败还叫 .failed] [答不了一轮 V2 chat/wake/extraction 是否从开始走到真实终态；图片明确失败会被误报 stalled] [不盲加 emitter；统一显式 phase/outcome 或扩展读侧闭集契约，验收见 A24，并加入 .failed→error 兼容用例]`
30. `[runtime_v2:worker 各 emit_debug_trace 调用点] [跨事件归并] [半探针：mcp.surface.provider/mcp.roundtrip.provider/provider.empty_response 带 turn trace_id；prompt frontier、context.truncation、agent.tool.call、thinking.surfaced、reply.language_follow、sanitized、mcp.turn.usage、image/MCP-resolved 等多处不传 trace_id] [答不了这些事件属于哪次 job/哪轮用户输入；唤醒 trace_id 为空时还会叠加 A25 的 ungrouped 跨回合虚假配对] [所有 job 内事件透传同一 trace_id + job_id + lane；验收：两个并发 job 的事件不得落同一 ungrouped 桶]`
31. `[runtime_v2:serve_worker.py:_sink_reply_in_transaction + worker.py:process_job._on_reply] [聊天回复最终提交] [留在别处：权威 chat 行与 effect disposition 已有，visible_reply 进 v2_turn_metrics，disposition 进加密 trajectory；普通成功没有内容无关的 debug 终态事件] [admin trace 答不了“模型说完后，气泡到底 committed / discarded / uncertain / superseded”] [在已核验 effect disposition/事务提交后发 reply.publish.done/error；job_id,trace_id,lane,outcome,part_count,content_types,reply_through_seq,dur_ms，不记正文]`
32. `[runtime_v2:tool_loop.py:require_reply + worker.py:_run_wake] [主动消息最终处置] [真零/留在别处混合：弱唤醒 require_reply=False 的空 provider 回复不会调用 on_empty_provider_response；stay_silent、chat_collision、input_advanced、published 主要只在 job 字段/指标/加密轨迹，普通 trace 无统一终态] [答不了“没气泡”是模型明确睡回去、provider 空回、聊天碰撞、输入推进，还是发布失败] [wake.turn.done/error；job_id,trace_id,lane,outcome=published|stay_silent|provider_empty|chat_collision|input_advanced|failed,reply_count,error_code,dur_ms]`
33. `[runtime_v2:prompt_frontier + _run_wake scheduled] [定时/主动任务失败终态] [半探针：有 v2.prompt_frontier.exhausted、指标、加密轨迹和 agent_jobs.last_error，但没有可连续聚合的 wake 终态 trace] [答不了“某用户已连续 N 天唤醒失败”；只查单次语义事件会漏持续失联] [复用 A32 的 wake.turn.error，增加 bounded consecutive_failures；实证：usr_fee1dfed 近 3 天 28 次 heartbeat 均为 wake_failed:prompt_frontier_exhausted，仅查 agent_jobs 才发现]`
34. `[runtime_v2:worker.py finally → send_reply_push/publish_voice_reply] [通知/语音投递边界] [零 trace：wake push 的 bool 仅折进 shadow row，chat push bool 被忽略；异常与 voice publish 异常只写 log] [答不了“气泡已提交但 APNs 是 suppressed/no_token/accepted/rejected/error，或语音是否发布”] [delivery.push.done/error + delivery.voice.done/error；job_id,trace_id,lane,outcome,transport_code,dur_ms；设备最终展示仍属 D3]`
35. `[runtime_v2:serve_worker.py:_generate_image_reply] [生图最终出口] [半探针且误报：route 未 ready、credential 缺失/解密失败都在 agent.image.generate.start 之前退出；进入 provider 后成功是 .done，失败却是 .failed，无法关闭 .start] [答不了 preflight 为什么没开始；已结束失败会被管理端显示成卡死] [所有 preflight 失败发同 stem 的 agent.image.generate.error，并把现有 .failed 迁移/兼容为 .error；trace_id,job_id,route_kind,error_code,dur_ms]`
36. `[runtime_v2:worker.py:_make_chat_tool_activity_callback] [工具调用最终出口] [半探针：status stream 有 started/result/error，debug trace 只在终态发 agent.tool.call 且通常无 trace_id] [admin trace 答不了工具是否卡在 started，也无法把终态可靠并回用户 turn] [桥接权威 status 边界为 agent.tool.start/done/error，或让检测器读取 status stream；activity_id,job_id,trace_id,tool,result_code,dur_ms]`
37. `[runtime_v2:capture_scheduler + serve_worker._tick_capture_for_user + worker._run_extraction] [记忆抓取调度/终态] [留在别处：V2 enqueue 明写 trace_id=None；gate 早退只返回 reason；V2 终态写 agent_jobs/capture state/metrics/trajectory，未复用 legacy 的 memory.capture.done/error trace] [答不了一次 capture 是没触发、coalesced、0 卡成功、写成 N 卡还是失败] [复用既有 memory.capture.done/error 形状并补 start；job_id 作为稳定 trace_id，trigger,outcome,cards_planned,cards_written,cards_failed,through_seq,dur_ms]`
38. `[runtime_v2:dream_scheduler + worker._run_extraction（逐项计数约 10629-10638；部分失败后仍 _complete_extraction/tm ok 约 10647-10665；_complete_extraction 内 mark_completed 约 9970-9984）] [梦境整理调度/终态] [半探针/假成功：memory.dream.tick 只记“判定/入队”；record_dream_job_status 没终态 trace；部分 memory action 失败时仍 mark_completed、tm status=ok，只剩 warning] [答不了梦真正整理/合并多少、是否部分丢写；tick 绿不能证明产出成功] [memory.dream.start/done/error；job_id,trace_id,outcome,proposals,applied,skipped,failed,organized,merged,dur_ms；failed>0 不得只报 ok]`
39. `[runtime_v2:worker.py:_run_extraction memory_context_reader] [抽取输入完整性/最终产物] [零 trace：memory context 读取异常仅 warning 后降级为空继续，capture/dream 仍可完成并写卡] [答不了“0/少量产物”是模型判断，还是 buckets/threads/cards 上下文缺失后的降级结果] [memory.extraction.context.error + 终态 degraded_context=true；lane,job_id,trace_id,error_class；不记录正文]`
40. `[runtime_v2:process_job/_run_wake completed 后 apply_pending_effects] [末轮副作用最终投递] [半探针/条件性假成功：job/reply 已 completed 后 drain；chat 只单独 surface EffectDeliveryUncertainError，其他异常以及 wake 全部 log-only，指标仍 ok] [答不了末轮写工具是否仍 pending/失败；用户可能看到完成回复但动作尚未落地] [effect.delivery.done/error；job_id,trace_id,lane,pending_count,applied_count,uncertain_count,effect_types；不得把 job 状态复制成产出]`
41. `[V1-hosted:consumer _run_agent → post_reply → backend chat_core.write_response] [聊天回复最终提交] [半探针且未实弹：agent.model.call.start/done/error 可配对，agent.reply 是发布前解析结果；真正提交只有 backend chat.response（不与 poll/claim 形成同 stem 生命周期）] [consumer 在 model.done 后、POST 前退出时，卡死检测显示模型已正常结束，却答不了用户为什么没有回复] [后端权威边界 resident.turn.start/done/error；parent_message_id/trace_id,consumer_id,runtime=hosted,outcome,reply_id,dur_ms；验收必须从 admin 最终出口读回]`
42. `[V1-hosted:chat_send_core.py 图片/文件 caption envelope] [用户输入最终接收] [零探针且降级成功：caption seal 失败只 print，主图片/文件仍 append 并 route.decided=agent_runtime] [用户写了说明但模型只收到附件，202/route trace 看起来仍是完整接受] [chat.input.caption.error；runtime=hosted,kind=image|file,message_id,trace_id,error_class；响应或终态 metadata 明示 degraded_input]`
43. `[V1-hosted:chat_resident_consumer.py:_emit_debug_trace] [探针持久化最终出口] [有代码无实弹：daemon thread 异步 POST，_post_debug_trace_event 不检查 HTTP status、不重试，异常静默吞；但执行位于我方机器，非结构性不可得] [答不了丰富的 model/MCP/reply 探针是否真的持久化并从 admin 可读] [托管环境做一条 fake CLI 的真实 E2E：start→done→server reply commit→admin readback；对 POST 非 2xx 计数/有界重试，不影响 turn]`
44. `[V1-resident:chat.poll.delivered → /v1/chat/response → chat.response] [后端可见的领取/回复终态] [半探针：边界都在后端且回复 chat 行是权威产物，但事件名不构成同 stem start/done/error] [答不了“消息已被哪个 resident 领走后多久仍未交回复”，只能人工拼 poll、claim、parent reply link] [resident.turn.start/done/error；parent_message_id 为 trace_id，consumer_id,outcome,reply_id,claim_age_ms；只观测网络边界，不要求 VPS 内部细节]`
45. `[V1-hosted + V1-resident:chat_core.trace_response_gated / consumer _post_reply_with_bootstrap_retry] [回复拒绝终态] [部分已覆盖：T139 已对显式 retryable=true 的 bootstrap_incomplete 409 做指数退避、有次数与 elapsed 硬上限的有界重试，覆盖瞬时误判；但重试耗尽后 consumer 仍记 terminal_response_error 并推进 checkpoint，父消息没有已回复/失败的权威终态] [用户发送已被接受，瞬时门禁可恢复；持续门禁时仍可能永久没有气泡，trace 只有“gate fired”] [在父消息/claim 上原子写 resident.turn.error outcome=bootstrap_incomplete，客户端可见稳定失败；trace_id=reply_to_message_id,stage,consumer_id]`

### A27 共享根因的 18 个生产调用按用户影响分层

- **直接用户可见/客户端会收到成功或读取该产物（9）**：`bootstrap` 首次初始化、memory migration progress、identity 卡、用户 MCP 配置、history import job、proactive settings、Genesis state、Genesis persona、Genesis voice。
- **间接改变用户体验（7）**：frames metadata、push tokens、push cooldown、live-activity dedupe、hosted turn pending cleanup、capture scheduler state、dream scheduler state；失败可表现为图片索引缺失、推送缺失/重复、状态动作重复或记忆任务漏跑/重跑。
- **主要是运维控制（1）**：debug trace enabled flag，失败会让管理端以为开关已保存。
- **已有调用者自校验（1）**：Genesis checkpoint 写后立即 `get_blob_strict` 对 digest，吞异常会被转成 `genesis_checkpoint_write_failed`，不属于用户假成功优先修复面。

以上计数来自 AST `Call` 节点，不含注释、日志字符串、函数定义；优先级应先放“直接用户可见 9 处”，但根因接线仍应在共享原语一次完成。

## B. 静默出口表

| 坐标 | 留下什么 | 什么会消失 |
|---|---|---|
| memory/service.py:_load_moments | stdout print | DB 异常被转成空列表，调用者照常 2xx |
| memory_core.py:existing_terms | 无 | enclave 异常被吞，回退也空时无法区分 |
| genesis/plaintext.py:_append_plaintext_onboarding_greeting | stdout/空串 | greeting append 失败，主 job 仍继续 |
| genesis/plaintext.py:_run_plaintext_genesis_v2 | job failed 状态但随后 done trace | 真失败从 trace 终态视图消失 |
| genesis/genesis_core.py:resident_complete | 可能只剩 200 done | completion 未写却继续删 chunks |
| worldbook_core.py:match/delete | 空 match 无事件；delete 恒 ok | unavailable/rejected/no-op 与成功混同 |
| vision_observer.py:observe | 400/404/409 响应 | invalid request/message missing/route mismatch 无 trace |
| setup_core.py:_run_setup_main_vision_test | stdout 或什么都没有 | stale/inactive/普通失败/thread 启动问题无最终事件 |
| identity/service.py:_load_identity/_save_identity | stdout/log/审计可能仍成功 | 读失败变 None，写失败被 set_blob 吞掉 |
| core/enclave.py decrypt preflight | 仅异常响应 | 缺 URL/key 在 start 事件前退出 |
| tee scheduler/reconciler | warning/部分 run row | snapshot 外层异常计数仍 0；一表异常使后表不执行 |
| admin/data_track read | 无 | 非 dict/坏时间戳/生命周期后缀不识别导致覆盖缺失 |
| debug_trace.py:set_enabled | 当前进程临时缓存 + enabled=true 响应 | blob 未落库时其他 worker 仍关闭，TTL/重启后本进程也回退 |
| runtime_v2 chat reply sink | 权威加密 chat 行、指标、加密 trajectory | 普通 debug trace 没有“气泡已提交/被丢弃/不确定”的终态输出事件 |
| runtime_v2 weak wake 空 provider / stay_silent / chat collision | job 字段、指标或加密 trajectory 的一部分 | require_reply=False 时 provider.empty_response 不发；管理端无法区分几种合法/异常沉默 |
| runtime_v2 image preflight | 异常/外层失败状态 | route 未就绪、credential 缺失或解密失败在 image.start 前退出；已有失败又因 .failed 后缀被误判 stalled |
| runtime_v2 capture/dream | agent_jobs、state blob、v2_turn_metrics、加密 trajectory | capture 未复用 legacy done/error；dream 只有 tick、没有实际整理终态 |
| runtime_v2 push/voice finally | wake shadow 的一个 bool 或 log | chat push 返回值、push/voice 异常没有可查询的最终投递结果 |
| V1-hosted caption seal | stdout print | 附件仍被接受，但用户说明从模型输入消失 |
| V1-hosted model.done → post_reply | model.done/agent.reply（解析结果） | consumer 在 POST 前退出时没有后端回复终态，现有卡死检测认为模型阶段已结束 |
| V1 common bootstrap_incomplete response gate | chat.response.gated trace + 409；T139 已覆盖显式 retryable=true 的瞬时误判 | 有界重试耗尽后 consumer 推进 checkpoint；父用户消息仍没有回复或失败终态 |

## C. 假成功表（与零探针分流）

统一验收：对每行注入实际写/发布失败，`success/done` 必须消失或变成 `failed`，并与权威落点对账；不能只断言“事件出现过”。

| 坐标 | 假成功长什么样 | 权威状态在哪 | 用户/排障者会看到什么 | 当前误导 |
|---|---|---|---|---|
| memory/service.py:_load_moments → index/list/get/verify | DB exception → []，index 还发 items=0 ok | DB 读取异常 | 花园为空/0 张/未写入，而非服务失败 | **是，用户可见** |
| genesis/plaintext.py:1583-1584 → 2371-2373 | mark_failed 后 return True，外层发 genesis.plaintext.done | genesis job/state=failed | 管理 trace 说 done，排障会停止追查 | 是，排障者 |
| genesis/plaintext.py:background catch → return True | enrichment exception mark_failed，随后外层 done | genesis job/state=failed | 同一 job 同时 failed 与 done | 是，排障者 |
| genesis/plaintext.py:_complete_plaintext_v2_job | genesis_complete_job 返回 None 不报错，v2 仍 return True | job row 未转 done | trace 说 done，用户 job 可能仍 processing | **是，用户与排障者** |
| genesis/genesis_core.py:resident_complete | 入口确认 job 存在后，若 complete 与 delete 两语句间 job 行被并发删除/reset，UPDATE 零行返 None；仍删 chunks 并回 `{status:done}` | genesis job row/受影响 chunks | 窄窗竞态下客户端看到 done、材料已删且终态未写；普通 DB 异常会抛出并阻止 delete | 条件性用户风险，非宽路径 |
| worldbook/worldbook_core.py:delete | 忽略 bool，恒回 `{ok:true}` | store.delete_world_book 返回值/DB row | 不存在或未删也显示成功 | **是，用户可见** |
| db.py:set_blob（identity 是已核实例；backend 共 18 个生产调用点） | 主写 set_blob_strict 异常被共享原语吞掉并正常返回；各调用者可继续 success/audit/2xx | 各 kind 的 user_blobs 行；identity 为 user_blobs.identity | identity 用户看到 created/replaced 但卡未落库；其余调用处同族结果需按调用者分流 | **是，跨子系统；identity 用户可见已证实** |
| debug_trace.py:set_enabled | blob 写失败仍无条件写当前进程 TTL cache 并回 `enabled=true` | DEBUG_TRACE_FLAG_BLOB + 各 worker 自己的 cache | 排障者以为全局已开，实际只有当前进程短暂记录；之后又静默关闭 | **是，量具自身假成功，影响所有零探针判断** |
| admin/tee_sync_scheduler.py:snapshot catch | 外层 exception 只 warning，snapshot_failures 默认 0 | exception/log/缺失 report | run row 看似 0 failure，值班误判健康 | 是，运维/排障者 |
| runtime_v2 image generation | start 后真实失败发 `.failed`，检测器只认 `.error`，start 永不关闭 | provider 异常 + agent.image.generate.failed | 管理端显示“仍卡住”，值班会追不存在的挂起 | **是，排障状态假成功/假卡死** |
| runtime_v2 dream partial memory writes | 部分 action failed 仍 mark_completed，tm status=ok | apply_memory_actions 的逐项结果 | 做梦看似成功，部分整理/合并实际没落地 | **是，后台产物部分丢失** |
| runtime_v2 completed 后 effect drain | job/reply 已完成；generic drain exception 仅 log，wake 亦如此 | effect outbox 待处理/失败行 | 用户看到完成回复，但末轮写工具可能仍 pending/失败 | 条件性用户可见，需按 effect 权威状态判定 |
| V1-hosted attachment caption | caption envelope 构建失败后仍 202/route.decided | caption envelope 是否写进 message extra | 用户以为附件与说明一起送达，模型实际只见附件 | **是，输入降级未声明** |
| V1-hosted `agent.reply` 探针 | 解析出文本即发，发生在 `post_reply` 之前 | backend chat reply row / chat.response | 排障者可能把“解析成功”误当“已发布” | 是，探针记了中间变量而非产出 |
| V1 common bootstrap response gate | T139 有界重试耗尽后，consumer 把持续 409 当 terminal 并推进 checkpoint | 父消息 reply_status/reply_message_id 或显式失败终态 | 用户消息已接受、瞬时误判已能恢复；持续门禁时 consumer 不再重试且没有回复 | **是，剩余用户可见静默终止** |

## D. 结构性不可得（不是 bug，不追）

1. resident V1/本地 Genesis 的 provider、tool、distillation 内部在用户机器；服务端只能观察 sealed upload/claim/heartbeat/complete 网络边界。
2. local_only memory/worldbook、identity 明文和 E2EE 语义内容服务端按设计不可见；探针只放 count/id/outcome，不放内容。
3. 设备端在 APNs 接受后的实际展示不可得；仅服务端投递边界可测。
4. admin/data_track 是 trace 消费者，不能给它加 trace_event 自我记录；但读侧必须返回覆盖率 metadata，具体字段与验收见 A26，不能据此解释成“读侧不用管”。
5. tee_shadow 的权威观测是系统级 tee_sync_runs/日志，不应硬塞进 per-user debug_trace；问题是计数真实性、分表定位与告警。
6. genesis/foreground.py 等未接入生产的 checkpoint 脚手架不是生产漏探针。
7. V1-resident 的 provider/CLI/tool/MCP 内部运行在用户 VPS；后端不能保证进程内细节、stdout 或本机文件可取，这不是服务端 bug。
8. resident consumer 的 `_emit_debug_trace` 会尝试经 HTTP 回传内部事件，但它是 daemon thread + best-effort 网络；用户机器断网/退出时丢失属于结构边界，不能把“应该必达”作为后端正确性前提。
9. V1-resident 的消息入队、poll/claim、`/v1/chat/response` 接收、父消息 CAS 与最终 reply 行都发生在后端，**不是**结构性不可得；A44/A45 只要求补齐这些权威网络边界。
10. V1-hosted 的 consumer/CLI 在我们的机器上，不适用 resident 的结构豁免；它缺的是实弹持久化验收与最终发布边界。
11. 加密 trajectory 的正文与 local-only 内容不应搬进 admin trace；只桥接闭集 outcome、计数、job/trace id 和耗时。

## E. 交叉辖区提醒

- `chat/chat_core.py:trace_response_gated`、`chat/poll_core.py:_trace_poll_delivered` 也是包装器，p1 若只数 raw emitter 会低估语义位点。
- 只搜 `emit_debug_trace(` 也会低估：`asyncio.to_thread(deps.emit_debug_trace, ...)` 把函数当参数传递，名字后是逗号；必须同时搜符号引用并逐个读语义调用点。
- T130 已证实唤醒道 trace_id 曾全空；与本清单 data_track 的 `ungrouped` 跨操作虚假配对直接交叉。
- `db.set_blob` 吞异常是跨辖区共享原语：18 个生产调用点中包含 hosted/turn、proactive 等 p1 路径，不能只按 identity 局部修。
- debug trace 开关自身存在跨 worker 假成功，任何辖区做实弹验收前都必须先“开后跨进程读回”，不能信设置接口的返回值。
- 现有单元测试只证明调用函数，不等于事件真实写入后能从 admin 最终出口读出；本轮没有实弹证据的统一标“未验证”。
- 三运行时的最终验收必须分开：runtime_v2 用真实 reply/effect/job 出口与 admin readback 对账；V1-hosted 启动托管 fake CLI 跑 model→POST→reply row→admin；V1-resident 只用后端 enqueue/claim/response/lease-expiry 构造矩阵，不要求 VPS 内部探针必达。
- 所有“补事件”测试都必须钉最终出口：删除事件桥或把 `.error` 改回 `.failed` 时，admin stalled/terminal 视图必须变红；仅断言 callback 被调用不算覆盖。
