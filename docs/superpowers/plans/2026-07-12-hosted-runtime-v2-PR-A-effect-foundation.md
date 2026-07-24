# Hosted Runtime V2 — PR A: Effect Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a generation-fenced, idempotent effect outbox + resident→draining→v2 cutover state machine + stable seq cursor, so every Hosted Runtime V2 side effect applies exactly once and never across a runtime-generation boundary.

**Architecture:** One core abstraction — the generation-fenced effect outbox. A monotonic per-user `runtime_generation` bumps on every cutover; each job pins the generation it was enqueued under; every side effect is written to `v2_effect_outbox` carrying that pinned generation + a deterministic `effect_id`; applying an effect is one transaction that dispatches iff `pinned == current` else discards; `UNIQUE(effect_id)` makes retries no-ops. Cursors anchor on the existing `chat_messages.seq`, never wall-clock `ts`.

**Tech Stack:** Python 3.11, psycopg, alembic, pytest. Source spec: `docs/superpowers/specs/2026-07-12-hosted-runtime-v2-PR-A-effect-foundation-design.md`.

## Global Constraints

- **NO-COMMIT mode.** Never run `git commit`/`git add`/`git stash`/`git checkout --`/`git reset`/`git clean`. Leave all work unstaged in the working tree. (The "Commit" step in each task is replaced by "leave in working tree; do not commit".)
- Worktree only: `/Users/zhengzhihao/Projects/teleport/feedling-mcp/.claude/worktrees/hosted-runtime-v2` on `feat/hosted-runtime-v2` @ 172b4ab. Never the main checkout.
- **Dependency direction** (AST-guarded by `tests/test_v2_dependency_direction.py`): `backend/model_api_runtime/v2/*` and `backend/capabilities/*` must NOT import `hosted` or `agent_runtime`. Only `serve_worker.py` (assembly) may. New pure modules (`effect_id.py`, `effect_outbox.py`, `cutover.py`, `cursor.py`) must stay import-clean; sinks that touch `hosted`/`capabilities` live in `serve_worker.py` assembly or `effect_sinks.py` (assembly-tier, exempt).
- **Do NOT treat missing/invalid `hosted_runtime_mode` as V2** (post-flip-revert invariant). `runtime_generation` is orthogonal to the resident/v2 *intent*; do not change the default.
- **No deploy, no user flip** during PR A. This is code only.
- Migration head before PR A is `0024_v2_worker_capacity`. New migrations chain after it; keep a single alembic head (verify with `ScriptDirectory.get_heads()` returning one).
- Full-suite command (a bare `pytest tests/` aborts at collection): `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py`. Postgres on `127.0.0.1:55432` (postgres/test). Baseline: ~11 pre-existing failures unrelated to this work; any NEW failure is a regression.

---

### Task 1: Deterministic `effect_id` (A5)

**Files:**
- Create: `backend/model_api_runtime/v2/effect_id.py`
- Test: `tests/test_v2_effect_id.py`

**Interfaces:**
- Produces:
  - `derive(*, job_id: int | None, effect_type: str, ordinal: int) -> str`
  - `derive_control(*, generation: int, effect_type: str, key: str) -> str`

- [ ] **Step 1: Write the failing test**

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import effect_id

def test_same_inputs_same_id():
    assert effect_id.derive(job_id=7, effect_type="reply", ordinal=0) == \
           effect_id.derive(job_id=7, effect_type="reply", ordinal=0)

def test_different_effect_different_id():
    a = effect_id.derive(job_id=7, effect_type="reply", ordinal=0)
    b = effect_id.derive(job_id=7, effect_type="status", ordinal=0)
    c = effect_id.derive(job_id=7, effect_type="reply", ordinal=1)
    d = effect_id.derive(job_id=8, effect_type="reply", ordinal=0)
    assert len({a, b, c, d}) == 4

def test_control_effect_id_no_job():
    x = effect_id.derive_control(generation=5, effect_type="cursor", key="s42")
    assert x == effect_id.derive_control(generation=5, effect_type="cursor", key="s42")
    assert x != effect_id.derive_control(generation=6, effect_type="cursor", key="s42")

