# Runtime V2 Provider 空响应恢复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Runtime V2 正确区分可纠正的语义空回复与 OpenRouter 异常空 completion，避免盲目重试，并统一归因到 `provider_empty_reply`。

**Architecture:** Provider client 继续负责 wire 解码，但 V2 tool loop 一律以 `require_reply=False` 获取结构合法的成功响应，再由 foreground policy 判断是否必须回复。tool loop 对带 reasoning 或正常 stop reason 的空回复执行最多一次、保留当前工具安全状态的语义纠正；对 stop reason 和 reasoning 都为空的异常 completion 立即失败。Worker 将新的内容无关异常映射到现有稳定状态码和 notice vocabulary。

**Tech Stack:** Python 3.11、asyncio、httpx、pytest、Runtime V2 provider-native tool loop、PostgreSQL 测试夹具。

## Global Constraints

- 只修改 Runtime V2；不修改 V1 resident、pi、Claude driver、session 或 actual-model 校验。
- 每个 foreground turn 最多一次语义空回复纠正，且计入现有 `max_calls`。
- OpenRouter 异常空 completion 不执行相同 payload 的 reliable retry。
- Wake lane 的 `require_reply=False` 静默成功语义保持不变。
- 已被安全策略禁用的工具不会因为纠正而重新启用；其他情况下纠正保留当前工具目录。
- 普通日志不得新增 prompt、用户内容、reasoning 正文、工具参数、工具结果或 API Key。
- 不新增公共错误类型，终态继续使用 `provider_empty_reply`。
- 所有生产代码必须先有失败测试，并观察到预期 RED。

---

### Task 1: 锁定 V2 空响应策略的失败测试

**Files:**
- Modify: `tests/test_v2_tool_loop.py`

**Interfaces:**
- Consumes: `tool_loop.run_tool_loop(..., require_reply: bool = True)` 和现有 `_ScriptedProvider` 测试夹具。
- Produces: 对 `tool_loop.ProviderEmptyReply`、一次性纠正和 Provider parser lenient 调用的行为契约。

- [ ] **Step 1: 增加异常空 completion 不重试测试**

```python
def test_foreground_abnormal_empty_completion_fails_without_retry(monkeypatch):
    provider = _ScriptedProvider([{
        "reply": "",
        "reasoning": "",
        "stop_reason": "",
        "tool_calls": [],
        "usage": {"prompt_tokens": 18504, "completion_tokens": 3},
    }])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)

    with pytest.raises(tool_loop.ProviderEmptyReply):
        asyncio.run(tool_loop.run_tool_loop(
            provider_config=_TEST_PROVIDER_CONFIG,
            build_messages=_RecordingBuildMessages(),
            dispatch_tools=_RecordingDispatch(),
            on_reply=_RecordingReply(),
            fold_new_messages=_RecordingFold([]),
            add_usage=_noop_add_usage,
            max_calls=5,
        ))

    assert len(provider.calls) == 1
    assert provider.calls[0]["require_reply"] is False
```

- [ ] **Step 2: 增加 thinking-only 一次纠正后成功测试**

```python
def test_foreground_semantic_empty_response_gets_one_correction(monkeypatch):
    provider = _ScriptedProvider([
        {
            "reply": "",
            "reasoning": "private first attempt",
            "stop_reason": "max_tokens",
            "tool_calls": [],
            "usage": {"completion_tokens": 4096},
        },
        {
            "reply": "recovered",
            "reasoning": "final reasoning",
            "stop_reason": "end_turn",
            "tool_calls": [],
            "usage": {"completion_tokens": 4},
        },
    ])
    monkeypatch.setattr(provider_client, "chat_completion_async", provider)
    published = []

    async def publish(text, *, final, reasoning=""):
        published.append((text, final, reasoning))

    outcome = asyncio.run(tool_loop.run_tool_loop(
        provider_config=_TEST_PROVIDER_CONFIG,
        build_messages=_RecordingBuildMessages(),
        dispatch_tools=_RecordingDispatch(),
        on_reply=publish,
        fold_new_messages=_RecordingFold([]),
        add_usage=_noop_add_usage,
        max_calls=5,
    ))

    assert outcome.final_text == "recovered"
    assert published == [("recovered", True, "final reasoning")]
    assert len(provider.calls) == 2
    assert "Do not return a thinking-only response" in provider.calls[1]["messages"][0]["content"]
```

- [ ] **Step 3: 增加纠正后真实工具调用测试**

构造三轮脚本：`thinking-only` → 标准 `memory_index` tool call → 最终文本。断言第二轮仍提供 `memory_index`，dispatcher 收到一次真实调用，第三轮包含 provider-native `ToolExchange`，最终文本成功发布。

