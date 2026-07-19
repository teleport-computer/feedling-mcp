# 接入路线术语（Access Routes）— 全队唯一口径

> 这份是**权威定义**。事故报告、测试矩阵、support 分诊、前端文案、团队沟通
> 一律用这里的名称。改动路线语义或增删路线时，先改这份，再改代码/文案。
> Seven 2026-07-19 拍板统一。

## 一句话区分

三条路线的分界线是**两个正交的轴**，别搅在一起：

- **轴 A — agent（大脑）跑在哪**：我们的 CVM ／ 用户自己的机器
- **轴 B — 后端在谁手里**：我们的后端 ／ 用户自己跑整套 openfeedling

## 三条路线（+ 一个辅助通道）

| # | 正式名（中/英） | 代码标识 `access_mode` | agent 跑在哪 | 我们后端的角色 | 用户维护什么 |
|---|---|---|---|---|---|
| **①** | **API 托管** / Hosted (Model API) | `model_api` | **我们的** runner CVM | 全托管：跑 agent + 加解密 + 存储 + 推送 | 只填一个模型 API key |
| **②** | **自有服务器** / Your Server (Resident) | `resident` | **用户自己的机器/VPS**（跑我们的 consumer + 用户的 Claude Code 等） | 正常运行：enclave 加解密代理、密封存储、消息中继、推送。用户直连 `api.feedling.app` | 跑好官方 consumer（**git clone 我们的仓库、原样运行、自动跟车更新**） |
| **③** | **完全自部署** / Fully Self-hosted (openfeedling) | 无（不在我们系统里注册） | 用户自己的服务器 | **零角色** —— 用户把整套后端 stack 抄走自己跑，数据完全不经过我们 | 整套后端 + consumer 全自运维；版本靠自己盯 openfeedling 仓库 |
| — | **官方 App 导入** / Official App Import | `official_import` | 不跑 agent | 仅存导入的官方 App 聊天记录 | 无（辅助通道，不是完整 agent 路线） |

## 关键澄清（最容易搞混的点）

1. **② 不是"云端托管"**。`resident` 的 agent 跑在**用户自己机器**上，我们只做
   后端服务。iOS 曾把它错标成"云端托管/Cloud-hosted"（方向反了），2026-07-19 已修。
2. **② vs ③ 的分界是"后端在谁手里"**。② 的后端还是我们的 → 我们能看到 poll、
   能发自愈提醒、admin 查得到；③ 连后端都用户自己跑 → **我们完全看不见**。
3. **② 不是 fork**。是 clone 官方公开仓库 `github.com/teleport-computer/feedling-mcp`
   的 `main` 分支，原样跑，自动更新和我们发版锁步。③ 才是把整套 openfeedling 搬走。
4. **③ 已有真实普通用户**（Seven 2026-07-19：已知至少 3 个，只是没追踪）。因此
   **每次破坏性后端改动都在影响我们看不见的人**——schema 迁移、API 契约、consumer
   协议演进对他们是"生命线级"风险。CLAUDE.md 的"公开文档同步"纪律 + docs-site
   changelog 是他们唯一的升级信号。
5. **storage 轴 ≠ 路线轴**。iOS 里另有一个 `settings.storage.mode`（云端/自部署）——
   那是**内容存储后端**的选择，和这里的接入路线是不同的轴，别把 storage 的"自部署"
   当成路线 ③。（命名撞词，待前端消歧。）

## 后端已有的机器标识（勿再造新词）

- `backend/accounts/registry.py`：`ACCESS_MODES = ("resident", "model_api", "official_import")`
- `ACCESS_MODE_LABELS`（admin/API 短标签）：`resident→"Server"`、`model_api→"API"`、
  `official_import→"Official App Chat"`
- 别名归一在 `_ACCESS_MODE_ALIASES`（server/api/official… 都会归一到三个正名）

## 前端（iOS）对应文案

onboarding 选择卡（措辞已 OK，保留）：
- `resident`：「我有自己的服务器 / I have my own server」
- `model_api`：「我有模型 API Key / I have a model API key」
- `official_import`：「我只用官方 App / I only use an official app」

设置页「接入方式」短标签（2026-07-19 修正）：
- `resident`：`settings.access_modes.route.server` = 「自有服务器 / Your Server」
  （原「云端托管/Cloud-hosted」是方向性 bug，已改）
- `model_api`：新增 `settings.access_modes.route.model_api` = 「模型 API / Model API」
  （原硬编码 `"API Key"`，中文用户无翻译，已补 i18n）
- `official_import`：`settings.access_modes.route.official_chat` = 「官方聊天 / Official Chat」

## support 分诊 / 事故排查口径

用户报障时**先确认是 ①②③ 哪条**，排查路径完全不同：
- ①`model_api`：查我们 runner CVM、模型 provider key（余额/失效）。
- ②`resident`：查用户 consumer 版本/在线（admin `consumer_commit`、`last_poll_at`），
  见 [事故库 usr_f13f / stale-consumer 自愈] 与 `docs/testing/RELEASE_TESTING_PROTOCOL.md` §7。
- ③ 自部署：**我们后台查不到任何痕迹**，得让用户自己看其后端日志；先确认版本。

关联：`docs/testing/RELEASE_TESTING_PROTOCOL.md`（测试矩阵/事故库）、CLAUDE.md（公开文档同步纪律）。
