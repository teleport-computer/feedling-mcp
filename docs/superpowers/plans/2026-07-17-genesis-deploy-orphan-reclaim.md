# Genesis deploy-orphan fast reclaim — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover an onboarding genesis job whose worker was killed (esp. by a
container deploy) in seconds via heartbeat death-detection, instead of the current
30-minute time-based reaper.

**Architecture:** Attribute each `uploaded`→`processing` genesis claim with the
claiming worker's id; beat the genesis heartbeat independently of the (blocking)
tick so liveness is truthful; each loop, reclaim `processing` jobs whose claiming
worker has no fresh `kind='genesis'` heartbeat — reset→`uploaded` when chunks are
stored (auto re-run) else `failed` fast (client retries). The 30-min time reaper
stays as the alive-but-wedged backstop.

**Tech Stack:** Python, Postgres (psycopg), alembic, existing
`genesis_import_jobs` / `v2_worker_heartbeats` tables.

## Global Constraints

- Design doc: `docs/superpowers/specs/2026-07-17-genesis-deploy-orphan-reclaim-design.md`.
- Postgres at `127.0.0.1:55432`. Full suite:
  `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py`.
  Baseline before this work: **4451 passed** — do not regress.
- Never touch the **turn** paths (PR D owns those). This is genesis-only.
- Keep the 30-min time reaper (`db.genesis_reap_stale_processing_jobs` +
  `genesis.worker.reap_stale_processing_jobs`) intact as a backstop — the new
  reclaim runs *before* it each loop but does not replace it.
- Env knob: `FEEDLING_GENESIS_WORKER_DEAD_SEC` (default **120**, floor 60) — how
  stale a `kind='genesis'` heartbeat must be to call the worker dead.
- TEE dual-write parity: any new write to `genesis_import_jobs` mirrors to TEE
  exactly like the existing genesis fns (`from tee_shadow import mirror`).
- All new DB fns fail closed / never raise into the loop (a reclaim/DB error is
  logged, never kills the genesis thread) — mirror the time reaper's tolerance.

---

### Task 1: Migration 0040 — claiming-worker columns on `genesis_import_jobs`

**Files:**
- Create: `backend/alembic/versions/0040_genesis_worker_claim.py`
- Test: `tests/test_genesis_worker_claim_migration.py`

**Interfaces:**
- Produces: columns `worker_claimed_by TEXT NOT NULL DEFAULT ''`,
  `worker_claimed_at TIMESTAMPTZ NULL` on `genesis_import_jobs`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_genesis_worker_claim_migration.py
import os, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db
pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")

def test_worker_claim_columns_exist():
    with db.get_pool().connection() as c:
        cols = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='genesis_import_jobs'").fetchall()}
    assert "worker_claimed_by" in cols
    assert "worker_claimed_at" in cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:55432/feedling_test python -m pytest tests/test_genesis_worker_claim_migration.py -q`
Expected: FAIL — `worker_claimed_by` not in cols.

- [ ] **Step 3: Write the migration**

```python
# backend/alembic/versions/0040_genesis_worker_claim.py
"""genesis serve-worker claim attribution (deploy-orphan fast reclaim).

The uploaded->processing claim (db.genesis_claim_uploaded_jobs) recorded no
worker id, so a job whose worker was killed (esp. a container deploy) could only
be recovered by the 30-min time reaper. These columns let a fast death-detected
reclaim tell whose claim went stale. Distinct from the resident path's
resident_* columns (that path is legacy agent-runner; this is the serve-worker
genesis thread).

Revision ID: 0040_genesis_worker_claim
"""
from alembic import op

revision = "0040_genesis_worker_claim"
down_revision = "0039_merge_tee_recon_state"
branch_labels = None
depends_on = None

_UP = """
ALTER TABLE genesis_import_jobs
    ADD COLUMN IF NOT EXISTS worker_claimed_by TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS worker_claimed_at TIMESTAMPTZ;
"""
_DOWN = """
ALTER TABLE genesis_import_jobs
    DROP COLUMN IF EXISTS worker_claimed_by,
    DROP COLUMN IF EXISTS worker_claimed_at;
"""

def upgrade() -> None:
    op.execute(_UP)

def downgrade() -> None:
    op.execute(_DOWN)
