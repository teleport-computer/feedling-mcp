# pettorokku Python 服务起步架构

Date: 2026-08-17

Status: 已批准的独立项目起步规格

## 1. 定位

`pettorokku` 是一个尚未绑定业务领域的 Python API 服务起步规格。它提供一条
克制但可上线的默认路径：FastAPI API、同仓库 Celery Worker、PostgreSQL、Redis，
以及不依赖外部服务的快速测试替身。

本文定义运行时边界、依赖方向和交付要求，不规定源码目录和第一天文件清单。
具体业务出现后，再按真实限界上下文组织代码，不为未来领域预留空包。

本文参考了成熟 Feedling 服务中已经验证的原则，例如 ASGI 生命周期、廉价存活探针、
结构化请求日志、路由模板指标和 PostgreSQL 连接治理，但不复制 Feedling 的产品架构。

## 2. 目标

- 构建一个可部署、可水平扩展的模块化单体。
- API 与 Worker 使用同一代码和镜像，通过不同入口启动。
- 领域逻辑依赖 Protocol，不直接依赖 FastAPI、Celery、Redis 或 psycopg。
- 生产使用 PostgreSQL 和 Celery/Redis；单元测试使用内存 Store 和同步任务执行器。
- 第一天具备 API Key 鉴权、健康探针、Prometheus 指标、结构化日志、数据库迁移、
  本地 Compose、多阶段镜像和架构护栏测试。
- 让本地、CI 和生产使用同一条数据库迁移路径。

## 3. 非目标

第一阶段不包含：

- 用户登录、第三方 IdP、OAuth/OIDC 或用户 session 的具体实现。
- 微服务拆分、服务网格或跨服务分布式事务。
- 多条 Celery 队列、任务编排 DSL 或通用工作流引擎。
- ORM、自动生成 repository 或领域无关的万能 Store。
- TEE/enclave、双数据库、双写、对象存储、向量检索或 agent runtime。
- 云厂商 SDK、Terraform 资源命名或厂商专属发布流程。
- 前端、Admin、兼容 API、旧路径转发或产品环境变量。
- 在单个 API 容器中运行多个 Web worker。

## 4. 运行时拓扑

```text
Client
  │
  ▼
FastAPI API ─────► Domain Service ─────► Store Protocol ─────► PostgreSQL
  │
  ├── submit ────► Task Backend Protocol ─► Redis ─► Celery Worker
  │                                                  │
  │                                                  └──► Store Protocol
  │
  └── auth ──────► API Key
```

生产使用同一镜像启动三个角色：

1. **API**：接收 HTTP 请求并提交异步任务。
2. **Worker**：消费 Celery 任务并直接调用 job handler。
3. **Migration**：在部署期间执行 `alembic upgrade head` 后退出。

PostgreSQL 与 Redis 在生产中是外置依赖。本地 Compose 可以启动它们，但 Compose
不是生产发布系统。

单个 API 容器只运行一个 Uvicorn 进程，通过增加容器副本水平扩容。该约束使连接池、
优雅关闭和 Prometheus 指标保持单进程语义。若未来需要容器内多进程，必须先单独设计
Prometheus multiprocess、进程本地缓存和资源池容量。

Celery 初始只使用 `default` 队列。只有观测到不同任务类型互相阻塞，并且优先级、
并发限制或独立扩缩容能解决该问题时，才增加队列。

## 5. 分层与依赖方向

### 5.1 HTTP Adapter

Router 只负责：

- HTTP 输入解析和 schema 校验；
- principal、请求模型与领域命令之间的映射；
- 调用领域 Service；
- 把成功结果映射为 HTTP 响应。

Router 不打开数据库连接、不写 SQL、不导入 Celery、不调用 `send_task()`，也不逐个
捕获领域异常并翻译状态码。

### 5.2 Domain Service

Service 负责用例编排、权限判定、事务边界和领域不变量。Service 依赖小而专用的
Protocol，不导入 FastAPI、Celery、Redis 或具体数据库实现。

不要创建覆盖整个系统的 `StoreProtocol`。每个真实领域声明自己需要的最小读写能力，
例如 `OrderReader`、`OrderWriter`。这样基础设施实现可以组合能力，领域服务不会逐步
依赖一个巨型接口。

