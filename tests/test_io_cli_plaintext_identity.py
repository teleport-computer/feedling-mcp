"""Identity CLI wire/owner/shape contract; loopback fixtures, never a provider."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import io_cli


@pytest.fixture
def cli(tmp_path):
    seen = []
    state = {'mode': {'user_id': 'usr_identity', 'content_encryption_effective': 'off'},
             'mode_status': 200, 'status': 200,
             'body': {'identity': {'owner_user_id': 'usr_identity', 'body': '{}',
                                   'days_with_user': 7, 'v': 1}},
             'enclave_status': 200,
             'enclave_body': {'identity': {'agent_name': 'legacy', 'decrypt_status': 'ok'}}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append((self.path, dict(self.headers)))
            if self.path == '/v1/users/whoami':
                status, body = state['mode_status'], state['mode']
            elif self.path.startswith('/enclave/'):
                status, body = state['enclave_status'], state['enclave_body']
            else:
                status, body = state['status'], state['body']
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'

    def run(*, token=False, backend=True, enclave=True):
        seen.clear()
        env = {'PATH': os.environ['PATH'], 'NO_PROXY': '*', 'no_proxy': '*',
               'PYTHONDONTWRITEBYTECODE': '1'}
        if backend:
            env['FEEDLING_API_URL'] = url
        if enclave:
            env['FEEDLING_ENCLAVE_URL'] = url + '/enclave'
        if token:
            path = tmp_path / 'synthetic-token'
            path.write_text('fixture-token')
            env['FEEDLING_RUNTIME_TOKEN_FILE'] = str(path)
        else:
            env['FEEDLING_API_KEY'] = 'fixture-key'
        p = subprocess.run([sys.executable, str(ROOT / 'tools/io_cli.py'), 'identity-read'],
                           env=env, capture_output=True, text=True, timeout=10)
        assert not p.stderr, p.stderr
        return p.returncode, json.loads(p.stdout)

    yield state, seen, run
    server.shutdown()
    server.server_close()
    thread.join(2)
    assert not thread.is_alive()


@pytest.mark.parametrize('token', [False, True])
@pytest.mark.parametrize('binary', [False, True])
def test_all_fields_and_backend_days(cli, token, binary):
    from identity import card_policy, card_view
    state, seen, run = cli
    inner = {key: '值🌏' for key in card_policy.PROFILE_STRING_FIELDS}
    inner.update({key: ['one', '二'] for key in card_policy.PROFILE_LIST_FIELDS})
    inner.update(dimensions=[{'name': '亲近', 'value': 42}], days_with_user=999,
                 last_dimensions_set={'reason': 'user request'})
    stored = state['body']['identity']
    raw = json.dumps(inner, ensure_ascii=False)
    if binary:
        del stored['body']
        stored['body_b64'] = base64.b64encode(raw.encode()).decode()
    else:
        stored['body'] = raw
    stored.update(created_at='created', updated_at='updated', replaced_at='replaced')
    code, body = run(token=token, enclave=False)
    assert code == 0, body
    assert body == {'ok': True, 'user_id': 'usr_identity', 'identity': card_view.plaintext_view(
        card_view.envelope_base(stored), inner, stored, days_with_user=7)}
    assert {h.get('X-Feedling-Runtime-Token' if token else 'X-Api-Key') for _, h in seen} == {
        'fixture-token' if token else 'fixture-key'}
    assert [p for p, _ in seen] == ['/v1/users/whoami', '/v1/identity/get']


@pytest.mark.parametrize('inner', [{}, {'agent_name': ''}, {'signature': [], 'custom_persona_prompt': ''}])
def test_sparse_and_empty_objects_are_valid(cli, inner):
    state, seen, run = cli
    state['body']['identity']['body'] = json.dumps(inner)
    code, body = run()
    assert code == 0 and body['identity']['decrypt_status'] == 'ok'
    assert not any('/enclave/' in p for p, _ in seen)


@pytest.mark.parametrize('stored', [None, {'owner_user_id': 'usr_identity', 'visibility': 'local_only',
                                         'body_ct': 'private', 'body': 'private', 'v': 1}])
def test_absent_and_local_only_never_read_content(cli, stored):
    state, seen, run = cli
    state['body']['identity'] = stored
    code, body = run()
    assert code == 0
    if stored is None:
        assert body['identity'] is None
    else:
        assert body['identity'] == {'v': 1, 'created_at': None, 'updated_at': None,
            'visibility': 'local_only', 'decrypt_status': 'local_only_agent_cannot_read'}
    assert 'private' not in json.dumps(body)
    assert not any('/enclave/' in p for p, _ in seen)


@pytest.mark.parametrize('patch', [
    {'body_ct': 'sealed'}, {'body_ct': ''}, {'body_ct': None}, {'body_b64': 'e30='},
    {'owner_user_id': 'foreign'}, {'owner_user_id': None},
    {'body': None}, {'body': 1}, {'body': ''}, {'body': '{'},
    {'body': 'null'}, {'body': '[]'}, {'body': '"text"'}, {'body': '\ud800'},
    {'days_with_user': None}, {'days_with_user': True}, {'days_with_user': -1}, {'v': 'bad'},
])
def test_off_bad_envelope_fails_without_fallback(cli, patch):
    state, seen, run = cli
    state['body']['identity'].update(patch)
    code, body = run()
    assert code == 1 and not body['ok']
    assert not any('/enclave/' in p for p, _ in seen)


@pytest.mark.parametrize('body', [{}, {'identity': []}, {'identity': {}},
                                  {'identity': None, 'user_id': 'foreign'}])
def test_response_shape_is_not_absence(cli, body):
    state, seen, run = cli
    state['body'] = body
    code, out = run()
    assert code == 1 and not out['ok']
    assert not any('/enclave/' in p for p, _ in seen)


@pytest.mark.parametrize('binary', ['!', '/w==', 4, None])
def test_bad_binary(cli, binary):
    state, seen, run = cli
    del state['body']['identity']['body']
    state['body']['identity']['body_b64'] = binary
    code, _ = run()
    assert code == 1 and not any('/enclave/' in p for p, _ in seen)


@pytest.mark.parametrize('mode', ['on', '', 'future', 'OFF', 'missing'])
def test_unknown_is_legacy_not_off(cli, mode):
    state, seen, run = cli
    if mode == 'missing':
        del state['mode']['content_encryption_effective']
    else:
        state['mode']['content_encryption_effective'] = mode
    code, body = run()
    assert code == 0 and body['identity']['agent_name'] == 'legacy'
    assert [p for p, _ in seen] == ['/v1/users/whoami', '/enclave/v1/identity/get']


@pytest.mark.parametrize('mode', [None, 1, False, {}, []])
def test_malformed_mode_stops_before_content(cli, mode):
    state, seen, run = cli
    state['mode']['content_encryption_effective'] = mode
    code, _ = run()
    assert code == 1 and [p for p, _ in seen] == ['/v1/users/whoami']


@pytest.mark.parametrize('body', [None, [], {}, {'user_id': ''}, {'user_id': 4}])
def test_missing_identity_in_mode_stops(cli, body):
    state, seen, run = cli
    state['mode'] = body
    code, _ = run()
    assert code == 1 and [p for p, _ in seen] == ['/v1/users/whoami']


@pytest.mark.parametrize('status', [401, 404, 500, 503])
@pytest.mark.parametrize('stage', ['mode_status', 'status'])
def test_http_failure_is_not_empty(cli, status, stage):
    state, seen, run = cli
    state[stage] = status
    code, body = run()
    assert code == 1 and body['http_status'] == status
    assert not any('/enclave/' in p for p, _ in seen)


def test_enclave_only_legacy_configuration(cli):
    _, seen, run = cli
    code, body = run(backend=False)
    assert code == 0 and body['identity']['agent_name'] == 'legacy'
    assert [p for p, _ in seen] == ['/enclave/v1/identity/get']


@pytest.mark.parametrize('token', [False, True])
def test_real_backend_auth_storage_and_faults(tmp_path, monkeypatch, token):
    """Actual CLI → loopback HTTP → assembled ASGI auth → isolated PostgreSQL."""
    import db
    from asgi_test_client import make_client
    from core import config, enclave, envelope, runtime_token
    from identity import service
    from psycopg.types.json import Jsonb
    from urllib.parse import urlsplit

    assert urlsplit(os.environ['DATABASE_URL']).hostname in ('127.0.0.1', 'localhost')
    assert urlsplit(os.environ['DATABASE_URL']).path.startswith('/feedling_test_')
    monkeypatch.setattr(config, 'FEEDLING_DIR', tmp_path)
    monkeypatch.setattr(enclave, '_get_enclave_info', lambda: None)  # public attestation only
    monkeypatch.setattr(envelope, 'PLAINTEXT_WRITES_ACCEPTED', True)
    monkeypatch.setenv('FEEDLING_RUNTIME_TOKEN_SECRET', 'synthetic-identity-secret')
    client = make_client()
    users = []
    for _ in range(2):
        r = client.post('/v1/users/register', json={
            'public_key': base64.b64encode(os.urandom(32)).decode(), 'archive_language': 'en'})
        assert r.status_code == 201, r.get_json()
        users.append(r.get_json())
    owner, stranger = users
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            seen.append(self.path)
            r = client.get(self.path, headers=dict(self.headers))
            data = r.get_data()
            self.send_response(r.status_code)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def invoke(user, invalid=False):
        env = {'PATH': os.environ['PATH'], 'NO_PROXY': '*', 'no_proxy': '*',
               'PYTHONDONTWRITEBYTECODE': '1',
               'FEEDLING_API_URL': f'http://127.0.0.1:{server.server_port}',
               'FEEDLING_ENCLAVE_URL': f'http://127.0.0.1:{server.server_port}/forbidden'}
        if token:
            path = tmp_path / 'token'
            path.write_text('invalid' if invalid else runtime_token.mint(
                b'synthetic-identity-secret', user_id=user['user_id'],
                runtime_instance_id='synthetic', scope=['identity']))
            env['FEEDLING_RUNTIME_TOKEN_FILE'] = str(path)
        else:
            env['FEEDLING_API_KEY'] = 'invalid' if invalid else user['api_key']
        p = subprocess.run([sys.executable, str(ROOT / 'tools/io_cli.py'), 'identity-read'],
                           env=env, capture_output=True, text=True, timeout=10)
        assert not p.stderr, p.stderr
        return p.returncode, json.loads(p.stdout)

    def store(value):
        with db.get_pool().connection() as conn:
            conn.execute('INSERT INTO user_blobs (user_id,kind,doc) VALUES (%s,%s,%s) '
                         'ON CONFLICT (user_id,kind) DO UPDATE SET doc=EXCLUDED.doc',
                         (owner['user_id'], 'identity', Jsonb(value)))

    try:
        code, body = invoke(owner)
        assert code == 0 and body['identity'] is None
        stored = {'v': 1, 'owner_user_id': owner['user_id'], 'body': json.dumps({
            'agent_name': '隔离身份🌏', 'days_with_user': 999}),
            'relationship_started_at': '2026-01-01', 'relationship_anchor_source': 'explicit'}
        store(stored)
        code, body = invoke(owner)
        assert code == 0 and body['identity']['agent_name'] == '隔离身份🌏'
        assert body['identity']['days_with_user'] == service._live_days_with_user(stored)
        code, body = invoke(stranger)
        assert code == 0 and body['identity'] is None  # cannot read owner's singleton
        before = len(seen)
        code, body = invoke(owner, invalid=True)
        assert code == 1 and body['http_status'] == 401
        assert seen[before:] == ['/v1/users/whoami']
        for malformed in (None, [], 'broken'):
            store(malformed)
            code, body = invoke(owner)
            assert code == 1 and body['http_status'] == 500
            assert body['error'] == {'error': 'identity_storage_invalid'}
        for patch in ({'owner_user_id': stranger['user_id']}, {'body_ct': 'old-sealed'},
                      {'body': 'malformed'}):
            store({**stored, **patch})
            code, body = invoke(owner)
            assert code == 1 and not body['ok']
        store(stored)
        # Exercise an actual SQL failure, not a pre-filled error response. A
        # missing table is queried only on this identity read path, in test DB.
        original = db.get_blobs_for_users
        def sql_failure(*args, **kwargs):
            assert args == ([owner['user_id']], ['identity'])
            assert kwargs == {'raise_on_error': True}
            with db.get_pool().connection() as conn:
                conn.execute('SELECT doc FROM t817_deliberately_missing_relation')
        monkeypatch.setattr(db, 'get_blobs_for_users', sql_failure)
        code, body = invoke(owner)
        assert code == 1 and body == {'ok': False, 'http_status': 503,
                                      'error': {'error': 'identity_read_unavailable'}}
        monkeypatch.setattr(db, 'get_blobs_for_users', original)
        assert not any('/forbidden' in path for path in seen)
        with db.get_pool().connection() as conn:
            row = conn.execute('SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s',
                               (owner['user_id'], 'identity')).fetchone()
        assert row[0] == stored  # all reads/failures left the card untouched
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        assert not thread.is_alive()


def test_mode_timeout_stops_before_content(monkeypatch, capsys):
    import socket
    seen = []
    monkeypatch.setenv('FEEDLING_API_URL', 'http://127.0.0.1:1')
    monkeypatch.setenv('FEEDLING_API_KEY', 'fixture')
    def timeout(request, **kw):
        seen.append(request.full_url)
        raise socket.timeout('synthetic timeout')
    monkeypatch.setattr(io_cli.urllib.request, 'urlopen', timeout)
    monkeypatch.setattr(sys, 'argv', ['io_cli', 'identity-read'])
    with pytest.raises(SystemExit) as exc:
        io_cli.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)['ok'] is False
    assert seen == ['http://127.0.0.1:1/v1/users/whoami']


def test_nonfinite_metadata_returns_json_failure(cli):
    state, seen, run = cli
    state['body']['identity']['v'] = float('inf')
    code, body = run()
    assert code == 1 and body['ok'] is False
    assert not any('/enclave/' in p for p, _ in seen)


@pytest.mark.parametrize('binary', [False, True])
@pytest.mark.parametrize('field', ['agent_name', 'signature', 'last_dimensions_set'])
def test_escaped_surrogate_in_projected_body_is_json_failure(cli, binary, field):
    state, seen, run = cli
    value = {'agent_name': '\ud800', 'signature': ['\udfff'],
             'last_dimensions_set': {'reason': '\ud800'}}[field]
    raw = json.dumps({field: value})  # ASCII JSON escapes, valid raw UTF-8
    stored = state['body']['identity']
    if binary:
        del stored['body']
        stored['body_b64'] = base64.b64encode(raw.encode()).decode()
    else:
        stored['body'] = raw
    code, body = run()
    assert code == 1 and body['ok'] is False
    assert not any('/enclave/' in p for p, _ in seen)


@pytest.mark.parametrize('field', ['created_at', 'updated_at', 'replaced_at'])
@pytest.mark.parametrize('local_only', [False, True])
def test_surrogate_metadata_is_json_failure(cli, field, local_only):
    state, seen, run = cli
    stored = state['body']['identity']
    stored[field] = '\ud800'
    if local_only:
        stored['visibility'] = 'local_only'
    code, body = run()
    assert code == 1 and body['ok'] is False
    assert not any('/enclave/' in p for p, _ in seen)


def test_valid_escaped_surrogate_pair_remains_unicode(cli):
    state, _, run = cli
    state['body']['identity']['body'] = r'{"agent_name":"\ud83c\udf0f"}'
    code, body = run()
    assert code == 0 and body['identity']['agent_name'] == '🌏'
