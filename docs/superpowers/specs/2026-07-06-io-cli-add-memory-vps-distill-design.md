# io_cli `add-memory` — VPS 侧二次蒸馏 (design)

Date: 2026-07-06
Owner: hx (verb + tests); skill 文案 also hx
Repo: feedling-mcp (`tools/io_cli.py`)

## 问题

常驻（VPS）agent 的工具带 `io_cli` 今天有记忆**读**（`memory-index` / `memory-fetch`）和
identity **直改**（`identity-write`：patch self_introduction / signature），但**没有把一份文件
蒸馏进记忆/身份的能力**。

同样的二次蒸馏在 iOS 上已经有三个入口（onboarding / IdentityMaterialSheet / GardenMaterialSheet），
全部走 `POST /v1/genesis/imports/plaintext`。该路由 + 蒸馏管线（`genesis/plaintext.py` 的
`_run_plaintext_add_memory_job` / `_run_plaintext_update_identity_job`）是**主进程内 daemon 线程**，
在 cloud CVM 和自托管 VPS 上跑的是同一份代码。缺的只是一个让常驻 agent 触发它的 CLI verb。

## 目标 / 非目标

- **目标**：给常驻 agent 一个 `io_cli add-memory` verb，把一份文件喂进已有 genesis 蒸馏管线，
  产出跟 app 完全一致的结构化记忆卡 / identity 更新（去重、结构化，一条管线三个入口）。
- **非目标**：不改后端蒸馏逻辑；不做目录 watcher / 后台常驻进程；不做服务端自动分类
  （memory vs identity 由 agent 从内容判断 —— agent-first）；不碰 `identity-write`（那是直改，不是蒸馏）。

## 设计决策（已确认）

1. **复用 genesis 管线**，后端零改动。
2. **agent 主动调 verb**，无 watcher —— 文件本就 drop 进 agent 工作目录，agent Read 后调 CLI。
3. **agent 声明** memory vs identity（`--as memory|identity`，默认 memory）；不建服务端分类器，
   不猜文件名。
4. memory 模式内部不区分 history / support material —— `add_memory` job 两者都蒸成记忆卡，
   统一当 memory（support material）送。

## 接口

```
io_cli add-memory --file <path>          # 读文件文本
io_cli add-memory --text "<inline>"      # 或内联；二选一，都没给且非 tty 时读 stdin
  [--as memory|identity]   # 默认 memory
  [--no-wait]              # 只提交，立即返回 job_id，不轮询
  [--timeout <sec>]        # 轮询上限，默认 120
```

输出 JSON 到 stdout（agent 解析）：
- 完成：`{"ok": true, "status": "done", "job_id": "...", "memories_created": <int>, "as": "memory"}`
- 超时未完成：`{"ok": true, "status": "pending", "job_id": "...", "as": "memory"}`
- 失败 / HTTP 错误：`{"ok": false, "status": "failed"|..., "job_id": "...", "error": ...}`

## 流程

1. 取输入文本：`--file` 读文件（basename 作 filename），或 `--text`，或 stdin。空 → `{ok:false, error:"empty_input"}`。
2. 拼 payload（复用 `_http_json` + `_auth_headers`，base = `FEEDLING_API_URL`）：

   `--as memory`（默认）：
   ```json
   {"format":"auto","content":"","fresh_start":false,"mode":"add_memory",
    "memory_summary_content":"<text>","memory_summary_filename":"<name>",
    "client_job_id":"vps-add-memory-<uuid>"}
   ```
   `--as identity`：
   ```json
   {"format":"auto","content":"","fresh_start":false,"mode":"update_identity",
    "ai_persona_content":"<text>","character_content":"<text>",
    "ai_persona_filename":"<name>","character_filename":"<name>",
    "client_job_id":"vps-update-identity-<uuid>"}
   ```
   > 字段名对齐 iOS `uploadGenesisPlaintext` + 服务端 `genesis/plaintext.py`：memory 走
   > `memory_summary_content`，identity 走 `ai_persona_content`/`character_content`；`mode` 显式设置
   > （不依赖 client_job_id 前缀推断）。
3. `POST {FEEDLING_API_URL}/v1/genesis/imports/plaintext` → 取 `body.job.job_id || body.job_id`。
4. `--no-wait` → 直接回 `job_id`。否则轮询 `GET /v1/genesis/imports/{job_id}`，读 `job.status`
   （`done`/`failed`/`processing`）；`done` 回 `memories_created`（best-effort，从 job body 取），
   `failed` 回错误，超时回 `pending`+job_id。轮询间隔 ~2s。

## 防跑偏 / 确认机制（分级，纯 skill 文案层，零代码）

"跑偏"分两类，风险不对称：
- **分类错**（memory ↔ identity 判反）不对称：人设误当 memory → 几条怪但无害、可删的卡；
  记忆误当 identity → **污染人设、改 companion 行为**，影响大。危险只在 identity 一侧。
- **内容错**（蒸馏抽歪）：app 的 Garden/Identity 上传本就没预确认，蒸馏是结构化抽取+去重、非
  raw 写入，已有约束。

现有兜底与 app 对齐：memory 卡在 Garden **可见/可编辑/可删**（soft supersede）；agent 拿到
`memories_created` 后可**回报清单**，用户当场可撤。

**分级策略（verb 保持哑，全在 skill 文案约束 agent 行为）：**
- **memory**：不硬拦。agent 写完**回报"记了啥"**，用户可事后一句话删。与 app 一致。
- **identity**：**写前先确认**。agent 先向用户复述打算写入的人设改动，用户点头才调
  `--as identity`。理由：改人设影响大 + 分类误判的唯一危险下家就是它。

不引入 `--dry-run` / 服务端预览（那需拆"蒸馏/落库"、破坏零改动）。

## 依赖 / 风险

- **网络出口**：需要 agent 沙箱能到本地 API，跟 `memory-index`/`memory-fetch` 同一要求。
  memory 读能用 → 这个就能用（已知坑：agent 沙箱禁网会让 io_cli 打不到 API）。
- **enclave**：蒸馏落库经 enclave；VPS 上是 simulator 模式，需 `FEEDLING_ENCLAVE_URL` 已配。
- **auth**：`FEEDLING_API_KEY` 或 `FEEDLING_RUNTIME_TOKEN_FILE`，`_auth_headers` 已覆盖。

## 测试

- 单测（mock `_http_json`）：memory 模式 payload 字段正确、identity 模式字段正确、job_id 提取、
  `--no-wait` 不轮询、轮询 done→memories_created / failed→ok:false / 超时→pending、空输入报错。
- 复用现有 `tests/test_genesis_plaintext_routes.py` 覆盖服务端行为，本 verb 只测客户端拼包 + 轮询。

## 交接

- 代码（`tools/io_cli.py` verb + `tests/`）：hx。
- VPS 常驻 skill 里"何时用 add-memory"那句文案：hx 起草并放置（本 spec 附草稿供参考）。

### skill 文案草稿（供 hx 放置）

> **记住一份文件** — 用户给了你一份文件让你记住/吸收时，用
> `io_cli add-memory --file <path>`。先读文件判断内容：
> - 事实/记忆（偏好、经历、近况…）→ 默认 `--as memory`。**写完把记了哪几条回报给用户**
>   （用户可让你删）。不必写前确认。
> - 人设/身份画像（希望 AI 怎么说话、性格、自称…）→ `--as identity`。**这会改你自己的人设、
>   影响大：写前先向用户复述打算改成什么，得到确认再调**。
>
> 工具蒸馏+去重后写入，返回 `memories_created`。
> 改 self_introduction/signature 用 `identity-write`，不是这个。
