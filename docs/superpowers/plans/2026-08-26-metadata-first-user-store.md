# Metadata-first UserStore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `UserStore` construction SQL-free and load only explicitly requested state sections, eliminating non-Chat paths that currently trigger a 256-row Chat hot snapshot.

**Architecture:** A focused `store_sections` module owns load-mode parsing and the per-section singleflight state machine. `UserStore` remains the stable per-user object, but construction creates only locks, waiters, empty values, and section slots; `get_store(..., require=...)` loads named sections, while TTL and cross-worker notifications affect only already-loaded sections. Existing bounded history, poll, Runtime V2, and write-through database paths keep their contracts and use a shell unless they read a cached section.

**Tech Stack:** Python 3.11+, PostgreSQL 16, psycopg 3, FastAPI/ASGI, PostgreSQL LISTEN/NOTIFY, gunicorn, pytest, Docker Compose, MDX.

**Spec:** `docs/superpowers/specs/2026-08-26-metadata-first-user-store-design.md`

## Global Constraints

- PostgreSQL remains authoritative; this phase has no schema or historical-data migration.
- Do not change RDS → TEE dual write, synchronization, import, or selected-primary behavior.
- Do not optimize `memory_moments`, `user_logs`, or `v2_runtime_state` in this phase.
- Keep `FEEDLING_CHAT_HOT_CACHE_LIMIT=256` in TEST and PROD.
- Preserve Chat/history/poll/V2 prompt-tail/resident-redelivery membership, ordering, clear semantics, and waiter latency.
- Shell-only paths may execute existing bounded SQL but must execute zero Chat hot-snapshot queries.
- First strict section-load failure returns a structured retryable 503; refresh failure retains last-good state.
- Logs and metrics contain no raw user ID, message body, ciphertext, API key, token, private configuration, or database URL.
- Rollout order is `legacy -> selective -> lazy`; rollback uses the same artifact with `FEEDLING_STORE_LOAD_MODE=legacy`.
- Acceptance requires at least 90% fewer Chat hot snapshots, at least 80% fewer `chat_messages` tuple fetches, key-path p95 regression no greater than 10%, and no correctness/error regression.

## File Map

- `backend/core/store_sections.py`: enums, mode parsing, singleflight slot, and section-load exception.
- `backend/core/store.py`: SQL-free shell, explicit `require`, section-aware TTL/reload/eviction, telemetry, and cold Chat writes.
- `backend/core/wake_bus.py`: loaded-only invalidation and reconnect catch-up.
- `backend/asgi/middleware.py`: stable 503 mapping.
- Production callers under `backend/accounts`, `admin`, `agent_runtime`, `genesis`, `hosted`, `model_api_runtime/v2`, `perception`, `screen`, `voice`, plus `backend/asgi_app.py`: explicit shell/section migration.
- `tests/test_store_sections.py`: state-machine tests.
- `tests/store_load_helpers.py`: shared loader-call instrumentation used by Store/wake query-budget tests.
- `tests/test_store_cache.py`: shell, TTL, eviction, query-budget, and mode tests.
- `tests/test_chat_incremental_sync.py`, `tests/test_store_append_chat_file.py`, `tests/test_v2_chat_clear_fence.py`: cold-write/load/clear concurrency.
- `tests/test_wake_bus.py`, `tests/test_chat_poll_cross_worker_staleness.py`: cross-worker behavior.
- `tests/test_store_load_contract.py`: AST guard and reviewed call-site manifest.
- `.github/workflows/ci.yml`: register new suites.
- `deploy/docker-compose.phala.test.yaml`, `deploy/docker-compose.phala.yaml`, `tests/test_deploy_yaml_strict.py`: pinned load mode.
- `docs/ops/metadata-first-user-store-runbook.md` and docs-site architecture/workflow/changelog: rollout and public documentation.

---

### Task 1: Section types, mode parsing, and singleflight

**Files:**
- Create: `backend/core/store_sections.py`
- Create: `tests/test_store_sections.py`

