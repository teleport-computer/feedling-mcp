# 联网搜索的用户开关（Runtime V2）— 设计文档

- 日期：2026-07-20
- 分支：`feat/web-search-gate`（基线 `origin/pre` @ `68504b43`）
- 需求来源：Lark t100535「联网搜索及其自定义开关（主要 APIkey 用户）」
- 前一版：`feat/web-search-toggle`（基于 `origin/test`）——**已废弃**，见 §1

---

## 1. 为什么重来

初版照着 `origin/test` 设计：spawn CLI agent、靠 `--allowed-tools` 授权、按
driver 分三条路。**该架构在 `origin/pre` 上不存在**——`backend/agent_runtime/
spawners.py` 已被 `backend/model_api_runtime/v2/` 取代，后端自己跑工具循环、
直接调模型 API。`pre` 领先 `test` 152 提交且从不反向合，是运行时的前沿线。

旧分支保留不删：其中的实测数据（DeepSeek 原生搜索、allow/disallow 矩阵、
Claude Code WebFetch 无 prior-context 校验）仍然有效，只是不再是主线方案。

## 2. 现状：功能已有一半

`pre` 上 `web_search` / `web_fetch` **已经是一等公民能力**：

- 实现：`backend/capabilities/web.py`（keyless DuckDuckGo 抓取，
  底层复用 `backend/model_api_runtime/tools.py`）
- 注册：`backend/capabilities/registry.py:27-28`，属 `READ_ACTIONS`
- 目录：`backend/capabilities/tool_schema.py:378-390` `build_tool_specs()`
- 子 agent 也能用：`worker.py:330-340` `_SUBAGENT_ALLOWED_TOOLS`

**由后端自己执行搜索，与模型供应商无关** —— `test` 上「gemini /
openai_compatible 拿不到原生搜索」的问题在 V2 上不存在，所有 provider 通吃。

安全防护也已就位（`core/net_safety`）：SSRF 拦截、**DNS rebinding 的 TOCTOU
防护**（校验时解析到的 IP 被 pin 住，不把域名交回 HTTP 客户端二次解析）、
每跳 redirect 独立重新校验并重新 pin（上限 5）、响应体 40KB 截断、
查询词敏感信息检测。

**缺的正是任务标题的后半句：没有任何 per-user 的开关。** 搜索对模型无条件可用。

## 3. 本期范围

**做**：一个 per-user 开关，关闭时 `web_search` / `web_fetch` 对该用户
**不出现在发给模型的请求里**。

**不做**（hx 决策）：
- 不碰搜索后端。keyless DDG 抓取在 2026 年限流严重（LangChain / Open WebUI /
  MetaGPT 下游均断过），但作为独立议题，不在本期。
- 不做引用 / 来源 UI。
- 不做按会话 / 按消息的粒度——需求明确是设置页的全局开关。

## 4. 核心机制：复用已有的 `disabled_tool_names`

`tool_loop.py` 已经有完全正确的接缝，**不需要新机制、不需要碰全局目录**：

```
disabled_tool_names=  (kwarg)
  → disabled_names            tool_loop.py:307-315
  → turn_catalog              :316-320  ← 新建 list，从不改 _catalog()
  → 每一轮的 tools             :363-405
  → offered_names             :534
```

一个名字进了 `disabled_names`，就**同时**满足：

- **不被 offer**：不在 `turn_catalog`，因此不在任何一轮的 `tools` 里，
  模型请求体里根本没有这个工具
- **不能执行**：`offered_names` 不含它 → `malformed` 判定命中
  （`:535-550`），整批 all-or-nothing 丢弃（`:557-587`），
  且 `executor.dispatch_tool_calls:93-101` 是第二道 fail-closed 边界

这正是 Codex 第 4 轮 P0 要求的语义（关闭态必须把工具摘出可用集合，而不是
「提供了但执行时拒绝」）。**在 V2 上这个语义天然成立，因为请求体完全由我们
构造**——不存在 CLI 那边「工具可见但未授权、模型去讨权限」的中间态。

`_CATALOG`（`tool_loop.py:15,38-42`）是进程级记忆化，**保持 user-agnostic，
本设计一行不碰**。

### 4.1 三个必须同时改的调用点

`run_tool_loop` 在 `worker.py` 有三处调用，**漏一处就有一条链路绕过开关**：

