# API 错误 slug 契约表

> `{"error": "<slug>"}` 的 slug 是稳定 API 面：一经写入本表即冻结，废弃走
> 「新增新 slug、旧 slug 保留」。新增错误返回必须先登记到本表（CONTRIBUTING
> 有此纪律）。iOS 本地化表以本表为输入；「需本地化」为空的 slug 走通用文案。
> 对外渲染规则见 docs/FRONTEND_ERROR_CONTRACT.md。
>
> **盘点方法**：`grep -rhoE '"error":\s*"[a-z_0-9]+"' backend/ --include="*.py"`
> （不要求 `{` 开头，覆盖多行 dict 字面量）+ 单独查 `_bad(`/`json_error(`/
> `api_error(` 等经 helper 拼装的调用点（genesis_core.py 的 `_bad(slug, status)`
> 就是这类，naive grep 会漏）。剔除：自由文本消息（含空格，不是合法 slug）、
> 仅供 AI 工具调用结果/内部 trace 字段消费（从不作为 HTTP 响应体顶层 `error`
> 返给 iOS 的，如 `screen/caption.py`、`screen/frames.py`、
> `proactive/tool_executor_v2.py`、`hosted/turn.py` 的 `last_runtime_error`
> 字段、`admin/data_track.py` 的 trace 反射字段）。状态码从直接返回处/调用处读；
> 多处不一致的用 `xxx/yyy` 列出全部。blame 只标能明确判定的（基础设施/我方
> bug → `system`；用户自己的 provider 配置/额度问题 → `user_provider`；纯参数
> 校验错误不判 blame，留 `—`）。「需本地化」按
> `docs/FRONTEND_ERROR_CONTRACT.md` §三目录勾选，未在该目录里的留空（多数是
> 校验类错误，走 `invalid_payload`/`detail` 通用兜底文案，不需要逐条本地化）。
> `enclave/*` 是独立的 backend↔enclave 内网面，iOS 从不直连，见文末单独一节。

## 通用

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `unauthorized` | 401 | — | 未认证/凭证失效 | ✅ |
| `forbidden` | 403 | — | 无权限 | ✅ |
| `internal_error` | 500 | system | 未捕获异常兜底（必带 request_id） | ✅ |
| `invalid_payload` | 400 | — | body/参数校验失败（FastAPI 校验重塑；detail 带 `[{loc,msg}]`，≤10 条） | ✅ |
| `envelope_missing_fields` | 400 | — | 加密信封缺字段（detail 带缺失字段名列表） | |
| `thinking_envelope_missing_fields` | 400 | — | 同上，thinking 信封 | |
| `anchor_required` | 400 | — | 记忆动作缺 anchor（detail.mem_type） | |
| `service_busy` | 503 | system | db 连接池耗尽，可退避重试 | ✅ |
| `service_unavailable` | 503 | system | admin token 未配置 | ✅ |
| `not_found` | 404 | — | 通用资源不存在 | ✅ |
| `not_owned` | 403 | — | 资源不属于调用者 | ✅ |
| `invalid_image` | 400 | — | 图片校验失败 | ✅ |
| `unsupported_file_type` | 400 | — | 聊天文件上传：文件类型不支持（heic/.doc/.xls/二进制）；detail 说明类型，hint 建议格式 | ✅ |
| `invalid_file` | 400 | — | 聊天文件上传：file_b64 缺失/空/非法 base64 | ✅ |

## 认证 / 账号

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `user_not_found` | 404 | — | admin 端点专用（用户侧 accounts_core.py 用的是自由文本 `"user not found"`，不是本 slug——历史不一致，见文末已知问题） | ✅ |
| `account_not_found` | 404 | — | | ✅ |
| `no_recoverable_account` | 404 | — | 密钥恢复：无匹配账号 | ✅ |
| `invalid_or_expired_challenge` | 401 | — | | ✅ |
| `challenge_failed` | 401 | — | | ✅ |
| `token_expired` | 410 | — | 邀请/迁移 token 过期 | ✅ |
| `token_already_used` | 409 | — | | ✅ |
| `invalid_token` | 404 | — | | ✅ |
| `token_access_mode_invalid` | 400 | — | | |
| `account_exists_for_key` | 409 | — | 注册时该 content public key 已有账号（引导走 recover） | |

