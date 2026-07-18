# 聊天 provider 失败可见性 — 实现计划（第一批 / spec §2）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** provider 失败时，用户当场在自己那条消息上看到真实原因与行动入口，而不是被一句「我这会儿有点慢」糊过去。

**Architecture:** 双载体。兜底回复消息（新消息、新 ts，能通过 `since` 增量过滤）承载**实时事件**并携带 `reply_to_message_id` 供客户端配对；用户消息 metadata 承载**冗余持久化**。客户端在消息列表 reconciliation 层做关联，按 blame 决定隐藏兜底还是保留。服务端一切既有行为（兜底话术、`reply_status`、409 双扣防护、3h 节流）**一字不改**。

**Tech Stack:** Python 3（backend + consumer，pytest）、Swift/SwiftUI（iOS，无测试 target，真机验证）。

**Spec:** `docs/superpowers/specs/2026-07-18-provider-error-visibility-design.md`（第 3 稿，commit a99797b）。本计划只覆盖 §2；§3 onboarding 降级是第二批，不在本计划内。

**仓库：** 两个 worktree
- backend：`/Users/hx/Projects/io/feedling-mcp-provider-errors`（分支 `fix/provider-error-notice-blame-throttle`）
- iOS：`/Users/hx/Projects/io/feedling-mcp-ios-provider-errors`（分支 `fix/provider-error-preserve-code`）

## Global Constraints

- **只做加法**：不得修改 `FALLBACK_REPLY` 文案、`reply_status` 语义、409 双扣防护逻辑、前台横幅 3h 节流分桶。
- **后台车道不写**失败字段：`source` 为 `heartbeat` / `live_activity` / proactive job 的回复不得携带 turn-failure 字段（后台失败不进聊天流，Seven 2026-07-11 决策）。
- **不下发 detail**：只下发 `error_class` / `blame` / `user_text`。`user_text` 上限 500 字符，且不得包含原始 provider 报错文本（可能夹带 provider HTML、request id、敏感上下文）。
- **blame 取值**仅三种：`user_provider` / `provider_transient` / `system`（`backend/notices/catalog.py` 的 `VALID_BLAME`）。
- **显示矩阵**（spec §2.3）：`user_provider` → 隐藏兜底、显示失败态、给「去设置」；`provider_transient` → 隐藏兜底、显示失败态、无入口；`system` → **保留兜底话术**、不显示失败态、无入口。
- **iOS 语义分离**：`deliveryState` 保持原义（消息有没有发到服务器），**不得**改动 `ChatMessage.swift:424` 的 `deliveryState = .sent`；新增独立的 `replyFailure` 表达「agent 是否成功回答」。`retryMessage()` 不得接受 provider reply failure。
- 后端测试运行方式：`cd /Users/hx/Projects/io/feedling-mcp-provider-errors && uv run --quiet pytest <path> -q`（本机已验证可用；4 个测试文件因缺 `requests` 依赖收集失败，与本改动无关，忽略）。

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `backend/chat/chat_core.py` | 解析 payload 里的 turn-failure 字段，写进回复消息 `extra`；冗余写用户消息 metadata；metadata 写失败记 log | 修改 |
| `backend/core/store.py` | metadata allowlist 增三键 | 修改 |
| `tools/chat_resident_consumer.py` | 兜底分支把 `classify_agent_error` 结果随 `post_reply` 带上 | 修改 |
| `tests/test_chat_turn_failure_fields.py` | 后端全部新行为的测试 | 新建 |
| `App/FeedlingTest/Pages/Chat/ChatMessage.swift`（iOS） | 新增 `ReplyFailure` 类型与字段解码 | 修改 |
| `App/FeedlingTest/Pages/Chat/ChatViewModel.swift`（iOS） | reconciliation 层配对与归并矩阵 | 修改 |
| `App/FeedlingTest/Pages/Chat/ChatView.swift`（iOS） | 按 blame 渲染失败态 / 隐藏兜底 / 「去设置」 | 修改 |
| `contracts/` + `docs/FRONTEND_ERROR_CONTRACT.md` | additive public API contract 同步 | 修改 |

**字段命名（全计划统一，勿改）**

回复（兜底）消息 `extra` 上：
- `reply_to_message_id` — 指向用户消息（**当前不存在，本计划新增**：`chat_core.py` 现在只用它更新 parent 与 trace，不落回复消息 doc）
- `turn_failure_error_class` / `turn_failure_blame` / `turn_failure_user_text`

用户消息 metadata 上（冗余）：
- `reply_error_class` / `reply_blame` / `reply_user_text`

> **为何不复用 `error_class` 键**：iOS `ChatMessage` 已把顶层 `error_class` / `detail` / `status_code` / `request_id` 解码为 `noticeErrorClass` 等，供 `ChatSystemNoticeBubble`（role=system 通知气泡）使用。在兜底消息（role=openclaw）上复用同名键会造成语义混淆，故用 `turn_failure_*` 前缀隔离。

---

### Task 1: 回复消息携带 turn-failure 字段与 reply_to_message_id

