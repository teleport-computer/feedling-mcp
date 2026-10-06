"""Content-based capture: stats batches disappear, domain assertions stay exact."""

from types import SimpleNamespace

import pytest

import db
from conftest import capture_mirror_groups
from tee_shadow import mirror


@pytest.mark.parametrize("sql", [
    db._TRACE_WRITE_STATS_UPSERT_SQL,
    db._TRACE_WRITE_STATS_HEALTH_UPSERT_SQL,
    db._CONTRACT_REJECTION_STATS_UPSERT_SQL,
])
def test_capture_excludes_whole_stats_group_and_keeps_domain_batches(monkeypatch, sql):
    sink = []
    assert capture_mirror_groups(monkeypatch, sink) is sink
    domain = [("DELETE FROM users WHERE user_id=%s", ("user-test",))]
    # A stats statement anywhere in a group excludes the entire group.
    mirror.execute_many([(sql, ("stats",))])
    mirror.execute_many(domain + [(sql, ("stats",))])
    mirror.execute_many([(sql, ("stats",))] + domain)
    assert sink == []
    mirror.execute_many(iter(domain))
    assert sink == [domain]
    assert sink[0] is not domain  # materialized snapshot; parameters stay verbatim


@pytest.mark.parametrize("sql,params", [
    ("INSERT INTO trace_write_stats_archive VALUES (%s)", (1,)),
    ("INSERT INTO business VALUES (%s)", ("trace_write_stats_health",)),
    ("INSERT INTO business SELECT * FROM contract_rejection_stats", ()),
    ("UPDATE business SET note='trace_write_stats'", ()),
])
def test_capture_does_not_filter_similar_names_sources_or_parameter_text(monkeypatch, sql, params):
    sink = capture_mirror_groups(monkeypatch)
    group = [(sql, params)]
    mirror.execute_many(group)
    assert sink == [group]


def test_capture_supports_flattening_and_forwarding_sinks(monkeypatch):
    captured, forwarded = [], []

    def append(group):
        captured.extend(group)
        forwarded.append(group)
        return "forwarded-result"

    sink = SimpleNamespace(append=append)
    assert capture_mirror_groups(monkeypatch, sink) is sink
    group = [("DELETE FROM domain WHERE id=%s", (7,))]
    assert mirror.execute_many(group) == "forwarded-result"
    assert captured == group and forwarded == [group]
    mirror.execute_many([(db._TRACE_WRITE_STATS_HEALTH_UPSERT_SQL, ())])
    assert captured == group and forwarded == [group]
