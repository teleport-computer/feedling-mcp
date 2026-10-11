"""Actual uploaded job -> worker/reducers -> output API -> primary artifacts.

Only provider and backend HTTP transport are substituted; eligibility, claim,
ledger, garden actions, profile, and output API execute against PostgreSQL.
"""
import json
import uuid
import pytest
import db
import provider_client
from core import store as stores
from genesis import genesis_core, worker, managed_artifacts, managed_eligibility
from test_genesis_managed_admission import account, upload


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status
        self.text = json.dumps(body, default=str)
    def json(self):
        return self.body
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('backend_output_rejected')


@pytest.fixture
def pipeline(account, monkeypatch):
    uid, key = account
    store = stores.get_store(uid)
    job = upload(uid)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE genesis_import_jobs SET source_kind='user_profile' WHERE user_id=%s", (uid,))
    calls = []
    def provider(runtime, messages, **kwargs):
        assert db.genesis_get_job(uid,'j')['metadata']['managed_worker_execution']['phase']=='execution_started'
        assert runtime.api_key == key['body']
        prompt = '\n'.join(str(m.get('content') or '') for m in messages)
        calls.append(prompt)
        if '{"candidates": []}' in prompt:
            reply = {'candidates':[{'about':'person','summary':'喜欢直接反馈','evidence':'喜欢直接反馈','occurred_at':None}]}
        elif 'The source describes itself as:' in prompt or '"cards"' in prompt:
            reply = {'cards':[{'action':'add','type':'fact','bucket':'偏好','threads':[],
                              'summary':'喜欢直接反馈','content':'对方喜欢直接反馈，是材料中明确表达的偏好。',
                              'importance':0.6,'pulse':0.3}]}
        else:
            reply = {'memory':'对方偏好直接、明确的反馈。','style':''}
        return {'reply':json.dumps(reply,ensure_ascii=False),'usage':{'input_tokens':1,'output_tokens':1},'stop_reason':'stop'}
    monkeypatch.setattr(provider_client,'reliable_chat_completion',provider)
    def get(url, **kwargs):
        assert url.endswith('/v1/model_api/key_envelope')
        return Response({'api_key_envelope':key})
    monkeypatch.setattr(managed_eligibility.httpx,'get',get)
    def post(url, *, json, **kwargs):
        assert url.endswith('/v1/genesis/imports/j/outputs'), url
        body,status=genesis_core.apply_outputs(store,'j',json,api_key=None,runtime_token='test')
        return Response(body,status)
    monkeypatch.setattr(worker.httpx,'post',post)
    wid='pipeline-'+uuid.uuid4().hex
    def tick():
        return worker.tick(api_url='http://backend',enclave_url='',worker_id=wid,
                           mint_runtime_token=lambda *args,**kwargs:'test')
    return store,calls,tick


def test_uploaded_managed_pipeline_writes_real_memory_profile_and_ledgers(pipeline):
    store,calls,tick=pipeline
    result=tick()
    assert result['processed']==1, result
    assert len(calls)>=3
    assert len(db.memory_load_strict(store.user_id))==1
    profile=db.get_blob_strict(store.user_id,managed_artifacts.service.v2_profile_store.PROFILE_BLOB_KIND)
    assert profile['memory']['envelope']['body']=='对方偏好直接、明确的反馈。'
    with db.get_pool().connection() as conn:
        receipts=conn.execute('SELECT output_type,doc FROM genesis_import_outputs WHERE user_id=%s',(store.user_id,)).fetchall()
    assert {'reducer','apply'}.issubset({r[0] for r in receipts})
    assert sum(bool(r[1].get('usage')) for r in receipts)>=3
    count=len(calls)
    assert tick()['claimed']==0
    assert len(calls)==count


def test_partial_memory_commit_survives_profile_failure_without_replay(pipeline,monkeypatch):
    store,calls,tick=pipeline
    def fail(*args):
        raise RuntimeError('injected_profile_failure')
    monkeypatch.setattr(managed_artifacts,'write_profile',fail)
    result=tick()
    assert result['failed']==1, result
    assert len(db.memory_load_strict(store.user_id))==1
    assert db.genesis_get_job(store.user_id,'j')['status']=='failed'
    assert db.get_blob_strict(store.user_id,'genesis_state')['status']=='failed'
    with db.get_pool().connection() as conn:
        outcomes=dict(conn.execute(
            "SELECT artifact,outcome FROM distillation_artifact_attempts WHERE user_id=%s",
            (store.user_id,)).fetchall())
    assert outcomes['memory']=='written'
    assert outcomes['profile']=='write_failed'
    count=len(calls)
    assert tick()['claimed']==0 and len(calls)==count
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT count(*) FROM genesis_import_outputs WHERE user_id=%s AND doc ? 'usage'",(store.user_id,)).fetchone()[0]>=3


