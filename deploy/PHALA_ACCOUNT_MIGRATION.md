# Phala 账号迁移 Runbook

把 Feedling 的 CVM（prod + test）从当前 Phala 账号
（`sxysun` / sxysun9@gmail.com，workspace `sxysuns-projects`）迁移到**另一个
Phala 账号**。

> 触发背景：当前账号预付费余额耗尽，Phala 在 2026-06-18 ~12:00 UTC 自动停掉了
> 账号下所有 CVM（prod `feedling-enclave-v2` + test `feedling-test`）。决定换号。

---

## 0. TL;DR + 唯一的成败手

**结论：高度可行。理想情况下零数据迁移、iOS 零改动、零重发版。**

原因（详见 §1）：用户数据的加密密钥绑定的是 `(prod9 集群 kms_root, app_id, path)`，
**与 Phala 账号无关**。Phala 账号只是给 VM 付钱的壳。只要新账号能复用**同一个
app_id**、落在**同一个 prod9 集群**、连**同一个 RDS**，新 CVM 派生出的
`enclave_content_pk` 与旧的逐字节相同 → 旧库数据照常解密。

**整件事只有一个 go/no-go（§3 Phase 0 必须先验证）：**

> Phala Cloud 控制面是否允许「账号 B」部署一个绑定到「账号 A 注册的现有 app_id /
> AppAuth 合约」的 CVM？

- 链上 + dstack-KMS 层**完全支持**（这是 dstack 的 multi-node replica 模式）。
- Phala 的 SaaS 控制面**可能**额外加了 app↔账号归属校验。无法从代码确认。

→ **先用 test 的 app_id 做一次 replica 部署试验**（Phase 0）。通过则走
**路径 A**（运维级小改动）；不通过则只能走**路径 B**（新 app_id + 全量重加密 +
iOS 重发版，重活）。

两套环境（prod / test）是**两次独立迁移**：各自独立 app_id、AppAuth 合约、RDS、
`*-cvm-id.txt`。**先迁 test 当彩排**，再迁 prod。

---

## 1. 为什么换账号 ≠ 丢数据（绑定关系）

| 资产 | 绑定在什么上 | 换 Phala 账号会变吗 | 证据 |
|---|---|---|---|
| `enclave_content_pk`（数据加密密钥） | `(kms_root, app_id, path="feedling-content-v1")` | **不变**（前提：同 app_id + 同 prod9 集群） | `backend/enclave_app.py:143-177`；DEPLOYMENTS.md:128-153（8+ 次 compose 升级密钥不变） |
| `app_id` | 链上 AppAuth 合约地址 | **不变**（前提：复用现有合约） | dstack-tutorial `03-keys-and-replication/deploy_replica.py:62` |
| 密钥释放授权 | `FeedlingAppAuth.isAppAllowed(composeHash)`，**只查 compose_hash 白名单、不绑 device** | 不受账号影响 | `contracts/src/FeedlingAppAuth.sol:145-146` |
| AppAuth owner（能加 compose_hash） | `ETH_DEPLOYER_KEY` 私钥 `0xa0eBcd26…` | 不受账号影响（你们持有该私钥） | DEPLOYMENTS.md:78 |
| 用户数据（v1 envelope，seal 给 `enclave_content_pk`） | 外部 AWS RDS，**不在 CVM 内** | 不动（库可复用） | DEPLOYMENTS.md:41 |

关键推论：`kms_root` 是 **prod9 集群级**共享的，不是账号级。所以**新账号的 CVM 必须
落在 prod9**——换集群（prod5/prod7）会换 `kms_root` → 密钥变 → 退化成路径 B。而且
只有 prod9 支持 `_dstack-app-address.<domain>` TXT 路由。

---

## 2. 迁移前要准备的东西

- [ ] **新 Phala 账号**已创建、**已充值**，并确认能在 **prod9** 节点开 `tdx.small`。
- [ ] 新账号的 `PHALA_CLOUD_API_KEY`。
- [ ] 本机 `phala` CLI 能切换/指定账号：`phala auth login` 或
      `PHALA_CLOUD_API_KEY=<新key> phala ...` / `--api-token <新key>`。
- [ ] 现有 AppAuth owner 私钥 `ETH_DEPLOYER_KEY`（路径 A 验证设备/加 compose_hash 时可能要用；
      当前是 throwaway，已在聊天记录泄露过——见 §6 安全建议）。
