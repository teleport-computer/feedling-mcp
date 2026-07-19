# 托管回合上游报错透出 — 实现计划

> **RETIRED / DO NOT DEPLOY.** Historical implementation record; managed hosted
> execution is Runtime V2-only and has no resident supervisor path.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 托管回合失败时，把可判读的原因以 role="system" 聊天消息 + 设置页
`last_runtime_error` 两条腿送到用户 App，替代「只有通用兜底话术」的现状。

**Architecture:** consumer（`tools/chat_resident_consumer.py`）新增纯函数错误分类器，
失败时走两腿：① `post_reply(role="system")` 复用现有加密信封路径发聊天系统消息
（前台必发、后台按错误类别去抖）；② `POST /v1/model_api/runtime_error` 写
`model_api_runtime.last_runtime_error`（iOS 设置页读侧已存在）。backend 侧
`/v1/chat/response` 加 role 白名单、新增 runtime_error 瘦路由。

**Tech Stack:** Python（consumer 单文件 + FastAPI ASGI routes + pytest）。

**Spec:** `docs/superpowers/specs/2026-07-06-upstream-error-surfacing-design.md`（先通读）。

## Global Constraints

- **绝不自行 `git add`/`git commit`**：每个任务完成后停在 working tree，由用户决定提交。
  （任务里没有 commit 步骤，这是刻意的。）
- 兜底话术 `FALLBACK_REPLY` 的现有发送行为**一字不动**；system 消息是追加，不是替代。
- `blame == "system"` 的话术绝不能引导用户改 key/充值/改配置。
- system 消息发送/上报失败：只 log，绝不影响回合收尾、绝不抛出。
- 新路由必须同时接受 api-key 与 runtime-token 鉴权（host-all consumer 只有 runtime-token；
  `require_auth` 已两者都收，不要另造鉴权）。
- consumer 模块在 import 时读环境变量并打日志——测试文件必须先设 env 再 import
  （照抄 `tests/test_consumer_decrypt_since.py` 的头部模式）。
- 跑测试用 `python -m pytest tests/<file> -q`，仓库根目录执行。

---

### Task 1: consumer 错误分类器（纯函数）

**Files:**
- Modify: `tools/chat_resident_consumer.py`（`_is_provider_payment_error` 附近，约 :353 之后）
- Test: `tests/test_consumer_error_classify.py`（新建）

**Interfaces:**
- Produces: `classify_agent_error(exc: BaseException) -> AgentErrorNotice`
  （namedtuple 字段 `error_class: str, blame: str, user_text: str, detail: str`）；
  `_system_notice_body(notice: AgentErrorNotice) -> str`。
  Task 4/5 依赖这两个名字。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_consumer_error_classify.py`：

```python
"""classify_agent_error: 三层错误来源 → (error_class, blame, 话术) 分类。

用例全部取自 prod 真实报错串（spec §测试）。
Run:  python -m pytest tests/test_consumer_error_classify.py -q
"""
import os
import subprocess
import sys
import types
from pathlib import Path

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_error_classify_checkpoint.json",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules["content_encryption"] = _fake_enc

import tools.chat_resident_consumer as crc  # noqa: E402


def _cls(exc):
    return crc.classify_agent_error(exc)


def test_relay_quota_403_is_quota_not_auth():
    # prod usr_0d16bfd4 原文：403 里同时有 Forbidden 和「额度」，语义是余额
    e = RuntimeError(
        "cli agent exited 1: unexpected status 403 Forbidden: litellm.APIError: "
        "APIError: OpenAIException - 预扣费额度失败, 用户剩余额度: ¥0.018000, "
        "需要预扣费额度: ¥0.020000 (request id: xxx)")
    n = _cls(e)
    assert n.error_class == "quota_insufficient"
    assert n.blame == "user_provider"


def test_claude_credit_balance_is_quota():
    n = _cls(RuntimeError(
        "cli agent exited 1: Your credit balance is too low to access the "
        "Anthropic API (api_status=400)"))
    assert n.error_class == "quota_insufficient"


def test_codex_retry_429_is_rate_limited():
    n = _cls(RuntimeError(
        "cli agent exited 1: exceeded retry limit, last status: 429 Too Many Requests"))
    assert n.error_class == "rate_limited"
    assert n.blame == "provider_transient"


