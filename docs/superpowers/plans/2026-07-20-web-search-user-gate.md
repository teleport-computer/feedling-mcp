# 联网搜索用户开关（Runtime V2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给用户一个「联网搜索」开关；关闭时 `web_search` / `web_fetch` 对该用户
**不出现在发给模型的请求里**，且后台轮次永远拿不到。

**Architecture:** 复用 `tool_loop.py` 已有的 `disabled_tool_names=` 接缝——进了
这个集合的工具既不被 offer 也无法执行。全局工具目录 `_CATALOG` 一行不碰。
运维 kill switch 走 V2 既有的 DB 控制表模式，并在**每批 dispatch 前**二次检查，
因为 `turn_catalog` 只在回合入口算一次。

**Tech Stack:** Python 3 / ASGI routes / pytest / alembic；SwiftUI（iOS）。

设计文档：`docs/superpowers/specs/2026-07-20-web-search-user-gate-design.md`。
**执行前必读该文档的 §4.2.1（dispatch 二次检查）与 §7（这是一次能力回收）。**

## Global Constraints

- 基线分支 `feat/web-search-gate`，出自 `origin/pre` @ `c63eacb6`。
- **`backend/model_api_runtime/v2/tool_loop.py` 不允许有任何改动。** 它声明了
  「dependency-clean：不 import hosted/agent_runtime/db，所有副作用注入」，
  且是所有用户共享的循环。本计划全部通过既有的 `disabled_tool_names=` 参数与
  三个 dispatcher 实现。
- **`_CATALOG` / `build_tool_specs()` 不允许有任何改动。**
- **默认关，不做老用户迁移**（hx 拍板）。blob 缺省即为关。
- **后台 lane 永不放开**：`wake` / `screen_watch` 及其子 agent，无论用户开关
  如何，web 工具一律禁用。
- **fail closed 三处**：`web_tools_enabled` 为 `None` / 抛异常 / 返回非法值，
  一律视为禁用，且**不得让整个 turn 失败**。控制表读失败同理。
- **合并必须是并集**：`disabled_tool_names = disabled_mutation_tool_names ∪
  disabled_web_tool_names`。web 门禁不得盖掉 mutation recovery 门禁。
- 新增测试文件若为纯单测，必须加进 `tests/conftest.py` 的 `_PURE_UNIT`
  （`:109` 起）；DB-backed 的不要加（会把无库机器的优雅跳过变成硬收集错误）。
- 本地已有 Postgres（`feedling-test-pg`，`127.0.0.1:55432`，
  `postgres/test`），conftest 默认就指向它，**全量测试可以本地跑**。
- **Task 5–9 是不可分割的运行时原子组。** 每个 task 仍各自一个 commit，但：
  - 不得把中间状态 cherry-pick 到任何环境
  - 5–9 未全部完成，PR 不得标 ready
  - backend release 必须包含 5–10 的全部内容

  理由：只合到 5，wake 与子 agent 仍无门禁（用户关了开关，后台还能搜）；
  合到 8 但没有 9，后端默认关已生效却**没有任何 API 能打开它**。
  Task 1–4 是不产生任何行为的基础设施，可以独立存在。
- 每个 Task 结束时提交，message 末尾加
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `backend/core/store.py` | 新增 `load_web_settings` / `save_web_settings`，blob kind `web_settings` |
| `backend/model_api_runtime/v2/web_gate.py` | **新建**。纯函数：给定「用户开关 / lane / halted 标志」算出本轮要禁用的 web 工具名集合 |
| `backend/model_api_runtime/v2/kill_switch.py` | 增加 `web_halted()` 读取器（与 `turns_halted` 同表同缓存风格） |
| `backend/alembic/versions/XXXX_web_halted_columns.py` | **新建**。`v2_runtime_control` 加两列 |
| `backend/model_api_runtime/v2/worker.py` | `TurnDeps.web_tools_enabled` + 三个 lane 接线 + 三个 dispatcher 二次检查 |
| `backend/model_api_runtime/v2/serve_worker.py` | 生产装配 `web_tools_enabled` |
| `backend/chat/web_settings_core.py` | **新建**。框架无关的读写 core |
| `backend/chat/routes_asgi.py` | `GET/POST /v1/web/settings` 两个薄路由 |

**为什么把判定放 `v2/web_gate.py`**：三个 lane + 三个 dispatcher + 子 agent 共
七处要用同一套判定，抽成无依赖纯函数才不会各写一遍然后漂移。放在 `v2/` 下
符合依赖方向（`tests/test_v2_dependency_direction.py` 只禁 `hosted` /
`agent_runtime`）。

---

### Task 1: `web_settings` blob 存储层

**Files:**
- Modify: `backend/core/store.py`（`save_proactive_settings` 之后，约 `:1122`）
- Test: `tests/test_web_settings_store.py`（新建）
- Modify: `tests/conftest.py:109`（`_PURE_UNIT` 加入新文件名）

