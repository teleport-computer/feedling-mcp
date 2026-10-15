"""Real isolated SQL candidate reader and typed optional boundaries (T820)."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / 'backend'))
import json
import threading
import time
import pytest
import db
from accounts import registry
from core import store
from memory import memory_core, recall_candidates, recall_contract, recall_operation, plaintext_recall


def card(uid, mid, body='{}', **extra):
    return dict(id=mid, owner_user_id=uid, body=body, visibility='shared',
                occurred_at='2026-01-01', **extra)


@pytest.fixture
def uid():
    registry.load_users()
    return registry._register_user()['user_id']


def test_real_sql_universe_order_and_page_match_legacy(uid):
    for i in range(6):
        row=card(uid, str(i), is_archived=(i == 4))
        assert db.memory_upsert(uid, str(i), row['occurred_at'], row)
    batch=recall_candidates.read_owned(uid, time.monotonic()+5)
    old, status=memory_core.list_moments(store.get_store(uid), limit_raw='3', since='', cursor='', include_archived_raw=None)
    assert status == 200
    assert len(batch.rows) == 6
    assert recall_candidates.first_page(batch, 3) == old['moments']


@pytest.mark.parametrize('extra', [{}, {'is_archived':True}, {'visibility':'local_only'}, {'body_ct':'sealed'}])
def test_real_sql_foreign_owner_not_hidden_by_exclusion(uid, extra):
    row=card('foreign', 'bad');row.update(extra)
    assert db.memory_upsert(uid, 'bad', row['occurred_at'], row)
    with pytest.raises(recall_contract.RecallFailure, match='owner_unconfirmed'):
        recall_candidates.read_owned(uid, time.monotonic()+5)


def test_real_sql_does_not_borrow_uncommitted_mutation(uid):
    committed=card(uid,'committed');pending=card(uid,'pending')
    assert db.memory_upsert(uid,'committed',committed['occurred_at'],committed)
    with db.memory_user_mutation_fence(uid):
        assert db.memory_upsert(uid,'pending',pending['occurred_at'],pending)
        batch=recall_candidates.read_owned(uid,time.monotonic()+5)
        assert [r['id'] for r in batch.rows] == ['committed']


@pytest.mark.parametrize('bad', ['{', '[]', 'null', '\ud800'])
def test_bad_same_owner_body_discards_whole_optional_result(uid, bad):
    batch=recall_contract.candidate_batch([card(uid,'good'),card(uid,'bad',bad)],uid)
    with pytest.raises(recall_contract.RecallUnavailable, match='candidate_payload_invalid'):
        recall_operation.decode(batch,200)


def test_bad_payload_cannot_hide_later_foreign_owner(uid):
    with pytest.raises(recall_contract.RecallFailure):
        recall_contract.candidate_batch([card(uid,'bad','{'),card('foreign','later')],uid)


def test_real_adapter_operation_bad_card_is_unavailable(uid):
    row=card(uid,'bad','{');assert db.memory_upsert(uid,'bad',row['occurred_at'],row)
    history=[dict(id='msg',owner_user_id=uid,seq=1,role='user',ts=1,v=1,body='hello')]
    deps=plaintext_recall.Deps(lambda u:'off',lambda u,s:history,None,None)
    payload,status=recall_operation.select_off(uid,1,deps,deadline=time.monotonic()+5,
        permit=threading.BoundedSemaphore(1),rollout='on',query_args=plaintext_recall.RESIDENT_QUERY_ARGS)
    assert status == 503 and payload['category'] == 'recall_unavailable'
    assert payload['context_memory_log']['counts'] == dict(selected=None,candidate_pool=None,injected=0)


def test_real_sql_candidate_doc_id_must_match_durable_identity(uid):
    row = card(uid, 'different-document-id')
    assert db.memory_upsert(uid, 'durable-id', row['occurred_at'], row)
    with pytest.raises(recall_contract.RecallFailure, match='candidate_identity_invalid'):
        recall_candidates.read_owned(uid, time.monotonic()+2)


@pytest.mark.parametrize('body', ['{"summary": []}', '{"content": 123}', '{"threads": 123}'])
def test_bad_card_shape_is_whole_optional_failure(uid, body):
    batch = recall_contract.candidate_batch([card(uid,'good'),card(uid,'bad',body)],uid)
    with pytest.raises(recall_contract.RecallUnavailable, match='candidate_payload_invalid'):
        recall_operation.decode(batch,200)


@pytest.mark.parametrize('fault', ['query_cancel', 'unknown_sql', 'late_pool_timeout'])
def test_actual_dedicated_query_fault_classification(uid, monkeypatch, fault):
    from contextlib import contextmanager
    from psycopg_pool import PoolTimeout
    pool=db.get_pool()
    class Connection:
        def __init__(self, conn):
            self.conn=conn
        def transaction(self):
            return self.conn.transaction()
        def execute(self, sql, params=None):
            if sql.startswith('SELECT moment_id, doc FROM memory_moments'):
                if fault == 'query_cancel':
                    self.conn.execute("SET LOCAL statement_timeout='1ms'")
                    return self.conn.execute('SELECT pg_sleep(0.05)')
                if fault == 'unknown_sql':
                    return self.conn.execute('SELECT nonexistent_recall_column FROM memory_moments')
                raise PoolTimeout('late query exception is not acquisition')
            return self.conn.execute(sql,params)
    class Pool:
        @contextmanager
        def connection(self, **kwargs):
            with pool.connection(**kwargs) as conn:
                yield Connection(conn)
    monkeypatch.setattr(db,'get_pool',lambda:Pool())
    expected=recall_contract.RecallUnavailable if fault=='query_cancel' else recall_contract.RecallFailure
    with pytest.raises(expected):
        recall_candidates.read_owned(uid,time.monotonic()+2)


def test_pool_acquisition_timeout_is_narrow_optional(uid,monkeypatch):
    from contextlib import contextmanager
    from psycopg_pool import PoolTimeout
    class Pool:
        @contextmanager
        def connection(self,**kwargs):
            raise PoolTimeout('synthetic acquisition timeout')
            yield
    monkeypatch.setattr(db,'get_pool',lambda:Pool())
    with pytest.raises(recall_contract.RecallUnavailable,match='candidate_read_unavailable'):
        recall_candidates.read_owned(uid,time.monotonic()+2)


def test_expired_candidate_deadline_is_not_an_isolated_availability_fault(uid, monkeypatch):
    monkeypatch.setattr(db, 'get_pool', lambda: pytest.fail('expired candidate read touched storage'))
    with pytest.raises(recall_contract.RecallFailure):
        recall_candidates.read_owned(uid, time.monotonic()-1)


@pytest.mark.parametrize('optional', [False, True], ids=['ownership_unproved', 'ownership_proved'])
def test_late_stage_completion_never_publishes_a_success(monkeypatch, optional):
    # Deterministic boundary race: wrapper finished but the waiter is scheduled
    # after the operation's absolute deadline.
    monkeypatch.setattr(plaintext_recall, 'run_bounded', lambda *a: ('ok', {'late':'result'}))
    error = recall_contract.RecallUnavailable if optional else recall_contract.RecallFailure
    with pytest.raises(error):
        recall_operation._stage(lambda: None, time.monotonic()-1, threading.BoundedSemaphore(1), optional=optional)


@pytest.mark.parametrize('stage', ['required_history', 'candidate_read', 'decode', 'selection'])
def test_real_operation_timeout_policy_retains_stage_permit(uid, monkeypatch, stage):
    put = card(uid, 'valid', '{"summary":"question"}')
    assert db.memory_upsert(uid, 'valid', put['occurred_at'], put)
    history = [dict(id='message', owner_user_id=uid, seq=1, role='user', ts=1, v=1, body='question')]
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    permit = threading.BoundedSemaphore(1)
    def block(value):
        entered.set()
        try:
            release.wait(2)
            return value
        finally:
            finished.set()
    history_reader = lambda u,s: block(history) if stage == 'required_history' else history
    deps = plaintext_recall.Deps(lambda _: 'off', history_reader, None, None)
    if stage == 'candidate_read':
        read = recall_candidates.read_owned
        monkeypatch.setattr(recall_candidates, 'read_owned', lambda u,d: block(read(u,d)))
    elif stage == 'decode':
        decode = recall_operation.decode
        monkeypatch.setattr(recall_operation, 'decode', lambda *a: block(decode(*a)))
    elif stage == 'selection':
        rank = recall_operation.rank
        monkeypatch.setattr(recall_operation, 'rank', lambda *a, **k: block(rank(*a, **k)))
    try:
        def select():
            return recall_operation.select_off(uid, 1, deps, deadline=time.monotonic()+0.1,
                permit=permit, rollout='on', query_args=plaintext_recall.RESIDENT_QUERY_ARGS)
        if stage in {'required_history', 'candidate_read'}:
            with pytest.raises(recall_contract.RecallFailure):
                select()
        else:
            payload, status = select()
            assert status == 503 and payload['category'] == 'recall_unavailable'
            assert payload['context_memory_log']['counts']['selected'] is None
        assert entered.is_set()
        assert not permit.acquire(blocking=False)
    finally:
        release.set()
    assert finished.wait(2)
    assert permit.acquire(timeout=2)
    permit.release()


def test_legacy_string_thread_keeps_existing_readside_normalization(uid):
    batch = recall_contract.candidate_batch([card(uid, 'valid', '{"summary":"text","threads":"legacy thread"}')], uid)
    decoded = recall_operation.decode(batch, 200)
    assert json.loads(decoded.cards_json)[0]['threads'] == ['legacy thread']