- [ ] 现有值备查：
  - prod app_id `9798850e096d770293c67305c6cfdceed68c1d28`，CVM `0711c9a4-afdc-40c6-ba49-d8cb95f7e850`，合约 `0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F`
  - test app_id `bb9716955423faed3508888e7c654ff46f5f0c2d`，CVM `19b13ebe-d12e-4d19-97d1-6cf41389b663`，合约 `0x9AC034AAEf6Bb80690Be4d1f698b51796Bb7F2D5`
  - prod RDS：`DATABASE_URL`；test RDS：`feedling-mcp-test-…us-east-1.rds.amazonaws.com`（`TEST_DATABASE_URL`）
- [ ] **RDS 网络**：新 CVM 的出口 IP 要能连到 RDS（RDS 须 Publicly accessible + SG 放行 5432）。
      换账号/换物理机后出口 IP 可能变——若 RDS SG 是按 IP 白名单，要加新 IP。

---

## 3. 路径 A（复用 app_id，零数据迁移）— 主路径

### Phase 0 — 验证 go/no-go（先做，别跳）

目标：证明新账号能复用 **test** app_id `bb97169…` 在 prod9 部署并拿到**相同**的
`enclave_content_pk`。

1. 记录旧 test 的 content pk（基线）。老 CVM 若已停，先在**旧账号**启动它：
   ```bash
   phala cvms start 19b13ebe-d12e-4d19-97d1-6cf41389b663      # 旧账号
   # 启动后取基线（走 dstack 网关 passthrough，注意本机若有 DNS 劫持需换网络）
   curl -sk https://bb9716955423faed3508888e7c654ff46f5f0c2d-5003s.dstack-pha-prod9.phala.network/attestation \
     | python3 -c 'import sys,json;print("OLD content_pk:",json.load(sys.stdin).get("enclave_content_pk_hex"))'
   ```
   预期 test 的 content pk 是该 app_id 自洽的一个固定值（prod 的已知是 `f50c90f7…`，
   test 因独立 RDS/独立 app_id 是另一个值——以这步实测为准）。

2. 在**新账号**用 dstack 的 "existing-app" 流程开 CVM（复用 app_id，**不铸新合约**）。
   参考 `~/Projects/teleport/dstack-tutorial/03-keys-and-replication/deploy_replica.py`
   的 `create_cvm_with_existing_app(app_id, compose_hash, app_auth_address, deployer_address)`：
   - `app_id = bb9716955423faed3508888e7c654ff46f5f0c2d`
   - `app_auth_address = 0x9AC034AAEf6Bb80690Be4d1f698b51796Bb7F2D5`（test 合约）
   - `compose_hash =` 当前 test live 的 hash（旧 CVM `/attestation` 里读，或合约 release 列表里取已 approved 的那个）
   - 目标节点：prod9

   > 若 Phala CLI/SDK 不直接暴露 existing-app 部署，用 `phala api` 走
   > `POST /cvms` 带上 `app_id` / `app_auth_contract_address`（见 tutorial 的 payload），
   > 或直接联系 Phala 支持确认跨账号复用 app_id 是否放行。

3. 新 CVM 起来后，再取一次 content pk，**与基线逐字节比对**：
   ```bash
   PHALA_CLOUD_API_KEY=<新key> phala cvms get <新CVM_UUID>          # 确认 ONLINE、prod9
   curl -sk https://bb9716955423faed3508888e7c654ff46f5f0c2d-5003s.dstack-pha-prod9.phala.network/attestation \
     | python3 -c 'import sys,json;print("NEW content_pk:",json.load(sys.stdin).get("enclave_content_pk_hex"))'
   ```
   - **相同** → 路径 A 通。继续 Phase 1。
   - **不同 / 起不来 / KMS 拒绝授权** → 路径 A 不可行，转**路径 B**（§4）。
     （若是「device 未授权」类报错，理论上本合约不绑 device，不该出现；若出现说明
     Phala/dstack 在该集群额外做了 device 校验，需用 owner key 处理或转路径 B。）

> ⚠️ Phase 0 期间若把新 CVM 指向了 **test 同一个 RDS**，新旧两个 CVM 会同时连一个库。
> 验证 content pk 不需要写库，建议 Phase 0 的试验 CVM **先不接生产 test 库**（或接一个
> 临时空库），只验密钥一致性，避免双写。

### Phase 1 — 迁 test（验证通过后）

1. **停旧 test 写入**：在旧账号停掉旧 test CVM，避免双写同一 RDS。
   ```bash
   phala cvms stop 19b13ebe-d12e-4d19-97d1-6cf41389b663          # 旧账号
   ```