## model_api / provider 配置（设置页）

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `model_api_not_configured` | 400/404 | user_provider | 400=运行时加载路径（chat_send/history_import/genesis plaintext）；404=setup_core 直接查询 | ✅ |
| `model_api_not_tested` | 400 | user_provider | 已配置但未通过测试 | ✅ |
| `model_api_config_invalid` | 400 | user_provider | | ✅ |
| `model_api_key_decrypt_failed` | 400 | system | | ✅ |
| `model_api_key_envelope_missing` | 400/404 | user_provider | 同 model_api_not_configured 的两条路径 | ✅ |
| `model_api_credential_write_failed` | 500 | system | 写 model_api_credentials 失败（DB 异常被 db.py 吞成 None） | |
| `model_api_route_write_failed` | 500 | system | 写/激活 model_api_routes 失败（DB 异常，或 route 被并发删除） | |
| `prompt_context_limit_unconfigured` | 400 | user_provider | 未审计的 OpenRouter/custom route 未提供 `context_window_tokens`；setup 在 provider I/O/写库前拒绝 | |
| `invalid_context_window_tokens` | 400 | user_provider | context window 越界、非整数，或不大于 output reserve + safety margin | |
| `prompt_frontier_configuration_invalid` | 503 | system | 服务端 prompt-window override/reserve 配置非法 | |
| `provider_not_configured` | 409 | user_provider | | ✅ |
| `provider_not_hostable` | 409 | user_provider | | ✅ |
| `hosting_runtime_unavailable` | 503 | system | 历史 hosted-supervisor 兼容 slug；Runtime V2 托管路径不再返回（worker 全挂改用 `workers_unavailable`） | ✅ |
| `provider_test_failed` | 400 | user_provider | 保存/测试 key 时上游拒绝（detail 带 status_code） | |
| `cannot_encrypt_provider_key` | 409 | — | 缺 content public key 或 enclave attestation 不可达 | |
| `route_not_found` | 404 | user_provider | 指定的 route id 不属于该用户或已删除 | |
| `credential_not_found` | 404 | user_provider | 指定的 credential id 不属于该用户或已删除 | |
| `api_key_or_credential_id_required` | 400 | user_provider | `POST /routes` 与 `POST /v1/model_api/models` 必须且只能给 api_key 与 credential_id 之一（present 且非空；给了 null/空串/两者都给都算违约） | |
| `nothing_to_update` | 400 | — | PATCH /credentials 两者（label/api_key）都不给 | |
| `invalid_reasoning_effort` | 400 | — | reasoning effort 取值非法（setup/patch 两处校验） | |
| `model_api_config_delete_failed` | 500 | system | 删除 model_api 配置时 DB 写失败 | |
| `model_catalog_auth_failed` | 400 | user_provider | `POST /models` 目录拉取：上游 401，provider key 被拒（本接口绝不透传 401，避免 iOS 误判登录失效） | ✅ |
| `model_catalog_access_denied` | 400 | user_provider | 目录拉取上游 402/403/451：额度/权限/地区/项目限制，非「key 错」 | ✅ |
| `model_catalog_rate_limited` | 429 | user_provider | 目录拉取上游 429 限流，保留输入可重试 | ✅ |
| `model_catalog_temporarily_unavailable` | 503 | user_provider | 目录拉取上游超时/网络/408/425/所有 5xx，可重试 | ✅ |
| `model_catalog_invalid_response` | 400 | user_provider | 目录拉取上游非 JSON/超限/坏 shape/其余 4xx（400/404/422 等），非鉴权问题 | ✅ |
| `vision_runtime_v2_required` | 409 | user_environment | 专用视觉路由仅支持 Hosted Runtime V2；V1/VPS 旧图片链路不变 | ✅ |
| `vision_model_required` | 409 | user_provider | V2 图片发送前没有已验证可用的视觉路由；消息尚未落库 | ✅ |
| `vision_model_unsupported` | 400 | user_provider | 候选模型未正确识别随机视觉测试图；可保留选择但图片发送仍被阻止 | ✅ |
| `vision_model_test_failed` | 400 | user_provider | 视觉能力验证因鉴权、额度、网络或 provider 错误未完成 | ✅ |
| `invalid_vision_mode` | 400 | user_provider | mode 不是 follow_main 或 dedicated | |