**Files:**
- Modify: `backend/chat/chat_core.py`（`extra` 组装处约 626-660 行）
- Test: `tests/test_chat_turn_failure_fields.py`（新建）

**Interfaces:**
- Consumes: 无（首个任务）
- Produces: `/v1/chat/response` 接受可选 payload 字段 `turn_failure_error_class: str`、`turn_failure_blame: str`、`turn_failure_user_text: str`；当 `turn_failure_error_class` 非空且 `role != "system"` 且 `source == "chat"` 时，回复消息 doc 的 `extra` 携带这三个字段 + `reply_to_message_id`。

- [ ] **Step 1: 写失败的测试**

创建 `tests/test_chat_turn_failure_fields.py`：

```python
"""聊天回合失败元信息随兜底回复下发（spec 2026-07-18 §2）。

Run: uv run --quiet pytest tests/test_chat_turn_failure_fields.py -q
"""
from __future__ import annotations

import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    with make_client() as c:
        yield c


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _env(user_id: str, marker: str) -> dict:
    return {
        "v": 1,
        "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x02" * 12),
        "K_user": _b64(b"\x03" * 48),
        "K_enclave": _b64(b"\x04" * 48),
    }


def _send_user_msg(client, user_id: str, api_key: str, marker: str = "u1") -> str:
    res = client.post(
        "/v1/chat/send",
        json={"envelope": _env(user_id, marker), "client_msg_id": str(__import__("uuid").uuid4())},
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)
    body = res.get_json()
    return str(body.get("message_id") or body.get("id") or "")


def _history(client, api_key: str) -> list[dict]:
    res = client.get("/v1/chat/history?limit=50", headers=_headers(api_key))
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()["messages"]


def test_fallback_reply_carries_turn_failure_and_parent_link(client):
    """兜底回复是实时载体：必须同时带 turn_failure_* 与 reply_to_message_id，
    否则客户端无法在增量流里配对回它失败的那条用户消息。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key)

    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r1"),
            "source": "chat",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "quota_insufficient",
            "turn_failure_blame": "user_provider",
            "turn_failure_user_text": "模型服务额度不足，充值后再发消息即可恢复。",
        },
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)

    msgs = _history(client, api_key)
    reply = [m for m in msgs if m.get("role") == "openclaw"][-1]
    assert reply["turn_failure_error_class"] == "quota_insufficient"
    assert reply["turn_failure_blame"] == "user_provider"
    assert reply["turn_failure_user_text"].startswith("模型服务额度不足")
    assert reply["reply_to_message_id"] == parent_id


def test_normal_reply_has_no_turn_failure_fields(client):
    """成功路径零变化：不带 turn_failure_* 的回复不得凭空出现这些键。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key)

    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r2"),
            "source": "chat",
            "reply_to_message_id": parent_id,
        },
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)

    reply = [m for m in _history(client, api_key) if m.get("role") == "openclaw"][-1]
    assert "turn_failure_error_class" not in reply
    assert "turn_failure_blame" not in reply
    assert "turn_failure_user_text" not in reply


def test_user_text_truncated_to_500(client):
    """契约：user_text ≤ 500，杜绝把原始 provider detail 灌进用户可见文案。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key)

    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r3"),
            "source": "chat",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "unknown",
            "turn_failure_blame": "system",
            "turn_failure_user_text": "x" * 900,
        },
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)

    reply = [m for m in _history(client, api_key) if m.get("role") == "openclaw"][-1]
    assert len(reply["turn_failure_user_text"]) == 500
```

- [ ] **Step 2: 运行测试确认失败**

```bash
cd /Users/hx/Projects/io/feedling-mcp-provider-errors
uv run --quiet pytest tests/test_chat_turn_failure_fields.py -q
```

预期：`test_fallback_reply_carries_turn_failure_and_parent_link` 与 `test_user_text_truncated_to_500` FAIL（KeyError: 'turn_failure_error_class'）；`test_normal_reply_has_no_turn_failure_fields` PASS。

- [ ] **Step 3: 实现**

在 `backend/chat/chat_core.py` 中，找到 409 双扣防护 guard 之后、`msg = store.append_chat(` 之前的位置（约 653 行），插入：

```python
    # Turn-failure metadata（spec 2026-07-18 §2）：兜底回复是【实时载体】——它是
    # 新消息、有新 ts，能通过 /v1/chat/history 的 `since` 增量过滤；而对用户那条
    # 旧消息就地更新 metadata 不产生新 ts，永远进不了增量流。reply_to_message_id
    # 必须一并落在回复消息上（此前只用于更新 parent 与 trace，不落 doc），否则
    # 客户端在增量流里拿到失败事件却无法配对回它失败的那条用户消息。
    # 只做加法：不携带这些字段时，本段完全不执行，成功路径零变化。
    turn_failure_error_class = str(payload.get("turn_failure_error_class") or "")[:64]
    if turn_failure_error_class and role != "system" and source == "chat":
        extra["turn_failure_error_class"] = turn_failure_error_class
        extra["turn_failure_blame"] = str(payload.get("turn_failure_blame") or "")[:32]
        # ≤500 且只放 catalog 的 user_text——绝不放原始 provider detail
        # （可能夹带 provider HTML / request id / 敏感上下文）。
        extra["turn_failure_user_text"] = str(payload.get("turn_failure_user_text") or "")[:500]
        if reply_to_message_id:
            extra["reply_to_message_id"] = reply_to_message_id
```