- [ ] **Step 4: 增加第二次语义空回复终止测试**

构造两次带 reasoning/stop reason 的空成功响应，断言只调用两次 Provider，第二次抛 `ProviderEmptyReply`，不会获得第三次纠正机会。

- [ ] **Step 5: 增加加密 trajectory 内容无关摘要测试**

传入 recording `on_trajectory_event`，断言事件包含：

```python
{
    "reason": "empty_provider_success",
    "response_shape": {
        "stop_reason": "max_tokens",
        "has_visible_text": False,
        "reasoning_present": True,
        "tool_call_count": 0,
        "completion_tokens": 4096,
    },
    "action": "semantic_correction",
}
```

断言摘要不包含 reasoning 正文或 messages。

- [ ] **Step 6: 运行定向测试并确认 RED**

Run:

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_v2_tool_loop.py -k 'empty_completion or semantic_empty or empty_response' -q
```

Expected: FAIL，因为 `ProviderEmptyReply` 和语义纠正分支尚不存在；失败不是导入、拼写或夹具错误。

---

### Task 2: 实现 V2 response-shape 分流与一次性纠正

**Files:**
- Modify: `backend/model_api_runtime/v2/tool_loop.py`
- Test: `tests/test_v2_tool_loop.py`

**Interfaces:**
- Consumes: provider result fields `reply`, `reasoning`, `stop_reason`, `tool_calls`, normalized `usage`。
- Produces: `ProviderEmptyReply`, `_empty_response_shape(pr) -> dict[str, object]`，以及 one-shot correction state。

- [ ] **Step 1: 定义内容无关异常和纠正指令**

```python
class ProviderEmptyReply(RuntimeError):
    """A structurally valid provider success had no foreground-usable output."""


_EMPTY_RESPONSE_CORRECTION = (
    "The previous response completed without visible text or a client tool call. "
    "Complete the user's request now. Return either non-empty visible answer text "
    "or a valid call to one of the offered client tools. Do not return a "
    "thinking-only response."
)
```

- [ ] **Step 2: 增加安全 response-shape helper**

```python
def _empty_response_shape(pr: ProviderResponse) -> dict[str, object]:
    return {
        "stop_reason": str(pr.raw.get("stop_reason") or ""),
        "has_visible_text": bool(pr.text.strip()),
        "reasoning_present": bool(str(pr.raw.get("reasoning") or "").strip()),
        "tool_call_count": len(pr.tool_calls),
        "completion_tokens": pr.usage.completion_tokens,
    }
```

该 helper 不读取或返回正文。

- [ ] **Step 3: 让 tool loop 获取结构合法的空成功响应**

Provider 调用 kwargs 固定包含：

```python
provider_kwargs = {"tools": tools, "require_reply": False}
```

`require_reply` 函数参数继续控制 lane 业务语义：foreground 为 True，wake 为 False。

- [ ] **Step 4: 注入一次性纠正状态**

在 turn 状态中增加：

```python
empty_response_recovery_used = False
empty_response_retry_instruction = ""
```

构建 messages 时，将 `delivery_retry_instruction` 和
`empty_response_retry_instruction` 作为临时 system suffix 合并；不写入 transcript。

- [ ] **Step 5: 在工具分发前执行响应分流**

解析 `ProviderResponse` 后，foreground 且 text/tool calls 都为空时：

```python
semantic_empty = bool(
    str(pr.raw.get("reasoning") or "").strip()
    or str(pr.raw.get("stop_reason") or "").strip()
)
can_correct = (
    semantic_empty
    and not empty_response_recovery_used
    and attempts < max_calls - 1
)
```

`can_correct` 时记录 trajectory、清除 reasoning fragments、设置临时纠正指令并
`continue`。否则记录 action=`fail_provider_empty_reply` 并抛
`ProviderEmptyReply("empty_reply")`。如果工具已因 prompt frontier 或安全 fallback 被禁用，
纠正不得重新启用它们。

- [ ] **Step 6: Provider success/failure callback 与空响应语义对齐**

只有可用文本、工具调用或 wake 合法静默才调用 `on_provider_success`。终态
`ProviderEmptyReply` 在抛出前调用一次 `on_provider_failure`；第一次可纠正空响应不清除
Provider failure streak，也不提前记 terminal failure。

- [ ] **Step 7: 运行 Task 1 测试并确认 GREEN**

Run:

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest tests/test_v2_tool_loop.py -q
```

Expected: PASS，且现有 tool-loop 测试无回归。

- [ ] **Step 8: 提交 response policy**

```bash
git add backend/model_api_runtime/v2/tool_loop.py tests/test_v2_tool_loop.py
git commit -m "fix(v2): recover semantic empty provider responses"
```

---

