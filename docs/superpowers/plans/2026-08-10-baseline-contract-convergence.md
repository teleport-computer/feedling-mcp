# 基线契约收口实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 收口 9 项陈旧测试契约，并让 TEE verify 严格覆盖 `voice_transcripts`，恢复排除外部服务型 `tests/test_api.py` 后的后端零失败基线。

**Architecture:** 已确认的 admin、Genesis、voice、image-generation 和 web 行为保持不变，只修正其二级测试契约。唯一生产修改位于 `tee_shadow.verify`：把已由复制器同步的 `voice_transcripts` 登记为严格行数与 terminal-pending 核验表，但不声称进行内容抽样。

**Tech Stack:** Python 3.11、pytest、Flask/FastAPI parity helpers、Psycopg 3、PostgreSQL、TEE shadow verifier。

## Global Constraints

- 不回滚 admin IA、ASGI 页面缓存、Genesis 活跃任务保护、voice transcript、image generation 或 web policy 功能。
- 不删除公开字段、不放宽运行时保护、不新增 skip/xfail。
- HTML parity 只剥离 `<div class='cache-note'>...</div>`；除此元素外，HTML 继续精确比较。
- Genesis 的真实连续第二次 plaintext 请求继续返回 `409 import_job_active`；只有 parity 测试在框架调用之间清理状态。
- `voice_transcripts` verify 配置固定使用 `rds_table="voice_transcripts"`、`tee_table="voice_transcripts"`、`item_col="call_id"`、`pending_table="voice_transcripts"`、`kind=None`，并沿用默认 `strict=True`。
- `kind=None` 只代表不做内容抽样；必须核对严格行数、terminal pending 和 report 汇总状态。
- 每项生产修复先观察覆盖该行为的测试按预期失败，再写最小实现。
- 在 `fix/health-probe-isolation` worktree 分支实施；基线恢复前不进入健康探针 Task 1。
- 测试统一使用 `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python` 和 `FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres'`。

---

## 文件结构

- 修改 `tests/test_admin_usage.py`：从诊断二级导航验证单用户 usage 链接。
- 修改 `tests/test_asgi_admin.py`：只标准化 ASGI cache note，并更新裸 data-track 首页标题。
- 修改 `tests/test_asgi_genesis.py`：隔离 Flask/ASGI 首次请求状态，同时锁住连续请求 409。
- 修改 `tests/test_capabilities_registry.py`：同步两个 voice transcript 只读能力。
- 修改 `tests/test_memory_readside.py`：同步 fetch-only `voice_call_id` 契约。
- 修改 `tests/test_model_api_profiles_db.py`：同步 image-generation 扩展后的 `_ROUTE_COLUMNS` 下标。
- 修改 `tests/test_v2_status_poll.py`：同步 `web_policy` 的默认和显式投影。
- 修改 `tests/test_tee_verify.py`：新增 voice transcript 两侧一致与 RDS-only 行数测试。
- 修改 `backend/tee_shadow/verify.py`：登记 `voice_transcripts` 严格行数核验配置。

---

### Task 1: 收口 Admin 与 Genesis 测试契约

**Files:**
- Modify: `tests/test_admin_usage.py:2566-2630`
- Modify: `tests/test_asgi_admin.py:135-150,292-311`
- Modify: `tests/test_asgi_genesis.py:554-565`

**Interfaces:**
- Consumes: 现有 `_render_data_track_view_nav` 二级导航、ASGI `cache-note`、`_reset_genesis(uid)` 测试 helper。
- Produces: 不改变生产接口；产出三个测试文件与当前生产契约一致的回归保护。

- [ ] **Step 1: 重跑三个现有失败组，记录红灯**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_admin_usage.py::test_usage_user_page_keeps_drilldown_path_in_presets_filters_and_sorts \
  tests/test_asgi_admin.py::test_data_track_page_parity \
  tests/test_asgi_admin.py::test_data_track_dau_page_parity \
  tests/test_asgi_genesis.py::test_plaintext_update_identity_without_identity_enqueues_202_parity -q
