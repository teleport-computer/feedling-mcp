# 后端代码组织规范（Contributing Guide）

> 背景：2026-06-12 我们把 17,600 行的 `backend/app.py` 单体拆成了 13 个
> 领域包（见 `docs/CHANGELOG.md` 当日条目）。这份文档的目的只有一个：
> **别让它长回去。** 所有后端 PR 按此检查。
>
> **⚠️ ASGI 迁移已完结（cutover 2026-07-04，收尾 2026-07-06）：后端 web 层是
> FastAPI/ASGI（入口 `asgi_app:app`，gunicorn `-k asgi.worker.FeedlingUvicornWorker`；
> dev/子进程入口 `backend/serve_dev.py`）。路由在各领域包的 `routes_asgi.py`
> （FastAPI `APIRouter` + `register_asgi(app)`），路由体委托给框架中立的
> `*_core.py`（拿 store + 已解析参数）。迁移期的 Flask parity facade
> `backend/app.py`（符号 re-export + test-client shim 门面）已于 2026-07-06 删除；
> 测试经 `asgi_test_client.make_client()` 驱动真实 ASGI app，全仓零 flask
> （守护：`tests/test_no_flask_anywhere.py`、`tests/test_no_app_py_regression.py`）。**

---

## 分支与发布流程

`test`、`pre`、`main` 的 push 会分别触发对应环境的部署；其中 `main` 是生产
发布分支。因此，普通开发改动（包括 `feature/*`、`fix/*`、`opt/*`、`codex/*`
等分支）**不得直接向 `main` 开 PR**。

默认流程如下：

1. 开发分支向 `test` 开 PR。
2. CI 通过后合入 `test`，在 test 环境完成与改动风险相匹配的验证，并把结果记录在
   后续发布 PR 中。
3. 需要发布生产时，从已验证的环境分支向 `main` 开 PR；`main` 只接受来源为
   `test` 或 `pre` 的 PR。

仓库不强制 `test` 与 `pre` 之间的固定合并方向：可以按发布范围选择从 `test` 或
`pre` 晋级到 `main`。但不能用普通开发分支绕过 test 环境验证直接进入生产。

紧急修复仍应尽可能先经过 test 环境。确需例外时，必须由维护者明确授权，并在 PR
中记录跳级原因、风险判断和补测计划；不要在代码中通过特殊分支名或标签静默绕过
门禁。

`.github/workflows/branch-flow.yml` 的 `branch flow` 检查会拒绝不符合上述来源
要求的 `main` PR。它使用 `pull_request_target`，因此不会被 test 环境自动生成的
`[skip ci]` 部署 pin 提交跳过；workflow 只 checkout 受信任的 base commit，绝不
执行 PR head 的代码。仓库管理员还必须在 GitHub ruleset / branch protection 中：

- 禁止直接 push `main`；
- 要求通过 PR 合并；
- 把 `branch flow` 配为 `main` 的 required status check；
- 只把紧急发布负责人加入 ruleset bypass list。

本地 merge 不会触发 PR 门禁，但把本地 `main` 推到远端会进入生产发布链路，不能把
本地合并当成绕过审核的方式。

---

## 一句话版本

**asgi_app.py 只做装配（lifespan、中间件、include 路由、注入接线），业务逻辑
进领域包的 `*_core.py`（框架中立）；新路由进对应包的 `routes_asgi.py`（FastAPI
`APIRouter`，经 `run_db` 把阻塞调用移出事件循环）；依赖只能向下，向上要用注入。**

---

## 1. 包结构与「代码该放哪」

