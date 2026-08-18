# 用户级加密开关偏好 `content_encryption`（Phase 2 Task 2.1 细案）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans
> （或 subagent-driven-development）逐 Task 执行。步骤用 `- [ ]` 跟踪。

**Goal:** 引入一等用户偏好 `content_encryption`（`on|off`，默认 `off`），作为
Phase 2 写侧/读侧格式路由的唯一判据来源。

**Architecture:** 完全照抄 `timezone` 的一等偏好模式——`registry` 提供瘦读写
helper（写侧带值校验 + **值未变即 no-op**），`accounts_core` 的 prefs 入口接受该
字段，`whoami` 下发。本细案**只做偏好本身**，不动任何读写格式路由（那是 Task
2.2 / 2.3）。

**Tech Stack:** Python 3.11 / pytest；`backend/accounts/{registry,accounts_core,whoami_core}.py`。

## Global Constraints

- **绝不主动 `git commit` / `git add`**：改动留工作区（用户全局规则）。
- **值未变必须 early return**：`persist_user` = users 行 upsert + TEE mirror +
  跨 worker 广播，每个 worker 会**整表重载**。timezone 就踩过这个坑
  （memory `users-reload-storm-resident-heartbeat` / `timezone-noop-persist-reload-storm`）。
- **本细案不改任何加解密行为**：偏好只是被存下来和读出来，没有消费者。
- **测试基线**：worktree 内 `python -m pytest tests/ -q
  --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
  = 2 failed（pre-existing）/ 6883 passed。需先起本地 PG（55432）。

## ⚠️ 一个计划未考虑的兼容性问题（本细案的关键决策）

主计划 Task 2.1 写的是：「whoami **按偏好决定**是否下发
`enclave_content_public_key_hex`（加密档需要它封 K_enclave；明文档不需要）」。

**照做会打断现网所有 iOS 客户端。** 现役 App 拿 `enclave_content_public_key_hex`
来封双收件人信封，这是它**唯一**的写入路径。Phase 2 上线时 Phase 3（iOS 开关
发版）尚未发生，默认 `off` + 按偏好停发公钥 = 现役 App 立刻写不进任何内容。

**本细案的决定：Phase 2 阶段 whoami 无条件继续下发该字段**，只**新增**
`content_encryption` 字段供新版 App 读取。停发公钥这件事推迟到 Phase 3 iOS 发版、
且旧版本淘汰之后再做，届时另立小细案。已在主计划 Task 2.1 下注明。

理由：下发一个明文档用不到的公钥**没有任何安全代价**（它是公钥，且本来就在
`/attestation` 端点公开），而停发的代价是全量写入中断。

---

### Task 1: registry 读写 helper

**Files:**
- Modify: `backend/accounts/registry.py`（`_set_user_timezone` 之后）
- Test: `tests/test_content_encryption_preference.py`（新建）

**Interfaces:**
- Produces:
  - `_get_user_content_encryption(user_id: str) -> str | None` —— 返回 `"on"`/`"off"`，
    未设置时 `None`（调用方决定默认值，与 `_get_user_timezone` 同风格）。
  - `_set_user_content_encryption(user_id: str, value: str | None) -> bool` ——
    只接受 `"on"`/`"off"`（大小写不敏感、去空格）；空值=清除。非法值返回 `False`
    且不写入。用户不存在返回 `False`。

- [x] **Step 1: 写失败测试**

```python
# tests/test_content_encryption_preference.py
"""一等偏好 content_encryption（Phase 2 Task 2.1）。

只验证「偏好被正确存取」，不涉及任何加解密格式路由——那是 Task 2.2/2.3。
"""
from __future__ import annotations

import uuid

import pytest

from accounts import registry


@pytest.fixture()
def uid(backend_env):
    """建一个真用户；registry 的 helper 只认在册用户。"""
    user_id = f"usr_{uuid.uuid4().hex[:12]}"
    registry.upsert_user({"user_id": user_id, "api_key_hash": "h", "doc": {}})
    return user_id


def test_unset_preference_reads_as_none(uid):
    assert registry._get_user_content_encryption(uid) is None


def test_set_on_then_read_back(uid):
    assert registry._set_user_content_encryption(uid, "on") is True
    assert registry._get_user_content_encryption(uid) == "on"


def test_set_off_then_read_back(uid):
    assert registry._set_user_content_encryption(uid, "off") is True
    assert registry._get_user_content_encryption(uid) == "off"


