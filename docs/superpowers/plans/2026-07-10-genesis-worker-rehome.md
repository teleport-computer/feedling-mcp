# Genesis Worker Rehome — Implementation Plan

> **RETIRED / DO NOT DEPLOY.** Historical implementation record; Genesis now
> runs with the pooled Runtime V2 worker, not a hosted resident supervisor.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the genesis import worker out of the `agent-runner` container (where it is a parasitic daemon thread inside `supervisor.serve()`) into the V2 `serve-worker` process, and give it a heartbeat so its death stops being silent.

**Architecture:** Extract the gate + loop from `agent_runtime/supervisor.py` into a new framework-neutral `backend/genesis/daemon.py`. `serve_worker.py` (the V2 assembly layer) starts it on a **dedicated `threading.Thread`** — NOT `asyncio.to_thread`, because a genesis tick blocks for minutes on LLM calls and would starve the event loop's shared default executor that all four V2 loops bridge their sync DB calls through. Liveness reuses `v2_worker_heartbeats` with a new `kind` discriminator column.

**Tech Stack:** Python, psycopg, alembic, pytest. Postgres on `127.0.0.1:55432` (postgres/test).

## Global Constraints

- **NO-COMMIT mode.** Never run `git commit`, `git add`, `git stash`, `git checkout --`, `git reset`, or `git clean`. Leave all work in the working tree.
- Work only in the worktree `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2`. Never touch the main checkout.
- **BYOK-only hard invariant:** every LLM call uses the user's own JIT-decrypted provider key. No platform LLM key fallback anywhere.
- **Dependency direction** (AST-guarded by `tests/test_v2_dependency_direction.py`): `backend/model_api_runtime/v2/*` and `backend/capabilities/*` must NOT import `hosted` or `agent_runtime`. Only `serve_worker.py` (assembly layer) may. `backend/genesis/*` is neither, so importing `genesis.daemon` from `serve_worker.py` is fine — but `genesis/daemon.py` itself must NOT import `agent_runtime` or `model_api_runtime`.
- Full suite command (a bare `pytest tests/` aborts at collection):
  ```
  python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
  ```
  Baseline before this plan: **3043 passed / 8 pre-existing failures**. Any NEW failure is a regression.
- Migration head before this plan is `0019_v2_screen_watch`. New migration must set `down_revision = "0019_v2_screen_watch"`.

---

### Task 1: `kind` discriminator on `v2_worker_heartbeats`

**Why first:** `v2_worker_heartbeats` is not an observability table — it is the chat/send admission gate. `workers_alive()` (`EXISTS`) backs the 503 `workers_unavailable` guard, and `live_worker_count()` (`count(*)`) feeds `admission.estimate_wait_sec(workers=...)` at `backend/hosted/chat_send_core.py:113`. Writing a genesis row into it un-filtered would make `live_worker_count()` return 2N for N processes, halving the estimated queue wait and **over-admitting users onto turn slots that do not exist**. The column must land before anything writes a genesis row.

**Files:**
- Create: `backend/alembic/versions/0020_v2_heartbeat_kind.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py:345-379`
- Test: `tests/test_v2_jobs_store.py`

**Interfaces:**
- Consumes: existing `v2_worker_heartbeats(worker_id TEXT PRIMARY KEY, beat_at TIMESTAMPTZ)` from `0015_v2_worker_heartbeats`.
- Produces:
  - `jobs_store.record_worker_heartbeat(worker_id: str, *, kind: str = "turn") -> None`
  - `jobs_store.workers_alive(*, within_sec: int = 30) -> bool` — now filters `kind = 'turn'`
  - `jobs_store.live_worker_count(*, within_sec: int = 30) -> int` — now filters `kind = 'turn'`
  - `jobs_store.genesis_worker_alive(*, within_sec: int = 60) -> bool` — filters `kind = 'genesis'`

- [ ] **Step 1: Write the failing regression test**

The load-bearing test is the one that proves a genesis heartbeat does NOT inflate the admission inputs. Add to `tests/test_v2_jobs_store.py`, matching the file's existing fixture/style:

```python
def test_genesis_heartbeat_does_not_inflate_turn_worker_liveness():
    """A genesis heartbeat row must be invisible to the chat/send admission gate.

    live_worker_count() feeds admission.estimate_wait_sec(workers=...); counting a
    genesis row as a turn worker would halve the estimated queue wait for a
    single-process pool and over-admit onto turn slots that do not exist.
    """
    jobs_store.record_worker_heartbeat("w1")                      # default kind='turn'
    jobs_store.record_worker_heartbeat("w1:genesis", kind="genesis")

    assert jobs_store.live_worker_count() == 1
    assert jobs_store.workers_alive() is True
    assert jobs_store.genesis_worker_alive() is True


def test_genesis_heartbeat_alone_does_not_open_the_send_gate():
    """Genesis alive but every turn worker dead => send must still 503."""
    jobs_store.record_worker_heartbeat("only:genesis", kind="genesis")

    assert jobs_store.workers_alive() is False
    assert jobs_store.live_worker_count() == 0
    assert jobs_store.genesis_worker_alive() is True


def test_genesis_worker_alive_false_when_nothing_beats():
    assert jobs_store.genesis_worker_alive() is False
```

