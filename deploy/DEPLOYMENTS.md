# Feedling deployment records

Canonical record of deployed artifacts. Entries accumulate as we move through
the phases. Historical operational identifiers may be redacted after
retirement when keeping the exact value no longer helps verification.

## 🚨 看到 "decrypt failed" 先跑这四步（triage runbook）

> Background: 2026-07 的假警报——一个 prod 用户全部 iOS 历史显示
> `[encrypted — decrypt failed]`，工程师误判为「enclave KMS 钥变了、prod 数据不可解」。
> **真因是那台设备的 Keychain 丢了 X25519 `content_sk`**（`K_user` 层，客户端侧），
> enclave 钥从未改变。下面四步专门防止再次跳到「KMS」结论。

**1. 一个用户，还是所有用户？** enclave/KMS 层的钥变会**在同一瞬间**打死所有用户。
   只有一个用户报 ⇒ 在证明相反之前，一律按**设备/账号侧**（`K_user`）处理，别碰 enclave。
   （2026-07 是 136 个聊天用户里仅 1 个受影响 → 明显是客户端。）

**2. 哪一层的 key？** `K_user`（设备 Keychain）vs `K_enclave`（enclave 内容钥）。
   证明 enclave 侧健康——现役 register→seal→enclave 解密往返：
   ```bash
   curl -sk https://9798850e096d770293c67305c6cfdceed68c1d28-5003s.dstack-pha-prod9.phala.network/attestation \
     | python3 -c 'import sys,json;print("live enclave_content_pk:",json.load(sys.stdin)["enclave_content_pk_hex"])'
   # 期望 = 2d642ec1f54719d8c6088e8cbaf394961cb804a533bd4d7366d48d1d543f5620（现役基线）
   ```
   等 §3 部署 canary 落地后，直接看它的绿灯即可（green ⇒ enclave 正常 ⇒ 客户端问题）。
   另一条快证据：受影响用户**自己的托管 agent**当天若还能用 `K_enclave` 解出历史，enclave 就没坏。

**3. 老数据还解得开吗？** §2 的 register→seal 只证明**新写入**能往返；本次事故的真正问题是
   「enclave 还能不能打开**旧** envelope」。等 §4 的 day-0 连续性 canary 落地后看它的绿灯；
   在此之前，手动跑一遍旧数据解密扫描（脚本 `tools/incident_unwrap_sweep.py` 为 §4 待补）。
   老数据仍能解 ⇒ 不是钥事件。

**4. 只有当「全员受影响」且上面 canary 全红时**，才比对 `/attestation`：
   - 比对**基线 repo var**（`ENCLAVE_CONTENT_PK_BASELINE`，见 §2）或本文件顶部
     Production CVM 表里的 `2d642ec1…`——**绝不要**拿退役部署表里的数字
     （尤其 `f50c90f7…`，那是死掉的 prod5 app `051a174f` 的钥）。
   - 跑 §5 的 `enclave_pk_fpr` SQL 找钥变日期。envelope 上 `sha256(pk)[:16]` 自 4 月起
     恒为 `50f9a01800d4a230de85507d25b86eb1`——一旦某月这个值变了，才是真的换了钥。
   - **只有到这一步，才联系 Phala。**

> ⚠️ runner CVM（`0cf2da16…` / 老记录里的 `87305c…` 等）有自己独立的 dstack app 与钥，
> **按设计从不持有内容钥**（它们通过 `FEEDLING_ENCLAVE_URL` 调主 enclave）。
> 它们的钥和主 enclave 不匹配是正常的，**不构成主 enclave 钥变的证据**。

> 用户可读版镜像到 io-onboarding `troubleshooting.md`（公共仓，另行 push）。

## Live services

> **Hosted runtime topology override — 2026-07-18.** Managed hosted execution is
> now Runtime V2-only in local, test, pre, and production source. Runner
> manifests contain only the pooled `serve-worker`; CI deploys the test, pre,
> and production runner CVMs whenever their hosted source changes. The
> `feedling-agent-runner` package name is historical—the image no longer contains
> a resident supervisor, agent CLIs, per-user homes/leases, or resident volumes.
> `FEEDLING_HOST_ALL`, `AGENT_RUNTIME_*`, per-user hosted flips, and
> `resident_only` rollback are retired. Recover by halting/scaling/rolling a
> database-compatible V2 worker image as documented in
> [`HOSTED_RUNTIME_V2_ROLLOUT.md`](HOSTED_RUNTIME_V2_ROLLOUT.md), never by
> relaunching resident. Sections explicitly labelled **retired** or
> **historical** are incident evidence only and must not be used as deployment
> instructions; the current CVM tables and V2 rollout runbook are authoritative.

> **Source is not live state.** The V2-only manifests and retirement guards can
> be complete in Git while a live CVM still runs an older image. Production also
> requires two independent runner IDs; the repository currently records one, so
> its hard topology gate must remain closed until runner two is provisioned.
> Declare fleet-wide retirement complete only after the reviewed images are
> deployed to every environment and a live process inventory shows no hosted
> resident supervisor or per-user CLI process.

> **⚠️ Superseded 2026-07-21/22 by dual-runtime coexistence (Task 11, see the
> section immediately below).** The "Runtime V2-only" banner above and the
> per-environment "Runtime V2 worker CVM" tables that follow describe a
> topology this repo no longer ships: prod and pre main CVMs now default
> `FEEDLING_HOSTED_RUNTIME_POLICY=dual` (not `v2_only`), the prod and pre
> runner CVMs are V1 `agent-runner` again, and pooled Runtime V2 also runs
> in-CVM on every main compose as a second `serve-worker` container. The
> tables are left as historical/incident record (CVM IDs, on-chain contracts,
> and most other facts in them are still accurate) — only their "Compose
> contains only serve-worker" / "V2-only" framing is stale. Treat the
> dual-runtime section immediately below as authoritative for current
> topology.

### 双运行时拓扑（dual-runtime coexistence，2026-07-21 起）

设计文档：
[`docs/superpowers/specs/2026-07-21-dual-runtime-v1-v2-coexistence-design.md`](../docs/superpowers/specs/2026-07-21-dual-runtime-v1-v2-coexistence-design.md)。
本节只记录**部署拓扑**（compose 文件形态 + CI 部署顺序），路由/切换控制器等
代码层设计见该 spec。

**拓扑图**（prod / test / pre 三环境同构，仅 CVM 数量与域名不同）：

```
┌─────────────────────────── 主 CVM（单个 dstack app）───────────────────────────┐
│                                                                                  │
│   ingress ──► backend ──► enclave        backend/enclave 走法与此前完全一致       │
│      │           │            ▲                                                 │
│      │           │            │ https://enclave:5003（compose 内网）            │
│      │           │            │                                                 │
│      │           └──────► serve-worker ──┘   同 backend 镜像，第二个容器；        │
│      │                        │               只服务 allowlist 里的 v2/draining  │
│      │                        │               账号（backend/hosted 路由/reconciler │
│      │                        ▼               决定谁去哪，见 spec §3/§4）         │
│      │                   PostgreSQL（backend 与 serve-worker 共享同一个 DB）      │
│      ▼                                                                          │
│  公网 api.<env>.feedling.app                                                    │
└───────────────────────────────────────────────────────────────────────────────┘
                                    ▲
                                    │  FEEDLING_API_URL / FEEDLING_ENCLAVE_URL
                                    │  （公网 / gateway passthrough，不是内网地址）
                                    │
┌────────────── runner CVM（独立 dstack app，prod/test/pre 各一份）───────────────┐
│                                                                                  │
│   agent-runner（V1 supervisor.py）── 走 Postgres lease，服务 allowlist 之外       │
│                                       （fence=resident）的所有账号                │
│                                                                                  │
└───────────────────────────────────────────────────────────────────────────────┘
```

test 与 prod/pre 同构：主 CVM 是 `dual`（backend + serve-worker），独立的
runner CVM（`deploy/docker-compose.phala.runner.yaml`，`feedling-io-agents-test`）
是 V1 `agent-runner`。test 环境**今天本来就是 V1 托管**——`origin/test` 上这
份 runner compose 从来就是 agent-runner 形态，CI `deploy-test-runner-cvm` 把
它部署到真实存在的 `feedling-io-agents-test` CVM（P0 的 host-all 修复正是在
这个 CVM 上验证的）；仓库文件此前因分支上更早的一轮 Runtime-V2-only 迁移工作
漂移成了纯 serve-worker 形态，未曾随之推送到该 CVM 的真实部署——本次连同 prod
一起，从 `origin/test` 逐字节恢复回真实的 V1 形态。三环境不再有拓扑差异。

**关键差异 vs 纯 V2-only 拓扑**：serve-worker 从独立 runner CVM 搬进主 CVM
（与 backend 同镜像的第二个容器），`FEEDLING_ENCLAVE_URL`/`FEEDLING_API_URL`
从「公网 / dstack-gateway passthrough」变成「compose 内网地址」（`https://
enclave:5003` / `http://backend:5001`）——这是全部改动里唯一的地址形态差异，
其余环境变量（`DATABASE_URL`、`FEEDLING_RUNTIME_TOKEN_SECRET` 等）与此前
runner 侧的 serve-worker 完全一致。

**环境变量表**（主 CVM 新增的 `serve-worker` 服务）：

| 变量 | 值 | 说明 |
|---|---|---|
| `FEEDLING_API_URL` | `http://backend:5001`（字面量，非 `${VAR}`） | compose 内网地址，照抄 ingress `ROUTING_MAP` 的转发目标 |
| `FEEDLING_ENCLAVE_URL` | `https://enclave:5003`（字面量） | compose 内网地址，照抄 backend 自己现用的同一个值 |
| `DATABASE_URL` | `${DATABASE_URL}` | 与 backend/enclave 同一个加密 env 注入 |
| `FEEDLING_RUNTIME_TOKEN_SECRET` | `${FEEDLING_RUNTIME_TOKEN_SECRET}` | 必须与 backend/enclave 同值，否则本地 whoami 校验失败退回 reentrant 往返 |
| `FEEDLING_V2_MAX_WORKERS` | `"4"`（字面量，灰度期保守值） | 每 CVM 并发 turn 上限 |
| `FEEDLING_V2_FLEET_IDENTITY_REQUIRED` | `"1"` | fail-closed，除非注入 `FEEDLING_V2_RUNNER_CVM_ID`/`FEEDLING_V2_DEPLOYED_BUILD` |
| `FEEDLING_V2_RUNNER_CVM_ID` | `${FEEDLING_V2_RUNNER_CVM_ID:-main-cvm}` | 默认 `main-cvm`（区别于独立 runner CVM 用自己的 CVM UUID） |
| `FEEDLING_V2_SANDBOX_PROVIDER` | `"disabled"`（字面量） | 灰度期不接 E2B 沙箱；照抄即可，不要用 `${VAR}` |
| `FEEDLING_HOSTED_RUNTIME_POLICY` | `"dual"` | backend 与 serve-worker 都设，取代旧的 `"v2_only"` 字面量 |
| `FEEDLING_RUNTIME_DEFAULT_DESIRED` | `"resident"` | 无 allowlist 记录的账号默认 fence；保证部署瞬间行为与部署前一致 |

**P3 部署序（prod）**（design doc §7b）：

1. **Migration 先行**：alembic 跑 V2 表（0017/0018 系）+ `v2_user_allowlist`。
   纯增量，V1 不读这些表，先跑 migration 再起新镜像是安全序。