```

- [ ] **Step 4: Apply the migration to the test DB, run test to verify it passes**

Run: `DATABASE_URL=... python -c "import sys; sys.path.insert(0,'backend'); import db; db.init_schema()"` (or the repo's standard alembic-upgrade path used by conftest), then the pytest from Step 2.
Expected: PASS. Also `cd backend && python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; s=ScriptDirectory.from_config(Config('alembic.ini')); print(s.get_heads())"` prints a single head `('0040_genesis_worker_claim',)`.

- [ ] **Step 5: Commit** (only if the executor is authorized to commit; otherwise leave in working tree per NO-COMMIT).

---

### Task 2: `jobs_store.live_genesis_worker_ids`

**Files:**
- Modify: `backend/model_api_runtime/v2/jobs_store.py` (near `workers_alive`, ~982)
- Test: `tests/test_v2_jobs_store.py` (add cases)

**Interfaces:**
- Produces: `live_genesis_worker_ids(*, within_sec: int = 30) -> list[str]` — the
  `worker_id`s with a `kind='genesis'` heartbeat fresher than `within_sec`.

- [ ] **Step 1: Write the failing test**

```python
def test_live_genesis_worker_ids_only_fresh(_clean=None):
    import time
    from model_api_runtime.v2 import jobs_store
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM v2_worker_heartbeats WHERE worker_id LIKE 'wtest-%'")
    jobs_store.record_worker_heartbeat("wtest-fresh:genesis", kind="genesis", capacity=0)
    # a stale one: insert with an old beat_at directly
    with db.get_pool().connection() as c:
        c.execute("INSERT INTO v2_worker_heartbeats (worker_id, beat_at, kind, capacity) "
                  "VALUES ('wtest-stale:genesis', now() - interval '600 seconds', 'genesis', 0) "
                  "ON CONFLICT (worker_id) DO UPDATE SET beat_at = EXCLUDED.beat_at")
    # a turn worker must NOT be returned
    jobs_store.record_worker_heartbeat("wtest-turn", kind="turn", capacity=4)
    ids = jobs_store.live_genesis_worker_ids(within_sec=120)
    assert "wtest-fresh:genesis" in ids
    assert "wtest-stale:genesis" not in ids
    assert "wtest-turn" not in ids
```

- [ ] **Step 2: Run test → FAIL** (`AttributeError: live_genesis_worker_ids`).

- [ ] **Step 3: Implement**

```python
def live_genesis_worker_ids(*, within_sec: int = 30) -> list[str]:
    """worker_ids with a fresh kind='genesis' heartbeat. Sibling to
    workers_alive()/live_worker_count() (which read kind='turn' only): those gate
    chat admission; this gates genesis orphan reclaim."""
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            "SELECT worker_id FROM v2_worker_heartbeats "
            "WHERE kind='genesis' AND beat_at > now() - make_interval(secs => %s)",
            (max(1, int(within_sec)),),
        ).fetchall()
    return [str(r[0]) for r in rows]
```

- [ ] **Step 4: Run test → PASS.**

- [ ] **Step 5: Commit (per authorization).**

---

### Task 3: `genesis_claim_uploaded_jobs` records the claiming worker

**Files:**
- Modify: `backend/db.py` — `genesis_claim_uploaded_jobs` (2830)
- Test: `tests/test_genesis_worker_claim.py` (new)

**Interfaces:**
- Consumes: nothing new.
- Produces: `genesis_claim_uploaded_jobs(*, worker_id: str = "", limit: int = 1)` —
  sets `worker_claimed_by = worker_id`, `worker_claimed_at = now()` on the claimed
  rows (primary + TEE mirror). `worker_id=""` keeps today's behavior (attribution
  blank) so any non-updated caller/test is unaffected.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_genesis_worker_claim.py
import os, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db
pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs PG")

def _mk_uploaded(uid, jid, *, chunks=0):
    db.genesis_create_job(uid, {"job_id": jid, "status": "uploaded",
                                "source_kind": "history_import", "total_chunks": 3})
    with db.get_pool().connection() as c:
        c.execute("UPDATE genesis_import_jobs SET status='uploaded', received_chunks=%s, "
                  "finalized_at=now() WHERE user_id=%s AND job_id=%s", (chunks, uid, jid))

