# Hosted Runtime V2 — Full-Conversation Context (condition 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development, **NO-COMMIT mode** (user commits at the end — never `git add`/`git commit`). Review each task via filesystem-snapshot diff. Steps use `- [ ]`.
> **Spec:** `docs/superpowers/specs/2026-07-09-hosted-runtime-v2-D1-full-conversation-context-design.md`.

**Goal:** The V2 chat turn shows the model the WHOLE conversation (encrypted itemized summary + verbatim recent tail), not just unreplied messages — so hosted 小克 stops forgetting — with a maintenance-lane compaction job keeping it bounded.

**Architecture:** New pure `context.py` (prompt assembly + budget check) and `compaction.py` (append-and-merge fold via the user's BYOK LLM). A new `v2_conversation_summary` table holds the encrypted summary + watermark + version in ONE row (single-row CAS = atomic, idempotent). New `TurnDeps` callbacks (`read_summary`/`write_summary`/`read_tail`) injected by the assembly layer `serve_worker.py`. `worker.process_job` dispatches by `lane`: `chat` → full-context turn (+ enqueue compaction when over budget); `maintenance` → run compaction.

**Tech Stack:** Python, FastAPI, Postgres (psycopg), Alembic, pytest, Docker PG on :55432.

## Global Constraints (copy verbatim into every reviewer prompt)

- **NO commits, NO `git add`.** User commits at the very end.
- **Dependency direction (AST-guarded by `tests/test_v2_dependency_direction.py`):** `backend/model_api_runtime/v2/*` MUST NOT import `hosted`/`agent_runtime`. Hosted/enclave access is injected via `TurnDeps` by `serve_worker.py` (the only assembly layer). `provider_client` (at `backend/provider_client.py`) IS allowed.
- **BYOK-only:** every LLM call (turn responder AND compaction) uses the user's own JIT-decrypted `provider_config` via `provider_client.reliable_chat_completion_async`. No platform/company LLM key anywhere.
- **no-filler:** only the turn's model-authored `final_response` writes a chat bubble. The compaction job writes NO bubble — only the summary row.
- **Single-decrypt (provider key):** the turn's/compaction-job's provider-key JIT decrypt stays once per job. The summary blob is an **envelope decrypt** (same class as per-message chat decrypt), wrapped by `ENCLAVE_SEMAPHORE`.
- **Encryption is LOCAL** (`content_encryption.build_envelope` / `core.envelope._build_shared_envelope_for_store`, in-process X25519+AEAD). Only DECRYPTION (`core.enclave._decrypt_envelope_via_enclave`) is an enclave HTTP round-trip. This is why the summary write can be a single-row CAS.
- **Gated:** all behavior behind `hosted_runtime_mode == "db_action_v2"` (resident CLI users untouched).
- **Baseline:** full backend suite = 2477 passed / 7 pre-existing failed (debug-trace/memory-capture/verify-ping) after Step 1. Zero new regressions allowed.
- **Budgets (env vars, defaults):** `FEEDLING_V2_TAIL_BUDGET_MSGS`=20 (over → compact), `FEEDLING_V2_TAIL_KEEP_MSGS`=10 (fold down to ~this many verbatim), `FEEDLING_V2_TAIL_HARD_CAP`=60 (max verbatim ever sent, safety before compaction catches up).

---

## Task 1: `context.py` — pure prompt assembly + budget check

**Files:**
- Create: `backend/model_api_runtime/v2/context.py`
- Test: `tests/test_v2_context.py`

**Interfaces:**
- Produces:
  - `build_turn_messages(*, system_prompt: str, summary: str, tail: list[dict], action_context: str = "") -> list[dict]` — returns the message list: `[{"role":"system","content":system_prompt}]` + (if `summary.strip()`: `[{"role":"system","content":"对话摘要（早前内容）：\n"+summary}]`) + the tail rendered as turns (each `{"role": _norm_role(m["role"]), "content": m["content"]}`, dropping blank content) + (if `action_context.strip()`: `[{"role":"system","content":action_context}]`). `_norm_role` maps assistant roles `{"openclaw","assistant","agent"}`→`"assistant"`, everything else→`"user"`.
  - `needs_compaction(tail: list[dict], *, budget: int) -> bool` — `len([m for m in tail if str(m.get("content") or "").strip()]) > budget`.

**Steps:**

- [ ] **Step 1: failing test**
```python
# tests/test_v2_context.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import context

def test_build_turn_messages_orders_persona_summary_tail():
    tail = [
        {"id":"1","ts":1.0,"role":"user","content":"hi"},
        {"id":"2","ts":2.0,"role":"openclaw","content":"hello"},
        {"id":"3","ts":3.0,"role":"user","content":"how are you"},
    ]
    msgs = context.build_turn_messages(system_prompt="SYS", summary="- talked about cats", tail=tail)
    assert msgs[0] == {"role":"system","content":"SYS"}
    assert msgs[1]["role"] == "system" and "talked about cats" in msgs[1]["content"]
    assert [m["role"] for m in msgs[2:]] == ["user","assistant","user"]
    assert msgs[-1]["content"] == "how are you"

def test_build_turn_messages_no_summary_skips_summary_block():
    msgs = context.build_turn_messages(system_prompt="SYS", summary="", tail=[{"id":"1","ts":1.0,"role":"user","content":"hi"}])
    assert [m["role"] for m in msgs] == ["system","user"]

def test_build_turn_messages_appends_action_context_last():
    msgs = context.build_turn_messages(system_prompt="S", summary="", tail=[{"id":"1","ts":1.0,"role":"user","content":"q"}], action_context="TOOLS: x")
    assert msgs[-1] == {"role":"system","content":"TOOLS: x"}

def test_build_turn_messages_drops_blank_tail_entries():
    tail=[{"id":"1","ts":1.0,"role":"user","content":"  "},{"id":"2","ts":2.0,"role":"user","content":"real"}]
    msgs = context.build_turn_messages(system_prompt="S", summary="", tail=tail)
    assert [m["content"] for m in msgs if m["role"]!="system"] == ["real"]

def test_needs_compaction_counts_nonblank():
    tail = [{"content":"a"}]*21
    assert context.needs_compaction(tail, budget=20) is True
    assert context.needs_compaction([{"content":"a"}]*20, budget=20) is False
    assert context.needs_compaction([{"content":"  "}]*30, budget=20) is False
```
- [ ] **Step 2: run → FAIL** `python -m pytest tests/test_v2_context.py -v` (ModuleNotFoundError / no attribute).
- [ ] **Step 3: implement `context.py`** — pure functions exactly matching the Interfaces block. No imports beyond stdlib. Mirror the assistant-role set from `coalesce.py` (`_ASSISTANT_ROLES`).
- [ ] **Step 4: run → PASS.**

---

## Task 2: `v2_conversation_summary` table + jobs_store row CAS

**Files:**
- Create: `backend/alembic/versions/0016_v2_conversation_summary.py` (`down_revision="0015_v2_worker_heartbeats"`)
- Modify: `backend/model_api_runtime/v2/jobs_store.py` (add `get_summary_row`, `upsert_summary_row_cas`)
- Test: `tests/test_v2_summary_store.py`

**Interfaces:**
- Table `v2_conversation_summary(user_id TEXT PK → users(user_id) ON DELETE CASCADE, summary_envelope JSONB, watermark_ts DOUBLE PRECISION NOT NULL DEFAULT 0, version INT NOT NULL DEFAULT 0, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())`.
- Produces (mirror the `_pool().connection()` pattern used by `enqueue_job`/`get_runtime_state`):
  - `get_summary_row(user_id) -> dict | None` — `{"summary_envelope": dict|None, "watermark_ts": float, "version": int}` or `None` if no row.
  - `upsert_summary_row_cas(user_id, *, summary_envelope: dict, watermark_ts: float, expected_version: int) -> bool` — if `expected_version == 0`: `INSERT ... (version=1) ON CONFLICT (user_id) DO NOTHING` → return `rowcount == 1`. Else: `UPDATE ... SET summary_envelope=%s, watermark_ts=%s, version=version+1, updated_at=now() WHERE user_id=%s AND version=%s` → return `rowcount == 1`. (0 rows ⇒ lost the CAS race ⇒ caller aborts this fold.) Store `summary_envelope` via `psycopg.types.json.Jsonb(...)` like other JSONB writes in the module.

**Steps:**
- [ ] **Step 1: migration 0016** (mirror `0015_v2_worker_heartbeats.py` style: `_UP`/`_DOWN` SQL, `op.execute`). Run `python -m alembic upgrade head` from the alembic dir (see `tests/test_v2_jobs_migration.py` for how migration tests bootstrap the DB); confirm the table exists.
- [ ] **Step 2: failing test**
```python
# tests/test_v2_summary_store.py  (DB test — uses the same fixtures as test_v2_jobs_store.py)
# seed a user row first (see test_hosted_runtime_mode._seed_bare_user pattern), then:
def test_get_missing_returns_none(...): assert jobs_store.get_summary_row("u_sum_1") is None
def test_first_write_inserts_version1(...):
    ok = jobs_store.upsert_summary_row_cas("u_sum_2", summary_envelope={"body_ct":"x"}, watermark_ts=5.0, expected_version=0)
    assert ok is True
    row = jobs_store.get_summary_row("u_sum_2")
    assert row["version"] == 1 and row["watermark_ts"] == 5.0 and row["summary_envelope"] == {"body_ct":"x"}
def test_first_write_conflict_returns_false(...):
    jobs_store.upsert_summary_row_cas("u_sum_3", summary_envelope={"a":1}, watermark_ts=1.0, expected_version=0)
    assert jobs_store.upsert_summary_row_cas("u_sum_3", summary_envelope={"a":2}, watermark_ts=2.0, expected_version=0) is False  # row exists → DO NOTHING
def test_cas_update_succeeds_on_matching_version(...):
    jobs_store.upsert_summary_row_cas("u_sum_4", summary_envelope={"a":1}, watermark_ts=1.0, expected_version=0)  # version→1
    assert jobs_store.upsert_summary_row_cas("u_sum_4", summary_envelope={"a":2}, watermark_ts=9.0, expected_version=1) is True
    row = jobs_store.get_summary_row("u_sum_4"); assert row["version"] == 2 and row["watermark_ts"] == 9.0
def test_cas_update_fails_on_stale_version(...):
    jobs_store.upsert_summary_row_cas("u_sum_5", summary_envelope={"a":1}, watermark_ts=1.0, expected_version=0)  # version→1
    assert jobs_store.upsert_summary_row_cas("u_sum_5", summary_envelope={"a":9}, watermark_ts=9.0, expected_version=7) is False  # stale
    assert jobs_store.get_summary_row("u_sum_5")["version"] == 1  # unchanged
```
- [ ] **Step 3: run → FAIL.**
- [ ] **Step 4: implement `get_summary_row` + `upsert_summary_row_cas`** in jobs_store.py.
- [ ] **Step 5: run → PASS**, plus `tests/test_v2_jobs_migration.py` still green.

---

## Task 3: `read_tail` (both-roles windowed) + TurnDeps

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py` (add `read_tail` field to `TurnDeps`)
- Modify: `backend/model_api_runtime/v2/serve_worker.py` (implement `_read_tail`, wire it into `build_production_deps`)
- Test: `tests/test_v2_serve_worker.py` (extend)

**Interfaces:**
- `TurnDeps.read_tail: Callable[[str, float, int], list[dict]]` — `(user_id, after_ts, limit) -> [{"id","ts","role","content"}]`: the most recent `limit` chat messages with `ts > after_ts`, **BOTH roles**, chronological, each enclave-decrypted. Default-None NOT allowed here (required for chat turns) — but keep existing tests working by adding it to `build_production_deps` and any test `TurnDeps(...)` constructions.
- `serve_worker._read_tail(user_id, after_ts, limit)` — **mirror `_read_messages`** (serve_worker.py:102-142) but: (a) do NOT slice at last-assistant; take the trailing window; (b) do NOT skip non-user rows — decrypt assistant rows too (they carry `body_ct`/`K_enclave`); set `role` from `_norm_role(m.get("role"))` where assistant roles → `"assistant"`; (c) filter `ts > after_ts`; (d) keep only the last `limit` after filtering; (e) reuse the same `core_enclave._decrypt_envelope_via_enclave(m, None, purpose="v2_chat_read", runtime_token=token)` call and the same image-row handling (`content_type=="image"` → `"[image]"`).

**Steps:**
- [ ] **Step 1: failing test** — extend `tests/test_v2_serve_worker.py` with a fake store whose `chat_messages` has interleaved user+assistant rows (mirror how existing serve_worker tests fake the store + monkeypatch `core_enclave._decrypt_envelope_via_enclave`). Assert `_read_tail(uid, after_ts=0.0, limit=10)` returns BOTH roles in ts order; assert `after_ts` filters older rows; assert `limit` caps to the newest N.
- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement `_read_tail`** + add `read_tail` to `TurnDeps` + wire into `build_production_deps`. Update any existing `TurnDeps(...)` test constructors that now need the new required field (grep `TurnDeps(` in tests).
- [ ] **Step 4: run → PASS**, plus `tests/test_v2_dependency_direction.py` green (worker.py gained only a dataclass field, no hosted import).

---

## Task 4: `read_summary` / `write_summary` (encrypt local + decrypt enclave + CAS) + TurnDeps

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py` (add `read_summary`, `write_summary` to `TurnDeps`)
- Modify: `backend/model_api_runtime/v2/serve_worker.py` (implement `_read_summary`, `_write_summary`, wire them)
- Test: `tests/test_v2_serve_worker.py` (extend)

**Interfaces:**
- `TurnDeps.read_summary: Callable[[str], tuple[str, float, int]]` — `user_id -> (summary_plaintext, watermark_ts, version)`; `("", 0.0, 0)` when no row.
- `TurnDeps.write_summary: Callable[[str, str, float, int], bool]` — `(user_id, summary, watermark_ts, expected_version) -> bool` (True if the CAS write landed).
- `serve_worker._read_summary(user_id)` — `row = jobs_store.get_summary_row(user_id)`; if `None` → `("", 0.0, 0)`; else decrypt `row["summary_envelope"]` via `core_enclave._decrypt_envelope_via_enclave(env, None, purpose="v2_summary_read", runtime_token=_mint_runtime_token(user_id)).decode("utf-8")` → `(plaintext, row["watermark_ts"], row["version"])`. If `summary_envelope` is None/empty → `("", row["watermark_ts"], row["version"])`.
- `serve_worker._write_summary(user_id, summary, watermark_ts, expected_version)` — `store = core_store.get_store(user_id)`; `env, err = core_envelope._build_shared_envelope_for_store(store, summary.encode("utf-8"))`; if `env is None` → log + return `False`; else `return jobs_store.upsert_summary_row_cas(user_id, summary_envelope=env, watermark_ts=watermark_ts, expected_version=expected_version)`. (Encryption is local; the CAS is the whole atomic write.)

**Steps:**
- [ ] **Step 1: failing test** — in `tests/test_v2_serve_worker.py`: monkeypatch `jobs_store.get_summary_row`/`upsert_summary_row_cas` + `core_enclave._decrypt_envelope_via_enclave` + `core_envelope._build_shared_envelope_for_store`. Assert: missing row → `("",0.0,0)`; present row → decrypts to plaintext + returns watermark/version; `_write_summary` builds an envelope and calls `upsert_summary_row_cas` with the right `expected_version`, returns its bool; `env is None` → returns False without calling CAS.
- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement** `_read_summary`/`_write_summary` + add the two `TurnDeps` fields + wire into `build_production_deps`. Both enclave-bound reads happen under `ENCLAVE_SEMAPHORE` at the worker call site (Task 6), not here.
- [ ] **Step 4: run → PASS**, dep-direction green.

---

## Task 5: responder consumes summary + tail

**Files:**
- Modify: `backend/model_api_runtime/v2/responder.py`
- Test: `tests/test_v2_responder.py` (update)

**Interfaces:**
- Consumes: `context.build_turn_messages` (Task 1).
- Changed: `async def respond(*, provider_config, summary: str, tail: list[dict], action_results: dict | None = None) -> str`. It builds `action_context` from `_fold_action_results(action_results)` (existing, capped at `_ACTION_CONTEXT_CHAR_CAP=8000`, `json.dumps(...)`), then `messages = context.build_turn_messages(system_prompt=_SYSTEM_PROMPT, summary=summary, tail=tail, action_context=action_context)`, then the existing `provider_client.reliable_chat_completion_async(provider_config, messages, max_tokens=_MAX_TOKENS, temperature=_TEMPERATURE, timeout=_TIMEOUT_SEC)` call + `ResponderError("empty_reply")` on blank. Raise `ResponderError("no_user_messages")` if `tail` has no non-blank entries (preserve the existing guard, now against `tail` instead of `coalesced_messages`).
- **Removed params:** `coalesced_messages`, `runtime_state` (assembly now comes from summary+tail; the caller passes those instead). The old user-only filtering in `_build_messages` is deleted — `context.build_turn_messages` owns assembly.

**Steps:**
- [ ] **Step 1: update tests** in `tests/test_v2_responder.py` — call `respond(provider_config=fake, summary="- prior", tail=[{"id":"1","ts":1.0,"role":"user","content":"hi"},{"id":"2","ts":2.0,"role":"openclaw","content":"hey"},{"id":"3","ts":3.0,"role":"user","content":"now"}], action_results=None)`; monkeypatch `provider_client.reliable_chat_completion_async` (async) to capture `messages` and return `{"reply":"ok"}`. Assert the captured messages contain the summary system block, both roles from the tail, and system prompt first. Add: empty/blank tail → `ResponderError`. Keep the BYOK/empty-reply tests.
- [ ] **Step 2: run → FAIL** (signature mismatch).
- [ ] **Step 3: implement** the new `respond`/assembly; delete the dead user-only `_build_messages` internals (fold into `context.build_turn_messages`); keep `_fold_action_results`, `_SYSTEM_PROMPT`, constants, `ResponderError`.
- [ ] **Step 4: run → PASS** (`tests/test_v2_responder.py`). Worker call-site is updated in Task 6 — expect `tests/test_v2_worker.py` to need Task 6 before it goes green; note this in the report.

---

## Task 6: `compaction.py` — append-and-merge fold via BYOK LLM

**Files:**
- Create: `backend/model_api_runtime/v2/compaction.py`
- Test: `tests/test_v2_compaction.py`

**Interfaces:**
- Produces: `async def compact(*, provider_config, current_summary: str, old_messages: list[dict], llm) -> str` — where `llm` is an async callable with the `reliable_chat_completion_async(provider_config, messages, **kw) -> dict` shape. Builds a fold prompt: a system instruction to **produce NEW itemized bullet lines summarizing `old_messages`, to be APPENDED to the existing summary — do not rewrite or restate existing items, output only the new bullets**; a user message containing the current summary (for context, "do not repeat these") + the old messages rendered as `role: content` lines. Calls `llm(provider_config, messages, max_tokens=500, temperature=0.3, timeout=60.0)`, takes `result["reply"]`, and returns `current_summary + "\n" + new_bullets` (append-and-merge; if `current_summary` empty, just the new bullets; strip trailing whitespace). If the LLM returns blank, return `current_summary` unchanged (no-op fold, caller will not advance watermark — see Task 7).

**Steps:**
- [ ] **Step 1: failing test**
```python
# tests/test_v2_compaction.py
import asyncio, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "backend"))
from model_api_runtime.v2 import compaction

def _fake_llm_returning(text):
    async def _llm(cfg, messages, **kw):
        _fake_llm_returning.seen = messages
        return {"reply": text}
    return _llm

def test_compact_appends_new_bullets_preserving_old():
    llm = _fake_llm_returning("- user asked about dogs")
    out = asyncio.run(compaction.compact(provider_config=object(), current_summary="- talked about cats",
                                         old_messages=[{"role":"user","content":"tell me about dogs"}], llm=llm))
    assert "- talked about cats" in out and "- user asked about dogs" in out   # OLD preserved, NEW appended
    assert out.index("cats") < out.index("dogs")                              # append order

def test_compact_empty_summary_is_just_new():
    llm = _fake_llm_returning("- first thing")
    out = asyncio.run(compaction.compact(provider_config=object(), current_summary="",
                                         old_messages=[{"role":"user","content":"x"}], llm=llm))
    assert out.strip() == "- first thing"

def test_compact_blank_llm_reply_is_noop():
    llm = _fake_llm_returning("   ")
    out = asyncio.run(compaction.compact(provider_config=object(), current_summary="- keep me",
                                         old_messages=[{"role":"user","content":"x"}], llm=llm))
    assert out == "- keep me"

def test_compact_passes_provider_config_and_old_messages_to_llm():
    llm = _fake_llm_returning("- ok")
    cfg = object()
    asyncio.run(compaction.compact(provider_config=cfg, current_summary="", old_messages=[{"role":"user","content":"SECRET_MARKER"}], llm=llm))
    dumped = str(_fake_llm_returning.seen)
    assert "SECRET_MARKER" in dumped   # old messages reach the LLM
```
- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement `compact`** exactly per the Interfaces block. No hosted/enclave imports. `llm` is injected (the worker passes `provider_client.reliable_chat_completion_async`).
- [ ] **Step 4: run → PASS.**

---

## Task 7: worker dispatch by lane + integration

**Files:**
- Modify: `backend/model_api_runtime/v2/worker.py` (`process_job`: chat path uses full context + enqueues compaction; new `maintenance` path runs compaction)
- Test: `tests/test_v2_worker.py` (extend), `tests/test_v2_compaction_integration.py` (new, DB)

**Interfaces:**
- Consumes: `context.build_turn_messages`/`needs_compaction` (T1), `jobs_store.get_summary_row`/`upsert_summary_row_cas`/`enqueue_job` (T2), `TurnDeps.read_tail`/`read_summary`/`write_summary` (T3/T4), `responder.respond(summary=,tail=,...)` (T5), `compaction.compact` (T6), `provider_client.reliable_chat_completion_async`.
- Budgets from env (module constants): `_TAIL_BUDGET = int(os.environ.get("FEEDLING_V2_TAIL_BUDGET_MSGS","20"))`, `_TAIL_KEEP = int(os.environ.get("FEEDLING_V2_TAIL_KEEP_MSGS","10"))`, `_TAIL_HARD_CAP = int(os.environ.get("FEEDLING_V2_TAIL_HARD_CAP","60"))`.

**Chat path change** (in `process_job`, the existing `lane=="chat"` turn): after `resolve_provider` + prefetch, replace the current responder call inputs:
```python
# all three enclave-bound reads under the semaphore (spec R3)
async with enclave_sem:
    summary, watermark, _ver = await asyncio.to_thread(deps.read_summary, user_id)
    tail = await asyncio.to_thread(deps.read_tail, user_id, watermark, _TAIL_HARD_CAP)
...
reply = await v2_responder.respond(
    provider_config=provider_config, summary=summary, tail=tail,
    action_results=action_state["action_results"])
...
# after a successful reply, if over budget, enqueue a compaction job (best-effort, non-blocking)
if context.needs_compaction(tail, budget=_TAIL_BUDGET):
    try:
        await asyncio.to_thread(jobs_store.enqueue_job, user_id, "maintenance", reason="compaction")
    except Exception as e:
        log.warning("[v2.worker] enqueue compaction failed for %s: %s", user_id, e)
```
Keep `coalesce`/`last_replied_ts`/`new_last_replied` bookkeeping (worker.py:211 early-exit + :280) UNCHANGED — they still gate "is there anything new" and drive the reply cursor. The tail feeds the prompt; coalesce feeds the bookkeeping (spec §2 "Tail vs. coalesce").

**New maintenance path** (branch at the top of `process_job` on `lane == "maintenance"`, before the chat turn logic):
```python
if lane == "maintenance":
    await asyncio.to_thread(jobs_store.mark_running, job_id)
    async with enclave_sem:
        provider_config, meta = await asyncio.to_thread(deps.resolve_provider, user_id)  # BYOK, single decrypt
    if provider_config is None:
        await asyncio.to_thread(jobs_store.mark_failed, job_id, f"provider_unavailable: {meta.get('error')}")
        return "failed"
    async with enclave_sem:
        summary, watermark, version = await asyncio.to_thread(deps.read_summary, user_id)
        tail = await asyncio.to_thread(deps.read_tail, user_id, watermark, 10_000)  # everything past watermark
    if len(tail) <= _TAIL_KEEP:
        await asyncio.to_thread(jobs_store.mark_completed, job_id)   # nothing to fold
        return "completed"
    old = tail[: len(tail) - _TAIL_KEEP]          # oldest to fold; keep the newest _TAIL_KEEP verbatim
    new_watermark = old[-1]["ts"]
    new_summary = await v2_compaction.compact(
        provider_config=provider_config, current_summary=summary, old_messages=old,
        llm=provider_client.reliable_chat_completion_async)
    if new_summary.strip() == summary.strip():    # blank/no-op fold → don't advance
        await asyncio.to_thread(jobs_store.mark_completed, job_id); return "completed"
    async with enclave_sem:
        ok = await asyncio.to_thread(deps.write_summary, user_id, new_summary, new_watermark, version)  # CAS
    await asyncio.to_thread(jobs_store.mark_completed if ok else jobs_store.mark_failed, job_id, *( () if ok else ("summary_cas_lost",) ))
    return "completed" if ok else "failed"
    # NO chat bubble written anywhere in this branch (no-filler).
```
(Adapt the exact `mark_*` call arity to the existing helpers; the point is: resolve → read → fold → CAS-write, no bubble.)

**Steps:**
- [ ] **Step 1: failing unit test** in `tests/test_v2_worker.py`: a fake `TurnDeps` with stub `read_summary`/`read_tail`/`write_summary`/`resolve_provider`. (a) a `lane="chat"` job calls `respond` with the summary+tail from the deps (spy respond); and when the tail exceeds `_TAIL_BUDGET`, an `enqueue_job(user_id,"maintenance",...)` happens (spy enqueue). (b) a `lane="maintenance"` job calls `compact` then `write_summary` with `expected_version` from `read_summary`, and writes NO reply (assert `_write_encrypted_reply`/`append_reply` never called).
- [ ] **Step 2: run → FAIL.**
- [ ] **Step 3: implement** the maintenance branch + chat-path context wiring in `process_job`.
- [ ] **Step 4: run unit tests → PASS** (`tests/test_v2_worker.py`, `tests/test_v2_responder.py` now green too).
- [ ] **Step 5: integration test** `tests/test_v2_compaction_integration.py` (DB, PG :55432): seed a user with >`_TAIL_BUDGET` chat messages (both roles); run the chat turn (with a fake provider/enclave via the deps) → assert a `maintenance` job row is enqueued. Then run the maintenance job → assert `get_summary_row` now has a non-null envelope + advanced `watermark_ts` + `version==1`, and a subsequent `read_tail(user_id, watermark, cap)` returns fewer rows. Use the existing DB-test fixtures + monkeypatch the enclave decrypt/provider LLM (no live enclave/provider).
- [ ] **Step 6: run → PASS.**

---

## Final whole-plan review

After Tasks 1-7: dispatch one whole-diff reviewer (most capable model) over the condition-2 diff. Verify cross-cutting: **BYOK-only** (compaction + responder both use user provider_config), **no-filler** (maintenance branch writes no bubble), **dependency direction** (context/compaction/worker no hosted import; serve_worker-only wiring), **single-decrypt + ENCLAVE_SEMAPHORE** (all summary/tail reads + provider resolve under the semaphore; one provider decrypt per job), **watermark idempotency** (CAS version guard; re-run folds nothing twice), **coalesce bookkeeping intact** (reply cursor + early-exit unchanged). Run the full backend suite (expect 2477 + new tests passed / 7 pre-existing). Record results in `.superpowers/sdd/progress.md`. Leave everything uncommitted.
```

## Self-Review

**Spec coverage:** §2 context model → T1(assembly)+T3(read_tail)+T5(responder). §2.1 watermark → co-located (T2 table). §2.2 storage → T2(table)+T4(encrypt/decrypt). §3 compaction → T6(fold)+T7(maintenance path). §3.1 budgets → T7 env consts. §3.2 idempotency/CAS → T2(CAS)+T7(version guard). §4 files/interfaces → all tasks. §5 invariants → final review. §6 testing → per-task + T7 integration. §7 out-of-scope → nothing planned for caching/loop/admission. Covered.

**Placeholder scan:** no TBD/“handle edge cases”/“similar to”. The one adapt-note ("adapt the exact mark_* arity") points at existing helpers, not a gap.

**Type consistency:** `read_summary → (str,float,int)` used consistently (T4 produces, T7 consumes `summary,watermark,version`). `write_summary(user_id,summary,watermark,expected_version)->bool` matches T4↔T7. `upsert_summary_row_cas(...expected_version)->bool` matches T2↔T4. `respond(summary=,tail=,action_results=)` matches T5↔T7. `build_turn_messages(system_prompt,summary,tail,action_context)` matches T1↔T5. `read_tail(user_id,after_ts,limit)` matches T3↔T7. Consistent.