## `GET /v1/model_api/usage`（provider 账单/额度查询）

> 4xx 全部复用上面 model_api / provider 配置表已登记的 loader slug——本端点
> 先走 `hosted/usage_core.py` 的 `resolve_usage_config`（其实是
> `config_store._load_runtime_provider_config`），未配置/未测试通过/信封缺失/
> 解密失败/config 非法分别命中 `model_api_not_configured` /
> `model_api_not_tested` / `model_api_key_envelope_missing` /
> `model_api_key_decrypt_failed` / `model_api_config_invalid`，状态码统一
> 400（不复用 setup_core 那条 404 分支）。下表列的是 200 响应体内
> `error` / `metrics.<key>.reason` 字段的取值——外呼 provider 计费接口失败时
> 整个请求仍是 200，只是 `status` 降级为 `partial`/`error`，具体原因走这些
> reason slug（同 `core/provider_usage.py` 的 `_err_slug`）。「状态码」列在本节
> 恒为 `—`（同「通知中心 error_class」节的复用姿势，非 HTTP 状态码，仅为复用
> 上面表格的行格式/守卫测试）。

| error/reason slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `usage_timeout` | — | provider_transient | 查询 provider 账单/额度接口超时（`PROVIDER_USAGE_TIMEOUT_SEC`） | |
| `usage_unreachable` | — | provider_transient | httpx 网络层错误（连接失败/DNS/TLS 等） | |
| `usage_bad_response` | — | system | 上游响应非预期格式，解析失败 | |
| `usage_blocked_url` | — | user_provider | 自定义 base_url 未过 `net_safety` 出网校验（非公网/内网地址） | |
| `usage_unsupported_provider` | — | — | provider/base_url 组合没有对应的账单查询适配器（`select_adapter` 命中 `unsupported`） | |
| `usage_http_<code>` | — | user_provider/system | 上游返回非 2xx HTTP 状态码，`<code>` 为原始状态码 | |

## 聊天

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `already_answered` | 409 | — | 静默处理，不弹窗 | ✅ |
| `content_pk_fpr_mismatch` | 409 | — | 信封封的钥 ≠ 当前注册内容钥（写手 whoami 缓存陈旧）；带 `current_public_key_fpr`/`envelope_content_pk_fpr`，写手应刷新 whoami 重封重试 | |
| `message_not_found` | 404 | — | | ✅ |
| `user_message_envelope_failed` | 409 | — | | ✅ |
| `confirmation_required` | 400 | — | 清空聊天 / 账号重置缺确认字段 | |
| `chat_clear_failed` | 500 | system | | |
| `runtime_control_unavailable` | 503 | system | 无法读取/验证 Runtime V2 control 或强制 `v2_only` policy，发送前失败关闭 | |
| `runtime_policy_not_ready` | 503 | system | 账号的 runtime control 尚未处于 `db_action_v2` + `v2`，消息不会落库或入队 | |
| `workers_unavailable` | 503 | system | 没有 live Runtime V2 turn-worker 心跳；发送前拒绝，绝不退回 resident | |
| `turns_halted` | 503 | system | 运维 kill switch 已暂停新 turn；Genesis 使用独立控制面 | |
| `busy` | 503 | system | live worker pool 的预计排队时间超过 admission SLA；`reason=queue_over_sla` | |
| `runtime_control_changed` | 503 | system | 加密消息写入前后 runtime generation/control 发生变化，事务回滚且不留下孤儿 job | |
| `client_msg_id_invalid` | 400 | — | `client_msg_id` 给了但不是合法 UUID 字符串 | |

