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
    provider="anthropic",
    model="claude-sonnet-4-test",
    api_key="sk-test",
    base_url="",
)


def _reset(uid: str) -> None:
    conftest.seed_user(uid)
    with db.get_pool().connection() as conn:
        conn.execute(
            "DELETE FROM v2_conversation_summary_segments WHERE user_id=%s", (uid,)
        )
        conn.execute("DELETE FROM v2_conversation_summary WHERE user_id=%s", (uid,))
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (uid,))


def _messages(uid: str, count: int) -> list[dict]:
    out = []
    for index in range(count):
        message_id = f"{uid}-m{index}"
        db.chat_append_strict(
            uid,
            message_id,
            float(index + 1),
            {"id": message_id, "role": "user", "body_ct": f"ct-{index}"},
            core_store.MAX_CHAT_MESSAGES,
        )
        out.append(
            {
                "id": message_id,
                "seq": int(db.chat_seq_for_msg_id(uid, message_id)),
                "ts": float(index + 1),
                "role": "user",
                "content": f"message {index}",
            }
        )
    return out


def test_leaf_checkpoint_and_materialized_head_are_one_versioned_cas():
    uid = "u_summary_segment_cas"
    _reset(uid)
    messages = _messages(uid, 5)

    assert jobs_store.append_summary_leaf_cas(
        uid,
        summary_envelope={"plaintext": "- leaf one"},
        head_summary_envelope={"plaintext": "- leaf one"},
        start_seq=messages[0]["seq"],
        end_seq=messages[1]["seq"],
        source_message_count=2,
        watermark_ts=messages[1]["ts"],
        expected_version=0,
        previous_watermark_seq=0,
    )
    assert jobs_store.append_summary_leaf_cas(
        uid,
        summary_envelope={"plaintext": "- leaf two"},
        head_summary_envelope={"plaintext": "- leaf one\n- leaf two"},
        start_seq=messages[2]["seq"],
        end_seq=messages[3]["seq"],
        source_message_count=2,
        watermark_ts=messages[3]["ts"],
        expected_version=1,
        previous_watermark_seq=messages[1]["seq"],
    )
    state = jobs_store.get_summary_frontier_state(uid)
    child_ids = tuple(row["segment_id"] for row in state["segments"])
    assert tuple(state["materialized_segment_ids"]) == child_ids
    assert state["version"] == 2

    assert jobs_store.insert_summary_checkpoint(
        uid,
        summary_envelope={"plaintext": "- parent"},
        head_summary_envelope={"plaintext": "- parent"},
        level=1,
        start_seq=messages[0]["seq"],
        end_seq=messages[3]["seq"],
        source_message_count=4,
        child_segment_ids=child_ids,
        expected_version=2,
        expected_watermark_seq=messages[3]["seq"],
    )
    after = jobs_store.get_summary_frontier_state(uid)
    assert after["version"] == 3
    assert len(after["segments"]) == 1
    assert tuple(after["materialized_segment_ids"]) == (
        after["segments"][0]["segment_id"],
    )
    assert after["summary_envelope"] == {"plaintext": "- parent"}
    with db.get_pool().connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v2_conversation_summary_segments WHERE user_id=%s",
            (uid,),
        ).fetchone()[0] == 3  # children retained; no automatic GC

    # A leaf that read v2 before the checkpoint cannot overwrite its bounded
    # materialized head after waiting on the row lock. It loses before insert.
    assert jobs_store.append_summary_leaf_cas(
        uid,
        summary_envelope={"plaintext": "- stale leaf"},
        head_summary_envelope={"plaintext": "- stale full head"},
        start_seq=messages[4]["seq"],
        end_seq=messages[4]["seq"],
        source_message_count=1,
        watermark_ts=messages[4]["ts"],
        expected_version=2,
        previous_watermark_seq=messages[3]["seq"],
    ) is False
    assert jobs_store.get_summary_row(uid)["summary_envelope"] == {
        "plaintext": "- parent"
    }


