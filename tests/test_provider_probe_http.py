"""Real default-transport loopback controls: no provider or external network."""
import concurrent.futures
from contextlib import contextmanager
import json
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'backend'))
import provider_client as pc
import provider_attempt_ledger as ledger


@contextmanager
def loopback(*modes):
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen()
    listener.settimeout(.1)
    stop = threading.Event()
    state = SimpleNamespace(count=0, methods=[], errors=[], port=listener.getsockname()[1])

    def serve():
        while not stop.is_set() and state.count < len(modes):
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with conn:
                conn.settimeout(2)
                try:
                    data = b''
                    while b'\r\n\r\n' not in data:
                        data += conn.recv(65536)
                    head, body = data.split(b'\r\n\r\n', 1)
                    method = head.split(b' ', 1)[0]
                    size = next((int(x.split(b':', 1)[1]) for x in head.split(b'\r\n')
                                 if x.lower().startswith(b'content-length:')), 0)
                    while len(body) < size:
                        body += conn.recv(65536)
                    mode = modes[state.count]
                    state.count += 1
                    state.methods.append(method)
                    if mode == 'headers':
                        stop.wait(2)
                    elif mode == 'body':
                        conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 9999\r\nX-Private: HEADER_SENTINEL\r\n\r\nBODY_SENTINEL')
                        stop.wait(2)
                    elif mode == 'connect':
                        assert method == b'CONNECT'
                        conn.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                        stop.wait(2)
                    else:
                        status = b'200 OK'
                        if mode == 'fallback':
                            status = b'400 Bad Request'
                            payload = b'{"error":{"message":"unsupported parameter: prompt_cache_key"}}'
                        elif mode == '404':
                            status = b'404 Not Found'
                            payload = b'{"error":{"message":"BODY_SENTINEL"}}'
                        elif mode == 'json':
                            payload = b'BODY_SENTINEL'
                        else:
                            payload = b'{"choices":[{"message":{"content":"ok"}}],"usage":{"total_tokens":2}}'
                        conn.sendall(b'HTTP/1.1 '+status+b'\r\nConnection: close\r\nContent-Length: '+str(len(payload)).encode()+b'\r\n\r\n'+payload)
                except Exception as exc:
                    state.errors.append(type(exc).__name__)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        yield state
    finally:
        stop.set()
        listener.close()
        thread.join(4)
        assert not thread.is_alive()
        assert not state.errors


@pytest.fixture
def local_probe(monkeypatch):
    with httpx.Client(trust_env=False) as client:
        monkeypatch.setattr(pc, '_http_client', lambda: client)
        # Fixture-only timeout injection: production 90 second constant is unmodified.
        assert pc.SETUP_PROBE_TIMEOUT_S == 90
        monkeypatch.setattr(pc, 'SETUP_PROBE_TIMEOUT_S', .15)
        yield


def config(server, **kw):
    return pc.ProviderConfig('openai_compatible', 'synthetic', 'KEY_SENTINEL',
                             f'http://127.0.0.1:{server.port}/URL_SENTINEL', **kw)


@pytest.mark.parametrize('mode,phase,seen,status', [
    ('headers', 'response_headers', False, None),
    ('body', 'response_body', True, 200),
    ('json', 'buffered_complete', True, 200),
    ('404', 'buffered_complete', True, 404),
    ('ok', 'buffered_complete', True, 200),
])
def test_real_buffered_phase(local_probe, mode, phase, seen, status, capsys):
    with loopback(mode) as server, pc.observe_setup_probe('route_activate') as obs:
        if mode == 'ok':
            assert pc.test_provider_key(config(server))['reply'] == 'ok'
        else:
            with pytest.raises(pc.ProviderError) as caught:
                pc.test_provider_key(config(server))
            if mode in {'headers', 'body'}:
                assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
                assert caught.value.status_code is None
        summary = obs.snapshot()
        assert summary == dict(version=1, phase=phase, headers_seen=seen,
                               observed_status_code=status, observation_valid=True,
                               post_attempt_count=1)
        assert ledger.project_probe_http(summary) == summary
        assert server.count == 1
    assert pc._SETUP_PROBE_OBSERVER.get() is None
    encoded = json.dumps(summary) + capsys.readouterr().out
    for sentinel in ('KEY_SENTINEL', 'BODY_SENTINEL', 'HEADER_SENTINEL', 'URL_SENTINEL'):
        assert sentinel not in encoded