## 记忆（memory 路由 + memory action）

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `type_invalid` | 400 | — | | |
| `type_required` | 400 | — | | |
| `anchor_self_reference` | 400 | — | 记忆不能锚定自己 | |
| `envelope_visibility_invalid` | 400 | — | | |
| `envelope_shared_requires_K_enclave` | 400 | — | | |
| `occurred_at_required` | 400 | — | | |
| `anchor_memory_ids_must_be_list` | 400 | — | | |
| `anchor_memory_ids_not_found` | 400 | — | anchor 引用了不存在的 memory id | |
| `insight_requires_anchor` | 400 | — | 仅 `/v1/memory/add` 直传路径；actions/retype 同语义已收敛为 `anchor_required` | |
| `reflection_requires_substrate` | 400 | — | | |
| `reflection_lifetime_cap` | 400 | — | 关系天数分层的反思次数上限 | |
| `reflection_too_soon` | 400 | — | 反思冷却期未到 | |
| `title_required` | 400 | — | | |
| `description_required` | 400 | — | | |
| `memory_id_required` | 400 | — | | |
| `patch_required` | 400 | — | | |
| `summary_required` | 400 | — | | |
| `supersedes_required` | 400 | — | | |
| `envelope_id_mismatch` | 400 | — | envelope.id 必须等于目标 memory_id（AEAD-bound） | |
| `action_must_be_object` | 400 | — | | |
| `actions_required` | 400 | — | | |
| `unsupported_memory_action` | 400 | — | | |
| `memory_action_failed` | — | — | `_execute_memory_actions` 的兜底默认值（正常路径下单个 action 总会带自己的 `error`，状态码随子 action） | |
| `db_write_failed` | 500 | system | | |

## 身份（identity 路由 + identity action）

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `already_initialized` | 409 | — | | |
| `days_with_user_mismatch` | 400 | — | | |
| `days_with_user_must_be_non_negative` | 400 | — | | |
| `days_with_user_required` | 400 | — | | |
| `identity_not_initialized` | 409 | — | 需要既有身份卡的 identity mutation / resident `identity.replace`；cloud plaintext 角色卡上传为 create-or-update，不返回此错误 | |
| `dimension_required` | 400 | — | | |
| `dimension_not_found` | 404 | — | | |
| `delta_required` | 400 | — | | |
| `agent_name_empty` | 400 | — | | |
| `agent_name_is_runtime_label` | 400 | — | 名字撞 card_policy.RUNTIME_LABELS（原 `agent_name_too_generic`，已统一） | |
| `user_preferred_name_empty` | 400 | — | identity.profile_patch 的本人称呼为空 | |
| `user_preferred_name_is_reserved` | 400 | — | 本人称呼是保留占位词（用户/user/TA，不区分大小写） | |
| `dimension_value_out_of_range` | 400 | — | dimension_nudge / patch dimensions / replace 三处共用（card_policy） | |
| `dimension_value_not_number` | 400 | — | 同上 | |
| `dimensions_must_be_list` | 400 | — | 同上 | |
| `too_many_dimensions` | 400 | — | 同上 | |
| `dimension_name_duplicate` | 400 | — | 同上 | |
| `dimension_name_empty` | 400 | — | dimension_nudge target 名为空 / patch dimensions 校验共用（card_policy） | |
| `self_introduction_empty` | 400 | — | | |
| `signature_must_be_list` | 400 | — | identity.profile_patch 的 list 字段校验（同族还有 boundaries/do_not_say/stable_definitions） | |
| `boundaries_must_be_list` | 400 | — | 同上 | |
| `do_not_say_must_be_list` | 400 | — | 同上 | |
| `stable_definitions_must_be_list` | 400 | — | 同上 | |
| `envelope_not_allowed` | 400 | — | identity.replace 不接受直接传 envelope | |
| `identity_base_stale` | 409 | — | identity.replace 带基线且期间发生过全量替换(P5 乐观并发) | |
| `identity_replace_requires_resident_distill_context` | 403 | — | | |
| `not_a_live_resident_distill_job` | 403 | — | | |
| `identity_required` | 400 | — | | |
| `action_must_be_object` | 400 | — | （与 memory 同名 slug，两条独立路由各自返回） | |
| `actions_required` | 400 | — | | |
| `unsupported_identity_action` | 400 | — | | |
| `nudge_delta_exceeds_cap` | 400 | — | `identity.dimension_nudge` 单次 delta 超出 card_policy 上限 | |
| `identity_action_failed` | — | — | `_execute_identity_actions` 的兜底默认值，状态码随子 action | |