def test_claim_records_worker_id():
    uid, jid = "u_gclaim", "genesis_claim1"
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM genesis_import_jobs WHERE user_id=%s", (uid,))
    _mk_uploaded(uid, jid)
    claimed = db.genesis_claim_uploaded_jobs(worker_id="w-1:genesis", limit=5)
    assert any(j["job_id"] == jid for j in claimed)
    with db.get_pool().connection() as c:
        row = c.execute("SELECT worker_claimed_by, worker_claimed_at, status "
                        "FROM genesis_import_jobs WHERE user_id=%s AND job_id=%s",
                        (uid, jid)).fetchone()
    assert row[0] == "w-1:genesis"
    assert row[1] is not None
    assert row[2] == "processing"
```

- [ ] **Step 2: Run test → FAIL** (`worker_claimed_by` is `''`).

- [ ] **Step 3: Implement** — add the `worker_id` kwarg and set the columns in BOTH
  the primary `UPDATE ... SET status='processing' ...` and the TEE `mirror_sql`.

```python
def genesis_claim_uploaded_jobs(*, worker_id: str = "", limit: int = 1) -> list[dict]:
    # ...docstring unchanged plus: "worker_id attributes the claim so a
    # death-detected reclaim can tell whose claim went stale."
    safe_limit = max(1, min(int(limit or 1), 16))
    wid = str(worker_id or "")
    with get_pool().connection() as conn:
        with conn.transaction():
            cur = conn.execute(
                """
                WITH picked AS (
                    SELECT user_id, job_id FROM genesis_import_jobs
                    WHERE status = 'uploaded'
                    ORDER BY finalized_at ASC NULLS LAST, updated_at ASC
                    LIMIT %s FOR UPDATE SKIP LOCKED
                )
                UPDATE genesis_import_jobs AS j SET
                    status = 'processing', error = '',
                    output = jsonb_build_object('stage', 'worker_claimed'),
                    worker_claimed_by = %s, worker_claimed_at = now(),
                    updated_at = now()
                FROM picked
                WHERE j.user_id = picked.user_id AND j.job_id = picked.job_id
                RETURNING j.*
                """,
                (safe_limit, wid),
            )
            rows = cur.fetchall(); cols = [d[0] for d in cur.description]
    out = []
    for row in rows:
        item = dict(zip(cols, row))
        for k, v in list(item.items()):
            if hasattr(v, "isoformat"):
                item[k] = v.isoformat()
        out.append(item)
    if out:
        placeholders = ", ".join(["(%s, %s)"] * len(out))
        mirror_sql = (
            "UPDATE genesis_import_jobs SET status='processing', error='', "
            "output=jsonb_build_object('stage','worker_claimed'), "
            "worker_claimed_by=%s, worker_claimed_at=now(), updated_at=now() "
            f"WHERE (user_id, job_id) IN ({placeholders})"
        )
        mirror_params = (wid, *(v for item in out for v in (item["user_id"], item["job_id"])))
        from tee_shadow import mirror
        mirror.execute(mirror_sql, mirror_params)
    return out
```

- [ ] **Step 4: Run test → PASS.** Also run the existing genesis suite
  (`pytest tests/ -q -k genesis`) — every current caller of
  `genesis_claim_uploaded_jobs()` still works because `worker_id` defaults to `""`.

- [ ] **Step 5: Commit (per authorization).**

---

### Task 4: `genesis_reclaim_orphaned_processing_jobs` (the reclaim DB fn)

**Files:**
- Modify: `backend/db.py` — new fn beside `genesis_reap_stale_processing_jobs` (2943)
- Test: `tests/test_genesis_worker_claim.py` (add cases)

**Interfaces:**
- Consumes: `worker_claimed_by`/`worker_claimed_at`/`received_chunks` (Tasks 1/3).
- Produces:
  `genesis_reclaim_orphaned_processing_jobs(live_worker_ids: list[str], *, dead_sec: int, error: str, limit: int = 50) -> list[dict]`
  — atomically, for `processing` rows where `worker_claimed_by <> ''` AND NOT in
  `live_worker_ids` AND `worker_claimed_at < now() - dead_sec`: reset to
  `uploaded` (clear worker attribution, so a live worker re-claims) when
  `received_chunks > 0`, else `failed` with `error`. Returns changed rows with a
  `_reclaim_action` key (`"requeued"` | `"failed"`) for the caller's state sync.

- [ ] **Step 1: Write the failing tests**

```python
def _mk_processing(uid, jid, *, worker, claimed_age_sec, chunks):
    db.genesis_create_job(uid, {"job_id": jid, "status": "uploaded",
                                "source_kind": "history_import", "total_chunks": 3})
    with db.get_pool().connection() as c:
        c.execute("UPDATE genesis_import_jobs SET status='processing', received_chunks=%s, "
                  "worker_claimed_by=%s, worker_claimed_at = now() - make_interval(secs=>%s), "
                  "updated_at = now() - make_interval(secs=>%s) "
                  "WHERE user_id=%s AND job_id=%s",
                  (chunks, worker, claimed_age_sec, claimed_age_sec, uid, jid))