def test_id_is_stable_string_shape():
    # No randomness, no timestamps: pure function of inputs.
    assert effect_id.derive(job_id=1, effect_type="memory", ordinal=2) == "job1:memory:2"
    assert effect_id.derive_control(generation=3, effect_type="schedule", key="wk") == "gen3:schedule:wk"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_v2_effect_id.py -q`
Expected: FAIL (module does not exist).

- [ ] **Step 3: Implement**

```python
"""Deterministic effect_id derivation (Hosted Runtime V2 PR A / spec A5).

The linchpin of exactly-once: retries of the SAME logical effect must produce the
SAME id (so a UNIQUE(effect_id) INSERT dedupes them), and distinct effects must
produce distinct ids. Pure function — NO randomness, NO clock. If this ever reads
time or random, retries double-write.
"""
from __future__ import annotations


def derive(*, job_id: int | None, effect_type: str, ordinal: int) -> str:
    """Effect emitted by a turn: keyed by (job, effect_type, execution-order ordinal)."""
    return f"job{int(job_id)}:{effect_type}:{int(ordinal)}"


def derive_control(*, generation: int, effect_type: str, key: str) -> str:
    """Control-plane effect with no owning job (e.g. a cutover-driven cursor advance):
    keyed by (generation, effect_type, caller-stable key)."""
    return f"gen{int(generation)}:{effect_type}:{key}"
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_v2_effect_id.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Leave in working tree; do NOT commit.**

---

### Task 2: Runtime generation + cutover state machine (A2)

**Files:**
- Create: `backend/alembic/versions/0025_v2_runtime_generation.py`
- Create: `backend/model_api_runtime/v2/cutover.py`
- Modify: `backend/db.py` (add generation/state accessors near the other v2 helpers)
- Test: `tests/test_v2_runtime_generation.py`

**Interfaces:**
- Consumes: `users` table; `model_api_runtime` profile blob storage (`db.get_blob`/`set_blob`).
- Produces:
  - `db.get_runtime_generation(user_id: str) -> int` (0 if user absent, ≥1 once initialized)
  - `db.advance_runtime_state(user_id: str, *, from_state: str, to_state: str) -> int | None` — CAS on state, bumps generation, returns new generation or `None` on lost race.
  - `cutover.RESIDENT = "resident"`, `cutover.DRAINING = "draining"`, `cutover.V2 = "v2"`
  - `cutover.VALID_TRANSITIONS: set[tuple[str, str]]`
  - `cutover.is_valid_transition(from_state, to_state) -> bool` (pure)

**Design note:** generation + state live in a dedicated table `v2_runtime_state` (one row per user), NOT the JSONB profile — a monotonic counter needs a real column + atomic `UPDATE ... +1`, and the fence query (Task 4) joins it cheaply.

- [ ] **Step 1: Write the failing test**

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))
import pytest, db
from model_api_runtime.v2 import cutover
from conftest import seed_user

def _fresh(uid):
    seed_user(uid)

def test_generation_starts_at_one_after_init(_pg):
    _fresh("u_gen1")
    # first read initializes the row lazily at generation 1, state resident
    assert db.get_runtime_generation("u_gen1") == 1

def test_valid_cutover_bumps_generation_monotonically(_pg):
    _fresh("u_gen2")
    assert db.get_runtime_generation("u_gen2") == 1
    g = db.advance_runtime_state("u_gen2", from_state="resident", to_state="draining")
    assert g == 2
    g = db.advance_runtime_state("u_gen2", from_state="draining", to_state="v2")
    assert g == 3
    assert db.get_runtime_generation("u_gen2") == 3

def test_lost_race_returns_none_no_bump(_pg):
    _fresh("u_gen3")
    db.get_runtime_generation("u_gen3")  # init at resident/1
    # from_state mismatch (already resident, ask draining->v2) => None, no bump
    assert db.advance_runtime_state("u_gen3", from_state="draining", to_state="v2") is None
    assert db.get_runtime_generation("u_gen3") == 1

def test_pure_transition_table():
    assert cutover.is_valid_transition("resident", "draining")
    assert cutover.is_valid_transition("draining", "v2")
    assert cutover.is_valid_transition("v2", "draining")
    assert cutover.is_valid_transition("draining", "resident")
    assert not cutover.is_valid_transition("resident", "v2")   # must pass through draining
    assert not cutover.is_valid_transition("v2", "resident")   # must pass through draining
