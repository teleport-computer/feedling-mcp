# BYOK 凭证读侧按形状路由（Phase 1 Task 1.1 细案）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 BYOK provider 凭证的读侧同时支持「信封（现状）」与「明文（TEE 主库
形态）」两种形状，使 Phase 4 cutover 后 hosted 线不会全线 `model_api_key_decrypt_failed`。

**Architecture:** 新增一个共用解封入口 `core.envelope.decrypt_provider_key_envelope`，
按行形状路由——有 `body_ct` 走现有 enclave 解密路径，只有 `body` 则直读明文。7 个
现存解密点改调它，异常语义与错误码保持不变（调用点的 try/except 一律不动）。

**Tech Stack:** Python 3.11 / psycopg3 / pytest；`backend/core/envelope.py`、
`backend/hosted/*`、`backend/agent_runtime/supervisor.py`。

## Global Constraints

- **绝不主动 `git commit` / `git add`**：改动留在工作区，由用户自行提交（用户全局规则）。
- **不改写侧**：本细案只动读侧。RDS 真源仍写信封，写侧格式路由属 Phase 2 Task 2.2。
- **错误码不变**：`model_api_key_decrypt_failed` / `model_api_key_envelope_missing`
  的外部语义与 HTTP 状态码保持原样，避免 iOS 与 io_cli 的既有分支失效。
- **明文分支不得联网**：明文行必须**本地直读**，绝不因为「顺手」再打一次 enclave。
- **测试基线**：worktree 内 `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py
  --ignore=tests/test_api.py`，当前基线 2 failed / 6873 passed（2 failed 为 ed6f2053
  自带的 pre-existing，与本改动无关）。需先起本地 PG：
  `docker run -d --name feedling-test-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16`。

## 事实基线（2026-07-29 实测，细案开工时以 grep 重扫为准）

- **RDS(test) `model_api_credentials`：25 行全密文**（`api_key_envelope ? 'body_ct'` = 25）。
- **TEE(test) 同表：25 行全明文**，key 集合为 `body,id,owner_user_id,visibility`，
  `body_ct` = 0 —— 表同步工作流的 CIPHERTEXT lane 已在复制时解密，**数据迁移无需另做**，
  故原计划 Task 1.1 的「一次性迁移工具」不再需要。
- 复制逻辑：`backend/tee_replicator/worker.py:627-670`（`_model_api_credentials_unpack`
  / `_model_api_credentials_upsert_args` / `_TABLES["model_api_credentials"]`）。
- 读侧 7 个解密点（全部 `purpose="model_api_provider_key"`）：
  `hosted/config_store.py:378`、`hosted/setup_core.py:320/491/994/1316/1546`、
  `hosted/vision_observer.py:113`；另有 `agent_runtime/supervisor.py:727` 走 HTTP
  直连 enclave（形态不同，单列 Task 3）。
- `core/enclave.py:224`：`_decrypt_envelope_via_enclave(envelope: dict,
  api_key: str | None, *, purpose: str, runtime_token: str = "") -> bytes`。
- `core/envelope.py` 已 `from core import enclave`（第 13 行），放 helper 无循环 import。

## File Structure

- `backend/core/envelope.py` — 新增 `decrypt_provider_key_envelope()`，唯一的形状路由点。
- `backend/hosted/config_store.py` — 1 处改调 helper（Runtime V2 主路径，带 runtime_token）。
- `backend/hosted/setup_core.py` — 5 处改调 helper。
- `backend/hosted/vision_observer.py` — 1 处改调 helper。
- `backend/agent_runtime/supervisor.py` — HTTP 路径，明文行直接短路，不打 enclave。
- `tests/test_byok_plaintext_read_routing.py` — 新建，覆盖两种形状 + 边界。

---

### Task 1: 形状路由 helper

**Files:**
- Modify: `backend/core/envelope.py`（在 `_model_api_key_encryption_material` 之后新增）
- Test: `tests/test_byok_plaintext_read_routing.py`（新建）

**Interfaces:**
- Produces: `decrypt_provider_key_envelope(envelope: dict, api_key: str | None, *,
  runtime_token: str = "") -> bytes` —— 成功返回 provider key 的 utf-8 **bytes**
  （与 `_decrypt_envelope_via_enclave` 一致，调用点的 `.decode("utf-8")` 不用改）；
  形状无法识别时 `raise ValueError("envelope_shape_unrecognized")`。

- [x] **Step 1: 写失败测试**