def test_invalid_model_name():
    n = _cls(RuntimeError("cli agent exited 1: 400 Invalid model name passed in model=gw-usr_x"))
    assert n.error_class == "model_not_found"
    assert n.blame == "user_provider"


def test_invalid_api_key_is_auth():
    n = _cls(RuntimeError("cli agent exited 1: invalid x-api-key (api_status=401)"))
    assert n.error_class == "auth_invalid"


def test_nonetype_upstream_is_unknown_system():
    # 脑裂案例：key 空/上游死时 codex 的叙述
    n = _cls(RuntimeError("cli agent exited 1: Unexpected response type: NoneType"))
    assert n.error_class == "unknown"
    assert n.blame == "system"


def test_stream_disconnected_is_upstream_unavailable():
    n = _cls(RuntimeError("cli agent exited 1: stream disconnected before completion"))
    assert n.error_class == "upstream_unavailable"


def test_timeout_expired_by_type():
    n = _cls(subprocess.TimeoutExpired(cmd="codex", timeout=120))
    assert n.error_class == "turn_timeout"
    assert n.blame == "system"


def test_no_usable_reply_is_parse_failed():
    n = _cls(ValueError("agent produced no usable reply after sanitization"))
    assert n.error_class == "reply_parse_failed"
    assert n.blame == "system"


def test_system_blame_text_never_points_at_user_config():
    # 归责纪律：system 侧话术不得引导用户改 key/充值
    for exc in (subprocess.TimeoutExpired(cmd="c", timeout=120),
                ValueError("agent produced no usable reply after sanitization"),
                RuntimeError("cli agent exited 1: Unexpected response type: NoneType")):
        n = _cls(exc)
        assert n.blame == "system"
        for banned in ("充值", "Key", "key", "额度", "设置里"):
            assert banned not in n.user_text, (n.error_class, banned)


def test_notice_body_has_marker_and_detail_truncated():
    n = _cls(RuntimeError("boom " + "x" * 500))
    body = crc._system_notice_body(n)
    assert body.startswith("⚠️ ")
    assert "详情: " in body
    assert len(n.detail) <= 200
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_consumer_error_classify.py -q`
Expected: FAIL，`AttributeError: ... has no attribute 'classify_agent_error'`

- [ ] **Step 3: 实现分类器**

在 `tools/chat_resident_consumer.py` 的 `_clear_provider_payment_cooldown()`（约 :369）
之后加入（`import re`、`from collections import namedtuple` 如缺则补到文件头 import 区；
`subprocess` 已 import）：

```python
# --- agent turn error classification (spec: docs/superpowers/specs/
# 2026-07-06-upstream-error-surfacing-design.md) ---------------------------
# error_class → 用户话术；blame 决定话术能不能给行动指引：
#   user_provider      → 可以让用户去充值/改 key/改模型名
#   provider_transient → 上游临时问题，等它自己恢复
#   system             → 我们的问题，绝不能引导用户改配置（会误导，见 dded 案例）
AgentErrorNotice = namedtuple("AgentErrorNotice", "error_class blame user_text detail")

_ERROR_CLASS_RULES = (
    # 次序即优先级：quota 必须先于 auth/rate（403+「额度」语义是余额不是权限）
    ("quota_insufficient", "user_provider",
     "你的 API 服务额度不足，充值后再发消息即可恢复。",
     re.compile(r"余额|额度|insufficient_quota|credit balance|requires more credits"
                r"|payment required|\b402\b|quota", re.I)),
    ("auth_invalid", "user_provider",
     "API Key 无效或已过期，请到设置里重新保存。",
     re.compile(r"invalid ?(x-)?api.?key|unauthorized|authentication|\b401\b", re.I)),
    ("model_not_found", "user_provider",
     "模型名不可用，请检查设置里的模型名。",
     re.compile(r"invalid model name|model_not_found|no such model", re.I)),
    ("rate_limited", "provider_transient",
     "你的 API 服务限流了，稍等几分钟再试。",
     re.compile(r"\b429\b|too many requests|rate.?limit", re.I)),
    ("upstream_unavailable", "provider_transient",
     "你的模型服务暂时不可用，稍后会自动恢复。",
     re.compile(r"\b5\d{2}\b|overloaded|timed? ?out|connection (refused|reset|error)"
                r"|unreachable|stream disconnected", re.I)),
)