def test_real_proxy_connect_not_target_headers(monkeypatch):
    with loopback('connect') as server:
        with httpx.Client(proxy=f'http://127.0.0.1:{server.port}', trust_env=False) as client:
            monkeypatch.setattr(pc, '_http_client', lambda: client)
            with pc.observe_setup_probe('credential_patch') as obs:
                with pytest.raises(httpx.ConnectTimeout):
                    pc._probe_json_post('https://synthetic.invalid', json={}, timeout=.15)
                assert obs.snapshot()['phase'] == 'unknown'
                assert obs.snapshot()['headers_seen'] is None
                assert obs.snapshot()['observed_status_code'] is None
            assert server.methods == [b'CONNECT']


def test_real_fallback_resets_last_post(local_probe):
    with loopback('fallback', 'headers') as server:
        with pc.observe_setup_probe('route_test') as obs:
            with pytest.raises(pc.ProviderError):
                pc.test_provider_key(pc.ProviderConfig('openai', 'gpt-4o', 'synthetic-key',
                    f'http://127.0.0.1:{server.port}/v1', prompt_cache_key='synthetic-affinity'))
            assert obs.snapshot() == dict(version=1, phase='response_headers', headers_seen=False,
                                         observed_status_code=None, observation_valid=True,
                                         post_attempt_count=2)
        assert server.count == 2


def test_real_concurrent_scopes(local_probe):
    def run(mode):
        with loopback(mode) as server, pc.observe_setup_probe('route_test') as obs:
            with pytest.raises(pc.ProviderError):
                pc.test_provider_key(config(server))
            return obs.snapshot()['phase']
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        assert list(pool.map(run, ['headers', 'body'])) == ['response_headers', 'response_body']
    with pc.observe_setup_probe('route_activate') as outer:
        with pytest.raises(RuntimeError), pc.observe_setup_probe('credential_patch'):
            raise RuntimeError('synthetic')
        assert pc._SETUP_PROBE_OBSERVER.get() is outer
    assert pc._SETUP_PROBE_OBSERVER.get() is None


@pytest.mark.parametrize('event,info', [
    ('http11.receive_response_body.started', {'request': SimpleNamespace(method=b'POST')}),
    ('http11.receive_response_headers.complete', {'return_value': (b'HTTP/1.1', 200)}),
    ('http11.receive_response_headers.started', {}),
    ('http2.receive_response_headers.started', {'request': SimpleNamespace(method=b'POST')}),
])
def test_invalid_or_unverified_sticky_through_real_success(local_probe, monkeypatch, event, info):
    original = pc._ProbeHTTPObservation._trace
    def corrupt(self, name, data):
        if name == 'http11.send_request_headers.started':
            original(self, event, info)
        original(self, name, data)
    monkeypatch.setattr(pc._ProbeHTTPObservation, '_trace', corrupt)
    with loopback('ok') as server, pc.observe_setup_probe('route_test') as obs:
        pc.test_provider_key(config(server))
        assert obs.snapshot()['phase'] == 'unknown'
        assert obs.snapshot()['observation_valid'] is False
        assert server.count == 1


def test_missing_trace_and_other_extension_preserved(monkeypatch):
    extensions = {'trace': lambda *a: None}
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={}))) as client:
        monkeypatch.setattr(pc, '_http_client', lambda: client)
        for ext in (None, extensions):
            with pc.observe_setup_probe('route_test') as obs:
                kwargs = {'extensions': ext} if ext else {}
                pc._probe_json_post('http://synthetic.invalid', json={}, **kwargs)
                assert obs.snapshot()['phase'] == 'unknown'
                assert obs.snapshot()['observation_valid'] is False
    assert len(extensions) == 1


def test_normal_traffic_scope_does_not_add_extensions(monkeypatch):
    calls = []
    monkeypatch.setattr(pc, '_http_client', lambda: SimpleNamespace(post=lambda *a, **kw: calls.append(kw)))
    for operation in ('setup', 'test', 'capture'):
        with pc.observe_setup_probe(operation):
            pc._probe_json_post('synthetic', json={}, timeout=90)
    assert calls == [{'json': {}, 'timeout': 90}] * 3


@pytest.mark.parametrize('mode', ['ok', 'headers', 'body'])
@pytest.mark.parametrize('fault', ['snapshot', 'trace', 'ledger'])
def test_observation_fault_preserves_real_probe(local_probe, monkeypatch, mode, fault):
    from hosted import setup_core
    def broken(*args, **kwargs):
        raise RuntimeError('PRIVATE_OBSERVER_SECRET')
    if fault == 'snapshot':
        monkeypatch.setattr(pc._ProbeHTTPObservation, 'snapshot', broken)
    elif fault == 'trace':
        monkeypatch.setattr(setup_core, '_emit_model_api_probe_trace', broken)
    else:
        monkeypatch.setattr(ledger, 'record_runtime_attempt', broken)
    # No DB side effects in this transport-level control.
    if fault != 'ledger':
        monkeypatch.setattr(ledger, 'record_runtime_attempt', lambda *a, **k: True)
    if fault != 'trace':
        monkeypatch.setattr(setup_core, '_emit_model_api_probe_trace', lambda *a, **k: None)
    with loopback(mode) as server:
        def run():
            return setup_core._test_provider_key_observed(SimpleNamespace(user_id='synthetic'),
                config(server), operation='route_test')
        if mode == 'ok':
            assert run()['reply'] == 'ok'
        else:
            with pytest.raises(pc.ProviderError) as caught:
                run()
            assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
        assert server.count == 1
    assert pc._SETUP_PROBE_OBSERVER.get() is None