**Interfaces:**
- Produces `StoreSection(str, Enum)`: `CHAT`, `FRAMES`, `WORLD_BOOKS`, `TOKENS`, `PUSH_STATE`, `LIVE_ACTIVITY`.
- Produces `SectionStatus(str, Enum)`: `UNLOADED`, `LOADING`, `FRESH`, `STALE`.
- Produces `StoreLoadMode(str, Enum)`: `LEGACY`, `SELECTIVE`, `LAZY`.
- Produces `store_load_mode() -> StoreLoadMode`.
- Produces `SectionSlot.ensure(loader, *, force: bool, strict: bool) -> bool` and `mark_stale(*, dirty_version: int | None = None) -> bool`.
- Produces `StoreSectionUnavailable` with fixed slug `store_section_unavailable`.

- [ ] **Step 1: Write RED tests for modes and basic transitions**

```python
@pytest.mark.parametrize("raw,expected", [
    (None, StoreLoadMode.LEGACY),
    ("legacy", StoreLoadMode.LEGACY),
    ("selective", StoreLoadMode.SELECTIVE),
    ("lazy", StoreLoadMode.LAZY),
])
def test_store_load_mode(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("FEEDLING_STORE_LOAD_MODE", raising=False)
    else:
        monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", raw)
    assert store_load_mode() is expected


def test_invalid_store_load_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "typo")
    with pytest.raises(RuntimeError, match="FEEDLING_STORE_LOAD_MODE"):
        store_load_mode()


def test_section_slot_first_load_and_stale_refresh():
    slot = SectionSlot(StoreSection.CHAT)
    calls = []
    assert slot.ensure(lambda: calls.append("cold"), force=False, strict=True)
    assert slot.mark_stale(dirty_version=7)
    assert slot.ensure(lambda: calls.append("refresh"), force=False, strict=True)
    assert calls == ["cold", "refresh"]
    assert slot.status is SectionStatus.FRESH
```

- [ ] **Step 2: Write RED tests for concurrency and failures**

```python
def test_one_hundred_first_callers_share_one_load():
    slot = SectionSlot(StoreSection.CHAT)
    entered, release = threading.Event(), threading.Event()
    calls = 0
    def load():
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(2)
    with ThreadPoolExecutor(max_workers=100) as pool:
        futures = [pool.submit(slot.ensure, load, force=False, strict=True) for _ in range(100)]
        assert entered.wait(2)
        release.set()
        assert all(f.result(timeout=2) for f in futures)
    assert calls == 1


def test_first_failure_unloads_but_refresh_failure_keeps_stale():
    slot = SectionSlot(StoreSection.CHAT)
    fail = lambda: (_ for _ in ()).throw(RuntimeError("db down"))
    with pytest.raises(StoreSectionUnavailable):
        slot.ensure(fail, force=False, strict=True)
    assert slot.status is SectionStatus.UNLOADED
    assert slot.ensure(lambda: None, force=False, strict=True)
    slot.mark_stale()
    assert slot.ensure(fail, force=False, strict=False) is False
    assert slot.status is SectionStatus.STALE
```

- [ ] **Step 3: Verify RED**

```bash
.venv-test/bin/python -m pytest tests/test_store_sections.py -q
```

Expected: collection fails because `core.store_sections` does not exist.

- [ ] **Step 4: Implement the primitive**

```python
class StoreSection(str, Enum):
    CHAT = "chat"
    FRAMES = "frames"
    WORLD_BOOKS = "world_books"
    TOKENS = "tokens"
    PUSH_STATE = "push_state"
    LIVE_ACTIVITY = "live_activity"


class StoreSectionUnavailable(RuntimeError):
    slug = "store_section_unavailable"
    def __init__(self, section: StoreSection):
        super().__init__(f"store section unavailable: {section.value}")
        self.section = section
```

Implement `SectionSlot` with `threading.Condition`. The loader runs outside the condition; waiters loop after notification. On error set `STALE` if a last-good cache existed, otherwise `UNLOADED`. `mark_stale` leaves `UNLOADED` unloaded and stores only a numeric dirty-version hint while `LOADING`.

- [ ] **Step 5: Verify and commit**

```bash
.venv-test/bin/python -m pytest tests/test_store_sections.py -q
git add backend/core/store_sections.py tests/test_store_sections.py
git commit -m "feat(store): add section load state machine"
```

---

### Task 2: SQL-free Store shell and explicit section API

**Files:**
- Modify: `backend/core/store.py`
- Modify: `backend/asgi/middleware.py`
- Create: `tests/store_load_helpers.py`
- Modify: `tests/test_store_cache.py`