**Interfaces:**
- Produces: `WEB_SETTINGS_BLOB = "web_settings"`；
  `UserStore.load_web_settings() -> dict`（`{"version": 1, "enabled": bool}`）；
  `UserStore.save_web_settings(patch: dict) -> dict`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_web_settings_store.py`：

```python
"""Pure-unit coverage for the web-search toggle blob.

Monkeypatches db.get_blob/set_blob rather than hitting Postgres: what is under
test is the default/merge/allowlist behaviour. That is what makes this file safe
to list in conftest's _PURE_UNIT.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db  # noqa: E402
from core.store import WEB_SETTINGS_BLOB, UserStore  # noqa: E402


@pytest.fixture()
def store(monkeypatch):
    saved: dict[tuple[str, str], object] = {}
    monkeypatch.setattr(db, "get_blob", lambda uid, kind: saved.get((uid, kind)))
    monkeypatch.setattr(
        db, "set_blob", lambda uid, kind, doc: saved.__setitem__((uid, kind), doc))
    s = UserStore("u-web-settings-test")
    s._saved_blobs = saved
    return s


def test_default_is_disabled(store):
    """No migration for existing users (hx): a missing blob means OFF."""
    assert store.load_web_settings() == {"version": 1, "enabled": False}


def test_roundtrip_and_blob_kind(store):
    assert store.save_web_settings({"enabled": True})["enabled"] is True
    assert store._saved_blobs[("u-web-settings-test", WEB_SETTINGS_BLOB)] == {
        "version": 1, "enabled": True}
    assert store.load_web_settings()["enabled"] is True


def test_can_be_turned_back_off(store):
    store.save_web_settings({"enabled": True})
    assert store.save_web_settings({"enabled": False})["enabled"] is False


def test_unknown_keys_are_dropped(store):
    assert "evil" not in store.save_web_settings({"enabled": True, "evil": "x"})


@pytest.mark.parametrize("bad", ["yes", "no", "true", "false", "", 1, 0, None, []])
def test_non_bool_input_is_rejected(store, bad):
    """严格布尔:绝不 bool() 强转。bool("no") is True —— 用户说不要,却被开启。"""
    with pytest.raises(ValueError):
        store.save_web_settings({"enabled": bad})


def test_only_real_bools_accepted(store):
    assert store.save_web_settings({"enabled": True})["enabled"] is True
    assert store.save_web_settings({"enabled": False})["enabled"] is False


def test_corrupt_stored_value_reads_as_disabled(store):
    """历史/腐坏 blob 里的非 bool 值 fail closed。"""
    store._saved_blobs[("u-web-settings-test", WEB_SETTINGS_BLOB)] = {"enabled": "yes"}
    assert store.load_web_settings()["enabled"] is False


def test_load_rebuilds_the_contract_and_drops_unknown_fields(store):
    """不要 {**default, **doc} —— 历史 blob 的未知字段会泄漏进 contract。"""
    store._saved_blobs[("u-web-settings-test", WEB_SETTINGS_BLOB)] = {
        "enabled": True, "version": 99, "legacy_junk": "x"}
    assert store.load_web_settings() == {"version": 1, "enabled": True}


def test_empty_or_non_dict_patch_keeps_current(store):
    store.save_web_settings({"enabled": True})
    assert store.save_web_settings({})["enabled"] is True
    assert store.save_web_settings("true")["enabled"] is True


def test_corrupt_blob_falls_back_to_disabled(store):
    store._saved_blobs[("u-web-settings-test", WEB_SETTINGS_BLOB)] = ["nope"]
    assert store.load_web_settings() == {"version": 1, "enabled": False}


def test_load_survives_storage_errors(store, monkeypatch):
    def boom(uid, kind):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_blob", boom)
    assert store.load_web_settings() == {"version": 1, "enabled": False}
```

- [ ] **Step 2: 加白名单并确认 RED**

在 `tests/conftest.py` 的 `_PURE_UNIT` 集合首行加
`"test_web_settings_store.py",`，然后：

```
python3 -m pytest tests/test_web_settings_store.py --collect-only -q
```
Expected：列出 19 个 test（10 个函数，其中 1 个 9 参数化）。**若为 0，先修白名单**

```
python3 -m pytest tests/test_web_settings_store.py -q
```
Expected：FAIL，`ImportError: cannot import name 'WEB_SETTINGS_BLOB'`

- [ ] **Step 3: 实现**

`backend/core/store.py` 顶部常量区（`PROACTIVE_*` 附近）加：

```python
# Web-search toggle. Blob-backed like proactive_settings, so no migration.
WEB_SETTINGS_BLOB = "web_settings"
```

`save_proactive_settings` 之后追加：

```python
    # ------- web search -------
    # USER PREFERENCE ONLY. An operator kill switch must never rewrite this —
    # otherwise restoring the feature would force every user to re-enable it by
    # hand. Whether web tools are actually offered on a turn is derived
    # (preference + lane + halted flags) in model_api_runtime/v2/web_gate.py.
    def load_web_settings(self) -> dict:
        """Web-search toggle. Defaults to OFF, with no migration for existing
        users (product decision 2026-07-20): a missing blob means off."""
        default = {"version": 1, "enabled": False}
        try:
            doc = db.get_blob(self.user_id, WEB_SETTINGS_BLOB)
            if isinstance(doc, dict):
                # Rebuild the contract; never spread the stored doc — a historic
                # blob's unknown fields would otherwise leak into the response.
                # ``is True``, never ``bool(...)``: bool("no") is True.
                return {"version": 1, "enabled": doc.get("enabled") is True}
        except Exception as e:
            print(f"[{self.user_id}/web_settings] load failed: {e}")
        return default

    def save_web_settings(self, patch: dict) -> dict:
        """Accepts only a real ``bool`` under ``enabled``.

        Strict on purpose: coercing would make ``{"enabled": "no"}`` turn web
        access ON. Every layer of this feature uses ``is True`` / isinstance,
        never ``bool(...)``.
        """
        cur = self.load_web_settings()
        if isinstance(patch, dict) and "enabled" in patch:
            if not isinstance(patch["enabled"], bool):
                raise ValueError("enabled must be boolean")
            cur["enabled"] = patch["enabled"]
        db.set_blob(self.user_id, WEB_SETTINGS_BLOB, cur)
        return cur
```

- [ ] **Step 4: GREEN**

```
python3 -m pytest tests/test_web_settings_store.py -q
```
Expected：19 passed

- [ ] **Step 5: 提交**

```bash
git add backend/core/store.py tests/test_web_settings_store.py tests/conftest.py
git commit -m "feat(web-gate): web_settings blob 存储层(默认关,无迁移)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 控制表两列 + `web_halted()` 读取器

**Files:**
- Create: `backend/alembic/versions/<rev>_web_halted_columns.py`
- Modify: `backend/model_api_runtime/v2/kill_switch.py`
- Test: `tests/test_v2_kill_switch_web.py`（新建，**DB-backed，不进白名单**）

**Interfaces:**
- Produces: `kill_switch.web_halted(default_on_error: bool = True) -> tuple[bool, bool]`
  返回 `(search_halted, fetch_halted)`；`kill_switch.set_web_halted(*, search=None, fetch=None)`

- [ ] **Step 1: 写迁移**

先看现有 head：

```
python3 -c "
import subprocess,glob,os
os.chdir('backend')
print(subprocess.run(['python3','-m','alembic','heads'],capture_output=True,text=True).stdout)
"
```

按仓库既有迁移文件的形状新建一个 revision（down_revision 填上面的 head）：

```python
def upgrade() -> None:
    op.add_column("v2_runtime_control",
                  sa.Column("web_search_halted", sa.Boolean(),
                            nullable=False, server_default=sa.false()))
    op.add_column("v2_runtime_control",
                  sa.Column("web_fetch_halted", sa.Boolean(),
                            nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("v2_runtime_control", "web_fetch_halted")
    op.drop_column("v2_runtime_control", "web_search_halted")
```

⚠️ 用**停用**语义（`*_halted`，默认 `false` = 不停用），与既有 `turns_halted`
一致，避免 `enabled` 的双重取反。

- [ ] **Step 2: 写失败测试**

新建 `tests/test_v2_kill_switch_web.py`（照抄现有 kill switch 测试的 fixture
写法；若没有，用 conftest 提供的 DB）：

```python
"""web_search_halted / web_fetch_halted 的读写与 fail-closed 语义。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import kill_switch  # noqa: E402