```

Add a `_pg` fixture to this file if the repo's conftest doesn't already gate DB tests — check `tests/conftest.py`: DB is provisioned session-wide and `DATABASE_URL` is set, so `_pg` can be a no-op fixture `@pytest.fixture def _pg(): yield` OR just drop the `_pg` param and rely on the session DB. Match how `tests/test_v2_jobs_store.py` gates (it uses the session DB directly). Prefer dropping `_pg` and truncating `v2_runtime_state` in a local autouse fixture.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_v2_runtime_generation.py -q`
Expected: FAIL (`cutover` / accessors missing, table missing).

- [ ] **Step 3: Write the migration**

`backend/alembic/versions/0025_v2_runtime_generation.py`:

```python
"""Per-user runtime generation + cutover state (Hosted Runtime V2 PR A / spec A2).

The monotonic fence value behind the generation-fenced effect outbox. One row per
user; state advances resident->draining->v2 (and back) and each transition bumps
generation by exactly 1. A real BIGINT column (not JSONB) so the bump is an atomic
UPDATE and the outbox fence can join it cheaply.

Revision ID: 0025_v2_runtime_generation
"""
from alembic import op

revision = "0025_v2_runtime_generation"
down_revision = "0024_v2_worker_capacity"
branch_labels = None
depends_on = None

_UP = """
CREATE TABLE IF NOT EXISTS v2_runtime_state (
  user_id            TEXT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
  hosted_runtime_state TEXT NOT NULL DEFAULT 'resident',
  runtime_generation BIGINT NOT NULL DEFAULT 1,
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

_DOWN = "DROP TABLE IF EXISTS v2_runtime_state;"


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
```

Confirm `0024_v2_worker_capacity` is the current head first: `ls backend/alembic/versions/ | sort | tail -3`. If a different head, set `down_revision` to it.

- [ ] **Step 4: Write `cutover.py`**

```python
"""Cutover state machine constants + pure transition table (spec A2).

Pure (no DB) so it stays import-clean under the dependency-direction guard. The
DB CAS lives in db.advance_runtime_state; this module only names states and says
which transitions are legal. draining is a mandatory barrier: you cannot jump
resident<->v2 directly.
"""
from __future__ import annotations

RESIDENT = "resident"
DRAINING = "draining"
V2 = "v2"

VALID_TRANSITIONS: set[tuple[str, str]] = {
    (RESIDENT, DRAINING),
    (DRAINING, V2),
    (V2, DRAINING),
    (DRAINING, RESIDENT),
}


def is_valid_transition(from_state: str, to_state: str) -> bool:
    return (from_state, to_state) in VALID_TRANSITIONS
```

- [ ] **Step 5: Add db accessors**

In `backend/db.py`, near the other v2 helpers:

```python
def get_runtime_generation(user_id: str) -> int:
    """Current monotonic runtime generation for the user. Lazily initializes the
    row at (resident, 1) on first read for a known user; returns 0 for an unknown
    user (no users row)."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO v2_runtime_state (user_id) VALUES (%s) "
                "ON CONFLICT (user_id) DO NOTHING "
                "WHERE EXISTS (SELECT 1 FROM users u WHERE u.user_id = %s)",
                (user_id, user_id),
            )
            cur.execute(
                "SELECT runtime_generation FROM v2_runtime_state WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    return int(row[0]) if row else 0


def advance_runtime_state(user_id: str, *, from_state: str, to_state: str) -> int | None:
    """CAS the cutover state resident<->draining<->v2 and bump generation by 1,
    atomically, only if the row is still in from_state. Returns the NEW generation,
    or None if the from_state no longer holds (lost race) — callers must treat None
    as 'someone else moved the machine; re-read', never as success. Also refuses an
    illegal transition (returns None without touching the row)."""
    from model_api_runtime.v2 import cutover
    if not cutover.is_valid_transition(from_state, to_state):
        return None
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE v2_runtime_state "
                "SET hosted_runtime_state = %s, "
                "    runtime_generation = runtime_generation + 1, "
                "    updated_at = now() "
                "WHERE user_id = %s AND hosted_runtime_state = %s "
                "RETURNING runtime_generation",
                (to_state, user_id, from_state),
            )
            row = cur.fetchone()
    return int(row[0]) if row else None
```

Note: `db.py` importing `model_api_runtime.v2.cutover` inside the function (not at module top) keeps import order clean and avoids a cycle. Verify no cycle by running the suite.

- [ ] **Step 6: Run tests**