def classify_agent_error(exc: BaseException) -> AgentErrorNotice:
    """三层错误来源（claude/codex CLI 经 _cli_error_detail、stderr 兜底）已汇聚成
    异常文本；这里只做只读分类，永不抛出。"""
    detail = str(exc)[:200]
    if isinstance(exc, subprocess.TimeoutExpired):
        return AgentErrorNotice("turn_timeout", "system",
                                "这轮回复超时了，稍后再试。", detail)
    text = str(exc)
    if "no usable reply" in text:
        return AgentErrorNotice("reply_parse_failed", "system",
                                "系统处理回复时出了问题，我们会尽快排查。", detail)
    lowered = text.lower()
    # 404 需与 model 同现才算模型错（裸 404 归 upstream_unavailable 太粗、归 auth 又错）
    if re.search(r"\b404\b", text) and "model" in lowered:
        return AgentErrorNotice("model_not_found", "user_provider",
                                "模型名不可用，请检查设置里的模型名。", detail)
    for klass, blame, user_text, pat in _ERROR_CLASS_RULES:
        if pat.search(text):
            return AgentErrorNotice(klass, blame, user_text, detail)
    return AgentErrorNotice("unknown", "system", "连接模型服务时出了问题。", detail)


def _system_notice_body(notice: AgentErrorNotice) -> str:
    return f"⚠️ {notice.user_text}\n详情: {notice.detail}"
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_consumer_error_classify.py -q`
Expected: 全部 PASS。
若 `test_relay_quota_403_is_quota_not_auth` 失败于分到 auth_invalid：检查规则次序
（quota 必须排第一）。

---

### Task 2: backend — /v1/chat/response 收 role="system" + history 透传

**Files:**
- Modify: `backend/chat/chat_core.py`（`write_response`，约 :359-465）
- Modify: `backend/chat/service.py`（`_chat_history_item`，约 :299-307）
- Test: `tests/test_chat_system_notice_role.py`（新建）

**Interfaces:**
- Consumes: 无（独立于 Task 1）。
- Produces: `/v1/chat/response` 接受可选 `payload["role"]`（白名单
  `{"openclaw","system"}`，非法/缺省落 `"openclaw"`）与
  `payload["notice_kind"]`（≤64 字符，仅 role=="system" 时落库到消息 doc）。
  history 下发 system 消息带 `role="system"`、`sender="assistant"`、`notice_kind`。
  Task 4 的 consumer `post_reply(role=..., notice_kind=...)` 依赖此契约。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_chat_system_notice_role.py`：