### Task 3: 接通 Worker 稳定错误分类

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`
- Modify: `tests/test_v2_worker.py`

**Interfaces:**
- Consumes: `v2_tool_loop.ProviderEmptyReply`。
- Produces: 稳定 `turn_failed:empty_reply` 状态码和 `provider_empty_reply` notice class。

- [ ] **Step 1: 先写 Worker 分类失败测试**

在现有参数化分类测试中加入：

```python
(v2_tool_loop.ProviderEmptyReply("empty_reply"), "provider_empty_reply")
```

再增加 `_safe_failure_code` 断言：

```python
assert worker._safe_failure_code(
    "turn_failed", v2_tool_loop.ProviderEmptyReply("empty_reply")
) == "turn_failed:empty_reply"
```

- [ ] **Step 2: 运行测试并确认 RED**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_v2_worker.py::test_v2_turn_failure_classification_uses_shared_notice_vocabulary \
  -q
```

Expected: 新增 case FAIL，当前异常会落 `unknown`，safe code 会使用类名。

- [ ] **Step 3: 实现稳定映射**

在 `_safe_failure_code()` 中将 `ProviderEmptyReply` 映射为 `empty_reply`；在
`_turn_failure_error_class()` 中将其映射为 `provider_empty_reply`。不要修改 notice catalog
或新增 error class。

- [ ] **Step 4: 运行 Worker 定向测试并确认 GREEN**

Run 同 Step 2。Expected: PASS。

- [ ] **Step 5: 提交 Worker attribution**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_worker.py
git commit -m "fix(v2): classify empty provider successes accurately"
```

---

### Task 4: 更新 V2 设计与公开变更记录

**Files:**
- Modify: `docs/superpowers/specs/2026-08-07-runtime-v2-empty-provider-response-recovery-design.md`
- Modify: `docs-site/content/docs/changelog.mdx`

**Interfaces:**
- Consumes: 实际实现后的 response-shape 判定和 retry 行为。
- Produces: 与代码一致的设计记录和 Unreleased 用户可见行为说明。

- [ ] **Step 1: 修正设计文档的证据边界**

明确拆分：

- Anthropic 直连的可能 thinking-only；
- OpenRouter 的 `finish_reason=null`、无 reasoning、极少 completion token 的异常空
  completion；
- 真实 prompt 形态相关只是当前最强相关性，不是已确认上游根因；
- V2 foreground 显式 `max_attempts=2`，不是 3。

- [ ] **Step 2: 记录最终恢复策略**

设计文档必须写清：语义空回复最多纠正一次；异常空 completion 立即失败；token 数量只作
辅助诊断；安全禁用工具不会在纠正中恢复。

- [ ] **Step 3: 更新 Unreleased changelog**

增加用户可见条目：Runtime V2 不再把 Provider 的 HTTP 200 空回复误报为上游网络故障；
可纠正的 reasoning-only 响应有一次有界恢复，异常空 completion 返回准确的模型渠道提示。

- [ ] **Step 4: 文档检查并提交**

```bash
git diff --check
git add docs/superpowers/specs/2026-08-07-runtime-v2-empty-provider-response-recovery-design.md \
  docs-site/content/docs/changelog.mdx
git commit -m "docs: record V2 empty response recovery"
```

---

### Task 5: 回归验证与 Test 环境准备

**Files:**
- Verify only; no planned production edits.

**Interfaces:**
- Consumes: Tasks 1-4 的完整实现。
- Produces: 本地验证证据和可部署到 test 的分支状态。

- [ ] **Step 1: 运行纯单元回归**

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_provider_client.py tests/test_v2_tool_loop.py -q
```

Expected: 全部 PASS。

- [ ] **Step 2: 运行数据库型 Worker 定向回归**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_v2_worker.py tests/test_v2_worker_tool_loop.py -q
```

Expected: 全部 PASS；不得静默跳过数据库模块。

- [ ] **Step 3: 运行相关安全与 trajectory 回归**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_v2_trajectory_unit.py tests/test_v2_wake_worker.py \
  tests/test_v2_wake_tool_loop.py tests/test_provider_tools_no_reply_text.py -q
```

Expected: 全部 PASS。

- [ ] **Step 4: 静态与 diff 检查**

```bash
git diff --check test...HEAD
git status --short
git log --oneline test..HEAD
```

Expected: 无 whitespace error；只有计划内文件；提交边界清晰。

- [ ] **Step 5: 准备 test 环境 E2E 矩阵**

部署前列出专用 V2 用户矩阵：Anthropic Fable 5 thinking-only recovery、OpenRouter 异常空
completion、真实 `memory_index`、Opus 4.8/5 回归、wake 静默。部署与真实 Provider 调用另行
执行并保留加密 trajectory 证据。