### 5.3 Job Handler

Job handler 是 Worker 调用的普通函数或异步函数。它接收经过版本化的 JSON payload，
通过 Worker Runtime 取得领域 Service，并记录任务结果。

Job handler 不获得任务提交接口。需要递归任务、扇出或工作流时，必须作为独立设计
处理，不能通过在任务内部再次 `submit()` 隐式形成。

### 5.4 Infrastructure

基础设施层实现 PostgreSQL Store、Celery Task Backend，以及以后可能增加的
Session Store。领域层只看 Protocol。

数据库连接池由 PostgreSQL 基础设施组合持有，不作为公共容器字段暴露给 Router 或
Service，避免业务代码绕过 Store 直接取连接。

## 6. Runtime Container

Runtime Container 保存已经完成校验和装配的运行时依赖：

- 冻结的 Settings；
- 领域 Store 实现集合；
- 领域 Service 集合；
- Task Backend；
- 可选 Session Store。

提供两个装配入口：

- `create_api_container()`：按照环境变量选择 Store、Task Backend 和可选 Session Store。
- `create_worker_container()`：复用生产 Store，但不向 job handler 暴露任务提交能力。

Container 负责统一的 `setup()` 与 `aclose()`，Router 只从 `app.state` 获取已装配依赖，
不自行读取环境变量或创建客户端。

只有真实依赖出现时才扩展 Container。例如在出现外部 HTTP 调用之前，不预留空的
HTTP client、gateway 或 agent client。

## 7. 配置

配置使用 Pydantic v2 与 `pydantic-settings`，在进程启动时解析一次并冻结。生产后端
缺少所需 URL 时必须启动失败，禁止静默回落到内存实现。

基础配置面：

| 变量 | 取值 | 条件 |
|---|---|---|
| `STORE_BACKEND` | `memory` \| `postgres` | `postgres` 需要 `DATABASE_URL` |
| `TASK_BACKEND` | `inline` \| `celery` | `celery` 需要 `CELERY_BROKER_URL` |
| `CELERY_RESULT_BACKEND` | Redis URL | Celery 任务需要查询状态时必填 |
| `SESSION_BACKEND` | `disabled` \| `memory` \| `redis` | 初始固定为 `disabled` |
| `SESSION_REDIS_URL` | Redis URL | 后续启用 Redis session 时必填 |
| `API_KEY` | 非空 secret | 启用受保护路由时必填 |

测试默认 `memory + inline + disabled`。本地完整栈默认
`postgres + celery + disabled`。

Celery broker、result backend 和未来的 session 可以使用同一 Redis 部署，但必须
使用独立 URL、逻辑数据库或明确 key namespace，避免生命周期、清理策略和权限边界
相互污染。文档不把一个笼统的 `REDIS_URL` 同时解释成三种资源。

`.env` 只服务本地开发，不进入镜像或版本控制。生产通过部署平台注入 secrets。

## 8. 数据库与迁移

业务访问使用 psycopg 3 原生 SQL 和 `psycopg_pool.AsyncConnectionPool`，不引入 ORM。
连接池必须显式打开和关闭，不依赖构造函数的隐式启动行为。

建议的连接默认值：

- `connect_timeout=5s`
- `statement_timeout=60s`
- `lock_timeout=10s`
- `idle_in_transaction_session_timeout=30s`

这些是安全起点，不是所有查询的永久上限。确有长事务需求时，应在具体操作上显式
调整并提供观测证据，不能全局取消限制。

Alembic 是 schema 版本的唯一事实来源。迁移可以通过 `op.execute()` 执行原生 SQL；
使用 Alembic 不意味着业务层使用 SQLAlchemy ORM。

API 和 Worker 不在启动时自动升级 schema。本地、CI 和生产都显式执行：

```bash
uv run --frozen alembic upgrade head
```

本地 Compose 中 Migration 服务成功退出后，API 和 Worker 才能启动。破坏性结构变更
采用 expand → migrate/backfill → contract，不假设所有进程同时升级。