## 世界书（worldbook）

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `worldbook_validate_unavailable` | 503 | system | enclave 校验回环不可达 | |
| `content_too_long` | 400 | — | 超字数上限（detail.max_chars） | |
| `worldbook_validate_failed` | 400 | — | | |
| `worldbook_match_unavailable` | 503 | system | | |

## 蒸馏 / 导入（genesis）

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `sealed_envelope_incomplete` | 400 | — | resident 蒸馏 sealed body 缺字段 | |
| `envelope_owner_mismatch` | 403 | — | | |
| `body_ct_invalid` | 400 | — | | |
| `material_too_large` | 413 | — | | |
| `consumer_id_required` | 400 | — | | |
| `json_object_required` | 400 | — | | |
| `job_not_found` | 404 | — | resident pending/heartbeat/complete 路径 | ✅ |
| `heartbeat_rejected` | 409 | — | 非 owner 或 job 已不在 processing | |
| `invalid_job_id` | 400 | — | | |
| `invalid_turn_id` | 400 | — | Runtime V2 chat activity turn id is malformed | |
| `turn_activity_not_found` | 404 | — | No authenticated V1/V2 user turn belongs to this id | |
| `invalid_activity_event` | 400 | — | V1 resident activity transition has invalid identifiers or state | |
| `activity_event_rejected` | 409 | — | V1 event conflicts with runtime ownership or an existing invocation | |
| `genesis_job_not_found` | 404 | — | plaintext job 状态/chunk 上传路径（同语义、不同 slug 名） | |
| `ciphertext_sha256_mismatch` | 400 | — | | |
| `chunk_envelope_required` | 400 | — | genesis 分片上传信封校验族（下 9 行同族，见 genesis/service.py raise 点） | |
| `chunk_envelope_body_ct_mismatch` | 400 | — | | |
| `chunk_envelope_missing_fields` | 400 | — | 实际 body 是 `chunk_envelope_missing_fields:<字段列表>`（历史写法，冒号拼接缺失字段，未按 Task 4 的 detail 分离模式收敛） | |
| `chunk_envelope_v_invalid` | 400 | — | | |
| `chunk_envelope_id_required` | 400 | — | | |
| `chunk_envelope_visibility_must_be_shared` | 400 | — | | |
| `chunk_envelope_owner_mismatch` | 400 | — | | |
| `chunk_encrypted_body_required` | 400 | — | | |
| `chunk_envelope_meta_missing` | 400 | — | | |
| `chunk_seq_out_of_range` | 400 | — | | |
| `empty_chunk` | 400 | — | | |
| `invalid_byte_range` | 400 | — | | |
| `chunk_hash_conflict` | 409 | — | genesis_core.py 有此分支但当前无 raise 点命中（防御性，未实证可达） | |
| `total_chunks_total_bytes_must_be_int` | 400 | — | | |
| `total_chunks_out_of_range` | 400 | — | | |
| `total_bytes_out_of_range` | 400 | — | | |
| `reducer_output_required` | 400 | — | | |
| `genesis_memory_summary_required` | 400 | — | | |
| `raw_reducer_field_not_allowed` | 400 | — | 实际 body 是 `raw_reducer_field_not_allowed:<field>` | |
| `identity_unavailable` | 409 | — | persona_backfill：身份未就绪 | |
| `persona_backfill_failed` | 500 | system | 实际 body 是 `persona_backfill_failed:<ExcType>:<msg>` | |
| `material_empty` | 400 | — | 上传素材为空/提取不到可用文本（detail 说明） | |
| `redistill_job_active` | 409 | — | 已有 redistill job 进行中（带 `active_job_id`） | |