| 位置 | lane | 现状 |
|---|---|---|
| `:6393` | chat（`process_job`） | 传 `disabled_mutation_tool_names`（`:5783` 起） |
| `:4803` | wake / screen_watch（`_run_wake`） | ⚠️ **完全没有传 `disabled_tool_names`** |
| `:2332` | 子 agent（`_make_task_batch_dispatcher`） | 传 `_SUBAGENT_DISABLED_TOOLS`（`:2341`） |

wake lane 那处是新增 kwarg，其余两处是并集。

### 4.2 子 agent 需要改两处，不是一处

`_SUBAGENT_DISABLED_TOOLS`（`worker.py:593-597`）是**导入时冻结的模块级常量**，
per-user 的值不能塞进去（且 `tests/test_v2_subagents.py:285,455` 与
`test_v2_worker_tool_loop.py:634` 都在断言这个常量）。做法是**保留常量，在
调用点做并集**：

- **offer 侧**：`:2341` 传 `_SUBAGENT_DISABLED_TOOLS | web_names`
- **execute 侧**：`:2251-2262` `_child_dispatch` 的 `_SUBAGENT_ALLOWED_TOOLS`
  检查同步收紧（这是独立的 fail-closed 边界，
  返回 `"error: subagent_tool_not_allowed"`）

两处都要，因为它们是彼此独立的两道闸。`_make_task_batch_dispatcher`
（`:2182-2192`）已经收 `store`，新增一个 keyword 即可，从两个构造点
（`:4343` wake、`:5807` chat）透传。

## 5. 开关的存储与读取

### 5.1 存储：新 blob kind，不塞进 proactive_settings

`proactive_settings` 有现成的 defaults + allowlist + 校验管线，但：

- 语义不对——联网搜索不是「主动陪伴」
- 塞进去要同步改 `proactive/controls_v2.py` 的 `SWITCH_KEYS_V2`、
  `ProactiveSettingsV2` dataclass、`store_v2` 的 load/save，**改动面反而更大**，
  而且动的是一条跑着的共享结构

因此：新 blob kind `web_settings`，doc `{"version": 1, "enabled": bool}`，
**零迁移**（`user_blobs` 是 JSONB）。

**默认值 = 关闭。** 搜索目前是无条件开着的，所以这一条**改变了现有行为**，
见 §7。

### 5.2 读取：走 `TurnDeps` 注入，不在 worker 里直接读库

`TurnDeps`（`worker.py:~700-819`）是 V2 既定的注入接缝，
`runtime_mode_enabled: Callable[[str], bool] | None`（`:723`）是**同类先例**：
core 声明 callable，`serve_worker.build_production_deps()`（`:2568-2600`）
接真实实现，worker 侧用 `asyncio.to_thread` 调用（先例见 `:5291-5293`）。

新增 `web_tools_enabled: Callable[[str], bool] | None = None`，照
`:2573-2577` 的形状接线。

**为什么不直接在 worker 里 `store.load_web_settings()`**（`core.store` 是
worker 的合法 import，依赖方向测试允许）：V2 的工具循环测试
（`test_v2_tool_loop.py` / `test_v2_worker_tool_loop.py` / `test_v2_subagents.py`）
**都不在 `tests/conftest.py` 的 `_PURE_UNIT` 白名单里，只在有 Postgres 时才跑**。
注入 callable 才能写出无库也能跑的纯单测。这不是洁癖，是本地可验证性。

⚠️ `tests/test_v2_dependency_direction.py` 禁止 `v2/*.py`（除
`serve_worker.py`）import `hosted` / `agent_runtime`。本设计不违反。

### 5.3 API

新增 `GET/POST /v1/web/settings`，按 CONTRIBUTING.md 的 routes（薄）+
`*_core.py`（框架无关、可单测）拆分，DB 操作走 `threadpool.run_db`。

响应形状（沿用旧版 review 的结论，`enabled` 只表示用户偏好）：

```json
{"enabled": true, "available": true, "effective": true, "unavailable_reason": null}
```

`unavailable_reason` ∈ `globally_disabled` | `null`。
**未知状态 / 未知 reason / 字段缺失一律按不可用处理。**

⚠️ **`enabled` 永远只表示用户保存的偏好，运维开关不得回写它**——否则功能恢复后
用户还得手动重新打开。`effective = enabled && available`，算出来的，不落库。

### 5.4 不需要动 poll