## 9. 异步任务

Task Backend Protocol 至少提供：

- `submit(name, payload) -> TaskRecord`
- `get(task_id) -> TaskRecord | None`
- `healthcheck()`

任务名是稳定、显式注册的字符串。Payload 是带版本字段的 JSON 数据，不传 Python
对象、数据库连接、Request 或其他框架对象。

`TaskRecord` 至少包含：

- `id`
- `name`
- `status`
- `attempts`
- `created_at`、`started_at`、`finished_at`
- 经过脱敏和截断的错误摘要

Celery result backend 是通用模板中的任务状态来源；业务领域若需要更强的审计、事务
一致性或长期保留，再设计领域任务表或 transactional outbox，不能假装“数据库写入 +
Celery 入队”天然原子。

Inline backend 使用同一任务 registry 同步执行真实 handler，为单元测试提供确定性。
它模拟行为，不模拟 Celery 的并发、重投和故障恢复；这些由独立集成烟测覆盖。

默认任务不重试。允许重试的任务必须同时定义：

- 最大次数；
- 退避与抖动；
- 幂等键或可证明的幂等行为；
- 可重试与不可重试错误分类；
- soft/hard time limit。

若启用 late acknowledgement 或 worker-lost 重投，handler 必须按“至少一次执行”设计，
不能假设任务只运行一次。

## 10. HTTP 与鉴权

### 10.1 Day-1 鉴权

第一阶段只实现机器/内部调用的 `X-Api-Key`：

- 值来自 `API_KEY`；
- 使用恒定时间比较；
- principal 写入 `request.state`；
- 领域 Service 读取 principal，不解析 header。

公开路由使用精确集合管理：`/healthz`、`/readyz`、`/metrics`。其余路由默认需要
鉴权，不维护容易漏项的“允许表 + 拒绝表”双重配置，也不使用路径子串豁免。

以后出现真实用户登录需求时，再增加 opaque session 和 `SessionStoreProtocol`。
第三方 IdP 只负责把外部身份换成本服务 session；IdP JWT 不自动等价于本服务 session。
多副本 API 不允许使用内存 session。

Webhook 在所属 Router 内按供应方协议验签，不复用 API Key 或用户 session 中间件。

### 10.2 中间件顺序

入站执行顺序为：

1. CORS；
2. request ID 与访问日志；
3. 鉴权；
4. Router。

Starlette 后添加的中间件位于外层，因此注册时按上述顺序的逆序添加：先鉴权，再请求
上下文/日志，最后 CORS。这样合法 `OPTIONS` 预检由 CORS 在鉴权之前处理。

### 10.3 Request ID 与日志

每个请求生成 request ID；若接受调用方提供的 `X-Request-ID`，必须先校验长度和字符集。
响应始终回传最终使用的 `X-Request-ID`。

结构化访问日志至少包含：

- request ID；
- method；
- FastAPI 路由模板；
- status；
- `latency_ms`；
- principal 类型。

日志不记录认证 header、原始 secret、完整敏感 query 或请求/响应正文。应用代码统一
使用 Loguru，并配置 JSON sink 输出结构化日志。Uvicorn 与 Celery 仍通过标准库
`logging` 产生日志，因此在应用入口设置单向 interception handler，把标准日志转发到
Loguru；禁止同时保留两套输出 handler，避免重复日志和上下文字段漂移。

## 11. 健康检查与指标

### 11.1 `/healthz`

只证明进程能够及时响应，不访问 PostgreSQL、Redis、Celery 或其他外部依赖。负载均衡
和容器存活探针使用这里。

### 11.2 `/readyz`

检查当前 Store 和 Task Backend 的 `healthcheck()`。任何必要依赖不可用或应用开始
关闭时返回 503。检查必须有短超时，不能无限等待连接池或 broker。

### 11.3 `/metrics`

使用 `prometheus-client` 暴露 Prometheus 文本格式。HTTP 指标的 route label 使用
FastAPI 路由模板，例如 `/items/{item_id}`，不使用原始 URL。未匹配路由统一标记为
`unmatched`，避免高基数。

