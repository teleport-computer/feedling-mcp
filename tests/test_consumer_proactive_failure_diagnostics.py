"""Actual resident payload -> authenticated route -> PostgreSQL -> offline reader."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
os.environ.setdefault('FEEDLING_API_URL', 'http://localhost:5001')
os.environ.setdefault('FEEDLING_API_KEY', 'test_key_00000000')
import tools.chat_resident_consumer as consumer
import db
from accounts import registry
from asgi_test_client import make_client
from core import store as stores
from proactive import proactive_core
from proactive import failure_diagnostics as diag
from tools import resident_failure_diagnostics as reader
from conftest import seed_user


@pytest.fixture
def job():
    uid = 'usr_t815_' + uuid.uuid4().hex[:12]
    key = 'synthetic_' + uuid.uuid4().hex
    seed_user(uid)
    registry._key_to_user[registry._hash_api_key(key)] = uid
    store = stores.get_store(uid)
    item = {'job_id': 't815_job', 'source': diag.SOURCE, 'status': 'claimed',
            'consumer_id': 'resident_test', 'ts': 1.0}
    store.append_proactive_job(item)
    return SimpleNamespace(uid=uid, store=store, item=item, client=make_client(),
                           headers={'X-API-Key': key})


def post(job, **body):
    return job.client.post('/v1/proactive/jobs/t815_job/status', headers=job.headers, json=body)


def wire(code='auth_invalid'):
    return {'v': 1, 'reported_error_class': code}


def saved(job):
    return next(row for row in db.log_read(job.uid, 'proactive_jobs') if row['job_id'] == 't815_job')


def failure(job, code='auth_invalid', **extra):
    payload = dict(status='failed', consumer_id='resident_test', failure_diagnostic=wire(code))
    payload.update(extra)
    response = post(job, **payload)
    assert response.status_code == 200, response.get_json()
    return saved(job)['failure_diagnostic']


@pytest.mark.parametrize('payment,code', [(False, 'auth_invalid'), (True, 'quota_insufficient')])
def test_real_producer_asgi_persistence_and_reader(job, monkeypatch, payment, code):
    # Real claim and status HTTP request bodies run through ASGI authentication.
    db.log_patch_item(job.uid, 'proactive_jobs', 't815_job', {'status': 'pending'})
    monkeypatch.setattr(consumer, 'CONSUMER_ID', 'resident_test')
    requests = []
    def http_post(url, json, **kwargs):
        from urllib.parse import urlsplit
        requests.append(dict(json))
        response = job.client.post(urlsplit(url).path, headers=job.headers, json=json)
        assert response.status_code == 200, response.get_json()
        return SimpleNamespace(raise_for_status=lambda: None, json=response.get_json)
    monkeypatch.setattr(consumer._HTTP, 'post', http_post)
    monkeypatch.setattr(consumer, '_plan_wake_coalescing', lambda jobs: None)
    monkeypatch.setattr(consumer, '_mark_seen', lambda key: True)
    monkeypatch.setattr(consumer, '_proactive_backing_off', lambda: False)
    monkeypatch.setattr(consumer, '_provider_payment_cooling_down', lambda: False)
    monkeypatch.setattr(consumer, '_screen_context_for_frame_ids', lambda frames: ('', [], []))
    monkeypatch.setattr(consumer, 'recent_chat_context_for_proactive', lambda: consumer.ProactiveChatContext())
    monkeypatch.setattr(consumer, '_proactive_perception_digest', lambda: ({}, [], {}))
    monkeypatch.setattr(consumer, '_message_for_proactive_job', lambda *a, **k: 'synthetic')
    monkeypatch.setattr(consumer, '_auto_memory_block_for', lambda *a: ('', []))
    calls = []
    message = 'HTTP 402 Insufficient balance' if payment else 'HTTP 401 invalid api key'
    def fail(*a, **k):
        calls.append('call')
        raise RuntimeError(message + ' PRIVATE_BODY')
    monkeypatch.setattr(consumer, 'call_agent', fail)
    monkeypatch.setattr(consumer, '_is_provider_payment_error', lambda exc: payment)
    monkeypatch.setattr(consumer, '_note_provider_payment_failure', lambda: calls.append('payment'))
    monkeypatch.setattr(consumer, '_note_proactive_failure', lambda: calls.append('backoff'))
    monkeypatch.setattr(consumer, '_notify_agent_turn_failure', lambda *a, **k: calls.append(('notify', k)))
    monkeypatch.setattr(consumer, 'post_reply', lambda *a, **k: pytest.fail('background must remain silent'))
    consumer._process_proactive_jobs([job.item])
    doc = saved(job)
    assert [r.get('status') for r in requests] == [None, 'realizing', 'failed']
    assert requests[-1]['reason'] == ('provider_payment_required: ' if payment else 'agent_call_failed: ') + message + ' PRIVATE_BODY'
    assert doc['failure_diagnostic'] == diag.envelope('reported', code)
    assert 'PRIVATE_BODY' not in json.dumps(doc['failure_diagnostic'])
    assert calls.count('call') == calls.count('backoff') == 1
    assert calls.count('payment') == int(payment)
    assert len([c for c in calls if isinstance(c, tuple)]) == 1
    assert calls[-1][1]['foreground'] is False
    result = reader.summarize({'schema_version': 1, 'jobs': [{'user_id': job.uid, 'job_id': 't815_job', 'doc': doc}]})
    assert result['confirmed_failed_jobs'] == result['reported_classes'][code] == 1
    assert 'PRIVATE_BODY' not in json.dumps(result) and job.uid not in json.dumps(result)


@pytest.mark.parametrize('value', [None, [], {'v': True, 'reported_error_class': 'auth_invalid'},
    {'v': 2, 'reported_error_class': 'auth_invalid'}, {'v': 1, 'reported_error_class': 'x' * 1000},
    {'v': 1, 'reported_error_class': 'auth_invalid', 'source': 'verified'},
    {'v': 1, 'reported_error_class': ['auth_invalid']}])
def test_invalid_wire_downgrades_without_rejecting_original_status(job, value):
    assert failure(job, failure_diagnostic=value) == diag.envelope('invalid')
    assert saved(job)['status'] == 'failed'


@pytest.mark.parametrize('raw,stored', [(123, '123'), (' resident_test ', 'resident_test'),
                                        ('x' * 161, 'x' * 161), ('', '')])
def test_original_identity_not_cooked_patch(job, raw, stored):
    db.log_patch_item(job.uid, 'proactive_jobs', 't815_job', {'consumer_id': stored})
    assert failure(job, consumer_id=raw) == diag.envelope('binding_unavailable')


def test_legacy_missing_and_auth_and_original_errors(job):
    assert job.client.post('/v1/proactive/jobs/t815_job/status', json={'status': 'failed'}).status_code == 401
    assert post(job, failure_diagnostic=wire()).status_code == 400
    assert post(job, status='failed', consumer_id='wrong', failure_diagnostic=wire()).status_code == 409
    assert saved(job)['status'] == 'claimed'
    assert job.client.post('/v1/proactive/jobs/absent/status', headers=job.headers, json={'status': 'failed'}).status_code == 404
    assert post(job, status='failed', reason='agent_call_failed: HTTP 401 PRIVATE').status_code == 200
    assert saved(job)['failure_diagnostic'] == diag.envelope('missing')
    assert saved(job)['status_reason'] == 'agent_call_failed: HTTP 401 PRIVATE'


def test_state_machine_conflict_clear_and_reclaim(job):
    assert failure(job) == diag.envelope('reported', 'auth_invalid')
    assert failure(job) == diag.envelope('reported', 'auth_invalid')
    assert failure(job, 'quota_insufficient') == diag.envelope('conflicting_reports')
    assert post(job, status='failed').status_code == 200
    assert saved(job)['failure_diagnostic'] == diag.envelope('conflicting_reports')
    assert failure(job, failure_diagnostic=[]) == diag.envelope('conflicting_reports')
    assert failure(job, consumer_id=' resident_test ') == diag.envelope('conflicting_reports')
    job.store.update_proactive_job('t815_job', {'agent_action': 'unchanged'})
    assert saved(job)['failure_diagnostic'] == diag.envelope('conflicting_reports')
    assert post(job, reason='new reason').status_code == 200
    assert saved(job)['failure_diagnostic'] == diag.envelope('missing')
    assert failure(job) == diag.envelope('reported', 'auth_invalid')
    assert post(job, status='completed').status_code == 200
    assert saved(job)['failure_diagnostic'] is None
    assert failure(job) == diag.envelope('binding_unavailable')
    assert post(job, status='claimed', consumer_id='resident_test').status_code == 200
    assert failure(job) == diag.envelope('reported', 'auth_invalid')
    job.store.update_proactive_job('t815_job', {'consumer_id': 'new'})
    assert saved(job)['failure_diagnostic'] == diag.envelope('binding_unavailable')


@pytest.mark.parametrize('old', [[], 'PRIVATE', True, 12, {'v': 1},
    {**diag.envelope('reported', 'auth_invalid'), 'secret': 'PRIVATE'},
    {**diag.envelope('reported', 'auth_invalid'), 'v': True}])
def test_bad_old_json_never_breaks_state_update(job, old):
    db.log_patch_item(job.uid, 'proactive_jobs', 't815_job', {'status': 'failed', 'failure_diagnostic': old})
    job.store.update_proactive_job('t815_job', {'agent_action': 'no_class_change'})
    assert saved(job)['failure_diagnostic'] == diag.envelope('invalid')
    assert post(job, status='completed').status_code == 200
    assert saved(job)['failure_diagnostic'] is None


def test_pre_read_race_is_checked_on_actual_update_row(job, monkeypatch):
    original = proactive_core._find_proactive_job
    def read_then_other_consumer(store, job_id):
        snapshot = original(store, job_id)
        db.log_patch_item(job.uid, 'proactive_jobs', 't815_job', {'consumer_id': 'new_owner'})
        return snapshot
    monkeypatch.setattr(proactive_core, '_find_proactive_job', read_then_other_consumer)
    assert failure(job) == diag.envelope('binding_unavailable')
    # Legacy status/consumer write still happens; diagnostic does not claim fencing.
    assert saved(job)['status'] == 'failed'


def test_direct_store_cannot_forge_context_or_bypass_guard(job):
    job.store.update_proactive_job('t815_job', {'status': 'failed', 'failure_diagnostic': diag.envelope('reported', 'auth_invalid')},
        failure_report_context={'consumer_valid': True})
    assert saved(job)['failure_diagnostic'] == diag.envelope('missing')
    before = saved(job)
    assert job.store.update_proactive_job('t815_job', {'status': 'completed'}, only_if_status='pending') is None
    assert saved(job) == before


def test_reader_deduplicates_conflicts_without_identity_or_text_output(job):
    d = {**job.item, 'status': 'failed', 'failure_diagnostic': diag.envelope('reported', 'auth_invalid')}
    def row(uid, doc): return {'user_id': uid, 'job_id': 't815_job', 'doc': doc}
    rows = [row('a', d), row('a', d), row('b', d), row('b', {**d, 'status': 'completed'}),
            row('c', d), row('c', {**d, 'failure_diagnostic': None}),
            row('d', {**d, 'source': None}), row('e', {**d, 'status': 'pending'}),
            row('f', {**d, 'source': 'memory_dream'})]
    a = reader.summarize({'schema_version': 1, 'jobs': rows})
    assert a == reader.summarize({'schema_version': 1, 'jobs': list(reversed(rows))})
    assert (a['candidate_jobs'], a['confirmed_failed_jobs'], a['conflicting_candidate_jobs']) == (3, 2, 1)
    assert a['reported_classes']['auth_invalid'] == a['evidence_states']['diagnostic_conflict'] == 1
    assert a['scope_unknown_jobs'] == a['non_failed_jobs'] == a['out_of_scope_jobs'] == 1
    assert sum(a['evidence_states'].values()) == 2


def test_reader_cli_rejects_bad_input_without_echo(tmp_path):
    path = tmp_path / 'input.json'
    path.write_text('{"PRIVATE":')
    result = subprocess.run([sys.executable, str(Path(reader.__file__)), str(path)], capture_output=True, text=True)
    assert result.returncode == 2 and result.stdout == ''
    assert result.stderr == 'Invalid resident diagnostic snapshot\n'


def test_standalone_vocabulary_and_classifier_failure(monkeypatch):
    assert consumer._RESIDENT_FAILURE_DIAGNOSTIC_CLASSES == diag.ERROR_CLASSES
    monkeypatch.setattr(consumer, 'classify_agent_error', lambda exc: (_ for _ in ()).throw(ValueError('PRIVATE')))
    assert consumer._proactive_failure_diagnostic(RuntimeError('PRIVATE')) == wire('unknown')


def test_shadow_rechecks_its_own_row_and_guard_rejection_is_not_mirrored(job, monkeypatch):
    from tee_shadow import mirror
    shadow_uid = 'usr_t815_shadow_' + uuid.uuid4().hex[:10]
    seed_user(shadow_uid)
    shadow_store = stores.get_store(shadow_uid)
    shadow_store.append_proactive_job({**job.item, 'consumer_id': 'shadow_drift'})
    captured = []
    def shadow_execute(sql, params):
        captured.append((sql, params))
        # Execute exactly the mirror expression against an independently bound
        # isolated storage row; no Python recomputation of the CASE result.
        translated = list(params)
        translated[5] = translated[7] = shadow_uid
        with db.get_pool().connection() as conn:
            conn.execute(sql, tuple(translated)).fetchone()
    monkeypatch.setattr(mirror, 'execute', shadow_execute)
    assert failure(job) == diag.envelope('reported', 'auth_invalid')
    shadow = shadow_store.list_proactive_jobs()[0]
    assert shadow['status'] == 'failed'
    assert shadow['failure_diagnostic'] == diag.envelope('binding_unavailable')
    assert len(captured) == 1
    assert job.store.update_proactive_job('t815_job', {'status': 'completed'}, only_if_status='pending') is None
    assert len(captured) == 1


def test_authenticated_user_does_not_write_other_users_same_job(job):
    other_uid = 'usr_t815_other_' + uuid.uuid4().hex[:10]
    seed_user(other_uid)
    other_store = stores.get_store(other_uid)
    other_store.append_proactive_job({**job.item, 'consumer_id': 'resident_test'})
    before = other_store.list_proactive_jobs()
    assert failure(job) == diag.envelope('reported', 'auth_invalid')
    assert other_store.list_proactive_jobs() == before
    forged = {**wire(), 'user_id': other_uid, 'job_id': 't815_job'}
    assert failure(job, failure_diagnostic=forged) == diag.envelope('invalid')
    assert other_store.list_proactive_jobs() == before


def test_existing_outcome_catalog_is_independent_of_reported_claim(job):
    from notices.catalog import v1_proactive_outcome_class
    from notices.agent_call_failure import normalize_reason
    original = 'agent_call_failed: HTTP 401 PRIVATE'
    before = v1_proactive_outcome_class('failed', original)
    assert before == 'operational_failure'
    assert failure(job, reason=original) == diag.envelope('reported', 'auth_invalid')
    after = saved(job)
    assert normalize_reason(original) == after['status_reason'] == original
    assert v1_proactive_outcome_class(after['status'], after['status_reason']) == before


@pytest.mark.parametrize('old', [diag.envelope('reported', 'auth_invalid'), diag.envelope('conflicting_reports')])
def test_no_status_explicit_same_consumer_clears_old_projection(job, old):
    db.log_patch_item(job.uid, 'proactive_jobs', 't815_job', {'status': 'failed', 'failure_diagnostic': old})
    assert post(job, consumer_id='resident_test').status_code == 200
    assert saved(job)['failure_diagnostic'] == diag.envelope('missing')


def test_public_contract_documents_claim_and_preserves_permissive_input():
    path = '/v1/proactive/jobs/{job_id}/status'
    schema = json.loads((Path(__file__).resolve().parents[1] / 'docs-site/openapi/public.json').read_text())
    operation = schema['paths'][path]['post']
    assert 'not verified provider causes' in operation['description']
    body = operation['requestBody']['content']['application/json']['schema']
    assert body['additionalProperties'] is True
    assert 'type' not in body['properties']['failure_diagnostic']
    assert 'unknown-class claims degrade' in body['properties']['failure_diagnostic']['description']


@pytest.mark.parametrize('source', ['memory_capture', 'memory_dream', None])
def test_non_target_job_does_not_acquire_reported_claim(job, source):
    db.log_patch_item(job.uid, 'proactive_jobs', 't815_job', {'source': source})
    assert failure(job) is None
    assert saved(job)['status'] == 'failed'


@pytest.mark.parametrize('snapshot', [
    {'schema_version': True, 'jobs': []},
    {'schema_version': 1, 'jobs': [{'user_id': 'x', 'job_id': 'j', 'doc': {'job_id': 'other', 'status': 'failed'}}]},
    {'schema_version': 1, 'jobs': [{'user_id': 'x', 'job_id': 'j', 'doc': {'job_id': 'j', 'status': 'failed', 'source': []}}]},
])
def test_reader_invalid_identity_and_schema_fail_closed(snapshot):
    with pytest.raises(ValueError, match='^invalid snapshot$'):
        reader.summarize(snapshot)


def test_reader_row_limit(monkeypatch):
    monkeypatch.setattr(reader, 'MAX_RECORDS', 0)
    with pytest.raises(ValueError, match='^invalid snapshot$'):
        reader.summarize({'schema_version': 1, 'jobs': [{}]})
