"""Integration test (Task 7): chat-turn over budget -> maintenance enqueue -> compaction
job -> summary populated + watermark advanced, end to end through real
jobs_store/DB primitives.

Hermetic (no live enclave, no live provider network call):
- `v2_responder.respond` (the chat turn's own LLM call) is monkeypatched — its
  correctness is covered exhaustively by test_v2_responder.py; this test only
  cares that the *dispatch* (summary+tail plumbing, over-budget enqueue) wires up.
- `provider_client.reliable_chat_completion_async` (the LLM injected into
  `v2_compaction.compact`) is monkeypatched to a deterministic fake bullet —
  `compaction.compact`'s own fold logic is covered by test_v2_compaction.py.
- `read_tail`/`read_summary`/`write_summary` are simple fakes that persist
  PLAINTEXT (not a real enclave-encrypted envelope — no live enclave in this
  test process) directly into the real `v2_conversation_summary` table via the
  real `jobs_store.get_summary_row`/`upsert_summary_row_cas` — so the CAS write,
  version increment, and watermark advance are all exercised for real, only the
  encrypt/decrypt hop is faked (mirrors the same pattern test_v2_worker.py uses
  for read_messages: real coalesce/jobs_store, faked crypto/LLM boundaries).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
import provider_client
import pytest
from capabilities import registry as cap_registry
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import responder as v2_responder
from model_api_runtime.v2 import worker

_BYOK = provider_client.ProviderConfig(
    provider="anthropic", model="claude-x", api_key="sk-user-byok", base_url="")


class _FakeCapResult:
    def to_dict(self):
        return {"ok": True, "data": {}, "error": None, "trace": {}, "warnings": []}


@pytest.fixture(autouse=True)
def _clean_agent_jobs_table():
    # claim_next_job is a GLOBAL claim (no user_id filter, by design). A pending
    # job left behind by another test file (e.g. the D3 claim-reservation / wake
    # tests enqueue jobs for other users) would pollute this test's global claim
    # ordering and get picked up instead of this test's own chat job. Truncate the
    # whole table before each test here (mirrors tests/test_v2_jobs_store.py).
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(uid):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))


def _make_fake_conversation_deps(messages: list[dict]):
    """`messages`: the full plaintext conversation (both roles), any order.

    - `read_messages`: mirrors serve_worker._read_messages's shape closely
      enough for this test — everything after the last assistant message,
      user-role only (feeds v2.coalesce so the chat lane doesn't short-circuit
      on "no pending messages").
    - `read_tail`/`read_summary`/`write_summary`: see module docstring — real
      jobs_store-backed CAS, plaintext instead of a real envelope.
    """
    ordered = sorted(messages, key=lambda m: m["ts"])

    def _read_messages(uid):
        last_assistant = -1
        for i, m in enumerate(ordered):
            if m["role"] in ("assistant", "openclaw"):
                last_assistant = i
        return [m for m in ordered[last_assistant + 1:] if m["role"] == "user"]

    def _read_tail(uid, after_ts, limit):
        out = [m for m in ordered if m["ts"] > after_ts]
        return out[-limit:] if limit > 0 else []

    def _read_summary(uid):
        row = jobs_store.get_summary_row(uid)
        if row is None:
            return "", 0.0, 0
        env = row["summary_envelope"]
        if not env:
            return "", row["watermark_ts"], row["version"]
        return str(env.get("plaintext") or ""), row["watermark_ts"], row["version"]

    def _write_summary(uid, summary, watermark_ts, expected_version):
        return jobs_store.upsert_summary_row_cas(
            uid, summary_envelope={"plaintext": summary},
            watermark_ts=watermark_ts, expected_version=expected_version)

    return _read_messages, _read_tail, _read_summary, _write_summary


def test_chat_turn_over_budget_enqueues_maintenance_then_compaction_advances_watermark(monkeypatch):
    uid = "u_v2_compact_integ"
    conftest.seed_user(uid)
    _reset(uid)

    # > _TAIL_BUDGET messages, both roles, so the chat turn's read_tail is over
    # budget and triggers the best-effort maintenance enqueue. The LAST message
    # must be an unanswered `user` turn — otherwise `_read_messages` (mirroring
    # "everything after the last assistant reply") sees nothing pending and the
    # chat lane short-circuits before ever reaching the responder/enqueue step.
    n = worker._TAIL_BUDGET + 8
    messages = [
        {"id": f"m{i}", "ts": float(i + 1), "role": "user" if i % 2 == 0 else "assistant",
         "content": f"turn {i}"}
        for i in range(n)
    ]
    messages.append({"id": f"m{n}", "ts": float(n + 1), "role": "user", "content": "final unanswered"})
    read_messages, read_tail, read_summary, write_summary = _make_fake_conversation_deps(messages)

    monkeypatch.setattr(cap_registry, "run_capability", lambda *a, **k: _FakeCapResult())

    async def _fake_respond(*, provider_config, summary, tail, action_results=None, usage_out=None):
        return "model reply"

    monkeypatch.setattr(v2_responder, "respond", _fake_respond)
    monkeypatch.setattr(worker, "_write_encrypted_reply", lambda store, text: {"id": "r"})

    async def _fake_llm(cfg, msgs, **kw):
        return {"reply": "- folded integration bullet"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)

    deps = worker.TurnDeps(
        read_messages=read_messages,
        resolve_provider=lambda uid_: (_BYOK, {}),
        is_official=lambda cfg: False,
        mint_enclave_token=lambda uid_: "rt",
        read_tail=read_tail,
        read_summary=read_summary,
        write_summary=write_summary,
    )

    # --- chat turn ---
    jobs_store.enqueue_job(uid, "chat")
    chat_job = jobs_store.claim_next_job("w-chat")
    status = asyncio.run(worker.process_job(
        chat_job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))
    assert status == "completed"

    # (a) the over-budget tail must have triggered a maintenance-lane enqueue.
    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT status, lane FROM agent_jobs WHERE user_id=%s AND lane='maintenance'",
            (uid,),
        ).fetchone()
    assert row is not None, "expected a pending maintenance job after an over-budget chat turn"
    assert row[0] == "pending"
    assert jobs_store.get_summary_row(uid) is None  # not compacted yet — only enqueued

    # --- maintenance turn ---
    maint_job = jobs_store.claim_next_job("w-maint")
    assert maint_job["lane"] == "maintenance"
    status2 = asyncio.run(worker.process_job(
        maint_job, deps, provider_config=_BYOK, is_official=False, api_key=None, runtime_token="rt"))
    assert status2 == "completed"

    # (b) get_summary_row is now populated: non-null envelope, version==1,
    # watermark advanced past the oldest folded batch.
    summary_row = jobs_store.get_summary_row(uid)
    assert summary_row is not None
    assert summary_row["summary_envelope"] is not None
    assert summary_row["summary_envelope"]["plaintext"] == "- folded integration bullet"
    assert summary_row["version"] == 1
    assert summary_row["watermark_ts"] > 0.0

    with db.get_pool().connection() as conn:
        maint_row = conn.execute(
            "SELECT status FROM agent_jobs WHERE id=%s", (maint_job["id"],)
        ).fetchone()
    assert maint_row[0] == "completed"

    # (c) a follow-up read_tail(user_id, new_watermark, cap) returns fewer rows
    # than a read from the very start — the oldest batch got folded away.
    full_tail = read_tail(uid, 0.0, 10_000)
    new_tail = read_tail(uid, summary_row["watermark_ts"], 10_000)
    assert len(new_tail) < len(full_tail)
    assert len(new_tail) == worker._TAIL_KEEP