`backend/chat/poll_core.py` 仍在（`poll_context` at `:26-40`），但**本设计不需要
往 poll 里加东西**：V2 的门禁完全在服务端，consumer 不参与工具装配。
iOS 直接读 `GET /v1/web/settings` 即可。

## 6. 运维 kill switch

⚠️ **V2 的 kill switch 是 DB 表驱动的，不是环境变量**
（`backend/model_api_runtime/v2/kill_switch.py`：单行表 `v2_runtime_control`
id=1、2 秒 TTL 缓存、`turns_halted()` 对 admission 调用方 fail-closed）。
V2 里的环境变量只用于**导入时校验的调优常量**，不做实时运维开关。

因此本设计**不引入新环境变量**，跟随 DB 表模式。这也比环境变量更符合
「出问题立刻下掉」——改表即刻生效，不用重启或重新部署。

语义差别要注意：`turns_halted` 是**让整个回合失败**；web 的 kill switch 应该是
**摘掉工具**（同一个 `disabled_tool_names` 机制），不能 fence 整个回合。

**待定（§9）**：两个独立开关（search / fetch 分开）意味着 `v2_runtime_control`
加两列，需要一次迁移。是否值得，见待确认项。

## 7. ⚠️ 行为变更：默认关会让现有用户失去一个已有能力

`pre` 上搜索目前**无条件可用**。本设计默认关闭后，所有用户在主动打开之前
都会失去它——回复质量会变化。这是**改到了正常流程**，不是纯旁路。

按工作区红线，这类改动必须高亮并留闸。这里的闸就是 §6 的 DB kill switch
的反向用法：**先以「默认开」上线、把开关做成纯粹的用户选择，还是以「默认关」
上线**，是一个需要 hx 明确拍板的产品决定，见 §9。

（旧版基于 test 的设计里默认关是合理的——那边搜索本来就不存在，默认关只是
「新能力不自动生效」。在 pre 上默认关的含义完全不同：**是收回一个已经在用的
能力**。这个差别是基线更换带来的，必须重新决策。）

## 8. 测试

单测（可无库运行的，须加进 `tests/conftest.py:109-150` 的 `_PURE_UNIT`）：
存储层、`*_core.py`、以及注入 callable 的判定函数。

需要 Postgres 的既有文件，按下表扩展：

| 文件 | 加什么 |
|---|---|
| `tests/test_v2_tool_loop.py` | 「用户关闭 → web 不在 offered catalog」。先例：`:216` 的 `test_child_loop_can_remove_reply_from_the_offered_catalog`（已在用 `disabled_tool_names`）、`:274` 的 `assert "web_search" not in second` |
| `tests/test_v2_worker_tool_loop.py` | chat lane 接线。先例 `:516` `assert {"web_search","web_fetch","task"}.isdisjoint(second_offered)` |
| `tests/test_v2_subagents.py` | 子 agent 的 disabled-web 变体（`:285` / `:455` 现断言 `offered == _SUBAGENT_ALLOWED_TOOLS`） |
| `tests/test_v2_wake_tool_loop.py` / `test_v2_wake_worker.py` | wake lane（**当前完全没有 disabled_tool_names 覆盖**） |
| `tests/test_capabilities_tool_schema.py` | 若改了目录构建则需配套用例（本设计不改，应保持绿） |

**验收口径**（沿用旧版 Codex review 的精确修正）：断言**成功的 web_search /
web_fetch tool_result 数量 = 0**、**无真实网络请求**、且该工具**不出现在
offered catalog** 里；不要断言「模型没调用」——模型可能幻觉式调用一个不存在的
工具，那是 UX 噪声不是安全边界。

⚠️ 本地无 Postgres 时，上表这些文件**一个都不会跑**。`_PURE_UNIT` 白名单只在
连不上库时生效，语义是「没库时只有名单里这些仍然收集」。加测试后必须
`--collect-only` 核对。

## 9. 待确认

- [ ] **§7 默认值**：`pre` 上搜索已无条件可用，默认关 = 收回现有能力。
      默认开还是默认关？（这是基线更换后必须重新做的决策）
- [ ] **§6 kill switch 粒度**：search / fetch 是否需要两个独立开关？
      两个 = `v2_runtime_control` 加两列 + 一次迁移。
- [ ] iOS 侧开关的落点与文案（设置 → 自定义设置）。