```
backend/
├── asgi_app.py     ← 装配层：lifespan、中间件、include 路由、注入接线。
│                      ❌ 禁止在这里加路由、业务函数、常量
├── core/           ← 共享内核：config / util / enclave / envelope / store(UserStore)
├── accounts/       ← 账号：registry / auth / onboarding / access / recover / routes
├── push/  screen/  ← APNs·Live Activity·推送决策 ｜ 帧存储·WS·聚合
├── proactive/      ← V2 wake：service / gate / dashboard / routes
├── identity/  memory/  ← 身份卡、记忆花园（service / actions / routes 三层）
├── bootstrap/      ← 门禁 gates + onboarding 路由
├── chat/           ← Resident 聊天条线：service / consumer / routes / verify_loop
├── agent/          ← resident agent 感知端点（routes-only，依赖 accounts/perception/proactive）
├── tracking/  admin/  content/  ← 埋点 ｜ data-track 后台 ｜ swap/rewrap/export
├── hosted/         ← Model API 托管条线（config_store / context / turn /
│                      chat_send_core+chat_routes_asgi / history_import …）
├── model_api_runtime/ ← Model API 线的 agent 运行时：tools + v2/
│                      （Runtime V2 全套：serve_worker / worker / tool_loop /
│                      jobs_store …；独立包，与 hosted/ 平级）
├── agent_runtime/  ← V1 agent-runner：多租户 resident supervisor
│                      （supervisor / spawners / leases / introduction）
├── perception/     ← 扩展感知（此模式的最早范本）
├── genesis/  worldbook/  notices/  ← 蒸馏导入 ｜ 世界书 ｜ 通知中心
├── web/  copytext/  diagnostics/  onboarding_archive/  notify_relay/
│                   ← 其余承载路由的领域包（同构：routes_asgi.py + *_core.py；
│                      完整注册表见 asgi_app._ASGI_PACKAGES）
├── capabilities/  workspace/  ← agent 能力层 ｜ Runtime V2 虚拟工作区
├── asgi/           ← ASGI 框架装配层：worker / lifespan / middleware /
│                      responses / threadpool（框架件，不放业务）
├── enclave/        ← enclave 服务实现包（enclave_app.py 薄入口指向这里；
│                      含 routes/ 子目录）
├── runtime/  tee_replicator/  tee_shadow/  ← 运行时辅助 ｜ TEE 影子库复制
├── alembic/  alembic_tee/  ← 主库 / TEE 库迁移
└── db.py · content_encryption.py · provider_client.py · provider_types.py ·
    enclave_app.py · dstack_tls.py · hosted_runtime.py · semantic_analysis.py ·
    memory_readside_core.py · memory_index_selector.py ·
    context_memory_selection.py · object_storage.py · redis_pool.py ·
    provider_attempt_ledger.py · worldbook_match.py ·
    worldbook_readside_core.py · debug_trace.py
                    ← 底层独立模块，保持无业务依赖
```

> **接入 Redis（缓存 / 锁 / 队列）**：连接池封装在 `backend/redis_pool.py`
> （用 `redis_pool.get_redis()`，别自己 new 客户端）；命名 / TTL / read-through
> 等使用规范见 **`docs/REDIS_USAGE.md`**。当前零流量，接入各自另开 spec。

**决策表——你的代码属于哪里：**