- [ ] **Step 2: Run to verify it fails**

```
python -m pytest tests/test_v2_jobs_store.py -q -k genesis
```
Expected: FAIL — `record_worker_heartbeat() got an unexpected keyword argument 'kind'`.

- [ ] **Step 3: Write the migration**

Create `backend/alembic/versions/0020_v2_heartbeat_kind.py`:

```python
"""Discriminate turn workers from the genesis import worker on the liveness table.

`v2_worker_heartbeats` is the chat/send admission gate, not just observability:
`workers_alive()` backs the 503 `workers_unavailable` guard and
`live_worker_count()` feeds `admission.estimate_wait_sec(workers=...)`. Once the
genesis import worker moved into the serve_worker process (it needs its own
liveness row — its thread can die while the turn loops keep beating), an
un-discriminated row would count as a turn worker and halve the estimated queue
wait. Both readers now filter `kind = 'turn'`; genesis reads `kind = 'genesis'`.

Existing rows are turn workers, hence the DEFAULT.

Revision ID: 0020_v2_heartbeat_kind
"""
from alembic import op

revision = "0020_v2_heartbeat_kind"
down_revision = "0019_v2_screen_watch"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE v2_worker_heartbeats
  ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'turn';
CREATE INDEX IF NOT EXISTS ix_v2_worker_heartbeats_kind_beat
  ON v2_worker_heartbeats (kind, beat_at DESC);
"""

_DOWN = """
DROP INDEX IF EXISTS ix_v2_worker_heartbeats_kind_beat;
ALTER TABLE v2_worker_heartbeats DROP COLUMN IF EXISTS kind;
"""


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
```

- [ ] **Step 4: Check `db.init_schema()` also creates the column**

`serve_worker.main()` calls `db.init_schema()` (it is a standalone entrypoint with no shared migration hook — see `serve_worker.py:869-874`). Grep `backend/db.py` for how `v2_worker_heartbeats` is created there (`init_schema` mirrors the alembic DDL). If it creates the table, add the `kind` column to that DDL too — otherwise a fresh worker CVM boots with a table that has no `kind` column and every read 500s. If `init_schema` does not create this table, note that in your report and skip.

- [ ] **Step 5: Implement the jobs_store changes**

Replace `jobs_store.py:345-379`:

```python
def record_worker_heartbeat(worker_id: str, *, kind: str = "turn") -> None:
    """UPSERT this process's liveness row (turn loops every ~10s via
    serve_worker._heartbeat_loop; the genesis thread every tick with
    kind='genesis').

    ``kind`` is load-bearing, not a label: workers_alive()/live_worker_count()
    read ONLY kind='turn' because they gate chat/send admission. A genesis row
    counted as a turn worker would halve the estimated queue wait.
    """
    with _pool().connection() as conn:
        conn.execute(
            "INSERT INTO v2_worker_heartbeats (worker_id, beat_at, kind) "
            "VALUES (%s, now(), %s) "
            "ON CONFLICT (worker_id) DO UPDATE SET beat_at = now(), kind = EXCLUDED.kind",
            (str(worker_id), str(kind)),
        )


def workers_alive(*, within_sec: int = 30) -> bool:
    """True iff at least one serve_worker TURN process has recorded a heartbeat
    within the last ``within_sec`` seconds. Used by the chat/send v2 liveness
    guard. Genesis heartbeats are deliberately invisible here — a live genesis
    thread says nothing about whether any turn slot exists to drain the job."""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM v2_worker_heartbeats "
                "WHERE kind = 'turn' AND beat_at > now() - make_interval(secs => %s))",
                (int(within_sec),),
            )
            return bool(cur.fetchone()[0])


def live_worker_count(*, within_sec: int = 30) -> int:
    """窗口内有心跳的 serve_worker TURN 进程数（workers_alive 的计数版，喂 admission
    ceiling）。genesis 心跳不计入——它不占 turn 槽位。"""
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM v2_worker_heartbeats "
                "WHERE kind = 'turn' AND beat_at > now() - make_interval(secs => %s)",
                (int(within_sec),),
            )
            return int(cur.fetchone()[0])


def genesis_worker_alive(*, within_sec: int = 60) -> bool:
    """True iff the genesis import worker thread has beaten recently.

    Window defaults to 60s (not 30s): a genesis tick holds the thread for the
    whole LLM reduce, and the heartbeat is written once per tick, so the gap
    between beats is the tick interval (default 10s) PLUS the last job's
    duration. Purely observational — nothing gates on this.
    """
    with _pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM v2_worker_heartbeats "
                "WHERE kind = 'genesis' AND beat_at > now() - make_interval(secs => %s))",
                (int(within_sec),),
            )
            return bool(cur.fetchone()[0])
```

