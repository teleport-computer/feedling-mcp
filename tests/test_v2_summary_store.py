"""v2_conversation_summary 表 + jobs_store 行级 CAS 读写。"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import db
from model_api_runtime.v2 import jobs_store


def _seed_user(uid):
    with db.get_pool().connection() as conn:
        conn.execute(
            "INSERT INTO users (user_id, created_at, doc) VALUES (%s,'','{}'::jsonb) "
            "ON CONFLICT (user_id) DO NOTHING",
            (uid,),
        )


def test_get_missing_returns_none():
    _seed_user("u_sum_1")
    assert jobs_store.get_summary_row("u_sum_1") is None


def test_first_write_inserts_version1():
    _seed_user("u_sum_2")
    assert (
        jobs_store.upsert_summary_row_cas(
            "u_sum_2", summary_envelope={"body_ct": "x"}, watermark_ts=5.0, expected_version=0
        )
        is True
    )
    row = jobs_store.get_summary_row("u_sum_2")
    assert row["version"] == 1
    assert row["watermark_ts"] == 5.0
    assert row["summary_envelope"] == {"body_ct": "x"}


def test_first_write_conflict_returns_false():
    _seed_user("u_sum_3")
    jobs_store.upsert_summary_row_cas(
        "u_sum_3", summary_envelope={"a": 1}, watermark_ts=1.0, expected_version=0
    )
    assert (
        jobs_store.upsert_summary_row_cas(
            "u_sum_3", summary_envelope={"a": 2}, watermark_ts=2.0, expected_version=0
        )
        is False
    )


def test_cas_update_succeeds_on_matching_version():
    _seed_user("u_sum_4")
    jobs_store.upsert_summary_row_cas(
        "u_sum_4", summary_envelope={"a": 1}, watermark_ts=1.0, expected_version=0
    )
    assert (
        jobs_store.upsert_summary_row_cas(
            "u_sum_4", summary_envelope={"a": 2}, watermark_ts=9.0, expected_version=1
        )
        is True
    )
    row = jobs_store.get_summary_row("u_sum_4")
    assert row["version"] == 2
    assert row["watermark_ts"] == 9.0


def test_cas_update_fails_on_stale_version():
    _seed_user("u_sum_5")
    jobs_store.upsert_summary_row_cas(
        "u_sum_5", summary_envelope={"a": 1}, watermark_ts=1.0, expected_version=0
    )
    assert (
        jobs_store.upsert_summary_row_cas(
            "u_sum_5", summary_envelope={"a": 9}, watermark_ts=9.0, expected_version=7
        )
        is False
    )
    assert jobs_store.get_summary_row("u_sum_5")["version"] == 1