```

Expected: 4 FAIL；分别显示一级 nav 找不到 `Token 与模型`、ASGI 多 cache-note、旧首页标题不匹配或 parity 不同、第二次 Genesis 请求为 409。

- [ ] **Step 2: 让 usage 测试只在诊断二级导航找链接**

将 nav 切片改为：

```python
    nav_start = body.index("<nav class='viewbar viewbar-diag'")
    nav_end = body.index("</nav>", nav_start)
    usage_nav = re.search(
        r"href='([^']+)'[^>]*>Token 与模型</a>",
        body[nav_start:nav_end],
    )
```

本 Step 只替换上述 nav 切片代码块；preset、form、sort path 和 query 参数的现有断言不在修改范围内。

- [ ] **Step 3: 标准化唯一允许的 ASGI cache note**

在 `_TS_RE` 附近增加：

```python
_CACHE_NOTE_RE = re.compile(
    r"<div class='cache-note'[^>]*>页面缓存 · 数据生成于 [^<]*</div>\n?"
)
```

将 helper 改为：

```python
def _norm_html(text: str) -> str:
    without_cache_note = _CACHE_NOTE_RE.sub("", text)
    return _TS_RE.sub("TS", without_cache_note)
```

将裸页标题断言改为：

```python
    assert "Feedling 值班首页" in f_body
```

保留 DAU 页 `Daily Active Users`、状态码、Content-Type 和 normalized-body 相等断言。

- [ ] **Step 4: 隔离 Genesis parity 状态并锁定真实 409**

把现有测试改成：

```python
def test_plaintext_update_identity_without_identity_enqueues_202_parity(user, monkeypatch):
    uid, api_key = user
    payload = {
        "mode": "update_identity",
        "ai_persona_content": "Name: Joy",
        "client_job_id": "identity-x",
    }
    monkeypatch.setattr(
        genesis_routes,
        "_start_plaintext_genesis_job",
        lambda *_args, **_kwargs: True,
    )

    f = _flask(
        "POST",
        "/v1/genesis/imports/plaintext",
        headers=_headers(api_key),
        json_body=payload,
    )
    _reset_genesis(uid)
    a = _asgi(
        "POST",
        "/v1/genesis/imports/plaintext",
        headers=_headers(api_key),
        json_body=payload,
    )

    assert f[0] == a[0] == 202
    assert _norm(f) == _norm(a)
    assert f[1]["status"] == "processing"


def test_plaintext_update_identity_rejects_a_second_active_job(user, monkeypatch):
    _uid, api_key = user
    payload = {
        "mode": "update_identity",
        "ai_persona_content": "Name: Joy",
        "client_job_id": "identity-active",
    }
    monkeypatch.setattr(
        genesis_routes,
        "_start_plaintext_genesis_job",
        lambda *_args, **_kwargs: True,
    )

    first = _asgi(
        "POST",
        "/v1/genesis/imports/plaintext",
        headers=_headers(api_key),
        json_body=payload,
    )
    second = _asgi(
        "POST",
        "/v1/genesis/imports/plaintext",
        headers=_headers(api_key),
        json_body=payload,
    )

    assert first[0] == 202
    assert second[0] == 409
    assert second[1]["error"] == "import_job_active"
```

- [ ] **Step 5: 跑受影响文件并静态检查**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_admin_usage.py tests/test_asgi_admin.py tests/test_asgi_genesis.py -q
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pyflakes \
  tests/test_admin_usage.py tests/test_asgi_admin.py tests/test_asgi_genesis.py
git diff --check
```

Expected: 三个测试文件零失败；pyflakes 和 diff check 退出 0。

- [ ] **Step 6: 提交**

```bash
git add tests/test_admin_usage.py tests/test_asgi_admin.py tests/test_asgi_genesis.py
git commit -m "test: align admin and genesis contracts"
```

---

### Task 2: 收口 Voice、Image 与 Web 测试契约