def test_orphan_with_chunks_requeued_to_uploaded():
    uid = "u_reclaim"; 
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM genesis_import_jobs WHERE user_id=%s", (uid,))
    _mk_processing(uid, "j_chunks", worker="dead:genesis", claimed_age_sec=300, chunks=3)
    changed = db.genesis_reclaim_orphaned_processing_jobs(
        ["live:genesis"], dead_sec=120, error="genesis_worker_lost")
    row = next(j for j in changed if j["job_id"] == "j_chunks")
    assert row["_reclaim_action"] == "requeued"
    with db.get_pool().connection() as c:
        st, wid = c.execute("SELECT status, worker_claimed_by FROM genesis_import_jobs "
                            "WHERE user_id=%s AND job_id='j_chunks'", (uid,)).fetchone()
    assert st == "uploaded" and wid == ""   # re-claimable, attribution cleared

def test_orphan_plaintext_failed_fast():
    uid = "u_reclaim2"
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM genesis_import_jobs WHERE user_id=%s", (uid,))
    _mk_processing(uid, "j_plain", worker="dead:genesis", claimed_age_sec=300, chunks=0)
    changed = db.genesis_reclaim_orphaned_processing_jobs(
        ["live:genesis"], dead_sec=120, error="genesis_worker_lost")
    assert next(j for j in changed if j["job_id"] == "j_plain")["_reclaim_action"] == "failed"
    with db.get_pool().connection() as c:
        st = c.execute("SELECT status FROM genesis_import_jobs WHERE user_id=%s AND job_id='j_plain'",
                       (uid,)).fetchone()[0]
    assert st == "failed"

def test_live_worker_job_untouched():
    uid = "u_reclaim3"
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM genesis_import_jobs WHERE user_id=%s", (uid,))
    _mk_processing(uid, "j_live", worker="live:genesis", claimed_age_sec=300, chunks=0)
    changed = db.genesis_reclaim_orphaned_processing_jobs(
        ["live:genesis"], dead_sec=120, error="x")
    assert not any(j["job_id"] == "j_live" for j in changed)

def test_recently_claimed_orphan_not_yet_dead():
    uid = "u_reclaim4"
    with db.get_pool().connection() as c:
        c.execute("DELETE FROM genesis_import_jobs WHERE user_id=%s", (uid,))
    _mk_processing(uid, "j_new", worker="dead:genesis", claimed_age_sec=30, chunks=0)
    changed = db.genesis_reclaim_orphaned_processing_jobs(
        ["live:genesis"], dead_sec=120, error="x")
    assert not any(j["job_id"] == "j_new" for j in changed)  # claimed 30s ago < 120s
