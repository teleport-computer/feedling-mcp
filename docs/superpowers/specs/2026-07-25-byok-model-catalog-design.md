# BYOK 模型目录：填凭据后实时拉全量模型 + 搜索选择

- 日期：2026-07-25
- 状态：设计待审（brainstorm 完成，含一轮 Codex plan_review + 独立核实）
- 影响仓库：`feedling-mcp`（后端，本 worktree `feat/model-catalog` 基于 origin/test）、`feedling-mcp-ios`（iOS，实现时另开 worktree）
- 基线核实：origin/test tip `c5eb479f`（2026-07-25 fetch）。本地主 checkout 落后 556 提交，`model_api` 相关文件在最新 test 上大改过，下述行号均以最新 test 为准。

---

## 1. 问题与目标

**痛点**：用户自带 API key（BYOK）加 key 时，「选模型」这步的可选模型是**写死在 iOS App 里**的一份 `recommendedModels`（`ModelAPIConfiguration.swift`）。服务商上新 / 下架，App 不发版就不会变，清单必然过时；用户只能靠一个自由手填框补。且这套「选服务商→选模型→填凭据」UI 在 iOS 里有**两份重复实现**（设置页、onboarding 首次引导），改一处另一处不动。

**目标**：
1. 填凭据后**实时从服务商拉它真实在售的模型全量清单**，直接全显示 + 搜索 + 默认排序；保留手填兜底。**不保留任何写死的精选/默认清单**（写死清单本身会过时，正是本功能要解决的问题）。
2. 把「选模型」抽成**一个共享组件**，onboarding 和设置两处都接入，消掉重复。

**改前 vs 改后**：

```
之前：选服务商 → 选模型(写死几个 + 手填框) → 填凭据
之后：选服务商 → 定凭据来源 → 拉目录 → 选模型(全量 + 搜索 + 默认排序 + 手填兜底) → 保存
```

---

## 2. 范围与非目标

**做（v1）**：
- 后端新增一个**旁路** endpoint「按凭据列某服务商的模型」。
- iOS 抽一个共享「选模型」组件；onboarding + 设置两处接入。
- **直接展示拉回的全量目录 + 搜索 + 默认排序**；不保留写死的精选/默认清单，现有 `recommendedModels` 随本功能移除。

**不做（v1 非目标，明确划走）**：
- **不自动判定「这个模型 io 的 agent runtime 到底能不能跑」**——这条判定线属 runtime（归 zhihao），v1 不碰。
- **不做服务端持久缓存 / 跨 worker 一致性**——目录只在单次填写流程内复用。
- **不让后端用 io 自有 key 预缓存各家目录**（key 不齐 + 基建成本）。
- **不做按模态的能力粗筛**（save-time 测活已兜底选错，见 §7）；留作后续可选。
- **不动加密信封**（envelope id / AAD / K_enclave 一律不改）。

---

## 3. 现状（最新 test 核实）

- **7 个 provider**：`openai / openrouter / anthropic / bedrock / gemini / deepseek / openai_compatible`（`provider_client.py:299` `validate_config`）。⚠️ 相比旧快照**新增了 `bedrock`**。
- **加路由契约**：`model_api_route_create` 强制 `api_key` 与 `credential_id` **二选一且仅一个**（`setup_core.py:1041`）。设置页因此支持「复用已存凭据、不重输 key」。
- **列全部凭据**：`model_api_credentials_get`（`setup_core.py:1006`）直接查 credentials 表，独立于是否有 route 引用。
- **验活**：`test_provider_key`（`provider_client.py:3344`）对**指定 model** 发一次真实 health-check 生成请求（`timeout=30`, `require_reply=False`）；`model_api_setup` 保存前、`route/activate` 激活前都会跑。
- **无任何 catalog / 列模型接口**（grep 确认）——本功能是净新增。
- iOS 两套重复 UI：设置 `Pages/Settings/ModelAPIConfigurationSheet.swift`；onboarding `Pages/Chat/ChatEmptyStateView.swift`（自带 `ModelAPISetupStep` 状态机）。二者共用领域模型 `Pages/Settings/ModelAPIConfiguration.swift`（provider 枚举 + 写死 `recommendedModels`）。视图层复制两份。
- 后端已有 4 个 wire 的**同步 + async** 实现（openai-compat / anthropic / gemini / openai-responses），payload 构造与响应解析共用（`provider_client.py:3374+`）——新接口应复用其 HTTP client 与常量。

---

## 4. 统一流程（credential-source-first）

所有 provider 一个顺序，选模型只发生一次、都从真实目录选：