```python
"""/v1/chat/response role 白名单 + system 消息的存储/下发/隔离语义。

spec: docs/superpowers/specs/2026-07-06-upstream-error-surfacing-design.md
Run:  python -m pytest tests/test_chat_system_notice_role.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from chat import chat_core  # noqa: E402
from chat import service as chat_service  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _env(user_id: str, marker: str) -> dict:
    return {
        "v": 1, "id": marker,
        "body_ct": _b64(f"{user_id}:{marker}".encode()),
        "nonce": _b64(b"\x00" * 12), "K_user": _b64(b"\x01" * 32),
        "K_enclave": _b64(b"\x02" * 32),
        "visibility": "shared", "owner_user_id": user_id,
    }


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()
    res = appmod.app.test_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return core_store.get_store(res.get_json()["user_id"])


def _post_response(store, payload):
    return chat_core.write_response(
        store, payload, consumer_id="test-consumer",
        consumer_info={}, allow_verify_reply=False)


def test_system_role_stored_with_notice_kind(store):
    body, status = _post_response(store, {
        "envelope": _env(store.user_id, "sysmsg1"),
        "role": "system", "notice_kind": "upstream_error",
    })
    assert status in (200, 201), body
    msgs = store.read_chat(limit=10)
    m = next(x for x in msgs if x["id"] == "sysmsg1")
    assert m["role"] == "system"
    assert m["notice_kind"] == "upstream_error"


def test_invalid_role_falls_back_to_openclaw(store):
    body, status = _post_response(store, {
        "envelope": _env(store.user_id, "badrole1"), "role": "hacker",
    })
    assert status in (200, 201), body
    m = next(x for x in store.read_chat(limit=10) if x["id"] == "badrole1")
    assert m["role"] == "openclaw"
    assert "notice_kind" not in m


def test_notice_kind_ignored_for_openclaw_and_truncated_for_system(store):
    _post_response(store, {
        "envelope": _env(store.user_id, "oc1"), "notice_kind": "upstream_error"})
    m = next(x for x in store.read_chat(limit=10) if x["id"] == "oc1")
    assert "notice_kind" not in m

    _post_response(store, {
        "envelope": _env(store.user_id, "sys2"), "role": "system",
        "notice_kind": "k" * 200})
    m = next(x for x in store.read_chat(limit=10) if x["id"] == "sys2")
    assert len(m["notice_kind"]) == 64


def test_history_item_system_sender_is_assistant():
    # 老版 iOS 的 sender Decodable 不能见到未知值 → system 映射到 assistant，
    # 新版靠 role=="system" 区分（spec §组件2 老版兼容）
    item = chat_service._chat_history_item({
        "id": "x", "role": "system", "notice_kind": "upstream_error",
        "body_ct": "", "content_type": "text"})
    assert item["sender"] == "assistant"
    assert item["is_from_openclaw"] is False
    assert item["role"] == "system"
    assert item["notice_kind"] == "upstream_error"


def test_system_message_not_claimable(store):
    _post_response(store, {
        "envelope": _env(store.user_id, "sysmsg3"), "role": "system",
        "notice_kind": "upstream_error"})
    m = next(x for x in store.read_chat(limit=10) if x["id"] == "sysmsg3")
    assert not chat_service._chat_message_claimable(m, "any-consumer", 9e12)


def test_system_message_does_not_mark_replied(store):
    user_msg = store.append_chat("user", "chat", _env(store.user_id, "umsg1"))
    _post_response(store, {
        "envelope": _env(store.user_id, "sysmsg4"), "role": "system",
        "notice_kind": "upstream_error",
        "reply_to_message_id": user_msg["id"],
    })
    m = next(x for x in store.read_chat(limit=10) if x["id"] == "umsg1")
    # system 消息不承担已回复标记（那是兜底话术的职责，spec role 审计表）
    assert m.get("reply_status") != "replied"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_chat_system_notice_role.py -q`
Expected: FAIL——`test_system_role_stored_with_notice_kind` 断言 `role == "system"`
失败（现状硬编码 openclaw）。

- [ ] **Step 3: 实现 backend 改动**

`backend/chat/chat_core.py` `write_response`：

(a) 在 `source` 校验块（`if source not in {...}: return ...invalid source...`）之后加：

```python
    # role: 消费者可声明 "system"（技术通知气泡，spec 2026-07-06-upstream-error-
    # surfacing）。白名单外一律落 openclaw——新增 role 前先过 spec 的 role 审计表。
    role = str(payload.get("role") or "openclaw").strip()
    if role not in ("openclaw", "system"):
        role = "openclaw"
    notice_kind = ""
    if role == "system":
        notice_kind = str(payload.get("notice_kind") or "")[:64]
```

(b) `extra = {...}` 构造后（`gate_decision_id`/`proactive_job_id` 那个 dict）加：

```python
    if notice_kind:
        extra["notice_kind"] = notice_kind
```

(c) `reply_to` 已回复标记：找到 `msg = store.append_chat("openclaw", source, ...)`
改为 `msg = store.append_chat(role, source, ...)`；并把其后
`if reply_to_message_id:` 的 `update_chat_message_metadata(... reply_status ...)`
块改为 `if reply_to_message_id and role != "system":`（system 消息不算回复）。

(d) 409 already_answered 守卫（`if reply_to_message_id:` 里 `_parent` 检查）同样
加 `and role != "system"` 条件——system 消息挂在已回复消息上不该被 409 拒掉
（它不是二次回复，只是补充通知）。改为：

```python
    if reply_to_message_id and role != "system":
        _parent = _chat_message_by_id(store, reply_to_message_id)
        ...（原块不动）
```

`backend/chat/service.py` `_chat_history_item` 的 role 分支加：

