"""Real CLI/urllib entry against a loopback backend; no enclave/provider required."""
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
sys.path.insert(0, str(ROOT / 'backend'))
from core.chat_images import encode_image_bundle

PIXELS = b'\x89PNG\r\n\x1a\nfixture'


def run_cli(tmp_path, *, row=None, mode='off', history_status=200, full=None,
            body_status=200, whoami_status=200, enclave=False, runtime_token=False):
    requests = []
    image_row = {'id': 'msg_one', 'role': 'user', 'content_type': 'image',
                 'body_b64': base64.b64encode(PIXELS).decode(), 'image_mime': 'image/png'}
    if row is not None:
        image_row = row

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append((self.path, self.headers.get('X-API-Key'),
                             self.headers.get('X-Feedling-Runtime-Token')))
            if self.path == '/v1/users/whoami':
                status, payload = whoami_status, {'content_encryption_effective': mode}
            elif self.path.startswith('/v1/chat/history?'):
                status, payload = history_status, {'messages': [image_row]}
            elif self.path == '/v1/chat/messages/msg_one/body':
                status, payload = body_status, {'message': full}
            elif self.path.startswith('/enclave/v1/chat/history?'):
                status, payload = 200, {'messages': [{'id': 'msg_one', 'image_b64':
                    base64.b64encode(PIXELS).decode(), 'image_mime': 'image/png'}]}
            else:
                status, payload = 500, {'error': 'unexpected_request'}
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    # Explicit fixture environment: never inherit real auth, trace or ledger paths.
    env = {'PATH': os.environ['PATH'], 'PYTHONDONTWRITEBYTECODE': '1',
           'FEEDLING_API_URL': url, 'IMAGE_TEMP_DIR': str(tmp_path / 'images'),
           'NO_PROXY': '127.0.0.1'}
    if runtime_token:
        token = tmp_path / 'runtime-token'
        token.write_text('fixture-runtime-token')
        env['FEEDLING_RUNTIME_TOKEN_FILE'] = str(token)
    else:
        env['FEEDLING_API_KEY'] = 'fixture-api-key'
    if enclave:
        env['FEEDLING_ENCLAVE_URL'] = url + '/enclave'
    try:
        result = subprocess.run([sys.executable, str(ROOT / 'tools/io_cli.py'),
            'chat-image', '--id', 'msg_one'], env=env, capture_output=True,
            text=True, timeout=10)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert not result.stderr, result.stderr
    return result.returncode, json.loads(result.stdout), requests


@pytest.mark.parametrize('runtime_token', [False, True])
def test_plaintext_image_without_enclave(tmp_path, runtime_token):
    code, body, requests = run_cli(tmp_path, runtime_token=runtime_token)
    assert code == 0 and body['ok'], body
    path = Path(body['image_file'])
    assert path.read_bytes() == PIXELS
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert len(requests) == 2
    assert all('/enclave/' not in path for path, _, _ in requests)
    assert all((key, token) == ((None, 'fixture-runtime-token') if runtime_token
                              else ('fixture-api-key', None)) for _, key, token in requests)


def test_plaintext_bundle(tmp_path):
    bundle = encode_image_bundle([(PIXELS, 'image/png'), (b'jpeg', 'image/jpeg')])
    code, body, requests = run_cli(tmp_path, row={'id': 'msg_one', 'content_type': 'image',
        'image_bundle_version': 1, 'body_b64': base64.b64encode(bundle).decode()})
    assert code == 0 and body['image_count'] == 2, body
    assert [Path(item['image_file']).read_bytes() for item in body['images']] == [PIXELS, b'jpeg']


def test_omitted_body(tmp_path):
    code, body, requests = run_cli(tmp_path,
        row={'id': 'msg_one', 'content_type': 'image', 'body_omitted': True,
             'body_size_bytes': len(PIXELS), 'owner_user_id': 'fixture-user'},
        full={'id': 'msg_one', 'owner_user_id': 'fixture-user', 'content_type': 'image',
              'body_b64': base64.b64encode(PIXELS).decode()})
    assert code == 0, body
    assert Path(body['image_file']).read_bytes() == PIXELS
    assert len(requests) == 3