```
选 provider
  → 定「凭据来源」
       onboarding        ：只有「新填 key」
       设置              ：「选已存 credential」或「新填 key」
       openai_compatible ：同时确定 base_url
  → 拉目录 (调 §5 新接口)
  → 选 model
       全量 = 「该账号可见」的目录全部（明确标注：非「io 已验证可运行」），带搜索 + 默认排序
       手填 = 目录不支持 / 未列出 / 用户已知 id 时
  → 保存 → 对具体 model 跑现有 test_provider_key（不变）
```

> 说明：OpenRouter 的 `/models` 是公开的（免 key），但为流程统一，仍走 credential-source-first。「分 provider 自适应顺序 + 产品对比开关」在 brainstorm 中考虑过并**否决**（§9）。

---

## 5. 后端：列模型接口

**Endpoint**：`POST /v1/model_api/models`（旁路，不碰 setup / test / routes 正常路径）。

**请求契约**（复用现有 `api_key XOR credential_id`）：
```json
{ "provider": "anthropic", "base_url": "", "api_key": "...", "credential_id": null }
```
- `api_key` 与 `credential_id` 必须且只能给一个。
- `credential_id` 路径：服务端解封该凭据的 provider key（复用 route-test 现有的解密路径，**不改 envelope/AAD/id**），用它去拉目录 → 这样设置页「复用已存 key」成立。
- `openai_compatible` 必须带 `base_url`。

**各 provider 拉取方式**：

| provider | 方式 | 认证 | 翻页 |
|---|---|---|---|
| openai / openrouter / deepseek / openai_compatible | `GET {base}/models` | `Authorization: Bearer`（openrouter 公开但仍带 key） | 单页 |
| anthropic | `GET /v1/models` | `x-api-key` + `anthropic-version` | `has_more` / `last_id` 循环 |
| gemini | `GET /v1beta/models` | **`x-goog-api-key` header（不放 query）** | `pageSize` / `nextPageToken` 循环 |
| bedrock | AWS `ListFoundationModels`（SigV4） | AWS 凭据 | 见开放问题 §10 |

**归一化**：统一成 `[{id, display_name}]`。后端归一化后 OpenRouter 那 535KB 会缩到很小（配合已有 GZip `asgi_app.py`）。

**上限与健壮性**（防自定义端点拖垮 worker）：
- 总预算 ~20s、最多 ~10 页、最多 ~2000 个模型。
- streaming 读取，解压后响应体上限 ~5MB。
- model id 长度上限 160（与保存约束一致）；去重；稳定顺序。
- 阻塞的 provider 请求继续经线程池执行。

**部分成功**：后续页失败但已有结果 → `complete:false` + warning；**第一页**就失败 → 返回错误。

**响应**：
```json
{ "provider": "gemini",
  "models": [{"id": "gemini-3.1-pro-preview", "display_name": "Gemini 3.1 Pro Preview"}],
  "complete": true, "warnings": [] }
```

**错误分类**（稳定 slug；**不得把上游 401 原样透传成本接口的 401**，否则 iOS 会把 provider key 失败误判成 Feedling 登录失效）：

| 情况 | slug | 客户端行为 |
|---|---|---|
| 上游 401 | `model_catalog_auth_failed` | 留在凭据步，提示换 key |
| 上游 403 | `model_catalog_access_denied` | 不说「key 错」，提示权限/地区/项目限制 |
| 429 | `model_catalog_rate_limited` | 保留输入，可重试 |
| timeout / 网络 / 5xx | `model_catalog_temporarily_unavailable` | 手填 + 重试 |
| compatible 无 `/models` | `model_catalog_unsupported` | 直接进手填，不阻塞配置 |
| 2xx 空数组 | 成功，`models:[]` | 显示「未返回模型」，保留手填 |
| 非 JSON / 超限 | `model_catalog_invalid_response` | 手填 + 重试 |

**契约与文档**：用**手写 JSON-Schema**（本仓 `tools/public_openapi_contracts.py` 的约定——旁路 raw-`Request` handler 不走 Pydantic 推断，schema 手工登记）：request 用 `oneOf` 表达 `api_key` XOR `credential_id`、provider 用 enum、`api_key` 标 `writeOnly`；response/model-item 列 `required`。error slug 登记进 `docs/API_ERRORS.md` 且加进 `tests/test_api_errors_doc.py` 的 `MUST_HAVE` 守卫集合（仅加 markdown 行不够）；跑 OpenAPI 回归 + 从 docs-site 重新生成契约。

---

## 6. iOS：共享「选模型」组件

