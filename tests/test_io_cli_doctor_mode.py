"""Real doctor subprocesses against authenticated loopback probes; no provider."""
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

KEYS = ['api', 'enclave', 'identity', 'memory', 'chat']


@pytest.fixture
def cli(tmp_path):
    state = {'account': {'user_id': 'fixture-user', 'content_encryption_effective': 'off'},
             'mode_status': 200, 'raw_mode': None, 'statuses': {}}
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def handle_probe(self):
            length = int(self.headers.get('Content-Length', '0'))
            body = self.rfile.read(length) if length else b''
            seen.append((self.command, self.path, dict(self.headers), body))
            authenticated = (self.headers.get('X-API-Key') == 'fixture-key'
                             or self.headers.get('X-Feedling-Runtime-Token') == 'fixture-token')
            if not authenticated:
                status, payload = 401, b'{"error":"unauthorized"}'
            elif self.path == '/v1/users/whoami':
                status = state['mode_status']
                payload = state['raw_mode'] if state['raw_mode'] is not None else json.dumps(state['account']).encode()
            else:
                status = state['statuses'].get(self.path, 200)
                # Empty identity and collections are valid reachability responses.
                payload = b'{"identity":null,"items":[],"messages":[]}'
            self.send_response(status)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = handle_probe
        do_POST = handle_probe

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'

    def run(*, token=False, enclave=True, backend=True, auth='valid'):
        seen.clear()
        env = {'PATH': os.environ['PATH'], 'NO_PROXY': '*', 'no_proxy': '*',
               'PYTHONDONTWRITEBYTECODE': '1'}
        if backend:
            env['FEEDLING_API_URL'] = url + '/'
        if enclave:
            env['FEEDLING_ENCLAVE_URL'] = url + '/enclave/'
        if token:
            path = tmp_path / 'synthetic-token'
            path.write_text('fixture-token' if auth == 'valid' else 'bad-token')
            env['FEEDLING_RUNTIME_TOKEN_FILE'] = str(path)
        elif auth != 'missing':
            env['FEEDLING_API_KEY'] = 'fixture-key' if auth == 'valid' else 'bad-key'
        proc = subprocess.run([sys.executable, str(ROOT / 'tools/io_cli.py'), 'doctor'],
                              env=env, capture_output=True, text=True, timeout=15)
        assert not proc.stderr, proc.stderr
        return proc.returncode, json.loads(proc.stdout)

    yield state, seen, run
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def assert_schema(out):
    assert out['schema'] == 'io_cli.doctor.v2'
    assert list(out['checks']) == KEYS
    assert all(type(value) is bool for value in out['checks'].values())
    assert out['ok'] == (not out['failed'])
    assert out['failed'] == [key for key in out['required_checks'] if not out['checks'][key]]
    assert not (set(out['required_checks']) & set(out['skipped_checks']))


@pytest.mark.parametrize('token', [False, True])
@pytest.mark.parametrize('enclave', [False, True])
def test_off_ready_without_enclave_content_calls(cli, token, enclave):
    state, seen, run = cli
    state['statuses']['/enclave/v1/identity/get'] = 500
    code, out = run(token=token, enclave=enclave)
    assert code == 0 and out['ok']
    assert_schema(out)
    assert out['account_mode'] == 'off'
    assert out['checks']['enclave'] is False
    assert out['required_checks'] == ['api', 'identity', 'memory', 'chat']
    assert out['skipped_checks'] == {'enclave': 'not_required_for_plaintext'}
    assert [(m, p) for m, p, _, _ in seen] == [
        ('GET', '/v1/users/whoami'), ('GET', '/v1/identity/get'),
        ('POST', '/v1/memory/index'), ('GET', '/v1/chat/history?limit=1')]
    assert json.loads(seen[2][3]) == {'limit': 1}


@pytest.mark.parametrize('mode', ['on', '', ' ', 'legacy', 'OFF', ' off ', 'ON', 'missing'])
def test_successful_legacy_modes_keep_required_enclave(cli, mode):
    state, seen, run = cli
    if mode == 'missing':
        del state['account']['content_encryption_effective']
    else:
        state['account']['content_encryption_effective'] = mode
    code, out = run()
    assert code == 0 and out['checks']['enclave'] is True
    assert_schema(out)
    assert out['account_mode'] == ('on' if mode == 'on' else 'unknown')
    assert out['required_checks'] == KEYS and out['skipped_checks'] == {}
    assert sum('/enclave/' in p for _, p, _, _ in seen) == 1


@pytest.mark.parametrize('mode', ['on', 'legacy'])
@pytest.mark.parametrize('enclave', [False, True])
def test_legacy_enclave_failure_is_not_skipped(cli, mode, enclave):
    state, seen, run = cli
    state['account']['content_encryption_effective'] = mode
    state['statuses']['/enclave/v1/identity/get'] = 503
    code, out = run(enclave=enclave)
    assert code == 1 and out['failed'] == ['enclave']
    assert out['skipped_checks'] == {}
    assert_schema(out)


