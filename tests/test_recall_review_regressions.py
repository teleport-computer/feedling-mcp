"""Regression probes adapted from claude3 independent T820 R1 review."""
import asyncio
import hashlib
import json
import pytest
from test_chat_resident_consumer import crc, _proactive_job_fixture, _reset_proactive_guard_state_between_tests
from memory import recall_contract, recall_attachments
from model_api_runtime.v2 import worker


@pytest.fixture(autouse=True)
def clear_seen_keys():
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()
    yield
    crc._seen_ids.clear()
    crc._seen_ids_order.clear()


def test_proactive_real_history_failure_is_terminal(monkeypatch):
    statuses=[]
    monkeypatch.setattr(crc, '_recall_account', lambda: ('u', 'off'))
    monkeypatch.setattr(crc, 'claim_proactive_job', lambda _: True)
    monkeypatch.setattr(crc, 'update_proactive_job_status', lambda job,status,reason='',**kw: statuses.append(status))
    monkeypatch.setattr(crc, '_screen_context_for_frame_ids', lambda _: ('', [], []))
    # Keep real recent_chat_context_for_proactive and projection inside the scope.
    monkeypatch.setattr(crc, 'get_decrypted_history', lambda **kw: [dict(id='m',seq=1,role='user',ts=1000,owner_user_id='foreign',body='hello',content='hello')])
    monkeypatch.setattr(crc, 'call_agent', lambda *a,**kw: pytest.fail('provider called'))
    crc._process_proactive_jobs([_proactive_job_fixture()])
    assert statuses[-1] == 'failed'


def test_v2_rejects_changed_canonical_attachment_between_two_proofs():
    uid='u'; mid='m'; raw=b'validated-first-version'
    source=dict(id=mid,seq=1,owner_user_id=uid,content_type='image',image_mime='image/png')
    proof=recall_attachments.AttachmentProof(uid,mid,1,json.dumps(source),raw)
    row=dict(id=mid,seq=1,content='',has_image=True,_recall_source_json=json.dumps(source),
             _recall_row_proof=recall_contract.TurnRowProof(uid,mid,1,''))
    # Server's second canonical read has observed a different version of the same id/seq.
    response=dict(account_mode='off',user_id=uid,seq=1,message_id=mid,
                  required_attachments=[dict(id=mid,seq=1,sha256=hashlib.sha256(b'changed-second-version').hexdigest())],
                  context_memories=[],context_memory_log={})
    deps=worker.TurnDeps(read_messages=lambda _:[],resolve_provider=lambda _:None,mint_enclave_token=lambda _:'synthetic',
                        prepare_recall_attachments=lambda *a,**kw:{mid:proof},read_context_memories=lambda *a,**kw:response)
    token=recall_contract.bind_operation_mode(uid,'off')
    try:
        with pytest.raises(recall_contract.RecallFailure):
            asyncio.run(worker._load_turn_memory_context(deps,uid,1,asyncio.Semaphore(1),required_rows=[row],required_ids=[mid]))
    finally:
        recall_contract.reset_operation_mode(token)


@pytest.mark.parametrize('change', ['body_b64', 'caption_body', 'image_mime', None])
def test_v2_actual_sql_attachment_version_change_is_rejected(monkeypatch, change):
    import base64
    import conftest
    import db
    import time
    from psycopg.types.json import Jsonb
    from model_api_runtime.v2 import serve_worker
    uid='reviewer_t820_attachment_race'
    conftest.seed_user(uid)
    doc=dict(id='m',owner_user_id=uid,role='user',source='user',ts=1000.0,content_type='image',image_mime='image/png',body_b64=base64.b64encode(b'first').decode())
    with db.get_pool().connection() as conn:
        seq=conn.execute('INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES(%s,%s,%s,%s) RETURNING seq',(uid,'m',1000,Jsonb(doc))).fetchone()[0]
    source={**doc,'seq':seq}
    row=dict(id='m',seq=seq,content='',has_image=True,_recall_source_json=json.dumps(source),_recall_row_proof=recall_contract.TurnRowProof(uid,'m',seq,''))
    monkeypatch.setattr(serve_worker.accounts_registry,'effective_content_encryption',lambda _: 'off')
    monkeypatch.setenv('FEEDLING_V2_PLAINTEXT_RECALL','off')
    observed=[]
    reads=[]
    real_read = recall_attachments.read
    def attachment_read(*args, **kwargs):
        reads.append(args[0]['id'])
        return real_read(*args, **kwargs)
    monkeypatch.setattr(recall_attachments, 'read', attachment_read)
    def selection(*args,**kwargs):
        if change is not None:
            values = {'body_b64':base64.b64encode(b'second').decode(), 'caption_body':'changed caption', 'image_mime':'image/webp'}
            changed={**doc,change:values[change]}
            with db.get_pool().connection() as conn:
                conn.execute('UPDATE chat_messages SET doc=%s WHERE user_id=%s AND msg_id=%s',(Jsonb(changed),uid,'m'))
        result=serve_worker._read_context_memories(*args,**kwargs)
        observed.append(result)
        return result
    deps=worker.TurnDeps(read_messages=lambda _:[],resolve_provider=lambda _:None,mint_enclave_token=lambda _:'synthetic',prepare_recall_attachments=serve_worker._prepare_recall_attachments,read_context_memories=selection)
    token=recall_contract.bind_operation_mode(uid,'off')
    try:
        def load():
            return asyncio.run(worker._load_turn_memory_context(deps,uid,seq,asyncio.Semaphore(1),required_rows=[row],required_ids=['m']))
        if change is not None:
            with pytest.raises(recall_contract.RecallFailure, match='attachment_changed'):
                load()
        else:
            result = load()
            assert row['_recall_attachment_proof'].body == b'first'
            assert result['required_attachments'][0]['sha256'] == hashlib.sha256(b'first').hexdigest()
            assert result['category'] == 'recall_unavailable'
        assert reads == ['m']  # Recall reuses the first proof; no second body read.
    finally:
        recall_contract.reset_operation_mode(token)