2. **新 CVM 接上 test RDS**：用新账号对新 CVM 跑一次正式 `phala deploy`，注入与 CI 相同的 env
   （照搬 `.github/workflows/ci.yml:516-525` 的 test 块）：
   ```bash
   PHALA_CLOUD_API_KEY=<新key> phala deploy \
     --api-token "<新key>" \
     --cvm-id "<新CVM_UUID>" \
     -c deploy/docker-compose.phala.test.yaml \
     -e "CF_ZONE_ID=$CF_ZONE_ID" \
     -e "CF_API_TOKEN=$CF_API_TOKEN" \
     -e "APNS_KEY_P8_B64=$APNS_KEY_P8_B64" \
     -e "APNS_KEY_ID=$APNS_KEY_ID" \
     -e "APNS_TEAM_ID=$APNS_TEAM_ID" \
     -e "OPENROUTER_API_KEY=$OPENROUTER_API_KEY" \
     -e "FEEDLING_ADMIN_TOKEN=$FEEDLING_ADMIN_TOKEN" \
     -e "DATABASE_URL=$TEST_DATABASE_URL" \
     --wait
   ```
   > 这些 `-e` 值走 Phala 加密通道注入、**不进 compose_hash**，所以可自由设置、不影响密钥/授权。
3. **compose_hash 授权**：因为复用同一 test 合约且镜像不变，当前 compose_hash 应已 approved。
   若 dstack 算出的 live hash 与合约里不一致，用 owner key 补一条：
   ```bash
   PRIVATE_KEY=$ETH_DEPLOYER_KEY \
   ETH_SEPOLIA_RPC_URL=$ETH_SEPOLIA_RPC_URL \
   FEEDLING_APP_AUTH_CONTRACT=0x9AC034AAEf6Bb80690Be4d1f698b51796Bb7F2D5 \
   FEEDLING_CVM_ID=<新CVM_UUID> PHALA_CLOUD_API_KEY=<新key> \
   FEEDLING_COMPOSE_FILE=deploy/docker-compose.phala.test.yaml \
   ./deploy/publish-compose-hash.sh eth_sepolia
   ```
4. **DNS**：`test-api.feedling.app` / `test-mcp.feedling.app` 通过
   `_dstack-app-address.<domain>` TXT 路由到 app_id。**app_id 没变**，所以 DNS/CF 通常无需改动；
   确认 `dstack-ingress` 在新 CVM 上正常签发了 LE 证书即可。
5. **回归验证**（§5）：用一个老 test 账号在 iOS 上看历史聊天/记忆**能否解密显示**——
   能解 = 密钥一致 = 迁移成功。
6. **更新仓库记录**：
   ```bash
   echo "<新CVM_UUID>" > deploy/test-cvm-id.txt
   ```
   并更新 `deploy/DEPLOYMENTS.md` 的 Test CVM 表（新 CVM ID / 新账号 / 日期）。
7. **更新 CI secret**：把 GitHub `PHALA_CLOUD_API_KEY` 换成新账号的 key
   （prod 和 test 两个 deploy job 共用同一个 secret——见 ci.yml:347/500——
   所以**换 secret 会同时影响 prod 部署**，要等 prod 也迁完或两边同批切）。

### Phase 2 — 迁 prod（test 跑稳后照搬）

与 Phase 1 相同，替换为 prod 的值：
- app_id `9798850e…`，合约 `0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F`，
  compose 文件 `deploy/docker-compose.phala.yaml`，`DATABASE_URL`（prod RDS）。
- 旧 prod CVM 现在也是 stopped；迁移期间保持 stopped，避免与新 CVM 双写。
- 更新 `deploy/prod-cvm-id.txt` + DEPLOYMENTS.md 的 Production 表。
- prod content pk 基线应为 `f50c90f711e8484c7178a69657cad99944cba7c0cdeaa3cccb0388021e7d2744`
  （DEPLOYMENTS.md:129）——新 CVM 必须派生出**同一个**值才算成功。

---

## 4. 路径 B（新 app_id，全量重加密）— 退路，仅当路径 A 不可行

如果 Phase 0 证明新账号拿不到同一 app_id 的密钥，就只能新建 app_id。**代价大**：
新 `enclave_content_pk` ≠ 旧的 → 旧 RDS 里所有 `K_enclave` 解不开 → 必须重加密全量数据，
且 iOS 要改常量 + 重发版。

高层步骤（非完整命令，确需走这条再细化）：

1. **新建 app_id**：新账号 `phala deploy`（不带 `--cvm-id`，或 `phala cvms create`）→
   自动铸新 app_id + 新 AppAuth；记录新 app_id / 新合约 / 新 deploy_tx。