```python
# tests/test_byok_plaintext_read_routing.py
"""BYOK 凭证读侧按形状路由（Phase 1 Task 1.1）。

cutover 后 TEE 主库里的 model_api_credentials.api_key_envelope 是
{body, id, owner_user_id, visibility} 明文形状（表同步工作流在复制时已解密），
没有 body_ct。读侧若无条件打 enclave，hosted 线会全线
model_api_key_decrypt_failed。
"""
from __future__ import annotations

import pytest

from core import envelope as core_envelope


def _plaintext_row(body: str = "sk-plain-123") -> dict:
    """TEE 主库形态：实测 key 集合 = body,id,owner_user_id,visibility。"""
    return {"body": body, "id": "cred-1", "owner_user_id": "usr_x",
            "visibility": "shared"}


def _envelope_row(body_ct: str = "CIPHER") -> dict:
    """RDS 现状形态：双收件人信封。"""
    return {"body_ct": body_ct, "nonce": "n", "K_user": "ku", "K_enclave": "ke",
            "id": "cred-1", "owner_user_id": "usr_x", "visibility": "shared",
            "v": 1}


def test_plaintext_row_is_read_locally_without_touching_enclave(monkeypatch):
    """明文行必须本地直读——不得打 enclave（cutover 后 enclave 可能已不在读路径上）。"""
    called = []
    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **kw: called.append(1) or b"WRONG")

    out = core_envelope.decrypt_provider_key_envelope(_plaintext_row(), "api-key")

    assert out == b"sk-plain-123"
    assert called == [], "明文行不应触发任何 enclave 调用"


def test_envelope_row_still_goes_through_enclave(monkeypatch):
    """信封行维持现状路径，且 purpose 必须仍是 model_api_provider_key。"""
    seen = {}

    def fake(envelope, api_key, *, purpose, runtime_token=""):
        seen.update(envelope=envelope, api_key=api_key, purpose=purpose,
                    runtime_token=runtime_token)
        return b"sk-from-enclave"

    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave", fake)

    out = core_envelope.decrypt_provider_key_envelope(
        _envelope_row(), "api-key", runtime_token="rt-1")

    assert out == b"sk-from-enclave"
    assert seen["purpose"] == "model_api_provider_key"
    assert seen["runtime_token"] == "rt-1"


def test_unrecognized_shape_raises(monkeypatch):
    """既无 body_ct 也无 body：必须显式报错，不能静默返回空 key。

    静默返回空会让上游拿着空 key 去打 provider，错误信息变成 provider 侧的
    401，排查时完全看不出根因在这里。
    """
    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **kw: b"SHOULD-NOT-BE-CALLED")

    with pytest.raises(ValueError, match="envelope_shape_unrecognized"):
        core_envelope.decrypt_provider_key_envelope({"id": "cred-1"}, "api-key")


def test_body_ct_wins_when_both_present(monkeypatch):
    """两个字段同时存在（迁移中间态）时以 body_ct 为准——密文是真源。

    反过来优先明文会在「已写新密文、旧明文残留」的窗口里读到过期的 key。
    """
    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **kw: b"sk-from-enclave")
    row = _envelope_row()
    row["body"] = "sk-stale-plaintext"

    assert core_envelope.decrypt_provider_key_envelope(row, "k") == b"sk-from-enclave"


def test_non_string_body_is_rejected(monkeypatch):
    """body 不是字符串（脏数据）时按无法识别处理，不要 str() 硬转。"""
    monkeypatch.setattr(core_envelope.enclave, "_decrypt_envelope_via_enclave",
                        lambda *a, **kw: b"SHOULD-NOT-BE-CALLED")

    with pytest.raises(ValueError, match="envelope_shape_unrecognized"):
        core_envelope.decrypt_provider_key_envelope({"body": {"nested": 1}}, "k")
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_byok_plaintext_read_routing.py -q`
Expected: FAIL —— `AttributeError: module 'core.envelope' has no attribute
'decrypt_provider_key_envelope'`（5 个测试全红）。

- [x] **Step 3: 最小实现**

在 `backend/core/envelope.py` 的 `_model_api_key_encryption_material` 之后加入：

```python
def decrypt_provider_key_envelope(envelope: dict, api_key: str | None, *,
                                  runtime_token: str = "") -> bytes:
    """取出 BYOK provider key，按行形状路由。

    两种形状并存是 TEE 扶正期的常态：
      - RDS（现状真源）：双收件人信封，有 ``body_ct`` → 走 enclave 解密。
      - TEE 主库：表同步在复制时已解密，形状是
        ``{body, id, owner_user_id, visibility}``、无 ``body_ct`` → **本地直读**。

    明文分支绝不打 enclave：cutover 后 enclave 只服务加密档用户，明文档的读路径
    不该再依赖它（也是「读路径不经 enclave 更快」的兑现点）。

    ``body_ct`` 优先于 ``body``：两者并存只可能出现在迁移中间态，此时密文是真源，
    反过来会读到过期的明文残留。
    """
    if not isinstance(envelope, dict):
        raise ValueError("envelope_shape_unrecognized")
    if envelope.get("body_ct"):
        return enclave._decrypt_envelope_via_enclave(
            envelope, api_key, purpose="model_api_provider_key",
            runtime_token=runtime_token)
    body = envelope.get("body")
    if isinstance(body, str):
        return body.encode("utf-8")
    raise ValueError("envelope_shape_unrecognized")
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_byok_plaintext_read_routing.py -q`
Expected: PASS（5 passed）。