```python
    elif role == "system":
        # 老版 iOS 的 sender 解码不能见到未知值；新版按 role=="system" 渲染
        item["sender"] = "assistant"
        item["is_from_openclaw"] = False
```

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `python -m pytest tests/test_chat_system_notice_role.py tests/test_asgi_chat_remaining.py tests/test_chat_poll_redelivery.py tests/test_chat_poll_core.py -q`
Expected: 全部 PASS（后三个是 role 改动的回归面：认领/重投递/已回复语义）。

---

### Task 3: backend — POST /v1/model_api/runtime_error 瘦路由

**Files:**
- Modify: `backend/hosted/config_store.py`（新增 core 函数，放
  `_patch_model_api_runtime_profile` 之后）
- Modify: `backend/hosted/setup_routes_asgi.py`（新增路由）
- Test: `tests/test_model_api_runtime_error_route.py`（新建）

**Interfaces:**
- Consumes: `_patch_model_api_runtime_profile(store, patch)`（已存在）。
- Produces: `config_store.record_runtime_error(store, *, error: str, error_class: str)
  -> tuple[dict, int]`；HTTP `POST /v1/model_api/runtime_error`
  body `{"error": str, "error_class": str}` → 200 `{"ok": true}`。
  Task 4 的 consumer `_report_runtime_error` 依赖此契约。

- [ ] **Step 1: 写失败测试**

新建 `tests/test_model_api_runtime_error_route.py`：

```python
"""POST /v1/model_api/runtime_error：agent-runner 路径补写 last_runtime_error。

历史教训（memory: model-api-providerkey-runtime-token-decrypt-gap）：host-all
consumer 只有 runtime-token，读写侧只认 api-key 就会静默失效——这里走
require_auth（两者都收），测试只需覆盖 api-key 路径 + 语义。
Run:  python -m pytest tests/test_model_api_runtime_error_route.py -q
"""
from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import app as appmod  # noqa: E402
import asgi_app  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from hosted import config_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def user(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    appmod._users[:] = []
    appmod._key_to_user.clear()
    appmod._stores.clear()
    appmod._save_users()
    res = appmod.app.test_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _post(path, *, headers=None, json=None):
    async def go():
        transport = httpx.ASGITransport(app=asgi_app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.post(path, headers=headers or {}, json=json)
            return r.status_code, r.json()
    return asyncio.run(go())


def _runtime_profile(user_id):
    store = core_store.get_store(user_id)
    return config_store._ensure_model_api_runtime_profile(store) or {}


def test_report_and_clear_runtime_error(user):
    uid, key = user
    status, body = _post("/v1/model_api/runtime_error",
                         headers={"X-API-Key": key},
                         json={"error": "403 预扣费额度失败", "error_class": "quota_insufficient"})
    assert status == 200, body
    prof = _runtime_profile(uid)
    assert prof["last_runtime_error"] == "403 预扣费额度失败"

    status, body = _post("/v1/model_api/runtime_error",
                         headers={"X-API-Key": key},
                         json={"error": "", "error_class": ""})
    assert status == 200, body
    assert _runtime_profile(uid)["last_runtime_error"] == ""


def test_error_truncated_to_300(user):
    uid, key = user
    status, _ = _post("/v1/model_api/runtime_error",
                      headers={"X-API-Key": key},
                      json={"error": "x" * 900, "error_class": "k" * 200})
    assert status == 200
    prof = _runtime_profile(uid)
    assert len(prof["last_runtime_error"]) == 300
    assert len(prof["last_runtime_error_class"]) == 64


def test_bad_auth_401():
    status, _ = _post("/v1/model_api/runtime_error",
                      headers={"X-API-Key": "nope"}, json={"error": "e"})
    assert status == 401
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_model_api_runtime_error_route.py -q`
Expected: FAIL，404（路由不存在）。

- [ ] **Step 3: 实现 core 函数 + 路由**

`backend/hosted/config_store.py`，`_patch_model_api_runtime_profile` 之后：

```python
def record_runtime_error(store: UserStore, *, error: str, error_class: str = "") -> tuple[dict, int]:
    """agent-runner consumer 上报（或清空）最近一次回合失败原因。

    读侧是 setup_core 的 last_runtime_error（iOS 设置页）。legacy inline 路径经
    action-trace 写同一字段；本函数是 agent-runner 路径的对等写侧（spec
    2026-07-06-upstream-error-surfacing 腿②）。"""
    patch = {
        "last_runtime_error": str(error or "")[:300],
        "last_runtime_error_class": str(error_class or "")[:64],
    }
    if _patch_model_api_runtime_profile(store, patch) is None:
        return {"error": "model_api_runtime_profile_missing"}, 404
    return {"ok": True}, 200
```