**Interfaces:**
- Consumes Task 1 types.
- Produces `ALL_STORE_SECTIONS: frozenset[StoreSection]`.
- Produces `UserStore.ensure_sections(sections, *, reason="first_use", strict=True, force=False) -> bool`.
- Produces `UserStore.loaded_sections() -> frozenset[StoreSection]`.
- Produces `get_store(user_id: str, *, require: Iterable[StoreSection] = ()) -> UserStore`.
- Produces temporary `get_store_legacy(user_id: str) -> UserStore`.
- Maps `StoreSectionUnavailable` to `503 {"error":"store_section_unavailable","section":"<enum>"}`.

- [ ] **Step 1: Write shell/isolation RED tests**

```python
def test_constructor_and_shell_get_are_sql_free(monkeypatch):
    calls = install_counting_loaders(monkeypatch, core_store)
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "lazy")
    store = core_store.get_store("u-shell")
    assert calls == []
    assert store.loaded_sections() == frozenset()


def test_require_chat_loads_only_chat(monkeypatch):
    calls = install_counting_loaders(monkeypatch, core_store)
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", "lazy")
    store = core_store.get_store("u-chat", require={StoreSection.CHAT})
    assert calls == ["chat"]
    assert store.loaded_sections() == frozenset({StoreSection.CHAT})
```

Define the shared test helper in `tests/test_store_cache.py`:

```python
def install_counting_loaders(monkeypatch, core_store):
    calls = []
    mapping = {
        "reload_chat_hot_strict": "chat",
        "_load_frames_meta": "frames",
        "_load_world_books": "world_books",
        "_load_tokens": "tokens",
        "_load_push_state": "push_state",
        "_load_live_activity_state": "live_activity",
    }
    for method_name, label in mapping.items():
        monkeypatch.setattr(
            core_store.UserStore,
            method_name,
            lambda _self, value=label: calls.append(value),
        )
    return calls
```

- [ ] **Step 2: Write mode and 503 RED tests**

```python
@pytest.mark.parametrize("mode,ordinary,compat", [
    ("legacy", 6, 6), ("selective", 0, 6), ("lazy", 0, 6),
])
def test_load_mode_matrix(monkeypatch, mode, ordinary, compat):
    calls = install_counting_loaders(monkeypatch, core_store)
    monkeypatch.setenv("FEEDLING_STORE_LOAD_MODE", mode)
    core_store.get_store(f"u-{mode}-ordinary")
    assert len(calls) == ordinary
    calls.clear()
    core_store.get_store_legacy(f"u-{mode}-compat")
    assert len(calls) == compat
```

Test the registered ASGI handler directly and assert the chained DB error text is absent from the 503 body.

- [ ] **Step 3: Verify RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_store_cache.py -q
```

Expected: Store construction remains eager and `require` is unsupported.

- [ ] **Step 4: Implement shell and loaders**

Remove all six loads from `UserStore.__init__`; retain identity, empty values, locks, waiters, and add one slot per section. Use this fixed mapping:

```python
def _section_loader(self, section):
    return {
        StoreSection.CHAT: self.reload_chat_hot_strict,
        StoreSection.FRAMES: self._load_frames_meta,
        StoreSection.WORLD_BOOKS: self._load_world_books,
        StoreSection.TOKENS: self._load_tokens,
        StoreSection.PUSH_STATE: self._load_push_state,
        StoreSection.LIVE_ACTIVITY: self._load_live_activity_state,
    }[section]
```

Sort requested sections by enum value, run loaders under `_reload_guard`, and preserve successful sections if a later section fails.

- [ ] **Step 5: Implement registry modes**

```python
def get_store(user_id: str, *, require=()) -> UserStore:
    with _stores_lock:
        store = _stores.get(user_id)
        if store is None:
            store = UserStore(user_id)
            _stores[user_id] = store
    store.mark_expired_sections_stale(time.monotonic())
    mode = store_load_mode()
    if mode is StoreLoadMode.LEGACY:
        store.ensure_sections(
            ALL_STORE_SECTIONS, reason="legacy_compat", strict=False
        )
    elif require:
        store.ensure_sections(
            frozenset(require), reason="first_use", strict=True
        )
    return store


def get_store_legacy(user_id: str) -> UserStore:
    store = get_store(user_id)
    store.ensure_sections(ALL_STORE_SECTIONS, reason="legacy_compat", strict=False)
    return store
