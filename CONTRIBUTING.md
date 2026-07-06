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
├── model_api_runtime/ ← Model API 线的 agent 运行时：prompts / tools /
│                      memory_tools / wake（独立包，与 hosted/ 平级；
│                      被 hosted·proactive·perception 复用）
├── perception/     ← 扩展感知（此模式的最早范本）
└── db.py · content_encryption.py · provider_client.py · enclave_app.py ·
    dstack_tls.py · hosted_runtime.py · semantic_analysis.py ·
    memory_readside_core.py · memory_index_selector.py ·
    context_memory_selection.py · migrate_to_pg.py
                    ← 底层独立模块，保持无业务依赖
```

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
```

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

## 7. 不变量（动之前先到群里喊一声）

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

---

## 8. PR 自查清单

```
[ ] asgi_app.py 的 diff 只有装配/注入变化（理想情况是零 diff）
[ ] 新路由在领域包 routes_asgi.py（APIRouter）上，新逻辑在 service/actions/core 层
[ ] 没有新增向上 import（需要时用了注入钩子）
[ ] 跨模块调用是 module.func() 形式；模块别名不与局部变量撞名
[ ] 没有引用已删除的 app.py facade；没有新造全局 re-export 门面
[ ] 全量 pytest 零新增失败；pyflakes 干净
[ ] 动了 compose / 路由集 / 加密路径的，PR 描述里写明
```
