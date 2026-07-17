# Feedling 测试规范（通用）— 改了什么，就测什么

**作者**：Claude（配合 Seven）
**日期**：2026-07-12
**定位**：这是**整体规范**，不是某个功能的排查记录。任何人（Claude / Codex / 人）每完成一类改动，照这张表做对应的测试即可。思维链（CoT）只是矩阵里"E. 网关/driver"那一类的例子。

> **发版和新功能另有一套**：本文档管**每次改动**（开发循环 L0）；每次
> test→main 发版的全量回归、新功能的能力矩阵申报与跨环境（driver × route ×
> provider）E2E，见 **`docs/RELEASE_TESTING_PROTOCOL.md`**（2026-07-17 起）。

---

## 0. 三条总原则

1. **改了什么就测什么** —— 见 §2 决策矩阵，按你**动过的文件类别**对号入座，做齐"必做"项。
2. **要证据，不要感觉** —— "跑通了"不算完成；本地要有 pytest 绿、碰链路要有 E2E `OK`、上了线要有 admin trace 字段。
3. **CI 兜底 ≠ 免你自测** —— CI（§4）会替你跑一部分，但它慢、且只在 push 后。本地先跑，别把 CI 当第一道防线。

---

## 1. 三层测试手段（工具箱）

| 层 | 命令 / 工具 | 证明什么 | 成本 |
|---|---|---|---|
| **L1 本地 pytest + pyflakes** | `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py` + `python -m pyflakes backend/<包>` | 纯逻辑 + ASGI app 正确 | 秒级，每次都跑 |
| **L2 本地 E2E 真链路** | `tools/e2e_model_api_test.py`（起真后端 + enclave 模拟器，走 register→setup→send）；`tools/*_roundtrip_test.py` | 加密/账号/vendor 整条路径通 | 分钟级，碰链路才跑 |
| **L3 部署态 E2E** | test 环境发真实加密信封 → 读 `/v1/admin/data-track/debug?user_id=…`（Bearer = `~/.feedling/data-track-admin-token`） | 部署后真生效、网关/CVM 行为对 | 需部署，碰运行时行为才跑 |

> L1 判据是**「零新增失败」**（有 2 个长期红的 enclave 依赖用例，backlog #12）。

---

## 2. ★决策矩阵★（核心）

**用法**：看你这次动了哪几类文件，把对应行的"必做"全部做齐。动了多类就叠加。

| 你改动的类别 | 典型文件 | L1 pytest | L2 本地E2E | L3 部署态 | 额外必做 |
|---|---|:--:|:--:|:--:|---|
| **A. 纯后端逻辑** | `service/` `core/` `actions/` | ✅ | — | — | pyflakes；对应 `test_<域>_*.py` 补/更新 |
| **B. 新增/改路由** | `*/routes_asgi.py` | ✅ | — | ⚠️ 视情况 | PR 描述**列出路由变更**（url_map 是回归基线）；补 `test_asgi_<域>.py` |
| **C. 错误返回 / slug** | 任何返回 `{"error":...}` 的地方 | ✅ | — | — | **同 PR 登记 `docs/API_ERRORS.md`**（有守卫测试）；slug 冻结、语义变更走新 slug |
| **D. 加密 / 信封 / 账号链路** | `content_encryption.py` `model_api` setup·send、`enclave_app.py`、`/v1/envelope/*` | ✅ | ✅ **必跑** | ⚠️ 建议 | `tools/e2e_encryption_test.py` / `v1_envelope_roundtrip_test.py`；确认"服务端永不见明文" |
| **E. Provider / 网关 / driver（含思维链）** | `litellm_gateway.py` `spawners.py` | ✅（`test_litellm_gateway.py` 等） | ✅ 各 provider | ✅ **必跑** | 部署 CVM 后读 trace：`thinking_present` / `reasoning_output_tokens` / `AGENT_CLI_CMD`；**按模型家族分层验**（Anthropic/OpenAI/Gemini/中转 wire 各不同） |
| **F. 消费端 consumer / proactive** | `tools/chat_resident_consumer.py` `backend/proactive/*` | ✅（sanitize 等单元断言） | — | ✅ **必跑** | **改完必 `systemctl --user restart feedling-chat-resident`**（否则跑旧内存态）；发消息验不泄漏协议碎片 |
| **G. DB schema / migration** | 建表 / 改列 / reset 路径 | ✅（`test_*_migration.py` `test_account_reset_purges_all_tables.py`） | — | ⚠️ | prod 用户极少，clean reinstall 迁移可接受（**须任务明确授权**）；reset 必须 CASCADE 清干净 |
| **H. compose / enclave / 链上不变量** | `deploy/docker-compose*.yaml` `enclave_app.py` compose 段 | ✅ | ✅（envelope roundtrip） | — | **compose 任何字面量变更 → `compose_hash` 变 → 重新上链**（`deploy/DEPLOYMENTS.md`） |
| **I. CVM runner 镜像 / 部署** | `deploy/Dockerfile.agent-runner` bump | — | — | ✅ **必跑** | `phala inspect` 确认 image tag == 目标 hash；`deploy/verify-remote.sh`；litellm 版本没变=桥行为没变 |
| **J. iOS** | `App/FeedlingTest.xcodeproj`（+ Widget target） | Xcode build/test | — | — | **DESIGN.md token 合规**：禁裸 hex / 裸字号 / 裸字体串，用 `Color.feedling…`/`Font.feedling…`/`Spacing.*`/`Radius.*`；改 UI 前先读 DESIGN.md |
| **K. 公开文档** | io-onboarding 的 `skill.md`/`quickstart.md`/`troubleshooting.md` | — | — | — | 在 **io-onboarding repo** 改并 push（不在本 repo）；`skill.md` push 即对所有装机 app 生效、无需 rebuild；改 agent 行为要**双改**（本 repo 代码 + skill.md） |
| **L. 智能合约** | Solidity / `forge` | `forge test -vvv` | — | ⚠️ | `forge build --sizes`；部署测试合约走 `deploy-test-contract.yml`（手动） |
| **M. 多 worker 共享状态** | 引入进程内共享缓存/状态 | ✅（`test_multi_tenant_isolation.py`） | — | — | 必须接 `core/wake_bus.py` 失效广播，否则多 worker 分叉；核对库 `max_connections`（每 worker +~17 连接） |