2. **主 CVM**：原地部署新镜像（`backend` 双路由 + `serve-worker` 容器）。
   原地重部署不翻 KMS 钥（2026-07-05 实证：compose_hash 变但钥不翻）；仍需
   先走完 pre → test 全流程再碰 prod。
3. **runner CVM：不动**。`deploy/docker-compose.phala.prod.runner.yaml`
   本次仅做「文件追平现状」的一次性恢复提交，此后不再随主 CVM 部署改动
   （除非未来任务显式改它）。
4. 部署完成瞬间：全员 fence 应为 `resident` → 行为与部署前完全一致
   （P3 验收点）；之后由 allowlist 逐步把个别账号切到 `v2`。

### Production CVM (prod9, current)

| | |
|---|---|
| Provider | Phala Cloud dstack on prod9 (`dstack-pha-prod9.phala.network`) |
| CVM ID | `0711c9a4-afdc-40c6-ba49-d8cb95f7e850` |
| App ID | `9798850e096d770293c67305c6cfdceed68c1d28` |
| Instance ID | `6fe9b54c9f2b428158c3e74de615d0f0a0c457ba` |
| Compose | `deploy/docker-compose.phala.yaml` — `ingress`, `backend`, `enclave`（`mcp` 服务已随 MCP 线于 2026-06-12 移除） |
| Current image | `ghcr.io/teleport-computer/feedling:22b0ed6` |
| Live git commit | `22b0ed6aa92a05d76951768f1924f45010ecda15` |
| Live built at | `2026-07-02T19:04:02Z` |
| Live compose hash | `0x0f136ba9dbc65dadfe2ad20cb663e6621d37d1e0c460830e22f6275bce3bad5d` |
| Public API | `https://api.feedling.app` via `dstack-ingress` |
| Public MCP | 已下线（FastMCP 服务器 2026-06-12 移除；`mcp.feedling.app` 不再服务） |
| Attestation | `https://9798850e096d770293c67305c6cfdceed68c1d28-5003s.dstack-pha-prod9.phala.network/attestation` |
| WS ingest | `wss://9798850e096d770293c67305c6cfdceed68c1d28-9998.dstack-pha-prod9.phala.network/ingest` |
| TLS model | `api.feedling.app` terminates at `dstack-ingress`; `/attestation` keeps its own dstack-KMS-derived TLS on `:5003` for iOS pinning. |
| MCP pubkey pin | Retired in prod9 architecture: `mcp_tls_cert_pubkey_fingerprint_hex` is empty by design; content-layer envelopes sealed to `enclave_content_pk` are the privacy boundary. |
| **Enclave content pk** | `2d642ec1f54719d8c6088e8cbaf394961cb804a533bd4d7366d48d1d543f5620` — **THE prod9 content-key baseline.** Verified against live `/attestation` 2026-07-03. Envelope `enclave_pk_fpr` = `sha256(pk)[:16]` = `50f9a01800d4a230de85507d25b86eb1`, a constant stamped on envelopes April→July → the enclave content key has **never changed**. ⚠️ Do NOT confuse with the retired prod5 value `f50c90f7…` (app `051a174f`) that still appears in the Phase A/B tables below — that is a different, dead CVM and is NOT this baseline. |
| mr-kms | `692afc6d7a86a32cfc1ebd9cad1a576aab012bab46986ba609bc8d6407270572` (live `/attestation` 2026-07-03) |
| KMS | legacy Phala KMS at `kms.dstack-pha-prod7.phala.network` (chain_id null — a KMS instance, NOT an on-chain KMS). The app-auth contract is on Sepolia: `0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F` (chain_id 11155111), per `/attestation` `app_auth`. |
| Deploy path | GitHub Actions `deploy-cvm` pins the GHCR image tag, deploys this CVM via Phala, then publishes the live dstack-computed compose hash on Sepolia. |

### Test CVM (prod9, `test` branch)

