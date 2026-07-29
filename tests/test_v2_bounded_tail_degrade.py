"""Option A (graceful degradation): a chat turn must NEVER fail because the
summary compaction is behind. Instead of requiring every post-watermark row in
the prompt, the turn serves a bounded recency tail and discloses the dropped
span as a coverage hole; the catch-up failure kicks the background maintenance
chain rather than failing the reply. Deadlock repro: usr_7f30/81a0/90184 on prod.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
import provider_client
from core import store as core_store
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import worker

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-sonnet-4-test", api_key="sk-test", base_url="")


def _reset(uid: str) -> None:
    conftest.seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM v2_conversation_summary_segments WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))


def _seed(uid: str, n: int) -> list[dict]:
    out = []
    for i in range(n):
        mid = f"{uid}-m{i}"
        role = "user" if i % 2 == 0 else "openclaw"
        db.chat_append_strict(
            uid, mid, float(i + 1),
            {"id": mid, "role": role, "body_ct": f"ct-{i}"},
            core_store.MAX_CHAT_MESSAGES,
        )
        out.append({"id": mid, "seq": int(db.chat_seq_for_msg_id(uid, mid)), "ts": float(i + 1)})
    return out


def _deps() -> worker.TurnDeps:
    def _tail_after(uid, after_seq, limit, *, through_seq=None):
        return db.chat_messages_after_seq(
            uid, after_seq, limit=limit, oldest_first=False, through_seq=through_seq)

    def _summary_with_seq(uid):
        row = jobs_store.get_summary_row(uid)
        if not row:
            return "", 0.0, 0, 0
        env = row["summary_envelope"] or {}
        return str(env.get("plaintext") or ""), row["watermark_ts"], row["version"], row["watermark_seq"]

    return worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        read_summary_with_seq=_summary_with_seq,
        read_tail_after_seq=_tail_after,
    )


def _run(uid, through_seq, *, tail_cap):
    return asyncio.run(
        worker._read_seq_adaptive_prompt_context(
            user_id=uid,
            deps=_deps(),
            through_seq=through_seq,
            target_turns=worker._CHAT_TAIL_MAX_TURNS,
            provider_config=_BYOK,
            enclave_sem=asyncio.Semaphore(1),
            claimed_by=None,
            job_id=None,
            add_usage=None,
            trajectory_recorder=None,
            tail_cap=tail_cap,
        )
    )


def test_bounded_tail_drops_backlog_and_discloses_the_hole():
    uid = "u_bounded_tail_hole"
    _reset(uid)
    rows = _seed(uid, 30)
    # Summary covers only the first 5 rows; 25 remain after the watermark.
    assert jobs_store.upsert_summary_row_cas(
        uid, summary_envelope={"plaintext": "- older summary"},
        watermark_ts=rows[4]["ts"], expected_version=0, watermark_seq=rows[4]["seq"])
    through = rows[-1]["seq"]

    summary, tail, _optional, _trunc, watermark_seq = _run(uid, through, tail_cap=10)

    # The tail is bounded to the newest 10 — the 15-row hole is NOT in the prompt.
    assert len(tail) == 10
    assert [r["seq"] for r in tail] == [r["seq"] for r in rows[-10:]]
    assert watermark_seq == rows[4]["seq"]
    # ...and the drop is disclosed to the model (25 backlog - 10 kept = 15).
    assert "15 earlier message" in summary
    assert "omitted" in summary and "long-term memory" in summary


def test_no_hole_when_backlog_fits_the_cap():
    uid = "u_bounded_tail_nohole"
    _reset(uid)
    rows = _seed(uid, 12)
    assert jobs_store.upsert_summary_row_cas(
        uid, summary_envelope={"plaintext": "- older summary"},
        watermark_ts=rows[1]["ts"], expected_version=0, watermark_seq=rows[1]["seq"])
    through = rows[-1]["seq"]

    summary, tail, _optional, _trunc, _wm = _run(uid, through, tail_cap=60)

    # 10 rows after the watermark, cap is 60 → everything fits, no hole disclosed.
    assert len(tail) == 10
    assert "omitted" not in summary
    assert summary == "- older summary"


def _degrade(uid):
    return asyncio.run(
        worker._ensure_prompt_coverage_or_degrade(
            uid, _deps(),
            provider_config=_BYOK,
            enclave_sem=asyncio.Semaphore(1),
            tail_limit=worker._TAIL_HARD_CAP,
            job_id=None,
            claimed_by=None,
            add_usage=None,
            trajectory_recorder=None,
            compact_through_seq=None,
        )
    )


def _maintenance_jobs(uid: str) -> int:
    with db.get_pool().connection() as conn:
        return int(conn.execute(
            "SELECT count(*) FROM agent_jobs WHERE user_id=%s AND lane='maintenance'",
            (uid,),
        ).fetchone()[0])


def test_coverage_exhaustion_degrades_and_kicks_background_compaction(monkeypatch):
    uid = "u_coverage_degrade"
    _reset(uid)
    conftest.set_v2_runtime_owner(uid)

    async def _raise_incomplete(*_a, **_k):
        raise worker.TurnError("prompt_coverage_incomplete")

    monkeypatch.setattr(worker, "_ensure_prompt_coverage", _raise_incomplete)

    assert _maintenance_jobs(uid) == 0
    # Must NOT raise — the reply is served from the bounded tail instead.
    _degrade(uid)
    # ...and the self-sustaining background drain chain was kicked.
    assert _maintenance_jobs(uid) >= 1


def test_control_flow_signals_still_propagate(monkeypatch):
    uid = "u_coverage_degrade_control_flow"
    _reset(uid)
    conftest.set_v2_runtime_owner(uid)

    async def _raise_lease(*_a, **_k):
        raise worker.LostJobLease("lease lost mid-catchup")

    monkeypatch.setattr(worker, "_ensure_prompt_coverage", _raise_lease)

    with pytest.raises(worker.LostJobLease):
        _degrade(uid)
    # A control-flow abort must NOT masquerade as a drainable backlog.
    assert _maintenance_jobs(uid) == 0