**Files:**
- Modify: `tests/test_capabilities_registry.py:8-55`
- Modify: `tests/test_memory_readside.py:346-380`
- Modify: `tests/test_model_api_profiles_db.py:535-586`
- Modify: `tests/test_v2_status_poll.py:29-54`

**Interfaces:**
- Consumes: `registry.CAPABILITIES`/`READ_ACTIONS`、`readside.build_memory_fetch_item`/`build_memory_index_item`、`db._ROUTE_COLUMNS`、`chat.poll_core.build_response`。
- Produces: 不改变生产接口；精确锁定四组已合入的扩展契约。

- [ ] **Step 1: 重跑五个现有失败节点，记录红灯**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_capabilities_registry.py::test_all_action_types_registered \
  tests/test_capabilities_registry.py::test_capabilities_is_a_real_populated_dict \
  tests/test_memory_readside.py::test_enclave_fetch_item_returns_v1_full_card_without_sensitive_scope \
  tests/test_model_api_profiles_db.py::test_route_columns_timestamps_are_utc_invariant_under_session_timezone \
  tests/test_v2_status_poll.py::test_build_response_defaults_are_empty_and_backward_compatible -q
```

Expected: 5 FAIL；差异分别为两个 voice verbs、`voice_call_id`、旧 SQL 下标和 `web_policy`。

- [ ] **Step 2: 更新 capability 精确集合并锁定只读分类**

在两个 expected 集合中都加入：

```python
        "voice_transcript_list", "voice_transcript_read",
```

把数量断言改成基于集合：

```python
    expected = {
        "identity_get", "identity_patch", "identity_nudge",
        "memory_index", "memory_fetch", "memory_write", "memory_search",
        "perception_snapshot", "perception_recent_apps", "perception_trend",
        "perception_history", "perception_glance", "screen_recent",
        "screen_read", "photo_recent", "photo_read", "chat_image_read",
        "chat_file_read", "voice_transcript_list", "voice_transcript_read",
        "web_search", "web_fetch", "schedule_wake", "cancel_wake",
        "workspace_list", "workspace_read", "workspace_write", "workspace_delete",
    }
    assert len(registry.CAPABILITIES) == len(expected)
    assert set(registry.CAPABILITIES) == expected
    assert len(list(registry.CAPABILITIES.items())) == len(expected)
    assert {"voice_transcript_list", "voice_transcript_read"} <= registry.READ_ACTIONS
    assert not ({"voice_transcript_list", "voice_transcript_read"} & registry.WRITE_ACTIONS)
```

- [ ] **Step 3: 更新 memory fetch-only voice call 契约**

在现有 expected dict 末尾加入：

```python
        "voice_call_id": "",
```

在同一测试末尾增加非空投影检查：

```python
    voice_inner = {
        "summary": "Voice memory",
        "content": "Transcript-derived memory",
        "bucket": "comfort",
        "threads": ["voice"],
        "voice_call_id": "vcall_123",
    }
    fetch_item = readside.build_memory_fetch_item({"id": "mem_voice"}, voice_inner)
    index_item = readside.build_memory_index_item({"id": "mem_voice"}, voice_inner)
    assert fetch_item["voice_call_id"] == "vcall_123"
    assert "voice_call_id" not in index_item
```

保留 `sensitive_scope` 不泄漏断言。

- [ ] **Step 4: 更新 `_ROUTE_COLUMNS` 时间戳位置**

将循环和非空断言替换为：

```python
    for idx, name in (
        (14, "last_test_at"),
        (17, "last_vision_test_at"),
        (20, "last_image_generation_test_at"),
        (24, "created_at"),
        (25, "updated_at"),
    ):
        assert baseline[idx] == shifted[idx], (
            f"{name} changed under SET TIME ZONE — to_char is reading the "
            f"session GUC instead of a fixed UTC offset: "
            f"{baseline[idx]!r} != {shifted[idx]!r}"
        )
    assert baseline[24] and baseline[24].endswith("Z")
    assert baseline[25] and baseline[25].endswith("Z")