- [ ] **Step 6: Run the tests**

```
python -m pytest tests/test_v2_jobs_store.py -q
python -m pytest tests/test_chat_send_v2_enqueue.py tests/test_v2_worker.py -q
```
Expected: PASS. The second command guards the readers you just changed.

- [ ] **Step 7: Mutation-verify the guard**

Temporarily drop `kind = 'turn' AND` from `live_worker_count`, re-run
`pytest tests/test_v2_jobs_store.py -q -k genesis`. It MUST fail on
`test_genesis_heartbeat_does_not_inflate_turn_worker_liveness`. Restore.
Report the observed failure output — a regression test that cannot fail is not a test.

---

### Task 2: Extract `genesis/daemon.py` out of `supervisor.py`

**Files:**
- Create: `backend/genesis/daemon.py`
- Modify: `backend/agent_runtime/supervisor.py` (remove `_genesis_worker_should_start` :1129-1136, `_genesis_worker_loop` :1139-1153, the start block :1335-1361, and the now-dead `genesis_stop` / `_GENESIS_*` constants + any now-unused imports)
- Modify: `tests/test_agent_runtime_genesis_gate.py` → the gate tests move to the new module
- Test: `tests/test_genesis_daemon.py` (new)

**Interfaces:**
- Consumes: `genesis.worker.tick`, `genesis.worker.reap_stale_processing_jobs` (unchanged).
- Produces:
  ```python
  genesis.daemon.should_start(*, enabled: str, secret: str, enclave_url: str) -> bool
  genesis.daemon.run_loop(*, api_url: str, enclave_url: str, mint_genesis: Callable,
                          interval: float, stop_event, on_beat: Callable[[], None] | None = None) -> None
  ```
  `stop_event` is anything with `.is_set()` / `.wait(timeout)` (a `threading.Event`).
  `on_beat` is called once per tick, **before** the tick runs and again after it
  returns, wrapped so a beat failure can never kill the loop. Default `None` = no beat
  (keeps the function pure for tests).

**Rationale for the module home:** `genesis/` is neither `hosted` nor `agent_runtime` nor `model_api_runtime`, so both `supervisor.py` and `serve_worker.py` may import it without violating the AST dependency guard. `genesis/daemon.py` must import NEITHER of those packages.

- [ ] **Step 1: Read the two functions being moved**

`backend/agent_runtime/supervisor.py:1129-1153`. Move them verbatim except:
- rename `_genesis_worker_should_start` → `should_start`, `_genesis_worker_loop` → `run_loop`
- `run_loop` gains the keyword-only `on_beat: Callable[[], None] | None = None`
- `_truthy` is currently a supervisor helper. Copy the minimal implementation into
  `genesis/daemon.py` (do NOT import it from `agent_runtime`). Check what
  `supervisor._truthy` actually does before copying — match it exactly.
- `log` → use `print(...)` or a module-level `logging.getLogger(__name__)`; match
  what the rest of `backend/genesis/*` already does (grep first — `worker.py` uses
  bare `print(f"[genesis:reaper] ...")`).

- [ ] **Step 2: Write `backend/genesis/daemon.py`**