@pytest.fixture(autouse=True)
def _reset():
    kill_switch.set_web_halted(search=False, fetch=False)
    kill_switch._invalidate()
    yield
    kill_switch.set_web_halted(search=False, fetch=False)
    kill_switch._invalidate()


def test_defaults_to_not_halted():
    assert kill_switch.web_halted() == (False, False)


def test_search_and_fetch_are_independent():
    kill_switch.set_web_halted(search=True)
    assert kill_switch.web_halted() == (True, False)
    kill_switch.set_web_halted(search=False, fetch=True)
    assert kill_switch.web_halted() == (False, True)


def test_set_invalidates_cache():
    assert kill_switch.web_halted() == (False, False)
    kill_switch.set_web_halted(fetch=True)      # must not wait out the TTL
    assert kill_switch.web_halted() == (False, True)


def test_read_error_fails_closed(monkeypatch):
    """A control-plane read failure must disable web, never enable it."""
    def boom():
        raise RuntimeError("control plane down")
    monkeypatch.setattr(kill_switch.db, "get_pool", boom)
    kill_switch._invalidate()
    assert kill_switch.web_halted() == (True, True)


def test_read_error_is_cached_so_a_flapping_db_is_not_hammered(monkeypatch):
    """没有这条,DB 抖动期间每个 turn/每批 dispatch 都会重查 —— 故障被放大。"""
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise RuntimeError("down")

    monkeypatch.setattr(kill_switch.db, "get_pool", boom)
    kill_switch._invalidate()
    assert kill_switch.web_halted() == (True, True)
    assert kill_switch.web_halted() == (True, True)
    assert kill_switch.web_halted() == (True, True)
    assert calls["n"] == 1          # 后两次走缓存


def test_missing_control_row_fails_closed(monkeypatch):
    """控制行不存在 = 未知状态 = 不给 web。绝不能 fail open。"""
    monkeypatch.setattr(kill_switch, "_fetch_web_halted_row", lambda: None)
    kill_switch._invalidate()
    assert kill_switch.web_halted() == (True, True)
```

- [ ] **Step 3: 运行确认 RED**

```
python3 -m pytest tests/test_v2_kill_switch_web.py -q
```
Expected：FAIL，`AttributeError: module ... has no attribute 'web_halted'`

- [ ] **Step 4: 实现**

在 `kill_switch.py` 里加**独立的**缓存槽（不要复用 `_cached_value`，那是
`turns_halted` 的），并一次查询读两列：

```python
_web_cache_lock = threading.Lock()
_web_cached: tuple[bool, bool] | None = None
_web_cached_at: float = 0.0


def _invalidate_web() -> None:
    global _web_cached, _web_cached_at
    with _web_cache_lock:
        _web_cached = None
        _web_cached_at = 0.0


def web_halted() -> tuple[bool, bool]:
    """``(search_halted, fetch_halted)``，缓存 ~`_CACHE_TTL_SEC` 秒。

    与 ``turns_halted`` 不同，**没有 fail-open 入口**：web 目前不存在任何合法的
    fail-open 调用方，留一个 ``default_on_error=False`` 参数等于在安全闸上预留
    绕过接口（YAGNI）。固定语义：

    - DB 正常 → 返回真实两列
    - DB 异常 → 返回 ``(True, True)`` **并缓存 ~2s**（不缓存的话，DB 抖动期间
      每个 turn / 每批 dispatch 都会重查，把故障放大成查询风暴）
    - 控制行缺失 → 未知状态 → ``(True, True)``，同样缓存
    - **绝不 raise**：调用方拿到 (True, True) 时正常继续，让模型无工具作答，
      不能让整个 turn 失败
    """
    global _web_cached, _web_cached_at
    now = time.monotonic()
    with _web_cache_lock:
        if _web_cached is not None and (now - _web_cached_at) < _CACHE_TTL_SEC:
            return _web_cached
    try:
        with db.get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT web_search_halted, web_fetch_halted "
                    "FROM v2_runtime_control WHERE id=1")
                row = cur.fetchone()
                # 控制行缺失 = 未知状态 → fail closed,不是 (False, False)
                value = (bool(row[0]), bool(row[1])) if row else (True, True)
    except Exception as exc:  # noqa: BLE001 — must never raise into callers
        log.warning("[v2.kill_switch] web_halted read failed, failing closed: %s", exc)
        value = (True, True)
    with _web_cache_lock:
        _web_cached = value          # 错误结果也要缓存,否则故障期变查询风暴
        _web_cached_at = now
    return value