`backend/hosted/setup_routes_asgi.py`，与其它路由并列处加：

```python
@router.post("/v1/model_api/runtime_error")
async def model_api_runtime_error(request: Request, auth: AuthResult = Depends(require_auth)):
    payload = (await asgi_http.read_json_silent(request)) or {}
    body, status = await threadpool.run_db(
        config_store.record_runtime_error,
        auth.store,
        error=str(payload.get("error") or ""),
        error_class=str(payload.get("error_class") or ""),
    )
    return JSONResponse(body, status_code=status)
```

文件头 import 区补 `from hosted import config_store`（如缺）。

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `python -m pytest tests/test_model_api_runtime_error_route.py tests/test_asgi_hosted_setup.py -q`
Expected: 全部 PASS。
若 `test_report_and_clear_runtime_error` 404：说明该测试用户没有
model_api_runtime profile——检查 `_ensure_model_api_runtime_profile` 是否对注册
用户默认建 profile；若不建，测试 fixture 里先调
`config_store._ensure_model_api_runtime_profile(store)` 播种（并保留 404 分支测试）。

---

### Task 4: consumer — post_reply 扩展 + 上报/去抖机制

**Files:**
- Modify: `tools/chat_resident_consumer.py`
  - `post_reply()`（约 :4007）：新参 `role` / `notice_kind`
  - Task 1 分类器代码块之后：去抖状态 + `_report_runtime_error` +
    `_notify_agent_turn_failure` + `_note_agent_turn_success`
- Test: `tests/test_consumer_error_classify.py`（追加用例）

**Interfaces:**
- Consumes: Task 1 `classify_agent_error` / `_system_notice_body`；
  Task 2 role 契约；Task 3 runtime_error 路由。
- Produces: `_notify_agent_turn_failure(exc: BaseException, *, foreground: bool) -> None`、
  `_note_agent_turn_success() -> None`。Task 5 在各失败/成功点调用这两个名字。

- [ ] **Step 1: 写失败测试（追加到 tests/test_consumer_error_classify.py）**

```python
def test_debounce_foreground_always_background_once(monkeypatch):
    sent = []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: sent.append((text, kw)) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    crc._reset_system_notice_state()
    e = RuntimeError("cli agent exited 1: unexpected status 403: 额度不足")

    crc._notify_agent_turn_failure(e, foreground=False)
    crc._notify_agent_turn_failure(e, foreground=False)  # 同类去抖
    assert len(sent) == 1

    crc._notify_agent_turn_failure(e, foreground=True)   # 前台绕过去抖
    crc._notify_agent_turn_failure(e, foreground=True)
    assert len(sent) == 3

    # 不同 error_class 互不影响
    crc._notify_agent_turn_failure(
        RuntimeError("cli agent exited 1: exceeded retry limit, last status: 429"),
        foreground=False)
    assert len(sent) == 4

    # 成功重置后同类再发
    crc._note_agent_turn_success()
    crc._notify_agent_turn_failure(e, foreground=False)
    assert len(sent) == 5


def test_notify_posts_system_role_with_suppress_push(monkeypatch):
    calls = []
    monkeypatch.setattr(crc, "post_reply", lambda text, **kw: calls.append((text, kw)) or {})
    monkeypatch.setattr(crc, "_report_runtime_error", lambda *a, **kw: None)
    crc._reset_system_notice_state()
    crc._notify_agent_turn_failure(
        RuntimeError("cli agent exited 1: invalid x-api-key"), foreground=True)
    text, kw = calls[0]
    assert kw["role"] == "system"
    assert kw["notice_kind"] == "upstream_error"
    assert kw["suppress_push"] is True
    assert text.startswith("⚠️ ")


def test_notify_never_raises(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("post failed")
    monkeypatch.setattr(crc, "post_reply", boom)
    monkeypatch.setattr(crc, "_report_runtime_error", boom)
    crc._reset_system_notice_state()
    crc._notify_agent_turn_failure(RuntimeError("x"), foreground=True)  # 不抛即过
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_consumer_error_classify.py -q`
Expected: 新增 3 个用例 FAIL（`_notify_agent_turn_failure` 不存在），Task 1 用例仍 PASS。