## 导入 / 归档（history_import / onboarding_archive / diagnostics / copytext）

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `missing_file` | 400 | — | | ✅ |
| `empty_file` | 400 | — | | ✅ |
| `payload_too_large` | 413 | — | | ✅ |
| `archive_failed` | 502 | system | R2 归档写入失败 | ✅ |
| `archive_unavailable` | 503 | system | | ✅ |

## Proactive / 引导（bootstrap gates）

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `bootstrap_incomplete` | 409 | — | chat_response 前置引导未完成（detail.stage） | |
| `empty_status_patch` | 400 | — | | |
| `consumer_mismatch` | 409 | — | | |
| `decision_id_required` | 400 | — | | |
| `decision_not_found` | 404 | — | | |
| `invalid_label` | 400 | — | | |

## 感知（perception / agent perception）

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `content_envelope_required` | 400 | — | | |
| `unknown_kind` | 400 | — | | |
| `invalid_items` | 400 | — | | |
| `app_required` | 400 | — | | |
| `unknown_signals` | 400 | — | agent 感知信号名不识别 | |
| `unknown_or_unhistorized_signal` | 400 | — | | |
| `invalid_days` | 400 | — | `days` 查询参数非数字 | |

## 内容 / 导出 / 账号重置（content_core）

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `public_key_rotation_requires_rewrap` | 409 | — | 已有加密内容时换公钥必须先走 rewrap | |
| `export_too_large` | 413 | — | 一次性导出超 80MiB 预算 | |
| `archive_cleanup_failed` | 503 | system | 账号重置：R2 归档清理失败，reset 中止（可安全重试） | |
| `confirmation_mismatch` | 400 | — | admin 删除用户端点专用：`confirm` 字段 ≠ `user_id` | |

## 通知中继（notify_relay）

| slug | 状态码 | blame | 说明 | 需本地化 |
|---|---|---|---|---|
| `device_already_enrolled` | 409 | — | 同一设备已在中继注册过（enroll 幂等冲突） | |

---

## 已知问题（登记备查，非本次任务修复范围）

> 以下为抽样登记，非穷举。

- `accounts/accounts_core.py` 多处用自由文本 `"user not found"`（含空格，非
  合法 slug），与 admin 侧的 `user_not_found` 不一致——历史遗留，本表不为它
  单独开行（不是稳定 slug）。
- `identity/identity_core.py` 的 `/v1/identity/init` `/replace` 自身的信封
  缺字段校验仍是自由文本 `f"envelope missing fields: {missing}"`，未纳入
  Task 4 收敛范围（本次收敛了 3 个文件共 6 处：chat_core.py×3、
  memory_core.py×2、actions.py×1）。
- `worldbook/worldbook_core.py::_request_envelope` / `_validate_envelope`
  同样是自由文本消息，未收敛。
- `screen/screen_read_core.py`（`/v1/screen/*` 的实际 HTTP 路由层）全部错误
  也是自由文本（`"not found"` / `"bad filename"` 等），未收敛，故本表未列出
  对应 slug 行。

---

## enclave surface（backend↔enclave 内网面，iOS 不直连）

以下 slug 只出现在 `backend/enclave/`——iOS 从不直接访问 enclave 的
HTTP 面，这条通道是 backend 进程内对 enclave 的回环调用；backend 侧遇到
enclave 报错通常会重新包一层自己的 slug（如 `model_api_key_decrypt_failed`、
`worldbook_validate_unavailable`）再对外返回。列在这里仅作排障备查，不进 iOS
本地化表。

| slug | 状态码 | blame | 说明 |
|---|---|---|---|
| `unauthorized` | 401 | — | whoami 缓存过、key 已被吊销 |
| `not_ready` | 503 | system | enclave 尚未完成初始化 |
| `missing_api_key` | 401 | — | envelope 路由鉴权前置检查缺 api_key |
| `cannot_resolve_user_id` | 401 | — | |
| `screen_caption_unconfigured` | 503 | — | |
| `backend_error` | 502 | system | `_errors.py::backend_call_or_error` 兜底；实际 body 是 `backend_error: <httpx 异常文本>`（历史写法，非规范 slug+detail 分离） |
| `key_derivation_unavailable` | 503 | system | `_errors.py::content_sk_or_503` 兜底；实际 body 是 `key_derivation_unavailable: <异常文本>`，同上历史写法 |