```
python -m pytest tests/test_v2_runtime_generation.py -q
python -m pytest tests/test_v2_dependency_direction.py -q
```
Both PASS. The second proves `cutover.py` didn't break layering.

- [ ] **Step 7: Confirm single alembic head**

```
cd backend && DATABASE_URL="postgresql://postgres:test@127.0.0.1:55432/postgres" python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; h=ScriptDirectory.from_config(Config('alembic.ini')).get_heads(); print(h); assert len(h)==1"
```

- [ ] **Step 8: Leave in working tree; do NOT commit.**

---

### Task 3: Job pins `expected_runtime_generation` (A3)

**Files:**
- Create: `backend/alembic/versions/0026_v2_job_expected_generation.py`
- Modify: `backend/model_api_runtime/v2/jobs_store.py` (`enqueue_job`, `claim_next_job`)
- Test: `tests/test_v2_jobs_store.py` (add cases)

**Interfaces:**
- Consumes: `db.get_runtime_generation` (Task 2), `agent_jobs`.
- Produces: `jobs_store.enqueue_job(user_id, lane, *, expected_generation: int | None = None, ...)` stamps the column; `claim_next_job` returns `expected_runtime_generation` in the job dict; a stale-generation job is completed as `superseded` at claim time.

- [ ] **Step 1: Write the failing test** (add to `tests/test_v2_jobs_store.py`)

```python
def test_enqueue_stamps_expected_generation(pg_clean):
    from conftest import seed_user
    seed_user("u_jobgen")
    gen = db.get_runtime_generation("u_jobgen")  # 1
    jid, _created = jobs_store.enqueue_job("u_jobgen", "chat", expected_generation=gen)
    row = jobs_store.claim_next_job("w1")
    assert row["id"] == jid
    assert row["expected_runtime_generation"] == gen

def test_stale_generation_job_superseded_at_claim(pg_clean):
    from conftest import seed_user
    seed_user("u_jobstale")
    jobs_store.enqueue_job("u_jobstale", "chat", expected_generation=1)
    # user cut over: generation moves to 3
    db.advance_runtime_state("u_jobstale", from_state="resident", to_state="draining")
    db.advance_runtime_state("u_jobstale", from_state="draining", to_state="v2")
    claimed = jobs_store.claim_next_job("w1")
    # stale job is not handed out for a turn; it is terminal 'superseded'
    assert claimed is None or claimed["status"] == "superseded"
```

Match the file's existing fixture name for a clean `agent_jobs`/`v2_runtime_state` (see how other tests there truncate; add `v2_runtime_state` to that truncation).

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_v2_jobs_store.py -q -k generation`
Expected: FAIL (column + kwarg missing).

- [ ] **Step 3: Migration**

`0026_v2_job_expected_generation.py` (down_revision `0025_v2_runtime_generation`):

```python
_UP = """
ALTER TABLE agent_jobs
  ADD COLUMN IF NOT EXISTS expected_runtime_generation BIGINT;
"""
_DOWN = "ALTER TABLE agent_jobs DROP COLUMN IF EXISTS expected_runtime_generation;"
```

Plus the standard `revision`/`down_revision`/`upgrade`/`downgrade` boilerplate mirroring Task 2's migration.

- [ ] **Step 4: Implement in `jobs_store.py`**

- `enqueue_job`: add keyword `expected_generation: int | None = None`; include `expected_runtime_generation` in the INSERT column list/values.
- `claim_next_job`: add `expected_runtime_generation` to the SELECT and returned dict. After selecting a candidate, if `expected_runtime_generation is not None and expected_runtime_generation < db.get_runtime_generation(user_id)`, mark it `superseded` (a terminal status; add `"superseded"` alongside existing terminal statuses) and continue to the next claimable job / return None. Keep this inside the same claim transaction to avoid hand-out races.

(Read the current `enqueue_job`/`claim_next_job` bodies first and thread the column through their existing SQL rather than rewriting them.)

- [ ] **Step 5: Run tests**

```
python -m pytest tests/test_v2_jobs_store.py -q
```
Expected: PASS (existing + 2 new).

- [ ] **Step 6: Single head check** (as Task 2 Step 7). **Leave in working tree; do NOT commit.**

---

### Task 4: Generation-fenced effect outbox (A4)

**Files:**
- Create: `backend/alembic/versions/0027_v2_effect_outbox.py`
- Create: `backend/model_api_runtime/v2/effect_outbox.py`
- Modify: `backend/db.py` (raw outbox row ops used by the pure module via injected callables OR thin db functions)
- Test: `tests/test_v2_effect_outbox.py`

**Interfaces:**
- Consumes: `effect_id` (Task 1), `db.get_runtime_generation` (Task 2), `v2_effect_outbox` table.
- Produces:
  - `db.effect_enqueue(effect_id, user_id, job_id, effect_type, expected_generation, payload) -> bool` (True if inserted, False if effect_id already present)
  - `db.effect_pending(user_id) -> list[dict]`
  - `db.effect_mark(effect_id, status, *, error="") -> None`
  - `effect_outbox.apply_pending_effects(user_id, *, dispatch) -> dict` where `dispatch(effect_type, payload) -> None` is injected (keeps the module sink-free / import-clean). Returns `{"applied": n, "discarded": m}`. Each row handled in its own transaction that re-reads generation `FOR UPDATE` on `v2_runtime_state`.

- [ ] **Step 1: Write the failing test**

```python
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))
import pytest, db
from model_api_runtime.v2 import effect_outbox, effect_id
from conftest import seed_user