| 你要做的事 | 放哪 |
|---|---|
| 新增一个 `/v1/...` HTTP 端点 | 对应领域包的 `routes_asgi.py`（FastAPI `APIRouter` + `register_asgi(app)`，在 `asgi_app._ASGI_PACKAGES` 注册）；路由体委托给同包的 `*_core.py`，阻塞调用走 `await threadpool.run_db(...)` |
| 新增业务逻辑/存取逻辑 | 对应包的 `service.py`（或 `actions.py`，如果是 envelope-action） |
| 新增 Model API 托管线的 HTTP/存储逻辑 | `hosted/` 下对应模块 |
| 新增 Model API agent 运行时（prompt/工具/wake）逻辑 | `model_api_runtime/` 下对应模块（独立包，与 `hosted/` 平级） |
| 新增跨域共享的工具函数 | `core/util.py`（必须无业务依赖才算「共享」） |
| 新增一个完整的新功能域 | 新建包，照抄 `perception/` 的形态：`__init__.py` 提供 `register(app)`，内部 `routes.py` + `service.py` 分层 |
| 新增测试 | 仓库根的 `tests/`，**绝不放 backend/**（规则见 §6） |
| 实在不知道放哪 | 问自己「这段代码服务于哪条用户线/哪个名词」，按名词归包；**答案永远不是 asgi_app.py** |

**单文件红线**：单个模块超过 **800 行**时，PR 里必须说明为什么不拆；
超过 **1500 行**直接拆，不接受理由。

---

## 2. 依赖方向（防止退化成隐式单体）

依赖层级，**只允许从上往下 import**：

```
asgi_app.py（装配，最高）
  ↑ hosted / agent
  ↑ tracking / admin / content
  ↑ chat
  ↑ bootstrap.gates
  ↑ model_api_runtime     （自身只依赖 core/memory；被 hosted·proactive·perception 复用）
  ↑ proactive / identity / memory / perception     （identity.service 可用 memory.service，反向禁止）
  ↑ push / screen
  ↑ accounts
  ↑ core
  ↑ db / content_encryption / provider_client / dstack_tls / hosted_runtime /
     semantic_analysis / memory_readside_core / memory_index_selector /
     context_memory_selection（最低；均为无业务依赖的共享/底层模块）
  ↑ memory_garden（最低；记忆判断力内核，**不 import 任何 io 模块**，
     由 tests/test_memory_garden_purity.py 的 AST 守卫钉死）
  ↑ core（最低；模型协议层的纯共享判据：思维链剥离、
     协议残片识别。**与记忆无关** —— 聊天主链路、工具循环、主动唤醒、
     记忆落卡都在用，所以它不属于任何一个领域。只依赖标准库）
```

> `memory_garden` 是被抽出来的纯函数内核（什么值得记 / 怎么归桶 / 打分排序 /
> 要不要整理 / 解析并算 mutation）。它只依赖标准库，所以天然处在最低层，
> 被 `memory` / `genesis` / `model_api_runtime` 等上层 import。
> 加解密、身份装配、锁、审计、调模型一律不在其中 —— 那些由调用方提供。
>
> `core` 与它平级、互不依赖。方向是单向的：
> `memory_garden` → `core`，聊天链路也 → `core`。
> 第一版曾把这两个模块塞进 `memory_garden`，导致普通聊天反向依赖记忆包，
> 已在 2026-08-14 拆开。
>
> 设计见 `docs/MEMORY_GARDEN_EXTRACTION_DESIGN.zh.md`。

- `routes.py` 可以 import 平级或更低的任何 service；`service.py` 只准向下。
- **需要「向上」调用时，用注入，不用 import。** 现有范例：
  - `core/store.py` 的 `on_proactive_job_appended` 钩子（store 不能 import hosted）
  - `core/envelope.py` 的 `get_user_public_key`（core 不能 import accounts；
    lifespan 接线，测试侧由 `make_client()` 镜像）
  - `push/live_activity.py` 的 `load_identity`、`admin/data_track.py` 的
    `_latest_history_import_job`（均由 `asgi_app.py` 末尾装配段接线）
- 不确定会不会成环：新 import 后跑 `python -c "import asgi_app"` 能过、
  `pyflakes backend/<你的包>` 干净，基本就没问题。

---

## 3. 跨模块调用的写法（关系到测试能不能 patch）

**一律 `from pkg import module` + `module.func()`，禁止 `from module import func` 拿裸函数。**

```python
# ✅ 正确：monkeypatch provider_client.chat_completion 时所有调用方都生效
import provider_client
result = provider_client.chat_completion(runtime, messages)

# ❌ 错误：拿到裸函数后，patch 定义处对你这份绑定无效
from provider_client import chat_completion
result = chat_completion(runtime, messages)
```

例外：类与常量的类型注解用途（如 `from core.store import UserStore`）可以直接 import。

**模块别名避开局部变量名**。本次重构修过 6 起同类 bug：函数里
`envelope = ...`、`access = ...`、`store = ...`、`tokens = ...` 这类局部变量
会遮蔽同名模块导致 `UnboundLocalError`。规避方法：别名带前缀
（`core_envelope`、`accounts_access`、`push_tokens`），路由函数名也不要
和模块别名同名（`def identity_actions()` 撞 `identity_actions` 模块就是事故现场）。

---

## 4. 全局可变状态

- 进程内单例（`_users`、`_stores`、各种 lock/缓存）**归属定义它的模块**，
  别处只通过模块属性访问，不复制引用。
- 这些容器**只能就地变更**（`_users[:] = ...`、`d.clear()`），**禁止重绑**
  （`_users = ...`）——测试与跨模块引用都依赖对象身份，
  重绑会静默分叉（历史教训：`_load_users` 重绑导致「注册后 whoami 401」）。
- 模块 import 阶段**禁止读数据库/发网络**（pepper 已改 lazy 就是为此）；
  需要启动期初始化的，提供显式 `start()`/`load_x()` 由 lifespan
  （`asgi/lifespan.py`）调用（范例：wake-bus、WS-leader 选举）。

---

## 5. 兼容层（已终结）

拆分迁移期的 `app.py` COMPAT re-export 门面已于 2026-07-06 随 `app.py` 一起
删除（守护：`tests/test_no_app_py_regression.py`）：

- ❌ 不准再造任何全局符号 re-export 门面；新代码直接 import 真正的模块。
- ❌ 不准新建 `backend/app.py`。

**关于 `memory_garden` 搬迁期的兼容壳**（2026-08-14 已收尾，仅存两个）：

内核提取时，被搬走的模块曾在原路径保留一层 re-export，让调用方不必一次性全改。
**这些纯转发壳已全部删除**，调用方现在直接 `import memory_garden.*`。

仍保留的两个 —— `memory/capture_prompt_v1.py` 与 `memory/dream_prompt_v1.py` ——
**不是 re-export 门面，是适配层**：内核不 import `identity`（那是宿主的身份体系），
所以称呼规则的装配放在这两个文件里，它们有实际逻辑，不只是转发。

新代码一律直接 import `memory_garden.*`。需要称呼装配时走上面这两个适配层。

---

## 6. 测试规范

- **monkeypatch 打在符号的定义模块上**：
  `monkeypatch.setattr(provider_client, "chat_completion", fake)`、
  `setattr(core_enclave, "_get_enclave_info", fake)`。
  patch 别处的独立绑定（裸函数引用/re-export）对调用方**不生效**。
- 测试驱动后端一律 `from asgi_test_client import make_client`（或 conftest 的
  `client`/`backend_env` fixture）；子进程集成用 `backend/serve_dev.py`。
- **所有测试文件一律放 `tests/`，不要放 backend/ 或其它代码目录**
  （2026-06-12 已把 backend/ 下的 4 个测试迁走，别再放回去）。
  文件开头加一行 `sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))`
  即可 import 后端模块——照抄 tests/ 里任何一个现有文件。
- 新功能的测试放 `tests/test_<域名>_*.py`，需要 DB 的走 `tests/conftest.py`
  的一次性测试库（`FEEDLING_TEST_PG`，默认 `127.0.0.1:55432`）；
  **不需要 DB 的纯单元测试**，把文件名加进 `tests/conftest.py` 的
  `_PURE_UNIT` 集合，这样没有 Postgres 的机器也能跑它。
- 两个特例不是 pytest 套件，永远用 `--ignore` 排除：
  `tests/test_api.py`（活服务器集成脚本，CI 单独起后端再跑它）、
  `tests/e2e_model_api_test.py`。
- 提交前本地至少跑：
  ```bash
  python -m pytest tests/ -q \
      --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py
  python -m pyflakes backend/<你改动的包>
  ```
  已知 2 个长期红的 enclave 依赖用例（见 backlog #12），判据是**零新增失败**。

---

## 7. 错误返回纪律

- 路由/core 的错误返回必须用稳定 slug：`{"error": "<snake_case_slug>", ...}`，
  禁止自由文本（如 f-string 拼接的句子）——动态内容放 `detail` 字段。
- 新增 slug 必须同 PR 登记进 `docs/API_ERRORS.md`（有测试守卫锁关键 slug）。
- slug 一经发布即冻结；语义变更走新增新 slug。
- 用户可见的话术不在后端维护（iOS 按 slug 本地映射）；`blame` 枚举见
  `backend/asgi/responses.py::VALID_BLAME`。

---

## 8. 不变量（动之前先到群里喊一声）

- gunicorn 入口 `"asgi_app:app"`（`-k asgi.worker.FeedlingUvicornWorker`）+
  `--chdir backend`。**已支持 `-w N`**（多 worker）：
  :9998 WS ingest 由 advisory-lock 选主只在一个 worker 绑定（`core/leader.py`），
  长轮询 waiter + per-user 缓存靠 Postgres LISTEN/NOTIFY 唤醒总线跨 worker 保持
  一致（`core/wake_bus.py`）。hosted tick 每 worker 各跑、按持 key 用户 key-gate。
  写新代码若引入「依赖单进程共享内存」的状态，必须同时接上 wake_bus 失效广播，
  否则多 worker 下会分叉。每 worker 约 +17 个 DB 连接（池 16 + listener 1），
  调大 `-w` 要核对库的 `max_connections`。
- `python -u backend/enclave_app.py` 入口；compose 文件的任何字面量变更
  都会改变 `compose_hash`，需要重新上链（`deploy/DEPLOYMENTS.md`）。
- 服务端永不解密用户内容；新端点收的内容字段必须是 v1 信封
  （参考 `docs/DESIGN_E2E.md`），明文只允许出现在 enclave 和客户端。
- 路由集变更（增/删/改路径）在 PR 描述里显式列出——url_map 是我们做
  大改动时的回归基线。
- **每用户数据隔离（共享主机上尤其致命）。** 托管 consumer 是同一台
  agent-runner 容器里的兄弟进程，**共享 `/tmp` 和容器 `HOME`**。任何 per-user
  的文件 / 目录 / 缓存 / 锁 / DB 作用域，其命名 key 必须在**所有模式下都逐用户
  唯一**——用 `user_id` 或 `consumer_env` 钉进 `{home}/…`（per-user home）。
  **绝不能用 `FEEDLING_API_KEY` 或它的 `sha1()` 指纹当隔离依据**：host-all
  （Stage-D 零 roster）consumer 是 keyless 的，`FEEDLING_API_KEY=""` →
  `sha1("")` 对同机每个用户塌成同一个值，指纹隔离直接归零。凡是把**解密后的
  机密**（MCP url/auth headers/CA、token 等）落到会被这样塌缩的路径，就是
  跨用户串泄（2026-07-20 host-all user-MCP 事故即此）。新增此类落盘时：
  (a) 路径在 `consumer_env` 里钉成 per-user，(b) 加进 `_CONSUMER_ENV_KEYS`
  让容器策略也透传，(c) 消费侧留一个 fail-safe——per-user pin 缺失时降级关闭
  该功能，而不是退回共享默认路径。**指纹只是锦上添花，从不是隔离边界。**
- **注释必须反映代码的实际行为。** 改代码时同步更正/删除过时注释——尤其是
  断言「安全 / 已隔离 / 不会共享 / 不会发生」这类安保性描述。一条与实现不符的
  安全注释比没有注释更危险：它会诱导后人相信某个不变量成立、从而跳过复核
  （2026-07-20 host-all user-MCP 串泄的直接推手，就是一句「...so two accounts
  on one host never share a file」的过时注释——它对有 key 的用户成立、对
  keyless host-all 不成立，作者照它类推就漏了隔离）。**宁可不写注释，也不要写
  与实际不符的注释；拿不准就照实描述边界条件，别断言绝对安全。**

---

## 9. PR 自查清单

```
[ ] 普通开发 PR 的目标是 test，并已计划/完成 test 环境验证
[ ] 目标为 main 时，PR 来源是 test 或 pre，且描述中附有环境验证结果
[ ] asgi_app.py 的 diff 只有装配/注入变化（理想情况是零 diff）
[ ] 新路由在领域包 routes_asgi.py（APIRouter）上，新逻辑在 service/actions/core 层
[ ] 没有新增向上 import（需要时用了注入钩子）
[ ] 跨模块调用是 module.func() 形式；模块别名不与局部变量撞名
[ ] 没有引用已删除的 app.py facade；没有新造全局 re-export 门面
[ ] 全量 pytest 零新增失败；pyflakes 干净
[ ] 动了 compose / 路由集 / 加密路径的，PR 描述里写明
[ ] 新增共享主机上的 per-user 文件/目录/缓存/锁/DB作用域，key 逐用户唯一
    （user_id 或 {home}），未用 FEEDLING_API_KEY / sha1(key) 指纹当隔离依据
[ ] 改动处的注释与代码实际行为一致；过时注释（尤其断言安全/隔离性的）已更正或删除
```