```

- [ ] **Step 2: Run tests → FAIL** (fn missing).

- [ ] **Step 3: Implement**

```python
def genesis_reclaim_orphaned_processing_jobs(
    live_worker_ids: list[str], *, dead_sec: int, error: str, limit: int = 50,
) -> list[dict]:
    """Fast-recover 'processing' genesis jobs whose claiming worker is dead (no
    fresh kind='genesis' heartbeat), instead of waiting out the 30-min time
    reaper. Resumable (received_chunks>0, chunks stored) -> back to 'uploaded' for
    a live worker to re-run; non-resumable (plaintext, received_chunks=0) ->
    'failed' so the client retries. Atomic + SKIP LOCKED so it can't race a live
    reducer. Only rows with a non-empty worker_claimed_by older than dead_sec and
    NOT in live_worker_ids are eligible — a live or recently-claimed job is never
    touched. Returns changed rows with `_reclaim_action`."""
    safe_sec = max(60, int(dead_sec or 0))
    safe_limit = max(1, min(int(limit or 1), 200))
    live = list(dict.fromkeys(str(w) for w in (live_worker_ids or []) if str(w)))
    with get_pool().connection() as conn:
        cur = conn.execute(
            """
            WITH picked AS (
                SELECT user_id, job_id FROM genesis_import_jobs
                WHERE status = 'processing'
                  AND COALESCE(worker_claimed_by, '') <> ''
                  AND NOT (worker_claimed_by = ANY(%s))
                  AND worker_claimed_at < now() - make_interval(secs => %s)
                ORDER BY worker_claimed_at ASC
                LIMIT %s FOR UPDATE SKIP LOCKED
            )
            UPDATE genesis_import_jobs AS j SET
                status = CASE WHEN j.received_chunks > 0 THEN 'uploaded' ELSE 'failed' END,
                error  = CASE WHEN j.received_chunks > 0 THEN '' ELSE %s END,
                worker_claimed_by = CASE WHEN j.received_chunks > 0 THEN '' ELSE j.worker_claimed_by END,
                worker_claimed_at = CASE WHEN j.received_chunks > 0 THEN NULL ELSE j.worker_claimed_at END,
                updated_at = now()
            FROM picked
            WHERE j.user_id = picked.user_id AND j.job_id = picked.job_id
            RETURNING j.*
            """,
            (live, safe_sec, safe_limit, error[:1000]),
        )
        rows = cur.fetchall(); cols = [d[0] for d in cur.description]
    out = []
    for row in rows:
        item = dict(zip(cols, row))
        for k, v in list(item.items()):
            if hasattr(v, "isoformat"):
                item[k] = v.isoformat()
        item["_reclaim_action"] = "requeued" if str(item.get("status")) == "uploaded" else "failed"
        out.append(item)
    if out:
        # TEE mirror: pin to the exact (user_id, job_id) pairs, same pattern as the
        # claim/time-reaper mirror writes.
        placeholders = ", ".join(["(%s, %s)"] * len(out))
        mirror_sql = (
            "UPDATE genesis_import_jobs j SET "
            "status = CASE WHEN j.received_chunks > 0 THEN 'uploaded' ELSE 'failed' END, "
            "error = CASE WHEN j.received_chunks > 0 THEN '' ELSE %s END, "
            "worker_claimed_by = CASE WHEN j.received_chunks > 0 THEN '' ELSE j.worker_claimed_by END, "
            "worker_claimed_at = CASE WHEN j.received_chunks > 0 THEN NULL ELSE j.worker_claimed_at END, "
            "updated_at = now() "
            f"WHERE (j.user_id, j.job_id) IN ({placeholders})"
        )
        mirror_params = (error[:1000], *(v for item in out for v in (item["user_id"], item["job_id"])))
        from tee_shadow import mirror
        mirror.execute(mirror_sql, mirror_params)
    return out
```

- [ ] **Step 4: Run tests → PASS (all 4).**

- [ ] **Step 5: Commit (per authorization).**

---

### Task 5: `genesis.worker.reclaim_orphaned_processing_jobs` + thread worker_id into the claim

**Files:**
- Modify: `backend/genesis/worker.py` — new wrapper near `reap_stale_processing_jobs`
  (~1552); `tick(...)` (1644) gains `worker_id`; the claim call passes it.
- Test: `tests/test_genesis_reclaim_worker.py` (new)

**Interfaces:**
- Consumes: `db.genesis_reclaim_orphaned_processing_jobs`,
  `jobs_store.live_genesis_worker_ids`.
- Produces:
  - `_genesis_worker_dead_sec() -> int` — `max(60, env FEEDLING_GENESIS_WORKER_DEAD_SEC, default 120)`.
  - `reclaim_orphaned_processing_jobs(worker_id: str) -> list[dict]` — reads live
    ids, calls the DB fn, syncs `genesis_state` for each changed row (mirror
    `reap_stale_processing_jobs`: `service.write_genesis_state(store, job,
    status=...)` where status is `"failed"` for failed, and for requeued write the
    `uploaded` state so iOS shows "still importing", not terminal). Never raises.
  - `tick(*, api_url, enclave_url, mint_runtime_token, worker_id="", max_jobs=1)` —
    passes `worker_id` to `db.genesis_claim_uploaded_jobs(worker_id=worker_id, ...)`.

- [ ] **Step 1: Write the failing test** (monkeypatch the DB seam so it's unit,
  no live genesis worker needed):

```python
# tests/test_genesis_reclaim_worker.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from genesis import worker as gworker

