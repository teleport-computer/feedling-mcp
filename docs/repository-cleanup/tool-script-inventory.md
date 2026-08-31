---
document_lifecycle: current
canonical_owner: self
---
# 工具、脚本与运维表面清单

审计日期：2026-08-31。本清单覆盖本次 `git ls-files tools scripts ops` 的全部
127 个 tracked 路径：`tools/` 101 个、`scripts/` 24 个、`ops/` 2 个。其中 123 个是
Task 5 taxonomy 的 executable/support asset；四个 tracked README
(`scripts/loadtest/README.md`、`tools/README.md`、`tools/e2e/README.md`、
`tools/provider_smoke/README.md`) 在文末另列为文档覆盖，**不计入**九类
tool/script taxonomy。清单记录当前 owner、分类、生命周期和**精确**消费者证据；不是
删除清单。静态 Python import 缺失不能证明一个 operator CLI、部署命令或远程恢复入口
已失效。

`retain` 表示保留到另一次有范围的功能/运维决策；`retain-protected` 表示不能因
本清单或零引用搜索删除；`historical-retained` 表示保留历史/恢复上下文但不作为当前
运行指令。除下列 continuity canary 外，本批没有形成新的 strong candidate。

## 证据索引

| ID | 现行消费者或可执行入口 |
|---|---|
| E1 | `deploy/docker-compose.phala.yaml` 与 `deploy/docker-compose.phala.test.yaml` 的 `cpu-recorder` command 都是 `python -u ops/cpu_recorder.py`；`tests/test_cpu_recorder_compose.py` 锁住该 command。 |
| E2 | [`docs/AGENT_MAILBOX.md`](../AGENT_MAILBOX.md) 逐一给出 `setup.sh`、`post.sh`、`read.sh`、`ack.sh` 的 operator command。 |
| E3 | [`candidates/README.md`](candidates/README.md) 将 resident-route audit 明确列为仍承担 remediation 的表面。 |
| E4 | `.github/workflows/branch-flow.yml` 执行 `scripts/check-pr-branch-flow.sh`；该 workflow 是 `main` PR branch-flow 门禁。 |
| E5 | `tests/test_perception_prompt_golden.py` 从基线 checkout 运行/校验该导出器的 golden fixture。 |
| E6 | [`scripts/loadtest/README.md`](../../scripts/loadtest/README.md) 规定手动 simulated run 和 token comparison；`tests/test_loadtest_{collect,compare,harness_smoke,mock_provider}.py` import 该 package。 |
| E7 | `tests/test_admin_usage.py` 运行 `scripts/perf/admin_usage_scale.py`；[`docs/superpowers/evidence/2026-08-02-admin-usage-scale.md`](../superpowers/evidence/2026-08-02-admin-usage-scale.md) 记录 operator command。 |
| E8 | `tests/test_provider_probe_smoke.py` import `scripts.provider_probe.probe`；[`candidates/provider-smoke-harness.md`](candidates/provider-smoke-harness.md) 明确保留原始 provider tool-call wire probe。 |
| E9 | `tests/test_repair_v2_bricked_frontier.py` 从精确脚本路径加载 repair guard。 |
| E10 | [`docs/CHANGELOG.md`](../CHANGELOG.md) 记录 `scripts/tee/decrypt_probe.py --dsn ...` 的部署核验命令。 |
| E11 | `backend/alembic_tee/versions/0004_full_table_alignment.py` 和 `tests/test_tee_schema.py` 明确将 DDL 派生归于 `scripts/tee/derive_tee_ddl.py`。 |
| E12 | `.github/workflows/tee-replicate.yml` 执行 `scripts/tee/replication_workflow_guard.py build/check`。 |
| E13 | [`docs/TRACE_T138_BLOCK0.md`](../TRACE_T138_BLOCK0.md) 给出 `scripts/trace-write-rate-report.py --days 7`；`tests/test_trace_write_stats.py` 锁住其底层 measurement。 |
| E14 | [`README.md`](../../README.md) 和 [`deploy/DEPLOYMENTS.md`](../../deploy/DEPLOYMENTS.md) 给出 `tools/audit_live_cvm.py` 的 audit command/证据。 |
| E15 | `.github/workflows/backfill-v2-profiles.yml` 是 `tools/backfill_v2_profiles.py` 的 manual operator workflow。 |
| E16 | `deploy/feedling-chat-resident.service` 的 `ExecStart` 和 `backend/agent_runtime/spawners.py` 的 hosted runner argv 都以精确固定路径执行 resident consumer；`deploy/Dockerfile.agent-runner` 以 `COPY tools/ ./tools/` 将整个工具目录随 image 交付。[`tools/README.md`](../../tools/README.md) 是 operator entrypoint，`tests/test_chat_resident_self_update.py` 锁住 consumer 的 checkout 更新文件集。 |
| E17 | [`deploy/SELF_HOSTING.md`](../../deploy/SELF_HOSTING.md) 和根 `README.md` 都给出 `python tools/check_chat_pipeline.py` 诊断命令。 |
| E18 | `.github/workflows/ci.yml` 执行 `tools/check_document_lifecycle.py` 及 `tools/ci_execution_evidence.py attest/report`；`tests/test_document_lifecycle.py` 锁住 lifecycle generator front matter。 |
| E19 | [`docs/testing/TESTING.md`](../testing/TESTING.md) 说明 `.github/workflows/continuity-canary.yml` 已于 2026-08-31 禁用，并链接其 archive/恢复条件。 |
| E20 | `tools/audit_live_cvm.py` 将 `tools/dcap/` 加入 import path，直接 import `dcap_parse.parse_quote` 并在 live attestation audit 中调用；[`tools/README.md`](../../tools/README.md) 也明确 DCAP parser 由该 audit CLI 使用。`.github/workflows/ci.yml` 另行执行 `cd tools/dcap && python3 -m unittest test_dcap_parse.py -v` 覆盖 parser test/fixtures。 |
| E21 | `.github/workflows/ci.yml` 在三条 deploy lane 执行 `tools/deploy_canary.py`；`tests/test_deploy_canary.py` 是其 review guard。 |
| E22 | [`tools/e2e/README.md`](../../tools/e2e/README.md) 是 P0 operator command、专项 probe 和 fixture 的现行入口，并登记 `garden_language_probe.py` 的适用变更；该 probe 自身 docstring 给出真实环境运行命令。`tests/test_e2e_*.py`、`tests/test_aup_gate_probe.py`、`tests/test_garden_language_flip.py`、`tests/test_tool_selection_eval.py` 和 `tests/test_user_mcp_handshake_probe.py` 覆盖相应 package/probe。 |
| E23 | [`docs/testing/TESTING.md`](../testing/TESTING.md) L2 表将 `tools/e2e_encryption_test.py` 和两种 envelope round-trip CLI 列为加密/账号/enclave 链路验证；`backend/enclave_app.py` 也明确该 E2E 脚本会拉起 enclave app。 |
| E24 | `.github/workflows/docs-site.yml` 执行 `tools/export_public_openapi.py`，并以 `tools/public_openapi_contracts.py` 作为触发路径。 |
| E25 | `tests/test_genesis_distill_acceptance.py` import `genesis_e2e` 并检查其 acceptance report。 |
| E26 | `tests/test_memory_readside_sandbox.py` 执行 sandbox；`tests/test_memory_readside_docker_e2e.py` 是 docker E2E guard，docker harness import sandbox/smoke helpers。 |
| E27 | `backend/agent_runtime/spawners.py` 与 `tools/chat_resident_consumer.py` 使用 `tools/io_cli.py`/`tools/io_cli_catalog.py`；`tests/test_io_cli_catalog.py`、`tests/test_agent_runtime_spawners.py` 锁住 catalog parity。 |
| E28 | `backend/agent_runtime/spawners.py` 的 Pi argv 指向 `tools/pi_mcp_bridge/index.js`；`tests/test_user_mcp_consumer.py` 将三个 bridge 文件纳入 resident 自更新文件集，`tests/test_pi_mcp_bridge.py` 覆盖 bridge。 |
| E29 | `tests/test_proactive_gate_eval.py` 以精确路径加载 evaluator，`.github/workflows/ci.yml` 执行该 test。 |
| E30 | `tests/test_prompt_cache_canary.py` import canary，`.github/workflows/ci.yml` 将脚本作为 deploy 相关变更触发路径。 |
| E31 | [`tools/provider_smoke/README.md`](../../tools/provider_smoke/README.md) 给出 test-CVM multi-provider operator command；[`candidates/provider-smoke-harness.md`](candidates/provider-smoke-harness.md) 的结论是 `feature-decision`，而非删除。 |
| E32 | `.github/workflows/recover-dream-churn.yml` 执行 `tools/recover_memory_dream_churn.py`。 |
| E33 | `backend/accounts/registry.py` 明确把 survivor selection 归于 `tools/recover_orphan_accounts.py`；[`docs/runbooks/lost-account-credentials-recovery.md`](../runbooks/lost-account-credentials-recovery.md) 和 `tests/test_recover_orphan_survivor.py` 分别保留 operator/recovery guard。 |
| E34 | [`README.md`](README.md) 将 `tools/repository_inventory.py` 作为确定性 tracked 分类器；`tests/test_repository_inventory.py` 覆盖分类。 |
| E35 | `backend/copytext/service.py` 明确说明 `tools/seed_copytext.py` 通过 admin HTTP endpoint 使用该服务。 |
| E36 | [`candidates/README.md`](candidates/README.md) 明确 `tools/seed_legacy_memory.py` 是兼容 fixture，仍需保留。 |
| E37 | `tests/test_store_load_contract.py` import inventory，并在 snapshot 变化时给出精确 `--write` command。 |
| E38 | `tools/chat_resident_consumer.py` 在 VPS 运行时 lazy-import 两个 user-MCP helper；`tests/test_user_mcp_consumer.py` 将二者纳入 self-update files，另有各自的 helper unit tests。 |
| E39 | [`candidates/v2-user-triage-semantic-compaction.md`](candidates/v2-user-triage-semantic-compaction.md) 记录已实施的局部诊断清理，并保留现行只读 CLI。 |
| E40 | [`deploy/DEPLOYMENTS.md`](../../deploy/DEPLOYMENTS.md) 三个部署段给出 `tools/verify_enclave_domain.py` command，并限定其 evidence-binding 作用。 |
| E41 | `tests/test_deploy_yaml_strict.py`、`tests/test_enclave_domain_compose.py`、`tests/test_frame_r2_deploy_config.py` 和 `tests/test_cpu_recorder_compose.py` 都 import `tools.strict_yaml.load_yaml_strict`。 |
| E42 | `backend/asgi_app.py` 指定以 `tools/gen_url_map.py` snapshot/diff live route surface；`tests/test_gen_url_map.py` 以该精确路径加载并对照 ASGI app。 |