def test_value_is_normalized(uid):
    """大小写与空格归一——iOS/io_cli 传 "ON " 不该产生第三种取值。"""
    assert registry._set_user_content_encryption(uid, " ON ") is True
    assert registry._get_user_content_encryption(uid) == "on"


def test_invalid_value_rejected_and_not_written(uid):
    """非法值必须拒绝且不落库：写进去会让下游路由拿到无法判定的第三态。"""
    registry._set_user_content_encryption(uid, "on")
    assert registry._set_user_content_encryption(uid, "maybe") is False
    assert registry._get_user_content_encryption(uid) == "on", "非法写入不得覆盖原值"


def test_empty_value_clears(uid):
    registry._set_user_content_encryption(uid, "on")
    assert registry._set_user_content_encryption(uid, "") is True
    assert registry._get_user_content_encryption(uid) is None


def test_unknown_user_returns_false(backend_env):
    assert registry._set_user_content_encryption("usr_nope_nope", "on") is False


def test_unchanged_value_is_a_noop(uid, monkeypatch):
    """值未变必须 early return，不得触发 persist_user。

    persist_user = users 行 upsert + TEE mirror + 跨 worker 广播，每个 worker
    收到广播会**整表重载**。timezone 正是在这里踩过重载风暴
    （memory users-reload-storm-resident-heartbeat）。
    """
    registry._set_user_content_encryption(uid, "on")

    calls = []
    monkeypatch.setattr(registry, "persist_user", lambda u: calls.append(1))

    assert registry._set_user_content_encryption(uid, "on") is True
    assert calls == [], "值未变时不应 persist（会引发全表重载风暴）"

    assert registry._set_user_content_encryption(uid, "off") is True
    assert len(calls) == 1, "值真的变了才 persist 一次"
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_content_encryption_preference.py -q`
Expected: FAIL —— `AttributeError: module 'accounts.registry' has no attribute
'_get_user_content_encryption'`（8 个全红）。

- [x] **Step 3: 实现**

在 `backend/accounts/registry.py` 的 `_set_user_timezone` 之后加入：

```python
_CONTENT_ENCRYPTION_VALUES = ("on", "off")


def _get_user_content_encryption(user_id: str) -> str | None:
    """Return the user's stored content_encryption preference ("on"/"off"),
    or None when unset. Thin read mirroring _get_user_timezone; the caller owns
    the default (v6: unset == plaintext == "off")."""
    with _users_lock:
        for u in _users:
            if u.get("user_id") == user_id:
                value = str(u.get("content_encryption") or "").strip().lower()
                return value if value in _CONTENT_ENCRYPTION_VALUES else None
    return None


def _set_user_content_encryption(user_id: str, value: str | None) -> bool:
    """Set (or clear, when value is falsy) the user's content_encryption
    preference. Only "on"/"off" are accepted — an unrecognized value is rejected
    rather than stored, so downstream format routing never sees a third state.
    Returns True when the record was found and updated, False when the user is
    unknown or the value is non-empty and invalid."""
    normalized = str(value or "").strip().lower()
    if normalized and normalized not in _CONTENT_ENCRYPTION_VALUES:
        return False
    with _users_lock:
        for u in _users:
            if u.get("user_id") == user_id:
                # Unchanged value is a pure no-op — persist_user is a users-row
                # upsert + TEE mirror + a cross-worker broadcast that makes EVERY
                # worker reload the whole registry (see _set_user_timezone).
                if normalized == str(u.get("content_encryption") or "").strip().lower():
                    return True
                if normalized:
                    u["content_encryption"] = normalized
                else:
                    u.pop("content_encryption", None)
                persist_user(u)
                return True
    return False
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_content_encryption_preference.py -q`
Expected: PASS（8 passed）。

- [x] **Step 5: 改动留工作区**（**不要 commit**）

---

### Task 2: prefs 入口接受 content_encryption

**Files:**
- Modify: `backend/accounts/accounts_core.py`（timezone 分支旁，约 316-330 行；**开工时 grep `has_tz` 重定位**）
- Test: `tests/test_content_encryption_preference.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `_set_user_content_encryption` / `_get_user_content_encryption`。
- Produces: prefs 响应体新增 `content_encryption` 键；非法值返回 400。

- [x] **Step 1: 写失败测试**