```

同步其上方注释为相同下标，避免文档和断言再次漂移。

- [ ] **Step 5: 更新 chat poll web policy 契约**

在默认测试中增加：

```python
    assert resp["web_policy"] is None
```

并把 `"web_policy"` 加入 exact key set。随后新增：

```python
def test_build_response_projects_explicit_web_policy():
    policy = {"effective": True, "search": True, "fetch": False}
    resp = chat_poll_core.build_response(
        messages=[],
        context={
            "runtime_v2": {},
            "client_release": {},
            "user_mcp": {},
            "web_policy": policy,
        },
        consumer_id="c1",
        claim=False,
        timed_out=False,
    )
    assert resp["web_policy"] == policy
```

- [ ] **Step 6: 跑受影响文件并静态检查**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_capabilities_registry.py tests/test_memory_readside.py \
  tests/test_model_api_profiles_db.py tests/test_v2_status_poll.py -q
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pyflakes \
  tests/test_capabilities_registry.py tests/test_memory_readside.py \
  tests/test_model_api_profiles_db.py tests/test_v2_status_poll.py
git diff --check
```

Expected: 四个文件零失败；pyflakes 和 diff check 退出 0。

- [ ] **Step 7: 提交**

```bash
git add \
  tests/test_capabilities_registry.py tests/test_memory_readside.py \
  tests/test_model_api_profiles_db.py tests/test_v2_status_poll.py
git commit -m "test: align voice image and web contracts"
```

---

### Task 3: 让 TEE Verify 覆盖 Voice Transcripts

**Files:**
- Modify: `tests/test_tee_verify.py:452-465`
- Modify: `backend/tee_shadow/verify.py:71-119`

**Interfaces:**
- Consumes: `verify.run(sample_rate: float) -> dict`、`_CIPHERTEXT_TABLES` 的现有行数/pending 配置协议、RDS `voice_transcripts.transcript_envelope`、TEE `voice_transcripts.doc`。
- Produces: `verify.covered_tables()` 包含 `voice_transcripts`；report `tables["voice_transcripts"]` 具有 `rds_rows`、`tee_rows`、`pending_rows`、`rows_ok`、`requeue_backlog`，顶层 `strict_ok` 反映严格结果。

- [ ] **Step 1: 先写两侧行数行为测试**

在 coverage guard 后增加 helper：

```python
def _insert_rds_voice_transcript(uid: str, call_id: str) -> None:
    envelope = {
        "v": 1,
        "id": call_id,
        "owner_user_id": uid,
        "visibility": "shared",
        "body_ct": "ciphertext",
        "nonce": "nonce",
        "K_user": "wrapped-user-key",
        "K_enclave": "wrapped-enclave-key",
    }
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO voice_transcripts "
            "(user_id, call_id, transcript_envelope) VALUES (%s,%s,%s)",
            (uid, call_id, Jsonb(envelope)),
        )


def _insert_tee_voice_transcript(uid: str, call_id: str) -> None:
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as conn:
        conn.execute(
            "INSERT INTO voice_transcripts (user_id, call_id, doc) VALUES (%s,%s,%s)",
            (uid, call_id, Jsonb({"transcript": "hello"})),
        )
```

新增测试：

```python
def test_voice_transcripts_equal_rows_are_strictly_verified():
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_rds_voice_transcript(uid, "vcall_equal")
    _insert_tee_voice_transcript(uid, "vcall_equal")

    report = verify.run(sample_rate=1.0)

    voice = report["tables"]["voice_transcripts"]
    assert voice["rds_rows"] == 1
    assert voice["tee_rows"] == 1
    assert voice["pending_rows"] == 0
    assert voice["rows_ok"] is True
    assert report["strict_ok"] is True
    assert report["ok"] is True


def test_voice_transcripts_rds_only_row_fails_strict_verification():
    uid = f"usr_{uuid.uuid4().hex[:8]}"
    _seed(uid)
    _insert_rds_voice_transcript(uid, "vcall_missing")

    report = verify.run(sample_rate=1.0)

    voice = report["tables"]["voice_transcripts"]
    assert voice["rds_rows"] == 1
    assert voice["tee_rows"] == 0
    assert voice["pending_rows"] == 0
    assert voice["rows_ok"] is False
    assert report["strict_ok"] is False
    assert report["ok"] is False
```