```

Register a fixed ASGI exception handler in `backend/asgi/middleware.py`; never render the underlying exception.

Add a multi-section failure test: if `TOKENS` succeeds and `CHAT` fails in the same `require` call, Tokens stays `FRESH`, Chat returns to `UNLOADED`, and the call raises the structured Chat exception.

- [ ] **Step 6: Verify and commit**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_store_sections.py tests/test_store_cache.py tests/test_asgi_chat_remaining.py -q
git add backend/core/store.py backend/asgi/middleware.py tests/store_load_helpers.py tests/test_store_cache.py
git commit -m "feat(store): make user store construction metadata-only"
```

---

### Task 3: Section-aware TTL, reload, eviction, and telemetry

**Files:**
- Modify: `backend/core/store.py`
- Modify: `tests/test_store_cache.py`
- Modify: `tests/test_chat_incremental_sync.py`

**Interfaces:**
- Produces `mark_expired_sections_stale(now_mono) -> frozenset[StoreSection]`.
- Changes `reload() -> bool` and `_evict_store()` to refresh only loaded sections.
- Produces fixed-enum `_store_load_telemetry(...)` without user identifiers.

- [ ] **Step 1: Write TTL/reload RED tests**

```python
def test_ttl_marks_loaded_chat_stale_without_loading(monkeypatch):
    store = core_store.get_store("u-ttl", require={StoreSection.CHAT})
    calls = install_counting_loaders(monkeypatch, core_store)
    store._section_slots[StoreSection.CHAT].loaded_at_mono = 1.0
    monkeypatch.setattr(core_store.time, "monotonic", lambda: 1000.0)
    assert core_store.get_store("u-ttl") is store
    assert calls == []
    assert store._section_slots[StoreSection.CHAT].status is SectionStatus.STALE
    assert store._section_slots[StoreSection.TOKENS].status is SectionStatus.UNLOADED


def test_reload_refreshes_only_used_sections(monkeypatch):
    store = core_store.get_store("u", require={StoreSection.CHAT, StoreSection.TOKENS})
    calls = install_counting_loaders(monkeypatch, core_store)
    assert store.reload()
    assert calls == ["chat", "tokens"]
```

- [ ] **Step 2: Write last-good and telemetry privacy RED tests**

Make a good Chat load, mark it stale, force the DB helper to raise, and assert rows remain and status is `STALE`. Capture the logger and assert fixed `section/reason/cache_state/rows/duration/outcome` fields exist while `user_id`, `body_ct`, `K_user`, and `postgresql://` do not.

- [ ] **Step 3: Verify RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_store_cache.py tests/test_chat_incremental_sync.py -q
```

- [ ] **Step 4: Implement section TTL and telemetry**

Use each slot's `loaded_at_mono`; remove full reload decisions based on `store.loaded_at`. `reload()` snapshots `loaded_sections()` once and forces exactly those slots. `_evict_store` calls `reload()` and wakes waiters without loading an unused section.

```python
log.info(
    "store_section_load section=%s reason=%s cache_state=%s rows=%d duration_ms=%.1f outcome=%s",
    section, reason, cache_state, max(0, rows), max(0.0, duration_ms), outcome,
)
```

Validate fields against fixed enums. Row counts are lengths for Chat/Frames/World Books/Tokens and `1` for successfully loaded scalar-state sections.

- [ ] **Step 5: Verify and commit**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_store_cache.py tests/test_chat_incremental_sync.py tests/test_asgi_admin.py -q
git add backend/core/store.py tests/test_store_cache.py tests/test_chat_incremental_sync.py
git commit -m "perf(store): refresh only loaded sections"
```

---

### Task 4: Cold Chat writes and clear/load concurrency

**Files:**
- Modify: `backend/core/store.py`
- Modify: `tests/test_chat_incremental_sync.py`
- Modify: `tests/test_store_append_chat_file.py`
- Modify: `tests/test_v2_chat_clear_fence.py`

**Interfaces:**
- Produces `chat_cache_loaded() -> bool`.
- Committed mutations update local rows only after a complete snapshot exists.
- Cold writes still persist, publish change events, and wake waiters without fetching 256 rows.

- [ ] **Step 1: Write cold-write RED tests**