def test_reclaim_reads_live_ids_and_syncs_state(monkeypatch):
    seen = {}
    monkeypatch.setattr(gworker.jobs_store, "live_genesis_worker_ids",
                        lambda *, within_sec: ["live:genesis"])
    def _fake_reclaim(live, *, dead_sec, error, limit=50):
        seen["live"] = live; seen["dead_sec"] = dead_sec
        return [{"user_id": "u1", "job_id": "j1", "status": "failed", "_reclaim_action": "failed"},
                {"user_id": "u2", "job_id": "j2", "status": "uploaded", "_reclaim_action": "requeued"}]
    monkeypatch.setattr(gworker.db, "genesis_reclaim_orphaned_processing_jobs", _fake_reclaim)
    synced = []
    monkeypatch.setattr(gworker.service, "write_genesis_state",
                        lambda store, job, status: synced.append((job["job_id"], status)))
    monkeypatch.setattr(gworker, "get_store", lambda uid: object())
    out = gworker.reclaim_orphaned_processing_jobs("live:genesis")
    assert seen["live"] == ["live:genesis"]
    assert {a for _, a in [(j["job_id"], j["_reclaim_action"]) for j in out]} == {"failed", "requeued"}
    # failed row synced terminal; requeued row synced back to importing (uploaded)
    assert ("j1", "failed") in synced
    assert any(jid == "j2" and status in ("uploaded", "processing") for jid, status in synced)
```

- [ ] **Step 2: Run → FAIL** (fn missing).

- [ ] **Step 3: Implement** the `_genesis_worker_dead_sec`, `reclaim_orphaned_processing_jobs`,
  and add `worker_id` to `tick` + the claim call. Follow `reap_stale_processing_jobs`'s
  structure (per-row try/except, `_trace_genesis`, never raise).

- [ ] **Step 4: Run → PASS.** Also `pytest tests/ -q -k genesis` green.

- [ ] **Step 5: Commit (per authorization).**

---

### Task 6: Wire reclaim into the loop + independent genesis heartbeat + thread worker_id

**Files:**
- Modify: `backend/genesis/daemon.py` — `run_loop` (51) gains `worker_id`; calls
  `reclaim_orphaned_processing_jobs(worker_id)` each loop (before the time reaper);
  passes `worker_id` into `tick`.
- Modify: `backend/model_api_runtime/v2/serve_worker.py` — `_start_genesis_thread`
  (2008): pass the genesis `worker_id` into `run_loop`; beat the heartbeat on a
  ~15s background cadence decoupled from the tick (a tiny daemon timer thread that
  calls `jobs_store.record_worker_heartbeat(genesis_worker_id, kind='genesis',
  capacity=0)` every `FEEDLING_GENESIS_HEARTBEAT_SEC` (default 15), stopped by the
  same stop_event), so a worker blocked in a long distill still looks alive.
- Test: `tests/test_genesis_daemon_reclaim.py` (new)

**Interfaces:**
- Consumes: `genesis.worker.reclaim_orphaned_processing_jobs`,
  `genesis.worker.tick` (worker_id), `jobs_store.record_worker_heartbeat`.
- Produces: `run_loop(*, api_url, enclave_url, mint_genesis, interval, stop_event,
  worker_id="", on_beat=None)` — reclaim + tick each iteration.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_genesis_daemon_reclaim.py
import sys, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from genesis import daemon

def test_run_loop_reclaims_before_tick(monkeypatch):
    order = []
    import genesis.worker as gw
    monkeypatch.setattr(gw, "reclaim_orphaned_processing_jobs",
                        lambda worker_id: order.append(("reclaim", worker_id)) or [])
    monkeypatch.setattr(gw, "reap_stale_processing_jobs", lambda: order.append(("reap",)) or [])
    def _tick(*, api_url, enclave_url, mint_runtime_token, worker_id="", max_jobs=1):
        order.append(("tick", worker_id))
    monkeypatch.setattr(gw, "tick", _tick)
    stop = threading.Event()
    # stop after one iteration via on_beat side-effect
    beats = {"n": 0}
    def _beat():
        beats["n"] += 1
        if beats["n"] >= 2:  # beat before + after one tick
            stop.set()
    daemon.run_loop(api_url="", enclave_url="", mint_genesis=lambda *a, **k: "t",
                    interval=0.01, stop_event=stop, worker_id="w:genesis", on_beat=_beat)
    assert ("reclaim", "w:genesis") in order
    assert order.index(("reclaim", "w:genesis")) < order.index(("tick", "w:genesis"))
```