def set_web_halted(*, search: bool | None = None, fetch: bool | None = None) -> None:
    """只更新显式传入的列，另一列保持不变。"""
    sets, params = [], []
    if search is not None:
        sets.append("web_search_halted=%s")
        params.append(bool(search))
    if fetch is not None:
        sets.append("web_fetch_halted=%s")
        params.append(bool(fetch))
    if not sets:
        return
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE v2_runtime_control SET {', '.join(sets)}, updated_at=now() "
                "WHERE id=1", tuple(params))
    _invalidate_web()
```

同时把 `_invalidate()` 改成也调用 `_invalidate_web()`（测试常统一调它）。

- [ ] **Step 5: GREEN + 迁移可回滚**

```
python3 -m pytest tests/test_v2_kill_switch_web.py -q
```
Expected：全部 passed

```
cd backend && python3 -m alembic upgrade head && python3 -m alembic downgrade -1 && python3 -m alembic upgrade head
```
Expected：三步都成功（验证 downgrade 不会炸）

- [ ] **Step 6: 提交**

```bash
git add backend/alembic/versions/ backend/model_api_runtime/v2/kill_switch.py \
        tests/test_v2_kill_switch_web.py
git commit -m "feat(web-gate): v2_runtime_control 加 web_search_halted/web_fetch_halted

停用语义(与 turns_halted 一致,不做 enabled 双重取反)。一次查询读两列、独立
缓存槽。与 turns_halted 相反:读失败默认 fail closed(不给 web 工具),但绝不
让整个 turn 失败。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: `web_gate.py` 判定纯函数

**Files:**
- Create: `backend/model_api_runtime/v2/web_gate.py`
- Test: `tests/test_v2_web_gate.py`（新建，纯单测，**进白名单**）
- Modify: `tests/conftest.py:109`

**Interfaces:**
- Produces:
  - `WEB_TOOL_NAMES = frozenset({"web_search", "web_fetch"})`
  - `disabled_web_tools(*, user_enabled, lane, search_halted, fetch_halted) -> frozenset[str]`
  - `resolve_user_enabled(web_tools_enabled, user_id) -> bool`（fail-closed 包装）
  - `FOREGROUND_LANES = frozenset({"chat"})`

- [ ] **Step 1: 写失败测试**

新建 `tests/test_v2_web_gate.py`：

```python
"""联网门禁的判定。七处调用点共用这一份，任何漂移都从这里暴露。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from model_api_runtime.v2 import web_gate as g  # noqa: E402

BOTH = frozenset({"web_search", "web_fetch"})


def test_user_off_disables_everything():
    assert g.disabled_web_tools(user_enabled=False, lane="chat",
                                search_halted=False, fetch_halted=False) == BOTH


def test_user_on_chat_enables_everything():
    assert g.disabled_web_tools(user_enabled=True, lane="chat",
                                search_halted=False, fetch_halted=False) == frozenset()


@pytest.mark.parametrize("lane", ["wake", "screen_watch", "scheduled", "", None, "future_lane"])
def test_background_and_unknown_lanes_always_disabled(lane):
    """后台搜索会增加轮次/token/延迟,且是无用户触发的对外数据流。
    未知 lane 一律 fail closed。"""
    assert g.disabled_web_tools(user_enabled=True, lane=lane,
                                search_halted=False, fetch_halted=False) == BOTH


def test_halted_flags_are_independent():
    assert g.disabled_web_tools(user_enabled=True, lane="chat",
                                search_halted=True, fetch_halted=False) == frozenset({"web_search"})
    assert g.disabled_web_tools(user_enabled=True, lane="chat",
                                search_halted=False, fetch_halted=True) == frozenset({"web_fetch"})


def test_halted_wins_over_user_preference():
    assert g.disabled_web_tools(user_enabled=True, lane="chat",
                                search_halted=True, fetch_halted=True) == BOTH


# ---- resolve_user_enabled: fail closed, never raise ----

def test_resolve_none_callable_is_disabled():
    assert g.resolve_user_enabled(None, "u1") is False


def test_resolve_true():
    assert g.resolve_user_enabled(lambda uid: True, "u1") is True


def test_resolve_raising_callable_is_disabled():
    def boom(uid):
        raise RuntimeError("store down")
    assert g.resolve_user_enabled(boom, "u1") is False


@pytest.mark.parametrize("bad", ["yes", 1, object(), None])
def test_resolve_non_bool_is_disabled(bad):
    """非法返回值一律当禁用 —— 不要用 bool() 把 'no' 变成 True。"""
    assert g.resolve_user_enabled(lambda uid: bad, "u1") is False
```

- [ ] **Step 2: 加白名单并确认 RED**

`_PURE_UNIT` 加 `"test_v2_web_gate.py",`，然后：

```
python3 -m pytest tests/test_v2_web_gate.py --collect-only -q
python3 -m pytest tests/test_v2_web_gate.py -q
```
Expected：能收集到；FAIL 于 `ModuleNotFoundError: ... web_gate`

- [ ] **Step 3: 实现**

```python
"""Which web tools this turn must NOT offer. Pure functions, no IO.

Seven call sites share this module (three lanes × offer + dispatch, plus the
subagent allowlist). Writing the rule seven times is how the two sides of a
gate drift apart, so all of them must go through here.
"""
from __future__ import annotations

WEB_TOOL_NAMES = frozenset({"web_search", "web_fetch"})

# Only the foreground chat turn may reach the network. Background turns
# (wake / screen_watch and their subagents) stay closed even when the user has
# the toggle ON: searching there adds model rounds, tokens and latency, and is
# an outbound data flow the user never triggered. Any lane not listed here is
# treated as background — new lanes fail closed by default.
FOREGROUND_LANES = frozenset({"chat"})


def disabled_web_tools(*, user_enabled: bool, lane: str | None,
                       search_halted: bool, fetch_halted: bool) -> frozenset[str]:
    if not user_enabled or lane not in FOREGROUND_LANES:
        return WEB_TOOL_NAMES
    out = set()
    if search_halted:
        out.add("web_search")
    if fetch_halted:
        out.add("web_fetch")
    return frozenset(out)


def resolve_user_enabled(web_tools_enabled, user_id: str) -> bool:
    """Read the per-user preference through the injected callable, fail closed.

    A missing callable, a raising callable, or a non-bool return all mean
    "disabled". Deliberately NOT ``bool(value)``: a stray "no" string would
    otherwise read as True.
    """
    if web_tools_enabled is None:
        return False
    try:
        value = web_tools_enabled(user_id)
    except Exception:  # noqa: BLE001 — a settings read must never fail the turn
        return False
    return value is True
```