2. **数据重加密**（关键难点，**现有代码缺工具**，需新建）：
   - 现有 `GET /v1/content/export`（`backend/content/routes.py:479-552`）导出的是**密文**，
     不直接可用。
   - 现有 `POST /v1/content/rewrap-to-current-key`（`:322-458`）只换 `K_user`、**不换 `K_enclave`**。
   - 现实可行做法（任选）：
     - **(a) 旧 CVM 在线 → 客户端中转**：iOS 用本地 `content_sk` 解 export 的密文 →
       拉新 CVM `/attestation` 拿新 `enclave_content_pk` → 用新 pk 重新 `build_envelope`
       （`backend/content_encryption.py:91-138`）→ 通过正常写入端点灌进新 CVM。
     - **(b) 新建服务端批量端点**：`POST /v1/content/bulk-rewrap-to-key`——旧 enclave 解
       `K_enclave` → 用目标新 `enclave_content_pk` 重新 seal。需要旧、新 CVM 同时在线。
   - 覆盖范围：chat / memory / identity / **photo envelopes**（`frame_envelopes` 表，
     `backend/perception/store.py:366-367`，同样 seal 给 enclave pk）。
     普通 perception items 是明文，可直接 SQL 搬。
3. **iOS 改常量 + 重发版**：
   - `App/FeedlingTest/API/CVMEndpoints.swift`（prod/test app_id 枚举）
   - `App/FeedlingBroadcast/SharedConfig.swift`（硬编码 WS ingest 端点）
   - 若换合约：后端 env `FEEDLING_APP_AUTH_CONTRACT` + iOS 审计卡读的链上信息
   - → App Store / TestFlight 重新发版，用户必须更新才能用。

---

## 5. 验证清单（每个环境迁完都跑）

- [ ] `phala cvms get <新CVM>`：Status `running`、节点 prod9、ONLINE。
- [ ] 新 CVM `/attestation` 的 `enclave_content_pk_hex` == 旧基线（路径 A 的核心判据）。
- [ ] `curl https://test-api.feedling.app/healthz`（或 prod）返回 200。
      （注意：从被 DNS 劫持的网络测会假失败，换干净网络或走 `-5001`/`-5003s` passthrough。）
- [ ] 合约 `isAppAllowed(当前compose_hash) == true`（Etherscan 读或 `cast call`）。
- [ ] **端到端**：老用户 iOS 登录后，历史 chat / memory **能正常解密显示**（路径 A 下数据零迁移的最终证明）。
- [ ] MCP：`https://*-mcp.feedling.app/sse?key=<api_key>` SSE 200。
- [ ] 旧账号的 CVM 已 stop（避免双写）；确认无残留双写后可考虑删除。
- [ ] `deploy/*-cvm-id.txt` + `DEPLOYMENTS.md` 已更新；GitHub `PHALA_CLOUD_API_KEY` 已换新账号。

---

## 6. 回滚与安全

**回滚（路径 A）**：数据全程在同一个 RDS、未被改写，密钥也一致，所以回滚 trivial——
重新在**旧账号**启动旧 CVM（`phala cvms start <旧UUID>`，旧账号需有余额）、把
`*-cvm-id.txt` 与 `PHALA_CLOUD_API_KEY` 改回旧值即可。迁移期间**不要删旧 CVM、不要动 RDS**，
直到新环境验证稳定（建议观察数天）。

**安全建议（顺手做）**：
- `ETH_DEPLOYER_KEY` 当前是 throwaway 且已在聊天记录泄露（DEPLOYMENTS.md:83）。换账号是
  轮换它的好时机：新私钥 → 对 AppAuth 合约 `transferOwnership(新owner)`
  （`contracts/src/FeedlingAppAuth.sol:131`）→ 更新 GitHub `ETH_DEPLOYER_KEY`。
  （注意：转 owner 不影响 app_id/密钥，只影响"谁能加 compose_hash"。）
- 新账号设**自动充值 / 低余额告警**，否则会重蹈这次"余额耗尽全停机"。

---

## 7. 一句话决策树

```
新账号能复用旧 app_id 在 prod9 拿到相同 content_pk？(Phase 0)
├─ 能  → 路径 A：换 PHALA key + 新 CVM 接旧 RDS + 改 cvm-id.txt。数据零迁移，iOS 不动。  ← 期望走这条
└─ 不能→ 路径 B：新 app_id + 写批量重加密工具 + iOS 改常量重发版。重活，重新评估是否值得。
```