`/healthz`、`/readyz` 和 `/metrics` 不进入业务请求 latency Histogram，但可以单独
记录探针失败计数。

## 12. 生命周期

启动顺序：

1. 解析并完整校验 Settings。
2. 创建 Runtime Container。
3. 打开数据库连接池。
4. 执行组件级 `setup()`。
5. 确认必要依赖可用。
6. 将 readiness 标记为 ready。

关闭顺序：

1. 立即将 readiness 标记为 not-ready。
2. 停止接受新的业务工作。
3. 在明确期限内等待在途请求完成。
4. 关闭任务客户端、Session Store 等外部资源。
5. 关闭数据库连接池。
6. 完成 Container 清理。

Uvicorn 和容器平台的优雅关闭期限必须覆盖上述过程。Worker 的关闭期限还应覆盖正常
任务时长；超长任务依赖任务 soft/hard time limit，而不是无限延长容器退出时间。

## 13. 错误模型

领域异常定义在领域层，由 FastAPI 全局 exception handler 映射。Router 不重复翻译。

| 情况 | HTTP |
|---|---:|
| 未认证或凭证无效 | 401 |
| 已认证但权限不足 | 403 |
| 资源不存在，或资源属于其他 principal | 404 |
| 输入格式或字段无效 | 422 |
| 业务状态冲突或幂等冲突 | 409 |
| 连接池耗尽、数据库或 broker 不可用 | 503 |
| 服务端处理期限耗尽 | 504 |
| 未捕获异常 | 500 |

适合重试的 503 响应携带 `Retry-After`。500 响应只返回稳定错误码和 request ID，
不返回 traceback、SQL 或内部异常文本。服务端日志保存异常栈，但先经过敏感信息过滤。

客户端断开、请求取消和预期的任务撤销单独分类，不全部记为 500。Worker 失败通过
Celery result backend 形成终态；领域若要求长期审计，再写入自己的持久记录。

## 14. 测试策略

默认测试不依赖 Docker、PostgreSQL 或 Redis：

- 使用内存 Store 和 Inline Task Backend；
- API 冒烟覆盖应用创建、公开探针、受保护路由 401、合法 API Key 和统一错误信封；
- 领域契约测试先对内存实现执行，再在 PostgreSQL 集成测试中复用；
- 任务测试执行真实 handler，并验证任务名注册、payload 版本和禁止递归提交；
- 测试 shutdown 后 readiness 立即失败以及资源按顺序关闭。

架构护栏通过 AST 或 import 检查锁定：

- Router 不导入 psycopg、Celery 或基础设施实现；
- Domain 不导入 FastAPI、Celery 或 Redis；
- Job handler 不调用任务提交接口；
- 除明确公开集合外，所有 HTTP 路由默认鉴权；
- 指标不使用原始请求路径作为 label。

PostgreSQL、Redis 和真实 Celery Worker 的 Compose 闭环烟测是独立测试层，不混入
快速单元测试。

## 15. 验证与 CI

统一验证应覆盖：

```bash
uv run --frozen pytest -q
uv run --frozen ruff check src tests
uv run --frozen ruff format --check src tests
uv run --frozen alembic check
docker compose -f compose.yml -f compose.local.yml config
docker build .
```

CI 至少包含：

1. 单元测试与架构护栏；
2. Ruff lint 和格式检查；
3. PostgreSQL 集成测试；
4. Alembic 单一 head 与从空库升级测试；
5. 镜像构建；
6. 可选的 API → Redis → Worker → PostgreSQL 闭环烟测。

`alembic check` 用于检测模型元数据时才有完整价值；本项目不使用 ORM，因此 CI 还必须
显式检查只有一个 Alembic head，并实际对空 PostgreSQL 执行 `upgrade head`。不能把
`alembic check` 当作原生 SQL migration 的充分验证。

## 16. 依赖与版本政策

每类能力只选一个默认实现：