- [ ] **Step 4: GREEN**

```
python3 -m pytest tests/test_v2_web_gate.py -q
```
Expected：全部 passed

- [ ] **Step 5: 提交**

```bash
git add backend/model_api_runtime/v2/web_gate.py tests/test_v2_web_gate.py tests/conftest.py
git commit -m "feat(web-gate): 判定纯函数(lane/用户开关/halted -> 禁用集合)

后台 lane 与未知 lane 一律 fail closed;resolve_user_enabled 对 None/抛异常/
非法返回值全部当禁用,且绝不让 turn 失败。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: `TurnDeps` 接缝 + 生产装配

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`（`TurnDeps`，`:723` 附近）
- Modify: `backend/model_api_runtime/v2/serve_worker.py`（`build_production_deps`，`:2568-2600`）
- Test: `tests/test_v2_dependency_direction.py` 必须保持绿

**Interfaces:**
- Produces: `TurnDeps.web_tools_enabled: Callable[[str], bool] | None = None`

- [ ] **Step 1: 加字段**

`worker.py` 的 `TurnDeps`，紧跟 `runtime_mode_enabled`（`:723`）：

```python
    # (user_id) -> bool：用户的联网搜索开关。None / 抛异常 / 非 bool 一律按
    # 禁用处理（web_gate.resolve_user_enabled）。默认 None：worker.py 自身不
    # import hosted，测试不必提供；生产装配见 serve_worker.build_production_deps。
    web_tools_enabled: Callable[[str], bool] | None = None
```

- [ ] **Step 2: 生产接线**

`serve_worker.py` 的 `build_production_deps()`，照 `runtime_mode_enabled`
（`:2573-2577`）的形状加：

```python
        # ``is True``，不是 bool(...)：全链路严格布尔，见 Task 1。
        web_tools_enabled=lambda user_id: (
            core_store.get_store(user_id).load_web_settings().get("enabled") is True),
```

⚠️ 具体的 store 取用方式**照抄同文件相邻装配项**，不要引入本文件没有的符号。

- [ ] **Step 3: 依赖方向不破**

```
python3 -m pytest tests/test_v2_dependency_direction.py -q
```
Expected：passed（`v2/*.py` 除 `serve_worker.py` 外不得 import `hosted` /
`agent_runtime`；本改动不违反）

- [ ] **Step 4: 提交**

```bash
git add backend/model_api_runtime/v2/worker.py backend/model_api_runtime/v2/serve_worker.py
git commit -m "feat(web-gate): TurnDeps.web_tools_enabled 注入接缝 + 生产装配

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: chat lane 接线（offer 侧）

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`（`:5783` 起、`:6408`）
- Test: `tests/test_v2_worker_tool_loop.py`（追加）

- [ ] **Step 1: 写失败测试**

照抄该文件 `:516` 附近的 offered-catalog 断言风格，追加：

```python
async def test_chat_lane_offers_web_when_user_enabled(...):
    """开启时 web_search/web_fetch 出现在首轮 offered catalog。"""
    ...
    assert {"web_search", "web_fetch"} <= first_offered


async def test_chat_lane_hides_web_when_user_disabled(...):
    """关闭时两个工具**根本不出现在请求里** —— 不是提供了再拒绝。"""
    ...
    assert {"web_search", "web_fetch"}.isdisjoint(first_offered)


async def test_web_gate_does_not_clobber_mutation_recovery(...):
    """并集,不是覆盖:mutation recovery 生效时 WRITE_ACTIONS 仍须被禁。"""
    ...
    assert cap_registry.WRITE_ACTIONS <= disabled_seen
    assert {"web_search", "web_fetch"} <= disabled_seen
```

⚠️ 参数与 fixture **照抄该文件已有的同类测试**，不要另起炉灶。

- [ ] **Step 2: RED**

```
python3 -m pytest tests/test_v2_worker_tool_loop.py -q -k "web"
```
Expected：FAIL

- [ ] **Step 3: 实现**

`worker.py:5783` 附近，把 `disabled_mutation_tool_names` 的最终值改成并集：

```python
        # web 门禁与 mutation recovery 是两件事,必须并集 —— 覆盖会让恢复期的
        # 写操作重新暴露。
        # store 读是同步的 → 必须 to_thread，否则阻塞 event loop（先例 :5291-5293）
        user_enabled = await asyncio.to_thread(
            web_gate.resolve_user_enabled, deps.web_tools_enabled, user_id)
        # 用户关 / 后台 lane 时根本不必查控制表——省一次 DB 读，也缩小攻击面
        if user_enabled and lane in web_gate.FOREGROUND_LANES:
            search_halted, fetch_halted = await asyncio.to_thread(kill_switch.web_halted)
        else:
            search_halted = fetch_halted = True
        disabled_web = web_gate.disabled_web_tools(
            user_enabled=user_enabled,
            lane=lane,                      # 真实 lane 变量（:5232），不写字面量
            search_halted=search_halted, fetch_halted=fetch_halted)
        disabled_tool_names_for_turn = frozenset(disabled_mutation_tool_names) | disabled_web
```

`:6408` 改为传 `disabled_tool_names=disabled_tool_names_for_turn`。

⚠️ **传真实的 `lane` 变量，不要写 `"chat"` 字面量**（Codex review 采纳）：
`process_job` 已有 `lane = job.get("lane") or "chat"`（`:5232`）。写字面量会引入
「有人打成 `"Chat"` 就静默全禁」的新风险；传变量则 chat 分支自然是 `"chat"`，
且策略字面量只存在于 `web_gate.FOREGROUND_LANES` 一处。