def test_proactive_malformed_body_through_real_history_reader_is_terminal(monkeypatch):
    import types
    statuses=[]
    monkeypatch.setattr(crc,'_recall_account',lambda:('u','off'))
    monkeypatch.setattr(crc,'claim_proactive_job',lambda _:True)
    monkeypatch.setattr(crc,'update_proactive_job_status',lambda job,status,reason='',**kw:statuses.append(status))
    monkeypatch.setattr(crc,'_screen_context_for_frame_ids',lambda _:('',[],[]))
    monkeypatch.setattr(crc,'call_agent',lambda *a,**kw:pytest.fail('provider called'))
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'messages':[dict(id='m',seq=1,ts=1000,role='user',owner_user_id='u',body=123,content='')]}
    monkeypatch.setattr(crc,'_HTTP',types.SimpleNamespace(get=lambda *a,**kw:Response()))
    crc._process_proactive_jobs([_proactive_job_fixture()])
    assert statuses[-1]=='failed'


def test_proactive_real_history_failure_continues_to_next_valid_job(monkeypatch):
    import types
    statuses, calls, replies, history_reads = [], [], [], []
    monkeypatch.setattr(crc, '_recall_account', lambda: ('u', 'off'))
    monkeypatch.setattr(crc, '_plan_wake_coalescing', lambda jobs: None)
    monkeypatch.setattr(crc, '_worldbook_context_for_wake', lambda job: '')
    monkeypatch.setattr(crc, 'claim_proactive_job', lambda _: True)
    monkeypatch.setattr(crc, 'update_proactive_job_status', lambda job,status,reason='',**kw: statuses.append((job,status)))
    monkeypatch.setattr(crc, '_screen_context_for_frame_ids', lambda _: ('', [], []))
    monkeypatch.setattr(crc, 'call_agent', lambda *a,**kw: calls.append(a) or ['valid reply'])
    monkeypatch.setattr(crc, 'post_reply', lambda *a,**kw: replies.append(a) or {'id':'reply'})
    monkeypatch.setattr(crc, '_emit_debug_trace', lambda *a,**kw: None)
    class Response:
        def __init__(self, payload, status=200): self.payload, self.status_code = payload, status
        def raise_for_status(self): pass
        def json(self): return self.payload
    def get(url, **kwargs):
        if not url.endswith('/v1/chat/history'):
            return Response({})
        history_reads.append(1)
        body = 123 if len(history_reads) == 1 else 'valid history'
        return Response({'messages':[dict(id='m',seq=1,v=1,ts=1000,role='user',owner_user_id='u',body=body)]})
    def post(url, **kwargs):
        if not url.endswith('/v1/memory/turn-selection'):
            return Response({})
        request = kwargs['json']
        return Response(dict(error='recall_unavailable', category='recall_unavailable',detail='local_disabled',
                             user_id='u',seq=request['seq'],message_id=request['message_id'],required_references=[]),503)
    monkeypatch.setattr(crc, '_HTTP', types.SimpleNamespace(get=get,post=post))
    bad = {**_proactive_job_fixture(), 'job_id':'bad-history'}
    good = {**_proactive_job_fixture(), 'job_id':'good-history', 'ts':2000}
    crc._process_proactive_jobs([bad, good])
    assert ('bad-history','failed') in statuses
    assert ('bad-history','realizing') not in statuses
    assert ('good-history','realizing') in statuses
    # Two required-context reads, then the valid job's ordinary post-time
    # chat-collision check. The failed job never reaches delivery.
    assert len(history_reads) == 3 and len(calls) == 1 and len(replies) == 1


def test_fixed_page_cannot_mix_an_older_prepared_attachment_version():
    from memory import recall_projection
    original = dict(id='older-file',seq=1,v=1,ts=1,role='user',owner_user_id='u',
                    content_type='file',body='first file',caption_body='first caption')
    changed = {**original,'caption_body':'second caption'}
    current = dict(id='current',seq=2,v=1,ts=2,role='user',owner_user_id='u',body='question')
    with pytest.raises(recall_contract.RecallFailure, match='attachment_changed'):
        recall_projection.turn([changed,current],'u',2, source_snapshots={'older-file':original})
    result = recall_projection.turn([original,current],'u',2, source_snapshots={'older-file':original})
    assert result.message_id == 'current'