@pytest.mark.parametrize('row', [
    {'body_ct': 'sealed', 'content': 'must not expose'},
    {'body_ct': 'sealed', 'body_b64': base64.b64encode(PIXELS).decode()},
    {'content_type': 'image', 'body_b64': 'not base64!'},
    {'content_type': 'image', 'body_b64': ''},
    {'content_type': 'image', 'image_bundle_version': 1, 'body_b64': 'YWJj'},
    {'content_type': 'image', 'image_bundle_version': 2, 'body_b64': 'YWJj'},
    {'content_type': 'image', 'content': 'caption only'},
    {'content': 'no body'},
])
def test_unreadable_rows_fail_without_enclave(tmp_path, row):
    code, body, requests = run_cli(tmp_path, row={'id': 'msg_one', **row}, enclave=True)
    assert code == 1 and body['ok'] is False, body
    assert all('/enclave/' not in path for path, _, _ in requests)
    assert not (tmp_path / 'images').exists()


@pytest.mark.parametrize('status,full', [
    (503, None), (200, None), (200, {'id': 'other', 'body_b64': 'YWJj'}),
    (200, {'id': 'msg_one', 'owner_user_id': 'other', 'body_b64': 'YWJj'}),
    (200, {'id': 'msg_one', 'body_ct': 'sealed'}),
])
def test_omitted_body_failure_does_not_fallback(tmp_path, status, full):
    code, body, requests = run_cli(tmp_path,
        row={'id': 'msg_one', 'owner_user_id': 'fixture-user', 'content_type': 'image',
             'body_omitted': True, 'body_size_bytes': 3},
        full=full, body_status=status, enclave=True)
    assert code == 1 and not body['ok'], body
    assert all('/enclave/' not in path for path, _, _ in requests)


def test_backend_failure_does_not_fallback(tmp_path):
    code, body, requests = run_cli(tmp_path, history_status=503, enclave=True)
    assert code == 1 and body['http_status'] == 503
    assert all('/enclave/' not in path for path, _, _ in requests)


@pytest.mark.parametrize('mode,status', [('on', 200), (None, 200), ('off', 503)])
def test_encrypted_or_unknown_preserves_enclave(tmp_path, mode, status):
    code, body, requests = run_cli(tmp_path, mode=mode, whoami_status=status, enclave=True)
    assert code == 0, body
    assert any('/enclave/' in path for path, _, _ in requests)
    assert not any(path.startswith('/v1/chat/history') for path, _, _ in requests)


def test_plaintext_text_only(tmp_path):
    code, body, _ = run_cli(tmp_path, row={'id': 'msg_one', 'body': 'hello', 'role': 'user'})
    assert code == 0 and body['content'] == 'hello', body


def test_image_write_failure_is_not_success(tmp_path):
    (tmp_path / 'images').write_text('not a directory')
    code, body, requests = run_cli(tmp_path, enclave=True)
    assert code == 1 and not body['ok'], body
    assert 'image_b64' not in body
    assert all('/enclave/' not in path for path, _, _ in requests)


def test_null_ciphertext_field_is_plaintext(tmp_path):
    code, body, _ = run_cli(tmp_path, row={'id': 'msg_one', 'content_type': 'image',
        'body_ct': None, 'body_b64': base64.b64encode(PIXELS).decode()})
    assert code == 0, body
    assert Path(body['image_file']).read_bytes() == PIXELS


@pytest.mark.parametrize('omitted', [False, True])
@pytest.mark.parametrize('caption,expected', [
    ({'caption_body': 'readable'}, 'readable'),
    ({'caption_owner_user_id': 'fixture-user', 'caption_body': 'same-owner'}, 'same-owner'),
    ({'caption_body_ct': 'sealed', 'caption_body': 'stale'}, ''),
    ({'caption_owner_user_id': 'other-user', 'caption_body': 'foreign'}, ''),
    ({'caption_body': None}, ''),
])
def test_caption_envelope_boundary(tmp_path, omitted, caption, expected):
    row = {'id': 'msg_one', 'owner_user_id': 'fixture-user', 'content_type': 'image',
           'body_b64': base64.b64encode(PIXELS).decode(), **caption}
    if omitted:
        code, body, requests = run_cli(tmp_path, enclave=True, full=row,
            row={'id': 'msg_one', 'owner_user_id': 'fixture-user',
                 'content_type': 'image', 'body_omitted': True, 'body_size_bytes': len(PIXELS)})
    else:
        code, body, requests = run_cli(tmp_path, enclave=True, row=row)
    assert code == 0 and body['content'] == expected, body
    assert Path(body['image_file']).read_bytes() == PIXELS
    assert all('/enclave/' not in path for path, _, _ in requests)