## 分类与归属

下表覆盖全部 123 个 Task 5 taxonomy asset。只有同一 owner、生命周期和证据类的路径
才会合并；fixture/package marker 也计入，避免把可执行工具的支持文件遗漏在审计外。

| 路径（数量） | owner | 分类 | 生命周期 | 证据 |
|---|---|---|---|---|
| `ops/__init__.py`<br>`ops/cpu_recorder.py`（2） | Deployment / observability | deployment | retain | E1 |
| `scripts/agent-mailbox/ack.sh`<br>`scripts/agent-mailbox/post.sh`<br>`scripts/agent-mailbox/read.sh`<br>`scripts/agent-mailbox/setup.sh`（4） | Engineering workflow | active diagnostic | retain | E2 |
| `scripts/audit_resident_model_routes.py`（1） | Hosted Resident operations | recovery | retain | E3 |
| `scripts/check-pr-branch-flow.sh`（1） | Release governance | deployment | retain | E4 |
| `scripts/dump_perception_baseline.py`（1） | Perception contracts | test support | retain | E5 |
| `scripts/loadtest/__init__.py`<br>`scripts/loadtest/collect.py`<br>`scripts/loadtest/compare_tokens.py`<br>`scripts/loadtest/fixtures.py`<br>`scripts/loadtest/measure_resident.py`<br>`scripts/loadtest/mock_provider.py`<br>`scripts/loadtest/run_loadtest.py`（7） | Runtime V2 validation | test support | retain | E6 |
| `scripts/perf/__init__.py`<br>`scripts/perf/admin_usage_scale.py`（2） | Admin/usage operations | active diagnostic | retain | E7 |
| `scripts/provider_probe/__init__.py`<br>`scripts/provider_probe/probe.py`（2） | Provider compatibility | active diagnostic | retain | E8 |
| `scripts/repair_v2_bricked_summary_frontier.py`（1） | Runtime V2 recovery | recovery | retain | E9 |
| `scripts/tee/decrypt_probe.py`（1） | TEE migration validation | migration | retain | E10 |
| `scripts/tee/derive_tee_ddl.py`（1） | TEE schema migration | migration | retain | E11 |
| `scripts/tee/replication_workflow_guard.py`（1） | TEE replication deployment | deployment | retain | E12 |
| `scripts/trace-write-rate-report.py`（1） | Trace capacity operations | active diagnostic | retain | E13 |
| `tools/audit_live_cvm.py`（1） | Deployment audit | active diagnostic | retain | E14 |
| `tools/backfill_v2_profiles.py`（1） | Hosted Runtime V2 deployment | deployment | retain | E15 |
| `tools/chat_resident_consumer.py`<br>`tools/chat_resident_requirements.txt`（2） | Resident runtime | production companion | **retain-protected** | E16 |
| `tools/check_chat_pipeline.py`（1） | Self-hosting support | active diagnostic | retain | E17 |
| `tools/check_document_lifecycle.py`（1） | Documentation lifecycle | generated helper | retain | E18 |
| `tools/ci_execution_evidence.py`（1） | CI evidence | test support | retain | E18 |
| `tools/continuity_canary.py`（1） | Encryption continuity history | historical | historical-retained | E19 |
| `tools/dcap/dcap_parse.py`（1） | Attestation validation | active diagnostic | retain | E20 |
| `tools/dcap/test_dcap_parse.py`<br>`tools/dcap/testdata/sample_attestation.json`<br>`tools/dcap/testdata/sample_quote.hex`（3） | Attestation validation | test support | retain | E20 |
| `tools/deploy_canary.py`（1） | Deployment safety | deployment | retain | E21 |
| `tools/e2e/__init__.py`<br>`tools/e2e/aup_gate_probe.py`<br>`tools/e2e/card_gate_probe.py`<br>`tools/e2e/client.py`<br>`tools/e2e/config.py`<br>`tools/e2e/continuity_probe.py`<br>`tools/e2e/deep.py`<br>`tools/e2e/elevenlabs_silent_turn_probe.py`<br>`tools/e2e/experience_probe.py`<br>`tools/e2e/fixtures/aup_gate/canary_instruction_v0.2.0.txt`<br>`tools/e2e/fixtures/aup_gate/manifest.json`<br>`tools/e2e/fixtures/aup_gate/user_message.txt`<br>`tools/e2e/garden_language_probe.py`<br>`tools/e2e/genesis_expired_stage_probe.py`<br>`tools/e2e/genesis_resume_probe.py`<br>`tools/e2e/hosted.py`<br>`tools/e2e/idempotency_probe.py`<br>`tools/e2e/image_autonomy_probe.py`<br>`tools/e2e/memory_probe.py`<br>`tools/e2e/memory_thinking_leak_probe.py`<br>`tools/e2e/p0.py`<br>`tools/e2e/perception_probe.py`<br>`tools/e2e/perception_wake_probe.py`<br>`tools/e2e/proactive_probe.py`<br>`tools/e2e/probe_common.py`<br>`tools/e2e/processing_probe.py`<br>`tools/e2e/provider_response_envelope_probe.py`<br>`tools/e2e/repeat_wake_probe.py`<br>`tools/e2e/resident_maintenance_smoke.py`<br>`tools/e2e/screen_watch_probe.py`<br>`tools/e2e/self_thinking_prompt_probe.py`<br>`tools/e2e/switch_matrix_probe.py`<br>`tools/e2e/temporal_probe.py`<br>`tools/e2e/tool_count_ceiling_probe.py`<br>`tools/e2e/tool_schema_rejection_probe.py`<br>`tools/e2e/tool_selection_cases.json`<br>`tools/e2e/tool_selection_eval.py`<br>`tools/e2e/turn_failure_smoke.py`<br>`tools/e2e/unlock.py`<br>`tools/e2e/user_mcp_handshake_probe.py`<br>`tools/e2e/voice_transcript_probe.py`<br>`tools/e2e/vps.py`<br>`tools/e2e/wake_tool_markup_probe.py`<br>`tools/e2e/wake_write_gate_probe.py`<br>`tools/e2e/worldbook_probe.py`（45） | Release validation | test support | retain | E22 |
| `tools/e2e_encryption_test.py`（1） | Encryption integration validation | test support | retain | E23 |
| `tools/export_public_openapi.py`<br>`tools/public_openapi_contracts.py`（2） | Public API documentation | generated helper | retain | E24 |
| `tools/frame_envelope_roundtrip_test.py`<br>`tools/v1_envelope_roundtrip_test.py`（2） | Envelope integration validation | test support | retain | E23 |
| `tools/gen_url_map.py`（1） | ASGI route accounting | active diagnostic | retain | E42 |
| `tools/genesis_e2e.py`（1） | Genesis acceptance validation | test support | retain | E25 |
| `tools/io_cli.py`<br>`tools/io_cli_catalog.py`（2） | Resident runtime | production companion | **retain-protected** | E27 |
| `tools/memory_readside_docker_e2e.py`<br>`tools/memory_readside_sandbox.py`<br>`tools/memory_readside_smoke.py`（3） | Memory readside validation | test support | retain | E26 |
| `tools/pi_mcp_bridge/index.js`<br>`tools/pi_mcp_bridge/mcp_client.js`<br>`tools/pi_mcp_bridge/tool_mapping.js`（3） | Resident user-MCP runtime | production companion | **retain-protected** | E28 |
| `tools/proactive_gate_eval.py`（1） | Proactive policy review | active diagnostic | retain | E29 |
| `tools/prompt_cache_canary.py`（1） | Runtime V2 deploy validation | active diagnostic | retain | E30 |
| `tools/provider_smoke/__init__.py`<br>`tools/provider_smoke/assertions.py`<br>`tools/provider_smoke/client.py`<br>`tools/provider_smoke/crypto.py`<br>`tools/provider_smoke/matrix.py`<br>`tools/provider_smoke/run_smoke.py`<br>`tools/provider_smoke/tests/__init__.py`<br>`tools/provider_smoke/tests/test_assertions.py`<br>`tools/provider_smoke/tests/test_client_helpers.py`<br>`tools/provider_smoke/tests/test_crypto.py`<br>`tools/provider_smoke/tests/test_matrix.py`<br>`tools/provider_smoke/tests/test_run_smoke_helpers.py`（12） | Provider compatibility | active diagnostic | retain (`feature-decision`) | E31 |
| `tools/recover_memory_dream_churn.py`（1） | Memory recovery | recovery | retain | E32 |
| `tools/recover_orphan_accounts.py`（1） | Account recovery | recovery | retain | E33 |
| `tools/repository_inventory.py`（1） | Repository cleanup | generated helper | retain | E34 |
| `tools/seed_copytext.py`（1） | Product copy operations | active diagnostic | retain | E35 |
| `tools/seed_legacy_memory.py`（1） | Memory compatibility | test support | retain | E36 |
| `tools/store_shell_only_inventory.py`（1） | Store-load contract | generated helper | retain | E37 |
| `tools/strict_yaml.py`（1） | Deployment configuration validation | test support | retain | E41 |
| `tools/user_mcp_ca_fetch.py`<br>`tools/user_mcp_materialize.py`（2） | Resident user-MCP runtime | production companion | **retain-protected** | E38 |
| `tools/v2_user_triage.py`（1） | Runtime V2 operations | active diagnostic | retain | E39 |
| `tools/verify_enclave_domain.py`（1） | Attestation deployment | deployment | retain | E40 |