```python
def test_cold_append_persists_and_wakes_without_snapshot(monkeypatch):
    store = core_store.get_store("u-cold")
    snapshots, wakes = [], []
    monkeypatch.setattr(core_store.db, "chat_load_hot_snapshot_strict", lambda *_a: snapshots.append(True))
    monkeypatch.setattr(store, "notify_chat_waiters", lambda: wakes.append(True))
    envelope = {
        "id": "m1", "v": 1, "body_ct": "ciphertext", "nonce": "nonce",
        "K_user": "wrapped-key", "owner_user_id": store.user_id,
    }
    row = store.append_chat("user", "chat", envelope, strict=True)
    assert row["id"] == "m1"
    assert snapshots == [] and wakes == [True]
    assert store.chat_messages == []
    assert store._section_slots[StoreSection.CHAT].status is SectionStatus.UNLOADED
```

Repeat for committed update, finalize, delete, and idempotent append helpers so none creates a partial cache.

- [ ] **Step 2: Write clear/load race RED test**

Block a snapshot after it reads pre-clear rows, call `backend/chat/chat_core.py::clear_history(store, {})`, release the snapshot, and assert durable history and cache stay empty. Assert the generation fence rejects the pre-clear snapshot and an unloaded Store remains unloaded.

- [ ] **Step 3: Verify RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_chat_incremental_sync.py tests/test_store_append_chat_file.py tests/test_v2_chat_clear_fence.py -q
```

- [ ] **Step 4: Implement complete-cache gating**

```python
def chat_cache_loaded(self) -> bool:
    return self._chat_has_complete_snapshot and self._section_slots[StoreSection.CHAT].status in {
        SectionStatus.LOADING, SectionStatus.FRESH, SectionStatus.STALE,
    }
```

Set `_chat_has_complete_snapshot=True` only after a version-consistent snapshot or complete incremental successor. Cold mutations record generation/version dirty hints but leave rows empty. Clear advances generation before an in-flight load can publish and never initiates a load.

- [ ] **Step 5: Verify and commit**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_chat_incremental_sync.py tests/test_store_append_chat_file.py tests/test_v2_chat_clear_fence.py tests/test_chat_response_finalize_cas.py -q
git add backend/core/store.py tests/test_chat_incremental_sync.py tests/test_store_append_chat_file.py tests/test_v2_chat_clear_fence.py
git commit -m "perf(chat): keep cold stores snapshot-free on writes"
```

---

### Task 5: Loaded-only wake dispatch and reconnect catch-up

**Files:**
- Modify: `backend/core/store.py`
- Modify: `backend/core/wake_bus.py`
- Modify: `tests/test_wake_bus.py`
- Modify: `tests/test_chat_poll_cross_worker_staleness.py`

**Interfaces:**
- Produces `note_section_change(section, *, dirty_version=None) -> bool`.
- Unloaded notifications execute zero SQL but Chat waiters still wake.
- Reconnect refreshes only loaded sections and wakes every cached Store's waiters.

- [ ] **Step 1: Write unloaded-notify RED tests**

```python
@pytest.mark.parametrize("channel", ["chat", "frames", "blob"])
def test_notify_does_not_load_unloaded_sections(monkeypatch, channel):
    store = core_store.get_store("u-notify")
    calls = install_counting_loaders(monkeypatch, core_store)
    payload = (
        {"v": 2, "c": "chat", "u": "u-notify", "r": 7}
        if channel == "chat"
        else {"c": channel, "u": "u-notify", "o": "OTHER"}
    )
    wake_bus._dispatch(json.dumps(payload))
    assert calls == []
    assert store.loaded_sections() == frozenset()
```

Assert Chat wake-only and mutation notifications release waiters when Chat is unloaded.

- [ ] **Step 2: Write loaded-only and reconnect RED tests**

Load only `TOKENS`, dispatch `blob`, and assert only Tokens refresh while World Books/Live Activity/Push remain unloaded. Cache one Chat-loaded Store and one shell, run reconnect, assert one Chat refresh and waiter wakes for both.

- [ ] **Step 3: Verify RED**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_wake_bus.py tests/test_chat_poll_cross_worker_staleness.py -q
```

- [ ] **Step 4: Implement loaded-only dispatch**

```python
if not store.chat_cache_loaded():
    store.note_section_change(StoreSection.CHAT, dirty_version=target_version)
    store.notify_chat_waiters()
    return