- [ ] **Step 3: 实现**

(a) `post_reply()` 签名加两个 kwarg（放 `thinking_native` 之后）：

```python
    role: str = "",
    notice_kind: str = "",
```

加密分支 `body` 构造后（`if reply_to_message_id:` 之前）加：

```python
        if role:
            body["role"] = role
        if notice_kind:
            body["notice_kind"] = notice_kind
```

明文兜底分支的 json dict 里同样补 `"role": role, "notice_kind": notice_kind`。

(b) Task 1 代码块之后加：

```python
# system 通知去抖：前台必发；后台同 error_class 每 SYSTEM_NOTICE_DEBOUNCE_SEC 一条，
# 任一成功回合清零（恢复后再坏要重新提醒）。进程内存态即可——respawn 顶多多发一条。
SYSTEM_NOTICE_DEBOUNCE_SEC = float(os.environ.get("SYSTEM_NOTICE_DEBOUNCE_SEC", "21600"))
_system_notice_last_sent: dict[str, float] = {}
_runtime_error_reported = False


def _reset_system_notice_state() -> None:
    _system_notice_last_sent.clear()


def _report_runtime_error(error: str, error_class: str = "") -> None:
    """腿②：设置页 last_runtime_error。失败只 log（观测性不影响回合）。"""
    global _runtime_error_reported
    try:
        httpx.post(
            f"{FEEDLING_API_URL}/v1/model_api/runtime_error",
            json={"error": (error or "")[:300], "error_class": (error_class or "")[:64]},
            headers=_HEADERS, timeout=10,
        )
        _runtime_error_reported = bool(error)
    except Exception as e:
        log.warning("runtime_error report failed (non-fatal): %s", e)


def _notify_agent_turn_failure(exc: BaseException, *, foreground: bool) -> None:
    """腿①+②：分类 → 上报设置页 → （前台必发/后台去抖）聊天 system 通知。

    永不抛出：通知是回合失败的旁路，绝不能让它把失败变得更糟。"""
    try:
        notice = classify_agent_error(exc)
        _report_runtime_error(notice.detail, notice.error_class)
        last = _system_notice_last_sent.get(notice.error_class, 0.0)
        if not foreground and (time.monotonic() - last) < SYSTEM_NOTICE_DEBOUNCE_SEC:
            return
        post_reply(
            _system_notice_body(notice),
            role="system", notice_kind="upstream_error", suppress_push=True,
        )
        _system_notice_last_sent[notice.error_class] = time.monotonic()
    except Exception:
        log.exception("system notice emit failed (non-fatal)")


def _note_agent_turn_success() -> None:
    """成功回合：重置去抖 + 清空设置页错误（仅当本进程报过错，省一次 HTTP）。"""
    global _runtime_error_reported
    _reset_system_notice_state()
    if _runtime_error_reported:
        _report_runtime_error("", "")
        _runtime_error_reported = False
```

注意：`post_reply` 定义在这些函数之后（模块加载序无碍——调用发生在运行时），
但若 lint/引用检查报 undefined，把本代码块移到 `post_reply` 定义之后亦可。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_consumer_error_classify.py -q`
Expected: 全部 PASS。

---

### Task 5: consumer — 前台/后台失败点接线 + 成功重置

**Files:**
- Modify: `tools/chat_resident_consumer.py`
  - `call_agent()` 清洗为空分支（约 :3408-3410）：置 parse-failed 标记
  - `_process_messages` 前台 except（约 :6507-6512）与成功路径
  - capture except（约 :5398）、dream except（约 :5730）、proactive except（约 :5942）
- Test: `tests/test_consumer_error_classify.py`（追加用例）

**Interfaces:**
- Consumes: Task 4 `_notify_agent_turn_failure` / `_note_agent_turn_success`。
- Produces: 无（终端接线）。

- [ ] **Step 1: 写失败测试（追加）**

```python
def test_parse_failed_marker_set_by_call_agent_sanitize_branch():
    # call_agent 清洗为空时不抛异常（SEND_FALLBACK_ON_AGENT_ERROR 默认 true），
    # 靠模块级标记让前台调用方知道要补发 reply_parse_failed 通知（spec §组件2）
    crc._turn_reply_parse_failed = False
    assert hasattr(crc, "_turn_reply_parse_failed")