```python
"""Genesis import worker daemon: the gate + the polling loop.

Extracted from ``agent_runtime.supervisor`` (2026-07-10). It lived there for one
reason — supervisor was the only long-running non-request process inside a CVM,
so it was the only place with a ``while True`` to hang a poller on. It never had
anything to do with the resident CLI runtime: it needs an enclave URL (to decrypt
E2E chunk envelopes), a runtime-token secret (to mint genesis-scoped tokens), and
a loop. Nothing else.

Framework-neutral on purpose: this module must import NEITHER ``agent_runtime``
nor ``model_api_runtime``, so the V2 assembly layer (``serve_worker.py``) and the
resident supervisor can both host it without violating the dependency-direction
guard in ``tests/test_v2_dependency_direction.py``.

Concurrency contract: the claim is ``FOR UPDATE SKIP LOCKED``
(``genesis.worker.tick`` -> ``db.genesis_claim_uploaded_jobs``), so running this
loop in several processes at once is safe and de-dupes. That is what makes a
coexistence rollout possible.
"""
from __future__ import annotations

import logging
from typing import Callable

log = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(value: str) -> bool:
    return str(value or "").strip().lower() in _TRUTHY


def should_start(*, enabled: str, secret: str, enclave_url: str) -> bool:
    """Activate the genesis worker only when explicitly enabled AND its
    prerequisites are present (runtime-token secret to mint scoped tokens, enclave
    URL to decrypt chunks). Default OFF — landing the hook must not run genesis
    until the env opts in; missing prereqs stay dormant rather than fail jobs."""
    if not _truthy(enabled):
        return False
    return bool(str(secret or "").strip()) and bool(str(enclave_url or "").strip())


def _beat(on_beat: Callable[[], None] | None) -> None:
    """A liveness-write failure must never kill the loop it is observing."""
    if on_beat is None:
        return
    try:
        on_beat()
    except Exception as e:  # noqa: BLE001
        log.warning("genesis heartbeat write failed: %s", e)


def run_loop(*, api_url, enclave_url, mint_genesis, interval, stop_event, on_beat=None) -> None:
    """Poll ``genesis.worker.tick``. Blocking — the caller owns the thread.

    Beats before AND after each tick: a tick can block for minutes on the user's
    LLM, and a beat only at the top of the loop would look dead for that whole
    window. Reaps jobs wedged in 'processing' before claiming new work, so a
    crashed job cannot block the user's onboarding forever.
    """
    from genesis import worker as genesis_worker
    while not stop_event.is_set():
        _beat(on_beat)
        try:
            genesis_worker.reap_stale_processing_jobs()
            genesis_worker.tick(api_url=api_url, enclave_url=enclave_url,
                                mint_runtime_token=mint_genesis, max_jobs=1)
        except Exception as e:  # noqa: BLE001
            log.exception("genesis worker tick failed: %s", e)
        _beat(on_beat)
        stop_event.wait(interval)
```

Note the deliberate `from genesis import worker` INSIDE `run_loop` — preserved from
the original. It keeps import-time cost (and any import error) out of module load.
**But that also means an ImportError kills the thread silently** — which is exactly
the failure mode the heartbeat now makes visible. Do not hoist it.

- [ ] **Step 3: Write `tests/test_genesis_daemon.py`**

```python
import threading

import pytest

from genesis import daemon


@pytest.mark.parametrize("enabled,secret,url,expected", [
    ("1", "s", "https://e", True),
    ("true", "s", "https://e", True),
    ("", "s", "https://e", False),
    ("0", "s", "https://e", False),
    ("1", "", "https://e", False),
    ("1", "s", "", False),
    ("1", "   ", "https://e", False),
])
def test_should_start(enabled, secret, url, expected):
    assert daemon.should_start(enabled=enabled, secret=secret, enclave_url=url) is expected


def test_run_loop_beats_before_and_after_each_tick(monkeypatch):
    beats = []
    calls = []

    class _FakeWorker:
        @staticmethod
        def reap_stale_processing_jobs():
            calls.append("reap")

        @staticmethod
        def tick(**kw):
            calls.append("tick")

    monkeypatch.setitem(__import__("sys").modules, "genesis.worker", _FakeWorker)

    stop = threading.Event()

    def _beat():
        beats.append(len(calls))
        if len(beats) >= 2:
            stop.set()

    daemon.run_loop(api_url="a", enclave_url="e", mint_genesis=lambda *a, **k: "t",
                    interval=0, stop_event=stop, on_beat=_beat)

    assert calls == ["reap", "tick"]
    assert beats == [0, 2]     # beat before the tick (0 calls) and after (2 calls)


def test_run_loop_survives_a_beat_failure(monkeypatch):
    """A liveness-write failure must not kill the loop it observes."""
    ticks = []

    class _FakeWorker:
        @staticmethod
        def reap_stale_processing_jobs():
            pass

        @staticmethod
        def tick(**kw):
            ticks.append(1)

    monkeypatch.setitem(__import__("sys").modules, "genesis.worker", _FakeWorker)
    stop = threading.Event()

    def _boom():
        if len(ticks) >= 1:
            stop.set()
        raise RuntimeError("db down")

    daemon.run_loop(api_url="a", enclave_url="e", mint_genesis=lambda *a, **k: "t",
                    interval=0, stop_event=stop, on_beat=_boom)

    assert ticks == [1]


def test_run_loop_survives_a_tick_failure(monkeypatch):
    beats = []

    class _FakeWorker:
        @staticmethod
        def reap_stale_processing_jobs():
            raise RuntimeError("pg gone")

        @staticmethod
        def tick(**kw):
            raise AssertionError("must not be reached")

    monkeypatch.setitem(__import__("sys").modules, "genesis.worker", _FakeWorker)
    stop = threading.Event()

    def _beat():
        beats.append(1)
        if len(beats) >= 2:
            stop.set()

    daemon.run_loop(api_url="a", enclave_url="e", mint_genesis=lambda *a, **k: "t",
                    interval=0, stop_event=stop, on_beat=_beat)

    assert len(beats) == 2   # beat-before + beat-after still ran despite the raise
```