@pytest.mark.parametrize('mode', [None, True, False, 0, [], {}])
def test_invalid_mode_stops_all_content_probes(cli, mode):
    state, seen, run = cli
    state['account']['content_encryption_effective'] = mode
    code, out = run()
    assert code == 1 and out['error'] == 'account_mode_unavailable'
    assert out['account_mode'] == 'unavailable'
    assert_schema(out)
    assert len(seen) == 1 and seen[0][1] == '/v1/users/whoami'


@pytest.mark.parametrize('account', [None, [], 'bad', {}, {'user_id': ''},
                                    {'user_id': '  '}, {'user_id': 3}, {'user_id': False}])
def test_invalid_identity_is_not_trusted_mode(cli, account):
    state, seen, run = cli
    state['account'] = account
    code, out = run()
    assert code == 1 and out['failed'] == ['api']
    assert_schema(out)
    assert len(seen) == 1


@pytest.mark.parametrize('status', [201, 401, 403, 404, 500, 503])
def test_mode_requires_exact_200_and_stops_on_http_failure(cli, status):
    state, seen, run = cli
    state['mode_status'] = status
    code, out = run()
    assert code == 1 and out['error'] == 'account_mode_unavailable'
    assert_schema(out)
    assert len(seen) == 1


def test_malformed_mode_json_fails_before_content(cli):
    state, seen, run = cli
    state['raw_mode'] = b'{broken'
    code, out = run()
    assert code == 1 and out['error'] == 'account_mode_unavailable'
    assert len(seen) == 1


@pytest.mark.parametrize('key,path', [('identity', '/v1/identity/get'),
                                    ('memory', '/v1/memory/index'),
                                    ('chat', '/v1/chat/history?limit=1')])
@pytest.mark.parametrize('status', [401, 500, 503])
def test_off_backend_failure_is_required_failure(cli, key, path, status):
    state, seen, run = cli
    state['statuses'][path] = status
    code, out = run()
    assert code == 1 and out['failed'] == [key]
    assert_schema(out)
    assert not any('/enclave/' in p for _, p, _, _ in seen)


@pytest.mark.parametrize('token', [False, True])
def test_invalid_credentials_never_probe_content(cli, token):
    state, seen, run = cli
    code, out = run(token=token, auth='invalid')
    assert code == 1 and out['failed'] == ['api']
    assert len(seen) == 1


@pytest.mark.parametrize('missing', ['backend', 'auth'])
def test_missing_configuration_does_not_probe_enclave(cli, missing):
    state, seen, run = cli
    code, out = run(backend=missing != 'backend', auth='missing' if missing == 'auth' else 'valid')
    assert code == 1 and out['error'] == 'account_mode_unavailable'
    assert_schema(out)
    assert seen == []


@pytest.mark.parametrize('failure_path', ['/v1/users/whoami', '/v1/identity/get',
                                         '/v1/memory/index', '/v1/chat/history?limit=1'])
def test_transport_exception_is_structured_failure(monkeypatch, capsys, failure_path):
    monkeypatch.setenv('FEEDLING_API_URL', 'http://fixture.invalid')
    monkeypatch.setenv('FEEDLING_ENCLAVE_URL', 'http://enclave.invalid')
    monkeypatch.setattr(io_cli, '_auth_headers', lambda: {'X-API-Key': 'synthetic'})
    seen = []

    def http(method, url, auth, **kwargs):
        path = url.removeprefix('http://fixture.invalid')
        seen.append(path)
        if path == failure_path:
            raise TimeoutError('must-not-leak-private-detail')
        return 200, {'user_id': 'fixture', 'content_encryption_effective': 'off'}

    monkeypatch.setattr(io_cli, '_http_json', http)
    with pytest.raises(SystemExit) as exc:
        io_cli.cmd_doctor(None)
    raw = capsys.readouterr().out
    assert exc.value.code == 1 and 'must-not-leak' not in raw
    assert_schema(json.loads(raw))
    assert not any('enclave' in p for p in seen)
    if failure_path.endswith('whoami'):
        assert len(seen) == 1


def test_each_invocation_reads_fresh_mode_not_previous_success(monkeypatch, capsys):
    monkeypatch.setenv('FEEDLING_API_URL', 'http://fixture.invalid')
    monkeypatch.setattr(io_cli, '_auth_headers', lambda: {'X-API-Key': 'synthetic'})
    calls = []
    mode = {'valid': True}

    def http(method, url, auth, **kwargs):
        calls.append(url)
        if url.endswith('whoami') and not mode['valid']:
            return 401, {'error': 'unauthorized'}
        return 200, {'user_id': 'fixture', 'content_encryption_effective': 'off'}

    monkeypatch.setattr(io_cli, '_http_json', http)
    with pytest.raises(SystemExit) as first:
        io_cli.cmd_doctor(None)
    assert first.value.code == 0
    capsys.readouterr()
    calls.clear()
    mode['valid'] = False
    with pytest.raises(SystemExit) as second:
        io_cli.cmd_doctor(None)
    assert second.value.code == 1
    assert json.loads(capsys.readouterr().out)['account_mode'] == 'unavailable'
    assert len(calls) == 1 and calls[0].endswith('whoami')