| | |
|---|---|
| Provider | Phala Cloud dstack on prod9 (`dstack-pha-prod9.phala.network`, node id `18`). **Account: `amiller-user` (amiller-users-projects)** since the 2026-07-01 account move — see below. |
| CVM ID | `5bfa1543-c5b4-42ca-842d-fd88984e5edf` (also in `deploy/test-cvm-id.txt`) |
| App ID | `173c7f49aeb54acb424676b17b17f78e5e2b2938` |
| Created | 2026-07-01 as `feedling-io-test`, instance `tdx.small`, **Phala KMS** (prod9 chain-0). Account migration (path B): the old test CVM `19b13ebe-d12e-4d19-97d1-6cf41389b663` / app_id `bb9716955423faed3508888e7c654ff46f5f0c2d` under `sxysun` was abandoned (balance exhausted 2026-06-18). Fresh app_id → new `enclave_content_pk`, so the reused test RDS was wiped of undecryptable rows. iOS test build repointed to the new app_id. Bootstrapped via the one-shot `.github/workflows/bootstrap-test-cvm.yml` (push to `bootstrap-cvm` branch; workflow since removed). CI deploy key is now `TEST_PHALA_CLOUD_API_KEY` (separate from prod's `PHALA_CLOUD_API_KEY`). |
| Compose | `deploy/docker-compose.phala.test.yaml` — same 3 services as prod (`ingress`/`backend`/`enclave`), with test domains + `_test` volumes |
| Public API | `https://test-api.feedling.app` (via dstack-ingress — live, `/healthz` 200) |
| Public MCP | 已下线（FastMCP 服务器 2026-06-12 移除） |
| Database | Dedicated test RDS `feedling-mcp-test-t4g-micro.cgh0oucoe0x9.us-east-1.rds.amazonaws.com:5432/postgres` — fully isolated from prod (separate instance → separate `enclave_content_pk` self-consistent, no shared schema). Injected via `TEST_DATABASE_URL`. |
| On-chain | **Separate** Sepolia FeedlingAppAuth `0x9AC034AAEf6Bb80690Be4d1f698b51796Bb7F2D5` (owner = the `ETH_DEPLOYER_KEY` address `0xa0eBcd26…`, so the CI `addComposeHash` is authorized), kept apart from prod's contract so the prod release log stays clean. Address lives in repo var `TEST_FEEDLING_APP_AUTH_CONTRACT`. Each `deploy-test-cvm` run publishes the live compose_hash here, fail-loud, same as prod. Deployed 2026-06-09 via a one-shot `workflow_dispatch` (since removed). |
| Deploy path | GitHub Actions `deploy-test-cvm` job (in `ci.yml`) on push to the `test` branch. Mirrors prod but targets the test compose / CVM / DB / contract and is branch-gated to `refs/heads/test`. |
| First-boot note | The CVM was first created 2026-06-09 WITHOUT a CF token (to mint the app_id quickly), so `dstack-ingress` couldn't issue the `test-*.feedling.app` LE certs initially. The `test`-branch CI deploy injects `CF_*` from GitHub secrets — domains + certs are now live. Backend also needed the test RDS reachable from the CVM (Publicly accessible + SG inbound 5432) before it stopped crash-looping. |
| iOS | The iOS app source is not in this repo. Point its test build at app_id `173c7f49aeb54acb424676b17b17f78e5e2b2938` + gateway `dstack-pha-prod9.phala.network` + test contract `0x9AC034AAEf6Bb80690Be4d1f698b51796Bb7F2D5`. ⚠️ (Was `bb9716955423…` before the 2026-07-01 path-B account move — that app_id is **retired**; do not point new builds at it.) |

### Runtime V2 worker CVM (test, `feedling-io-agents-test`)

Standalone pooled worker CVM (no backend/enclave/ingress). The historical image
package name remains `feedling-agent-runner`, but the image and compose contain
only the Python Runtime V2 `serve-worker`.

| | |
|---|---|
| Provider | Phala Cloud dstack on prod9, account `amiller-user` (same as main test CVM) |
| CVM ID | `0f065d29-37c6-4c79-b871-04e526c6c91d` (also in `deploy/test-runner-cvm-id.txt`) |
| App ID | `0cf2da16edc368625cee6898852ebc5dabb51558` |
| Created | 2026-07-02 as `feedling-io-agents-test`, `tdx.small`, **Phala KMS** (prod9). Provisioned locally via `phala deploy` (no `--cvm-id` ⇒ new app) pinned to `feedling-agent-runner:ab78491` with only the non-secret cross-CVM env (`FEEDLING_API_URL` / `FEEDLING_ENCLAVE_URL` / `AGENT_MAX_CHILDREN`). The **healthy, secret-bearing** deploy + on-chain compose_hash auth are done by the CI `deploy-test-runner-cvm` job (it holds `TEST_DATABASE_URL` / `TEST_FEEDLING_RUNTIME_TOKEN_SECRET` / `ETH_DEPLOYER_KEY`), which `phala deploy --cvm-id`s this same CVM in place. |
| Compose | `deploy/docker-compose.phala.runner.yaml` — exactly one `serve-worker`; no resident service, CLI toolchain, per-user process, data volume, lease, or checkpoint. Genesis runs on the worker's dedicated thread and claims from PostgreSQL. |
| Runtime | `backend/model_api_runtime/v2/serve_worker.py`; chat jobs coordinate through `FOR UPDATE SKIP LOCKED`, and worker liveness is published in `v2_worker_heartbeats`. |
| Shares w/ main test CVM | same test RDS (`TEST_DATABASE_URL`), same `FEEDLING_RUNTIME_TOKEN_SECRET`, same Sepolia FeedlingAppAuth `0x9AC0…` (runner publishes its OWN compose_hash there — harmless; iOS audit card only checks the MAIN app's hashes) |
| Cross-CVM reach | `FEEDLING_API_URL=https://test-api.feedling.app`; `FEEDLING_ENCLAVE_URL=https://173c7f49…-5003s.dstack-pha-prod9.phala.network` (main enclave passthrough, in-enclave TLS, `verify=False`) |
| Deploy path | Mandatory CI `deploy-test-runner-cvm` job after every hosted/CVM-affecting `test` deployment. |
| Status | V2-only source topology. A dead pool fails chat before persistence with `workers_unavailable`; there is no resident fallback selector. |

### Pre CVM (prod9, `pre` branch)

Third environment: `pre` branch → `deploy-pre-cvm` CI job → this CVM. It shares
the test Phala account/key, R2 buckets, runtime-token secret and most feature-flag
vars (all `TEST_*` references in the CI job are deliberate), while DB, AppAuth
contract, domain and CVM are pre-specific. Hosted runtime ownership is V2-only
in test, pre, and production.

| | |
|---|---|
| Provider | Phala Cloud dstack on prod9 (node id `18`), account `amiller-user` — same account + `TEST_PHALA_CLOUD_API_KEY` as test |
| CVM ID | `82485d6f-9c23-48f1-9bdd-5a0d38531c3e` (also in `deploy/pre-cvm-id.txt`) |
| App ID | `7d18a1f234a0d90e5f643cac8283b6048451b8f7` |
| Created | 2026-07-07 as `feedling-io-pre`, `tdx.small`, **Phala KMS** (prod9). Provisioned locally via `phala deploy` (no `--cvm-id` ⇒ new app) without secrets, to mint the app_id; the healthy secret-bearing deploy is the CI `deploy-pre-cvm` job. |
| Compose | `deploy/docker-compose.phala.pre.yaml` — same 3 services as test (`ingress`/`backend`/`enclave`), with `pre-api.feedling.app` + `_pre` volumes. `FEEDLING_IO_ONBOARDING_BRANCH` stays `test` (io-onboarding has no pre branch). `FEEDLING_HOSTED_RUNTIME_POLICY` is literal `v2_only`; no encrypted env can select resident. |
| Public API | `https://pre-api.feedling.app` (dstack-ingress auto-creates the CF DNS records once CI injects `CF_*`) |
| Attestation | `https://7d18a1f234a0d90e5f643cac8283b6048451b8f7-5003s.dstack-pha-prod9.phala.network/attestation` (repo var `PRE_MAIN_ENCLAVE_URL`) |
| Database | Dedicated pre RDS, injected via `PRE_DATABASE_URL` — fully isolated from test/prod (pre's enclave content key differs from test's, so sharing a DB would mix mutually-undecryptable ciphertext + double-schedule proactive jobs). |
| On-chain | **Separate** Sepolia FeedlingAppAuth `0x65844Dd69eba4Aa4a784e089dA9D9308F430F794` (owner = the `ETH_DEPLOYER_KEY` address, deployed 2026-07-07 via `deploy-test-contract.yml` dispatch), repo var `PRE_FEEDLING_APP_AUTH_CONTRACT`. Kept apart from test's contract because a shared AppAuth + a newly created CVM is the exact combination that flipped the main enclave key in the 2026-07-05 prod-runner incident. |
| Deploy path | CI `deploy-pre-cvm` job (in `ci.yml`) on push to the `pre` branch. Clone of `deploy-test-cvm` with pre compose / CVM / DB / contract, branch-gated to `refs/heads/pre`. |
| Baseline | Set repo var `PRE_ENCLAVE_CONTENT_PK_BASELINE` from a manual `/attestation` read after the first healthy deploy (the attestation gate is inert until then). |

### Runtime V2 worker CVM (pre, `feedling-io-agents-pre`)

Mirror of the V2-only test worker CVM for the pre environment.

| | |
|---|---|
| Provider | Phala Cloud dstack on prod9 (node id `18`), account `amiller-user` |
| CVM ID | `d83aa64f-b0d9-40a1-91dd-c66307bd2c08` (also in `deploy/pre-runner-cvm-id.txt`) |
| App ID | `cd73962001b190ce1be1e438422aeb46e95f5a79` |
| Created | 2026-07-07 as `feedling-io-agents-pre`, `tdx.small`, **Phala KMS** (prod9). |
| Compose | `deploy/docker-compose.phala.pre.runner.yaml` — exactly one pooled `serve-worker`, no resident service or data volume |
| Shares w/ main pre CVM | pre RDS (`PRE_DATABASE_URL`), `TEST_FEEDLING_RUNTIME_TOKEN_SECRET` (same secret as test — reuse is deliberate), Sepolia FeedlingAppAuth `0x6584…` (runner publishes its own compose_hash there) |
| Cross-CVM reach | `FEEDLING_API_URL=https://pre-api.feedling.app` (`PRE_MAIN_API_URL`); `FEEDLING_ENCLAVE_URL=https://7d18a1f2…-5003s.dstack-pha-prod9.phala.network` (`PRE_MAIN_ENCLAVE_URL`) |
| Deploy path | CI `deploy-pre-runner-cvm` job runs unconditionally after every CVM-affecting `pre` deploy; disabling the runner while the backend is V2-only is not an allowed configuration. |
| Status | Provisioned and CI-managed. `serve-worker` is the sole hosted owner; every qualifying deploy must pass turn-worker, capacity, Genesis, and runtime-policy coverage gates. |

### Runtime V2 worker fleet (production)

| | |
|---|---|
| CVM IDs | One independent worker CVM per non-comment line in `deploy/prod-runner-cvm-ids.txt`; at least two distinct IDs are a hard CI precondition |
| Compose | `deploy/docker-compose.phala.prod.runner.yaml` — exactly one pooled `serve-worker` per CVM |
| Shared control plane | Production `DATABASE_URL`, `FEEDLING_RUNTIME_TOKEN_SECRET`, main API URL, and main enclave URL |
| Deploy path | Mandatory CI `deploy-prod-runner-cvm`; the same image and compose are rolled across every listed CVM |
| Application identity | CI injects `FEEDLING_V2_RUNNER_CVM_ID=<target CVM>` and `FEEDLING_V2_DEPLOYED_BUILD=<exact 7-char image build>`; production `serve-worker` fails closed on missing/mismatched values |
| Post-deploy proof | After outliving old heartbeat freshness, CI requires a positive-capacity turn heartbeat and matching Genesis heartbeat for every exact inventory CVM/build identity, plus fully reconciled `v2_only` policy |
| Scale model | Increase `FEEDLING_V2_MAX_WORKERS` for slots per CVM or add independent CVM IDs for failure domains; PostgreSQL job claims coordinate the fleet |
| Recovery | Halt admission or roll forward/back to a database-compatible V2 worker image. Never relaunch resident. See `HOSTED_RUNTIME_V2_ROLLOUT.md`. |

### Retired hosted resident topology

The former supervisor, per-user CLI processes, homes, leases, and rollback
procedure have been removed from this runbook. Git history preserves the
incident record; it must not be copied into current manifests. Recover hosted
incidents only through [`HOSTED_RUNTIME_V2_ROLLOUT.md`](HOSTED_RUNTIME_V2_ROLLOUT.md).

## Enclave configuration

### Screen frame VLM captioning

Screen perception captioning is opt-in per user via the `screen_caption_enabled` flag (default OFF, fail-closed). To enable:

- **Required secret**: `FEEDLING_SCREEN_VLM_API_KEY` — OpenRouter API key for VLM inference. Injected via `phala deploy -e FEEDLING_SCREEN_VLM_API_KEY=<key>` (encrypted env channel, not in compose_hash). If absent, the `/v1/screen/frames/<id>/caption` route fails closed with `screen_caption_unconfigured`.
- **Optional overrides**: `FEEDLING_SCREEN_VLM_MODEL` (default `qwen/qwen3-vl-8b-instruct`), `FEEDLING_SCREEN_VLM_BASE_URL` (default `https://openrouter.ai/api/v1`). Injected same way.

**Non-code prerequisites before enabling for any user:**
1. **Privacy disclosure**: Disclose to users that screen pixels egress to OpenRouter (third-party inference provider) for captioning. Although the backend never holds plaintext pixels (enclave decrypts, captions only), this is a new privacy expansion.
2. **Data retention policy**: Configure the OpenRouter account to disable prompt logging, model training, and other retention policies. Prefer zero-retention settings or an explicit no-training SLA.

### Screen frame ciphertext offload to R2 (object storage)

The heavy frame ciphertext (`frame_envelopes.doc.body_ct`, >150KB ChaCha20-Poly1305 screenshot blob) is offloaded to Cloudflare R2 (S3-compatible) so it stops bloating Postgres rows/TOAST and backups. PG keeps only the small envelope metadata (`env_meta`) + an R2 pointer (`body_key`); see `backend/object_storage.py` and migration `0007_frame_body_to_r2`.

- **Config** (reuses the repo's existing `R2_*` credentials; the frame bucket is a dedicated var so it never collides with the WAL-G backup bucket `R2_BUCKET`):
  - `R2_ENDPOINT` (`https://<accountid>.r2.cloudflarestorage.com`; derived from `R2_ACCOUNT_ID` if unset) — shared R2 endpoint.
  - `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` — R2 S3 credentials. **The token MUST be scoped to the frames bucket** (a token scoped only to other buckets returns `AccessDenied`).
  - `R2_FRAMES_BUCKET` — the dedicated frames bucket, e.g. `io-image-frames`.
  - `R2_CHAT_FILES_BUCKET` — dedicated bucket for heavy chat ciphertext — **both `content_type=file` AND `content_type=image`** — offloaded off the `chat_messages` row (keeps the row a slim pointer; the body is lazily re-fetched at the delivery exits). Two prefixes in the one bucket: `chatfiles/<user>/<msg>` and `chatimages/<user>/<msg>`, split so images can carry their own lifecycle rule / usage accounting (they dwarf files in both count and bytes). Reads and deletes use the `body_key` stored on the row — never a recomputed one — so an older key layout still resolves; `object_storage.chat_key_owned_by` rejects any key outside the row owner's own prefix. Non-secret name, so both compose files default it to `io-user-attachments` — it activates automatically wherever the frames R2 credentials are injected. **The R2 token MUST also be scoped to `io-user-attachments`** (a frames-only token returns `AccessDenied` on PUT → the offload fails and the row stays inline in Postgres, exactly like today). Create the bucket + widen the token scope before relying on the R2 path.
  - **Migrating existing chat images**: rows written before images joined the offload still carry a 1–2MB base64 body inline. Run `backend/backfill_chat_images_to_r2.py` offline against the target `DATABASE_URL` + R2 creds (`--dry-run` first to count/size; `--user <uid>` to scope). Idempotent + resumable; already-offloaded rows are skipped, and a failed upload leaves the row inline and readable rather than pointing at a missing object.
  Injected via `phala deploy -e R2_*=<value>` (encrypted env channel; the compose `environment:` keys exist for interpolation, so the *values* are not baked into compose_hash — same mechanism as `DATABASE_URL` / `FEEDLING_SCREEN_VLM_API_KEY`).
- **GitHub Secrets / CI wiring** (`.github/workflows/ci.yml` deploy jobs map these into the `phala deploy -e` calls; `backend` service env lives in `deploy/docker-compose.phala*.yaml`):
  - **Prod** (`deploy-cvm`): repo secrets `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_FRAMES_BUCKET`.
  - **Test** (`deploy-test-cvm`): `TEST_`-prefixed secrets `TEST_R2_ENDPOINT`, `TEST_R2_ACCESS_KEY_ID`, `TEST_R2_SECRET_ACCESS_KEY`, `TEST_R2_FRAMES_BUCKET` (mapped to the un-prefixed container env, same convention as `TEST_DATABASE_URL`).
  - Note: adding the four `R2_*` keys to the backend compose changes `compose_hash` once; the deploy job's existing on-chain publish step re-auths it. Until the secrets are populated the feature stays OFF (fail-open to legacy inline storage).
- **Fail-open to legacy**: if the credentials/bucket are absent, the backend keeps storing `body_ct` inline in the row (legacy shape) — the feature is gated on config, so a missing/incomplete secret degrades gracefully rather than dropping frames.
- **Egress**: the non-TEE backend (not the enclave) makes outbound HTTPS to R2 on the frame write/read paths. The enclave is unaffected — it still pulls frame envelopes via the backend's `/v1/screen/frames/<id>/envelope` route, which now transparently reconstructs `body_ct` from R2.
- **Threat model**: R2 creds live in the TDX CVM; a leak exposes only ciphertext blobs (content_sk is in the enclave/iOS, never the backend) — equivalent to a `DATABASE_URL` leak today.
- **Migrating existing rows**: run `backend/backfill_frames_to_r2.py` offline against prod `DATABASE_URL` + the R2 creds (`--dry-run` first to count/size). Idempotent + resumable; already-offloaded rows are skipped. The schema migration (`0006`) only adds columns — it does NOT move data.

### Client diagnostic logs to R2 (`backend/diagnostics/`)

Lets a client upload its persistent `diagnostics.log` (`POST /v1/diagnostics/logs`, user auth) so a developer can pull it by user id (`GET /v1/admin/diagnostics/logs/<user_id>`, admin auth → presigned download URLs). See `backend/diagnostics/`.

- **Plaintext, by design**: unlike frame ciphertext, these logs are stored as plaintext — a scoped exception to the "server never sees user plaintext" invariant (user-initiated upload, few testers, private bucket, short retention). Treat the bucket accordingly.
- **Config** (reuses the same `R2_ENDPOINT` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` credentials as frames):
  - `R2_USER_LOGS_BUCKET` — dedicated bucket `io-user-logs`, separate from `R2_FRAMES_BUCKET` and the WAL-G backup `R2_BUCKET`. The bucket name is **not** secret, so both compose files (`docker-compose.phala.yaml` / `docker-compose.phala.test.yaml`) default it to `io-user-logs` — no extra GitHub secret / `-e` flag needed. It activates automatically wherever the frames R2 credentials are already injected.
  - **The R2 token MUST also be scoped to `io-user-logs`** (a frames-only token returns `AccessDenied` here → the route falls back to inline Postgres, see below). Create the bucket and widen the token scope before relying on the R2 path.
- **Retention**: set a Cloudflare lifecycle rule on `io-user-logs` to expire objects after ~7 days. DB-side, the route trims each user's index stream to the newest 10 rows.
- **Fail-open to Postgres**: when `R2_USER_LOGS_BUCKET`/creds are absent, the log text is stored inline in the `client_diagnostics` Postgres log stream instead — local dev / tests need no R2.
- **Egress**: the non-TEE backend (not the enclave) makes outbound HTTPS to R2 on upload/admin-read.

### Retired VPS (historical, redacted)

| | |
|---|---|
| Host | Retired VPS IP redacted |
| Install root | Retired host path redacted |
| Data dir | Retired host path redacted; wiped + re-seeded on 2026-04-20 |
| Services | `feedling-backend.service`, `feedling-mcp.service` — user-level systemd units on the retired host. The old `feedling-chat-bridge.service` was retired on 2026-04-20 when MCP's `feedling.chat.post_message` took over agent replies. |
| Mode | Multi-tenant only. Per-user HMAC-peppered api_keys issued by `POST /v1/users/register`; no shared key, no `SINGLE_USER` env var anymore. |
| Ports | Flask `:5001`, MCP SSE `:5002`, WebSocket ingest `:9998` |
| APNs key | Retired path redacted |
| Last commit | `78b51a6` (v0 / SINGLE_USER strip, 2026-04-20) |
| Backups | Retired host backup paths redacted |

Flip history: The VPS originally ran in `SINGLE_USER=true` mode with
a shared `FEEDLING_API_KEY`. Prod user's data was silently migrated v0→v1
on 2026-04-20 (task #32), and the same day the SINGLE_USER/v0 stack was
stripped entirely (tasks #23/#33). After the strip, the data directory
was wiped and the user reinstalled fresh against a multi-tenant backend
via the normal `POST /v1/users/register` flow from iOS.

## On-chain

## Live

### Ethereum Sepolia release log (current)

| | |
|---|---|
| Chain | Ethereum Sepolia (11155111) |
| Contract | `0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F` |
| Owner | `0xa0eBcd26D7816D68a74b0CdC8037C16F8fcbF9C0` (throwaway) |
| Deployed at | block 10691079, tx `0x752f213ae95f6759a86750dab9545c79c6841ad7838082ddf6ad5271d117915f` |
| First `addComposeHash` | block 10691089, tx `0x6ea7f87fc597352bd1007adb6cf0d5d5b4e787dd9ea6915d0a890089b5813893` for the simulator compose_hash `ea549f02e1a25fabd1cb788380e033ec5461b2ffe4328d753642cf035452e48b` |
| Explorer | https://sepolia.etherscan.io/address/0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F |
| Purpose | Current public release log for authorized Feedling CVM compose hashes. Moving this log to mainnet remains deferred. |
| Deployer key status | **Throwaway. Rotate before any Phase 2 work.** The private key was pasted in a chat transcript (Apr 19, 2026) and must not be reused for anything that holds real value. |

### Phase 2 TDX CVM (superseded by Phase 3, 2026-04-20)

| | |
|---|---|
| Provider | Phala Cloud (dstack-dev-0.5.8, Intel TDX) on node `prod5` (US-WEST-1) |
| Name | `feedling-enclave` |
| App ID | `051a174f2457a6c474680a5d745372398f97b6ad` |
| Instance ID | `7a4c69589d441e84e9397c0c8a387e8c9e6adcae` |
| VM UUID | `4386636e-1325-4b92-99d8-f2ca00befdb4` |
| Instance | tdx.small (1 vCPU, 2 GB RAM, 20 GB disk) |
| Compose | `deploy/docker-compose.phala.yaml` @ commit `4826ec7` |
| Image | `ghcr.io/account-link/feedling:4826ec7` (git_commit baked) |
| Compose hash | `0x698b1824bfe18ce8a1b0d5f3b951984d6025d90bf60dbfde04efb20c88d9c93c` |
| MRTD | `f06dfda6dce1cf904d4e2bab1dc37063…` |
| Gateway base | `dstack-pha-prod5.phala.network` (dstack-gateway TEE TLS) |
| On-chain entries | Initial compose_hash `0xd118700e…`: Sepolia tx `0xdfbc0b8df0a3f9306c4bb4c226cce1756230663ad7ecbdefff3371c562445f5b`. Bake-git_commit rehash `0x698b1824…`: Sepolia tx `0x29e89b3dfdb9ea7a44f13a192e5228f26a35723cac07fe5b1552c95ce2683633`. |
| Dashboard | https://cloud.phala.com/dashboard/cvms/4386636e-1325-4b92-99d8-f2ca00befdb4 |
| Purpose | First real-TDX deployment. iOS audit card replays the event log, verifies RTMR3 binding to compose_hash, checks compose_hash is authorized on-chain. |
| Retired by | Phase 3 TLS-in-enclave deploy on the same CVM (see below). |

### Phase 3 TDX CVM with in-enclave TLS (superseded by Phase A, 2026-04-20)

| | |
|---|---|
| Compose | `deploy/docker-compose.phala.yaml` @ commit `8e1280b` — first with `FEEDLING_ENCLAVE_TLS=true` |
| Image | `ghcr.io/account-link/feedling:451b5b0` |
| Compose hash | `0xb0fb1f848151ec8fb39c4814f138b1d1b143d4d729dc800302d5123c1c0f2163` |
| On-chain | Sepolia tx `0x8de67abaf677e221ba4ee34b5a004753d0f4981bdc3c952cbcb4112a652a169c` (block 10692341) |
| Purpose | First Feedling deployment where TLS for the audit port is generated *inside* the CVM and pinned by clients against a fingerprint in the signed TDX quote. |
| Retired by | Phase A deploys below. |

### Phase A TDX CVM with content-encryption + migration (superseded by Phase B, 2026-04-20)

| | |
|---|---|
| Provider | Phala Cloud (dstack-dev-0.5.8, Intel TDX) on node `prod5` (US-WEST-1) |
| Name | `feedling-enclave` (same CVM, compose updated in place) |
| App ID | `051a174f2457a6c474680a5d745372398f97b6ad` |
| Instance ID | `7a4c69589d441e84e9397c0c8a387e8c9e6adcae` |
| VM UUID | `4386636e-1325-4b92-99d8-f2ca00befdb4` |
| Compose | `deploy/docker-compose.phala.yaml` @ commit `0a54414` |
| Image | `ghcr.io/account-link/feedling:90c8ff6` — adds `POST /v1/content/rewrap` (batched v0→v1 migration endpoint) and surfaces a clear `409 nudge_not_supported_on_v1_cards_yet` instead of silent 404 when `identity.nudge` hits a v1 card |
| Compose hash | `0x9f7fe0a823bf2820877851863d322b0f3be7fff819a40a8826e6ca994597cf48` (attested by `mr_config_id[1:33]` + `compose-hash` event in RTMR3) |
| TLS cert fingerprint | `5698f0ade4bb412d6b0847a62d695138f3bbd287dc7d1dbdeb67b15dc445e5ef` — unchanged from Phase 3 because the TLS key derivation path (`feedling-tls-v1`) is stable for this app_id. Phala dstack-KMS derives keys from `(kms_root, app_id, path)`, not `compose_hash`, so compose updates do not rotate keys. |
| Enclave content pk | `f50c90f711e8484c7178a69657cad99944cba7c0cdeaa3cccb0388021e7d2744` — ⚠️ **retired prod5 app `051a174f` ONLY — NOT the prod9 baseline.** The live prod9 content pk is `2d642ec1…` (see the Production CVM table at the top). Do not compare live `/attestation` against this value. — also stable across compose updates, same reason. Implication: v1 envelopes wrapped for this enclave survive compose rotations without a rewrap dance. |
| MRTD | `f06dfda6dce1cf904d4e2bab1dc37063…` (unchanged — same base image) |
| Endpoints | unchanged from Phase 3 — app-id-bound URLs at dstack-pha-prod5, with `-5003s.` passthrough for /attestation |
| Enclave /attestation | https://051a174f2457a6c474680a5d745372398f97b6ad-5003s.dstack-pha-prod5.phala.network/attestation |
| Backend /healthz | https://051a174f2457a6c474680a5d745372398f97b6ad-5001.dstack-pha-prod5.phala.network/healthz |
| MCP SSE | https://051a174f2457a6c474680a5d745372398f97b6ad-5002.dstack-pha-prod5.phala.network/sse |
| On-chain entries | Every historical compose_hash is still `isAppAllowed()=true`, so older iOS audit-card captures still pass. Ordered from oldest to newest: `0xb0fb1f84…` (Phase 3): tx `0x8de67abaf677e221ba4ee34b5a004753d0f4981bdc3c952cbcb4112a652a169c`. `0x2f0b80b6…` (Phase A.1 :8b53404 before FEEDLING_FLASK_URL fix): tx `0xc9b5c89c25bd7541ec87bdbc0a4b4e74336821fb91b016a8087dab689b91f1d2`. `0x593cb8aa…` (Phase A.1 fixed): tx `0x5b5a933dfc6e1f6376a32029d7a31632723dcc75447104b12ebd5da5e2f3e825`. **Current `0x9f7fe0a8…` (Phase A.6): tx `0xb3b434b6db6abd45eb492d2a708d8d7d6b99d5af59d5f01bc1686a74ed3e6c27`.** |
| Dashboard | https://cloud.phala.com/dashboard/cvms/4386636e-1325-4b92-99d8-f2ca00befdb4 |
| Audit evidence | CLI 7/7 green (`tools/audit_live_cvm.py`). Live E2E: register → whoami returns user + enclave pubkeys → MCP wraps memory.add → backend stores ciphertext (no plaintext title/description/type) → enclave `/v1/memory/list` returns plaintext via `K_enclave` decrypt. `/v1/content/rewrap` verified live (empty-items returns {summary: {total:0,…}}). |
| Purpose | First Feedling deployment where content written through MCP is stored as ciphertext end-to-end AND where a silent v0→v1 migration endpoint exists. Server operators with full backend-disk access cannot read users' memory/identity content. Chat already encrypted via iOS write path (shipped earlier). Remaining plaintext surface: `identity.nudge` (mutate-in-place, 409s on v1 now with a pointer to Phase C), `chat.post_message` (agent-authored chat replies, same constraint). |
| Retired by | Phase B deploy below. |

### Phase B TDX CVM with privacy UX + export/reset endpoints (superseded by Phase C, 2026-04-20)

| | |
|---|---|
| Provider | Phala Cloud (dstack-dev-0.5.8, Intel TDX) on node `prod5` (US-WEST-1) |
| Name | `feedling-enclave` (same CVM, compose updated in place) |
| App ID | `051a174f2457a6c474680a5d745372398f97b6ad` |
| VM UUID | `4386636e-1325-4b92-99d8-f2ca00befdb4` |
| Compose | `deploy/docker-compose.phala.yaml` @ commit `aa34c7e` |
| Image | `ghcr.io/account-link/feedling:123a45b` — adds `GET /v1/content/export` + `POST /v1/account/reset` endpoints powering the Phase B Settings → Privacy flows |
| Compose hash | `0x83a415ad16718ceab6eb9bab04a69c05157324c9deaf911d570b10051a772a18` (attested by `mr_config_id[1:33]` + `compose-hash` event in RTMR3) |
| TLS cert fingerprint | `5698f0ade4bb412d6b0847a62d695138f3bbd287dc7d1dbdeb67b15dc445e5ef` — unchanged from Phase 3 (dstack-KMS derivation is stable per app_id across four compose rotations now) |
| Enclave content pk | `f50c90f711e8484c7178a69657cad99944cba7c0cdeaa3cccb0388021e7d2744` — ⚠️ **retired prod5 app `051a174f` ONLY — NOT the prod9 baseline** (live prod9 = `2d642ec1…`). — unchanged for the same reason. Implication stands: v1 envelopes from earlier compose states are still decryptable after this deploy. |
| MRTD | `f06dfda6dce1cf904d4e2bab1dc37063…` (unchanged) |
| On-chain entry | compose_hash `0x83a415ad…`: Sepolia tx `0x8b9b77165cd45aeaf99e9976a8f9cfb2091db45dc2b04134b5b32af8332681fa`. Every prior compose hash still `isAppAllowed()=true`. |
| Audit evidence | CLI 7/7 green. Live E2E: register → seed chat + memory → export returns JSON with `attestation_snapshot.compose_hash == 0x83a415ad…` and a Content-Disposition suggesting `feedling-export-…` filename → reset w/o confirm body returns 400 → reset with `{"confirm":"delete-all-data"}` returns `{deleted: true}` → subsequent call returns 401 (account gone). |
| iOS | `xcodebuild BUILD SUCCEEDED` on iPhone 16 Pro sim. First-launch onboarding renders. Full iOS UX surface (onboarding + Privacy page + export/delete/reset + audit-card tap-to-expand + raw JSON + compose-hash consent modal) is in the image but needs a physical device or a TestFlight build for the one real prod user to exercise. |
| Purpose | First Feedling deployment where users can exercise their own data: export a decrypted archive, hard-delete their account, or reset and re-import. The Settings → Privacy page surfaces the audit card as a first-class destination with plain-language mechanism reveals per row + a raw `/attestation` JSON viewer for auditors. Compose-hash-changed consent modal blocks the app when the Feedling team pushes a new version until the user reviews or signs out — the consent trigger is `compose_hash` (app layer), NOT MRTD (dstack-OS platform layer), per dstack-tutorial §1. |
| Retired by | Phase C deploy below. |

### Phase C TDX CVM with MCP-port TLS-in-enclave (superseded by Phase C.3, 2026-04-20)

| | |
|---|---|
| Provider | Phala Cloud (dstack-dev-0.5.8, Intel TDX) on node `prod5` (US-WEST-1) |
| Name | `feedling-enclave` (same CVM, compose updated in place) |
| App ID | `051a174f2457a6c474680a5d745372398f97b6ad` |
| VM UUID | `4386636e-1325-4b92-99d8-f2ca00befdb4` |
| Compose | `deploy/docker-compose.phala.yaml` @ commit `37b40a4` |
| Image | `ghcr.io/account-link/feedling:60014a7` — first image where MCP (port 5002) terminates TLS inside the enclave with the same dstack-KMS-derived cert as the attestation port |
| Compose hash | `0x14cd6edb382b3229ebe36bf030f1bdc087765a9004d1ad323af58904c72df38f` |
| TLS cert fingerprint | `5698f0ade4bb412d6b0847a62d695138f3bbd287dc7d1dbdeb67b15dc445e5ef` — unchanged across five compose rotations (Phase 3 → A.1 → A.1 fixed → A.6 → B → C). Confirms dstack-KMS derivation is stable per app_id. |
| On-chain entry | compose_hash `0x14cd6edb…`: Sepolia tx `0xa6e0282c698cbe8e925c968624a2f2315bad5cc868568053598ccb6071984252`. Every prior compose hash still `isAppAllowed()=true`. |
| Audit evidence | CLI **8/8** green. New Row 8: `openssl s_client`-style TLS handshake against `-5002s.*` returns a peer cert whose `sha256(DER)` matches `enclave_tls_cert_fingerprint_hex` — byte-identical to the Row 7 attestation-port pin. |
| Routing unchanged | `mcp.feedling.app` still goes through Caddy reverse-proxy → gateway-terminated TLS so Claude.ai and existing MCP clients don't break. The `-5002s.` passthrough URL is the pinnable path; a future Phase C sub-ship moves `mcp.feedling.app` to layer4 SNI passthrough + ACME-DNS-01 inside the enclave. |
| Purpose | First Feedling deployment where both the attestation port AND the MCP port terminate TLS inside the TDX-attested enclave boundary, with the same enclave-bound cert. An auditor running `tools/audit_live_cvm.py` can now cryptographically verify end-to-end that the `-5002s.*` MCP endpoint is the exact enclave the attestation quote describes. Agent ↔ enclave metadata is no longer trust-the-gateway-operator on the pinned path. |
| Retired by | Phase C.3 deploy below. |

### Phase C.3 TDX CVM with encrypted nudge + encrypted agent chat reply (superseded by Phase C.2 ACME, 2026-04-20)

| | |
|---|---|
| Provider | Phala Cloud (dstack-dev-0.5.8, Intel TDX) on node `prod5` (US-WEST-1) |
| Name | `feedling-enclave` (same CVM, compose updated in place) |
| App ID | `051a174f2457a6c474680a5d745372398f97b6ad` |
| VM UUID | `4386636e-1325-4b92-99d8-f2ca00befdb4` |
| Compose | `deploy/docker-compose.phala.yaml` @ commit `a9109c3` |
| Image | `ghcr.io/account-link/feedling:cc329a8` — adds `/v1/identity/replace` + `/v1/chat/response` envelope branch. Unlocks MCP-side decrypt→mutate→rewrap for `identity.nudge` on v1 cards and agent-authored chat replies landing as ciphertext on disk. |
| Compose hash | `0xa04608c72639c66a625706b7ac4b9f1ac8dd449c690a0544b173ecede265e83e` |
| TLS cert fingerprint | `5698f0ade4bb412d6b0847a62d695138f3bbd287dc7d1dbdeb67b15dc445e5ef` — **unchanged across SIX compose rotations now** (Phase 3 → A.1 → A.1 fixed → A.6 → B → C → C.3). dstack-KMS per-app derivation is load-bearing stable. |
| On-chain entry | compose_hash `0xa04608c7…`: Sepolia tx `0x7873c5dd4c9b6636994d9a3adda7ded8618394ce1a9f577a1ba9c74dc5acf7b0`. |
| Audit evidence | CLI **8/8** green. Live E2E: `/v1/identity/replace` rejects missing envelope (400 ✓), `/v1/chat/response` envelope branch validates (400 on malformed ✓), plaintext content path still accepted (200 ✓ back-compat). Full decrypt→mutate→rewrap flow validated locally against the dstack simulator before deploy. |
| Purpose | Closes the last plaintext-at-rest gaps for the two write paths that couldn't be closed in Phase A: `identity.nudge` mutations (now wrapped end-to-end via MCP's orchestration of decrypt from enclave → mutate in MCP process → rewrap → replace) and agent-authored chat replies via `feedling.chat.post_message` (MCP wraps plaintext into v1 envelope before POSTing). Remaining plaintext surfaces are limited to the in-flight message itself (present in the MCP process memory inside the TDX-attested container boundary for the duration of one RPC) — never at rest on disk. `mcp.feedling.app` (CA-signed) routing unchanged pending Phase C part 2 (ACME-DNS-01). |
| Retired by | Phase C.2 deploy below. |

### Phase C.2 TDX CVM with ACME-DNS-01 Let's Encrypt cert inside enclave (superseded by Phase D, 2026-04-20)

| | |
|---|---|
| Provider | Phala Cloud (dstack-dev-0.5.8, Intel TDX) on node `prod5` (US-WEST-1) |
| Name | `feedling-enclave` (same CVM, compose updated in place) |
| App ID | `051a174f2457a6c474680a5d745372398f97b6ad` |
| VM UUID | `4386636e-1325-4b92-99d8-f2ca00befdb4` |
| Compose | `deploy/docker-compose.phala.yaml` @ commit `f53cbbd` |
| Image | `ghcr.io/account-link/feedling:169cb6a` — adds ACME-DNS-01 client in `backend/acme_dns01.py` (file since deleted 2026-06-12 with the mcp removal), CF API token env injection via Phala's encrypted channel, `/tls` dir pre-created with feedling ownership so the LE cert cache is writable |
| Compose hash | `0x23a2c2869567d15220383e4acb5ceb5cf27d78e087d2d4e357e4b3c053a5dc68` |
| TLS cert fingerprint (attestation port 5003) | `5698f0ade4bb412d6b0847a62d695138f3bbd287dc7d1dbdeb67b15dc445e5ef` — unchanged across SEVEN compose rotations. dstack-KMS per-app derivation is still load-bearing stable. |
| MCP TLS pubkey fingerprint (port 5002) | `e98665a3e94ac90a0a26453a73e16d5a569f791c181cfbc6ba98598f358cf63e` — sha256(SubjectPublicKeyInfo DER) of the LE cert's pubkey. Derived from dstack-KMS at path `feedling-mcp-tls-v1`, so the pubkey is stable across LE cert renewals (the cert changes every 90 days, the key doesn't). |
| On-chain entry | compose_hash `0x23a2c286…`: Sepolia tx `0xe2a9ceab0334cc2133baede9daca94c79956f5f9d7c5751a97955b9e9e78426a`. |
| Audit evidence | CLI **8/8** green (`tools/audit_live_cvm.py`). Row 8 now proves: (a) MCP port 5002 presents a Let's Encrypt-signed cert with SAN=mcp.feedling.app, CA-verified against system roots via manual x509 verification; (b) cert pubkey SPKI sha256 matches attested value — cert key is provably inside the TDX-attested CVM. |
| SNI quirk | Phala's dstack-gateway routes connections by SNI and only accepts its own `-PORTs.*.phala.network` hostname. Row 8 of the audit script connects with the gateway hostname as SNI, then verifies the served cert manually. Caddy on the VPS mirrors this (`tls_server_name` = gateway hostname + `tls_insecure_skip_verify` in `deploy/Caddyfile`). Trust root is the attestation, not Caddy. |
| Routing | `mcp.feedling.app` DNS → Caddy on VPS `[retired VPS IP redacted]` (A record at `37bec2c25ad8959659dcc14c244fce4e` zone, DNS-only, not proxied) → reverse-proxies to `-5002s.dstack-pha-prod5.phala.network` with gateway SNI. Claude.ai / Claude Desktop clients see a CA-valid Caddy cert for `mcp.feedling.app`; audit-aware clients can pin directly against the attested pubkey fingerprint via the `-5002s.` path. |
| Secrets | `CF_ZONE_ID` + `CF_API_TOKEN` injected via `phala deploy -e KEY=VALUE` (encrypted env channel, not baked into compose_hash). Token scope: `Zone:DNS:Edit` for `feedling.app` only. |
| Purpose | First Feedling deployment where the MCP-port cert is a real CA-signed LE cert (not self-signed dstack-KMS) whose private key is provably inside the TDX enclave. Agents (Claude.ai / mobile MCP clients) get a cert their OS trusts out of the box AND auditors can verify the pubkey is enclave-bound. `mcp.feedling.app` is now end-to-end trusted without trusting the gateway operator on the audit-aware path. |
| Retired by | Phase D deploy below. |

### Phase E migration — pure-CVM, ingress-terminated TLS (running, 2026-04-22)

**Status**: prod9 is live. The VPS split was retired; production now runs
from the single CVM described in **Production CVM (prod9, current)** above.

| | |
|---|---|
| Provider | Phala Cloud dstack on node `prod9` — ONLY gateway that supports `_dstack-app-address.<domain>` TXT routing (prod5/prod7 don't). |
| Name | `feedling-enclave-v2` (new CVM → new app_id → new on-chain authorization required). |
| App ID | `9798850e096d770293c67305c6cfdceed68c1d28` |
| CVM ID | `0711c9a4-afdc-40c6-ba49-d8cb95f7e850` |
| Compose | `deploy/docker-compose.phala.yaml` — at the Phase-E writeup, 4 services: `ingress` (dstack-ingress 2.2 multi-domain, HAProxy-based), `enclave` (decrypt + attestation, own TLS on :5003), `backend` (Flask HTTP + WS ingest), `mcp` (FastMCP SSE, plain HTTP behind ingress). ⚠️ **Historical snapshot** — `mcp` was removed 2026-06-12 and `backend` is FastAPI/ASGI since 2026-07; see **Production CVM (prod9, current)** above for the live service set. |
| Compose_hash at Phase-E writeup | `0xf09f1ddc41a5fc1b5ee434f1a7beafbefba880b93bcad33582ac64ad5f14bc09` (from `/attestation`, 2026-05-18; build `b1e72a6`). ⚠️ **Historical — this is the value as of the Phase E writeup, NOT current.** Live prod9 is now compose `0x0f136ba9…` / build `22b0ed6` (2026-07-02) — see the **Production CVM (prod9, current)** table at the top of this file for the live values. |
| TLS termination | **Migrated**: mcp.feedling.app + api.feedling.app are terminated by `dstack-ingress` inside the CVM (LE certs issued via CF DNS-01, `CLOUDFLARE_API_TOKEN` injected via `phala deploy -e`, not in compose_hash). `enclave` service still terminates its own TLS on :5003 (reached via `-5003s.` passthrough) — iOS audit card Row 7 still pins `sha256(cert.DER)` to REPORT_DATA. WS ingest on :9998 stays gateway-TLS with FrameEnvelope v1 app-layer crypto. |
| MCP pubkey pin (Phase C.2) | **Retired**: `FEEDLING_MCP_TLS_IN_ENCLAVE=false` on the enclave service, so `mcp_tls_cert_pubkey_fingerprint_hex` is empty. iOS audit card shows the existing "Pre-Phase-C.2 deployment" disclosure row. Content-layer envelope crypto (enclave_content_pk) remains the real trust boundary for reads/writes. |
| VPS | **Decommissioned**: `deploy-vps` CI job deleted; `api.feedling.app` + `mcp.feedling.app` DNS moved off the retired host and onto dstack-gateway/ingress. Prod user re-onboards from scratch per 2026-04-21 user direction (no v0→v1-style migration path). |
| iOS | `testapp/FeedlingTest/CVMEndpoints.swift` centralizes URL construction via `appId` + `gatewayDomain`; compiled defaults now point at prod9. |
| On-chain | compose_hash is auto-published on Eth Sepolia by the `deploy-cvm` CI job after each CVM deploy. |

### Phase D TDX CVM — multi-tenant-only, envelope-only backend (superseded by Phase E, 2026-04-22)

| | |
|---|---|
| Provider | Phala Cloud (dstack-dev-0.5.8, Intel TDX) on node `prod5` (US-WEST-1) |
| Name | `feedling-enclave` (same CVM, compose updated in place) |
| App ID | `051a174f2457a6c474680a5d745372398f97b6ad` |
| VM UUID | `4386636e-1325-4b92-99d8-f2ca00befdb4` |
| Compose | `deploy/docker-compose.phala.yaml` @ commit `f3b4837` |
| Image | `ghcr.io/account-link/feedling:78b51a6` — first image where `SINGLE_USER` mode and the v0 plaintext write path are fully retired. Backend rejects plaintext chat/identity/memory writes with `400`; WS ingest drops frames without a v1 envelope silently; `/v1/content/rewrap` and `/v1/identity/nudge` HTTP endpoints removed (nudge now runs decrypt→mutate→rewrap inside MCP). `chat_bridge.py` + `feedling-chat-bridge.service` deleted. |
| Compose hash | `0xd92bcd3cb1713ffe8e152417ab46e8179510c37ceed5ae6d423c586a2cd60049` |
| TLS cert fingerprint (attestation port 5003) | `5698f0ade4bb412d6b0847a62d695138f3bbd287dc7d1dbdeb67b15dc445e5ef` — unchanged across EIGHT compose rotations. dstack-KMS per-app derivation remains load-bearing stable. |
| MCP TLS pubkey fingerprint (port 5002) | `e98665a3e94ac90a0a26453a73e16d5a569f791c181cfbc6ba98598f358cf63e` — unchanged; LE cert key is still derived from `feedling-mcp-tls-v1`. |
| MRTD | `f06dfda6dce1cf904d4e2bab1dc37063…` (unchanged — same base image) |
| On-chain entry | compose_hash `0xd92bcd3c…`: Sepolia tx `0x235f0120d6982cbf8872e927ee2e59133627177ca9d3f862554d748ac6e60c7c` (block 10696873). Every prior compose hash still `isAppAllowed()=true`. |
| Audit evidence | CLI **8/8** green (`tools/audit_live_cvm.py`) against `compose_hash=0xd92bcd3c…`. VPS flat-layout data wiped same day — prod user reinstalls fresh via `POST /v1/users/register`. |
| Purpose | First Feedling deployment where the backend has no plaintext-write path at all. There is no `SINGLE_USER` flag, no shared `FEEDLING_API_KEY`, no v0→v1 migration endpoint, and no chat-bridge daemon. Every chat message, memory entry, and identity card landing on disk is a v1 envelope wrapped for the enclave's content pk. |

## Planned

### Mainnet release log migration

- Redeploy `FeedlingAppAuth` to a mainnet environment.
- Use a fresh deployer keypair held in hardware-backed custody.
- Verify source on the relevant explorer.
- Ship an iOS update with the new pinned contract address before
  moving users to the new release log.

## How to re-run the deploy

See `deploy/BUILD.md` for the reproducible-build recipe that determines the
compose_hash you're authorizing. To deploy the contract itself:

```bash
cd contracts
cp .env.example .env       # fill in PRIVATE_KEY, RPC URL, etc.
source .env
forge script script/DeployFeedlingAppAuth.s.sol \
  --rpc-url "$RPC_URL" \
  --broadcast \
  --private-key "$PRIVATE_KEY"
```

After deploy, run `cast send` with `addComposeHash()` for your compose_hash.
Record the new address + first-tx info in the table above.

## TEE Postgres — ✅ 已开通（test + prod）

**状态更新（2026-07-18）**：`feedling-io-db-test` 与 `feedling-io-db-prod`
（2026-07-14 上线，WAL-G 备份 + 双写 + in-process 同步调度器）均已运行。
实际开通流程与踩坑记录见 `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md`——
**新开实例以那篇为准**。下列原始 runbook 清单保留作核对参考（对应
`docs/superpowers/plans/2026-07-07-tee-pg-phase0-1-infra.md` 的 Task 编号）：

- **首次 create + AppAuth**：为 feedling-pg 建独立 CVM 与独立 AppAuth 合约
  （切勿复用主 app 合约，见「新建 runner CVM 换钥」教训），授权其 compose_hash
  （Phase 0 / P1T3–T4）。
- **R2 桶 + 双钥托管**：建 WAL-G 备份桶并把两把加密钥（内容钥 + 备份钥）按托管
  流程分存（Phase 1 / P1T4）。
- **证书重签**：用 `deploy/postgres/gen-certs.sh` 重签 server/client TLS 证书，
  把 `TEE_DATABASE_URL` 的 sslmode/根证书接进后端 secrets（P1T1）。
- **restore 演练**：开通前跑一次 WAL-G 全量 restore + PITR 演练，确认备份可用
  （Phase 1 验收）。
- **Phase 1 验收清单**：走一遍 reconcile → replicate → verify（`ok==true`）
  的三段收敛，作为停 RDS gate 的硬条件（P2T7 / plan Phase 1 验收 Task）。
  verify 作为该 gate 前，先跑一遍 `python -m backend.tee_replicator run
  --table <t>`（对全部密文表）把 requeue 清空——verify 报告每张密文表的
  `requeue_backlog` 应读 0（非零只代表正常积压未收敛，不是 verify 的 bug，
  见 `tee_shadow/verify.py` 的 `_split_pending`）。

密码一律用 `openssl rand -hex`（十六进制无特殊字符）——引号 / `$` / 反引号等字符会
破坏 ensure-roles 的 SQL 与 compose 环境注入。

### 磁盘 sizing 依据（实测 2026-07-13，prod RDS）

CVM 磁盘创建时定死、事后扩容麻烦，故按「未来可能指向 prod / 长期不扩容」一次留够。

**关键：TEE 影子库 ≠ prod 逻辑大小。** prod RDS 当时 1.28 GB，但迁进 TEE PG 的
实际数据只有约 **650–700 MB**，因为最大的两块要么搬去 R2、要么明文化后缩水：

| 表 | prod RDS | 进 TEE PG | 说明 |
|---|---|---|---|
| `frame_envelopes` | 491 MB | **~10 MB** | 485 MB 内联帧体（TOAST）在 TEE 侧重加密进 R2（`frames-tee/`），PG 只留指针 |
| `chat_messages` | 291 MB | **~200 MB** | 252 MB base64 密文（TOAST）解密成明文约 ×0.75 缩水 |
| `user_logs` | 376 MB | **~376 MB** | 本就明文，逐字双写原样 |
| memory/blobs/perception/genesis 等 | ~60 MB | **~60 MB** | 明文化后量级不变 |
| `genesis_import_chunks` / `bak_*` / `model_api_*` | — | **不复制** | staging / 临时备份表 / 非 baseline 表 |

- **额外落 R2**（不占 PG 磁盘）：~485 MB 重加密帧体 + chat 文件体；WAL-G 全量备份
  也在同一套 R2 凭证下（不同前缀）。R2 用量 ≈ 帧体 + 备份历史。
- **增长率**：`user_logs`（append-only 最大明文表）实测 ~8.8 MB/天 ≈ 270 MB/月；
  连同 chat/memory，prod 级总数据增长约 350–400 MB/月（当时用户量）。

**磁盘建议**：
- 纯 test 用途（影子 test 的 ~115 MB，帧体走 R2 后更小）：**20 GB** 足够
  （数据 <100 MB，但要算 OS + postgres 镜像 ~1.5 GB + WAL + 初次批量 replicate
  临时文件 + autovacuum 前 MVCC 膨胀）。
- 想让该 CVM 未来也扛 prod 规模 / 长期不扩容：**30 GB**（prod 数据 ~700 MB、
  月增 ~400 MB、帧体在 R2 不占 PG → 约 5 年跑道，含 WAL/膨胀/OS）。**推荐 30 GB**。
- WAL：`archive_timeout=60s`，初次批量 replicate 会短时冲高 WAL，30 GB 完全吸收得住。

（RDS 实例分配磁盘无法从本地 aws cli 查——RDS 在另一 AWS 账号下，当前 IAM 用户
无权 describe。sizing 以上述实测逻辑数据量为准，不依赖 RDS 分配值。）

## TEE Redis — 待开通（test + pre + prod）

设计文档 `docs/superpowers/specs/2026-07-24-tee-redis-cvm-design.md`，
实施计划 `docs/superpowers/plans/2026-07-24-tee-redis-cvm.md`。

**当前状态**：代码已就绪，三台 CVM 尚未开通（三个 `deploy/*-redis-cvm-id.txt`
仍是纯注释 → `redis-deploy` workflow 会 fail-closed 拒绝运行，这是刻意的）。
**当前零流量**：没有任何业务代码引用 Redis，接入各自另开 spec。

| | test | pre | prod |
|---|---|---|---|
| CVM 名 | `feedling-redis-test` | `feedling-redis-pre` | `feedling-redis-prod` |
| Phala 账号 | `amiller-user` | `amiller-user` | **`sxysun`** |
| 规格 | 1 vCPU / 2 GB / 20 GB | 1 vCPU / 2 GB / 20 GB | 2 vCPU / 4 GB / 30 GB |
| `maxmemory` | 1 GB | 1 GB | 2560 MB |
| Phala API key secret | `TEST_PHALA_CLOUD_API_KEY` | `TEST_PHALA_CLOUD_API_KEY` | `PHALA_CLOUD_API_KEY` |
| 机密 secret 前缀 | `TEST_REDIS_*` | `PRE_REDIS_*` | `PROD_REDIS_*` |
| 身份模型 | `--kms phala`（无链上 AppAuth） | 同左 | 同左 |
| 部署分支 | `test` | `pre` | `main` |
| R2 前缀 | `test/redis/` | `pre/redis/` | `prod/redis/` |
| R2 凭证来源 | 复用 `TEST_PG_BACKUP_R2_*` | 复用 `TEST_PG_BACKUP_R2_*` | 复用 `PROD_PG_BACKUP_R2_*` |
| 监控 | 不监控（数据可弃） | ✅ | ✅ |

R2 桶复用 PG 备份的 `io-in-enclave-db`（`deploy/docker-compose.phala.redis.yaml`
里 `REDIS_BACKUP_BUCKET` 默认值），靠前缀隔离，**没有新建 R2 凭证** ——
`redis-deploy.yml` 直接把 `TEST_PG_BACKUP_R2_*` / `PROD_PG_BACKUP_R2_*`
三个既有 secret 注入成 `<PREFIX>_REDIS_BACKUP_R2_*`（见该 workflow「Deploy」
步骤的 env 块）。**R2 token 的 scope 必须覆盖新前缀**（`io-user-attachments`
那边踩过 token scope 不够导致 PUT `AccessDenied` 的坑——开通前找持有 R2
token 权限的人确认三个新前缀已在 scope 内，而不是等第一次备份失败才发现）。

需要新建的 GitHub secret 只有每环境这 4 个（其余都是复用 Phala token /
R2 凭证）：`<PREFIX>_REDIS_PASSWORD`、`<PREFIX>_REDIS_TLS_CERT_B64`、
`<PREFIX>_REDIS_TLS_KEY_B64`、`<PREFIX>_REDIS_BACKUP_AGE_RECIPIENT`
（`<PREFIX>` = `TEST` / `PRE` / `PROD`）。监控用的
`<PREFIX>_REDIS_HOST`、`<PREFIX>_REDIS_CA_B64` 另加（见下面 runbook 第 10 步）。

### 首次开通 runbook（每环境各跑一遍，不走 workflow）

> `redis-deploy.yml`（日常部署）平时负责构建/推送两个镜像、把
> `REPLACE_SHA` 换成真实 tag、再 `phala deploy --cvm-id <既有id>` 原地
> 更新——但它读 `deploy/<env>-redis-cvm-id.txt` 校验 cvm-id 非空后才敢跑
> （fail-closed，绝不静默新建 CVM）。首次开通时这个文件还只是纯注释，
> 所以下面第 5/6 步得手动把 workflow 做的这几件事各做一遍：构建推送镜像、
> 钉 tag、**用 `phala deploy`（不带 `--cvm-id`）一步创建并部署 CVM**——
> 装 CLI 后 `phala cvms create --help` 顶行标着
> `[DEPRECATED] (use "phala deploy" instead)`，`docs/TEE_POSTGRES_SHADOW_PROVISIONING.md`
> §2.3 走的也是这条路，本 runbook 照抄同一形态。

1. **切 Phala profile**：test/pre 用 miller 的；**prod 必须先切到 `sxysuns` profile**
   （最容易忘的一步）。
   ```bash
   PROFILE="填入 amiller-user 或 sxysuns 对应的 profile 名"   # 只有这一行需要手改，拿不准用 phala profiles 查
   phala switch "$PROFILE"
   ```
2. `deploy/redis/gen-certs.sh feedling-redis-<env> <outdir>` 生成 TLS 材料。
   **`ca.key` 立即移到离线冷存**，`server.crt`/`server.key` 走 base64 填进
   `<PREFIX>_REDIS_TLS_CERT_B64` / `<PREFIX>_REDIS_TLS_KEY_B64` secret
   （脚本自己会打印这两个可直接粘贴的值）。重跑这个脚本前如果输出目录
   已经有 `ca.key`/`ca.crt`，脚本会拒绝覆盖——这是刻意的，覆盖 CA 会让它
   已经签过的证书全部失效。
3. 生成 age 密钥对：`age-keygen -o <env>-redis-backup.key`。**私钥离线冷存**
   （按 PG 的「内容钥 + 备份钥」双钥托管流程分存），公钥填进
   `<PREFIX>_REDIS_BACKUP_AGE_RECIPIENT` secret。
4. 口令：`openssl rand -hex 32` → `<PREFIX>_REDIS_PASSWORD`。
   **必须是十六进制**：引号 / `$` / 反引号会破坏 compose env 注入。
5. **构建 + 推送镜像，把真实 tag 钉进 compose**（`redis-deploy.yml` 首次
   跑不了——它要读的 cvm-id 文件这时还是空的——这一步是手动做同一件事；
   形态照抄 `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` §2.3）：
   ```bash
   IMG_TAG="$(git rev-parse HEAD)"   # 完整 40 位 sha，与 redis-deploy.yml 的 tag 约定一致
   docker build -f deploy/redis/Dockerfile        -t "ghcr.io/teleport-computer/feedling-redis:${IMG_TAG}"        deploy
   docker build -f deploy/redis/Dockerfile.backup -t "ghcr.io/teleport-computer/feedling-redis-backup:${IMG_TAG}" deploy
   GH_USER="填入你的 GitHub 用户名"   # 需要对 teleport-computer 的 GHCR push 权限
   docker login ghcr.io -u "$GH_USER"
   docker push "ghcr.io/teleport-computer/feedling-redis:${IMG_TAG}"
   docker push "ghcr.io/teleport-computer/feedling-redis-backup:${IMG_TAG}"

   ENV_NAME=test   # 换成 pre / prod，只有这一行需要手改
   sed -e "s|feedling-redis:REPLACE_SHA|feedling-redis:${IMG_TAG}|" \
       -e "s|feedling-redis-backup:REPLACE_SHA|feedling-redis-backup:${IMG_TAG}|" \
       deploy/docker-compose.phala.redis.yaml > "compose.redis-${ENV_NAME}.yaml"
   ```
   （`compose.redis-<env>.yaml` 只是本地工作文件，不提交——仓库里
   `deploy/docker-compose.phala.redis.yaml` 继续留着 `REPLACE_SHA`，日常
   更新交给 `redis-deploy.yml` 自己 sed。）
6. **建 CVM + 首次部署，一条命令做完**（`phala deploy` 不带 `--cvm-id` 即
   为新建；`--kms phala` 走 Phala 默认 KMS 按部署账号授权，redis CVM
   **不需要链上 AppAuth**，与 TEE Postgres 同，见
   `docs/TEE_POSTGRES_SHADOW_PROVISIONING.md` §0——**切勿复用主 app 的
   AppAuth 合约**，那会翻掉主 enclave 的钥，教训见本文件「新建 runner
   CVM 换掉主 enclave 钥」条目。`--instance-type` 与上表规格的对应关系
   已用装好的 CLI `phala instance-types cpu` 核实：`tdx.small` = 1 vCPU /
   2 GB，`tdx.medium` = 2 vCPU / 4 GB）：
   ```bash
   phala deploy --name "feedling-redis-${ENV_NAME}" --compose "compose.redis-${ENV_NAME}.yaml" \
     --kms phala --instance-type tdx.small --disk-size 20G \
     -e "REDIS_PASSWORD=<第 4 步生成的口令>" \
     -e "REDIS_TLS_CERT_B64=<第 2 步 gen-certs.sh 打印的值>" \
     -e "REDIS_TLS_KEY_B64=<第 2 步 gen-certs.sh 打印的值>" \
     -e "REDIS_MAXMEMORY=1gb" \
     -e "REDIS_BACKUP_S3_PREFIX=${ENV_NAME}/redis/" \
     -e "REDIS_BACKUP_AGE_RECIPIENT=<第 3 步 age-keygen 打印的公钥>" \
     -e "REDIS_BACKUP_R2_ENDPOINT=<复用 PG 备份的 R2 endpoint>" \
     -e "REDIS_BACKUP_R2_ACCESS_KEY_ID=<复用 PG 备份的 R2 access key>" \
     -e "REDIS_BACKUP_R2_SECRET_ACCESS_KEY=<复用 PG 备份的 R2 secret key>" \
     --wait
   # prod 换成 --instance-type tdx.medium --disk-size 30G、REDIS_MAXMEMORY=2560mb。
   # 记下输出里的 CVM ID 与 App ID —— 下一步和后面冒烟/restore 演练都要用。
   ```
7. 把上一步输出的 `cvm_id` 写进 `deploy/<env>-redis-cvm-id.txt` 并提交
   （之后 `redis-deploy.yml` 的日常更新才认得到这台 CVM）。
8. 冒烟（占位值先设成变量——直接把 app_id 写进命令行时 `<` `>` 会被
   shell 当成重定向）：
   ```bash
   APP_ID="填入本环境 app_id"          # 只有这一行需要手改
   REDIS_HOST="${APP_ID}-6379s.dstack-pha-prod9.phala.network"
   read -rs REDIS_PW                   # 交互输入，不进 shell history
   REDIS_CA_FILE=./ca.crt REDISCLI_AUTH="$REDIS_PW" \
     ./deploy/verify-redis.sh "$REDIS_HOST" 443
   ```
   期望最后一行 `[verify] ALL GREEN`。
9. **restore 演练（硬 gate，不做完不算开通）**：本地先跑
    `./deploy/redis/e2e-drill.sh` 把整条链路（写入 → 快照 → 加密 → 上传 →
    下载 → 解密 → 校验 RDB 魔数）在本地过一遍；再对着**真实环境的 R2 前缀**
    重放一次 `restore.sh`，确认离线身份文件真的能解密这个环境实际推上去
    的快照（本地 drill 用的是本地生成的 age 身份，验证不了「离线冷存的这
    把私钥能不能用」这件事，必须单独补这一步）。

    `backup` 服务刻意不挂 `redisdata` 卷（快照走 `redis-cli --rdb`，sidecar
    从不直接读卷内文件，见 `deploy/redis/backup-push.sh` 的 D4 注释）。因此
    `restore.sh` 不是进现有容器里跑，而是从 `feedling-redis-backup` 镜像
    另起一个一次性容器，显式挂上 `redisdata` 卷和离线身份文件。

    **⚠️先停生产 `redis` 服务，并确认它真的停了——这一步必须排在下面任何
    命令挂载 `redisdata` 卷之前，顺序不能反**：这台 CVM 上的 `redis`
    服务这时如果还在跑，它自己的定时 `save` 触发的 BGSAVE 会拿它当前
    （空的，或者是故障前残留的）数据集去覆盖你即将恢复出来的
    `dump.rdb`——这正是下面「AOF 会抢占加载」那段解释的同一个覆盖机制，
    只是这次由还在运行的生产容器自己触发；`redis-aof-repair` 临时容器
    同样会跟一个仍在写入的生产容器共享同一份 `/data`，谁的写落在后面纯
    属运气。这台 CVM 上跑：

    ```bash
    # docker compose 项目名 feedling-redis + 服务名 redis 的默认容器名，
    # 拿不准就用 phala ps <APP_ID> 或 docker ps 核实实际名字。
    docker stop feedling-redis-redis-1
    # 确认已经停了——期望没有任何一行输出；看到 "Up ..." 状态说明还没
    # 停干净，不要往下走。
    docker ps --filter name=feedling-redis-redis-1 --format '{{.Names}} {{.Status}}'
    ```

    确认停妥之后才继续：

    ```bash
    # 用当前 Dockerfile.backup 在本地重建同一份镜像（不确定线上具体
    # tag 时最简单可靠的做法；context 必须是 deploy/，同 CI 的构建方式）：
    docker build -f deploy/redis/Dockerfile.backup -t feedling-redis-backup:local deploy

    ENV_NAME="填入本环境：test / pre / prod"        # 只有这几行需要手改
    IDENTITY_FILE="填入离线冷存身份文件的本地路径"
    VOLUME_NAME="feedling-redis_redisdata"           # compose 默认命名；
                                                      # 拿不准就在 CVM 上
                                                      # docker volume ls | grep redisdata 核实
    R2_ENDPOINT="填入本环境 R2 endpoint（同 REDIS_BACKUP_R2_ENDPOINT secret 的值）"
    R2_ACCESS_KEY_ID="填入本环境 R2 access key"
    R2_SECRET_ACCESS_KEY="填入本环境 R2 secret key"

    docker run --rm \
      -v "${VOLUME_NAME}:/data" \
      -v "${IDENTITY_FILE}:/id.txt:ro" \
      -e REDIS_BACKUP_AGE_IDENTITY_FILE=/id.txt \
      -e REDIS_BACKUP_BUCKET=io-in-enclave-db \
      -e REDIS_BACKUP_S3_PREFIX="${ENV_NAME}/redis/" \
      -e AWS_ENDPOINT_URL="${R2_ENDPOINT}" \
      -e AWS_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID}" \
      -e AWS_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY}" \
      -e AWS_REGION=auto \
      -e AWS_DEFAULT_REGION=auto \
      --entrypoint restore.sh feedling-redis-backup:local
    ```

    （`AWS_REGION` / `AWS_DEFAULT_REGION` 必须都设成 `auto`：backup 镜像
    没内置任何 AWS 配置，aws CLI 在客户端侧校验 region 时不认
    `--endpoint-url` 指向的是 R2，缺这两个变量会在发出请求前就报一个
    与 R2/网络无关的签名错误——同 `redis-monitor.yml`、`backup` 服务、
    `e2e-drill.sh` 里的同一条注释。）

    省略末尾的 object key 参数会自动列出该前缀下全部快照并取最新一份
    （key 形如 `redis-20260724T110000Z.rdb.age`，字典序=时间序）。脚本会
    把解出的 RDB 写到卷内的 `dump.rdb`。

    **⚠️AOF 会抢占加载，而且比"清掉 appendonlydir/ 就行"要棘手得多**：
    `redis.conf` 里 `appendonly yes` 是硬编码的（不可改，见下方「已知
    限制」）。只要目标卷里没有 `appendonlydir/`——不管是因为它本来就没
    有，还是被人清空过——Redis 8 在 `appendonly yes` 下走的都是"AOF 为
    空 = 数据集为空"这条路径，**根本不会去看 `dump.rdb`**。已用本仓
    pinned image（`redis:8-alpine@sha256:9d317178eceac8454a2284a9e6df2466b93c745529947f0cd42a0fa9609d7005`）
    配这份真实 `redis.conf` 实测复现：卷内只有刚恢复出的 `dump.rdb`、没有
    `appendonlydir/` 时启动生产容器，日志打 `BGSAVE done, 0 keys saved`，
    `DBSIZE` 是 0——**恢复被无声吞掉，且下一轮定时 `save` 触发的 BGSAVE
    还会把这份空快照写回去覆盖掉 `dump.rdb`**，之后连重试都没有原始数据
    可用了。也就是说，「卷本来就是全新的，不用管 `appendonlydir/`」这个
    判断本身就是错的——全新卷同样必须走下面这套转换步骤。

    教科书式修法（先用 `appendonly no` 起服务让 RDB 加载，再
    `CONFIG SET appendonly yes` 让 Redis 用内存里的数据重建 AOF）在这台
    机器上也用不了：`redis.conf` 用 `rename-command CONFIG ""` 把 `CONFIG`
    命令禁掉了（`redis-monitor` 的 monitor 测试断言它必须保持禁用，见下方
    「已知限制」），生产容器里发不出 `CONFIG SET`。直接换一种思路——用
    **不带这层加固的临时容器**（就是 `e2e-drill.sh` 校验环节用的同一个
    裸官方 pinned image，不是 `feedling-redis` 镜像）把 `dump.rdb` 转成
    合法 AOF，再把卷交还给生产容器。**这套顺序已经用本仓 pinned image +
    真实 `redis.conf` 端到端跑通**（51 个已知 key 的合成数据集，从
    restore.sh 落盘 → 临时容器转 AOF → 生产容器加载，最终生产容器
    `DBSIZE=51` 且抽样 key 逐字节一致；生产容器重启第二次依然
    `DBSIZE=51`，证明落地的是合法持久化而非侥幸的一次性加载）：

    ```bash
    # 1) 临时容器（无鉴权、无 TLS、CONFIG 未被禁用）挂上同一个 redisdata
    #    卷（$VOLUME_NAME 沿用上面 restore.sh 那一步已经设好的同一个变量，
    #    同一个 shell 会话里接着跑，不用重新填），按 restore.sh 刚写好的
    #    dump.rdb 启动。只用官方 pinned image，不构建/不使用
    #    feedling-redis 镜像——这是刻意的，生产镜像里的 redis.conf 硬编码
    #    appendonly yes 且 CONFIG 已被禁用，这两点都是这一步需要绕开的。
    docker run -d --name redis-aof-repair \
      -v "${VOLUME_NAME}:/data" \
      redis:8-alpine@sha256:9d317178eceac8454a2284a9e6df2466b93c745529947f0cd42a0fa9609d7005 \
      redis-server --appendonly no --dir /data --dbfilename dump.rdb --save ""

    # 2) 确认 RDB 真的加载进来了——期望值是故障前的已知 key 数，不是 0：
    docker exec redis-aof-repair redis-cli DBSIZE

    # 3) 用这个临时容器专属的 CONFIG（生产容器里没有这条命令）触发 AOF
    #    重建，Redis 会用当前内存数据集生成一份新的 base AOF：
    docker exec redis-aof-repair redis-cli CONFIG SET appendonly yes

    # 4) 等重建真正做完再往下走——没等就关容器会留下不完整的 AOF：
    docker exec redis-aof-repair redis-cli INFO persistence \
      | grep -E 'aof_rewrite_in_progress|aof_last_bgrewrite_status'
    #   必须看到 aof_rewrite_in_progress:0 且 aof_last_bgrewrite_status:ok，
    #   没达到就再等几秒重查这两行，不要提前进第 5 步。

    # 5) 优雅关闭并清理临时容器（NOSAVE 只是跳过一次多余的 RDB SAVE——
    #    AOF 已经落盘，这一步不会丢数据；但从这里开始到第 6 步之间不要
    #    再对这个卷做任何写入）：
    docker exec redis-aof-repair redis-cli SHUTDOWN NOSAVE
    docker rm -f redis-aof-repair
    ```

    跑完这五步，卷里会多出 `appendonlydir/`（`appendonly.aof.<n>.base.rdb`
    就是转换出来的完整数据集，配一份 `.manifest` 和一份空的
    `.incr.aof`）。

    **6) 这之后才能启动真正的生产 `redis` 服务**（该环境正常的部署方式——
    走 `phala deploy` 或本地 `compose up redis`，取决于事故现场）。生产
    服务会像正常冷启动一样加载这份 AOF（日志会打 `keys loaded: <N>`，`N`
    应等于上面第 2 步的 `DBSIZE`），全程没有碰过 `rename-command CONFIG ""`
    这道加固：`CONFIG SET` 从头到尾都是在上面那个临时容器里发的，生产
    容器完全不需要、也确实用不了 `CONFIG`。

    **7) 验证（必过，不是可选项）**：跑一遍第 8 步冒烟连上生产实例，
    `DBSIZE` 必须等于故障前的预期 key 数，并额外挑 1-2 个已知业务 key
    `GET` 出来核对值——**只看容器"起来了"或健康检查过了不能证明数据是
    对的，这正是这条 runbook 曾经出错的地方（生产配置在这个场景下会正常
    启动、正常回应 PING，但数据集是空的）**。`DBSIZE` 或抽样值对不上，
    立即停止宣布恢复完成，回头检查是否跳过了上面第 1-5 步、或步骤 3/4
    在重建完成前就被打断。

    这一步是硬 gate：没跑通对真实 R2 前缀 + 离线身份文件的恢复，这个环境
    不算开通完成。
10. 把 `<app-id>-6379s.…` 主机名填进 `<PREFIX>_REDIS_HOST`、`ca.crt` 的 base64
    填进 `<PREFIX>_REDIS_CA_B64`（`redis-monitor` workflow 要用；prod/pre 才需要，
    test 不监控可跳过）。
11. 手动触发一次 `redis-monitor` workflow，确认全绿。

### 已知限制

- **Redis 端口在公网可达**。dstack CVM 之间没有私网，跨 CVM 只能走 gateway
  passthrough `<app-id>-6379s.…:443`，只靠 TLS + AUTH 保护。TEE Postgres 现在
  也是这个模型。
- **`CONFIG` 命令被禁用**：查容量只能 `INFO memory`，不能 `CONFIG GET maxmemory`。
- **单实例无 HA**：实例故障需人工恢复，RPO ≤1h 由备份保证（小时快照留最近
  24 份，每日 03:00 UTC 那份额外留 7 天）。
- **prod 账号余额**：test 的老 CVM 就是在 `sxysun` 账号下余额耗尽被废弃
  （2026-06-18，app_id 报废 + 内容钥全换）。多一台 CVM 多一份烧钱速率。