- [ ] **Step 2: 运行新测试并确认缺少 report 表项**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_tee_verify.py::test_voice_transcripts_equal_rows_are_strictly_verified \
  tests/test_tee_verify.py::test_voice_transcripts_rds_only_row_fails_strict_verification -q
```

Expected: 两项都以 `KeyError: 'voice_transcripts'` 失败；若失败发生在 fixture 或 SQL 装配，先修正测试直到它准确暴露缺失 report 项，不得提前改生产代码。

- [ ] **Step 3: 添加最小 verify 配置**

在 `_CIPHERTEXT_TABLES` 末尾增加：

```python
    "voice_transcripts": dict(
        rds_table="voice_transcripts",
        tee_table="voice_transcripts",
        item_col="call_id",
        pending_table="voice_transcripts",
        kind=None,
    ),
```

不要添加 `strict=False`，不要为 `kind=None` 新增内容 transform。

- [ ] **Step 4: 跑新测试、coverage guard 与完整 verify 文件**

Run:

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_tee_verify.py::test_voice_transcripts_equal_rows_are_strictly_verified \
  tests/test_tee_verify.py::test_voice_transcripts_rds_only_row_fails_strict_verification \
  tests/test_tee_verify.py::test_verify_covers_every_synced_table -q
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_tee_verify.py -q
```

Expected: 所有选择项和完整 verify 文件零失败。

- [ ] **Step 5: 静态检查并提交**

```bash
/Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pyflakes \
  backend/tee_shadow/verify.py tests/test_tee_verify.py
git diff --check
git add backend/tee_shadow/verify.py tests/test_tee_verify.py
git commit -m "fix: verify voice transcript replication"
```

---

## Final Verification

- [ ] **Step 1: 重跑最初 10 个失败节点**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_admin_usage.py::test_usage_user_page_keeps_drilldown_path_in_presets_filters_and_sorts \
  tests/test_asgi_admin.py::test_data_track_page_parity \
  tests/test_asgi_admin.py::test_data_track_dau_page_parity \
  tests/test_asgi_genesis.py::test_plaintext_update_identity_without_identity_enqueues_202_parity \
  tests/test_capabilities_registry.py::test_all_action_types_registered \
  tests/test_capabilities_registry.py::test_capabilities_is_a_real_populated_dict \
  tests/test_memory_readside.py::test_enclave_fetch_item_returns_v1_full_card_without_sensitive_scope \
  tests/test_model_api_profiles_db.py::test_route_columns_timestamps_are_utc_invariant_under_session_timezone \
  tests/test_tee_verify.py::test_verify_covers_every_synced_table \
  tests/test_v2_status_poll.py::test_build_response_defaults_are_empty_and_backward_compatible -q
```

Expected: 10 passed。

- [ ] **Step 2: 运行受影响文件全集**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests/test_admin_usage.py tests/test_asgi_admin.py tests/test_asgi_genesis.py \
  tests/test_capabilities_registry.py tests/test_memory_readside.py \
  tests/test_model_api_profiles_db.py tests/test_tee_verify.py \
  tests/test_v2_status_poll.py -q
```

Expected: 零失败、零错误。

- [ ] **Step 3: 恢复完整后端基线**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' \
  /Users/zhengzhihao/Projects/teleport/feedling-mcp/.venv-test/bin/python -m pytest \
  tests -q --ignore=tests/test_api.py
```

Expected: exit 0，零失败、零错误；输出必须包含 PostgreSQL-backed suites。`tests/test_api.py` 仍需另行启动 `localhost:5001`，不得把未运行写成通过。

- [ ] **Step 4: 检查分支和提交序列**

```bash
git diff --check
git status --short
git log --oneline -6
```

Expected: diff check 无输出、工作区为空、日志包含三个基线收口提交；随后才进入健康探针隔离计划 Task 1。