```

Frames refreshes only loaded `FRAMES`. The broad `blob` channel iterates `WORLD_BOOKS`, `TOKENS`, `LIVE_ACTIVITY`, and `PUSH_STATE`, refreshing only non-`UNLOADED` slots. Reconnect calls `store.reload()` and `_wake_store_waiters(store)` for every cached Store; preserve extra-handler replay.

- [ ] **Step 5: Verify and commit**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_wake_bus.py tests/test_chat_poll_cross_worker_staleness.py tests/test_store_cache.py -q
git add backend/core/store.py backend/core/wake_bus.py tests/test_wake_bus.py tests/test_chat_poll_cross_worker_staleness.py
git commit -m "perf(store): skip unloaded sections on cross-worker wakes"
```

---

### Task 6: Production call-site migration and guard

**Files:**
- Modify: `backend/accounts/accounts_core.py`, `backend/accounts/auth_core.py`
- Modify: `backend/admin/admin_core.py`, `backend/admin/data_track.py`
- Modify: `backend/agent_runtime/spawners.py`, `backend/agent_runtime/supervisor.py`
- Modify: `backend/asgi_app.py`, `backend/genesis/worker.py`, `backend/hosted/runtime_reconciler.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py`, `profile_store.py`, `serve_worker.py`, `worker.py`
- Modify: `backend/perception/service.py`, `backend/screen/ws.py`, `backend/voice/routes_asgi.py`
- Create: `tests/test_store_load_contract.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes `get_store(..., require=...)` and `StoreSection`.
- Produces a reviewed `SHELL_ONLY_GET_STORE_SITES` manifest in the contract test.
- Leaves no production direct `UserStore(...)` outside `backend/core/store.py` and no `get_store_legacy` production calls.

- [ ] **Step 1: Write AST guard RED tests**

```python
def test_no_direct_user_store_construction():
    assert find_calls("UserStore", exclude={"backend/core/store.py"}) == []


def test_get_store_sites_are_explicit_or_reviewed_shell_only():
    implicit = {(s.path, s.lineno) for s in find_get_store_calls() if not s.has_require_keyword}
    assert implicit == set(SHELL_ONLY_GET_STORE_SITES)


def test_no_production_legacy_compatibility_calls():
    assert find_calls("get_store_legacy", roots=["backend"]) == []