- [ ] **Step 4: GREEN + 回归**

```
python3 -m pytest tests/test_v2_worker_tool_loop.py tests/test_v2_worker.py -q
```
Expected：全绿，**既有测试不得变红**

- [ ] **Step 5: 提交**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_worker_tool_loop.py
git commit -m "feat(web-gate): chat lane 按用户开关裁剪 web 工具(并集,不覆盖)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: wake lane 接线（当前完全没有该 kwarg）

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`（`:4803` 的 `run_tool_loop`）
- Test: `tests/test_v2_wake_worker.py` / `tests/test_v2_wake_tool_loop.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
async def test_wake_lane_never_offers_web_even_when_user_enabled(...):
    """后台轮次是硬禁用:用户开着也不给。"""
    ...
    assert {"web_search", "web_fetch"}.isdisjoint(offered)
```

- [ ] **Step 2: RED → 实现**

`:4803` 的 `run_tool_loop(...)` 新增 kwarg（该调用点**当前没有**
`disabled_tool_names`）：

```python
                disabled_tool_names=web_gate.disabled_web_tools(
                    user_enabled=False, lane=lane,
                    search_halted=True, fetch_halted=True),
```

`_run_wake` 的签名里已经有 `lane: str`（`:3997`，取值 heartbeat / scheduled /
manual_wake / screen_watch）——**直接传这个变量**，保留真实 lane 身份，不要压平
成 `"wake"` 字面量。

因这些 lane 都不在 `FOREGROUND_LANES`，结果恒为两个工具全禁；**但仍然走同一个
判定函数**，将来若 lane 规则变化这里不会漏掉。这条路径**不查控制表**（结论已
确定，省一次 DB 读）。

- [ ] **Step 3: GREEN**

```
python3 -m pytest tests/test_v2_wake_worker.py tests/test_v2_wake_tool_loop.py -q
```

- [ ] **Step 4: 提交**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_wake_worker.py tests/test_v2_wake_tool_loop.py
git commit -m "feat(web-gate): wake/screen_watch lane 硬禁用 web 工具

该调用点此前完全没有传 disabled_tool_names。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: 子 agent（offer 侧 + execute 侧，同一份结果）

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`
  （`_make_task_batch_dispatcher` `:2182`、`_child_dispatch` `:2251-2262`、
  `:2341`；两个构造点 `:4343` wake / `:5807` chat）
- Test: `tests/test_v2_subagents.py`（追加）

- [ ] **Step 1: 写失败测试**

```python
async def test_child_loses_web_when_parent_lane_disables_it(...):
    """子 agent 继承父 lane 的结果,不是只继承用户布尔值。"""
    ...
    assert {"web_search", "web_fetch"}.isdisjoint(child_offered)


async def test_child_dispatch_rejects_web_when_disabled(...):
    """execute 侧是独立的第二道闸,必须同步收紧。"""
    result = await child_dispatch([ToolCall(name="web_search", ...)])
    assert result[0].content == "error: subagent_tool_not_allowed"
```

- [ ] **Step 2: 实现**

`_make_task_batch_dispatcher(:2182)` 新增 keyword `disabled_web_names`，
在函数内**算一次**并让两侧引用同一份：

```python
    child_allowed = _SUBAGENT_ALLOWED_TOOLS - set(disabled_web_names)
    child_disabled = _SUBAGENT_DISABLED_TOOLS | frozenset(disabled_web_names)
```

- offer 侧 `:2341` 传 `disabled_tool_names=child_disabled`
- execute 侧 `:2252` 的检查改用 `child_allowed`

⚠️ `_SUBAGENT_DISABLED_TOOLS`（`:593-597`）是导入时冻结的模块级常量，且
`tests/test_v2_subagents.py:285,455` 与 `test_v2_worker_tool_loop.py:634`
在断言它——**保留常量本身，只在调用点做并集**。

两个构造点 `:4343`（wake）与 `:5807`（chat）分别把**本 lane 已算出的**
`disabled_web` 传进来。

- [ ] **Step 3: GREEN + 既有断言不破**

```
python3 -m pytest tests/test_v2_subagents.py tests/test_v2_worker_tool_loop.py -q
```

- [ ] **Step 4: 提交**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_subagents.py
git commit -m "feat(web-gate): 子 agent 双闸同步(offer + execute 引用同一份结果)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8:【P0】dispatch 时的二次检查

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py`
  （`_dispatch_tools` `:4354` wake、`:5818` chat；`_child_dispatch` `:2222`）
- Test: `tests/test_v2_worker_tool_loop.py`（追加）

**为什么需要**：`turn_catalog` 只在 `run_tool_loop` 入口算一次
（`tool_loop.py:307-320`），整个多轮循环复用。运维在回合中途关掉开关，
第二轮仍然能调用。

⚠️ **`tool_loop.py` 不改**——它声明了不 import db、所有副作用注入。二次检查
放在三个 dispatcher 里（那里访问 DB 是合法的）。

- [ ] **Step 1: 写失败测试 —— 三条路径各一条**

实现会落在 chat / wake / child 三个 dispatcher，**就必须有三处证据**，否则
「三处都写了」没有对应验证：

```python
async def test_chat_dispatcher_blocks_batch_when_flipped_mid_turn(...):
    """回合开始时 fetch 开 → 第一轮跑 → 运维关掉 → 第二轮整批不执行。"""
    # 之后 kill_switch.set_web_halted(fetch=True)
    assert web_result.content == "error: web_tool_halted"
    assert sibling_result.content == "error: batch_cancelled_web_halted"


async def test_wake_dispatcher_refuses_web_even_if_a_call_reaches_it(...):
    """后台 lane 的 offer 侧已经禁了;dispatcher 是第二道闸,
    即便有 web call 直达也不执行。"""
    assert result.content == "error: web_tool_halted"