The `monkeypatch.setitem(sys.modules, ...)` trick is required because `run_loop`
imports `genesis.worker` lazily inside the function body. Verify this actually
intercepts it; if `from genesis import worker` resolves the attribute off the
`genesis` package rather than `sys.modules`, patch `genesis.worker` with
`monkeypatch.setattr` instead. **Run the test and confirm it genuinely exercises the
fake** (e.g. by asserting `calls`), do not assume.

- [ ] **Step 4: Run it**

```
python -m pytest tests/test_genesis_daemon.py -q
```
Expected: PASS.

- [ ] **Step 5: Gut the supervisor call site**

In `backend/agent_runtime/supervisor.py`, delete `_genesis_worker_should_start`,
`_genesis_worker_loop`, the `genesis_enabled` / `genesis_stop` block at :1335-1361,
and any `_GENESIS_TOKEN_TTL_DEFAULT_SEC` / `_GENESIS_TICK_DEFAULT_SEC` constants that
become unreferenced. Grep for every symbol you delete before deleting it. Do NOT
leave a shim — the whole point is that `agent-runner` no longer hosts genesis.

If `runtime_token` / `threading` imports become unused, remove them; if they are still
used elsewhere in the file, leave them.

- [ ] **Step 6: Retarget the old gate test**

`tests/test_agent_runtime_genesis_gate.py` tests `supervisor._genesis_worker_should_start`,
which no longer exists. Its cases are now covered by `test_genesis_daemon.py::test_should_start`.
**Delete `tests/test_agent_runtime_genesis_gate.py`** and say so explicitly in your report —
first confirm every case it covers has an equivalent in the new parametrize list, and
report any case that does NOT (add it rather than dropping it).

- [ ] **Step 7: Full suite**

```
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```
Expected: 8 pre-existing failures, zero new. Report the exact counts.

---

### Task 3: Host the genesis daemon in `serve_worker.py`

**Files:**
- Modify: `backend/model_api_runtime/v2/serve_worker.py`
- Test: `tests/test_v2_serve_worker_genesis.py` (new)

**Interfaces:**
- Consumes: `genesis.daemon.should_start` / `genesis.daemon.run_loop` (Task 2),
  `jobs_store.record_worker_heartbeat(..., kind="genesis")` (Task 1),
  `core.runtime_token.mint` (already imported as `runtime_token`).
- Produces:
  ```python
  serve_worker._GENESIS_TOKEN_SCOPE: list[str]            # ["envelope_decrypt", "genesis"]
  serve_worker._mint_genesis_token(user_id, scopes=None) -> str
  serve_worker._start_genesis_thread(worker_id: str) -> tuple[threading.Thread, threading.Event] | None
  ```
  Returns `None` when the gate says dormant.

**Two constraints that are load-bearing:**

