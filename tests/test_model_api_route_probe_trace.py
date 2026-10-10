"""Actual route endpoints, persisted safe observation and no extra provider calls."""
import json
from types import SimpleNamespace
import httpx
import pytest
import db
import debug_trace
import provider_client
from hosted import setup_core
from test_model_api_probe_trace import probe_user, _events


@pytest.mark.parametrize('operation', ['activate', 'test'])
@pytest.mark.parametrize('failure', [True, False])
def test_route_probe_safe_correlation_and_single_call(client, probe_user, monkeypatch, operation, failure):
    uid, headers = probe_user
    monkeypatch.setattr(provider_client, 'test_provider_key', lambda cfg: {})
    initial = client.post('/v1/model_api/setup', headers=headers, json={
        'provider':'openai_compatible', 'model':'existing', 'api_key':'synthetic-key',
        'base_url':'http://127.0.0.1:19999/v1', 'context_window_tokens':65536})
    assert initial.status_code == 200
    old = db.model_api_active_route(uid)['id']
    cid = db.model_api_credentials_list(uid)[0]['id']
    sentinel = 'PRIVATE_MODEL_SENTINEL'
    created = client.post('/v1/model_api/routes', headers=headers, json={
        'credential_id':str(cid), 'model':sentinel, 'context_window_tokens':65536, 'activate':False})
    rid = created.get_json()['route']['id']
    _events(client, headers)
    debug_trace.clear_trace(SimpleNamespace(user_id=uid))
    calls = []
    def probe(config):
        calls.append(config)
        if failure:
            exc=provider_client.ProviderError('PRIVATE_ERROR_SENTINEL', response_detail='PRIVATE_BODY_SENTINEL')
            exc.feedling_error_class='PRIVATE_CLASS_SENTINEL'
            raise exc from httpx.ReadTimeout('PRIVATE_URL_SENTINEL')
        return {'raw_id':'PRIVATE_RESPONSE_ID_SENTINEL', 'usage':{'total_tokens':2}}
    monkeypatch.setattr(provider_client, 'test_provider_key', probe)
    response = client.post(f'/v1/model_api/routes/{rid}/{operation}', headers=headers)
    assert response.status_code == (400 if failure else 200)
    assert len(calls)==1 and calls[0].model==sentinel
    events = _events(client, headers)
    assert len(events)==2
    assert {e['detail']['phase'] for e in events}=={'started','finished'}
    assert len({e['trace_id'] for e in events})==1
    for e in events:
        d=e['detail']
        assert d['route_id']==rid and d['request_id']==response.headers['X-Request-Id']
        assert d['request_id'] in e['trace_id'] and rid in e['trace_id']
        assert d['operation']==('route_activate' if operation=='activate' else 'route_test')
        assert d['http_phase']=='unknown' and d['model'].startswith('sha256:')
        assert d['status_code'] is None
        if d['phase']=='finished':
            assert e['dur_ms']>=0
            assert d['exception_type']==('ReadTimeout' if failure else 'unknown')
    # Read the always-on ledger as persisted, not just a callback spy.
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT doc FROM user_logs WHERE user_id=%s AND stream='provider_attempts'",(uid,))
            ledger=[row[0] for row in cur.fetchall()]
    route_rows=[v for v in ledger if v.get('lane')=='route_'+operation]
    assert len(route_rows)==1 and route_rows[0]['parent_message_id']==events[0]['trace_id']
    assert route_rows[0]['dur_ms']>=0
    encoded=json.dumps(events)+json.dumps(route_rows)
    for secret in ['PRIVATE_MODEL_SENTINEL','PRIVATE_ERROR_SENTINEL','PRIVATE_BODY_SENTINEL','PRIVATE_CLASS_SENTINEL','PRIVATE_URL_SENTINEL','PRIVATE_RESPONSE_ID_SENTINEL','sk-test']:
        assert secret not in encoded
    if failure:
        assert str(db.model_api_active_route(uid)['id'])==str(old)
        assert db.model_api_route_get(uid,rid)['test_status']=='failed'


@pytest.mark.parametrize('failure',[True,False])
def test_observer_failure_does_not_change_provider_result_or_retry(monkeypatch, failure):
    calls=[]
    error=provider_client.ProviderError('synthetic')
    result={'usage':{}}
    def probe(config):
        calls.append(config)
        if failure:raise error
        return result
    def observer(*a,**kw):raise RuntimeError('PRIVATE_OBSERVER_SECRET')
    monkeypatch.setattr(provider_client,'test_provider_key',probe)
    monkeypatch.setattr(setup_core,'_emit_model_api_probe_trace',observer)
    monkeypatch.setattr(setup_core.provider_attempt_ledger,'record_runtime_attempt',observer)
    context=setup_core._route_probe_context('00000000-0000-0000-0000-000000000001')
    fn=lambda:setup_core._test_provider_key_observed(SimpleNamespace(user_id='synthetic'),provider_client.ProviderConfig('openai_compatible','model','synthetic-key'),operation='route_test',route_context=context)
    if failure:
        with pytest.raises(provider_client.ProviderError) as got:fn()
        assert got.value is error
    else:assert fn() is result
    assert len(calls)==1


