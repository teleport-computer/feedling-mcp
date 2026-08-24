"""Durable, content-free rejection visibility for the ErrorSpec boundary."""
from __future__ import annotations

import time

import pytest

import db
from notices import rejection_stats


@pytest.fixture(autouse=True)
def _clean_contract_stats(backend_env):
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM contract_rejection_stats")
    yield
    with db.get_pool().connection() as conn:
        conn.execute("DELETE FROM contract_rejection_stats")


def test_absolute_total_replay_uses_greatest_and_new_writer_is_additive():
    now = time.time()
    base = (
        "vision", "resident_vision_response", "error_class_unregistered",
        "release-a", "writer-a", 1, now - 10, now - 5,
    )
    db.upsert_contract_rejection_stats([base])
    db.upsert_contract_rejection_stats([base])
    db.upsert_contract_rejection_stats([(
        "vision", "resident_vision_response", "error_class_unregistered",
        "release-a", "writer-a", 3, now - 10, now,
    )])
    db.upsert_contract_rejection_stats([(
        "vision", "resident_vision_response", "error_class_unregistered",
        "release-a", "writer-b", 2, now - 2, now,
    )])

    report = db.contract_rejection_stats_measurement(
        release_sha="release-a", now_epoch=now + 1
    )
    assert report["rows"] == [{
        "contract_domain": "vision",
        "boundary": "resident_vision_response",
        "fallback": "error_class_unregistered",
        "release_sha": "release-a",
        "total": 5,
        "first_seen": pytest.approx(now - 10),
        "last_seen": pytest.approx(now),
    }]


def test_resident_header_ingestion_persists_only_controlled_dimensions():
    reporter = rejection_stats.ResidentRejectionReporter(
        writer_id="resident:test:one", release_sha="release-b"
    )
    reporter.record(
        "image_generation",
        "resident_image_generation_response",
        "error_class_unregistered",
    )
    rejection_stats.ingest_resident_header(reporter.header_value())

    with db.get_pool().connection() as conn:
        row = conn.execute(
            "SELECT contract_domain,boundary,fallback,release_sha,writer_id,total "
            "FROM contract_rejection_stats"
        ).fetchone()
        columns = {
            item[0]
            for item in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='public' "
                "AND table_name='contract_rejection_stats'"
            ).fetchall()
        }
    assert row == (
        "image_generation",
        "resident_image_generation_response",
        "error_class_unregistered",
        "release-b",
        "resident:test:one",
        1,
    )
    assert columns == {
        "contract_domain", "boundary", "fallback", "release_sha",
        "writer_id", "total", "first_seen", "last_seen",
    }