def test_enqueue_is_idempotent_on_effect_id(pg_clean):
    seed_user("u_ob1")
    eid = effect_id.derive(job_id=1, effect_type="reply", ordinal=0)
    assert db.effect_enqueue(eid, "u_ob1", 1, "reply", 1, {"text": "hi"}) is True
    assert db.effect_enqueue(eid, "u_ob1", 1, "reply", 1, {"text": "DUP"}) is False
    pend = db.effect_pending("u_ob1")
    assert len(pend) == 1 and pend[0]["payload"]["text"] == "hi"

def test_apply_dispatches_when_generation_matches(pg_clean):
    seed_user("u_ob2")
    db.get_runtime_generation("u_ob2")  # init at 1
    eid = effect_id.derive(job_id=2, effect_type="reply", ordinal=0)
    db.effect_enqueue(eid, "u_ob2", 2, "reply", 1, {"text": "keep"})
    seen = []
    res = effect_outbox.apply_pending_effects("u_ob2", dispatch=lambda t, p: seen.append((t, p)))
    assert res == {"applied": 1, "discarded": 0}
    assert seen == [("reply", {"text": "keep"})]
    assert db.effect_pending("u_ob2") == []

def test_apply_discards_stale_generation_without_dispatch(pg_clean):
    seed_user("u_ob3")
    db.get_runtime_generation("u_ob3")  # 1
    eid = effect_id.derive(job_id=3, effect_type="memory", ordinal=0)
    db.effect_enqueue(eid, "u_ob3", 3, "memory", 1, {"card": "x"})
    # cut over -> generation 3; the pinned-at-1 effect must be discarded, NOT dispatched
    db.advance_runtime_state("u_ob3", from_state="resident", to_state="draining")
    db.advance_runtime_state("u_ob3", from_state="draining", to_state="v2")
    seen = []
    res = effect_outbox.apply_pending_effects("u_ob3", dispatch=lambda t, p: seen.append((t, p)))
    assert res == {"applied": 0, "discarded": 1}
    assert seen == []

def test_apply_is_rerunnable_after_partial(pg_clean):
    # A second apply pass over already-applied rows is a no-op (idempotent applier).
    seed_user("u_ob4")
    db.get_runtime_generation("u_ob4")
    eid = effect_id.derive(job_id=4, effect_type="status", ordinal=0)
    db.effect_enqueue(eid, "u_ob4", 4, "status", 1, {"k": "v"})
    n = []
    effect_outbox.apply_pending_effects("u_ob4", dispatch=lambda t, p: n.append(1))
    effect_outbox.apply_pending_effects("u_ob4", dispatch=lambda t, p: n.append(1))
    assert n == [1]  # dispatched exactly once
```

`pg_clean` = local autouse fixture truncating `v2_effect_outbox, v2_runtime_state, agent_jobs, user_blobs`.

- [ ] **Step 2: Run to verify it fails.** `python -m pytest tests/test_v2_effect_outbox.py -q` → FAIL.

- [ ] **Step 3: Migration** `0027_v2_effect_outbox.py` (down_revision `0026_v2_job_expected_generation`), DDL exactly the `v2_effect_outbox` table + partial index from spec §A4.

- [ ] **Step 4: db row ops** in `db.py`:

```python
def effect_enqueue(effect_id, user_id, job_id, effect_type, expected_generation, payload) -> bool:
    import json
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO v2_effect_outbox "
                "(effect_id, user_id, job_id, effect_type, expected_generation, payload) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (effect_id) DO NOTHING",
                (effect_id, user_id, job_id, effect_type, int(expected_generation),
                 json.dumps(payload)),
            )
            inserted = cur.rowcount == 1
    return inserted