- [x] **Step 5: 改动留在工作区**

**不要 commit**（用户全局规则）。只确认 `git status --short` 里是
`backend/core/envelope.py` 与新测试文件两项。

---

### Task 2: 7 个读侧解密点改调 helper

**Files:**
- Modify: `backend/hosted/config_store.py:378`
- Modify: `backend/hosted/setup_core.py:320,491,994,1316,1546`
- Modify: `backend/hosted/vision_observer.py:113`
- Test: `tests/test_byok_plaintext_read_routing.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `core_envelope.decrypt_provider_key_envelope`。
- Produces: 无新符号；外部错误码与 HTTP 状态码保持不变。

- [x] **Step 1: 写失败测试（追加到同一文件）**

```python
def test_no_unrouted_provider_key_decrypt_sites_remain():
    """守卫：读侧不得再直接调 _decrypt_envelope_via_enclave 解 provider key。

    新增一处「无形状路由」的解密点，cutover 后就是一条静默的
    model_api_key_decrypt_failed。这条守卫让它在 CI 就红。
    允许的例外只有 core/envelope.py 自己（路由函数内部）。
    """
    import pathlib
    import re

    backend = pathlib.Path(__file__).parent.parent / "backend"
    offenders = []
    pat = re.compile(r"_decrypt_envelope_via_enclave\s*\(")
    for f in sorted(backend.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        if f.relative_to(backend).as_posix() in {"core/envelope.py", "core/enclave.py"}:
            continue
        src = f.read_text(encoding="utf-8")
        if not pat.search(src):
            continue
        # 只关心 provider key 这一类
        if "model_api_provider_key" in src:
            offenders.append(f.relative_to(backend).as_posix())
    assert not offenders, (
        "以下文件仍直接调 _decrypt_envelope_via_enclave 解 provider key，"
        "cutover 后遇到 TEE 的明文行会静默失败：\n  " + "\n  ".join(offenders)
        + "\n改调 core.envelope.decrypt_provider_key_envelope（按行形状路由）。"
    )
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_byok_plaintext_read_routing.py::test_no_unrouted_provider_key_decrypt_sites_remain -q`
Expected: FAIL，offenders 列出 `hosted/config_store.py`、`hosted/setup_core.py`、
`hosted/vision_observer.py`（3 个文件、7 处调用）。

- [x] **Step 3: 逐处替换**

`backend/hosted/config_store.py:378`（唯一带 runtime_token 的主路径）：

```python
        provider_key = core_envelope.decrypt_provider_key_envelope(
            envelope,
            api_key,
            **decrypt_kwargs,
        ).decode("utf-8")
```

`backend/hosted/setup_core.py:320`：

```python
        provider_key = core_envelope.decrypt_provider_key_envelope(
            envelope,
            caller_api_key,
            **decrypt_kwargs,
        ).decode("utf-8")
```

`backend/hosted/setup_core.py:491`：

```python
        provider_key = core_envelope.decrypt_provider_key_envelope(
            existing_envelope, caller_api_key,
        ).decode("utf-8")
```

`backend/hosted/setup_core.py:994`：

```python
        provider_key = core_envelope.decrypt_provider_key_envelope(
            envelope, api_key).decode("utf-8")
```

`backend/hosted/setup_core.py:1316` 与 `:1546`（两处形态相同，1546 多一层缩进）：

```python
        provider_key = core_envelope.decrypt_provider_key_envelope(
            envelope, caller_api_key).decode("utf-8")
```

`backend/hosted/vision_observer.py:113` 一并改为 `decrypt_provider_key_envelope`，
删掉该调用的 `purpose="model_api_provider_key"` 实参（helper 内部已固定）。

每个文件确认已 `from core import envelope as core_envelope`；没有则加 import
（`config_store.py` / `setup_core.py` 已有 `core_enclave` 的 import，若替换后
`core_enclave` 在该文件不再被使用，一并删掉那行 import——注意先 grep 确认无其他
用途，`backend/core/__init__.py` 有 re-export 惯例，见 memory
`autoflake-kills-module-attr-reexports`）。

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_byok_plaintext_read_routing.py -q`
Expected: PASS（6 passed）。

再跑受影响模块的既有测试：

Run: `python -m pytest tests/ -q -k "model_api or byok or setup_core or config_store" `
Expected: 无新增失败。

- [x] **Step 5: 静态检查 + 留工作区**

Run: `python -m pyflakes backend/hosted backend/core`
Expected: 除全仓恒有的 1 条 unused 外无新增（见 memory `autoflake-kills-module-attr-reexports`）。
**不要 commit。**

---

### Task 3: supervisor 的 HTTP 解密路径

**Files:**
- Modify: `backend/agent_runtime/supervisor.py:727`
- Test: `tests/test_byok_plaintext_read_routing.py`（追加）

**Interfaces:**
- Consumes: Task 1 的形状判据（此处不复用函数——supervisor 走 HTTP、不 import core）。
- Produces: 无新符号。

**背景：** 该处不调 `_decrypt_envelope_via_enclave`，而是直接
`json={"envelope": envelope, "purpose": "model_api_provider_key"}` POST 给 enclave。
明文行发过去会被 enclave 以 `decrypt_failed: envelope missing body_ct` 拒绝——正是
Task 0.2 里让 verify 瘫痪的同一条报错。

- [x] **Step 1: 写失败测试**

```python
def test_supervisor_reads_plaintext_row_without_http_call(monkeypatch):
    """supervisor 遇到明文行必须短路，不发 HTTP。

    它发出去只会拿回 enclave 的
    decrypt_failed: envelope missing body_ct（与 2026-07-28 verify 瘫痪同因）。
    """
    from agent_runtime import supervisor

    posted = []
    # raising=False：Step 3 才会把 HTTP 段抽成 _post_enclave_decrypt，此刻它还不存在。
    # 不加这个参数，红的原因会变成 monkeypatch 自己的 AttributeError，而不是
    # 「_provider_key_from_envelope 未实现」——那就验不出我们想验的东西。
    monkeypatch.setattr(supervisor, "_post_enclave_decrypt",
                        lambda *a, **kw: posted.append(1) or "WRONG",
                        raising=False)

    got = supervisor._provider_key_from_envelope(
        {"body": "sk-plain-9", "id": "c1", "owner_user_id": "u", "visibility": "shared"},
        api_key="k")

    assert got == "sk-plain-9"
    assert posted == []
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_byok_plaintext_read_routing.py::test_supervisor_reads_plaintext_row_without_http_call -q`
Expected: FAIL —— `AttributeError: module 'agent_runtime.supervisor' has no attribute
'_provider_key_from_envelope'`。

- [x] **Step 3: 实现**

先把 `supervisor.py:727` 现有的那段 HTTP 调用抽成 `_post_enclave_decrypt(envelope,
api_key)`（**纯搬移，不改行为**），然后新增：

```python
def _provider_key_from_envelope(envelope: dict, *, api_key: str | None) -> str:
    """取 provider key：明文行本地直读，信封行才发 enclave。

    与 core.envelope.decrypt_provider_key_envelope 同一判据（body_ct 优先）。
    这里不 import core —— supervisor 跑在 runner 侧、只经 HTTP 与 enclave 打交道。
    """
    if not isinstance(envelope, dict):
        raise ValueError("envelope_shape_unrecognized")
    if envelope.get("body_ct"):
        return _post_enclave_decrypt(envelope, api_key)
    body = envelope.get("body")
    if isinstance(body, str):
        return body
    raise ValueError("envelope_shape_unrecognized")
```

把原调用点改为 `_provider_key_from_envelope(envelope, api_key=api_key)`。

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_byok_plaintext_read_routing.py -q`
Expected: PASS（7 passed）。

- [x] **Step 5: 全量回归 + 留工作区**

Run: `python -m pytest tests/ -q --ignore=tests/e2e_model_api_test.py --ignore=tests/test_api.py`
Expected: 仍是 2 failed（pre-existing）/ passed 数 = 基线 + 7。
**不要 commit。**

---

## 出口 gate（Task 1.1 完成判据）

- [x] 5 个读侧点全部经形状路由，守卫测试 `test_no_unrouted_provider_key_decrypt_sites_remain` 绿。
- [x] supervisor 与 genesis worker 明文行不再发 HTTP。
- [x] 全量 L1 无新增失败。
- [ ] ⚠️ **暂不做**：`api_key_envelope JSONB → api_key TEXT` 的列改造。TEE 侧现存
      形状是 `{body,…}` 而非裸文本，读侧已能直读；列改名要同时动 replicator 的
      upsert、verify 的表登记与 alembic_tee，收益只是「清爽」，**风险却落在 cutover
      关键路径上**。建议留到 Phase 5 与 `tee_shadow/` 清理一起做，届时 RDS 已退役、
      不必维护双形状。此项已在主计划 Task 1.1 下注明。