## 非 executable 文档覆盖（不属于 Task 5 taxonomy）

| 路径（数量） | owner | 生命周期 | 原因 |
|---|---|---|---|
| `scripts/loadtest/README.md`（1） | Runtime V2 validation documentation | current | 它是 simulated load/token harness 的 operator/runbook entrypoint，不是 tool/script asset；E6 仍是其现行使用证据。 |
| `tools/README.md`（1） | Tooling documentation | current | 它是本目录的 operator/documentation entrypoint，不是 tool/script asset；为保证全部 tracked-path 可导航而单列。 |
| `tools/e2e/README.md`（1） | Release validation documentation | current | 它是 P0/operator probe runbook，不是 tool/script asset；E22 仍是其现行使用证据。 |
| `tools/provider_smoke/README.md`（1） | Provider compatibility documentation | current | 它是 smoke operator runbook，不是 tool/script asset；E31 仍是其现行使用证据。 |

## 覆盖计数与结论

| 分类 | 路径数 |
|---|---:|
| production companion | 9 |
| deployment | 7 |
| recovery | 4 |
| migration | 2 |
| active diagnostic | 29 |
| test support | 66 |
| generated helper | 5 |
| historical | 1 |
| **Task 5 taxonomy assets** | **123** |
| 非 executable 文档覆盖 | 4 |
| **全部 tracked 路径** | **127** |

- 未归类：0；`unowned candidate`：0；本批不创建 candidate record。
- `tools/chat_resident_consumer.py` 是用户 VPS 单文件分发、自更新/re-exec、systemd
  和 hosted agent-runner 共用的受保护边界；不得修改、拆分或以 import 搜索为由删除。
- 已合并的候选/删除历史保留在 [`candidates/`](candidates/)；它们是本清单的背景证据，
  不是授权重做删除的理由。
