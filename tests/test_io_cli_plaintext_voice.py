"""CLI argv/urllib coverage; synthetic loopback only, no provider/decrypt service."""
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
MISSING = object()


@pytest.fixture
def voice_cli(tmp_path):
    requests = []
    state = {'mode': {'user_id': 'usr_voice', 'content_encryption_effective': 'off'},
             'mode_status': 200, 'status': 200,
             'envelope': {'owner_user_id': 'usr_voice', 'body': '你好🌍'},
             'decrypt_status': 200}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            if self.path == '/v1/users/whoami':
                code, body = state['mode_status'], state['mode']
            else:
                code = state['status']
                body = {'transcript': state['envelope'], 'turn_count': 3,
                        'duration_sec': 90, 'created_at': 'fixture-time'}
            self.reply(code, body)

        def do_POST(self):
            requests.append((self.path, dict(self.headers)))
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.reply(state['decrypt_status'], {'plaintext_b64': base64.b64encode(
                'legacy全文'.encode()).decode()})

        def reply(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'

    def run(*, offset=0, token=False, enclave=True):
        requests.clear()
        env = {'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1',
               'FEEDLING_API_URL': url, 'NO_PROXY': '*', 'no_proxy': '*'}
        if token:
            path = tmp_path / 'synthetic-token'
            path.write_text('fixture-token')
            env['FEEDLING_RUNTIME_TOKEN_FILE'] = str(path)
        else:
            env['FEEDLING_API_KEY'] = 'fixture-key'
        if enclave:
            env['FEEDLING_ENCLAVE_URL'] = url + '/enclave'
        result = subprocess.run([sys.executable, str(ROOT / 'tools/io_cli.py'),
            'voice-transcript-read', '--call-id', 'vcall_fixture', '--offset', str(offset)],
            env=env, capture_output=True, text=True, timeout=10)
        assert not result.stderr, result.stderr
        return result.returncode, json.loads(result.stdout)

    yield state, requests, run
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
    assert not thread.is_alive()


@pytest.mark.parametrize('token', [False, True])
@pytest.mark.parametrize('binary', [False, True])
def test_plaintext_full_pagination(voice_cli, token, binary):
    state, requests, run = voice_cli
    text = '你好🌍\n' * 3201
    state['envelope'] = {'owner_user_id': 'usr_voice',
                         **({'body_b64': base64.b64encode(text.encode()).decode()}
                            if binary else {'body': text})}
    pages, offset = [], 0
    while offset is not None:
        code, body = run(offset=offset, token=token, enclave=False)
        assert code == 0, body
        assert body['offset'] == offset and len(body['text']) <= 6000
        assert body['total_chars'] == len(text)
        assert (body['turn_count'], body['duration_sec'], body['created_at']) == (3, 90, 'fixture-time')
        assert all('/enclave/' not in path for path, _ in requests)
        assert len(requests) == 2
        assert all(headers.get('X-Feedling-Runtime-Token' if token else 'X-Api-Key')
                   == ('fixture-token' if token else 'fixture-key') for _, headers in requests)
        pages.append(body['text'])
        offset = body['next_offset']
    assert ''.join(pages) == text


@pytest.mark.parametrize('offset', [-3, 0, 9000])
def test_empty_text_and_offsets(voice_cli, offset):
    state, requests, run = voice_cli
    state['envelope']['body'] = ''
    code, body = run(offset=offset)
    assert code == 0 and body['text'] == '' and body['next_offset'] is None
    assert body['offset'] == max(0, offset)
    assert not any('/enclave/' in p for p, _ in requests)


@pytest.mark.parametrize('envelope', [
    {'owner_user_id': 'usr_voice', 'body_ct': 'sealed'},
    {'owner_user_id': 'usr_voice', 'body_ct': 'sealed', 'body': 'stale'},
    {'owner_user_id': 'foreign', 'body': 'private'}, {'body': 'owner missing'},
    {'owner_user_id': 'usr_voice'}, {'owner_user_id': 'usr_voice', 'body': 3},
    {'owner_user_id': 'usr_voice', 'body_b64': '%%%bad'},
    {'owner_user_id': 'usr_voice', 'body_b64': '/w=='},
    {'owner_user_id': 'usr_voice', 'body_b64': 4},
    {'owner_user_id': 'usr_voice', 'body_b64': None, 'body': 'stale'},
    {'owner_user_id': 'usr_voice', 'body_b64': 'YQ==', 'body': 'stale'},
    {'owner_user_id': 'usr_voice', 'body_ct': '', 'body': 'stale'},
    {'owner_user_id': 'usr_voice', 'body': '\ud800'}, None,
])
def test_off_unreadable_never_decrypts(voice_cli, envelope):
    state, requests, run = voice_cli
    state['envelope'] = envelope
    code, body = run()
    assert code != 0 and not body['ok']
    assert not any('/enclave/' in p for p, _ in requests)


@pytest.mark.parametrize('status', [401, 404, 500])
def test_read_error_never_decrypts(voice_cli, status):
    state, requests, run = voice_cli
    state['status'] = status
    code, body = run()
    assert code != 0 and not body['ok']
    assert not any('/enclave/' in p for p, _ in requests)


@pytest.mark.parametrize('status,account', [(401, {}), (503, {}), (200, []),
    (200, {}), (200, {'user_id': ''}), (200, {'user_id': 4}), *[(200, {'user_id': 'usr_voice',
    'content_encryption_effective': v}) for v in (None, False, 1, [], {})]])
def test_mode_failure_stops_before_content(voice_cli, status, account):
    state, requests, run = voice_cli
    state.update(mode_status=status, mode=account)
    code, body = run()
    assert code != 0 and not body['ok']
    assert [p for p, _ in requests] == ['/v1/users/whoami']


@pytest.mark.parametrize('mode', ['on', '', 'unknown', 'OFF', MISSING])
def test_legal_compatibility_retains_decrypt(voice_cli, mode):
    state, requests, run = voice_cli
    state['mode'] = {'user_id': 'usr_voice'}
    if mode is not MISSING:
        state['mode']['content_encryption_effective'] = mode
    state['envelope'] = {'body_ct': 'sealed'}
    code, body = run()
    assert code == 0 and body['text'] == 'legacy全文'
    assert [p for p, _ in requests][-1] == '/enclave/v1/envelope/decrypt'


@pytest.mark.parametrize('mode', ['on', ''])
def test_compatibility_errors_preserved(voice_cli, mode):
    state, requests, run = voice_cli
    state['mode']['content_encryption_effective'] = mode
    state['envelope'] = {'body_ct': 'sealed'}
    code, body = run(enclave=False)
    assert code == 2 and 'FEEDLING_ENCLAVE_URL' in body['error']
    state['decrypt_status'] = 503
    code, body = run()
    assert code == 1 and body['http_status'] == 503


def test_mode_transport_timeout_fails_closed(tmp_path, monkeypatch):
    # Exercise the real urllib error adapter and argparse, injecting only socket IO.
    import socket
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    import io_cli
    monkeypatch.setenv('FEEDLING_API_URL', 'http://127.0.0.1:1')
    monkeypatch.setenv('FEEDLING_API_KEY', 'fixture-key')
    monkeypatch.delenv('FEEDLING_RUNTIME_TOKEN_FILE', raising=False)
    monkeypatch.delenv('FEEDLING_TRACE_ID', raising=False)
    seen = []

    def timeout(request, **kwargs):
        seen.append(request.full_url)
        raise socket.timeout('synthetic timeout')

    monkeypatch.setattr(io_cli.urllib.request, 'urlopen', timeout)
    monkeypatch.setattr(sys, 'argv', ['io_cli', 'voice-transcript-read',
                                    '--call-id', 'vcall_fixture'])
    with pytest.raises(SystemExit) as exc:
        io_cli.main()
    assert exc.value.code == 1
    assert seen == ['http://127.0.0.1:1/v1/users/whoami']


@pytest.mark.parametrize('token', [False, True])
def test_real_backend_auth_storage_and_cross_user(tmp_path, monkeypatch, token):
    """CLI subprocess → loopback HTTP → actual ASGI auth/route → isolated PG."""
    import db
    from asgi_test_client import make_client
    from core import config, enclave, envelope, runtime_token
    from psycopg.types.json import Jsonb
    import uuid

    # conftest creates a unique disposable database; never allow non-loopback DB.
    from urllib.parse import urlsplit
    assert urlsplit(os.environ['DATABASE_URL']).hostname in ('127.0.0.1', 'localhost')
    assert urlsplit(os.environ['DATABASE_URL']).path.startswith('/feedling_test_')
    monkeypatch.setattr(config, 'FEEDLING_DIR', tmp_path)
    monkeypatch.setattr(enclave, '_get_enclave_info', lambda: None)  # attestation metadata only
    monkeypatch.setattr(envelope, 'PLAINTEXT_WRITES_ACCEPTED', True)
    monkeypatch.setenv('FEEDLING_RUNTIME_TOKEN_SECRET', 'synthetic-voice-secret')
    client = make_client()
    identities = []
    for _ in range(2):
        response = client.post('/v1/users/register', json={
            'public_key': base64.b64encode(os.urandom(32)).decode(), 'archive_language': 'en'})
        assert response.status_code == 201, response.get_json()
        identities.append(response.get_json())
    owner, stranger = identities
    call_id = 'vcall_' + uuid.uuid4().hex
    text = '真实隔离存储全文🌏' * 800
    with db.get_pool().connection() as conn:
        conn.execute('INSERT INTO voice_transcripts '
                     '(user_id, call_id, turn_count, duration_sec, char_count, transcript_envelope) '
                     'VALUES (%s,%s,2,60,%s,%s)',
                     (owner['user_id'], call_id, len(text), Jsonb({
                         'id': 'vtx_' + call_id, 'owner_user_id': owner['user_id'],
                         'visibility': 'shared', 'body': text})))
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            response = client.get(self.path, headers=dict(self.headers))
            data = response.get_data()
            self.send_response(response.status_code)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            requests.append(self.path)
            self.send_error(503, 'enclave content must never be called')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def invoke(identity, *, offset=0, invalid=False):
        env = {'PATH': os.environ['PATH'], 'NO_PROXY': '*', 'no_proxy': '*',
               'PYTHONDONTWRITEBYTECODE': '1',
               'FEEDLING_API_URL': f'http://127.0.0.1:{server.server_port}',
               'FEEDLING_ENCLAVE_URL': f'http://127.0.0.1:{server.server_port}/forbidden'}
        if token:
            path = tmp_path / 'token'
            path.write_text('invalid' if invalid else runtime_token.mint(
                b'synthetic-voice-secret', user_id=identity['user_id'],
                runtime_instance_id='synthetic', scope=['chat', 'memory', 'identity']))
            env['FEEDLING_RUNTIME_TOKEN_FILE'] = str(path)
        else:
            env['FEEDLING_API_KEY'] = 'invalid' if invalid else identity['api_key']
        result = subprocess.run([sys.executable, str(ROOT / 'tools/io_cli.py'),
            'voice-transcript-read', '--call-id', call_id, '--offset', str(offset)],
            env=env, capture_output=True, text=True, timeout=10)
        assert not result.stderr, result.stderr
        return result.returncode, json.loads(result.stdout)

    try:
        pages, offset = [], 0
        while offset is not None:
            code, body = invoke(owner, offset=offset)
            assert code == 0, body
            pages.append(body['text'])
            offset = body['next_offset']
        assert ''.join(pages) == text
        code, body = invoke(stranger)
        assert code == 1 and body['http_status'] == 404, body
        before = len(requests)
        code, body = invoke(owner, invalid=True)
        assert code == 1 and body['http_status'] == 401, body
        assert requests[before:] == ['/v1/users/whoami']
        assert not any('/forbidden' in path for path in requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()