| 类别 | 默认 |
|---|---|
| HTTP | FastAPI + Uvicorn |
| 配置 | Pydantic v2 + pydantic-settings |
| 数据库 | PostgreSQL + psycopg 3 + psycopg-pool |
| 迁移 | Alembic，migration 内使用原生 SQL |
| 异步任务 | Celery + Redis |
| Redis 客户端 | redis-py |
| 指标 | prometheus-client |
| 日志 | Loguru；统一接管 Uvicorn/Celery 的标准 logging 输出 |
| 包管理 | uv |
| 测试 | pytest + HTTPX |
| lint/format | Ruff |

项目声明 `requires-python = ">=3.12,<3.14"`，开发环境与 Runtime 镜像默认固定
Python 3.13。升级到新的 Python minor 版本前，必须先确认 FastAPI、Celery、psycopg
及其二进制依赖均已支持，并在 CI 和镜像中完成验证。

运行时 Python 依赖：

```toml
[project]
requires-python = ">=3.12,<3.14"
dependencies = [
  "fastapi",
  "uvicorn[standard]",
  "pydantic",
  "pydantic-settings",
  "celery[redis]",
  "redis",
  "psycopg[binary,pool]",
  "alembic",
  "loguru",
  "prometheus-client",
  "python-dotenv",
]

[dependency-groups]
test = [
  "pytest",
  "httpx",
]
lint = [
  "ruff",
]
```

`pytest`、HTTPX 和 Ruff 只用于开发与 CI，不进入生产 Runtime 依赖。Python、uv、
PostgreSQL、Redis Server、Docker 和 Compose 是语言工具或外部运行组件，不写入
`project.dependencies`。依赖声明使用合理兼容范围，`uv.lock` 固定实际构建版本。

暂不引入 SQLAlchemy ORM、LangChain、RabbitMQ、Kafka、MySQL、Poetry、pip-tools、
Flask 或 Django。新依赖必须由真实需求触发，而不是为了预留扩展点。

## 17. 本地与生产交付

使用一个多阶段、非 root 运行的镜像。Runtime 镜像只包含锁定依赖、应用代码和必要的
证书/系统库，不包含编译工具、测试缓存或本地 secrets。

同一镜像支持：

- API：监听 `0.0.0.0:8000`；
- Worker：启动 Celery Worker；
- Migration：执行 Alembic 升级后退出。

Dockerfile、Compose、健康检查和文档统一使用端口 `8000`。API 与 Worker 分别扩缩容。
生产 PostgreSQL 与 Redis 外置，并设置备份、TLS、认证、容量和告警；这些属于部署环境
职责，不在通用模板里绑定具体厂商。

本地 Compose 应通过 dependency health 和 Migration 成功状态控制启动顺序，但应用本身
仍必须对依赖暂时不可用进行明确失败或有限重试，不能把 Compose 启动顺序当作可靠性
保证。

## 18. 延后决策

以下能力等真实需求出现后再设计：

- 用户 session 与 IdP 身份交换；
- transactional outbox；
- 独立任务状态表和长期任务审计；
- 多队列、定时任务和工作流；
- 对象存储和大文件上传；
- 外部 HTTP client 与熔断策略；
- 向量检索；
- Webhook 验签；
- 多进程 Web 容器；
- 云厂商发布和基础设施即代码。

增加这些能力时，继续遵守同一原则：先出现真实需求，再增加一个边界清楚、可替换、
可独立测试的组件。

## 19. 与 Feedling 的边界

可以复用的经验：

- FastAPI/ASGI 应用装配与 lifespan；
- 领域逻辑和 HTTP adapter 分离；
- 廉价 `/healthz` 与依赖型 `/readyz` 分工；
- request ID、结构化日志和路由模板指标；
- psycopg 连接池和 PostgreSQL 超时治理；
- 同一镜像的 API/Worker 多入口；
- 通过测试锁定分层和安全边界。

不能复制的产品实现：

- TEE/enclave 与 attestation；
- RDS/TEE 双数据库和影子同步；
- Runtime V2 的 PostgreSQL 持久队列、lease、reaper 与多池调度；
- R2、iOS 兼容面、WebSocket ingest、agent runtime 和模型供应商适配；
- Feedling 路由、环境变量、云资源名和发布拓扑。

`pettorokku` 不是 Feedling 的精简分支，而是一份吸收其通用经验、删除其产品约束的
独立起步架构。