@pytest.mark.parametrize("failure", [True, False])
def test_credential_rotation_observes_one_probe_without_leaking_or_losing_old_key(
        client, probe_user, monkeypatch, failure):
    uid, headers = probe_user
    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {})
    model = "PRIVATE_ROTATION_MODEL"
    initial = client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "openai_compatible", "model": model,
        "api_key": "synthetic-old-key", "base_url": "http://127.0.0.1:19999/v1",
        "context_window_tokens": 65536})
    assert initial.status_code == 200
    active = db.model_api_active_route(uid)
    cid = active["credential_id"]
    before = db.model_api_credential_get(uid, cid)
    monkeypatch.setattr(setup_core.core_envelope, "_build_shared_envelope_for_store",
                        lambda *a, **k: ({"v": 1, "body_ct": "new-ct", "nonce": "new-n"}, None))
    _events(client, headers)
    debug_trace.clear_trace(SimpleNamespace(user_id=uid))
    calls = []

    def probe(config):
        calls.append(config)
        if failure:
            raise provider_client.ProviderError(
                "PRIVATE_ROTATION_ERROR", response_detail="PRIVATE_ROTATION_BODY"
            ) from httpx.ReadTimeout("PRIVATE_ROTATION_URL")
        return {"raw_id": "PRIVATE_ROTATION_RESPONSE", "usage": {"total_tokens": 2}}

    monkeypatch.setattr(provider_client, "test_provider_key", probe)
    response = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers,
                            json={"api_key": "PRIVATE_ROTATION_KEY"})
    assert response.status_code == (400 if failure else 200)
    assert len(calls) == 1
    assert calls[0].api_key == "PRIVATE_ROTATION_KEY" and calls[0].model == model
    after = db.model_api_credential_get(uid, cid)
    if failure:
        assert after["api_key_envelope"] == before["api_key_envelope"]
        assert after["api_key_hint"] == before["api_key_hint"]
    else:
        assert after["api_key_envelope"] != before["api_key_envelope"]
    assert db.model_api_active_route(uid)["id"] == active["id"]
    assert db.model_api_active_route(uid)["test_status"] == "ok"
    events = _events(client, headers)
    assert len(events) == 2
    assert {e["detail"]["phase"] for e in events} == {"started", "finished"}
    assert len({e["trace_id"] for e in events}) == 1
    for event in events:
        detail = event["detail"]
        assert detail["operation"] == "credential_patch"
        assert detail["route_id"] == active["id"]
        assert detail["request_id"] == response.headers["X-Request-Id"]
        assert detail["http_phase"] == "unknown"
        assert detail["model"].startswith("sha256:")
    with db.get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT doc FROM user_logs WHERE user_id=%s AND stream='provider_attempts'", (uid,))
            rows = [row[0] for row in cur.fetchall() if row[0].get("lane") == "credential_patch"]
    assert len(rows) == 1
    assert rows[0]["parent_message_id"] == events[0]["trace_id"]
    encoded = json.dumps(events) + json.dumps(rows)
    assert "PRIVATE_ROTATION_" not in encoded


@pytest.mark.parametrize("kind", ["label_only", "inactive_key"])
def test_credential_patch_without_active_key_change_does_not_probe(
        client, probe_user, monkeypatch, kind):
    uid, headers = probe_user
    monkeypatch.setattr(provider_client, "test_provider_key", lambda cfg: {})
    initial = client.post("/v1/model_api/setup", headers=headers, json={
        "provider": "openai_compatible", "model": "existing",
        "api_key": "synthetic-old-key", "base_url": "http://127.0.0.1:19999/v1",
        "context_window_tokens": 65536})
    assert initial.status_code == 200
    active = db.model_api_active_route(uid)
    cid = active["credential_id"]
    payload = {"label": "Renamed"}
    if kind == "inactive_key":
        created = client.post("/v1/model_api/routes", headers=headers, json={
            "provider": "openai_compatible", "model": "inactive",
            "api_key": "synthetic-inactive-key", "base_url": "http://127.0.0.1:19998/v1",
            "context_window_tokens": 65536, "activate": False})
        assert created.status_code == 200
        cid = created.get_json()["route"]["credential_id"]
        assert cid != active["credential_id"]
        payload = {"api_key": "synthetic-replacement-key"}
    _events(client, headers)
    debug_trace.clear_trace(SimpleNamespace(user_id=uid))

    def unexpected_probe(*args, **kwargs):
        pytest.fail("patch without active key change must not probe")

    monkeypatch.setattr(setup_core, "_test_provider_key_observed", unexpected_probe)
    monkeypatch.setattr(provider_client, "test_provider_key", unexpected_probe)
    response = client.patch(f"/v1/model_api/credentials/{cid}", headers=headers, json=payload)
    assert response.status_code == 200
    assert db.model_api_active_route(uid)["id"] == active["id"]
    assert not _events(client, headers)