- [ ] **Step 4: 运行测试确认通过**

```bash
uv run --quiet pytest tests/test_chat_turn_failure_fields.py -q
```

预期：3 passed。

- [ ] **Step 5: 回归确认既有聊天行为未变**

```bash
uv run --quiet pytest tests/test_chat_route_debug_trace.py tests/test_chat_poll_redelivery.py tests/test_chat_notice_fanout.py -q
```

预期：全部 passed，无新增失败。

- [ ] **Step 6: 提交**

```bash
git add backend/chat/chat_core.py tests/test_chat_turn_failure_fields.py
git commit -m "feat(chat): 兜底回复携带 turn-failure 元信息与 parent 链接

兜底回复是实时载体（新消息新 ts，能过 since 增量过滤）；对用户旧消息就地更新
metadata 不产生新 ts，永远进不了增量流。reply_to_message_id 一并落到回复消息
doc 上（此前只用于更新 parent 与 trace），否则客户端拿到失败事件无法配对。

只做加法：不带 turn_failure_* 时本段不执行，成功路径零变化。user_text 截断
500 且只放 catalog 文案，不放原始 provider detail。"
```

---

### Task 2: 用户消息 metadata 冗余持久化 + 写失败记 log

**Files:**
- Modify: `backend/core/store.py`（`update_chat_message_metadata` allowlist，约 580-599 行）
- Modify: `backend/chat/chat_core.py`（`update_chat_message_metadata` 调用处，约 661-668 行）
- Test: `tests/test_chat_turn_failure_fields.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `turn_failure_error_class` / `turn_failure_blame` / `turn_failure_user_text` payload 字段
- Produces: 用户消息 doc 上出现 `reply_error_class` / `reply_blame` / `reply_user_text`

- [ ] **Step 1: 写失败的测试**

在 `tests/test_chat_turn_failure_fields.py` 末尾追加：

```python
def test_parent_metadata_mirrors_turn_failure(client):
    """冗余持久化：用户消息 metadata 同写一份，供全量 history / 重启后恢复。
    兜底消息仍是权威载体（跨 worker 时 metadata 可能静默写失败）。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key)

    res = client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r4"),
            "source": "chat",
            "reply_to_message_id": parent_id,
            "turn_failure_error_class": "auth_invalid",
            "turn_failure_blame": "user_provider",
            "turn_failure_user_text": "API Key 无效或已过期，请到设置里重新保存。",
        },
        headers=_headers(api_key),
    )
    assert res.status_code in (200, 201), res.get_data(as_text=True)

    parent = [m for m in _history(client, api_key) if m.get("id") == parent_id][0]
    assert parent["reply_error_class"] == "auth_invalid"
    assert parent["reply_blame"] == "user_provider"
    assert parent["reply_user_text"].startswith("API Key")
    # 既有语义不得改变
    assert parent["reply_status"] == "replied"


def test_parent_metadata_absent_on_normal_reply(client):
    """成功回合不得写这些键（成功路径零变化）。"""
    user_id, api_key = _register(client)
    parent_id = _send_user_msg(client, user_id, api_key)

    client.post(
        "/v1/chat/response",
        json={
            "envelope": _env(user_id, "r5"),
            "source": "chat",
            "reply_to_message_id": parent_id,
        },
        headers=_headers(api_key),
    )

    parent = [m for m in _history(client, api_key) if m.get("id") == parent_id][0]
    assert "reply_error_class" not in parent
    assert parent["reply_status"] == "replied"
```

- [ ] **Step 2: 运行测试确认失败**

```bash
uv run --quiet pytest tests/test_chat_turn_failure_fields.py::test_parent_metadata_mirrors_turn_failure -q
```

预期：FAIL（KeyError: 'reply_error_class'）。

- [ ] **Step 3: allowlist 加三键**

在 `backend/core/store.py` 的 `update_chat_message_metadata` 的 `allowed` 集合里，`"replied_at",` 之后追加：

```python
            # 回合失败冗余持久化（spec 2026-07-18 §2.2）。权威载体是兜底回复消息；
            # 这里是全量 history / 重启后的恢复路径。
            "reply_error_class",
            "reply_blame",
            "reply_user_text",
```

- [ ] **Step 4: 调用处同写 + 写失败记 log**

在 `backend/chat/chat_core.py` 中，把现有的 metadata 更新块替换为：

```python
    if reply_to_message_id and role != "system":
        _meta: dict = {
            "reply_status": "replied",
            "reply_message_id": str(msg.get("id") or ""),
            "replied_by": consumer_id,
            "replied_at": f"{time.time():.3f}",
        }
        if turn_failure_error_class and source == "chat":
            _meta["reply_error_class"] = turn_failure_error_class
            _meta["reply_blame"] = str(payload.get("turn_failure_blame") or "")[:32]
            _meta["reply_user_text"] = str(payload.get("turn_failure_user_text") or "")[:500]
        # 返回 None = parent 不在本 worker 内存里，metadata 静默没落库。既有代码
        # 忽略了返回值；失败必须可见，但绝不能影响回合收尾——兜底回复消息才是
        # 权威载体，这里只是冗余（spec §2.1）。
        if store.update_chat_message_metadata(reply_to_message_id, _meta) is None:
            log.warning(
                "chat reply metadata not persisted (parent not in this worker): parent=%s",
                reply_to_message_id,
            )
        _maybe_mark_first_chat_ok(store, reply_to_message_id)
```

若 `chat_core.py` 尚未定义模块级 `log`，在文件的 import 区之后加：

```python
import logging

log = logging.getLogger(__name__)
```

（先 `grep -n "^log = \|^import logging" backend/chat/chat_core.py` 确认，已存在则不要重复定义。）

- [ ] **Step 5: 运行测试确认通过**

```bash
uv run --quiet pytest tests/test_chat_turn_failure_fields.py -q
```

预期：5 passed。

- [ ] **Step 6: 回归**

```bash
uv run --quiet pytest tests/ -q -k "chat" --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py --ignore=tests/test_bootstrap_gates.py --ignore=tests/test_multi_tenant_isolation.py
```

预期：全部 passed，无新增失败。

- [ ] **Step 7: 提交**

```bash
git add backend/core/store.py backend/chat/chat_core.py tests/test_chat_turn_failure_fields.py
git commit -m "feat(chat): 用户消息 metadata 冗余持久化回合失败 + 写失败记 log

兜底回复消息是权威载体；metadata 是全量 history / 重启后的恢复路径。
update_chat_message_metadata 仅在 parent 在本 worker 内存时才落库，原调用处
忽略返回值——跨 worker 会静默丢失，现改为记 warning（仍不影响回合收尾）。

reply_status 语义与 409 双扣防护逻辑一字未改。"
```

---

### Task 3: consumer 在兜底分支带上失败元信息

**Files:**
- Modify: `tools/chat_resident_consumer.py`（`post_reply` 签名约 6122 行、body 组装约 6206 行、前台 chat lane 的 `post_kwargs` 约 9129 行）
- Test: `tests/test_consumer_error_classify.py`（追加）

**Interfaces:**
- Consumes: Task 1 定义的三个 payload 字段
- Produces: `post_reply(..., turn_failure_error_class=..., turn_failure_blame=..., turn_failure_user_text=...)`

- [ ] **Step 1: 写失败的测试**

在 `tests/test_consumer_error_classify.py` 末尾追加：

```python
def test_fallback_post_carries_turn_failure_kwargs(monkeypatch):
    """兜底回复必须把分类结果带给后端——这是 iOS 实时看到失败原因的唯一通路。"""
    captured = {}

    def _fake_post_reply(text, **kw):
        captured["text"] = text
        captured.update(kw)
        return {"id": "m1"}

    monkeypatch.setattr(crc, "post_reply", _fake_post_reply)
    notice = crc.classify_agent_error(RuntimeError("cli agent exited 1: 402 余额不足"))
    kwargs = crc.turn_failure_post_kwargs(notice)

    assert kwargs["turn_failure_error_class"] == "quota_insufficient"
    assert kwargs["turn_failure_blame"] == "user_provider"
    assert kwargs["turn_failure_user_text"] == notice.user_text
    assert "detail" not in kwargs           # 绝不下发原始报错
    assert len(kwargs["turn_failure_user_text"]) <= 500


def test_turn_failure_kwargs_empty_for_none():
    """无失败时返回空 dict——成功路径不得凭空带字段。"""
    assert crc.turn_failure_post_kwargs(None) == {}
```

- [ ] **Step 2: 运行测试确认失败**

```bash
FEEDLING_TEST_PG="postgresql://invalid" uv run --quiet pytest tests/test_consumer_error_classify.py -q
```

预期：FAIL（`AttributeError: module has no attribute 'turn_failure_post_kwargs'`）。

- [ ] **Step 3: 实现 helper 与 post_reply 参数**

在 `tools/chat_resident_consumer.py` 的 `_system_notice_body` 函数之后，加：

```python
def turn_failure_post_kwargs(notice: "AgentErrorNotice | None") -> dict:
    """把分类结果转成 post_reply 的 turn-failure kwargs（spec 2026-07-18 §2.2）。

    只带 error_class / blame / user_text——detail 绝不下发（可能夹带 provider
    HTML、request id、敏感上下文；排障走设置页 last_runtime_error 与 admin 面）。
    无失败时返回空 dict，成功路径零变化。"""
    if notice is None:
        return {}
    return {
        "turn_failure_error_class": notice.error_class[:64],
        "turn_failure_blame": notice.blame[:32],
        "turn_failure_user_text": notice.user_text[:500],
    }
```

在 `post_reply` 签名中，`notice_kind: str = "",` 之后追加：

```python
    turn_failure_error_class: str = "",
    turn_failure_blame: str = "",
    turn_failure_user_text: str = "",
```

在 `post_reply` 的 body 组装处，`if notice_kind:` 块之后追加：

```python
            if turn_failure_error_class:
                body["turn_failure_error_class"] = turn_failure_error_class
                body["turn_failure_blame"] = turn_failure_blame
                body["turn_failure_user_text"] = turn_failure_user_text
```

- [ ] **Step 4: 前台 chat lane 接线**

在前台 chat lane 的 `post_kwargs` 组装处（`post_kwargs = {}` 之后、`result = post_reply(reply, **post_kwargs)` 之前），追加：

```python
                # 兜底回复才带失败元信息：pending_failure_notice 非空即表示本轮
                # 是兜底糊的、不是真回复。只给第一条（兜底只有一条）。
                if idx == 0 and pending_failure_notice is not None:
                    post_kwargs.update(
                        turn_failure_post_kwargs(classify_agent_error(pending_failure_notice))
                    )
```

**不要**改动 proactive/后台车道的 `post_kwargs`（约 8511 行那处）——后台失败不进聊天流。

- [ ] **Step 5: 运行测试确认通过**

```bash
FEEDLING_TEST_PG="postgresql://invalid" uv run --quiet pytest tests/test_consumer_error_classify.py -q
```

预期：全部 passed（含原有 31 项 + 新增 2 项）。

- [ ] **Step 6: 提交**

```bash
git add tools/chat_resident_consumer.py tests/test_consumer_error_classify.py
git commit -m "feat(consumer): 兜底回复带上失败分类，供客户端实时展示

只带 error_class/blame/user_text，detail 绝不下发。仅前台 chat lane 且
pending_failure_notice 非空时携带；后台车道不进聊天流，保持原样。"
```

---

### Task 4: iOS 解码新字段与 ReplyFailure 类型

**Files:**
- Modify: `/Users/hx/Projects/io/feedling-mcp-ios-provider-errors/App/FeedlingTest/Pages/Chat/ChatMessage.swift`

**Interfaces:**
- Consumes: Task 1/2 下发的 `turn_failure_*`、`reply_to_message_id`、`reply_error_class` 等字段
- Produces: `struct ReplyFailure { let errorClass: String; let blame: ReplyFailureBlame; let userText: String }`；`ChatMessage.turnFailure: ReplyFailure?`（兜底消息自带）、`ChatMessage.replyToMessageID: String?`、`ChatMessage.replyFailure: ReplyFailure?`（用户消息上的结果，可由解码或 reconciliation 赋值）

- [ ] **Step 1: 新增类型与字段**

在 `ChatMessage.swift` 中 `ChatDeliveryState` 定义之后加：

```swift
/// 归责三分类（后端 notices/catalog.py 的 VALID_BLAME）。
enum ReplyFailureBlame: String, Codable {
    case userProvider = "user_provider"
    case providerTransient = "provider_transient"
    case system
}

/// 「agent 有没有成功回答这一轮」——与 `ChatDeliveryState`（消息有没有发到
/// 服务器）是不同语义，不可混用。spec 2026-07-18 §2.6。
struct ReplyFailure: Equatable {
    let errorClass: String
    let blame: ReplyFailureBlame
    let userText: String

    /// user_provider 才给行动入口：他不去改配置就永远不好。
    var showsSettingsEntry: Bool { blame == .userProvider }
    /// system 是我们的锅，保留有温度的兜底话术，不显示失败态。
    var hidesFallbackBubble: Bool { blame != .system }
}
```

在属性区 `let noticeDetail: String?` 之后加：

```swift
    /// 兜底消息自带的回合失败信息（实时载体）。
    let turnFailure: ReplyFailure?
    /// 兜底消息指向的用户消息 id，用于 reconciliation 配对。
    let replyToMessageID: String?
    /// 本条用户消息的回合结果。可由服务端 metadata 解出，或由 reconciliation
    /// 依 `turnFailure` 事件回填。与 `deliveryState` 语义无关。
    var replyFailure: ReplyFailure? = nil
```

在 `CodingKeys` 中加：

```swift
        case replyToMessageID = "reply_to_message_id"
        case turnFailureErrorClass = "turn_failure_error_class"
        case turnFailureBlame = "turn_failure_blame"
        case turnFailureUserText = "turn_failure_user_text"
        case replyErrorClass = "reply_error_class"
        case replyBlame = "reply_blame"
        case replyUserText = "reply_user_text"
```

- [ ] **Step 2: 解码**

在 `init(from:)` 里，**不要动** `deliveryState = .sent` 那一行，另加：

```swift
        replyToMessageID = try? c.decode(String.self, forKey: .replyToMessageID)

        // 兜底消息自带（实时载体）
        if let cls = (try? c.decode(String.self, forKey: .turnFailureErrorClass)),
           !cls.isEmpty {
            turnFailure = ReplyFailure(
                errorClass: cls,
                blame: ReplyFailureBlame(
                    rawValue: (try? c.decode(String.self, forKey: .turnFailureBlame)) ?? "system"
                ) ?? .system,
                userText: (try? c.decode(String.self, forKey: .turnFailureUserText)) ?? ""
            )
        } else {
            turnFailure = nil
        }

        // 用户消息 metadata（冗余持久化，全量 history / 重启后恢复）
        if let cls = (try? c.decode(String.self, forKey: .replyErrorClass)), !cls.isEmpty {
            replyFailure = ReplyFailure(
                errorClass: cls,
                blame: ReplyFailureBlame(
                    rawValue: (try? c.decode(String.self, forKey: .replyBlame)) ?? "system"
                ) ?? .system,
                userText: (try? c.decode(String.self, forKey: .replyUserText)) ?? ""
            )
        }
```

同时在**所有** memberwise `init(...)` 里给 `turnFailure` / `replyToMessageID` 补默认值 `nil`（编译器会指出缺哪个）。

- [ ] **Step 3: 编译验证**

```bash
cd /Users/hx/Projects/io/feedling-mcp-ios-provider-errors/App
xcodebuild -scheme FeedlingTest -project FeedlingTest.xcodeproj \
  -destination 'generic/platform=iOS Simulator' -configuration Debug build \
  CODE_SIGNING_ALLOWED=NO 2>&1 | tail -5
```

预期：`** BUILD SUCCEEDED **`

- [ ] **Step 4: 提交**

```bash
cd /Users/hx/Projects/io/feedling-mcp-ios-provider-errors
git add App/FeedlingTest/Pages/Chat/ChatMessage.swift
git commit -m "feat(chat): 解码回合失败字段，新增 ReplyFailure 类型

replyFailure 表达「agent 有没有成功回答」，与 deliveryState（消息有没有发到
服务器）是不同语义；deliveryState 原有行为一字未改。"
```

---

### Task 5: iOS reconciliation 层配对与归并矩阵

**Files:**
- Modify: `/Users/hx/Projects/io/feedling-mcp-ios-provider-errors/App/FeedlingTest/Pages/Chat/ChatViewModel.swift`（`upsertMessages` 处，约 495-520 行）

**Interfaces:**
- Consumes: Task 4 的 `ChatMessage.turnFailure` / `replyToMessageID` / `replyFailure`
- Produces: `func reconcileReplyFailures(_ messages: inout [ChatMessage])`；调用后，被兜底事件指向的用户消息其 `replyFailure` 已回填

- [ ] **Step 1: 实现归并函数**

在 `ChatViewModel.swift` 中加：

```swift
    /// 把兜底失败事件归并回它所回复的那条用户消息（spec 2026-07-18 §2.4）。
    ///
    /// 必须在【每次】 upsertMessages、冷缓存恢复、加载 older 之后重跑——不能只在
    /// 「实时收到一条新消息」时执行，否则分页加载后归并不上。
    ///
    /// 归并矩阵：
    /// - 事件 + parent 都在 → 事件优先，回填 parent.replyFailure
    /// - 只有 parent metadata → 解码时已填，保持
    /// - 只有事件、parent 未加载（分页切割）→ 不动，事件自身作为独立失败提示展示
    /// - 两者冲突 → 事件优先
    func reconcileReplyFailures(_ messages: inout [ChatMessage]) {
        var failureByParent: [String: ReplyFailure] = [:]
        for m in messages {
            if let f = m.turnFailure, let parent = m.replyToMessageID, !parent.isEmpty {
                failureByParent[parent] = f     // 事件优先（同 parent 后到者覆盖）
            }
        }
        guard !failureByParent.isEmpty else { return }
        for i in messages.indices {
            if let f = failureByParent[messages[i].id] {
                messages[i].replyFailure = f
            }
        }
    }
```

- [ ] **Step 2: 在所有装载路径上调用**

在 `upsertMessages` 内，写回 `self.messages` 之前调用一次：

```swift
        reconcileReplyFailures(&result)
```

用以下命令找出全部需要接线的位置，逐处确认都在写回 `messages` 前调用了 `reconcileReplyFailures`：

```bash
cd /Users/hx/Projects/io/feedling-mcp-ios-provider-errors
grep -n "messages = \|self.messages = \|upsertMessages(" App/FeedlingTest/Pages/Chat/ChatViewModel.swift
```

预期需覆盖：增量轮询合并、全量 history 装载、冷缓存恢复、加载 older 分页。

- [ ] **Step 3: 编译验证**

```bash
cd /Users/hx/Projects/io/feedling-mcp-ios-provider-errors/App
xcodebuild -scheme FeedlingTest -project FeedlingTest.xcodeproj \
  -destination 'generic/platform=iOS Simulator' -configuration Debug build \
  CODE_SIGNING_ALLOWED=NO 2>&1 | tail -5
```

预期：`** BUILD SUCCEEDED **`

- [ ] **Step 4: 提交**

```bash
cd /Users/hx/Projects/io/feedling-mcp-ios-provider-errors
git add App/FeedlingTest/Pages/Chat/ChatViewModel.swift
git commit -m "feat(chat): 把兜底失败事件归并回对应的用户消息

在每次 upsert / 冷缓存恢复 / 加载 older 后重跑，分页切割时 parent 未加载则
不归并，事件自身仍作为独立提示展示（避免错误原因和兜底一起消失）。"
```

---

### Task 6: iOS 按 blame 渲染

**Files:**
- Modify: `/Users/hx/Projects/io/feedling-mcp-ios-provider-errors/App/FeedlingTest/Pages/Chat/ChatView.swift`（消息列表渲染处，约 428-450 行）
- Modify: `/Users/hx/Projects/io/feedling-mcp-ios-provider-errors/App/FeedlingTest/Localizable.xcstrings`

**Interfaces:**
- Consumes: Task 4/5 的 `ChatMessage.replyFailure` / `turnFailure`
- Produces: 用户可见的失败态渲染

- [ ] **Step 1: 加本地化文案**

用与阶段一 `scene.error.code_suffix` 相同的手法，在 `Localizable.xcstrings` 的 `"strings" : {` 之后插入（en / zh-Hans 两语言）：

- `chat.reply_failure.undelivered` → `未送达` / `Not delivered`
- `chat.reply_failure.open_settings` → `去设置` / `Open Settings`

插入后用 `python3 -c "import json;json.load(open('App/FeedlingTest/Localizable.xcstrings'))"` 验证 JSON 合法。

- [ ] **Step 2: 隐藏兜底气泡**

在消息列表渲染的 `ForEach` 过滤处，跳过「已被归并、且 blame 非 system」的兜底事件：

```swift
    /// 兜底气泡是否该显示。system 类保留（我们的锅，留住有温度的话）；
    /// user_provider / provider_transient 隐藏，改由用户消息上的失败态承载。
    /// parent 尚未加载（分页切割）时不隐藏——否则错误原因会连兜底一起消失。
    private func showsFallbackBubble(_ msg: ChatMessage, in messages: [ChatMessage]) -> Bool {
        guard let f = msg.turnFailure, f.hidesFallbackBubble else { return true }
        guard let parent = msg.replyToMessageID else { return true }
        return !messages.contains { $0.id == parent }   // parent 未加载 → 仍显示
    }
```

- [ ] **Step 3: 用户消息上渲染失败态**

在用户消息气泡下方，`replyFailure` 非 nil 且 blame 非 system 时渲染：

```swift
    @ViewBuilder
    private func replyFailureRow(_ f: ReplyFailure) -> some View {
        if f.blame != .system {
            VStack(alignment: .trailing, spacing: 4) {
                Text("⚠️ " + "chat.reply_failure.undelivered".localized + " · " + f.userText)
                    .font(CinType.caption)
                    .foregroundStyle(Color.cinDanger)
                    .multilineTextAlignment(.trailing)
                if f.showsSettingsEntry {
                    Button("chat.reply_failure.open_settings".localized) {
                        openModelAPISettings()
                    }
                    .font(CinType.caption.weight(.medium))
                }
            }
        }
    }
```

`Color.cinDanger` / `CinType.caption` 若不存在，用 `grep -n "cinDanger\|cinWarning" App/FeedlingTest/` 找到本仓实际的令牌名替换（遵循 iOS 仓 DESIGN.md 令牌，不要硬编码颜色）。`openModelAPISettings()` 复用现有跳模型配置页的入口——用 `grep -rn "ModelAPISettingsView(" App/FeedlingTest/` 找到现有导航方式。

**不得**渲染重试按钮：`retryMessage()` 只服务真正的 delivery failure。

- [ ] **Step 4: 编译验证**

```bash
cd /Users/hx/Projects/io/feedling-mcp-ios-provider-errors/App
xcodebuild -scheme FeedlingTest -project FeedlingTest.xcodeproj \
  -destination 'generic/platform=iOS Simulator' -configuration Debug build \
  CODE_SIGNING_ALLOWED=NO 2>&1 | tail -5
```

预期：`** BUILD SUCCEEDED **`

- [ ] **Step 5: 提交**

```bash
cd /Users/hx/Projects/io/feedling-mcp-ios-provider-errors
git add App/FeedlingTest/Pages/Chat/ChatView.swift App/FeedlingTest/Localizable.xcstrings
git commit -m "feat(chat): 按归责渲染回合失败态

user_provider/provider_transient 隐藏兜底气泡、在用户消息上显示原因，
user_provider 另给「去设置」；system 保留兜底话术不显示失败态。
不渲染重试按钮——余额不足时重试无效，用户需要的是充值。"
```

---

### Task 7: 契约同步

**Files:**
- Modify: `docs/FRONTEND_ERROR_CONTRACT.md`
- Modify: `contracts/`（用 `ls contracts/` 确认 OpenAPI 文件名）

**Interfaces:**
- Consumes: Task 1/2 的字段定义
- Produces: 文档化的 additive public API contract

- [ ] **Step 1: 更新前端契约文档**

在 `docs/FRONTEND_ERROR_CONTRACT.md` 增一节，说明：兜底回复消息可能携带 `turn_failure_error_class` / `turn_failure_blame` / `turn_failure_user_text` / `reply_to_message_id`；用户消息可能携带 `reply_error_class` / `reply_blame` / `reply_user_text`；**兜底回复消息为权威载体**；`user_text` ≤ 500 且不含原始 provider detail；按 blame 的显示矩阵（照抄 spec §2.3 的表）。

- [ ] **Step 2: 同步 OpenAPI**

```bash
cd /Users/hx/Projects/io/feedling-mcp-provider-errors
ls contracts/
uv run --quiet pytest tests/test_public_openapi_contract.py -q 2>/dev/null \
  || grep -rn "openapi" tools/export_public_openapi.py | head -5
```

按本仓既有方式重新导出/更新公开 OpenAPI，并运行契约测试确认通过。

- [ ] **Step 3: 全量后端回归**

```bash
uv run --quiet pytest tests/ -q \
  --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py \
  --ignore=tests/test_bootstrap_gates.py --ignore=tests/test_multi_tenant_isolation.py
```

预期：无新增失败（上述 4 个文件因本机缺 `requests` 依赖收集失败，与本改动无关）。

- [ ] **Step 4: 提交**

```bash
git add docs/FRONTEND_ERROR_CONTRACT.md contracts/
git commit -m "docs(contract): 回合失败字段进前端契约与 OpenAPI

additive public API contract：新增字段不改变既有字段语义，老版客户端忽略即可。"
```

---

### Task 8: 真机验收

**Files:** 无（验证任务）

- [ ] **Step 1: 部署后端与 consumer 到 test**

按本仓既有流程部署（backend 与 agent-runner 镜像同批 —— consumer 有改动）。

- [ ] **Step 2: 逐项真机验证**

装 iOS build 到真机，逐条走：

1. **正常聊天零变化（最高优先级）**：发文字、发图、连续多轮对话 —— 与改动前完全一致，无失败态、无异常隐藏。
2. **实时性**：把 provider key 改成无效值 → 发消息 → **不杀 App、不下拉刷新** → 当场看到「未送达 · API Key 无效或已过期」+「去设置」，且兜底话术气泡不出现。
3. **余额不足**：用一个欠费的 key → 确认文案是充值指引。
4. **system 类**：制造一次 `turn_timeout`（或用 debug 手段）→ 确认**仍显示兜底话术**「我这会儿有点慢…」，无失败态、无「去设置」。
5. **重启持久**：在第 2 步的失败态下杀掉 App 重开 → 失败态仍在。
6. **分页 orphan**：多发几轮把失败那轮顶到上一页 → 冷启动只加载最新页时，确认兜底事件不被隐藏、作为独立提示展示；下拉加载 older 后归并到用户消息上。

- [ ] **Step 3: 记录结果**

把 6 项的实际结果（通过 / 不通过 + 现象）写进 PR 描述。**任何一项不通过都不合并。**

---

## Self-Review

**Spec 覆盖检查（§2 全部条目）**

| spec 条目 | 对应任务 |
|---|---|
| §2.1 双载体、兜底消息为权威 | Task 1（事件载体）+ Task 2（冗余）|
| §2.2 字段定义、user_text ≤500、不下发 detail | Task 1、Task 3（helper 里剔除 detail）|
| §2.3 显示矩阵 | Task 6 + Task 4 的 `hidesFallbackBubble` / `showsSettingsEntry` |
| §2.4 归并矩阵（分页 orphan） | Task 5 + Task 6 Step 2 的 parent-未加载分支 |
| §2.5 后端三处改动 | Task 1、Task 2、Task 3 |
| §2.6 语义分离、不动 deliveryState、retry 不接受 | Task 4（新增 replyFailure）+ Task 6（不渲染重试）|
| §2.7 契约同步 | Task 7 |
| 测试清单 iOS 1–6 项 | Task 8 |
| 后台车道不写 | Task 3 Step 4 显式约束 |

**类型一致性**：`ReplyFailure` / `ReplyFailureBlame` / `turnFailure` / `replyToMessageID` / `replyFailure` / `reconcileReplyFailures` 在 Task 4/5/6 中命名一致；后端字段名 `turn_failure_*` / `reply_*` 在 Task 1/2/3/7 中一致。

**已知需实现者现场确认的点**（非占位符，是本仓约定的查找步骤，均给了确切命令）：
- `chat_core.py` 是否已有模块级 `log`（Task 2 Step 4 给了 grep 命令）
- iOS 颜色/字体令牌实际名、跳设置页的现有入口（Task 6 Step 3 给了 grep 命令）
- `contracts/` 下 OpenAPI 的实际文件名与导出方式（Task 7 Step 2 给了命令）
