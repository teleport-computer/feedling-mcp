"""Genesis v2 — Step 3/4 acceptance net (e2e skeleton).

Codex's call: before rewriting the genesis MAIN PATH (foreground reducer +
background continuation), stand up the 6 scenarios so Step 3/4 can't silently
bypass the contract (greeting at done, replace-all, re-running done chunks, losing
resume, duplicate cards).

How this nets the real worker: `_fg_pass` / `_bg_pass` below are a REFERENCE
implementation that drives `genesis.checkpoint` + Step-1 `reliable_chat_completion`
the way Step 3/4 must. The 6 tests assert behaviour against this reference (green
now). When Step 3/4 land the REAL worker entry points, point these same assertions
at them (swap `_fg_pass`/`_bg_pass`) — the assertions, not the fake, are the net.
"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import provider_client as pc  # noqa: E402
from provider_client import ProviderError, reliable_chat_completion  # noqa: E402
from genesis import checkpoint as ckpt  # noqa: E402

CORE_N = 3  # foreground writes at most this many "core" memories


def _candidate(i):
    return {"candidate_id": f"cand_{i}", "source_ref": f"hist:{i}"}


# --- reference worker (Step 3/4 must behave like this) -----------------------

def _fg_pass(cp, candidates):
    """Foreground: write identity baseline (implied) + up to CORE_N core memories,
    mark them foreground_written, flip phase to FOREGROUND_READY (greeting may fire)."""
    for c in candidates[:CORE_N]:
        cp = ckpt.upsert_task(
            cp, task_id="fact_write", chunk_id=c["candidate_id"], status=ckpt.TASK_DONE,
            source_ref=c["source_ref"], candidate_id=c["candidate_id"],
            written_memory_ids=[f"mem_{c['candidate_id']}"], foreground_written=True,
        )
    return ckpt.set_phase(cp, ckpt.PHASE_FOREGROUND_READY)


def _bg_pass(cp, candidates, completion):
    """Background: process every candidate not already written. Dedup against what
    foreground / earlier passes wrote. Each LLM call goes through the Step-1 retry
    wrapper; a provider_config failure blocks (resumable), it does NOT fail the job."""
    cp = ckpt.set_phase(cp, ckpt.PHASE_BACKGROUND_PROCESSING)
    for c in candidates:
        if ckpt.is_task_done(cp, "fact_write", c["candidate_id"]):
            continue  # large-import / resume: never re-run a done chunk
        if ckpt.should_skip_candidate(cp, source_ref=c["source_ref"], candidate_id=c["candidate_id"]):
            continue  # contract #1/#2: foreground (or earlier) already wrote it
        try:
            completion()  # the real worker calls reliable_chat_completion here
        except ProviderError as exc:
            cls = pc.classify_provider_error(exc)
            if cls == "provider_config":
                cp = ckpt.upsert_task(
                    cp, task_id="fact_write", chunk_id=c["candidate_id"],
                    status=ckpt.TASK_PROVIDER_CONFIG_BLOCKED, error_class="provider_config",
                    error_type=type(exc).__name__, error_message=str(exc),
                    provider_status_code=exc.status_code,
                    source_ref=c["source_ref"], candidate_id=c["candidate_id"], bump_attempts=True,
                )
                return ckpt.mark_provider_config_blocked(cp, reason=str(exc))  # stop; user must fix
            cp = ckpt.upsert_task(
                cp, task_id="fact_write", chunk_id=c["candidate_id"],
                status=ckpt.TASK_TRANSIENT_FAILED, error_class="transient_exhausted",
                error_type=type(exc).__name__, error_message=str(exc),
                source_ref=c["source_ref"], candidate_id=c["candidate_id"], bump_attempts=True,
            )
            continue
        cp = ckpt.upsert_task(
            cp, task_id="fact_write", chunk_id=c["candidate_id"], status=ckpt.TASK_DONE,
            source_ref=c["source_ref"], candidate_id=c["candidate_id"],
            written_memory_ids=[f"mem_{c['candidate_id']}"],
        )
    if not ckpt.pending_tasks(cp):
        cp = ckpt.set_phase(cp, ckpt.PHASE_DONE)
    return cp


# --- the 6 scenarios ---------------------------------------------------------

def test_1_small_import_happy_path():
    cands = [_candidate(i) for i in range(2)]
    cp = _fg_pass(ckpt.new_checkpoint(now=0.0), cands)
    assert cp["phase"] == ckpt.PHASE_FOREGROUND_READY
    assert ckpt.greeting_allowed(cp["phase"])               # greeting can fire now
    assert len(ckpt.all_written_memory_ids(cp)) == 2        # core written
    cp = _bg_pass(cp, cands, completion=lambda: "ok")
    assert cp["phase"] == ckpt.PHASE_DONE


def test_2_large_import_checkpoint_no_rerun():
    cands = [_candidate(i) for i in range(8)]
    cp = _fg_pass(ckpt.new_checkpoint(now=0.0), cands)
    calls = {"n": 0}
    def comp():
        calls["n"] += 1
    cp = _bg_pass(cp, cands, completion=comp)
    assert cp["phase"] == ckpt.PHASE_DONE
    # 3 written by foreground, 5 by background → background only called LLM 5 times
    assert calls["n"] == 5
    # re-running background (resume-style) must NOT re-call any done chunk
    calls["n"] = 0
    _bg_pass(cp, cands, completion=comp)
    assert calls["n"] == 0


def test_3_transient_retry_does_not_fail_job(monkeypatch):
    cands = [_candidate(0)]
    cp = ckpt.new_checkpoint(now=0.0)  # no foreground → background processes it
    seq = [ProviderError("timeout", status_code=503),
           ProviderError("timeout", status_code=503), "ok"]
    calls = {"n": 0}
    def fake(*a, **k):
        item = seq[min(calls["n"], len(seq) - 1)]; calls["n"] += 1
        if isinstance(item, BaseException):
            raise item
        return item
    monkeypatch.setattr(pc, "chat_completion", fake)
    cp = _bg_pass(cp, cands, completion=lambda: reliable_chat_completion(base_delay_sec=0.0))
    assert cp["phase"] == ckpt.PHASE_DONE          # retry absorbed the blips
    assert calls["n"] == 3
    assert ckpt.is_task_done(cp, "fact_write", "cand_0")


def test_4_provider_config_blocks_and_is_resumable():
    cands = [_candidate(0), _candidate(1)]
    cp = ckpt.new_checkpoint(now=0.0)
    # candidate 0 done, candidate 1 hits 402 (out of credits)
    state = {"i": 0}
    def comp():
        if state["i"] == 1:
            raise ProviderError("402 out of credits", status_code=402)
        state["i"] += 1
    cp = _bg_pass(cp, cands, completion=comp)
    assert cp["phase"] == ckpt.PHASE_PROVIDER_CONFIG_BLOCKED
    assert cp["resumable"] is True
    assert ckpt.is_task_done(cp, "fact_write", "cand_0")    # earlier work kept
    blocked = ckpt.get_task(cp, "fact_write", "cand_1")
    assert blocked["provider_status_code"] == 402           # original error kept (ops)
    assert ckpt.runnable_tasks(cp) == []                    # worker must NOT auto-run while blocked


def test_5_resume_keeps_done_runs_rest():
    cands = [_candidate(0), _candidate(1)]
    cp = ckpt.new_checkpoint(now=0.0)
    cp = ckpt.upsert_task(cp, task_id="fact_write", chunk_id="cand_0", status=ckpt.TASK_DONE,
                          source_ref="hist:0", written_memory_ids=["mem_cand_0"])
    cp = ckpt.upsert_task(cp, task_id="fact_write", chunk_id="cand_1",
                          status=ckpt.TASK_PROVIDER_CONFIG_BLOCKED, error_class="provider_config",
                          source_ref="hist:1")
    cp = ckpt.mark_provider_config_blocked(cp, reason="402")
    # user fixes provider → resume → background re-runs only the non-done one
    cp = ckpt.resume(cp)
    calls = {"n": 0}
    cp = _bg_pass(cp, cands, completion=lambda: calls.__setitem__("n", calls["n"] + 1))
    assert cp["phase"] == ckpt.PHASE_DONE
    assert calls["n"] == 1                                   # only cand_1 re-run
    assert ckpt.is_task_done(cp, "fact_write", "cand_1")


def test_6_foreground_background_dedup():
    cands = [_candidate(0), _candidate(1)]
    cp = _fg_pass(ckpt.new_checkpoint(now=0.0), cands)       # foreground writes cand_0, cand_1 (<=CORE_N)
    before = ckpt.all_written_memory_ids(cp)
    # background sees the SAME candidates → must skip (no duplicate cards)
    calls = {"n": 0}
    cp = _bg_pass(cp, cands, completion=lambda: calls.__setitem__("n", calls["n"] + 1))
    assert calls["n"] == 0                                   # nothing re-derived
    assert ckpt.all_written_memory_ids(cp) == before        # no duplicate memory ids
