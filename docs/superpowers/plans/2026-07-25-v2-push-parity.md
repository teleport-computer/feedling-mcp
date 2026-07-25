# Runtime V2 推送能力补齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Runtime V2 的聊天回复和 wake 主动消息都能发出 APNs 通知，投递决策链与 V1 逐字一致。

**Architecture:** V2 worker 在回合闭包里维护一个「最后一条已落库回复」槽位（明文只在进程内存活一个回合），回合收尾时经 `TurnDeps.send_reply_push` 打 backend 的内部端点；backend 复用 V1 的 `_deliver_ai_message_push_if_background` 发推送并把 delivery metadata 回写到那条 chat message。APNs 私钥仍然只存在于 backend 容器。

**Tech Stack:** Python 3.11 / FastAPI / psycopg / pytest。

**Spec:** `docs/superpowers/specs/2026-07-25-v2-push-parity-design.md`

## Global Constraints

- **明文零落库。** 推送正文只在 worker 进程内存里传递，绝不写进 `v2_effect_outbox.payload` 或任何持久化列。
- **推送是 best-effort。** 任何推送环节的异常都只记日志，绝不让回合 `mark_failed`、绝不改变回合返回值。
- **推送标题固定为 `"IO"`。** 逐字对齐 V1 的 `payload.get("title") or "IO"`，本轮不做文案改进。
- **一个回合最多一条推送。** 取本回合最后一条 `status == "applied"` 的回复正文。
- **投递判定只认 `get_effect_disposition`。** 绝不用 `apply_pending_effects` 的返回值判断某条 effect 是否落库 —— 独立 sweeper 可能抢先赢下该行，返回值只反映本次调用改了哪些行。
- **正文截断 240 字符**（进入 backend 前）；APNs 侧 80 字符截断由 V1 的 `_send_chat_alert` 已有逻辑负责，不要重复实现。
- **跑任何 DB 测试前必须起 Postgres**：`docker run -d --name feedling-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16`。没起 PG 会静默跳过约 2000 个用例，"全绿"是假象。
- **只在 worktree 隔离分支内 commit。** 当前分支是 `worktree-v2-push-parity`
  （工作目录 `.claude/worktrees/v2-push-parity`）。逐任务 commit 即可，无需另行确认。
  **绝不切换分支、绝不碰主工作树的 `test` 分支、绝不 push。** 最终如何合入由用户
  在主工作树上自行决定。

---

## File Structure

| 文件 | 职责 | 动作 |
|---|---|---|
| `tools/export_public_openapi.py` | 公开 OpenAPI 的过滤规则 | 修改：`/v1/internal` 加入排除前缀 |
| `backend/proactive/controls_v2.py` | 主动消息设置与投递判定 | 修改：新增 `load_settings_v2_for_store` |
| `backend/chat/chat_core.py` | V1 聊天路由核心 | 修改：`_settings_v2_for_store` 改为委托 |
| `backend/push/push_core.py` | 推送的框架中立逻辑 | 修改：新增 `ai_reply_push` |
| `backend/push/routes_asgi.py` | 推送路由适配层 | 修改：新增 `POST /v1/internal/push/ai_reply` |
| `backend/model_api_runtime/v2/worker.py` | V2 回合逻辑（纯逻辑层） | 修改：`wake_kind` 参数、`TurnDeps.send_reply_push`、两个 lane 的槽位与收尾 |
| `backend/model_api_runtime/v2/serve_worker.py` | V2 生产接线层 | 修改：`wake_kind` 落 extra、`_send_reply_push`、`_mint_push_token`、kill switch |
| `backend/core/store.py` | 聊天行构造 | 修改：`_build_chat_message` 的 `extra` 白名单加 `wake_kind`（**计划初版漏了这条**：该白名单是硬编码的，不加则字段静默丢弃，Task 3 实现时发现并补上） |
| `tests/test_v2_push_endpoint.py` | 端点契约与 gate | 新建 |
| `tests/test_v2_push_delivery.py` | worker 侧槽位与收尾语义 | 新建 |

---

### Task 1: `/v1/internal` 排除出公开 OpenAPI 契约

新增内部路由前，先关掉它进入公开契约的通道。否则 Task 2 落地时
`test_public_operation_and_parameter_inventory` 的 `len(operations) == 148` 会直接
变红，而且新端点会被写进对外文档。

**Files:**
- Modify: `tools/export_public_openapi.py:30-35`
- Test: `tests/openapi/test_public_openapi.py`

**Interfaces:**
- Consumes: 无
- Produces: `EXCLUDED_PREFIXES` 包含 `"/v1/internal"`，后续任务新增的
  `/v1/internal/**` 路由不会进入 `public.json`，`len(operations)` 保持 148。

- [ ] **Step 1: 写失败测试**

在 `tests/openapi/test_public_openapi.py` 末尾追加：

