"""Bounded old/new read-error comparison. Captures intent, never writes a card.

The legacy Genesis re-init residual is deliberately observable here. Equal
behavior is not an assertion that re-init after a read failure is safe.
"""
import json
from types import SimpleNamespace

import httpx
import pytest

from asgi_test_client import _AsgiTestClient
from core import enclave as core_enclave
from enclave import auth, backend_client, state
from enclave.routes import build_app
from genesis import service as genesis
from identity import actions, service


@pytest.fixture
def error_chain(monkeypatch):
    mode = {'status': 200}
    calls = []
    monkeypatch.setitem(state._state, 'ready', True)
    auth.reset_cache()
    async def backend_get(path, headers, params=None):
        calls.append(path)
        if path == '/v1/users/whoami':
            return {'user_id': 'usr_comparison'}
        assert path == '/v1/identity/get'
        if mode['status'] == 200:
            return {'identity': None}
        response = httpx.Response(503, json={'error': 'identity_read_unavailable'},
                                  request=httpx.Request('GET', 'http://backend.invalid' + path))
        response.raise_for_status()
    monkeypatch.setattr(backend_client, 'backend_get', backend_get)
    client = _AsgiTestClient(build_app())
    observed = []
    def enclave_get(url, *, headers, params, timeout):
        assert url == 'http://enclave.invalid/v1/identity/get'
        result = client.get('/v1/identity/get', headers=headers)
        observed.append((result.status_code, result.get_json()))
        return httpx.Response(result.status_code, json=result.get_json(), request=httpx.Request('GET', url))
    monkeypatch.setenv('FEEDLING_ENCLAVE_URL', 'http://enclave.invalid')
    monkeypatch.setattr(core_enclave, '_client', lambda: SimpleNamespace(get=enclave_get))
    yield mode, calls, observed
    auth.reset_cache()


def test_genesis_legacy_residual_old_and_new_intent_equal(monkeypatch, error_chain, capsys):
    mode, calls, observed = error_chain
    store = SimpleNamespace(user_id='usr_comparison')
    stored = {'id': 'identity_fixture', 'v': 1, 'owner_user_id': store.user_id,
              'body_ct': 'sealed', 'visibility': 'shared', 'created_at': 'fixed',
              'relationship_anchor_source': genesis.GENESIS_SOURCE}
    monkeypatch.setattr(service, '_load_identity', lambda store: dict(stored))
    intents = []
    def build(store, raw, **kw):
        intents.append(('build', json.loads(raw)))
        return {'id': 'identity_fixture', 'owner_user_id': store.user_id,
                'body': raw.decode(), 'visibility': 'shared'}, ''
    monkeypatch.setattr(genesis.core_envelope, '_build_shared_envelope_for_store', build)
    monkeypatch.setattr(service, '_save_identity', lambda store, doc: intents.append(('write', json.loads(doc['body']))))
    monkeypatch.setattr(genesis, '_relationship_anchor_from_output', lambda *a: '2026-01-01')
    monkeypatch.setattr(genesis.boot_gates, '_log_bootstrap_event', lambda *a, **k: None)
    monkeypatch.setattr(service, '_append_identity_change', lambda *a, **k: None)
    output = {'identity': {'agent_name': 'New', 'dimensions': [
        {'name': 'Curiosity', 'value': 60, 'description': 'fixture'}]}}
    outcomes = []
    for source in (genesis.GENESIS_SOURCE, 'user_explicit'):
        stored['relationship_anchor_source'] = source
        pair = []
        for status in (200, 503):
            mode['status'] = status
            intents.clear()
            normalized, error = genesis._existing_identity_plain_for_update(store, 'fixture')
            assert normalized is None
            if status == 200:
                assert error == 'identity_plain_not_available'
            else:
                assert error.startswith('enclave_http_502:')
            result = genesis.init_identity_if_absent(store, output, api_key='fixture')
            pair.append((result, list(intents)))
            outcomes.append({'backend_status': status, 'source': source, 'normalized': error,
                             'enclave_status': observed[-1][0], 'result': result, 'intents': list(intents)})
        assert pair[0] == pair[1]
        if source == genesis.GENESIS_SOURCE:
            assert pair[0][0] == 'updated'
            assert [kind for kind, _ in pair[0][1]] == ['build', 'write']
            assert pair[0][1][1][1]['self_introduction'] == ''  # existing residual, NOT safe
        else:
            assert pair == [('already_initialized', []), ('already_initialized', [])]
    with capsys.disabled():
        print('LEGACY_GENESIS_RESIDUAL_OBSERVATION=' + json.dumps(outcomes, ensure_ascii=False))
    assert set(calls) == {'/v1/users/whoami', '/v1/identity/get'}


