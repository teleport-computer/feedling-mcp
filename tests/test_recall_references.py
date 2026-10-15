"""Required explicit references never use optional recall's failure policy."""
import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'backend'))
import db
from accounts import registry
from memory import plaintext_recall, recall_contract, recall_operation, recall_references
from model_api_runtime.v2 import serve_worker


@pytest.fixture
def uid():
    registry.load_users()
    return registry._register_user()['user_id']


def put(uid, mid='reference', **extra):
    row = dict(id=mid, owner_user_id=uid, body=json.dumps({'summary': 'Required reference text'}),
               visibility='shared', occurred_at='2026-01-01')
    row.update(extra)
    assert db.memory_upsert(uid, mid, row['occurred_at'], row)


def test_archived_explicit_reference_is_read_in_request_order(uid):
    put(uid, 'one', is_archived=True)
    put(uid, 'two')
    cards = recall_references.read(uid, ['two', 'one', 'two'], time.monotonic()+3)
    assert [card['id'] for card in cards] == ['two', 'one']
    assert cards[1]['summary'] == 'Required reference text'


@pytest.mark.parametrize('extra', [dict(body='{'), dict(body='{}'), dict(body_ct='sealed'),
    dict(visibility='local_only'), dict(owner_user_id='foreign'), dict(id='other'),
    dict(body=json.dumps({'summary': []}))])
def test_required_reference_invalid_is_hard(uid, extra):
    put(uid, **extra)
    with pytest.raises(recall_contract.RecallFailure):
        recall_references.read(uid, ['reference'], time.monotonic()+3)


def test_missing_and_expired_references_are_hard(uid, monkeypatch):
    with pytest.raises(recall_contract.RecallFailure):
        recall_references.read(uid, ['missing'], time.monotonic()+3)
    monkeypatch.setattr(db, 'get_pool', lambda: pytest.fail('expired read touched storage'))
    with pytest.raises(recall_contract.RecallFailure):
        recall_references.read(uid, ['missing'], time.monotonic()-1)


def test_reference_owner_pass_precedes_payload_decode(uid):
    put(uid, 'malformed', body='{')
    put(uid, 'foreign', owner_user_id='foreign')
    with pytest.raises(recall_contract.RecallFailure, match='owner_unconfirmed'):
        recall_references.read(uid, ['malformed', 'foreign'], time.monotonic()+3)


def test_required_reference_survives_optional_unavailability_and_wake_omits_it(uid):
    put(uid)
    history = [dict(id='message', owner_user_id=uid, seq=1, role='user', ts=1,
                    v=1, body='Explain this', quoted_memory_ids='reference')]
    deps = plaintext_recall.Deps(lambda _: 'off', lambda u,s: history, None, None)
    def select(**kwargs):
        return recall_operation.select_off(uid, 1, deps, deadline=time.monotonic()+3,
            permit=threading.BoundedSemaphore(1), rollout='off',
            query_args=plaintext_recall.RESIDENT_QUERY_ARGS, **kwargs)
    payload, status = select()
    assert status == 503 and payload['category'] == 'recall_unavailable'
    assert payload['required_references'][0]['id'] == 'reference'
    history[0]['quoted_memory_ids'] = 'missing'
    with pytest.raises(recall_contract.RecallFailure):
        select()
    payload, status = select(include_references=False)
    assert status == 503 and payload['required_references'] == []


def test_v2_reference_expansion_keeps_verified_prompt_proof(uid, monkeypatch):
    put(uid)
    row = dict(id='message', seq=1, content='Explain', quoted_memory_ids='reference',
               _recall_row_proof=recall_contract.TurnRowProof(uid, 'message', 1, 'Explain'))
    monkeypatch.setattr(serve_worker.memory_core, 'fetch', lambda *a, **k: pytest.fail('enclave compatibility used'))
    expanded = serve_worker._expand_quoted_memories(uid, [row])
    assert 'Required reference text' in expanded[0]['content']
    recall_contract.validate_prompt_rows(expanded, uid, 1, required_ids=['message'])
    row['quoted_memory_ids'] = 'missing'
    with pytest.raises(recall_contract.RecallFailure):
        serve_worker._expand_quoted_memories(uid, [row])