```python
def test_prefs_endpoint_sets_and_returns_preference(client):
    """PATCH prefs 能设置并回显 content_encryption。"""
    from tests.helpers import register_user  # 若无此 helper，照本文件既有注册方式

    uid, api_key = register_user(client)
    r = client.patch("/v1/users/prefs", headers={"X-API-Key": api_key},
                     json={"content_encryption": "on"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["content_encryption"] == "on"


def test_prefs_endpoint_rejects_invalid_value(client):
    from tests.helpers import register_user

    uid, api_key = register_user(client)
    r = client.patch("/v1/users/prefs", headers={"X-API-Key": api_key},
                     json={"content_encryption": "maybe"})
    assert r.status_code == 400
```

> ⚠️ 开工第一步先 `grep -rn "users/prefs\|def .*prefs" backend/accounts/` 确认
> 真实路由路径与 HTTP 方法，以及本仓测试注册用户的既有写法
> （`tests/test_model_api_path.py` 有现成 `_register` 模式）。上面的
> `register_user` 只是占位调用名，**照抄仓库既有 helper，不要新造**。

- [x] **Step 2: 跑测试确认失败**（预期：响应体无该键 / 400 未触发）

- [x] **Step 3: 实现**

照 `has_tz` 分支加一段（`tz_raw` 取值处旁边取 `content_encryption`）：

```python
    if has_content_encryption:
        if not registry._set_user_content_encryption(store.user_id, ce_raw):
            return {"error": 'content_encryption must be "on", "off", or null'}, 400
```

并在返回体加：

```python
        "content_encryption": registry._get_user_content_encryption(store.user_id),
```

- [x] **Step 4: 跑测试确认通过**

- [x] **Step 5: 留工作区**（不 commit）

---

### Task 3: whoami 下发偏好

**Files:**
- Modify: `backend/accounts/whoami_core.py`（`timezone` 下发处附近，约 40-51 行）
- Test: `tests/test_content_encryption_preference.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `_get_user_content_encryption`。
- Produces: whoami 响应新增 `content_encryption`（未设置时下发 `"off"`——
  客户端不必再理解「缺字段」这第三态）。

- [x] **Step 1: 写失败测试**

```python
def test_whoami_reports_off_by_default(client):
    """未设置时 whoami 必须明确下发 "off"，而不是省略该字段。

    省略会让客户端各自猜默认值；v6 的默认是明文，必须由服务端讲清楚。
    """
    from tests.helpers import register_user

    uid, api_key = register_user(client)
    body = client.get("/v1/users/whoami", headers={"X-API-Key": api_key}).get_json()
    assert body["content_encryption"] == "off"


def test_whoami_still_emits_enclave_public_key_for_plaintext_users(client):
    """⚠️ Phase 2 阶段明文档用户仍要拿到 enclave_content_public_key_hex。

    现役 iOS 用它封双收件人信封，这是它唯一的写入路径。Phase 3 发版前停发
    = 全量写入中断。见本细案开头的兼容性决策。
    """
    from tests.helpers import register_user

    uid, api_key = register_user(client)
    body = client.get("/v1/users/whoami", headers={"X-API-Key": api_key}).get_json()
    assert body["content_encryption"] == "off"
    assert body.get("enclave_content_public_key_hex"), \
        "明文档也必须继续下发 enclave 公钥，否则现役 App 写不进内容"
```

- [x] **Step 2: 跑测试确认失败**

- [x] **Step 3: 实现**

在 `whoami_core.py` 的 timezone 下发之后加入：

```python
    # v6：未设置 == 明文档。显式下发 "off" 而不是省略字段，免得各客户端自己猜默认。
    resp["content_encryption"] = registry._get_user_content_encryption(store.user_id) or "off"
```

**不要**动 `enclave_content_public_key_hex` 的下发条件（见开头的兼容性决策）。

- [x] **Step 4: 跑测试确认通过**

- [x] **Step 5: 全量 L1 + 留工作区**

Run: `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
Expected: 2 failed（pre-existing）/ passed 数 = 6883 + 本细案新增测试数。
**不要 commit。**

---

## 出口 gate（Task 2.1 完成判据）

- [x] `content_encryption` 可设、可读、可清除；非法值被拒绝且不覆盖原值。
- [x] 值未变时不触发 `persist_user`（防重载风暴）。
- [x] whoami 未设置时下发 `"off"`，且**仍下发** `enclave_content_public_key_hex`。
- [x] 全量 L1 无新增失败。
- [ ] ⚠️ **不在本细案范围**（避免与主计划混淆）：写侧格式路由（Task 2.2）、
      读侧明文旁路（Task 2.3）、去 local_only（写侧行为，随 2.2）、
      按偏好停发 enclave 公钥（推迟到 Phase 3 之后）。本细案交付后，偏好**还没有
      任何消费者**——这是有意的，让路由改造能独立评审。