@pytest.mark.parametrize("translated_watermark", [0, 900])
def test_legacy_blob_with_no_retained_rows_seeds_and_unary_checkpoints(
    translated_watermark: int,
):
    uid = f"u_summary_legacy_{translated_watermark}"
    _reset(uid)
    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "- " + "legacy " * 10_000},
        watermark_ts=1.0,
        expected_version=0,
        watermark_seq=translated_watermark,
    )
    assert jobs_store.seed_legacy_summary_segment(
        uid,
        expected_version=1,
        translated_watermark_seq=translated_watermark,
    )
    seeded = jobs_store.get_summary_frontier_state(uid)
    assert seeded["version"] == 2
    assert len(seeded["segments"]) == 1
    legacy = seeded["segments"][0]
    assert legacy["coverage_kind"] == "legacy_opaque"
    assert legacy["source_message_count"] == 0
    assert legacy["legacy_opaque_through_seq"] == translated_watermark
    assert tuple(seeded["materialized_segment_ids"]) == (legacy["segment_id"],)
    if translated_watermark == 0:
        # Once the zero boundary is bound to an opaque segment it is exact.
        # A later backdated row must not reactivate legacy ts->seq translation.
        db.chat_append_strict(
            uid,
            f"{uid}-late",
            0.5,
            {"id": f"{uid}-late", "role": "user", "body_ct": "late"},
            core_store.MAX_CHAT_MESSAGES,
        )
        assert jobs_store.get_summary_row(uid)["watermark_seq"] == 0
        assert jobs_store.get_summary_frontier_state(uid)["watermark_seq"] == 0

    assert jobs_store.insert_summary_checkpoint(
        uid,
        summary_envelope={"plaintext": "- bounded legacy checkpoint"},
        head_summary_envelope={"plaintext": "- bounded legacy checkpoint"},
        level=1,
        start_seq=0,
        end_seq=translated_watermark,
        source_message_count=0,
        child_segment_ids=(legacy["segment_id"],),
        expected_version=2,
        expected_watermark_seq=translated_watermark,
        coverage_kind="legacy_opaque",
        legacy_opaque_through_seq=translated_watermark,
    )
    bounded = jobs_store.get_summary_frontier_state(uid)
    assert bounded["version"] == 3
    assert bounded["summary_envelope"] == {
        "plaintext": "- bounded legacy checkpoint"
    }


def test_inline_prompt_catchup_appends_immutable_leaf_not_legacy_blob(monkeypatch):
    uid = "u_summary_inline_segment"
    _reset(uid)
    messages = _messages(uid, 12)
    assert jobs_store.upsert_summary_row_cas(
        uid,
        summary_envelope={"plaintext": "- legacy first row"},
        watermark_ts=messages[0]["ts"],
        expected_version=0,
        watermark_seq=messages[0]["seq"],
    )

    def _read_summary(user_id):
        row = jobs_store.get_summary_row(user_id)
        return (
            str((row["summary_envelope"] or {}).get("plaintext") or ""),
            row["watermark_ts"],
            row["version"],
            row["watermark_seq"],
        )

    def _read_oldest(user_id, after_seq, limit):
        return [row for row in messages if row["seq"] > after_seq][:limit]

    def _append(user_id, segment, **kwargs):
        current = kwargs.pop("current_summary")
        return jobs_store.append_summary_leaf_cas(
            user_id,
            summary_envelope={"plaintext": segment},
            head_summary_envelope={
                "plaintext": current.rstrip() + "\n" + segment.strip()
            },
            **kwargs,
        )

    legacy_write_calls = []
    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (_BYOK, {}),
        mint_enclave_token=lambda _uid: "rt",
        read_summary_with_seq=_read_summary,
        read_compaction_tail_after_seq=_read_oldest,
        read_tail_after_seq=_read_oldest,
        write_summary=lambda *args, **kwargs: legacy_write_calls.append((args, kwargs)),
        append_summary_segment=_append,
    )

    async def _fake_llm(_config, _messages, **_kwargs):
        return {"reply": "- immutable catchup leaf"}

    monkeypatch.setattr(provider_client, "reliable_chat_completion_async", _fake_llm)
    asyncio.run(
        worker._ensure_prompt_coverage(
            uid,
            deps,
            provider_config=_BYOK,
            enclave_sem=asyncio.Semaphore(1),
            tail_limit=2,
            catchup_deadline_sec=5,
        )
    )
    assert legacy_write_calls == []
    state = jobs_store.get_summary_frontier_state(uid)
    assert state["watermark_seq"] == messages[-3]["seq"]
    assert [row["coverage_kind"] for row in state["segments"]] == [
        "legacy_opaque",
        "exact",
    ]
    assert len(state["materialized_segment_ids"]) == 2