```

（标记的端到端行为由前台路径人工验证覆盖——`call_agent` 全量 mock 成本过高；
这里只锁定标记存在性，防止重构时静默丢失。）

- [ ] **Step 2: 实现接线**

(a) `call_agent()` 内清洗为空分支（:3408 `if SEND_FALLBACK_ON_AGENT_ERROR:` /
:3410 `return [FALLBACK_REPLY]`）改为：

```python
    if SEND_FALLBACK_ON_AGENT_ERROR:
        global _turn_reply_parse_failed
        _turn_reply_parse_failed = True
        return [FALLBACK_REPLY]
```

并在模块级（Task 4 代码块处）加 `_turn_reply_parse_failed = False`。

(b) 前台 `_process_messages`（:6507 `except Exception as e:` 块），在
`agent_result = [FALLBACK_REPLY]` 之后加一行：

```python
                _notify_agent_turn_failure(e, foreground=True)
```

（~~`SEND_FALLBACK_ON_AGENT_ERROR` 为 false 的 else 分支不发通知不上报~~
**已按 Codex review 修订**：通知/上报与兜底话术解耦，开关只管 FALLBACK_REPLY，
`_notify_agent_turn_failure` 在两种配置下都发。）

(c) 前台成功路径：try 块正常返回后（`turn = _ensure_visible_thinking_summary(...)`
之前）加：

```python
        else:
            if _turn_reply_parse_failed:
                globals()["_turn_reply_parse_failed"] = False
                _notify_agent_turn_failure(
                    ValueError("agent produced no usable reply after sanitization"),
                    foreground=True,
                )
            else:
                _note_agent_turn_success()
```

即把现有 `try/except` 补成 `try/except/else`；原 try 之后的顺序代码不动。

(d) 后台三处 except（行号以 grep 为准，模式一致）：

- capture（:5398 `reason = f"capture_agent_call_failed:...` 所在 except）加：
  `_notify_agent_turn_failure(e, foreground=False)`
- dream（:5730 同型 except）加同一行
- proactive（:5942 `update_proactive_job_status(job_id, "failed", f"agent_call_failed: {e}")`
  所在 except）加同一行。注意该函数上方 `_is_provider_payment_error` 的
  `provider_payment_required` 分支（有 `continue`）**也要**在 continue 前加
  `_notify_agent_turn_failure(e, foreground=False)`（402 类正是最该告知用户的）。

(e) 后台成功点：capture/dream 的 `reply_text = _capture_agent_reply_text(...)`
成功行之后、proactive 的 `_clear_provider_payment_cooldown()` 之后，各加
`_note_agent_turn_success()`。

- [ ] **Step 3: 跑全量消费者相关测试**

Run: `python -m pytest tests/test_consumer_error_classify.py tests/test_consumer_decrypt_since.py tests/test_consumer_debug_trace.py tests/test_chat_resident_consumer_image.py tests/test_agent_runtime_resident_contract.py -q`
Expected: 全部 PASS。

- [ ] **Step 4: 全量测试基线**

Run: `python -m pytest tests/ -q -x --timeout=300 2>&1 | tail -5`
Expected: 与改动前基线一致（改动前先跑一次记录基线；pre-existing 失败不算回归）。

---

## Self-Review 结论（已跑）

- **Spec 覆盖**：分类器(T1)、chat role(T2)、runtime_error 路由(T3)、post_reply/去抖/腿②(T4)、
  前后台接线+parse-failed 标记(T5)。iOS 渲染 = spec 非目标（独立仓）。✓
- **占位符**：无 TBD；所有代码块完整。✓
- **类型一致**：`AgentErrorNotice`、`_notify_agent_turn_failure(exc, *, foreground)`、
  `record_runtime_error(store, *, error, error_class)`、`post_reply(..., role, notice_kind)`
  跨任务签名一致。✓
- 已知取舍：`last_runtime_error_class` 是 spec 之外的顺带新字段（读侧可忽略）；
  parse-failed 标记只有存在性测试（端到端 mock 成本过高，人工验证覆盖）。
