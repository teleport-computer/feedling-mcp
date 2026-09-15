"""IO write rejection, atomic receipts and import crash recovery on Postgres."""
import copy

import pytest

from genesis import import_engine
from memory import action_receipts, garden_import
import db
from test_memory_store_conformance import IoHost
import test_memory_store_conformance as conformance_host
import test_garden_import_engine as fixture

io_world = conformance_host.io_world


def action(text="A complete synthetic memory.", key="same-request"):
    return {"type": "memory.add", "idempotency_key": key,
            "memory": {"summary": "Synthetic memory", "content": text, "source": "chat"}}


@pytest.mark.parametrize("size", [4999, 5000, 5001])
def test_plaintext_limit_rejects_without_truncation(io_world, size):
    host = IoHost(io_world)
    result = host._act("alice", action("字" * size))
    if size > 5000:
        assert not result.ok and "memory_content_too_long" in result.detail
        assert host.history("alice") == []
    else:
        assert result.ok, result
        assert host.inspect("alice", result.record_ids[0])["content"] == "字" * size


def test_action_replay_conflict_isolation_and_delete(io_world):
    host = IoHost(io_world)
    a = action()
    first = host._act("alice", a)
    assert first.ok
    assert host._act("alice", a).record_ids == first.record_ids
    assert len(host.history("alice")) == 1
    conflict = host._act("alice", action("Different content."))
    assert not conflict.ok and "memory_idempotency_conflict" in conflict.detail
    bob = host._act("bob", a)
    assert bob.ok and bob.record_ids != first.record_ids
    assert host._act("alice", {"type": "memory.delete", "id": first.record_ids[0]}).ok
    assert host._act("alice", a).record_ids == first.record_ids
    assert host.history("alice") == []  # replay may acknowledge, never resurrect


@pytest.mark.parametrize("key,valid", [("k" * 159, True), ("k" * 160, True),
                                      ("k" * 161, False), ("  ", False), (None, False)])
def test_action_key_boundary(io_world, key, valid):
    host = IoHost(io_world)
    result = host._act("alice", action(key=key))
    assert result.ok is valid
    if not valid:
        assert "memory_idempotency_key_invalid" in result.detail
        assert host.history("alice") == []


def test_oversize_supersede_preserves_target(io_world):
    host = IoHost(io_world)
    rid = host._act("alice", action()).record_ids[0]
    before = host.inspect("alice", rid)
    request = {**action("字" * 5001, key="correction"),
               "type": "memory.supersede", "supersedes": [rid]}
    result = host._act("alice", request)
    assert not result.ok and "memory_content_too_long" in result.detail
    assert host.inspect("alice", rid) == before
    assert len(host.history("alice")) == 1


def test_receipt_failure_rolls_back_memory_and_retry_succeeds(io_world, monkeypatch):
    host = IoHost(io_world)
    real_jsonb = action_receipts.Jsonb
    def fail(_doc):
        raise RuntimeError("receipt storage failed")
    monkeypatch.setattr(action_receipts, "Jsonb", fail)
    with pytest.raises(RuntimeError, match="receipt storage failed"):
        host._act("alice", action())
    assert host.history("alice") == []
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT count(*) FROM user_logs WHERE user_id=%s AND stream='memory_changes'",
                            (host._uid("alice"),)).fetchone()[0] == 0
    monkeypatch.setattr(action_receipts, "Jsonb", real_jsonb)
    assert host._act("alice", action()).ok
    assert len(host.history("alice")) == 1


