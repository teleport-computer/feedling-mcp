# T154 探针缺漏普查（p3 最终清单）

基线：`origin/test@baa22e89309606e1a9fd47b27f2d11a7e972c188`。直接 `debug_trace.trace_event` 数不能当语义探针数：`_trace_genesis`、`_trace_enclave`、`trace_identity_dimensions_set` 等包装器会扩成多个语义位点。下列“有但没实弹验过”表示代码位点/单测存在，但本轮没有看到真实环境持久化后经 admin 出口读回的证据。所有“零探针”判定还带一个前提：trace 开关确实持久化并对执行该路径的 worker 生效；A28 证明这个前提当前并不可靠，因此未实弹条目不得宣称“已覆盖”。

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

## D. 结构性不可得（不是 bug，不追）

1. resident V1/本地 Genesis 的 provider、tool、distillation 内部在用户机器；服务端只能观察 sealed upload/claim/heartbeat/complete 网络边界。
2. local_only memory/worldbook、identity 明文和 E2EE 语义内容服务端按设计不可见；探针只放 count/id/outcome，不放内容。
3. 设备端在 APNs 接受后的实际展示不可得；仅服务端投递边界可测。
4. admin/data_track 是 trace 消费者，不能给它加 trace_event 自我记录；但读侧必须返回覆盖率 metadata，具体字段与验收见 A26，不能据此解释成“读侧不用管”。
5. tee_shadow 的权威观测是系统级 tee_sync_runs/日志，不应硬塞进 per-user debug_trace；问题是计数真实性、分表定位与告警。
6. genesis/foreground.py 等未接入生产的 checkpoint 脚手架不是生产漏探针。

## E. 交叉辖区提醒

- `chat/chat_core.py:trace_response_gated`、`chat/poll_core.py:_trace_poll_delivered` 也是包装器，p1 若只数 raw emitter 会低估语义位点。
- T130 已证实唤醒道 trace_id 曾全空；与本清单 data_track 的 `ungrouped` 跨操作虚假配对直接交叉。
- `db.set_blob` 吞异常是跨辖区共享原语：18 个生产调用点中包含 hosted/turn、proactive 等 p1 路径，不能只按 identity 局部修。
- debug trace 开关自身存在跨 worker 假成功，任何辖区做实弹验收前都必须先“开后跨进程读回”，不能信设置接口的返回值。
- 现有单元测试只证明调用函数，不等于事件真实写入后能从 admin 最终出口读出；本轮没有实弹证据的统一标“未验证”。