```

Store the manifest as `{(path, line): reason}` so line churn forces review. The initial RED inventory should report the direct constructor in `backend/agent_runtime/spawners.py` and all current implicit Store sites.

- [ ] **Step 2: Classify and migrate shell-only sites**

Retain plain `get_store(user_id)` only for identity, locks, waiters, or direct DB/blob helpers. This covers auth objects, trace emitters, runtime-control helpers, memory/proactive settings helpers already backed by DB blobs, envelope creation, and genesis state helpers. Add every retained site and its reason to the manifest.

Replace the production direct constructor:

```python
return bool(web_settings_core.get_settings(core_store.get_store(user_id)).get("effective"))
```

- [ ] **Step 3: Migrate cached-section sites**

```python
store = core_store.get_store(user_id, require={StoreSection.CHAT})          # direct chat_messages scans
store = core_store.get_store(user_id, require={StoreSection.WORLD_BOOKS})  # direct world_books use
store = core_store.get_store(user_id, require={StoreSection.FRAMES})       # direct frames_meta use
store = core_store.get_store(user_id, require={StoreSection.TOKENS, StoreSection.PUSH_STATE})
store = core_store.get_store(user_id, require={StoreSection.LIVE_ACTIVITY})
```

Import `StoreSection` from `core.store_sections`. Production code must never request `ALL_STORE_SECTIONS`.

- [ ] **Step 4: Add high-risk query-budget tests**

Patch `db.chat_load_hot_snapshot_strict` to raise `AssertionError` and cover auth resolution, admin runtime mode, genesis claim/failure, perception settings/wake, Runtime V2 bounded prompt tail, debug trace, profile store, and runtime reconciler. Separately prove the Runtime V2 helper that scans `store.chat_messages` explicitly loads Chat and preserves order/membership.

- [ ] **Step 5: Run migration slice and commit**

```bash
.venv-test/bin/python -m pytest tests/test_store_load_contract.py tests/test_access_modes.py tests/test_agent_runtime_spawners.py tests/test_admin_runtime_mode.py tests/test_genesis_worker.py tests/test_perception.py tests/test_runtime_reconciler.py tests/test_v2_p0_history_safety.py tests/test_voice_context_regressions.py -q
git add backend/accounts backend/admin backend/agent_runtime backend/asgi_app.py backend/genesis backend/hosted/runtime_reconciler.py backend/model_api_runtime/v2 backend/perception/service.py backend/screen/ws.py backend/voice/routes_asgi.py tests/test_store_load_contract.py .github/workflows/ci.yml
git commit -m "refactor(store): declare production section dependencies"
```

---

### Task 7: Deployment controls and documentation

**Files:**
- Modify: `deploy/docker-compose.phala.test.yaml`, `deploy/docker-compose.phala.yaml`
- Modify: `tests/test_deploy_yaml_strict.py`
- Create: `docs/ops/metadata-first-user-store-runbook.md`
- Modify: `docs-site/content/docs/architecture.mdx`
- Modify: `docs-site/content/docs/workflows/chat.mdx`
- Modify: `docs-site/content/docs/changelog.mdx`

**Interfaces:**
- Pins `FEEDLING_STORE_LOAD_MODE` for every service importing `core.store`.
- Documents exact rollout, measurement, and rollback; public API/OpenAPI remains unchanged.

- [ ] **Step 1: Write deploy-config RED tests**

```python
assert environment["FEEDLING_STORE_LOAD_MODE"] == "legacy"
assert "${" not in environment["FEEDLING_STORE_LOAD_MODE"]
assert environment["FEEDLING_CHAT_HOT_CACHE_LIMIT"] == "256"
```

- [ ] **Step 2: Verify RED, then add pinned config**

```bash
.venv-test/bin/python -m pytest tests/test_deploy_yaml_strict.py -q
```

Add `FEEDLING_STORE_LOAD_MODE: "legacy"` beside existing Chat flags in TEST/PROD backend and worker services. Do not add it to DB, enclave, migration-only, or unrelated sidecars.

- [ ] **Step 3: Write runbook and public docs**

The runbook must specify tracked-config promotion `legacy -> selective -> lazy`, immediate tracked rollback to `legacy`, TEST and PROD 1h/6h/24h gates, CloudWatch Network TX/ReadIOPS/CPU, Chat p50/p95/5xx, `store_section_load` Logs Insights counts, `pg_stat_user_tables` deltas, hot-snapshot counts, timestamps/timezone, commit/image IDs, and evidence-table columns.

Record the production phase target explicitly: reduce current Network TX from about `10.10 MB/s` into the `4–6 MB/s` range; this phase does not claim to reach the final `2.64 MB/s` target alone.

Update architecture/workflow/changelog to say worker-local state is metadata-first, bounded history/poll remain database-backed, and NOTIFY is a latency hint over durable PostgreSQL. State that API, encryption, trust boundary, and storage authority do not change; do not regenerate OpenAPI.

- [ ] **Step 4: Verify and commit**

```bash
.venv-test/bin/python -m pytest tests/test_deploy_yaml_strict.py -q
cd docs-site && npm run types:check && npm run lint && npm run build
cd ..
git add deploy/docker-compose.phala.test.yaml deploy/docker-compose.phala.yaml tests/test_deploy_yaml_strict.py docs/ops/metadata-first-user-store-runbook.md docs-site/content/docs/architecture.mdx docs-site/content/docs/workflows/chat.mdx docs-site/content/docs/changelog.mdx
git commit -m "docs(store): add metadata-first rollout controls"
```

---

### Task 8: Full verification, four-worker exercise, and TEST evidence

**Files:**
- Modify: `docs/ops/metadata-first-user-store-runbook.md` only to append observed timestamps, outputs, identifiers, and measurements.

**Interfaces:**
- Produces reproducible local and TEST evidence required before production promotion.

- [ ] **Step 1: Run focused DB-backed suites**

```bash
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests/test_store_sections.py tests/test_store_cache.py tests/test_store_load_contract.py tests/test_chat_incremental_sync.py tests/test_chat_poll_cross_worker_staleness.py tests/test_store_append_chat_file.py tests/test_v2_chat_clear_fence.py tests/test_wake_bus.py -q
```

Expected: pass with no DB-backed skip.

- [ ] **Step 2: Run full repository and docs verification**

```bash
~/fleet/bus/which_tests.sh --vs origin/test
FEEDLING_TEST_PG='postgresql://postgres:test@127.0.0.1:55432/postgres' .venv-test/bin/python -m pytest tests -q --ignore=tests/test_api.py
cd docs-site && npm run types:check && npm run lint && npm run build
```

Any baseline failure must be reproduced by exact test name on `origin/test` before it is classified as unrelated.

- [ ] **Step 3: Run four-worker lazy-mode exercise**

Run the isolated local PostgreSQL/backend stack with gunicorn `-w 4` and `FEEDLING_STORE_LOAD_MODE=lazy`. Exercise append/history/poll/finalize/delete/clear; cross-worker write/read; one dropped NOTIFY; LISTEN reconnect; worker replacement; one first-load DB failure followed by successful retry; and 100 concurrent first Chat consumers. Capture worker PIDs, fixed-enum logs, response status/body hashes, and snapshot counts. Require zero shell-only snapshots and exactly one concurrent first-load snapshot.

- [ ] **Step 4: Commit local evidence**

```bash
git add docs/ops/metadata-first-user-store-runbook.md
git commit -m "test(store): record metadata-first verification evidence"
```

- [ ] **Step 5: Merge to `test` and deploy TEST in legacy mode**

Open/merge only against `test`. Deploy the exact merged commit with `legacy`, record image/commit and same-version smoke/query baseline. Do not target `main` from the feature branch.

- [ ] **Step 6: Promote TEST to selective, then lazy**

For each tracked config commit, repeat Chat send/history/poll, Runtime V2 prompt tail/reply, voice finalize/cancel, perception wake, genesis, world book, push/live activity, admin eviction, reconnect, and worker recycle.

```text
compatibility call sites = 0
Chat correctness mismatches = 0
clear resurrection = 0
new 5xx = 0
p95 regression <= 10%
Chat hot snapshots <= 10% of legacy baseline
chat_messages tuple fetch <= 20% of legacy baseline
```

- [ ] **Step 7: Commit TEST evidence and stop before production**

```bash
git add docs/ops/metadata-first-user-store-runbook.md
git commit -m "test(store): record test rollout evidence"
```

Hand off the tested `test` state for maintainer-controlled production promotion. PROD must deploy the same artifact in `legacy`, then use reviewed config commits for `selective` and `lazy`, with 1h/6h/24h gates and immediate rollback on any correctness issue.

---

### Task 9: Maintainer-controlled production rollout and 24-hour acceptance

**Files:**
- Modify: `docs/ops/metadata-first-user-store-runbook.md` only to append production evidence.

**Interfaces:**
- Consumes the exact artifact validated in TEST and a maintainer-approved `test`/`pre` to `main` promotion.
- Produces the final 1h/6h/24h correctness, latency, query, and Network TX measurements.

- [ ] **Step 1: Establish a same-version legacy baseline**

After the maintainer-approved production deployment, keep `FEEDLING_STORE_LOAD_MODE=legacy`. Record exact commit/image, worker count, 1-hour Chat hot-snapshot count/rows, `chat_messages` tuple fetch delta, Network TX, ReadIOPS, CPU, key-path p50/p95/5xx, strict snapshot errors, and wake-bus errors.

- [ ] **Step 2: Promote production to selective**

Use a reviewed tracked-config commit from `test` or `pre`; do not edit a running container. At 1h, 6h, and 24h compare the same metrics and run Chat send/history/poll, Runtime V2 reply, voice finalize, perception wake, and clear-history smoke tests. Immediately deploy the tracked `legacy` rollback if any result is missing, duplicated, reordered, resurrected after clear, or if new 5xx appears.

- [ ] **Step 3: Promote production to lazy**

Only after selective meets all gates, promote with another reviewed tracked-config commit and repeat the 1h/6h/24h observation. Require:

```text
Chat hot snapshots <= 10% of same-version legacy
chat_messages tuple fetch <= 20% of same-version legacy
key-path p95 regression <= 10%
correctness mismatches = 0
new Store/DB/wake errors = 0
RDS Network TX target = 4–6 MB/s from the prior ~10.10 MB/s average
```

If correctness is stable but Network TX remains above 6 MB/s, keep the measured result and open a separate design for the next attributed table; do not add Memory or user-log work to this change.

- [ ] **Step 4: Commit the 24-hour evidence**

```bash
git add docs/ops/metadata-first-user-store-runbook.md
git commit -m "ops(store): record production metadata-first acceptance"
```

The evidence must also record the remaining distance to the final whole-RDS target of `2.64 MB/s`. This task is complete only after the full 24-hour lazy window passes or a rollback is documented with its exact trigger and follow-up decision.
