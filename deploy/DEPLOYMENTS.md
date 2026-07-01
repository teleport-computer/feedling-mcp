# Feedling deployment records

Canonical record of deployed artifacts. Entries accumulate as we move through
the phases. Historical operational identifiers may be redacted after
retirement when keeping the exact value no longer helps verification.

## Live services

### Production CVM (prod9, current)

| | |
|---|---|
| Provider | Phala Cloud dstack on prod9 (`dstack-pha-prod9.phala.network`) |
| CVM ID | `0711c9a4-afdc-40c6-ba49-d8cb95f7e850` |
| App ID | `9798850e096d770293c67305c6cfdceed68c1d28` |
| Instance ID | `6fe9b54c9f2b428158c3e74de615d0f0a0c457ba` |
| Compose | `deploy/docker-compose.phala.yaml` — `ingress`, `backend`, `mcp`, `enclave` |
| Current image | `ghcr.io/teleport-computer/feedling:b1e72a6` |
| Live git commit | `b1e72a6404560f3cbde72e62f7a0f97950c8fd7b` |
| Live built at | `2026-05-16T05:55:43Z` |
| Live compose hash | `0xf09f1ddc41a5fc1b5ee434f1a7beafbefba880b93bcad33582ac64ad5f14bc09` |
| Public API | `https://api.feedling.app` via `dstack-ingress` |
| Public MCP | `https://mcp.feedling.app/sse?key=<api_key>` via `dstack-ingress` |
| Attestation | `https://9798850e096d770293c67305c6cfdceed68c1d28-5003s.dstack-pha-prod9.phala.network/attestation` |
| WS ingest | `wss://9798850e096d770293c67305c6cfdceed68c1d28-9998.dstack-pha-prod9.phala.network/ingest` |
| TLS model | `api.feedling.app` + `mcp.feedling.app` terminate at `dstack-ingress`; `/attestation` keeps its own dstack-KMS-derived TLS on `:5003` for iOS pinning. |
| MCP pubkey pin | Retired in prod9 architecture: `mcp_tls_cert_pubkey_fingerprint_hex` is empty by design; content-layer envelopes sealed to `enclave_content_pk` are the privacy boundary. |
| Deploy path | GitHub Actions `deploy-cvm` pins the GHCR image tag, deploys this CVM via Phala, then publishes the live dstack-computed compose hash on Sepolia. |

### Test CVM (prod9, `test` branch)