async def test_child_dispatcher_refuses_after_parent_started(...):
    """parent 建立 dispatcher 之后 flip,child 的 execute 侧仍然拒绝。"""
    assert result.content == "error: web_tool_halted"
```

- [ ] **Step 2: 实现**

先在 `worker.py` 里写**一个共用 helper**（三处各写一遍必然漂移）：

```python
async def _web_batch_cancellation(tool_calls, *, disabled_web_snapshot, lane):
    """整批取消的判定。返回 None 表示放行,否则返回要回给模型的 ToolResult 列表。

    turn_catalog 在回合入口只算一次(tool_loop.py:307-320),运维中途关闭开关对
    后续轮次无效 —— 因此每批真正执行前再查一次。控制表读带 ~2s TTL 缓存。

    语义边界:约 2s 内阻止**新的** dispatch;已经在途的 HTTP 请求不保证取消。

    检查的是「回合入口快照 ∪ 当前 live halted」。只查 live kill switch 不够 ——
    dispatcher 被定义为第二道 fail-closed 边界,就不能假设 user_enabled=True、
    把用户偏好与 lane 的判定完全交给上游。
    """
    if not any(tc.name in web_gate.WEB_TOOL_NAMES for tc in tool_calls):
        return None
    search_halted, fetch_halted = await asyncio.to_thread(kill_switch.web_halted)
    live = web_gate.disabled_web_tools(
        user_enabled=True, lane=lane,
        search_halted=search_halted, fetch_halted=fetch_halted)
    blocked = frozenset(disabled_web_snapshot) | live
    if not any(tc.name in blocked for tc in tool_calls):
        return None
    # 整批取消,但把 web 调用和被牵连的 sibling 区分开 —— 模型才知道
    # memory_fetch 不是自己失败的。
    return [
        ToolResult(
            call_id=tc.id,
            content=("error: web_tool_halted" if tc.name in web_gate.WEB_TOOL_NAMES
                     else "error: batch_cancelled_web_halted"))
        for tc in tool_calls
    ]
```

三个 dispatcher 入口各调一次，把**本 lane 在回合入口算出的** `disabled_web`
作为快照传进来：

```python
        cancelled = await _web_batch_cancellation(
            tool_calls, disabled_web_snapshot=disabled_web, lane=lane)
        if cancelled is not None:
            return cancelled
```

⚠️ **整批不执行**（与 tool_loop 的 malformed 处理一致的 all-or-nothing 语义）。
只拒 web、放行 sibling 会造成半批副作用：sibling 可能是写操作，或者
`memory_fetch` 先执行、把私密内容带进下一轮，而模型无法分辨哪些结果被策略取消。

⚠️ 控制表读失败时 `web_halted` 返回 `(True, True)` → 整批被拦。**这只影响这批
工具，聊天继续**，绝不 raise。

- [ ] **Step 3: GREEN**

```
python3 -m pytest tests/test_v2_worker_tool_loop.py tests/test_v2_wake_worker.py \
                  tests/test_v2_subagents.py -q -k "halted or cancel"
python3 -m pytest tests/test_v2_tool_loop.py -q
```
Expected：三条路径的新测试各自通过；`test_v2_tool_loop.py` **一行未改，
必须仍然 24 passed**

- [ ] **Step 4: 提交**

```bash
git add backend/model_api_runtime/v2/worker.py tests/test_v2_worker_tool_loop.py
git commit -m "feat(web-gate)!: dispatch 前二次检查 kill switch

turn_catalog 只在回合入口算一次,中途关闭的开关对第二轮无效。三个 dispatcher
入口各加一次控制表检查(~2s TTL 缓存),命中则整批不执行。tool_loop.py 一行未改。

语义:约 2s 内阻止新的 dispatch;已在途的 HTTP 请求不保证取消。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 9: API 路由 + core

**Files:**
- Create: `backend/chat/web_settings_core.py`
- Modify: `backend/chat/routes_asgi.py`
- Test: `tests/test_web_settings_routes.py`（新建，纯单测，进白名单）

**Interfaces:**
- Produces: `GET/POST /v1/web/settings`；
  `web_settings_core.get_settings(store) -> dict`、`update_settings(store, payload) -> dict`

响应形状：

```json
{
  "enabled": true,            // 用户偏好，只由用户改写
  "available": true,          // = !search_halted（入口叫「联网搜索」）
  "effective": true,          // = enabled && available，算出来的
  "unavailable_reason": null,
  "capabilities": {"search": true, "fetch": true}
}
```

- [ ] **Step 1: 写失败测试**

```python
def test_get_defaults_to_disabled_but_available(store):
    got = web_settings_core.get_settings(store, halted_reader=lambda: (False, False))
    assert got == {"enabled": False, "available": True, "effective": False,
                   "unavailable_reason": None,
                   "capabilities": {"search": True, "fetch": True}}


def test_effective_is_derived_not_stored(store):
    """kill switch 关停时 available=False,但 enabled 保持用户的选择不被回写。"""
    web_settings_core.update_settings(store, {"enabled": True},
                                      halted_reader=lambda: (False, False))
    got = web_settings_core.get_settings(store, halted_reader=lambda: (True, True))
    assert got["enabled"] is True            # 用户偏好原样保留
    assert got["available"] is False
    assert got["effective"] is False
    assert got["unavailable_reason"] == "globally_disabled"
    # 关键:存储里没有被改写
    assert store.load_web_settings()["enabled"] is True


def test_update_requires_enabled(store):
    with pytest.raises(ValueError):
        web_settings_core.update_settings(store, {})


@pytest.mark.parametrize("bad", ["no", "true", 1, 0, None])
def test_update_rejects_non_bool(store, bad):
    with pytest.raises(ValueError):
        web_settings_core.update_settings(store, {"enabled": bad})


def test_search_halted_alone_reports_unavailable(store):
    """入口叫「联网搜索」——只停了 search 就不能说可用。"""
    got = web_settings_core.get_settings(store, halted_reader=lambda: (True, False))
    assert got["available"] is False
    assert got["capabilities"] == {"search": False, "fetch": True}


def test_fetch_halted_alone_keeps_search_available(store):
    got = web_settings_core.get_settings(store, halted_reader=lambda: (False, True))
    assert got["available"] is True
    assert got["capabilities"] == {"search": True, "fetch": False}
```