@pytest.mark.parametrize("prebuilt", [False, True])
def test_import_write_committed_before_checkpoint_recovers_without_duplicates(io_world, prebuilt):
    host = IoHost(io_world)
    writer = import_engine.store_writer(host._store("alice"), None)
    sources = fixture._sources("窗口：两件事\n")
    state = garden_import.new_state(locale="zh-Hans", strategy="single_pass")
    model = fixture.Model([("两件事", fixture._reply(
        fixture._card("周末常去西湖边骑车"), fixture._card("每天早上一杯冰美式")))])
    checkpoint = {}
    def save(doc):
        if (doc.get("pending") or {}).get("ids"):
            raise RuntimeError("crash before checkpoint")
        checkpoint["doc"] = copy.deepcopy(doc)
    kwargs = dict(sources=sources, job_key="job1", owner_key=host._uid("alice"),
                  write=writer)
    preparations = []
    if prebuilt:
        from test_memory_store_conformance import _seal
        from memory import actions
        def prepare(mutations):
            preparations.append(True)
            return [{"type": "memory.add", "envelope": {
                **_seal(m["card"], owner=host._uid("alice"), item_id=f"mom_prepared_{i}"),
                "occurred_at": "2026-01-01T00:00:00Z", "source": "chat", "type": "fact",
            }} for i, m in enumerate(mutations)]
        def rows(batch):
            return actions._execute_memory_actions(host._store("alice"), None, batch)[0]["results"]
        kwargs.update(prepare_write=prepare, write=lambda batch, key:
                      garden_import.write_with_executor(batch, build_action=dict,
                                                        execute=rows, idempotency_key=key))
    with pytest.raises(RuntimeError, match="crash before checkpoint"):
        garden_import.run_import(**kwargs, state=state, existing_cards=[], complete=model, save=save)
    assert len(host.history("alice")) == 2
    resumed = garden_import.run_import(
        **kwargs, state=checkpoint["doc"], existing_cards=host.history("alice"),
        complete=fixture.Model([], default="MUST NOT CALL"), save=lambda _doc: None)
    assert resumed.done and resumed.cards_written == 2
    assert len(host.history("alice")) == 2
    assert len(preparations) == int(prebuilt)  # replay never prepares another envelope


def test_partial_storage_failure_is_not_a_content_rejection():
    rows = [{"http_status": 200, "memory": {"id": "first"}},
            {"http_status": 500, "error": "db_write_failed"}]
    with pytest.raises(RuntimeError, match="db_write_failed"):
        garden_import.write_with_executor(
            [{}, {}], build_action=lambda _m: {"type": "memory.add"},
            execute=lambda _actions: rows)


def test_concurrent_replay_has_one_card_and_one_change(io_world):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    host = IoHost(io_world)
    uid = host._uid("alice")
    barrier = threading.Barrier(2)
    def write():
        barrier.wait(timeout=10)
        return host._act("alice", action())
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(write) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]
    assert all(r.ok for r in results)
    assert results[0].record_ids == results[1].record_ids
    assert len(host.history("alice")) == 1
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT count(*) FROM user_logs WHERE user_id=%s AND stream='memory_changes'",
                            (uid,)).fetchone()[0] == 1


def test_existing_long_card_fetch_is_complete(io_world):
    from test_memory_store_conformance import _seal

    host = IoHost(io_world)
    body = "历史正文🙂" * 1200
    envelope = _seal({"summary": "Historical memory", "content": body,
                      "bucket": "Life", "threads": []},
                     owner=host._uid("alice"), item_id="mom_historical_long")
    envelope.update(occurred_at="2025-01-01T00:00:00Z", source="chat", type="fact")
    result = host._act("alice", {"type": "memory.add", "envelope": envelope})
    assert result.ok, result
    assert host.fetch("alice", result.record_ids)[0]["content"] == body


def test_import_fallback_replay_keeps_original_success(io_world):
    host = IoHost(io_world)
    writer = import_engine.store_writer(host._store("alice"), None)
    mutation = {"op": "supersede", "target_id": "missing_target",
                "card": fixture._card("周末常去西湖边骑车")}
    first = writer([mutation], "fallback_batch")
    assert first[0]
    assert writer([mutation], "fallback_batch") == first
    assert len(host.history("alice")) == 1