| | |
|---|---|
| Provider | Phala Cloud dstack on prod9 (`dstack-pha-prod9.phala.network`, node id `18`). **Account: `amiller-user` (amiller-users-projects)** since the 2026-07-01 account move — see below. |
| CVM ID | `5bfa1543-c5b4-42ca-842d-fd88984e5edf` (also in `deploy/test-cvm-id.txt`) |
| App ID | `173c7f49aeb54acb424676b17b17f78e5e2b2938` |
| Created | 2026-07-01 as `feedling-io-test`, instance `tdx.small`, **Phala KMS** (prod9 chain-0). Account migration (path B): the old test CVM `19b13ebe-d12e-4d19-97d1-6cf41389b663` / app_id `bb9716955423faed3508888e7c654ff46f5f0c2d` under `sxysun` was abandoned (balance exhausted 2026-06-18). Fresh app_id → new `enclave_content_pk`, so the reused test RDS was wiped of undecryptable rows. iOS test build repointed to the new app_id. Bootstrapped via the one-shot `.github/workflows/bootstrap-test-cvm.yml` (push to `bootstrap-cvm` branch). CI deploy key is now `TEST_PHALA_CLOUD_API_KEY` (separate from prod's `PHALA_CLOUD_API_KEY`). |
| Compose | `deploy/docker-compose.phala.test.yaml` — same 4 services as prod, with test domains + `_test` volumes |
| Public API | `https://test-api.feedling.app` (via dstack-ingress — live, `/healthz` 200) |
| Public MCP | `https://test-mcp.feedling.app/sse?key=<api_key>` (via dstack-ingress — live, SSE 200) |
| Database | Dedicated test RDS `feedling-mcp-test-t4g-micro.cgh0oucoe0x9.us-east-1.rds.amazonaws.com:5432/postgres` — fully isolated from prod (separate instance → separate `enclave_content_pk` self-consistent, no shared schema). Injected via `TEST_DATABASE_URL`. |
| On-chain | **Separate** Sepolia FeedlingAppAuth `0x9AC034AAEf6Bb80690Be4d1f698b51796Bb7F2D5` (owner = the `ETH_DEPLOYER_KEY` address `0xa0eBcd26…`, so the CI `addComposeHash` is authorized), kept apart from prod's contract so the prod release log stays clean. Address lives in repo var `TEST_FEEDLING_APP_AUTH_CONTRACT`. Each `deploy-test-cvm` run publishes the live compose_hash here, fail-loud, same as prod. Deployed 2026-06-09 via a one-shot `workflow_dispatch` (since removed). |
| Deploy path | GitHub Actions `deploy-test-cvm` job (in `ci.yml`) on push to the `test` branch. Mirrors prod but targets the test compose / CVM / DB / contract and is branch-gated to `refs/heads/test`. |
| First-boot note | The CVM was first created 2026-06-09 WITHOUT a CF token (to mint the app_id quickly), so `dstack-ingress` couldn't issue the `test-*.feedling.app` LE certs initially. The `test`-branch CI deploy injects `CF_*` from GitHub secrets — domains + certs are now live. Backend also needed the test RDS reachable from the CVM (Publicly accessible + SG inbound 5432) before it stopped crash-looping. |
| iOS | The iOS app source is not in this repo. Point its test build at app_id `bb9716955423faed3508888e7c654ff46f5f0c2d` + gateway `dstack-pha-prod9.phala.network` + test contract `0x9AC034AAEf6Bb80690Be4d1f698b51796Bb7F2D5`. |

### agent-runner (hosted agent-runtime) — 4th CVM service

The `agent-runner` service runs `backend/agent_runtime/supervisor.py`: a
multi-tenant supervisor that hosts the resident consumer
(`tools/chat_resident_consumer.py`) one process per user, driving `claude` /
`codex exec` in cli mode. It's defined **inline** in both
`docker-compose.phala.yaml` and `docker-compose.phala.test.yaml` (its image
`ghcr.io/teleport-computer/feedling-agent-runner:<sha>` is built by
`docker-publish.yml` from `deploy/Dockerfile.agent-runner` and pinned by the same
CI step that pins the backend image). The standalone
`docker-compose.agent-runner.yaml` overlay is now **local-dev only** (superseded).

**Idle by default = zero behaviour change.** With `AGENT_RUNTIME_USERS` empty and
`AGENT_RUNTIME_AUTODISCOVER` unset, the supervisor spawns nobody (it idles and
re-checks each tick instead of exiting). All the knobs below flow through the
**encrypted env channel** (`phala deploy -e …`), so they are NOT baked into
compose_hash — flipping them on later needs **no on-chain re-auth**.

| Env | Purpose | Default |
|---|---|---|
| `AGENT_RUNTIME_USERS` | roster JSON `[{"api_key":"…"}]` — who to host (carries per-user keys) | empty → idle |
| `AGENT_RUNTIME_AUTODISCOVER` | also pull hosted-enabled users from the DB (intersected with the roster's creds) | off |
| `FEEDLING_RUNTIME_TOKEN_SECRET` | Stage-D: mint short-lived per-user runtime tokens (consumer drops the long-term api key) | off → consumer uses api key |
| `FEEDLING_LITELLM_ENABLE` | run the in-CVM LiteLLM gateway (codex non-openai providers). **Must match the backend's same var** (the cutover routing decision is backend-side) | off |
| `FEEDLING_LITELLM_API_KEY` | gateway bearer codex presents | — |
| `FEEDLING_HOST_ALL` | **zero-touch hosting**: every configured user (tested-ok provider, not opted out) is hosted with NO `AGENT_RUNTIME_USERS` roster — the supervisor mints a runtime token per DB-discovered user and resolves the provider key with it; the backend routes their sends to the agent-runner; a freshly-hosted user's chat gate is auto-opened via verify_loop. **Requires `FEEDLING_RUNTIME_TOKEN_SECRET` set on BOTH services** (backend verifies, agent-runner mints) — inert without it. **Must match the backend's same var.** Per-user opt-out: set that user's `agent_runtime_driver="legacy"`. | off → per-user flag still required |

CI secrets/vars (test job; `TEST_`-prefixed): `secrets.TEST_AGENT_RUNTIME_USERS`,
`vars.TEST_AGENT_RUNTIME_AUTODISCOVER`, `secrets.TEST_FEEDLING_RUNTIME_TOKEN_SECRET`,
`vars.TEST_FEEDLING_LITELLM_ENABLE`, `secrets.TEST_FEEDLING_LITELLM_API_KEY`,
`vars.TEST_FEEDLING_HOST_ALL`. Prod job uses the un-prefixed names.

**Zero-touch host-all rollout order (`FEEDLING_HOST_ALL`):**
1. First set `TEST_FEEDLING_RUNTIME_TOKEN_SECRET` (generate a random secret) so
   BOTH backend + agent-runner share it. Re-deploy `test` with `HOST_ALL` still
   off — confirms token auth wired, zero behaviour change.
2. Set `TEST_FEEDLING_HOST_ALL=1`. Re-deploy. A user who only configured an
   **anthropic** provider (NO `/v1/model_api/driver` flip, NOT in any roster) must
   now: appear in `agent_runtime_instances` (lease row), get its chat gate
   auto-opened, and reply via hosted claude on the first message.
3. Prod: same two steps (secret first, then `FEEDLING_HOST_ALL=1`), gated on your
   go — prod auto-hosting has only a global kill-switch + per-user opt-out, no
   per-user gradual ramp.

**Wedge guard (cross-service safety).** After the legacy-inline cutover the backend
routes EVERY fit-provider send to the agent-runner, so a turn wedges in
`processing` forever if no consumer is hosting. Two layers prevent silent wedges:
- **Startup** (`assert_hosting_ready`): the backend refuses to boot unless its own
  `FEEDLING_LITELLM_ENABLE` + `FEEDLING_HOST_ALL` + `FEEDLING_RUNTIME_TOKEN_SECRET`
  are set (validated in `__main__` and via `gunicorn_conf.py`'s `on_starting`).
- **Per request** (`check_supervisor_live`): the supervisor writes a global
  heartbeat to `server_config` each tick (`ts` + `host_all` + `gateway`); before
  routing a send the backend reads it and returns **503 `hosting_runtime_unavailable`**
  (with a `reason`) instead of parking the turn — when the heartbeat is missing,
  stale, or its `host_all`/`gateway` flags are off. This is what catches the case
  the startup check can't see: the **agent-runner service** crashed, or has the
  three vars set on the backend but NOT on itself. **So set all three vars on BOTH
  services** — a backend-only config still 503s every send (loudly) rather than
  hanging. Staleness window: `FEEDLING_SUPERVISOR_HEARTBEAT_MAX_AGE_SEC` (default
  90s ≈ 6 ticks); a DB read error fails **open** (routes anyway) so the guard never
  becomes its own outage.

**To start real-device testing (recommended order — least → most unvalidated):**
1. Deploy as-is (agent-runner idle). Confirms the 4th service builds + boots
   without disturbing existing users.
2. Set `TEST_AGENT_RUNTIME_USERS` to a roster with **one** test user whose
   provider is **anthropic** (→ claude, native, no gateway), leave
   `TEST_FEEDLING_LITELLM_ENABLE` off. Re-deploy `test`. Validate A0:
   onboarding/verify_loop green, chat works.
3. Add an **openai** user (→ codex native).
4. **Last**, after an offline `codex→LiteLLM→gemini/openrouter` tool-loop eval:
   set `TEST_FEEDLING_LITELLM_ENABLE=1` (it auto-includes the gateway providers in
   discovery + starts the proxy) and a gemini/openrouter test user.

**Hosted-cutover deployment prerequisites (三件套，缺一不可):**

收口后，backend cutover 无条件把配了合适 provider 且 `test_ok` 的用户路由到
agent-runner。以下三个变量必须同时设置；缺任何一个会导致全员 hang 或后端启动失败：

| 变量 | 若缺失 |
|---|---|
| `FEEDLING_LITELLM_ENABLE` | 后端 `on_starting` **启动失败 fail-fast**（`gunicorn_conf.py` 的 `assert_hosting_ready` 检查此变量）。**纯 native 部署（无 codex/gemini）也必须设**，否则后端拒绝启动。 |
| `FEEDLING_HOST_ALL` | 后端 `on_starting` **启动失败 fail-fast**；且 supervisor 不 spawn consumer，用户请求被 backend 路由到 agent-runner 但无 consumer 处理。 |
| `FEEDLING_RUNTIME_TOKEN_SECRET` | 后端 `on_starting` **启动失败 fail-fast**；且 supervisor 无法为 DB 发现的用户 mint runtime token，host-all 发现静默失败。须在 backend 和 agent-runner 两侧同时设置相同的值。 |

部署顺序：先设 `FEEDLING_RUNTIME_TOKEN_SECRET`（backend + agent-runner 共享同一 secret），
确认 token 鉴权通过、行为不变；再同步开启 `FEEDLING_HOST_ALL` 和 `FEEDLING_LITELLM_ENABLE`。

### 横向扩展 — 多节点 agent-runner

The supervisor is **multi-node ready with no per-runner index**. Coordination is
entirely via Postgres: the per-user lease (`agent_runtime_instances`, owner =
`<hostname>:<pid>`) guarantees exactly one consumer per user and lets a survivor
take over a dead runner's users after the lease TTL; the per-owner heartbeat table
(`agent_runtime_supervisor_heartbeats`, migration `0010`) records each runner
independently so the backend's `check_supervisor_live` aggregates the cluster
(any one fresh hosting runner ⇒ live; empty/all-stale ⇒ legacy-key fallback, so a
rollback/mixed fleet doesn't 503). There is **no static shard** — every runner
scans the full host-all set and races to acquire; `AGENT_MAX_CHILDREN` bounds how
many each takes so they split the load.

| Env (per runner) | Purpose | Default |
|---|---|---|
| `AGENT_MAX_CHILDREN` | steady-state per-runner capacity ceiling (0 = unlimited). **Σ across ALL runners must ≥ hosted-user count**, else some users go unserved. Distinct from `AGENT_MAX_SPAWNS_PER_TICK` (cold-start rate). | 0 |
| `AGENT_SUPERVISOR_HEARTBEAT_PRUNE_SEC` | drop dead-runner heartbeat rows older than this (each restart is a new `<host>:<pid>` owner) | 3600 |

**Form A — multiple containers, same CVM** (quickest validation): duplicate the
inline `agent-runner` service into `agent-runner-0` / `agent-runner-1` (distinct
container names ⇒ distinct owners). Same-CVM containers MAY share the
`feedling_agent_runtime*` volume so a takeover reuses the per-user home; set
`AGENT_MAX_CHILDREN` on each. Editing the compose changes `compose_hash` →
requires `addComposeHash()` on-chain. Caveat: CPU/OOM still share the main CVM.

**Form B — independent runner CVM(s)** (production scale-out, fault-isolated):
`deploy/docker-compose.phala.runner.yaml` is a standalone runner-only CVM (no
backend / enclave / ingress; 2 runner containers, each its own volume). It is its
**own dstack app** (own app-id + compose_hash + on-chain auth) sharing the main
CVM's Postgres + secrets via the encrypted env channel.

Cross-CVM reachability (the only real wiring):
- `FEEDLING_API_URL` → the main CVM's **public ingress** domain (e.g.
  `https://test-api.feedling.app`), NOT `http://backend:5001`.
- `FEEDLING_ENCLAVE_URL` → the main CVM enclave's **dstack-gateway passthrough**
  (`https://<main-app-id>-5003s.dstack-pha-prod9.phala.network`) — the same
  attested, in-enclave-TLS decrypt endpoint clients already use. The runner auths
  per-user with a short-lived Stage-D runtime token (no api key), so this is the
  existing exposure, not a new one. Confirm `/v1/envelope/decrypt` accepts the
  runtime token over this path before rollout.
- `DATABASE_URL` + `FEEDLING_RUNTIME_TOKEN_SECRET` (MUST equal the main backend's)
  + `FEEDLING_LITELLM_API_KEY` via encrypted env.

Rollout: provision the runner CVM (own dstack app) → build/pin
`feedling-agent-runner:<sha>` → set the encrypted env above → boot with
`AGENT_RUNTIME_USERS` empty / `FEEDLING_HOST_ALL` matching the main CVM →
`addComposeHash()` on-chain for the runner app. Verify: both runners appear as
distinct rows in `agent_runtime_supervisor_heartbeats`, users distribute across
owners in `agent_runtime_instances`, and killing one runner lets the other take
over its users after the TTL — all while the main CVM ingress/backend stay up.

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
| Enclave content pk | `f50c90f711e8484c7178a69657cad99944cba7c0cdeaa3cccb0388021e7d2744` — also stable across compose updates, same reason. Implication: v1 envelopes wrapped for this enclave survive compose rotations without a rewrap dance. |
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
| Enclave content pk | `f50c90f711e8484c7178a69657cad99944cba7c0cdeaa3cccb0388021e7d2744` — unchanged for the same reason. Implication stands: v1 envelopes from earlier compose states are still decryptable after this deploy. |
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
| Image | `ghcr.io/account-link/feedling:169cb6a` — adds ACME-DNS-01 client in `backend/acme_dns01.py`, CF API token env injection via Phala's encrypted channel, `/tls` dir pre-created with feedling ownership so the LE cert cache is writable |
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
| Compose | `deploy/docker-compose.phala.yaml` — now 4 services: `ingress` (dstack-ingress 2.2 multi-domain, HAProxy-based), `enclave` (decrypt + attestation, own TLS on :5003), `backend` (Flask HTTP + WS ingest), `mcp` (FastMCP SSE, plain HTTP behind ingress). |
| Current live compose_hash | `0xf09f1ddc41a5fc1b5ee434f1a7beafbefba880b93bcad33582ac64ad5f14bc09` (from `/attestation`, 2026-05-18; live build `b1e72a6`, built `2026-05-16T05:55:43Z`). |
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