---

## 通知中心 error_class（`GET /v1/notices`，非 HTTP slug）

> 本节与上面所有表格是**两套完全不同的命名空间**——上面的 slug 是 HTTP
> `{"error": "<slug>"}` 顶层字段值；这里列的是 `GET /v1/notices` 返回条目里
> `error_class` 字段的取值，对应 `backend/notices/catalog.py` 的
> `_CATALOG`，只用于通知中心展示话术，从不出现在 HTTP 错误响应体里。
> `blame` 语义同 `docs/FRONTEND_ERROR_CONTRACT.md` §二分类；`severity`
> 取值 `error`/`warning`，决定通知中心 UI 展示优先级（`warning` 语气弱化，
> 不打扰用户）。「状态码」列在本节恒为 `—`（notice 不走 HTTP 状态码，此列
> 仅为复用上面表格的行格式/守卫测试）。
>
> `quota_insufficient` / `auth_invalid` / `model_not_found` /
> `rate_limited` / `upstream_unavailable` / `turn_timeout` /
> `reply_parse_failed` / `unknown` 8 类是 Phase B/B3 既有的 chat 上游类
> （`tools/chat_resident_consumer.py` `_ERROR_CLASS_RULES` 同源），未在本次
> 新增，不重复列。下表保留 Phase C 后加入的目录项；其中 `runner_*` 三项只是
> 已冻结的历史 resident/supervisor 通知兼容值，Runtime V2 不再产生它们。

| error_class | 状态码 | blame | severity | 触发场景 |
|---|---|---|---|---|
| `provider_incompatible` | — | user_provider | error | chat：Runtime V2 provider/tool loop 把上游「不支持某参数/工具」类错误分类上报（`classify_upstream`/`_ERROR_CLASS_RULES` 命中） |
| `context_overflow` | — | user_provider | error | chat：这轮对话超出模型上下文窗口 |
| `content_filtered` | — | provider_transient | error | chat：回复被上游内容策略拦截 |
| `genesis_failed` | — | system | error | genesis：蒸馏 job 整体失败（`service.mark_failed`；先过 `classify_upstream` 分类，未命中时兜底到本类） |
| `genesis_partial` | — | system | warning | genesis：蒸馏跑完但有记忆卡片被丢弃（`apply_reducer_output` / `plaintext.py` 直传路径统计 dropped>0） |
| `import_failed` | — | system | error | history_import：聊天记录导入失败 |
| `import_stale` | — | system | error | history_import：导入 job 卡在 queued/processing 超过阈值，判定超时失败 |
| `memory_backoff` | — | system | warning | memory：capture/migrate/dream 三条 lane 之一连续失败 streak ≥ 3（`_BACKOFF_NOTICE_STREAK`），已进自动退避 |
| `runner_spawn_failed` | — | system | error | **历史兼容，不再产生**：旧 supervisor 拉起 per-user 子进程失败 |
| `runner_key_decrypt_failed` | — | system | error | **历史兼容，不再产生**：旧 supervisor 为 per-user 子进程解密 provider key 失败 |
| `runner_degraded` | — | system | warning | **历史兼容，不再产生**：旧 resident 子进程 runtime-token 刷新失败 |
| `resident_consumer_stale` | — | user_environment | warning | chat：自托管 resident consumer 仍在 poll，但 commit 缺失/不匹配，或 resident-only 蒸馏任务迟迟没被 claim；notice 可带 `copyable_prompt` |
| `resident_decrypt_source_unavailable` | — | user_environment | warning | chat：resident 明确报告 `degraded`/`unconfigured`/`unreachable`；notice 可立即出现，维护消息仅对新用户立即注入，老用户需持续失败超过宽限期 |
| `resident_decrypt_health_unreported` | — | user_environment | warning | chat：resident 未上报有效、近期的 decrypt-health；仅发 notice，不生成解密修复文案或聊天维护消息 |
| `resident_never_claimed` | — | user_environment | error | genesis：resident-only 入住/记忆蒸馏 job 超过 reaper 阈值仍无人 claim，已失败 |