1. **Dedicated `threading.Thread`, NOT `asyncio.to_thread`.** Every other sync bridge in
   this file (`_reaper_loop`, `_heartbeat_loop`, `_scheduler_loop`, and the per-turn DB
   calls) goes through `asyncio.to_thread`, which uses the loop's *default* executor —
   `min(32, cpu_count + 4)` threads, so **6 on a 2-core CVM**. A genesis tick holds its
   thread for the entire LLM reduce (minutes). Parking that in the shared executor
   alongside `MAX_WORKERS` turn coroutines' DB calls risks starving them. Give genesis
   its own thread. This mirrors the original supervisor decision ("never inline in the
   supervisor tick loop") for the same reason, at higher stakes.

2. **The token scope is different.** `_RUNTIME_TOKEN_SCOPE` (`serve_worker.py:88`) is
   `["envelope_decrypt"]`. Genesis needs `["envelope_decrypt", "genesis"]` and a much
   longer TTL (imports outlive the 900s chat TTL). Do NOT widen `_RUNTIME_TOKEN_SCOPE` —
   the chat path must keep the narrower scope. Add a separate minter.

- [ ] **Step 1: Write the failing test**

Create `tests/test_v2_serve_worker_genesis.py`:

```python
import threading

import pytest

from model_api_runtime.v2 import serve_worker


def test_genesis_thread_dormant_without_env(monkeypatch):
    monkeypatch.delenv("FEEDLING_GENESIS_WORKER_ENABLED", raising=False)
    assert serve_worker._start_genesis_thread("w1") is None


def test_genesis_thread_dormant_when_prereqs_missing(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_WORKER_ENABLED", "1")
    monkeypatch.delenv("FEEDLING_RUNTIME_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave")
    assert serve_worker._start_genesis_thread("w1") is None


def test_genesis_thread_starts_and_stops(monkeypatch):
    monkeypatch.setenv("FEEDLING_GENESIS_WORKER_ENABLED", "1")
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "s3cret")
    monkeypatch.setenv("FEEDLING_ENCLAVE_URL", "https://enclave")
    monkeypatch.setenv("FEEDLING_API_URL", "https://api")
    monkeypatch.setenv("FEEDLING_GENESIS_WORKER_INTERVAL_SEC", "0")

    ran = threading.Event()
    beats: list[tuple] = []

    def _fake_run_loop(*, api_url, enclave_url, mint_genesis, interval, stop_event, on_beat=None):
        assert api_url == "https://api"
        assert enclave_url == "https://enclave"
        if on_beat:
            on_beat()
        ran.set()
        stop_event.wait(5)

    monkeypatch.setattr(serve_worker.genesis_daemon, "run_loop", _fake_run_loop)
    monkeypatch.setattr(serve_worker.jobs_store, "record_worker_heartbeat",
                        lambda wid, **kw: beats.append((wid, kw.get("kind"))))

    started = serve_worker._start_genesis_thread("w1")
    assert started is not None
    thread, stop = started
    assert ran.wait(5)
    assert beats == [("w1:genesis", "genesis")]

    stop.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_genesis_token_scope_is_wider_than_chat_scope():
    """The chat path must NOT get the genesis scope; genesis must have both."""
    assert serve_worker._RUNTIME_TOKEN_SCOPE == ["envelope_decrypt"]
    assert set(serve_worker._GENESIS_TOKEN_SCOPE) == {"envelope_decrypt", "genesis"}


def test_mint_genesis_token_carries_both_scopes(monkeypatch):
    monkeypatch.setenv("FEEDLING_RUNTIME_TOKEN_SECRET", "s3cret")
    from core import runtime_token
    token = serve_worker._mint_genesis_token("usr_1")
    claims = runtime_token.verify(b"s3cret", token)
    assert claims["user_id"] == "usr_1"
    assert set(claims["scope"]) == {"envelope_decrypt", "genesis"}
```

- [ ] **Step 2: Run to verify it fails**

```
python -m pytest tests/test_v2_serve_worker_genesis.py -q
```
Expected: FAIL — `AttributeError: module ... has no attribute '_start_genesis_thread'`.

- [ ] **Step 3: Implement in `serve_worker.py`**

Add near the other imports (this file is the assembly layer — it may import `genesis`):

```python
from genesis import daemon as genesis_daemon
```

Add beside `_RUNTIME_TOKEN_SCOPE` (`:88`):

```python
# Genesis mints its own token: a wider scope (it decrypts chunk envelopes AND calls
# the genesis apply route) and a much longer TTL — a history import routinely
# outlives the 900s chat token. Deliberately NOT folded into _RUNTIME_TOKEN_SCOPE:
# the per-turn chat path must keep the narrower scope.
_GENESIS_TOKEN_SCOPE = ["envelope_decrypt", "genesis"]
_GENESIS_TOKEN_TTL_SEC = float(os.environ.get("FEEDLING_GENESIS_RUNTIME_TOKEN_TTL_SEC", "7200"))
_GENESIS_INTERVAL_SEC = float(os.environ.get("FEEDLING_GENESIS_WORKER_INTERVAL_SEC", "10"))
```

Read `_GENESIS_TOKEN_TTL_SEC` / `_GENESIS_INTERVAL_SEC` **inside** `_start_genesis_thread`
rather than at module import if the tests above need `monkeypatch.setenv` to take effect —
check which the test requires and pick accordingly. State your choice in the report.

Then, near `_mint_runtime_token` (`:174`):

```python
def _mint_genesis_token(user_id: str, scopes: list[str] | None = None) -> str:
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip().encode("utf-8")
    if not secret:
        raise RuntimeError("FEEDLING_RUNTIME_TOKEN_SECRET not set")
    return runtime_token.mint(
        secret,
        user_id=user_id,
        runtime_instance_id="v2-genesis",
        scope=list(scopes or _GENESIS_TOKEN_SCOPE),
        ttl=float(os.environ.get("FEEDLING_GENESIS_RUNTIME_TOKEN_TTL_SEC", "7200")),
    )
```

(`runtime_instance_id` is a free-form label — `core/runtime_token.authorize` checks only
`user_id` + `scope`, never `sub`. `serve_worker` already mints with the made-up
`"v2-worker"` at `:181`.)

Then, beside the other loops:

```python
def _start_genesis_thread(worker_id: str):
    """Start the genesis import worker on a DEDICATED thread, or return None if dormant.

    Rehomed here from `agent_runtime.supervisor` (2026-07-10): genesis never depended
    on the resident CLI runtime — it needed an enclave URL, a token secret, and a
    long-running loop, and supervisor merely happened to be the only process in the
    CVM that had one. Deleting agent-runner would have silently stopped draining
    `genesis_import_jobs`, stalling every new user's onboarding distillation with no
    error surfaced anywhere.

    NOT `asyncio.to_thread`: a tick blocks for the whole LLM reduce (minutes), and
    `to_thread` would park that in the loop's default executor — `min(32, cpu+4)`, i.e.
    6 threads on a 2-core CVM — which every turn coroutine's sync DB call also bridges
    through. Its own thread cannot starve them.

    The heartbeat (kind='genesis', invisible to `workers_alive`/`live_worker_count`)
    exists because this thread can die while the process lives on: `run_loop` imports
    `genesis.worker` lazily, so an ImportError kills only the thread and the turn loops
    keep beating happily. Today that failure is completely silent.
    """
    enabled = os.environ.get("FEEDLING_GENESIS_WORKER_ENABLED", "")
    secret = os.environ.get("FEEDLING_RUNTIME_TOKEN_SECRET", "").strip()
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").strip()
    api_url = os.environ.get("FEEDLING_API_URL", "").strip()

    if not genesis_daemon.should_start(enabled=enabled, secret=secret, enclave_url=enclave_url):
        if enabled:
            log.warning("[v2.serve_worker] FEEDLING_GENESIS_WORKER_ENABLED set but "
                        "prerequisites missing (need FEEDLING_RUNTIME_TOKEN_SECRET + "
                        "FEEDLING_ENCLAVE_URL) — genesis worker dormant")
        return None

    genesis_worker_id = f"{worker_id}:genesis"
    stop_event = threading.Event()
    interval = float(os.environ.get("FEEDLING_GENESIS_WORKER_INTERVAL_SEC", "10"))

    def _beat() -> None:
        jobs_store.record_worker_heartbeat(genesis_worker_id, kind="genesis")

    thread = threading.Thread(
        target=genesis_daemon.run_loop, daemon=True, name="v2-genesis",
        kwargs={"api_url": api_url, "enclave_url": enclave_url,
                "mint_genesis": _mint_genesis_token, "interval": interval,
                "stop_event": stop_event, "on_beat": _beat},
    )
    thread.start()
    log.info("[v2.serve_worker] genesis worker enabled — interval=%.0fs worker_id=%s",
             interval, genesis_worker_id)
    return thread, stop_event
```

Add `import threading` to the imports if absent.

- [ ] **Step 4: Wire it into `_serve()` shutdown**

In `_serve()` (`:837`), start the thread before the `asyncio.gather` and stop it after:

```python
    genesis = _start_genesis_thread(worker_id)
    ...
    await asyncio.gather(...)
    if genesis is not None:
        genesis_thread, genesis_stop = genesis
        genesis_stop.set()
        # Bounded: a tick in flight finishes its current LLM call. daemon=True means
        # a wedged tick can never block process exit.
        await asyncio.to_thread(genesis_thread.join, 10.0)
    log.info("[v2.serve_worker] drained; exiting worker=%s", worker_id)
```

- [ ] **Step 5: Run the tests**

```
python -m pytest tests/test_v2_serve_worker_genesis.py -q
python -m pytest tests/test_v2_dependency_direction.py -q
```
Both must PASS. The second proves `serve_worker` importing `genesis` does not break
the layering guard (`serve_worker.py` is in `_EXEMPT`; `genesis/daemon.py` is not in
the scanned dirs — confirm and say so).

- [ ] **Step 6: Full suite**

```
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```
Expected: 8 pre-existing failures, zero new. Report the exact counts.

---

### Task 4: Compose env move + admin metric + docs

**Files:**
- Modify: `deploy/docker-compose.phala.prod.runner.yaml` (move the three `FEEDLING_GENESIS_*` keys from `agent-runner` `:87-89` to `serve-worker` `:105-118`; fix the now-false comment at `:96` that says "no genesis worker")
- Modify: `deploy/docker-compose.phala.runner.yaml` (same move; note the test compose uses a YAML anchor shared by two `agent-runner` containers — `:79-81`)
- Modify: `backend/admin/admin_core.py:151-156` (`v2_metrics()` gains `"genesis_alive": jobs_store.genesis_worker_alive()`)
- Modify: `docs/HOSTED_RUNTIME_V2_PARITY_MATRIX.md` (§F bucket 4 → empty; §D genesis row → rehomed)
- Modify: `deploy/HOSTED_RUNTIME_V2_ROLLOUT.md` (Step 0 gains the genesis verification; delete the stale "capture lane ... follow-up" Deferred bullet — capture shipped)
- Modify: `deploy/DEPLOYMENTS.md:102-103` (runner CVM rows: genesis now lives in `serve-worker`)
- Test: `tests/test_admin_runtime_mode.py` or wherever `v2_metrics` is covered — grep first.

- [ ] **Step 1: Grep for the existing `v2_metrics` test**

```
grep -rn "v2_metrics\|v2-metrics" tests/
```
Add a case asserting `genesis_alive` is present and is a bool.

- [ ] **Step 2: Move the compose env**

In BOTH runner composes, cut:
```yaml
      FEEDLING_GENESIS_WORKER_ENABLED: "1"
      FEEDLING_GENESIS_WORKER_INTERVAL_SEC: "${FEEDLING_GENESIS_WORKER_INTERVAL_SEC:-10}"
      FEEDLING_GENESIS_RUNTIME_TOKEN_TTL_SEC: "${FEEDLING_GENESIS_RUNTIME_TOKEN_TTL_SEC:-7200}"
```
from the `agent-runner` environment block and paste into `serve-worker`'s, with this comment:

```yaml
      # Genesis import worker — rehomed here 2026-07-10. It was a daemon thread inside
      # agent-runner's supervisor for one accidental reason: that was the only
      # long-running process in the CVM. It has no dependency on the resident CLI
      # runtime. `genesis_import_jobs` has exactly ONE drain in the codebase
      # (genesis/worker.py `genesis_claim_uploaded_jobs`), so deleting agent-runner
      # while genesis lived there would have silently stalled every new user's
      # onboarding distillation. Claim is FOR UPDATE SKIP LOCKED — safe on N replicas.
      # Liveness: v2_worker_heartbeats kind='genesis' (invisible to the chat/send
      # admission gate, which reads kind='turn'); surfaced as `genesis_alive` on
      # GET /v1/admin/v2-metrics.
```

**Careful:** in `docker-compose.phala.runner.yaml` those three keys sit in a YAML anchor
(around `:79`) that is merged into two `agent-runner` containers. Read the file and
understand the anchor before editing — do not blindly delete lines. If `serve-worker`
does not consume that anchor, add the keys to its own `environment:` block explicitly.

- [ ] **Step 3: Fix the false comment**

`deploy/docker-compose.phala.prod.runner.yaml:96` currently says the serve-worker has
"no per-user home, no codex subprocess, **no genesis worker**". The last clause is now
false. Rewrite that clause only; leave the rest.

- [ ] **Step 4: Verify both composes still parse**

```
python -c "import yaml,sys; [yaml.safe_load(open(f)) for f in ['deploy/docker-compose.phala.runner.yaml','deploy/docker-compose.phala.prod.runner.yaml']]; print('ok')"
```

- [ ] **Step 5: Docs**

- `HOSTED_RUNTIME_V2_ROLLOUT.md` Step 0 item 4 ("Verify") gains:
  `GET /v1/admin/v2-metrics` returns `genesis_alive: true`; drive one real genesis import
  end-to-end and confirm it decrypts (DEPLOYMENTS.md already flags this: "confirm a real
  import decrypts once after cutover").
- Same file, Deferred section: delete the stale bullet
  `**capture lane** wake execution (D3 scoped to heartbeat/scheduled/manual_wake; capture = memory-extraction, follow-up)`
  — capture shipped. Verify with `grep -rn '"capture"' backend/model_api_runtime/v2/worker.py` before deleting.
- `HOSTED_RUNTIME_V2_PARITY_MATRIX.md`: §F bucket 4 has one item ("Move the genesis import
  worker out of the agent-runner container", `:133`). Mark it done and note the bucket is
  now empty. Update the §D genesis row (`:79`) — it cites `prod.runner.yaml:87` and the
  supervisor line number, both of which you just changed.
- `deploy/DEPLOYMENTS.md:102-103`: the runner-CVM rows describe genesis as running in the
  agent-runner containers. Correct them.

- [ ] **Step 6: Full suite**

```
python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py
```
Expected: 8 pre-existing failures, zero new.

---

## Rollback

Rehoming is a compose change → new `compose_hash` → on-chain `addComposeHash()`.
**This is free right now**: `serve-worker` was already added to both runner composes and
Step 0 (`deploy/HOSTED_RUNTIME_V2_ROLLOUT.md`) has never been executed, so the pending
re-auth covers this change too. Landing it after Step 0 would cost a second re-auth.

If genesis misbehaves in `serve-worker`, the rollback is to put the three
`FEEDLING_GENESIS_*` keys back on `agent-runner` — but Task 2 removes supervisor's call
site, so that rollback also needs the image re-pinned to a pre-rehome `:<sha>`
(`ROLLOUT.md` "Image rollback" already covers this mechanic). Coexistence during a
transition is safe if ever needed (`FOR UPDATE SKIP LOCKED`), but is not wired.

## Out of scope

- Promoting genesis to its own container/service. The user chose the thread-in-serve_worker
  shape; a container would give it a restart policy and a real crash domain, and can be
  done later at the cost of one more `addComposeHash()`.
- `v2_wake_schedule.next_capture_at` (dead, already-plumbed column).