- [ ] **Step 2 → 4: RED → 实现 → GREEN**

`web_settings_core.py`：

```python
def get_settings(store, *, halted_reader=kill_switch.web_halted) -> dict:
    """``halted_reader`` 注入是为了让本模块可纯单测——默认值是 DB-backed 的。"""
    enabled = store.load_web_settings().get("enabled") is True
    search_halted, fetch_halted = halted_reader()
    # 产品入口叫「联网搜索」，所以 available 跟 search 走。用
    # `not (search and fetch)` 会在「只停了搜索」时谎报可用。
    available = not search_halted
    return {
        "enabled": enabled,
        "available": available,
        "effective": enabled and available,
        "unavailable_reason": None if available else "globally_disabled",
        # 半开状态显式暴露，不让一个 available 掩盖掉
        "capabilities": {"search": not search_halted, "fetch": not fetch_halted},
    }


def update_settings(store, payload, *, halted_reader=kill_switch.web_halted) -> dict:
    if not isinstance(payload, dict) or "enabled" not in payload:
        raise ValueError("enabled is required")
    if not isinstance(payload["enabled"], bool):
        raise ValueError("enabled must be boolean")   # 严格布尔，见 Task 1
    store.save_web_settings({"enabled": payload["enabled"]})
    return get_settings(store, halted_reader=halted_reader)
```

⚠️ `enabled` **只由用户改写**，kill switch 绝不回写它。

路由照抄 `backend/chat/routes_asgi.py` 相邻路由的真实写法（装饰器 / 取 store /
400 返回 / `threadpool.run_db`），不要引入该文件没有的辅助函数。

- [ ] **Step 5: 提交**

```bash
git add backend/chat/web_settings_core.py backend/chat/routes_asgi.py \
        tests/test_web_settings_routes.py tests/conftest.py
git commit -m "feat(web-gate): GET/POST /v1/web/settings

enabled 只表示用户偏好,kill switch 不回写;effective/available 是算出来的。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 10: 公开 API 的附带义务

新增公开路由触发仓库 `CLAUDE.md`「Public documentation synchronization」。

- [ ] 更新 OpenAPI 源 / overrides
- [ ] `cd docs-site && npm run openapi:generate`，review `openapi/public.json` 的 diff
- [ ] 更新 `docs-site/content/docs/` 下受影响页面
- [ ] `docs-site/content/docs/changelog.mdx` 的 `Unreleased` 记一条：
      **联网搜索改为默认关闭，需在设置页手动开启**（这是用户可见的行为变更）
- [ ] 跑 OpenAPI 契约测试；`cd docs-site && npm run types:check && npm run lint && npm run build`
- [ ] 提交

---

### 发布顺序（⚠️ 不是"短窗口"问题）

默认关 + 无迁移 + iOS 无法强制更新 ⇒ **所有仍在用旧客户端的用户会持续无法开启
联网，直到他们更新 App**——这不是后端与 iOS 发布之间的一段窗口。

**不要为此加一个"功能未启用"的隐藏总闸**：那正是本工作区明令禁止的、
造成过「代码上线了功能没上线」的东西。正确顺序：

1. iOS 版本先上架并确认可下载
2. 通知内测用户更新
3. 再发布后端 Task 1–10
4. 交付说明写明：**旧客户端用户在更新前无法开启联网**
5. 若该能力对内测体验关键，用**最低版本要求**，而不是再加隐形开关

---

### Task 11: iOS 开关 —— 拆成独立的 iOS 实施计划

**Files:**（仓库 `/Users/hx/Projects/io/feedling-mcp-ios`，走 PR 不直接 merge）
- `App/FeedlingTest/Pages/Settings/SettingsView.swift`（`customizationSettingsList`）
- `App/FeedlingTest/API/FeedlingAPI.swift`
- `App/FeedlingTest/Localizable.xcstrings`

⚠️ **本 task 目前只是 checklist，不是可执行计划**（Codex review 指出）。它在
另一个 repo、另一条 PR 线上，且**直接决定发布顺序**，因此必须单独写成一份
`docs/superpowers/plans/` 下的 iOS 实施计划，至少覆盖：

- 首次进入设置页的 `GET /v1/web/settings` 时机与 loading 态
- `POST` 失败时 UI 回滚还是保留乐观值
- **旧后端返回 404 时 fail closed**（置灰 + 说明，不能显示成可用）
- response 的 decode model（含新增的 `capabilities` 对象）
- 精确的 localization key 列表
- 单测 / 构建命令与预期输出
- 真机验证前可自动化的部分

文案必须说清两件事：**打开只是把工具交给模型、搜不搜由模型判断**；
**后台主动陪伴不会联网**。

本后端计划的 Task 1–10 不依赖它，但**发布顺序依赖它先上架**（见上一节）。

---

## 完成标准（DoD）

- `docs/testing/TESTING.md` §2 决策矩阵：按动过的文件类别对号入座；满足 §7 DoD。
- 全量本地跑：`python3 -m pytest tests/ -q`（本地 Postgres 已就绪），
  与基线对比**零新增失败**。
- 新增纯单测文件全部在 `_PURE_UNIT` 内且 `--collect-only` 确认被收集；
  DB-backed 的**不要**加进去。
- `tests/test_v2_dependency_direction.py` 绿。
- 共享核心**零改动**，用机器断言而不是目测：

  ```bash
  git diff --exit-code origin/pre -- \
    backend/model_api_runtime/v2/tool_loop.py \
    backend/capabilities/tool_schema.py
  ```
  Expected：exit 0（有任何差异即失败）
- 交付说明必须高亮：**这是一次能力回收**，现有用户会失去联网（详见设计文档
  §7），上线后**必须主动验证开启率**。