```python
def test_internal_prefix_is_never_public() -> None:
    """`/v1/internal/**` 是 runtime 内部面（runtime-token scope 鉴权），
    不属于对外产品 API，必须排除在公开契约之外。"""
    from export_public_openapi import EXCLUDED_PREFIXES

    assert "/v1/internal" in EXCLUDED_PREFIXES
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/openapi/test_public_openapi.py::test_internal_prefix_is_never_public -v`
Expected: FAIL，`AssertionError`（`/v1/internal` 不在元组里）

- [ ] **Step 3: 实现**

`tools/export_public_openapi.py:30`：

```python
EXCLUDED_PREFIXES = (
    "/admin",
    "/debug",
    "/v1/admin",
    "/v1/debug",
    # Runtime-internal surface (runtime-token scope auth, called only by
    # serve-worker over the compose network). Not a product API.
    "/v1/internal",
)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/openapi/ -v`
Expected: 全部 PASS（含原有的 `len(operations) == 148`）

- [ ] **Step 5: commit**

```bash
git add tools/export_public_openapi.py tests/openapi/test_public_openapi.py
git commit -m "chore(openapi): exclude /v1/internal from the public contract"
```

---

### Task 2: backend 推送端点

**Files:**
- Modify: `backend/proactive/controls_v2.py`（末尾新增函数）
- Modify: `backend/chat/chat_core.py:91-97`
- Modify: `backend/push/push_core.py`（末尾新增函数）
- Modify: `backend/push/routes_asgi.py`
- Test: `tests/test_v2_push_endpoint.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 `EXCLUDED_PREFIXES`
- Produces:
  - `proactive.controls_v2.load_settings_v2_for_store(store) -> ProactiveSettingsV2`
  - `push.push_core.ai_reply_push(store: UserStore, *, payload: dict) -> dict`
    —— `payload` 形如 `{"msg_id": str, "body": str, "is_wake": bool}`，返回
    `{"status": str, "reason": str}`
  - 路由 `POST /v1/internal/push/ai_reply`，鉴权 `require_scope("chat_push")`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_v2_push_endpoint.py`：

```python
"""POST /v1/internal/push/ai_reply —— V2 回复推送的 backend 入口。

V2 的 serve-worker 容器没有 APNs 私钥（只注入 backend），所以推送由它把明文正文
交给这个端点、再走 V1 那条完全相同的投递链（presence gate -> APNs alert ->
delivery metadata 回写）。这里断言的是契约与 gate，APNs 出网被 monkeypatch 掉。
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi import middleware  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import runtime_token  # noqa: E402
from core import store as core_store  # noqa: E402
from push import routes_asgi as push_asgi  # noqa: E402
from push import service as push_service  # noqa: E402

_SECRET = b"test-runtime-token-secret"


@pytest.fixture()
def app_obj():
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    middleware.register_exception_handlers(app)
    push_asgi.register_asgi(app)
    return app


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={
            "public_key": base64.b64encode(b"\x11" * 32).decode("ascii"),
            "archive_language": "en",
        },
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["user_id"]


def _post(app, path, json_body, headers):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            resp = await client.post(path, json=json_body, headers=headers)
            try:
                return resp.status_code, resp.json()
            except Exception:
                return resp.status_code, resp.text

    return asyncio.run(go())


def _token(user_id, scope):
    return runtime_token.mint(
        _SECRET, user_id=user_id, runtime_instance_id="v2-worker",
        scope=scope, ttl=60.0,
    )


def test_wrong_scope_is_forbidden(app_obj, user):
    status, _ = _post(
        app_obj,
        "/v1/internal/push/ai_reply",
        {"msg_id": "m1", "body": "hi"},
        {"X-Feedling-Runtime-Token": _token(user, ["envelope_decrypt"])},
    )
    assert status == 403


def test_delivers_and_writes_back_metadata(app_obj, user, monkeypatch):
    seen = {}

    def _fake_deliver(store, *, body, title="", data=None, visual_state="reply"):
        seen.update(user_id=store.user_id, body=body, title=title)
        return {"push_decision": "send", "push_reason": "no_app_presence",
                "alert_status": "delivered", "alert_reason": ""}

    monkeypatch.setattr(
        push_service, "_deliver_ai_message_push_if_background", _fake_deliver)
    written = {}
    monkeypatch.setattr(
        core_store.UserStore, "update_chat_message_metadata",
        lambda self, msg_id, fields: written.update(msg_id=msg_id, fields=fields))

    status, body = _post(
        app_obj,
        "/v1/internal/push/ai_reply",
        {"msg_id": "msg-abc", "body": "回复正文", "is_wake": False},
        {"X-Feedling-Runtime-Token": _token(user, ["chat_push"])},
    )

    assert status == 200
    assert body["status"] == "delivered"
    assert seen["body"] == "回复正文"
    assert seen["title"] == "IO"
    assert written["msg_id"] == "msg-abc"
    assert written["fields"]["alert_status"] == "delivered"


def test_wake_respects_reminders_delivery_off(app_obj, user, monkeypatch):
    from proactive import controls_v2

    monkeypatch.setattr(
        controls_v2, "load_settings_v2_for_store",
        lambda store: controls_v2.resolve_settings_v2({"reminders_delivery": False}))
    called = {"n": 0}
    monkeypatch.setattr(
        push_service, "_deliver_ai_message_push_if_background",
        lambda *a, **k: called.update(n=called["n"] + 1) or {})
    written = {}
    monkeypatch.setattr(
        core_store.UserStore, "update_chat_message_metadata",
        lambda self, msg_id, fields: written.update(fields=fields))

    status, body = _post(
        app_obj,
        "/v1/internal/push/ai_reply",
        {"msg_id": "msg-wake", "body": "主动消息", "is_wake": True},
        {"X-Feedling-Runtime-Token": _token(user, ["chat_push"])},
    )

    assert status == 200
    assert body["status"] == "suppressed"
    assert called["n"] == 0
    assert written["fields"]["alert_status"] == "suppressed"


def test_empty_body_is_skipped(app_obj, user, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        push_service, "_deliver_ai_message_push_if_background",
        lambda *a, **k: called.update(n=called["n"] + 1) or {})

    status, body = _post(
        app_obj,
        "/v1/internal/push/ai_reply",
        {"msg_id": "m1", "body": "   "},
        {"X-Feedling-Runtime-Token": _token(user, ["chat_push"])},
    )

    assert status == 200
    assert body["status"] == "skipped"
    assert called["n"] == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_v2_push_endpoint.py -v`
Expected: FAIL —— 404（路由不存在）/ `AttributeError: module 'proactive.controls_v2' has no attribute 'load_settings_v2_for_store'`

- [ ] **Step 3: 实现设置载入的单一入口**

`backend/proactive/controls_v2.py` 末尾新增：

```python
def load_settings_v2_for_store(store) -> ProactiveSettingsV2:
    """Load one user's V2 proactive settings, falling back to the store's own
    copy when the DB-backed reader is unavailable. Single source of truth for
    both the V1 chat route and the V2 push endpoint."""
    try:
        from proactive.store_v2 import DBProactiveSettingsStoreV2

        return DBProactiveSettingsStoreV2().load(store.user_id)
    except Exception:
        return resolve_settings_v2(store.load_proactive_settings())
```

`backend/chat/chat_core.py:91` 改为委托（保留函数名，V1 调用点与既有测试不变）：

```python
def _settings_v2_for_store(store: UserStore):
    from proactive.controls_v2 import load_settings_v2_for_store

    return load_settings_v2_for_store(store)
```

- [ ] **Step 4: 实现 push_core.ai_reply_push**

`backend/push/push_core.py` 末尾新增：

```python
def ai_reply_push(store: UserStore, *, payload: dict) -> dict:
    """V2 回复的推送入口 —— 与 V1 ``chat_core.response`` 走同一条投递链。

    V2 的 serve-worker 没有 APNs 私钥（只注入 backend），所以它把已落库回复的
    明文正文交到这里。正文只经过内存：不写库、不进日志正文。

    ``is_wake`` 为真表示这是 agent 主动发起的消息，额外受用户的
    ``reminders_delivery`` 开关管辖；用户发消息后的应答不受该开关影响。
    """
    from proactive.controls_v2 import evaluate_delivery_v2, load_settings_v2_for_store
    from push import service as push_service

    msg_id = str(payload.get("msg_id") or "").strip()
    body = str(payload.get("body") or "").strip()
    is_wake = bool(payload.get("is_wake"))
    if not msg_id:
        return {"status": "skipped", "reason": "missing_msg_id"}
    if not body:
        return {"status": "skipped", "reason": "empty_body"}

    if is_wake:
        decision = evaluate_delivery_v2(
            load_settings_v2_for_store(store), source="heartbeat", manual=False)
        if not decision.allow_visible_delivery:
            fields = {
                "push_decision": "suppressed",
                "push_reason": decision.reason,
                "alert_status": "suppressed",
                "alert_reason": decision.reason,
                "live_activity_status": "suppressed",
                "live_activity_reason": decision.reason,
            }
            store.update_chat_message_metadata(msg_id, fields)
            return {"status": "suppressed", "reason": decision.reason}

    fields = push_service._deliver_ai_message_push_if_background(
        store, body=body[:240], title="IO", data={}, visual_state="reply")
    store.update_chat_message_metadata(msg_id, fields)
    return {
        "status": str(fields.get("alert_status") or "unknown"),
        "reason": str(fields.get("push_reason") or ""),
    }
```

- [ ] **Step 5: 实现路由**

`backend/push/routes_asgi.py`，在 import 段加入 `require_scope`：

```python
from asgi.deps import require_auth, require_scope
```

在 `register_asgi` 之前追加：

```python
@router.post("/v1/internal/push/ai_reply")
async def internal_ai_reply_push(
    request: Request, auth: AuthResult = Depends(require_scope("chat_push"))
):
    """Runtime-internal: V2 serve-worker 把一条已落库回复的明文正文交给 backend
    发推送。不是产品 API（已排除出公开 OpenAPI 契约）。"""
    body = await _json_body(request)
    return await threadpool.run_db(push_core.ai_reply_push, auth.store, payload=body)
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_v2_push_endpoint.py tests/test_asgi_push.py tests/openapi/ -v`
Expected: 全部 PASS。特别确认 `test_public_operation_and_parameter_inventory` 仍是 148（Task 1 的排除生效）。

- [ ] **Step 7: commit**

```bash
git add backend/proactive/controls_v2.py backend/chat/chat_core.py \
        backend/push/push_core.py backend/push/routes_asgi.py \
        tests/test_v2_push_endpoint.py
git commit -m "feat(push): add runtime-internal ai_reply push endpoint for V2"
```

---

### Task 3: `wake_kind` 落库

给 V2 的回复行加一个可观测标记，区分「用户发完消息的应答」和「agent 主动 wake
消息」。推送分流和事后取证都读它。只做加法，不碰已上线的 `role` / `source` 写入
语义。

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py:3153-3179`
- Modify: `backend/model_api_runtime/v2/serve_worker.py:1810-1824`
- Test: `tests/test_v2_push_delivery.py`（新建）

**Interfaces:**
- Consumes: 无
- Produces:
  - `worker._build_encrypted_reply_effect_payload(store, text, *, effect_id, reply_through_seq=None, wake_kind="")`
    —— payload 里多一个 `"wake_kind"` 键（仅当非空时写入）
  - 落库后 `chat_messages.doc.wake_kind` 携带该值

- [ ] **Step 1: 写失败测试**

新建 `tests/test_v2_push_delivery.py`：

```python
"""V2 推送投递语义：wake_kind 标记 + 回合级推送槽位。

推送本身是 best-effort 的附加动作，这里断言的核心是「它绝不能反过来伤到消息」：
落库照旧、回合结果不受影响，而推送只在这条回复真的 applied 之后发一次。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import worker  # noqa: E402


class _FakeStore:
    user_id = "u_push_delivery"


def _fake_envelope(monkeypatch):
    """与 tests/test_v2_atomic_reply_cursor.py:589 同一个 patch 点与返回形状
    （`(envelope, error_str)`）—— 单测里不做真实 enclave 信封往返。"""
    def _build(_store, plaintext, *, item_id=None):
        return {"id": item_id, "body_ct": "ct", "nonce": "n", "K_user": "k",
                "visibility": "shared", "owner_user_id": _store.user_id}, ""

    monkeypatch.setattr(
        worker.core_envelope, "_build_shared_envelope_for_store", _build)


def test_wake_kind_is_carried_on_the_effect_payload(monkeypatch):
    _fake_envelope(monkeypatch)
    payload = worker._build_encrypted_reply_effect_payload(
        _FakeStore(), "hello", effect_id="job1:reply:0", wake_kind="heartbeat")
    assert payload["wake_kind"] == "heartbeat"


def test_chat_lane_omits_wake_kind(monkeypatch):
    _fake_envelope(monkeypatch)
    payload = worker._build_encrypted_reply_effect_payload(
        _FakeStore(), "hello", effect_id="job1:reply:0")
    assert "wake_kind" not in payload
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_v2_push_delivery.py -v`
Expected: FAIL，`TypeError: _build_encrypted_reply_effect_payload() got an unexpected keyword argument 'wake_kind'`

- [ ] **Step 3: 实现 payload 侧**

`backend/model_api_runtime/v2/worker.py:3153`，签名与函数体末尾：

```python
def _build_encrypted_reply_effect_payload(
    store,
    text: str,
    *,
    effect_id: str,
    reply_through_seq: int | None = None,
    wake_kind: str = "",
) -> dict:
```

在 `return payload` 之前插入：

```python
    if wake_kind:
        # Observability marker only: lets the reply row (and the push gate) tell an
        # agent-initiated wake message apart from a reply to the user's own message.
        # Not plaintext — a fixed vocabulary shared with the proactive_jobs log.
        payload["wake_kind"] = str(wake_kind)
```

在 wake lane 的 `_on_reply`（`worker.py:4906` 一带，`payload = await asyncio.to_thread(_build_encrypted_reply_effect_payload, …)` 调用处）补上该 lane 的 wake kind 实参，chat lane 的同名调用保持不传。

- [ ] **Step 4: 实现落库侧**

`backend/model_api_runtime/v2/serve_worker.py:1822`，`_sink_reply_in_transaction` 内：

```python
    thinking_extra = v2_worker._thinking_extra(payload.get("thinking"))
    build_extra = dict(thinking_extra) if thinking_extra else {}
    wake_kind = str(payload.get("wake_kind") or "")
    if wake_kind:
        build_extra["wake_kind"] = wake_kind
    build_kwargs = {"extra": build_extra} if build_extra else {}
    msg = store._build_chat_message("openclaw", "model_api", envelope, **build_kwargs)
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_v2_push_delivery.py tests/test_v2_effect_sinks.py tests/test_v2_atomic_reply_cursor.py -v`
Expected: 全部 PASS

- [ ] **Step 6: commit**

```bash
git add backend/model_api_runtime/v2/worker.py \
        backend/model_api_runtime/v2/serve_worker.py \
        tests/test_v2_push_delivery.py
git commit -m "feat(v2): mark agent-initiated wake replies with wake_kind"
```

---

### Task 4: `TurnDeps.send_reply_push` + wake lane 接线

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`（`TurnDeps` 约 `:972`；`_run_wake` 的 `try` 在 `:4340`，applied 分支在 `:4986` 之后）
- Test: `tests/test_v2_push_delivery.py`

**Interfaces:**
- Consumes: Task 3 的 `wake_kind`
- Produces: `TurnDeps.send_reply_push: Callable[..., None] | None`，实参为
  `(user_id: str, *, msg_id: str, body: str, is_wake: bool)`。`None` 表示特性关闭。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_v2_push_delivery.py`：

```python
def test_turn_deps_accepts_send_reply_push():
    calls = []
    deps = worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (None, {}),
        mint_enclave_token=lambda uid: "rt",
        send_reply_push=lambda uid, **kw: calls.append((uid, kw)),
    )
    deps.send_reply_push("u1", msg_id="m1", body="hi", is_wake=True)
    assert calls == [("u1", {"msg_id": "m1", "body": "hi", "is_wake": True})]


def test_send_reply_push_defaults_to_none():
    deps = worker.TurnDeps(
        read_messages=lambda uid: [],
        resolve_provider=lambda uid: (None, {}),
        mint_enclave_token=lambda uid: "rt",
    )
    assert deps.send_reply_push is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_v2_push_delivery.py -v`
Expected: FAIL，`TypeError: TurnDeps.__init__() got an unexpected keyword argument 'send_reply_push'`

- [ ] **Step 3: 加 TurnDeps 字段**

`backend/model_api_runtime/v2/worker.py`，紧跟 `apply_pending_effects` 字段之后：

```python
    # (user_id, *, msg_id, body, is_wake) -> None：把本回合最后一条已落库回复的
    # 明文正文交给 backend 发 APNs（serve-worker 容器没有 APNs 私钥，只有 backend
    # 有）。best-effort：实现方吞掉自身异常，调用点也再兜一层 —— 推送失败绝不能
    # 把一个已经成功发布回复的回合打成 failed。None（所有不接线的测试/legacy
    # 调用方）= 特性关闭，行为与补齐推送之前完全一致。
    send_reply_push: Callable[..., None] | None = None
```

- [ ] **Step 4: wake lane 写槽位 + 收尾发送**

在 `_run_wake`（`:4300`）的函数体顶部、`try:`（`:4340`）之前定义槽位：

```python
    push_slot: dict | None = None
```

在 wake lane `_on_reply` 的 applied 判定处（`disposition["status"]` 取出之后、
现有的 `if status == "applied" and not final: return` 之前）插入：

```python
                if status == "applied":
                    # 覆盖式：一个回合可能吐多条气泡，只有最后一条会成为推送。
                    nonlocal push_slot
                    push_slot = {
                        "msg_id": str(payload["envelope"]["id"]),
                        "body": text[:240],
                        "is_wake": True,
                    }
```

把 `try:`（`:4340`）的 `try/except` 补上 `finally`，在 `except`（`:5201`）之后追加：

```python
    finally:
        if push_slot is not None and deps.send_reply_push is not None:
            try:
                await asyncio.to_thread(
                    deps.send_reply_push,
                    user_id,
                    msg_id=push_slot["msg_id"],
                    body=push_slot["body"],
                    is_wake=push_slot["is_wake"],
                )
            except Exception as e:  # noqa: BLE001 — 推送绝不能影响回合结果
                log.warning(
                    "[v2.worker] wake reply push failed user=%s: %s", user_id, e)
```

- [ ] **Step 5: 覆盖范围说明（不写测试）**

wake lane 本任务只有 Step 1 的 `TurnDeps` 契约测试。行为覆盖的分工：

- **收尾发送 / 失败不伤回合** → Task 5 在 chat lane 上用真实回合覆盖。两个 lane 的
  收尾代码逐字同构，chat lane 绿了即证明该形状成立。
- **wake lane 端到端** → Task 7 的 pre 环境真机验证（第 4 条：关掉主动提醒开关）。

不要为此写一个自己构造 `push_slot` 再断言它的"测试" —— 那种测试只验证测试自己写
的循环，产品代码改坏了它照样绿。

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_v2_push_delivery.py tests/test_v2_wake_worker.py tests/test_v2_wake_success.py -v`
Expected: 全部 PASS

- [ ] **Step 7: commit**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_push_delivery.py
git commit -m "feat(v2): push the wake lane's final reply at end of turn"
```

---

### Task 5: chat lane 接线

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`（`process_job` 的 applied 分支
  `:7160`，最外层 `finally` `:7568`）
- Test: `tests/test_v2_atomic_reply_cursor.py`

**Interfaces:**
- Consumes: Task 4 的 `TurnDeps.send_reply_push`
- Produces: chat lane 回合收尾时以 `is_wake=False` 调用 `send_reply_push`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_v2_atomic_reply_cursor.py`。两条都照抄本文件
`test_final_reply_effect_surfaces_sealed_thinking`（`:690`）的完整骨架 ——
预置一条 pending reply effect，让 `process_job` 开头的 recovery drain 把它 apply
掉，provider 全程不被调用。

```python
def test_recovery_drained_reply_is_not_pushed(monkeypatch):
    """上个进程崩溃遗留的 effect 由回合开头的 recovery drain 落库 —— 它不经
    `_on_reply`，没有明文也不写槽位，所以不推送。消息照常落库。"""
    uid = "u_atomic_reply_push_recovery"
    conftest.seed_user(uid)
    _reset(uid)

    db.chat_append_strict(
        uid, "user-message", 100.0,
        {"id": "user-message", "role": "user", "ts": 100.0,
         "body_ct": "u", "nonce": "n", "K_user": "k", "K_enclave": "e"},
        5000,
    )
    input_seq = db.chat_messages_after_seq(uid, 0, limit=None)[0]["seq"]
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("push-recovery-worker")
    generation = db.get_runtime_generation(uid)
    effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type="reply",
        ordinal=0,
        expected_generation=generation,
        payload={
            "envelope": _envelope("b" * 32, body="reply-ciphertext"),
            "reply_through_seq": input_seq,
            effect_outbox.FINAL_REPLY_FENCE_KEY: {
                "claimed_by": "push-recovery-worker",
                "input_generation": 0,
                "through_seq": input_seq,
            },
        },
    )

    async def _provider(*args, **kwargs):
        raise AssertionError("recovery must drain the pending reply before model work")

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)

    def _read_after_seq(_uid: str, after_seq: int):
        if after_seq >= input_seq:
            return []
        return [{"id": "user-message", "seq": input_seq, "ts": 100.0,
                 "role": "user", "content": "hello"}]

    pushes = []
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        read_messages_after_seq=_read_after_seq,
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
        send_reply_push=lambda uid, **kw: pushes.append((uid, kw)),
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_TEST_PROVIDER_CONFIG,
        api_key=None, runtime_token="rt",
    ))

    assert status == "completed"
    assert pushes == []
    store = core_store.get_store(uid)
    store.reload()
    replies = [m for m in store.chat_messages if m.get("role") == "openclaw"]
    assert len(replies) == 1


def test_push_transport_failure_does_not_fail_the_turn(monkeypatch):
    """推送实现抛异常 —— 回合仍然 completed，回复仍然在库里。"""
    uid = "u_atomic_reply_push_boom"
    conftest.seed_user(uid)
    _reset(uid)

    db.chat_append_strict(
        uid, "user-message", 100.0,
        {"id": "user-message", "role": "user", "ts": 100.0,
         "body_ct": "u", "nonce": "n", "K_user": "k", "K_enclave": "e"},
        5000,
    )
    input_seq = db.chat_messages_after_seq(uid, 0, limit=None)[0]["seq"]
    job_id, _ = jobs_store.enqueue_job(uid, "chat")
    job = jobs_store.claim_next_job("push-boom-worker")
    generation = db.get_runtime_generation(uid)
    effect_outbox.enqueue_effect(
        job_id=job_id,
        user_id=uid,
        effect_type="reply",
        ordinal=0,
        expected_generation=generation,
        payload={
            "envelope": _envelope("b" * 32, body="reply-ciphertext"),
            "reply_through_seq": input_seq,
            effect_outbox.FINAL_REPLY_FENCE_KEY: {
                "claimed_by": "push-boom-worker",
                "input_generation": 0,
                "through_seq": input_seq,
            },
        },
    )

    async def _provider(*args, **kwargs):
        raise AssertionError("recovery must drain the pending reply before model work")

    monkeypatch.setattr(provider_client, "chat_completion_async", _provider)

    def _read_after_seq(_uid: str, after_seq: int):
        if after_seq >= input_seq:
            return []
        return [{"id": "user-message", "seq": input_seq, "ts": 100.0,
                 "role": "user", "content": "hello"}]

    def _boom(_uid, **_kw):
        raise RuntimeError("apns down")

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        read_messages_after_seq=_read_after_seq,
        resolve_provider=lambda _uid: (None, {}),
        mint_enclave_token=lambda _uid: "rt",
        apply_pending_effects=serve_worker._apply_pending_effects_for_user,
        send_reply_push=_boom,
    )

    status = asyncio.run(worker.process_job(
        job, deps, provider_config=_TEST_PROVIDER_CONFIG,
        api_key=None, runtime_token="rt",
    ))

    assert status == "completed"
    store = core_store.get_store(uid)
    store.reload()
    assert [m for m in store.chat_messages if m.get("role") == "openclaw"]
```

**正向路径的覆盖缺口（明知并接受）：** 「provider 真跑一轮 → `_on_reply` 写槽位 →
收尾推一次」这条正向链没有单测，因为在单测里把 provider、enclave envelope、outbox
全拉起来的成本高于它能捕获的回归。它由两侧夹住：backend 端点的全部分支已在 Task 2
覆盖，端到端由 Task 7 的 pre 真机验证第 1、2 条覆盖。Task 7 的验证**不是可选项**。

- [ ] **Step 2: 跑测试 —— 这两条是护栏，不是驱动**

Run: `python -m pytest tests/test_v2_atomic_reply_cursor.py -k push -v`
Expected: **PASS**。这两条守的是不变量（recovery drain 不推、推送失败不伤回合），
在接线前后都应该绿。它们的价值在 Step 4：如果实现把槽位错误地写在了 recovery
路径上、或者没兜住推送异常，它们会立刻变红。

本任务的 red-green 驱动由 Task 4 的 `TurnDeps` 契约测试和 Task 2 的端点测试承担。
不要为了凑出一个红色而把护栏测试改成断言未实现的行为。

- [ ] **Step 3: 实现**

在 `process_job`（`:6038`）函数体顶部定义 `push_slot: dict | None = None`。

在 chat lane `_on_reply` 的 `status == "applied"` 判定处（`:7160`，现有的
`if status == "applied" and not final: return` 之前）插入：

```python
                if status == "applied":
                    nonlocal push_slot
                    push_slot = {
                        "msg_id": str(payload["envelope"]["id"]),
                        "body": text[:240],
                        "is_wake": False,
                    }
```

在最外层 `finally:`（`:7568`）的 lease-keepalive 清理之后追加：

```python
        if push_slot is not None and deps.send_reply_push is not None:
            try:
                await asyncio.to_thread(
                    deps.send_reply_push,
                    user_id,
                    msg_id=push_slot["msg_id"],
                    body=push_slot["body"],
                    is_wake=push_slot["is_wake"],
                )
            except Exception as e:  # noqa: BLE001 — 推送绝不能影响回合结果
                log.warning(
                    "[v2.worker] chat reply push failed user=%s: %s", user_id, e)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_v2_atomic_reply_cursor.py tests/test_v2_worker_tool_loop.py -v`
Expected: 全部 PASS

- [ ] **Step 5: commit**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_atomic_reply_cursor.py
git commit -m "feat(v2): push the chat lane's applied reply at end of turn"
```

---

### Task 6: serve_worker 生产接线 + kill switch

**Files:**
- Modify: `backend/model_api_runtime/v2/serve_worker.py`（`_mint_runtime_token` 在
  `:430` 一带；`build_production_deps` 组装 `TurnDeps` 处）
- Test: `tests/test_v2_serve_worker.py`

**Interfaces:**
- Consumes: Task 2 的端点、Task 4 的 `TurnDeps.send_reply_push`
- Produces:
  - `serve_worker._mint_push_token(user_id) -> str`（scope `["chat_push"]`，TTL 60s）
  - `serve_worker._send_reply_push(user_id, *, msg_id, body, is_wake) -> None`
  - `FEEDLING_V2_PUSH_ENABLED`（默认开）决定是否把 `_send_reply_push` 注入 `TurnDeps`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_v2_serve_worker.py`：

```python
def test_push_token_carries_only_the_chat_push_scope():
    from core import runtime_token

    token = serve_worker._mint_push_token("u_push_scope")
    claims = runtime_token.verify(b"test-runtime-token-secret", token)
    assert claims["scope"] == ["chat_push"]
    assert claims["user_id"] == "u_push_scope"


def test_send_reply_push_posts_to_backend(monkeypatch):
    posted = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"status": "delivered"}

    def _fake_post(url, json=None, headers=None, timeout=None):
        posted.update(url=url, json=json, headers=headers)
        return _Resp()

    monkeypatch.setenv("FEEDLING_API_URL", "http://backend:5001")
    monkeypatch.setattr(serve_worker.httpx, "post", _fake_post)

    serve_worker._send_reply_push(
        "u_push_send", msg_id="m1", body="hi", is_wake=True)

    assert posted["url"] == "http://backend:5001/v1/internal/push/ai_reply"
    assert posted["json"] == {"msg_id": "m1", "body": "hi", "is_wake": True}
    assert posted["headers"]["X-Feedling-Runtime-Token"]


def test_send_reply_push_swallows_transport_errors(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setenv("FEEDLING_API_URL", "http://backend:5001")
    monkeypatch.setattr(serve_worker.httpx, "post", _boom)

    # 不抛：推送失败绝不能冒到回合上去。
    serve_worker._send_reply_push("u_push_err", msg_id="m1", body="hi", is_wake=False)


def test_kill_switch_unwires_the_push_dep(monkeypatch):
    monkeypatch.setenv("FEEDLING_V2_PUSH_ENABLED", "0")
    deps = serve_worker.build_production_deps()
    assert deps.send_reply_push is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_v2_serve_worker.py -k push -v`
Expected: FAIL，`AttributeError: module has no attribute '_mint_push_token'`

- [ ] **Step 3: 实现 token 与发送**

`backend/model_api_runtime/v2/serve_worker.py`，`_mint_runtime_token`（`:430`）之后：

```python
# Push is a separate, deliberately narrow scope: the reply-push call only needs to
# hand backend a plaintext body for an already-published row, never to decrypt
# anything. Minted per call with a short TTL — do NOT widen _RUNTIME_TOKEN_SCOPE
# (see the comment above it: the enclave's local check ignores scope, so that list
# must stay stable).
_PUSH_TOKEN_SCOPE = ["chat_push"]


def _mint_push_token(user_id: str) -> str:
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip().encode("utf-8")
    if not secret:
        raise RuntimeError("FEEDLING_RUNTIME_TOKEN_SECRET not set")
    return runtime_token.mint(
        secret,
        user_id=user_id,
        runtime_instance_id="v2-push",
        scope=_PUSH_TOKEN_SCOPE,
        ttl=60.0,
    )


def _send_reply_push(user_id: str, *, msg_id: str, body: str, is_wake: bool) -> None:
    """`TurnDeps.send_reply_push` 的生产接线。

    APNs 私钥只注入 backend 容器，所以推送由 backend 发；这里只把明文正文经
    compose 内网交过去（与 V1 consumer 走 HTTP 传 push_body 是同一个姿态）。
    完全 best-effort：任何异常都在这里吞掉并记日志，绝不冒到回合上。
    """
    api_url = os.environ.get("FEEDLING_API_URL", "").strip()
    if not api_url:
        log.warning("[v2.push] FEEDLING_API_URL not set — reply push skipped")
        return
    try:
        resp = httpx.post(
            f"{api_url}/v1/internal/push/ai_reply",
            json={"msg_id": msg_id, "body": body, "is_wake": is_wake},
            headers={"X-Feedling-Runtime-Token": _mint_push_token(user_id)},
            timeout=10.0,
        )
        if resp.status_code >= 400:
            log.warning(
                "[v2.push] reply push rejected user=%s status=%s",
                user_id, resp.status_code)
    except Exception as e:  # noqa: BLE001 — best-effort by contract
        log.warning("[v2.push] reply push failed user=%s: %s", user_id, e)
```

若 `httpx` 尚未在 `serve_worker.py` 顶部导入，补上 `import httpx`。

- [ ] **Step 4: 接进 build_production_deps**

在 `build_production_deps` 组装 `TurnDeps(...)` 的地方追加：

```python
        send_reply_push=(
            _send_reply_push
            if os.environ.get("FEEDLING_V2_PUSH_ENABLED", "1").strip() != "0"
            else None
        ),
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_v2_serve_worker.py -v`
Expected: 全部 PASS

- [ ] **Step 6: commit**

```bash
git add backend/model_api_runtime/v2/serve_worker.py tests/test_v2_serve_worker.py
git commit -m "feat(v2): wire the production reply-push transport"
```

---

### Task 7: 文档与全量回归

**Files:**
- Modify: `docs/CHANGELOG.md`
- Test: 全量

**Interfaces:**
- Consumes: Task 1–6 全部
- Produces: 可部署的完整特性

- [ ] **Step 1: 起 Postgres**

```bash
docker run -d --name feedling-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16
```

- [ ] **Step 2: 跑 L1 全量**

Run: `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`

本分支起点（`af38ca29`）已实测的真实基线，耗时约 5 分钟：

```
3 failed, 6338 passed, 1 skipped, 9 xfailed, 3 subtests passed
```

三个 pre-existing 红（与本计划无关，**不要试图修**）：

- `tests/test_chat_response_finalize_cas.py::test_finalize_reply_once_explain_uses_parent_primary_key`
- `tests/test_consumer_model_call_trace.py::test_call_agent_threads_trace_id_to_cli`
- `tests/test_e2b_template_contract.py::test_tracked_template_tag_matches_extractor_and_pinned_contract`
  （本机缺 `e2b` 模块，环境问题）

Expected: 失败集合与上面**完全一致**，passed 数不低于 6338。**新增的红一个都不接受。**
若 passed 数只有几百，说明 PG 没连上、DB 用例被静默跳过了，重查 Step 1。

- [ ] **Step 3: pyflakes**

Run: `python -m pyflakes backend/push backend/model_api_runtime/v2 backend/proactive`
Expected: 全仓恒剩 1 条 unused 是预期基线，不要新增。

- [ ] **Step 4: 写 CHANGELOG**

在 `docs/CHANGELOG.md` 顶部追加一条，说明：V2 补齐推送（此前 V2 用户收不到任何
通知）、投递链与 V1 共用、`wake_kind` 新字段、`FEEDLING_V2_PUSH_ENABLED` 回滚
拉杆、`/v1/internal` 不进公开契约。

本次不改公开 API 契约（新端点是 runtime 内部面且已排除），因此
`docs-site/content/docs/` 与 `docs-site/openapi/public.json` 无需改动。

- [ ] **Step 5: pre 环境端到端验证**

部署到 pre 后，用真机账号验证四条：

1. app 切后台 → 发一条消息 → 锁屏收到通知，点进去能看到该消息
2. app 在前台 → 收到回复但**不**弹通知，且该行 `push_decision=suppress`
3. 库里核对：`select doc->>'push_decision', doc->>'alert_status', doc->>'wake_kind'
   from chat_messages where user_id like 'usr_…%' order by seq desc limit 5;`
4. 关掉「主动提醒投递」开关 → wake 消息仍落库，但 `alert_status=suppressed`

- [ ] **Step 6: commit**

```bash
git add docs/CHANGELOG.md
git commit -m "docs: record V2 push parity"
```

---

## 已知取舍

- **worker 崩溃 → 那条推送丢失。** 上个进程崩溃后，effect 由下个回合开头的
  recovery drain（`worker.py:6243`）落库，那次 apply 不经 `_on_reply`，没有明文也
  不写槽位。消息不丢、推送丢。V1 在同样场景下也没有推送，接受。
- **一个回合多条气泡只推最后一条。** 用户点进 app 本来就能看到全部气泡，推送只是
  唤醒手段，一次足够。
- **`title` 仍是硬编码 `"IO"`。** 中文用户看到英文标题的问题留到下一轮和 V1 一起
  改，避免只改一边造成两个 runtime 观感不一致。