@pytest.mark.parametrize('provider,model', [('openai', 'o3'), ('anthropic', 'claude-sonnet-4-5'),
                                         ('bedrock', 'anthropic.claude-sonnet-4'),
                                         ('gemini', 'gemini-2.5-flash')])
@pytest.mark.parametrize('mode', ['headers', 'body'])
def test_other_sync_adapters_real_phase(local_probe, provider, model, mode):
    with loopback(mode) as server, pc.observe_setup_probe('route_test') as obs:
        cfg = pc.ProviderConfig(provider, model, 'synthetic-key', f'http://127.0.0.1:{server.port}/v1')
        with pytest.raises(pc.ProviderError) as caught:
            pc.test_provider_key(cfg)
        assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
        assert obs.snapshot()['phase'] == ('response_headers' if mode == 'headers' else 'response_body')
        assert obs.snapshot()['post_attempt_count'] == server.count == 1


def test_real_forward_proxy_body(monkeypatch):
    with loopback('body') as server:
        with httpx.Client(proxy=f'http://127.0.0.1:{server.port}', trust_env=False) as client:
            monkeypatch.setattr(pc, '_http_client', lambda: client)
            with pc.observe_setup_probe('route_test') as obs:
                with pytest.raises(httpx.ReadTimeout):
                    pc._probe_json_post('http://synthetic.invalid/private', json={}, timeout=.15)
                assert obs.snapshot()['phase'] == 'response_body'
                assert server.methods == [b'POST']


@pytest.mark.parametrize('operation', ['route_test', 'ordinary_chat'])
def test_observed_unobserved_fallback_count_equivalent(local_probe, operation):
    with loopback('fallback', 'ok') as server, pc.observe_setup_probe(operation):
        result = pc.test_provider_key(pc.ProviderConfig('openai', 'gpt-4o', 'synthetic-key',
            f'http://127.0.0.1:{server.port}/v1', prompt_cache_key='synthetic-affinity'))
        assert result['reply'] == 'ok'
        assert server.count == 2


def test_failed_body_cannot_restart_into_trusted_success(local_probe, monkeypatch):
    original = pc._ProbeHTTPObservation._trace
    def corrupt(self, event, info):
        if event == 'http11.receive_response_body.started':
            original(self, event, info)
            original(self, 'http11.receive_response_body.failed',
                     {'exception': RuntimeError('PRIVATE_OBSERVER_SECRET')})
        original(self, event, info)
    monkeypatch.setattr(pc._ProbeHTTPObservation, '_trace', corrupt)
    with loopback('ok') as server, pc.observe_setup_probe('route_test') as obs:
        assert pc.test_provider_key(config(server))['reply'] == 'ok'
        assert obs.snapshot() == dict(version=1, phase='unknown', headers_seen=None,
            observed_status_code=None, observation_valid=False, post_attempt_count=1)
        assert server.count == 1
    assert pc._SETUP_PROBE_OBSERVER.get() is None


@pytest.mark.parametrize('operation', ['setup', 'test'])
@pytest.mark.parametrize('failure', [False, True])
def test_legacy_probe_callback_contract_unchanged(monkeypatch, operation, failure):
    from hosted import setup_core
    calls = []
    traces = []
    def probe(config):
        if failure:
            raise pc.ProviderError('synthetic failure')
        return {'reply': 'ok'}
    monkeypatch.setattr(pc, 'test_provider_key', probe)
    monkeypatch.setattr(ledger, 'record_runtime_attempt', lambda *a, **k: calls.append(k))
    monkeypatch.setattr(setup_core, '_emit_model_api_probe_trace', lambda *a, **k: traces.append(k))
    def run():
        return setup_core._test_provider_key_observed(SimpleNamespace(user_id='synthetic'),
            pc.ProviderConfig('openai_compatible', 'synthetic', 'synthetic'), operation=operation)
    if failure:
        with pytest.raises(pc.ProviderError):
            run()
    else:
        assert run()['reply'] == 'ok'
    assert len(calls) == 1 and len(traces) == 2
    assert calls[0]['dur_ms'] >= 0
    assert all('probe_http' not in row for row in calls + traces)
    assert pc._SETUP_PROBE_OBSERVER.get() is None
