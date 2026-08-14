"""Integration coverage for metadata-only Runtime V2 compaction."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import conftest
import db
import pytest
from core import store as core_store
from model_api_runtime.v2 import jobs_store
from model_api_runtime.v2 import serve_worker
from model_api_runtime.v2 import worker


@pytest.fixture(autouse=True)
def _clean_jobs():
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs")
    yield


def _reset(user_id: str) -> None:
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM agent_jobs WHERE user_id=%s", (user_id,))
        conn.execute("DELETE FROM runtime_state WHERE user_id=%s", (user_id,))
        conn.execute(
            "DELETE FROM v2_conversation_summary WHERE user_id=%s",
            (user_id,),
        )
        conn.execute("DELETE FROM chat_messages WHERE user_id=%s", (user_id,))
    conftest.set_v2_runtime_owner(user_id)


def test_metadata_coverage_bounds_exclude_gc_able_rows_and_honor_limit():
    user_id = "u_v2_metadata_coverage_bounds"
    conftest.seed_user(user_id)
    _reset(user_id)
    for index, source in enumerate(
        [
            "model_api",
            "verify_ping",
            "model_api",
            "resident_maintenance",
            "model_api",
        ]
    ):
        db.chat_append_strict(
            user_id,
            f"bounds-{index}",
            float(index + 1),
            {
                "id": f"bounds-{index}",
                "role": "user",
                "source": source,
                "body_ct": f"cipher-{index}",
            },
            core_store.MAX_CHAT_MESSAGES,
        )
    eligible = db.chat_messages_after_seq(
        user_id,
        0,
        limit=None,
        exclude_synthetic_sources=True,
    )

    assert [row["source"] for row in eligible] == [
        "model_api",
        "model_api",
        "model_api",
    ]
    assert db.chat_coverage_bounds_after_seq(user_id, 0, limit=2) == (
        eligible[0]["seq"],
        eligible[1]["seq"],
        2,
    )
    assert db.chat_coverage_bounds_after_seq(
        user_id,
        eligible[0]["seq"],
        limit=10,
        through_seq=eligible[1]["seq"],
    ) == (eligible[1]["seq"], eligible[1]["seq"], 1)


def test_large_backlog_uses_no_model_and_keeps_frontier_bounded(monkeypatch):
    user_id = "u_v2_deterministic_large_backlog"
    conftest.seed_user(user_id)
    _reset(user_id)
    total = 1_510
    with db.get_pool().connection() as conn:
        with conn.transaction():
            for index in range(total):
                conn.execute(
                    "INSERT INTO chat_messages (user_id,msg_id,ts,doc) "
                    "VALUES (%s,%s,%s,%s)",
                    (
                        user_id,
                        f"det-{index}",
                        float(index + 1),
                        db.Jsonb(
                            {
                                "id": f"det-{index}",
                                "role": "user" if index % 2 == 0 else "openclaw",
                                "source": "model_api",
                                "body_ct": f"cipher-{index}",
                            }
                        ),
                    ),
                )

    monkeypatch.setattr(worker, "_COMPACTION_BATCH", 200)
    monkeypatch.setattr(worker, "_SUMMARY_ROLLUP_FANOUT", 8)
    head_sizes: list[int] = []

    def _append_segment(uid, segment_text, **kwargs):
        head_text = str(kwargs["head_summary"])
        head_sizes.append(len(head_text))
        return jobs_store.append_summary_leaf_cas(
            uid,
            summary_envelope={"plaintext": str(segment_text)},
            head_summary_envelope={"plaintext": head_text},
            start_seq=kwargs["start_seq"],
            end_seq=kwargs["end_seq"],
            source_message_count=kwargs["source_message_count"],
            watermark_ts=kwargs["watermark_ts"],
            expected_version=kwargs["expected_version"],
            previous_watermark_seq=kwargs["previous_watermark_seq"],
        )

    def _append_checkpoint(uid, checkpoint_text, **kwargs):
        head_text = str(kwargs["head_summary"])
        head_sizes.append(len(head_text))
        return jobs_store.insert_summary_checkpoint(
            uid,
            summary_envelope={"plaintext": str(checkpoint_text)},
            head_summary_envelope={"plaintext": head_text},
            level=kwargs["level"],
            start_seq=kwargs["start_seq"],
            end_seq=kwargs["end_seq"],
            source_message_count=kwargs["source_message_count"],
            child_segment_ids=kwargs["child_segment_ids"],
            coverage_kind=kwargs["coverage_kind"],
            legacy_opaque_through_seq=kwargs["legacy_opaque_through_seq"],
            expected_version=kwargs["expected_version"],
            expected_watermark_seq=kwargs["expected_watermark_seq"],
        )

    deps = worker.TurnDeps(
        read_messages=lambda _uid: [],
        resolve_provider=lambda _uid: (_ for _ in ()).throw(
            AssertionError("maintenance must not resolve a provider")
        ),
        mint_enclave_token=lambda _uid: (_ for _ in ()).throw(
            AssertionError("maintenance must not mint a token")
        ),
        runtime_mode_enabled=lambda _uid: True,
        read_summary_frontier_metadata=serve_worker._read_summary_frontier_metadata,
        append_summary_segment=_append_segment,
        append_summary_checkpoint=_append_checkpoint,
    )
    expected_jobs = (
        total - worker._TAIL_KEEP + worker._COMPACTION_BATCH - 1
    ) // worker._COMPACTION_BATCH
    jobs_store.enqueue_job(user_id, "maintenance", reason="deterministic-test")
    completed_jobs = 0
    while completed_jobs < expected_jobs + 2:
        job = jobs_store.claim_next_job("deterministic-worker")
        if job is None:
            break
        assert asyncio.run(worker._run_turn(job, deps)) == "completed"
        completed_jobs += 1

    state = jobs_store.get_summary_frontier_state(user_id)
    assert state is not None
    assert db.count_messages_after_seq(
        user_id,
        state["watermark_seq"],
        exclude_synthetic_sources=True,
    ) == worker._TAIL_KEEP
    assert completed_jobs == expected_jobs
    assert len(state["segments"]) <= worker._SUMMARY_FRONTIER_MAX_SEGMENTS
    assert any(int(row["level"]) > 0 for row in state["segments"])
    assert head_sizes and max(head_sizes) < 80
