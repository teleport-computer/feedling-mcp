"""Real operation identity and closed trace boundary; no account-reader bypass."""
import json

import httpx
import pytest

from test_chat_resident_consumer import crc


@pytest.mark.parametrize('payload,expected', [
    ({'user_id':'u','content_encryption_effective':'off'}, ('u','off')),
    ({'user_id':'u','content_encryption_effective':'on'}, ('u','on')),
    ({'user_id':'u','content_encryption_effective':'unknown'}, ('u','unknown')),
    ({'user_id':'u'}, ('u','unknown')),
])
def test_real_operation_identity_uses_authenticated_response_not_cache(monkeypatch, payload, expected):
    seen=[]
    def handle(request):
        seen.append(request)
        return httpx.Response(200,json=payload)
    monkeypatch.setattr(crc,'_whoami_cache',{'user_id':'wrong','content_encryption_effective':'off'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(crc,'_HTTP',client)
        assert crc._recall_account()==expected
    assert len(seen)==1 and seen[0].url.path=='/v1/users/whoami'
    for key,value in crc._HEADERS.items():
        assert seen[0].headers[key]==value


@pytest.mark.parametrize('failure',['401','503','transport','json','missing_uid','mode_type'])
def test_real_operation_identity_never_falls_back_to_cached_account(monkeypatch,failure):
    def handle(request):
        if failure=='transport':raise httpx.ReadTimeout('private-dsn-secret',request=request)
        if failure=='json':return httpx.Response(200,content=b'private-invalid-json')
        if failure in {'401','503'}:return httpx.Response(int(failure))
        return httpx.Response(200,json={} if failure=='missing_uid' else {'user_id':'u','content_encryption_effective':False})
    monkeypatch.setattr(crc,'_whoami_cache',{'user_id':'cached-valid','content_encryption_effective':'off'})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(crc,'_HTTP',client)
        with pytest.raises(crc._RecallInputError) as error:crc._recall_account()
    assert str(error.value) in {'account_unconfirmed','account_mode_invalid'}


def test_foreground_recall_failure_never_emits_exception_text(monkeypatch):
    events=[]
    monkeypatch.setattr(crc,'_seen_ids',set())
    monkeypatch.setattr(crc,'_seen_ids_order',[])
    monkeypatch.setattr(crc,'_reset_proactive_idle_guard',lambda:None)
    monkeypatch.setattr(crc,'_clear_proactive_failure',lambda:None)
    monkeypatch.setattr(crc,'_resident_chat_runtime_v2_enabled',lambda:False)
    monkeypatch.setattr(crc,'_emit_debug_trace',lambda *a,**kw:events.append((a,kw)))
    def fail(*a,**kw):raise crc._RecallInputError('private-dsn-secret')
    monkeypatch.setattr(crc,'_auto_memory_block_for',fail)
    monkeypatch.setattr(crc,'call_agent',lambda *a,**kw:pytest.fail('provider called'))
    monkeypatch.setattr(crc,'post_reply',lambda *a,**kw:pytest.fail('reply posted'))
    assert crc._process_messages([{'id':'privacy-negative','seq':1,'ts':1,'role':'user','source':'chat','content':'hello'}])==0
    failure=[kw for args,kw in events if args[1]=='context.recall_failed']
    assert len(failure)==1
    assert failure[0]['detail']=={'reason':'required_content_unavailable'}
    assert 'private-dsn-secret' not in json.dumps(events)
    assert 'privacy-negative' not in crc._seen_ids


@pytest.mark.parametrize('caption', ['safe independent caption', ''])
def test_missing_sealed_attachment_reaches_provider_only_as_plain_caption(monkeypatch,caption):
    from chat import service as chat_service
    from test_backlog_collapse import _neutralize_pipeline
    calls,replies=[],[]
    _neutralize_pipeline(monkeypatch,calls,replies)
    monkeypatch.setattr(crc,'_seen_ids',set())
    monkeypatch.setattr(crc,'_seen_ids_order',[])
    monkeypatch.setattr(crc,'_whoami_cache',{'user_id':'u','content_encryption_effective':'off'})
    monkeypatch.setattr(crc,'_foreground_history_injection_enabled',lambda:False)
    monkeypatch.setattr(chat_service.db.object_storage,'chat_files_enabled',lambda:False)
    source={'id':'sealed-projection','seq':1,'ts':1,'role':'user','source':'chat',
            'owner_user_id':'u','content_type':'image','body_key':'u/private-object',
            'body_ct_len':100,'K_enclave':'private-sealed-key','nonce':'private-nonce',
            'vision_route_id':'private-vision-route','caption_body':caption}
    projected=chat_service._chat_history_item(source,include_image_body=True)
    assert 'body_ct' not in projected and projected['K_enclave']=='private-sealed-key'
    requests=[]
    def handle(request):
        requests.append(request.url.path)
        if request.url.path=='/v1/users/whoami':
            return httpx.Response(200,json={'user_id':'u','content_encryption_effective':'off'})
        if request.url.path=='/v1/chat/history':
            return httpx.Response(200,json={'messages':[projected]})
        if request.url.path=='/v1/memory/turn-selection':
            body=json.loads(request.content)
            return httpx.Response(503,json={'category':'recall_unavailable','detail':'local_disabled',
                'user_id':'u','seq':body['seq'],'message_id':body['message_id'],'required_references':[]})
        pytest.fail('unexpected network/media request: '+request.url.path)
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(crc,'_HTTP',client)
        rows=crc.get_decrypted_history(0)
        latest=crc._process_messages(rows)
    assert latest==(1 if caption else 0)
    assert len(calls)==len(replies)==int(bool(caption))
    if caption:
        assert caption in calls[0]
        assert all(secret not in json.dumps(calls) for secret in ['private-sealed-key','private-nonce','private-object','private-vision-route'])
    else:
        assert 'sealed-projection' not in crc._seen_ids
        assert '/v1/memory/turn-selection' not in requests