---

## 3. 三个"何时才需要下沉一层"的判断

- **只动纯逻辑/文案** → L1 够了。
- **碰了加密、账号、信封、vendor 调用** → 必须 L2（本地真链路），因为 L1 mock 不掉 enclave 包/解。
- **碰了运行时行为**（driver 选择、网关 wire、consumer 提取、proactive 清洗、CVM 镜像）→ 必须 L3（部署态 + admin trace），因为**代码合了不等于跑着的进程/CVM 生效**。

---

## 4. CI 会自动替你跑什么（`.github/workflows/`）

推上去后 CI 跑（**别依赖它当第一道防线**）：

- **`ci.yml`**：
  - forge build/test/coverage（合约）
  - 起后端 → `tests/test_api.py --multi-tenant` → 隔离回归（`test_db.py` `test_multi_tenant_isolation.py`）→ Round 3 V2 回归
  - `docker compose build --no-cache`（`--require-hashes`）+ healthcheck
  - syntax + static（pyflakes）
- **`continuity-canary.yml`**（每日 06:17 UTC cron）：prod day-0 信封解密连续性（`tools/continuity_canary.py`）——防"某天起解不开老信封"。
- **`deploy-test-contract.yml`**（手动）：部署 FeedlingAppAuth 到 Sepolia。
- **`docker-publish.yml`**：镜像发布。

---

## 5. 部署态 E2E 标准动作（L3 展开）

1. **复用**（优先）或新建 test model_api 账号。
2. 拿账号 X25519 keypair；`whoami` 拿 `public_key`。
3. `backend/content_encryption.py::build_envelope(...)` 构造加密信封。
4. 发一条真实加密消息（`/v1/.../chat/send`）。
5. `GET /v1/admin/data-track/debug?user_id=<uid>`（Bearer admin token）。
6. 读 `agent.model.call.done.detail` 的字段验收。
   - ⚠️ stdout excerpt **1000 字节截断** → 用短 prompt 或多拉 done 事件。
   - ⚠️ 读 trace 要**过滤 `ts > 你发消息的 ts`**，别读到上一轮 proactive 的旧事件。

---

## 6. 通用坑（文档里查不到、只有做过才知道）

- **git pull ≠ 生效**：拉代码只更新文件，跑着的 Python 进程仍是旧内存态 → 改 consumer 必 `systemctl --user restart feedling-chat-resident`。
- **复用账号 config 可能是旧的**：改了网关行为要 `phala inspect` 确认 CVM 真部署了新镜像；litellm 版本没变 = 桥行为没变。
- **别一个诊断套所有模型**：必做「模型家族失败分层」（Anthropic 家族 / OpenAI·o 系 / Gemini / 中转，wire 形状与行为各异）——web_search 400 和思维链上都栽过这个。
- **driver 决定命运**：claude driver（Anthropic 家族 + DeepSeek）走原生 thinking；codex driver 有固有天花板；CLI 从 session 文件读。查前先看 `AGENT_CLI_CMD` 定 driver。
- **绝不自产假思维链**：源头不给就不展示（产品铁律）。
- **别囤测试账号**：优先复用，用完 `POST /v1/account/reset {"confirm":"delete-all-data"}`（用账号自己的 key）；新建就存 `user_id+api_key+keypair`，否则删不掉（无 admin 删除口）。

---

## 7. "完成"的定义（Definition of Done）

一个改动可以宣布完成，当且仅当：

```
[ ] 按 §2 矩阵，本类改动的"必做"测试全部做齐并通过
[ ] L1：全量 pytest 零新增失败；pyflakes 干净
[ ] 碰链路的：L2 本地 E2E 相关 provider/roundtrip = OK
[ ] 碰运行时行为的：L3 部署态 admin trace 拿到预期字段（有证据）
[ ] 动了 compose/路由集/加密路径/slug 的：PR 描述写明 + 对应登记（API_ERRORS.md / 上链）
[ ] 消费端改动：已 restart 服务并复验
[ ] asgi_app.py diff 仅装配/注入（理想零 diff）；无向上 import；无 app.py facade 引用
```

---

## 8. 一句话

**对号入座（§2 矩阵）→ 逐层拿证据（§1 工具箱）→ 满足 DoD（§7）才叫完成。** 规范的重点不是"多测"，而是"**改了哪类、就精确补哪几项、每项有硬证据**"。