def test_managed_backfill_failure_is_not_lazily_reenqueued(account,monkeypatch):
    from genesis import persona_backfill, managed_execution
    import genesis_managed_store as persistence
    uid,key=account
    store=stores.get_store(uid)
    material={'agent_name':'Companion','custom_persona_prompt':'Respond patiently.'}
    job=persona_backfill.run_persona_backfill(store,material)
    jid=job['job_id']
    claimed,prepared,witness=persistence.prepare_claim(uid,jid,'backfill-worker',key,managed_eligibility.prepare)
    assert 'Respond patiently.' in prepared.texts[0]
    managed_execution.begin_execution(store,jid,witness)
    persistence.fail(uid,jid,witness,'possible_effects')
    monkeypatch.setattr(db,'genesis_list_jobs',lambda *a,**kw: [])  # outside the recent-job page
    retained=persona_backfill.run_persona_backfill(store,material)
    assert retained['job_id']==jid and retained['status']=='failed'
    with db.get_pool().connection() as conn:
        assert conn.execute('SELECT count(*) FROM genesis_import_jobs WHERE user_id=%s',(uid,)).fetchone()[0]==1


def test_sealed_dependency_tick_has_no_provider_or_artifact_attempt(pipeline):
    store,calls,tick=pipeline
    db.set_blob_strict(store.user_id,'identity',{'body_ct':'sealed','owner_user_id':store.user_id})
    assert tick()['claimed']==0 and calls==[]
    job=db.genesis_get_job(store.user_id,'j')
    assert job['status']=='uploaded'
    assert job['output']['managed_unavailable']['dependency']=='identity'
    assert db.memory_load_strict(store.user_id)==[]
    with db.get_pool().connection() as conn:
        assert conn.execute('SELECT count(*) FROM distillation_artifact_attempts WHERE user_id=%s',(store.user_id,)).fetchone()[0]==0


def test_persona_backfill_api_to_worker_to_artifacts(account,monkeypatch):
    from core import envelope
    uid,key=account
    store=stores.get_store(uid)
    identity={'agent_name':'Companion','custom_persona_prompt':'Respond patiently.'}
    doc,error=envelope._build_shared_envelope_for_store(store,json.dumps(identity).encode())
    assert not error
    db.set_blob_strict(uid,'identity',doc)
    response,status=genesis_core.persona_backfill(store,api_key=None,runtime_token='test')
    assert status==202, response
    jid=response['job_id']
    calls=[]
    replies=[json.dumps({'identity':{'agent_name':'Companion','dimensions':[]},'memories':[]}),
             'Respond patiently and make careful observations.',
             json.dumps({'memory':'','style':'Patient, careful and concise.'})]
    def provider(runtime,messages,**kwargs):
        with db.get_pool().connection() as conn:
            conn.execute('SELECT 1 FROM genesis_import_jobs WHERE user_id=%s AND job_id=%s FOR UPDATE NOWAIT',(uid,jid))
        calls.append(messages)
        return {'reply':replies[len(calls)-1],'usage':{'input_tokens':1,'output_tokens':1},'stop_reason':'stop'}
    monkeypatch.setattr(provider_client,'reliable_chat_completion',provider)
    monkeypatch.setattr(managed_eligibility.httpx,'get',lambda *a,**kw:Response({'api_key_envelope':key}))
    def post(url, *, json, **kwargs):
        assert url.endswith(f'/v1/genesis/imports/{jid}/outputs')
        body,status=genesis_core.apply_outputs(store,jid,json,api_key=None,runtime_token='test')
        return Response(body,status)
    monkeypatch.setattr(worker.httpx,'post',post)
    result=worker.tick(api_url='http://backend',enclave_url='',worker_id='backfill-'+uuid.uuid4().hex,
                       mint_runtime_token=lambda *a,**kw:'test')
    assert result['processed']==1,result
    assert len(calls)==3
    assert db.get_blob_strict(uid,'genesis_persona')['content_envelope']['body']==replies[1]
    assert db.get_blob_strict(uid,'v2_agent_profile')['style']['envelope']['body']=='Patient, careful and concise.'


def test_lost_completed_output_reply_reads_done_without_replaying(pipeline,monkeypatch):
    store,calls,tick=pipeline
    post=worker.httpx.post
    def lost_reply(*args,**kwargs):
        response=post(*args,**kwargs)
        assert response.status_code==200
        raise worker.httpx.ReadTimeout('injected reply loss')
    monkeypatch.setattr(worker.httpx,'post',lost_reply)
    result=tick()
    assert result['processed']==1,result
    assert db.genesis_get_job(store.user_id,'j')['status']=='done'
    assert len(db.memory_load_strict(store.user_id))==1