抽一个组件，被设置页 `ModelAPIConfigurationSheet` 与 onboarding `ChatEmptyStateView` 两处引用。

**组件负责**：
- loading / retry / partial / empty / unsupported 状态；
- 搜索、去重、默认排序、手填模式；
- `LazyVStack` / `List` 虚拟化（数百条不能一次性构造全部卡片——现状两处都是 `VStack + ForEach`）；
- **请求取消 + request-id 防旧响应覆盖**：用户退回换 provider/key/base_url 后，旧请求的结果/错误不得写回当前 UI；
- 输出最终选定的 model id。

**组件不负责**（留在各宿主）：
- provider / key / credential / base_url 输入；
- onboarding 的 materials / progress；设置页的 credential label / activate / rollback；
- 保存、激活、runtime 测活；宿主级 analytics / 导航 / toast。

**不保留写死精选**：直接展示全量目录（默认排序 + 搜索），文案叫**「该账号可见」**，不叫「可兼容 / 可运行」。旧的 `ModelAPIConfiguration.recommendedModels` 随本功能移除。

**视觉**：遵 `DESIGN.md` token（无 raw hex / pt / font 字符串）。

---

## 7. 关键决策

- **列表接口独立、不并入 `test_provider_key`**：列目录时还没有 model，且验活是对具体 model 发真实请求——两者语义不同。列表的「验证」只到「凭据被目录接口接受（2xx）」；**保存时对具体 model 的测活保持不变**，这才是「选错模型」的真正兜底。
- **不做 runtime 兼容过滤**：直接把「该账号可见」的全量铺给用户看似有「选到 io 跑不了的模型」的风险，但**保存前的 `test_provider_key` 会先对该 model 发一次真实请求**，选错的非聊天模型（embedding / 画图等）会在**保存时**就失败、route 标 failed，用户当场看到——不是「聊天时才挂」。因此 v1 用 save-time 测活兜底，不引入模态粗筛。
- **credential-source-first 而非 key-first**：因为设置页支持复用已存凭据（此时手上无明文 key），接口必须收 `credential_id`，流程也就以「凭据来源」而非「填 key」为节点。

---

## 8. 测试

- **后端**（`docs/testing/TESTING.md` §2：动了 routes + provider client）：
  - 单测每个 wire 的归一化 / 翻页 / 上限 / 错误分类（mock httpx；覆盖 2xx 空、非 JSON、超限、部分页失败、401/403/429/timeout）。
  - typed contract + OpenAPI 回归。
  - `credential_id` 路径会触及 enclave 解密（只读取 provider key 用于 `GET /models`，不改信封）——按红线，**该路径建议在 test 上真跑一次**确认解封可用。
- **iOS**：组件各状态；旧响应竞态（换 provider 后旧结果不覆盖）；数百条列表滚动性能。
- 满足 §2 对应行的必做项与 DoD 才算完成。

---

## 9. 放弃的替代方案

1. **分 provider 自适应顺序 + 产品对比调试开关**（OpenRouter 免 key 先看模型、其余 key-first）→ 太复杂、两种顺序增加分支和测试面；改统一 credential-source-first。
2. **后端用 io 自有 key 预缓存各家目录**（全 provider 免 key 浏览）→ 只确认本地有 OpenRouter/DeepSeek 两把 key，其余家后端是否有可用 key 未知，且要目录缓存/刷新基建，v1 不值。
3. **自动探测模型能否在 io agent runtime 跑并过滤** → 属 runtime owner 判定线，工作量大且 v1 准确度低。
4. **按模态能力粗筛** → v1 不做（save-time 测活兜底），留作后续可选。
5. **合并进 `test_provider_key`（验证+列表一次往返）** → 语义不同、列表时无 model，会削弱保存保护。
6. **服务端持久缓存** → v1 仅单次填写流程内复用（provider/key/base_url 未变则复用，点重试再拉）。
7. **保留写死的精选/默认清单并与目录取交集** → 否决：写死清单本身会过时（正是本功能要修的问题），且引入交集复杂度；改为**直接全显示 + 搜索 + 默认排序**，`recommendedModels` 移除。

---

## 10. 开放问题（需在写 plan 前确认）

1. **Bedrock 列模型**：走 AWS `ListFoundationModels`（SigV4），与 bearer `/models` 完全不同。v1 建议让 bedrock **退回「手填」**（拉不到目录时的通用兜底路径），真实目录留到后续。→ 需确认接受。
2. **接口路径命名**：`POST /v1/model_api/models` vs `/model_api/catalog`。→ 待定，不影响设计。