def effect_pending(user_id) -> list[dict]:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT effect_id, job_id, effect_type, expected_generation, payload "
                "FROM v2_effect_outbox WHERE user_id=%s AND status='pending' "
                "ORDER BY created_at ASC",
                (user_id,),
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return rows
```

- [ ] **Step 5: The applier** in `effect_outbox.py` (pure of sinks; dispatch injected). The fence + dispatch + status flip is ONE transaction per row, re-reading generation `FOR UPDATE`:

```python
"""Generation-fenced effect applier (spec A4). Pure of sinks: dispatch is injected
so this module never imports hosted/capabilities. Each pending row is handled in
its own transaction that locks the user's v2_runtime_state row FOR UPDATE, so a
concurrent cutover cannot slip between the generation read and the apply.
"""
from __future__ import annotations
import json
from typing import Callable
import db


def apply_pending_effects(user_id: str, *, dispatch: Callable[[str, dict], None]) -> dict:
    applied = discarded = 0
    for row in db.effect_pending(user_id):
        eid = row["effect_id"]
        with db.get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT runtime_generation FROM v2_runtime_state "
                    "WHERE user_id=%s FOR UPDATE", (user_id,))
                gr = cur.fetchone()
                current = int(gr[0]) if gr else 0
                # re-check the row is still pending under the lock (rerun safety)
                cur.execute(
                    "SELECT status FROM v2_effect_outbox WHERE effect_id=%s", (eid,))
                st = cur.fetchone()
                if not st or st[0] != "pending":
                    continue
                if int(row["expected_generation"]) == current:
                    payload = row["payload"]
                    if isinstance(payload, str):
                        payload = json.loads(payload)
                    dispatch(row["effect_type"], payload)  # sink is effect_id-unique -> replay-safe
                    cur.execute(
                        "UPDATE v2_effect_outbox SET status='applied', applied_at=now() "
                        "WHERE effect_id=%s", (eid,))
                    applied += 1
                else:
                    cur.execute(
                        "UPDATE v2_effect_outbox SET status='discarded' WHERE effect_id=%s",
                        (eid,))
                    discarded += 1
    return {"applied": applied, "discarded": discarded}
```

- [ ] **Step 6: Run tests** `python -m pytest tests/test_v2_effect_outbox.py tests/test_v2_dependency_direction.py -q` → PASS. **Single head check. Leave in working tree; do NOT commit.**

---

### Task 5: Stable seq cursor (A1)

**Files:**
- Modify: `backend/db.py` (add `chat_max_seq`, `chat_messages_after_seq`)
- Modify: `backend/model_api_runtime/v2/invalidation.py` and any reader using `cursor_ts` → repoint to seq
- Create: `backend/model_api_runtime/v2/cursor.py` (`CursorState`, load/advance via the profile key `v2_reply_cursor_seq`)
- Test: `tests/test_v2_cursor.py`, plus a guard test `tests/test_v2_no_cursor_ts.py`

**Interfaces:**
- Produces: `db.chat_max_seq(user_id) -> int`; `db.chat_messages_after_seq(user_id, after_seq, *, limit) -> list[dict]`; `cursor.load_seq(store) -> int`; `cursor.advance_effect(job_id, ordinal, generation, new_seq) -> tuple[str, dict]` returning `(effect_id, payload)` for an outbox `cursor` effect.

- [ ] **Step 1: Failing tests**

```python
def test_after_seq_orders_by_seq_not_ts(pg_clean):
    # two messages with IDENTICAL ts must come back in seq order, both present
    from conftest import seed_user
    seed_user("u_cur1")
    db.chat_append("u_cur1", {"id": "m1", "ts": 100.0, "role": "user"})
    db.chat_append("u_cur1", {"id": "m2", "ts": 100.0, "role": "user"})  # same ts
    out = db.chat_messages_after_seq("u_cur1", 0, limit=10)
    assert [m["id"] for m in out] == ["m1", "m2"]
    assert db.chat_max_seq("u_cur1") == out[-1]["seq"]