@pytest.mark.parametrize('status', [200, 503])
def test_safe_update_consumers_stop_before_build(monkeypatch, error_chain, status):
    mode, _, _ = error_chain
    mode['status'] = status
    store = SimpleNamespace(user_id='usr_comparison')
    stored = {'id': 'fixture', 'owner_user_id': store.user_id, 'body_ct': 'sealed', 'visibility': 'shared'}
    monkeypatch.setattr(service, '_load_identity', lambda store: dict(stored))
    def forbidden(*a, **kw):
        raise AssertionError('read failure must not build or save')
    monkeypatch.setattr(genesis.core_envelope, '_build_shared_envelope_for_store', forbidden)
    monkeypatch.setattr(service, '_save_identity', forbidden)
    monkeypatch.setattr(service, '_save_identity_cas', forbidden)
    result, effects, code = actions._identity_profile_patch(
        store, 'fixture', {'patch': {'self_introduction': 'new introduction'}})
    assert code == 409 and not effects
    result, effects, code = actions._identity_dimension_nudge(
        store, 'fixture', {'dimension': 'Curiosity', 'delta': 1})
    assert code == 409 and not effects
    result, effects, code = actions._identity_dimensions_set(
        store, 'fixture', {'dimensions': [{'name': 'Curiosity', 'value': 60}], 'reason': 'fixture'})
    assert code == 409 and not effects
    result = genesis.replace_identity_preserving_anchor(
        store, {'identity': {'agent_name': 'New', 'self_introduction': 'text'}}, api_key='fixture')
    assert result == 'identity_plain_unavailable'


@pytest.mark.parametrize('status', [500, 503])
def test_supervisor_read_failure_does_not_enqueue_or_mark(monkeypatch, status):
    from agent_runtime import supervisor
    response = httpx.Response(status, json={'error': 'identity_read_unavailable'},
                              request=httpx.Request('GET', 'http://backend.invalid/v1/identity/get'))
    monkeypatch.setattr(supervisor.httpx, 'get', lambda *a, **k: response)
    def forbidden(*a, **kw):
        raise AssertionError('failure cannot mark introduced')
    store = SimpleNamespace(proactive_activation_ready=lambda: True,
                            introduction_done=lambda: False, mark_introduced=forbidden)
    identity, reason = supervisor._fetch_identity_plain_for_intro(
        {'api_key': 'fixture'}, api_url='http://backend.invalid', enclave_url='http://enclave.invalid')
    assert identity is None and reason.startswith('identity_fetch_failed:')
    assert supervisor._enqueue_introduction_job_if_needed(
        'usr_comparison', {'api_key': 'fixture'}, api_url='http://backend.invalid',
        enclave_url='http://enclave.invalid', get_store_fn=lambda uid: store) is None


@pytest.mark.parametrize('status', [500, 503])
def test_resident_read_error_remains_empty_view(monkeypatch, tmp_path, status):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
    monkeypatch.setenv('FEEDLING_API_URL', 'http://backend.invalid')
    monkeypatch.setenv('FEEDLING_API_KEY', 'fixture')
    monkeypatch.setenv('FEEDLING_DATA_DIR', str(tmp_path))
    import chat_resident_consumer as resident
    response = httpx.Response(status, json={'error': 'identity_read_unavailable'},
                              request=httpx.Request('GET', 'http://backend.invalid/v1/identity/get'))
    seen = []
    def get(url, **kw):
        seen.append(url)
        return response
    monkeypatch.setattr(resident, '_client_for', lambda root: SimpleNamespace(get=get))
    monkeypatch.setattr(resident, '_refresh_auth_header', lambda: None)
    monkeypatch.setattr(resident, 'FEEDLING_API_URL', 'http://backend.invalid')
    assert resident._resident_identity_view() == {}  # existing limitation, not fixed
    assert seen == ['http://backend.invalid/v1/identity/get']
