"""Real disposable PG history/compaction plus real provider HTTP serialization.
Provider responses are scripted; the plaintext World Book readside is real.
Profile selection is a fixture. This is not live model-adherence evidence.
"""

import asyncio
import copy
import json
import time

import httpx
import pytest

import conftest
import db
import provider_client
import worldbook_readside_core
from core import envelope
from model_api_runtime.v2 import worker, jobs_store, profile_store, serve_worker
from test_v2_worker_tool_loop import _reset, _patch_real_write, _deps


@pytest.mark.parametrize("always_on", [False, True])
@pytest.mark.parametrize("voice", [False, True])
def test_direct_worldbook_survives_history_compaction_and_clear(
    monkeypatch, tmp_path, always_on, voice
):
    uid = f"t801_direct_{always_on}_{voice}"
    conftest.seed_user(uid)
    _reset(uid)
    _patch_real_write(monkeypatch)
    monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "off")
    monkeypatch.setattr(worker, "_PROFILE_ENABLED", True)
    monkeypatch.setattr(envelope, "PLAINTEXT_WRITES_ACCEPTED", True)
    monkeypatch.setattr(envelope, "resolve_content_encryption", lambda _: "off")
    lore = "T801_PRIVATE_LAB_LORE: copper feather unlocks the moon-library."
    entry = {
        "id": "lore",
        "name": "Moon library",
        "enabled": True,
        "alwaysOn": always_on,
        "keywords": ["moon-library"],
        "content": lore,
    }
    assert db.world_book_upsert(
        uid,
        "lore",
        "2026-10-07",
        {"id": "lore", "owner_user_id": uid, "body": json.dumps(entry)},
    )
    original = copy.deepcopy(db.world_book_load(uid))
    base_time = time.time()
    n = 0

    def append(text, role="user"):
        nonlocal n
        n += 1
        doc = {
            "id": f"m{n}",
            "role": role,
            "source": "model_api",
            "body": text,
            "owner_user_id": uid,
        }
        if voice and role == "user":
            doc.update(voice_call_id="matrix-voice", voice_turn_id=f"voice-{n}")
        db.chat_append_strict(uid, doc["id"], base_time + n, doc, 10000)

    def rows(after=0, through=None, limit=None):
        found = db.chat_messages_after_seq(
            uid, after, limit=None, through_seq=through, exclude_synthetic_sources=True
        )
        found = found[-limit:] if limit else found
        return [dict(r, content=r.get("body", "")) for r in found]

    def summary(_):
        s = jobs_store.get_summary_frontier_state(uid)
        if not s:
            return "", 0.0, 0, 0
        return (
            s["summary_envelope"].get("body", ""),
            float(s["watermark_ts"]),
            int(s["version"]),
            int(s["watermark_seq"]),
        )

    evidence = []

    def write_plain(store, text, *, extra=None):
        return store.append_chat(
            "openclaw",
            "model_api",
            {"body": text, "owner_user_id": uid},
            strict=True,
            extra=extra,
        )

    monkeypatch.setattr(worker, "_write_encrypted_reply", write_plain)
    monkeypatch.setattr(
        worldbook_readside_core,
        "post_enclave_worldbook_match",
        lambda *a, **kw: pytest.fail("plaintext worldbook must not call enclave"),
    )
    for stage in [
        "short",
        "long_before_compaction",
        "after_compaction",
        "followup_1",
        "followup_2",
        "cleared_chat",
    ]:
        if stage == "long_before_compaction":
            for i in range(24):
                append(
                    "Neutral synthetic conversation detail "
                    + str(i)
                    + " "
                    + ("ordinary setting. " * 40),
                    "user" if i % 2 == 0 else "openclaw",
                )
        if stage == "after_compaction":
            snapshot = copy.deepcopy(db.chat_messages_after_seq(uid, 0, limit=None))
            # Invoke real metadata compactor/CAS. Job is never claimed for a provider call.
            job_id, _ = jobs_store.enqueue_job(uid, "maintenance", reason="t801-matrix")
            deps = worker.TurnDeps(
                read_messages=lambda _: pytest.fail("compaction plaintext read"),
                resolve_provider=lambda _: pytest.fail("compaction model"),
                mint_enclave_token=lambda _: pytest.fail("compaction enclave"),
                append_summary_segment=serve_worker._append_summary_segment,
                read_summary_frontier_metadata=serve_worker._read_summary_frontier_metadata,
            )
            assert (
                asyncio.run(
                    worker._run_compaction(job_id, uid, deps, asyncio.Semaphore(1))
                )
                == "completed"
            )
            assert db.chat_messages_after_seq(uid, 0, limit=None) == snapshot
            assert summary(uid)[3] > 0
        if stage == "cleared_chat":
            # Strictly synthetic owner, chat table only. Profile/worldbook/runtime/summary retained.
            with db.get_pool().connection() as conn:
                conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))
        append("Continue the moon-library conversation.")
        deps = _deps(messages=rows())
        deps.read_summary_with_seq = summary
        deps.read_tail_after_seq = lambda _u, after, limit, through_seq=None: rows(
            after, through_seq, limit
        )
        deps.select_profile_for_turn = (
            lambda *_a, **_kw: profile_store.ProfilePromptSelection(
                summary="",
                memory="T801_PROFILE_MEMORY",
                style="T801_PROFILE_STYLE",
                used_profile=True,
            )
        )
        deps.append_summary_segment = serve_worker._append_summary_segment
        deps.read_worldbook_context = serve_worker._read_worldbook_context
        payloads = []

        def handle(request):
            body = json.loads(request.content)
            payloads.append(body)
            item = {"type": "text", "text": "Synthetic neutral answer."}
            return httpx.Response(
                200,
                json={
                    "id": "synthetic",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-test",
                    "content": [item],
                    "stop_reason": (
                        "tool_use" if item["type"] == "tool_use" else "end_turn"
                    ),
                    "usage": {"input_tokens": 100, "output_tokens": 10},
                },
            )

        async def execute():
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handle)
            ) as client:
                monkeypatch.setattr(
                    provider_client, "_async_http_client", lambda: client
                )
                # Pending synthetic maintenance successors aren't provider chat jobs.
                with db.get_pool().connection() as conn:
                    conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (uid,))
                jobs_store.enqueue_job(uid, "chat")
                job = jobs_store.claim_next_job("t801-matrix")
                assert job and job["user_id"] == uid
                return await worker.process_job(
                    job,
                    deps,
                    provider_config=deps.resolve_provider(uid)[0],
                    api_key=None,
                    runtime_token="synthetic",
                )

        assert asyncio.run(execute()) == "completed"
        first = json.dumps(payloads[0])
        last = json.dumps(payloads[-1])
        assert lore in first
        assert lore in last
        assert len(payloads) == 1
        assert (
            "worldbook_match" in first
            and "T801_PROFILE_MEMORY" in first
            and "T801_PROFILE_STYLE" in first
        )
        assert db.world_book_load(uid) == original
        evidence.append(
            {
                "stage": stage,
                "rows": len(rows()),
                "watermark": summary(uid)[3],
                "schema_present": True,
                "tool_called": False,
                "first_contains_lore": lore in first,
                "last_contains_lore": lore in last,
                "worldbook_unchanged": True,
                "requests": payloads,
            }
        )
    assert evidence[1]["watermark"] == 0
    (tmp_path / f"DIRECT-MATRIX-{always_on}-{voice}.json").write_text(
        json.dumps(
            {
                "limitations": "HTTP MockTransport, scripted model choice; synthetic plaintext readside; real local PG history/compaction; clear deletes only this owner chat rows",
                "cases": evidence,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