def test_cursor_advance_is_a_cursor_effect(pg_clean):
    eid, payload = cursor.advance_effect(job_id=7, ordinal=3, generation=2, new_seq=42)
    assert payload == {"new_seq": 42}
    assert eid == "job7:cursor:3"
```

Check `db.chat_append`'s real signature first (grep `def chat_append`) and match it; the test must use the real append path so `seq` is assigned the same way production does.

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement** `chat_max_seq` / `chat_messages_after_seq` (SELECT ... WHERE user_id=%s AND seq > %s ORDER BY seq ASC LIMIT %s; include `seq` in the returned dict), and `cursor.py`:

```python
"""Stable per-user reply cursor keyed on chat_messages.seq (spec A1). Never ts:
identical timestamps under load make ts non-monotonic. Advancing the cursor is an
outbox 'cursor' effect so it is generation-fenced + idempotent like every effect.
"""
from __future__ import annotations
from model_api_runtime.v2 import effect_id

CURSOR_KEY = "v2_reply_cursor_seq"


def advance_effect(*, job_id: int, ordinal: int, generation: int, new_seq: int):
    return effect_id.derive(job_id=job_id, effect_type="cursor", ordinal=ordinal), {"new_seq": new_seq}
```

`load_seq(store)` reads `CURSOR_KEY` off the model_api_runtime profile (default 0). The actual cursor WRITE happens in the `cursor` dispatch sink (Task 6), not here.

- [ ] **Step 4: Guard test** `tests/test_v2_no_cursor_ts.py`: scan `backend/model_api_runtime/v2/*.py` for `cursor_ts` in non-comment code lines; assert none remain in read paths. (Mirror the structure of `tests/test_v2_no_gateway_dependency.py`'s source guard.)

- [ ] **Step 5: Run** `python -m pytest tests/test_v2_cursor.py tests/test_v2_no_cursor_ts.py -q` → PASS. **Leave in working tree; do NOT commit.**

---

### Task 6: Effect dispatch sinks (A6)

**Files:**
- Create: `backend/model_api_runtime/v2/serve_worker`-adjacent assembly — put sinks in `backend/model_api_runtime/v2/serve_worker.py` (assembly tier, exempt from the layering guard) as a `build_effect_dispatch(deps) -> Callable[[str, dict], None]`
- Test: `tests/test_v2_effect_sinks.py`

**Interfaces:**
- Consumes: `apply_pending_effects`'s `dispatch` contract; existing sinks already in `serve_worker.py` (`_write_encrypted_reply`, memory apply, wake schedule, status append, cursor write, follow-up enqueue).
- Produces: `serve_worker.build_effect_dispatch(...) -> dispatch(effect_type, payload)` routing the 7 effect types to their real writes, each keyed by `effect_id` so replay is safe.

- [ ] **Step 1: Failing test** — a fake-deps `build_effect_dispatch` routes each of the 7 `effect_type`s to the right injected sink exactly once; unknown type raises. (Use monkeypatched sink callables recording calls; assert routing table.)

- [ ] **Step 2–4:** Implement the router; wire it as the `dispatch` passed to `apply_pending_effects` at end-of-turn in the worker path. Reuse the existing reply/memory/schedule/status writers already in `serve_worker.py` — do NOT duplicate them. Each sink must upsert keyed by `effect_id` (add an `effect_id` column or a dedup guard at each sink) so an applier replay after a crash is a no-op.

- [ ] **Step 5: Run** `python -m pytest tests/test_v2_effect_sinks.py tests/test_v2_dependency_direction.py -q` → PASS. **Leave in working tree; do NOT commit.**

---

### Task 7: Transactional send + enqueue (A7)

**Files:**
- Modify: `backend/db.py` (a `chat_append_and_enqueue(user_id, message_doc, lane, *, expected_generation, reason)` doing both in ONE transaction)
- Modify: `backend/hosted/chat_send_core.py` (the v2 enqueue branch calls the transactional primitive; stamps `expected_generation = db.get_runtime_generation(user_id)`)
- Test: `tests/test_v2_send_enqueue_atomic.py`, plus update `tests/test_chat_send_v2_enqueue.py` if the enqueue call site changes shape

**Interfaces:**
- Consumes: `db.get_runtime_generation`, `jobs_store.enqueue_job` semantics, existing `chat_append`.
- Produces: `db.chat_append_and_enqueue(...) -> tuple[int, int]` returning `(seq, job_id)`; both rows commit together or neither does.

- [ ] **Step 1: Failing test** — simulate a failure between the message insert and the job insert (monkeypatch the job INSERT to raise inside the transaction); assert NEITHER the message NOR the job is present after (atomic rollback). And a success case: both present, job's `expected_runtime_generation` == the user's generation at send time.

- [ ] **Step 2–4:** Implement `chat_append_and_enqueue` as a single `with conn: with conn.transaction():` wrapping the existing message INSERT + the agent_jobs INSERT (reuse the SQL from `chat_append` and `enqueue_job`; do not open two pool connections). Repoint `chat_send_core.py`'s v2 branch to it. Add the reconciliation backstop as a `db.reconcile_unenqueued_v2_messages()` helper (find v2 users with `chat_max_seq > last enqueued chat seq` and single-flight enqueue a catch-up) — a function + unit test; wiring its periodic call is deferred to PR D's sweeper (note in the docstring).

- [ ] **Step 5: Run** `python -m pytest tests/test_v2_send_enqueue_atomic.py tests/test_chat_send_v2_enqueue.py -q` → PASS. **Leave in working tree; do NOT commit.**

---

### Task 8: PR A P0 fault-injection tests (the 3)

**Files:**
- Create: `tests/test_v2_p0_exactly_once.py`, `tests/test_v2_p0_aba.py`, `tests/test_v2_p0_seq_integrity.py`

**Interfaces:** Consumes everything above. No production code (unless a fault-injection seam is missing — if so, add a minimal injectable seam, not real logic).

- [ ] **Step 1: P0 — exactly-once across a durable-effect boundary** (`test_v2_p0_exactly_once.py`): enqueue a `reply` effect; run `apply_pending_effects` with a `dispatch` that RAISES after performing the sink write the first time (simulating crash after dispatch, before status flip); re-run the applier; assert the sink write happened exactly once (effect_id-unique at the sink) and the row ends `applied`. Parametrize the crash point (before dispatch / after dispatch-before-status / after status).

- [ ] **Step 2: P0 — no cross-generation contamination (ABA)** (`test_v2_p0_aba.py`): pin generation g on a job; enqueue all 7 effect types at g; force `draining→v2` to g+1; `apply_pending_effects`; assert every effect is `discarded`, dispatch was never called, and NO durable sink write occurred for g.

- [ ] **Step 3: P0 — history integrity under identical timestamps** (`test_v2_p0_seq_integrity.py`): append 5000 messages with an identical `ts`; assert `chat_messages_after_seq` returns all 5000 in seq order, `chat_max_seq` is correct, and a cursor advanced to `chat_max_seq` then re-read loses nothing and mis-orders nothing.

- [ ] **Step 4: Run all three** `python -m pytest tests/test_v2_p0_exactly_once.py tests/test_v2_p0_aba.py tests/test_v2_p0_seq_integrity.py -q` → PASS.

- [ ] **Step 5: Full suite** `python -m pytest tests/ -q --ignore=tests/test_api.py --ignore=tests/e2e_model_api_test.py` → 11-baseline, zero new failures. Single alembic head. **Leave in working tree; do NOT commit.**

---

## Self-Review

- **Spec coverage:** A1 cursor → Task 5; A2 generation/cutover → Task 2; A3 job pin → Task 3; A4 outbox → Task 4; A5 effect_id → Task 1; A6 sinks → Task 6; A7 send+enqueue → Task 7; 3 P0 fault injections → Task 8. All seven components + the three PR-A P0s covered.
- **Type consistency:** `effect_id.derive(job_id, effect_type, ordinal)` used identically in Tasks 1/4/5/8; `advance_runtime_state(from_state,to_state)->int|None` and `get_runtime_generation->int` consistent across Tasks 2/3/4/7; `apply_pending_effects(user_id,*,dispatch)->{"applied","discarded"}` consistent across Tasks 4/6/8; `expected_generation` kwarg consistent Tasks 3/7.
- **Ordering:** dependency order 1(effect_id)→2(generation)→3(job pin)→4(outbox)→5(cursor)→6(sinks)→7(send)→8(P0). Each task independently testable.
- **NO-COMMIT:** every task ends "leave in working tree; do not commit."