For the independent heartbeat, add a `serve_worker` test that fakes a slow tick and
asserts the heartbeat row's `beat_at` advanced during the blocked tick (or, if that
is too integration-heavy, a unit test of the timer helper: it calls
`record_worker_heartbeat` at least twice over 2 intervals while a `stop_event` is
unset, then stops on set).

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement.** `daemon.run_loop`:

```python
def run_loop(*, api_url, enclave_url, mint_genesis, interval, stop_event,
             worker_id="", on_beat=None) -> None:
    from genesis import worker as genesis_worker
    while not stop_event.is_set():
        _beat(on_beat)
        try:
            genesis_worker.reclaim_orphaned_processing_jobs(worker_id)  # NEW: fast death-detected reclaim
            genesis_worker.reap_stale_processing_jobs()                 # backstop (unchanged)
            genesis_worker.tick(api_url=api_url, enclave_url=enclave_url,
                                mint_runtime_token=mint_genesis,
                                worker_id=worker_id, max_jobs=1)
        except Exception as e:  # noqa: BLE001
            print(f"[genesis:daemon] tick failed: {type(e).__name__}:{str(e)[:200]}")
        _beat(on_beat)
        stop_event.wait(interval)
```

`serve_worker._start_genesis_thread`: pass `worker_id=genesis_worker_id` into
`run_loop`, and start a background heartbeat timer thread (stopped by the existing
genesis `stop_event`) that records the `kind='genesis'` heartbeat every
`FEEDLING_GENESIS_HEARTBEAT_SEC` (default 15) independent of the tick. Keep the
existing before/after-tick `on_beat` too (harmless; the timer is the liveness
guarantee during a blocked tick).

- [ ] **Step 4: Run → PASS.** Full genesis suite green.

- [ ] **Step 5: Commit (per authorization).**

---

### Task 7: P0 acceptance — deploy-kill recovery, no false reclaim, backstop intact

**Files:**
- Test: `tests/test_genesis_deploy_orphan_p0.py` (new, real DB)

**Interfaces:** consumes everything above.

- [ ] **Step 1: Write the P0 tests**

```python
# tests/test_genesis_deploy_orphan_p0.py — real-DB, end-to-end via the DB + worker seams
# 1. deploy-kill recovery: a processing job claimed by a worker with NO fresh
#    kind='genesis' heartbeat, claimed_at > dead_sec ago:
#      - chunks stored  -> reclaim resets to 'uploaded' within dead_sec (not 30min)
#      - plaintext      -> reclaim marks 'failed' within dead_sec
# 2. no false reclaim: same job but its worker HAS a fresh kind='genesis'
#    heartbeat -> reclaim_orphaned_processing_jobs leaves it 'processing' even
#    though updated_at is stale (simulating a live worker mid-long-distill).
# 3. backstop intact: db.genesis_reap_stale_processing_jobs still fails a job
#    whose worker heartbeats fine but the job's updated_at is > 30-min cutoff.
```
Drive via `genesis.worker.reclaim_orphaned_processing_jobs(<live worker id>)` after
seeding `v2_worker_heartbeats` + `genesis_import_jobs` rows directly (mirror
`test_genesis_worker_claim.py`'s `_mk_processing`). Assert final `status` per case.

- [ ] **Step 2: Run → FAIL where unimplemented, else PASS.**

- [ ] **Step 3: Fill any gaps surfaced.**

- [ ] **Step 4: Full suite** `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py`
  → **4451 + new tests, 0 regressions.**

- [ ] **Step 5: Commit (per authorization).**

---

## Deploy + verify on pre

After merge/deploy, reproduce the original failure safely: start an onboarding on
a pre V2 account, then trigger a runner-CVM redeploy mid-distill; confirm the
orphaned job recovers within `FEEDLING_GENESIS_WORKER_DEAD_SEC` (resumable →
completes; plaintext → fails fast → retry works) instead of the 30-minute wait.

## Self-Review notes

- **Spec coverage:** every design component (1–6) maps to Tasks 1–6; acceptance to
  Task 7. The 30-min time reaper is untouched (backstop).
- **Genesis-only:** no turn-path file is modified. PR D invariants preserved.
- **Type consistency:** `worker_id` is the `<worker_id>:genesis` heartbeat id
  throughout (claim records it, `live_genesis_worker_ids` returns it, reclaim
  compares it). `_reclaim_action` ∈ {"requeued","failed"} everywhere.
- **False-reclaim safety:** the independent heartbeat (Task 6) is what makes the
  120s `dead_sec` safe — without it a live worker mid-distill would look dead.
