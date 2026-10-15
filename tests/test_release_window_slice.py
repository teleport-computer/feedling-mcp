"""Offline tests for the fixed, bounded OpenAI V2 slice; never provider calls."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from tools.release_window import bounded_proxy as proxy, bounded_v2 as v2, contract, receipts


def manifest():
    return dict(api_url='https://test-api.feedling.app', lane='openai-official-v2-text',
        drain_scope=dict(parent_generation='synthetic-parent',resident_owners=['synthetic-resident'],exclusive_test_runner=True,config_action='delete-model-api',worker_ids={p:'synthetic:'+p for p in ('foreground','wake','heavy')},pool_slots={p:1 for p in ('foreground','wake','heavy')}),
        model='gpt-5.4-mini', run_id='synthetic-never-run', expected_release='a'*40,
        relay_origin='https://synthetic-relay.invalid', relay_port=8789, claim_path='/synthetic/run.claim',
        receipt_sha256={k:'f'*64 for k in contract.RECEIPTS},
        funded_budget_receipt='/synthetic/funds', model_availability_receipt='/synthetic/model',
        execution_approval_receipt='/synthetic/approval', billing_contract_receipt='/synthetic/billing',
        execution_environment_receipt='/synthetic/environment', input_count_receipt='/synthetic/count',
        input_count=dict(enabled=True,max_requests=4,timeout_seconds=5,usd_per_request_cap='0'),
        billing=dict(model='gpt-5.4-mini',currency='USD',input_bound_kind='responses_input_tokens',
                     enforced_input_tokens_max=8192,output_cap_includes_reasoning=True,exclusive_funding=True,
                     account_scope='synthetic-only',uncached_input_usd_per_million='1',
                     billable_output_usd_per_million='1',extra_usd_per_start='0',
                     max_cost_usd='1',funded_remaining_usd='1',task_cap_usd='1'),
        limits=dict(starts=4, request_bytes=8192,input_tokens_per_start=8192,total_input_tokens=32768,
        total_bytes=32768, output_tokens_per_start=300, total_output_tokens=1200,
        wall_seconds=60, drain_seconds=2, cleanup_seconds=10, poll_seconds=1, read_requests=64,
        phase_starts=dict(setup=1,dialogue_1=1,dialogue_2=1,background=1),
        allocations={p:dict(primary=1,retry=0,background=0) for p in contract.PHASES}))


def body():
    return json.dumps({'model':'gpt-5.4-mini','input':[{'role':'user','content':[{'type':'input_text','text':'synthetic'}]}],
                       'max_output_tokens':500, 'store':False}).encode()


def make_run(tmp_path, handler=None, reviewed_manifest=None, count_handler=None, **kwargs):
    calls=[]
    reviewed_manifest = reviewed_manifest or manifest()
    bind_synthetic_count(reviewed_manifest, tmp_path)
    def dispatch(req):
        if req.url == proxy.COUNT_ENDPOINT:
            if count_handler: return count_handler(req)
            return httpx.Response(200,json={"object":"response.input_tokens","input_tokens":3})
        calls.append(req)
        if handler:
            return handler(req)
        return httpx.Response(200, json={'usage':{'input_tokens':3,'output_tokens':2,'total_tokens':5},'output':[]})
    run=proxy.Run(token='synthetic'*8,upstream_key='not-a-real-key',journal=tmp_path/'proxy.jsonl',
                  manifest=reviewed_manifest,transport=httpx.MockTransport(dispatch),**kwargs)
    return run,calls


@pytest.mark.parametrize('field,value', [('model','gpt-4.1-mini'),('relay_port',True),('lane','vps'),('expected_release','main')])
def test_manifest_rejects_unbound_contract(field,value):
    m=manifest();m[field]=value
    with pytest.raises(ValueError):contract.validate_manifest(m)


def test_allocation_must_cover_declared_actual_starts():
    m=manifest();m['limits']['allocations']['setup']['retry']=1
    with pytest.raises(ValueError,match='allocation_sum'):contract.validate_manifest(m)


def test_real_backend_gpt5_builder_matches_responses_protocol():
    from provider_client import _build_openai_responses_payload
    payload,url,_=_build_openai_responses_payload(model='gpt-5.4-mini',base_url='https://synthetic-relay.invalid/v1',
        key='synthetic',messages=[{'role':'user','content':'hello'}],max_tokens=500,response_format=None)
    assert url.endswith('/v1/responses')
    encoded=json.loads(proxy.payload(json.dumps(payload).encode(),manifest()['limits']|{'model':'gpt-5.4-mini'}))
    assert encoded['max_output_tokens']==300 and encoded['store'] is False


def test_phase_and_total_journal_are_real_caps(tmp_path):
    run,calls=make_run(tmp_path)
    for phase in contract.PHASES:
        if phase!='setup':run.set_phase(phase)
        assert run.forward(body())[0]==200
    with pytest.raises(proxy.Refused):run.forward(body())
    run.close()
    assert len(calls)==4
    assert all(r.url==proxy.UPSTREAM for r in calls)
    assert all(json.loads(r.content)['max_output_tokens']==300 for r in calls)
    receipt=proxy.verify_closed_admission(tmp_path/'proxy.jsonl')
    assert receipt['send_reservations']==4 and receipt['manifest_digest']==contract.digest(run.manifest)
    text=(tmp_path/'proxy.jsonl').read_text()
    assert 'not-a-real-key' not in text and '"text"' not in text
    with pytest.raises(FileExistsError):make_run(tmp_path)


def test_parallel_requests_cannot_exceed_phase_cap(tmp_path):
    run,calls=make_run(tmp_path)
    def send(_):
        try:return run.forward(body())[0]
        except proxy.Refused:return 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes=list(pool.map(send,range(4)))
    run.close()
    assert outcomes.count(200)==1 and len(calls)==1 and run.count_requests==1


def test_error_stops_retries_and_deadline_stops_before_dispatch(tmp_path):
    run,calls=make_run(tmp_path,handler=lambda r:httpx.Response(503,json={}))
    assert run.forward(body())[0]==503
    with pytest.raises(proxy.Refused):run.forward(body())
    assert len(calls)==1
    run.close()
    other=tmp_path/'other';other.mkdir();clock=[0]
    run,calls=make_run(other,clock=lambda:clock[0]);clock[0]=61
    with pytest.raises(proxy.Refused):run.forward(body())
    assert not calls
    run.close()


def test_drain_refuses_when_actual_exchange_lock_held(tmp_path):
    run,_=make_run(tmp_path)
    run.limits['drain_seconds']=0.01
    run.lock.acquire()
    try:
        with pytest.raises(proxy.Refused,match='drain_unconfirmed'):run.close()
        assert run.closing.is_set()
    finally:run.lock.release();run.close()


def test_readonly_producer_queries_only_owned_uid(tmp_path):
    seen=[]
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def transaction(self):return self
        def execute(self,sql,params=None):
            seen.append((sql,params))
            if 'current_database' in sql:r=('feedling','feedling_owner','on',True)
            elif sql==receipts.SQL:r=(False,0,0,0,False,0,datetime.now(timezone.utc))
            elif 'LEFT JOIN user_blobs' in sql:r=None
            elif 'model_api_routes' in sql:r=(0,False)
            else:r=None
            return SimpleNamespace(fetchone=lambda:r)
    owner=SimpleNamespace(verify=lambda:None,client=SimpleNamespace(user_id='synthetic-owned'),binding=lambda:{'run_id':'r'})
    value=receipts.observe(owner,connector=Conn)
    assert value['member'] is False and value['job_leases']==0
    assert seen[0][0]=='SET TRANSACTION READ ONLY'
    assert seen[2][1]==('synthetic-owned',)*6
    assert seen[3][1]==('synthetic-owned',)
    assert not any('content' in q or 'body' in q for q,_ in seen)


def test_owner_requires_actual_registration_and_originals(tmp_path,monkeypatch):
    from tools.e2e import client
    monkeypatch.setattr(client,'_ORPHANS_DIR',tmp_path/'orphans')
    c=SimpleNamespace(user_id='synthetic-owned',api_key='synthetic-key',api_url=manifest()['api_url'])
    c._orphan_file=client._write_orphan_manifest(c.api_url,c.user_id,c.api_key)
    c._registration_receipt={'http':201,'user_id':c.user_id,'response_digest':'f'*64}
    out=tmp_path/'out';out.mkdir()
    owner=receipts.Owner(c,manifest(),out)
    owner.verify()
    assert 'synthetic-key' not in (out/'creation.json').read_text()
    c._orphan_file.write_text('{}')
    with pytest.raises(RuntimeError,match='original mismatch'):owner.verify()


@pytest.mark.parametrize('bad', ['membership','lease','old_key'])
def test_cleanup_real_failure_keeps_owner_marker(tmp_path,monkeypatch,bad):
    run,_=make_run(tmp_path)
    events=[]
    class Client:
        def post(self,*a,**kw):events.append('reset');return httpx.Response(200)
        def get(self,*a,**kw):events.append('oldkey');return httpx.Response(200 if bad=='old_key' else 401)
    receipts._write_private_json(tmp_path/'scenario-result.json',{'status':'INCOMPLETE'})
    owner=SimpleNamespace(out=tmp_path,manifest=run.manifest,client=Client(),binding=lambda:{},verify=lambda:events.append('verify'),
        save=lambda name,data:events.append((name,data)))
    empty=dict(member=True,pending=0,claimed=0,running=0,resident_lease_active=False,job_leases=0)
    rows=[empty,empty|{'member':bad=='membership','job_leases':1 if bad=='lease' else 0}]
    monkeypatch.setattr(v2,'snapshot',lambda *a,**kw:rows.pop(0))
    monkeypatch.setattr(v2,'runtime_drain',lambda *a,**kw:rows.pop(0))
    with pytest.raises(RuntimeError,match='cleanup not proven'):v2.cleanup(owner,run,v2.Reads(10),admin=object())
    assert events.index('reset')<events.index('oldkey')
    assert events[-1][0]=='cleanup' and events[-1][1]['status']=='FAILED'


def test_cleanup_evidence_failure_stops_before_reset(tmp_path,monkeypatch):
    run,_=make_run(tmp_path)
    def fail(*a,**kw):raise OSError('private disk unavailable')
    receipts._write_private_json(tmp_path/'scenario-result.json',{'status':'INCOMPLETE'})
    owner=SimpleNamespace(out=tmp_path,manifest=run.manifest,binding=lambda:{},client=SimpleNamespace(post=lambda *a,**k:pytest.fail('reset')),verify=lambda:None,save=fail)
    monkeypatch.setattr(v2,'snapshot',lambda *a,**kw:dict(pending=0,claimed=0,running=0,resident_lease_active=False,job_leases=0))
    monkeypatch.setattr(v2,'runtime_drain',lambda *a,**kw:{})
    with pytest.raises(OSError):v2.cleanup(owner,run,v2.Reads(10),admin=object())


def test_synthetic_complete_exercise_and_cleanup(tmp_path,monkeypatch):
    run,calls=make_run(tmp_path)
    saved={}
    state={'mid':None,'marker':None,'number':0}
    class Client:
        content_encryption_effective='off'
        def post(self,path,*,json=None,**kw):
            if path.endswith('/setup'):
                run.forward(body())
                return httpx.Response(200,json={'test_status':'ok'})
            if path.endswith('/reset'):
                assert run.closing.is_set()
                state['deleted']=True
                return httpx.Response(200)
            if json['client_msg_id']!=state.get('client_msg_id'):
                state['client_msg_id']=json['client_msg_id'];state['number']+=1
                state['mid']='synthetic-message-'+str(state['number'])
                if state['number']==1:state['marker']=json['message'].split('code ')[1].split('.')[0]
                run.forward(body())
            return httpx.Response(202,json={'user_message':{'id':state['mid'],'ts':100}})
        def get(self,path,**kw):
            if path.endswith('/whoami'):return httpx.Response(401)
            if '/turn-activity/' in path:return httpx.Response(200,json={'complete':True,'failure':None})
            return httpx.Response(200,json={'messages':[
                {'id':state['mid'],'role':'user'},
                {'id':'reply','role':'assistant','reply_to_message_id':state['mid'],'content':state['marker']} ]})
        def read_reply_strict(self,row):return row['content']
        def classify_reply(self,*args):return 'ok',''
    def save(name,data):
        saved[name]=data
        receipts._write_private_json(tmp_path/(name+'.json'),data)
    owner=SimpleNamespace(client=Client(),out=tmp_path,manifest=run.manifest,binding=lambda:{},verify=lambda:None,save=save)
    def observe(*a,**kw):
        return dict(member=not state.get('deleted',False),pending=0,claimed=0,running=0,
                    resident_lease_active=False,job_leases=0,runtime_mode='db_action_v2',runtime_state='v2')
    monkeypatch.setattr(v2,'snapshot',observe)
    monkeypatch.setattr(v2,'runtime_drain',observe)
    # httpx fake responses need a request for raise_for_status; locally attach one.
    original=httpx.Response.raise_for_status
    monkeypatch.setattr(httpx.Response,'raise_for_status',lambda self:self if self.status_code<400 else original(self))
    result=v2.exercise(owner,run,v2.Reads(20),sleep=lambda _:None)
    save('scenario-result',result)
    assert v2.cleanup(owner,run,v2.Reads(20),admin=object(),sleep=lambda _:None)=='CONFIRMED'
    assert len(calls)==3 and saved['cleanup']['status']=='CONFIRMED'
    assert all(saved[f'reply-{i}']['assertions']['readable_known_answer'] for i in (1,2))
    assert not any(state['marker'] in p.read_text() for p in tmp_path.glob('*.json'))


def test_missing_safe_scenario_stops_before_reset(tmp_path):
    run,_=make_run(tmp_path)
    owner=SimpleNamespace(out=tmp_path,manifest=run.manifest,verify=lambda:None)
    with pytest.raises(RuntimeError,match='scenario evidence missing'):
        v2.cleanup(owner,run,v2.Reads(10),admin=object())
    run.close()


def executor_fixture(tmp_path,monkeypatch,*,exercise=None,configure=None,response=None):
    import hashlib
    from tools.e2e import client as client_mod
    m=manifest();m['claim_path']=str(tmp_path/'run.claim')
    if configure:configure(m)
    for name in contract.RECEIPTS:
        path=tmp_path/(name+'.txt');path.write_text('synthetic reviewed fixture')
        m[name]=str(path);m['receipt_sha256'][name]=hashlib.sha256(path.read_bytes()).hexdigest()
    bind_synthetic_billing(m)
    monkeypatch.setenv('FEEDLING_ADMIN_KEY','synthetic-admin')
    monkeypatch.setenv('E2E_KEY_OPENAI','synthetic-provider')
    monkeypatch.setattr(client_mod,'_ORPHANS_DIR',tmp_path/'orphans')
    state={"calls":[]}
    original_client=httpx.Client
    original_run=proxy.Run
    def make(**kw):
        def dispatch(req):
            if req.url == proxy.COUNT_ENDPOINT:
                return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':3})
            state['calls'].append(req)
            if response:return response(req,len(state['calls']))
            return httpx.Response(200,json={'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}})
        kw['transport']=httpx.MockTransport(dispatch)
        r=original_run(**kw);state['run']=r;return r
    monkeypatch.setattr(proxy,'Run',make)
    class HTTP:
        def __init__(self,*a,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def close(self):pass
        def post(self,url,**kw):
            if url.endswith('/v1/responses'):
                r=state['run'];r.probe_received=True
                return httpx.Response(200,json={'nonce':r.probe_nonce,'ack':r.probe_ack})
            return httpx.Response(200,request=httpx.Request('POST','https://synthetic.invalid'))
    # Keep the relay's real HTTP client with MockTransport; only executor HTTP is fake.
    monkeypatch.setattr(v2,'httpx',SimpleNamespace(Client=HTTP))
    class Server:
        def __init__(self,*a):pass
        def serve_forever(self):pass
        def shutdown(self):pass
        def server_close(self):pass
    monkeypatch.setattr(proxy,'ThreadingHTTPServer',Server)
    monkeypatch.setattr(v2,'candidate',lambda *a:'synthetic-worker')
    monkeypatch.setattr(receipts,'resident_guard',lambda *a,**kw:{'fresh':True})
    monkeypatch.setattr(receipts,'preflight',lambda **kw:{'readonly':True,'tls':True,'schema_checked':True,'model_calls':0})
    def provision(**kw):
        assert kw['defer_failure_cleanup'] is True
        c=SimpleNamespace(user_id='synthetic-owned',api_url=m['api_url'],api_key='synthetic-key',_http=HTTP())
        c._orphan_file=client_mod._write_orphan_manifest(c.api_url,c.user_id,c.api_key)
        c._registration_receipt={'http':201,'user_id':c.user_id,'response_digest':'f'*64}
        c.post=lambda *a,**kw:httpx.Response(200)
        c.get=lambda *a,**kw:httpx.Response(401)
        return c
    monkeypatch.setattr(v2.E2EClient,'provision',provision)
    monkeypatch.setattr(v2,'exercise',exercise or (lambda *a,**kw:{'status':'PASS_SHORT_VISIBLE_ONLY','assertions':{'two_dialogues':True}}))
    rows=[dict(member=True,pending=0,claimed=0,running=0,resident_lease_active=False,job_leases=0),
          dict(member=False,pending=0,claimed=0,running=0,resident_lease_active=False,job_leases=0)]
    monkeypatch.setattr(v2,'snapshot',lambda *a,**kw:rows.pop(0))
    monkeypatch.setattr(v2,'runtime_drain',lambda *a,**kw:rows.pop(0))
    result=v2.execute(m,tmp_path/'run')
    assert result['cleanup']=='CONFIRMED'
    assert json.loads((tmp_path/'run'/'result.json').read_text())==result
    assert (tmp_path/'orphans'/'synthetic-owned.json').exists()  # evidence not erased
    with pytest.raises(FileExistsError):v2.execute(m,tmp_path/'other-output')

    return result,state


def test_executor_binds_claim_and_persists_cleanup_outcome(tmp_path,monkeypatch):
    result,_=executor_fixture(tmp_path,monkeypatch)
    assert result['status']=='PASS_SHORT_VISIBLE_ONLY'


def test_mutated_approval_receipt_stops_before_key_loading(tmp_path):
    m=manifest();m['claim_path']=str(tmp_path/'claim')
    for name in contract.RECEIPTS:
        p=tmp_path/name;p.write_text('drift');m[name]=str(p)
    with pytest.raises(RuntimeError,match='receipt changed'):
        v2.execute(m,tmp_path/'out')
    assert not (tmp_path/'claim').exists()


def test_db_preflight_refuses_wrong_identity_before_schema_reads():
    calls=[]
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def transaction(self):return self
        def execute(self,sql):
            calls.append(sql)
            return SimpleNamespace(fetchone=lambda:('wrong-db','feedling_owner','on',True))
    with pytest.raises(RuntimeError,match='identity'):
        receipts.preflight(connector=Conn)
    assert len(calls)==2 and not any('FROM users' in s for s in calls)


def test_standalone_relay_is_plan_only(tmp_path,monkeypatch,capsys):
    import sys
    path=tmp_path/'manifest.json';path.write_text(json.dumps(manifest()))
    monkeypatch.setattr(sys,'argv',['bounded_proxy','--manifest',str(path)])
    monkeypatch.setattr(proxy,'Run',lambda **kw:pytest.fail('paid relay constructed'))
    proxy.main()
    assert 'PLAN_ONLY' in capsys.readouterr().out
    monkeypatch.setattr(sys,'argv',['bounded_proxy','--manifest',str(path),'--run'])
    with pytest.raises(SystemExit) as exc:proxy.main()
    assert exc.value.code==2


@pytest.mark.parametrize('failure', ['credentials', 'candidate_preflight', 'db_preflight'])
def test_every_consumed_preflight_failure_has_safe_outcome(tmp_path,monkeypatch,failure):
    import hashlib
    m=manifest();m['claim_path']=str(tmp_path/'consumed.claim')
    for key in contract.RECEIPTS:
        p=tmp_path/key;p.write_text('synthetic-reviewed')
        m[key]=str(p);m['receipt_sha256'][key]=hashlib.sha256(p.read_bytes()).hexdigest()
    bind_synthetic_billing(m)
    def fail(*a,**kw):raise RuntimeError('SECRET must not be persisted')
    monkeypatch.delenv('FEEDLING_ADMIN_KEY',raising=False)
    if failure!='credentials':monkeypatch.setenv('FEEDLING_ADMIN_KEY','synthetic')
    monkeypatch.setattr(v2,'candidate',fail if failure=='candidate_preflight' else lambda *a:'worker')
    monkeypatch.setattr(receipts,'preflight',fail)
    monkeypatch.setattr(proxy,'Run',lambda **kw:pytest.fail('relay reached after failed preflight'))
    result=v2.execute(m,tmp_path/'out')
    persisted=json.loads((tmp_path/'out'/'result.json').read_text())
    assert persisted==result and result['status']=='INCOMPLETE'
    assert result['error_stage']==failure and result['cleanup']=='NOT_ATTEMPTED'
    assert (tmp_path/'consumed.claim').exists()
    assert 'SECRET' not in (tmp_path/'out'/'result.json').read_text()
    with pytest.raises(FileExistsError):v2.execute(m,tmp_path/'retry')


def bind_synthetic_billing(m):
    import hashlib
    from pathlib import Path
    p=Path(m['billing_contract_receipt'])
    p.write_text(json.dumps({'run_id':m['run_id'],'billing':m['billing'],
        'funded_budget_sha256':m['receipt_sha256']['funded_budget_receipt'],
        'model_availability_sha256':m['receipt_sha256']['model_availability_receipt']}))
    m['receipt_sha256']['billing_contract_receipt']=hashlib.sha256(p.read_bytes()).hexdigest()
    import time
    p=Path(m['execution_environment_receipt'])
    evidence={}
    for name in ('deployment_inventory','process_inventory','exclusive_ownership'):
        item=p.with_name(name+'.synthetic');item.write_text('SYNTHETIC ONLY: never an execution approval')
        evidence[name]={'path':str(item),'sha256':hashlib.sha256(item.read_bytes()).hexdigest()}
    p.write_text(json.dumps({**{k:m[k] for k in ('run_id','api_url','expected_release','drain_scope')},
        'valid_from_epoch':time.time()-60,'valid_until_epoch':time.time()+3600,'evidence':evidence}))
    m['receipt_sha256']['execution_environment_receipt']=hashlib.sha256(p.read_bytes()).hexdigest()
    bind_synthetic_count(m, p.parent)


@pytest.mark.parametrize('which',['input','money'])
def test_full_input_and_money_reserved_before_upstream(tmp_path,which):
    m=manifest()
    m['limits']['phase_starts']['setup']=2
    m['limits']['phase_starts']['background']=0
    m['limits']['allocations']['setup']['primary']=2
    m['limits']['allocations']['background']['primary']=0
    if which=='input':m['limits']['total_input_tokens']=8192
    else:m['billing']['funded_remaining_usd']='0.01'
    run,calls=make_run(tmp_path,reviewed_manifest=m)
    run.forward(body())
    with pytest.raises(proxy.Refused,match='token_or_monetary_budget'):run.forward(body())
    assert len(calls)==1 and run.reserved_input_tokens==8192
    assert run.reserved_nanodollars==8492000
    run.close()
    rows=[json.loads(x) for x in (tmp_path/'proxy.jsonl').read_text().splitlines()]
    reserve=next(x for x in rows if x['event']=='send_reserved')
    assert reserve['input_tokens_reserved']==8192  # small wire did not become a token estimate
    assert reserve['billing_state']=='RESERVED_NOT_SETTLED'
    assert json.loads(calls[0].content)['tool_choice']=='none'


def test_unknown_send_keeps_full_token_and_money_reservation(tmp_path):
    def timeout(req):raise httpx.ReadTimeout('synthetic transport timeout')
    run,calls=make_run(tmp_path,handler=timeout)
    with pytest.raises(proxy.Refused):run.forward(body())
    assert run.reserved_input_tokens==8192 and run.reserved_nanodollars==8492000
    with pytest.raises(proxy.Refused):run.forward(body())
    assert len(calls)==1
    run.close()


@pytest.mark.parametrize('field,value',[('uncached_input_usd_per_million',None),
    ('funded_remaining_usd','0'),('input_bound_kind','wire_bytes'),
    ('output_cap_includes_reasoning',False),('exclusive_funding',False),
    ('enforced_input_tokens_max',32768)])
def test_unproven_billing_inputs_refuse_before_relay(tmp_path,field,value):
    m=manifest();m['billing'][field]=value
    with pytest.raises(ValueError):make_run(tmp_path,reviewed_manifest=m)
    assert not (tmp_path/'proxy.jsonl').exists()


def test_cost_rounds_up_to_nanodollar_and_uses_smallest_cap():
    b=manifest()['billing']
    b.update(uncached_input_usd_per_million='0.000000001',billable_output_usd_per_million='0.000000001')
    assert contract.cost_reservation(b,300)==1
    b.update(max_cost_usd='1',funded_remaining_usd='0.5',task_cap_usd='0.25')
    assert contract.cost_ceiling(b)==250000000

@pytest.mark.parametrize('bad',['missing','release','generation','expired','source_changed'])
def test_environment_binding_rejects_unproven_exclusive_runner(tmp_path,bad):
    import hashlib
    m=manifest()
    for key in contract.RECEIPTS:
        p=tmp_path/key;p.write_text('synthetic');m[key]=str(p)
        m['receipt_sha256'][key]=hashlib.sha256(p.read_bytes()).hexdigest()
    bind_synthetic_billing(m)
    assert receipts.verify_environment(m)['run_id']==m['run_id']
    path=Path(m['execution_environment_receipt']);data=json.loads(path.read_text())
    if bad=='missing':path.unlink()
    elif bad=='source_changed':
        Path(data['evidence']['process_inventory']['path']).write_text('changed')
    else:
        if bad=='release':data['expected_release']='b'*40
        if bad=='generation':data['drain_scope']['parent_generation']='restarted'
        if bad=='expired':data['valid_until_epoch']=0
        path.write_text(json.dumps(data))
        m['receipt_sha256']['execution_environment_receipt']=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError,match='execution environment'):
        receipts.verify_environment(m)


@pytest.mark.parametrize('kind',['starts','bytes','input','cost','payload_bytes','unknown','transport','provider'])
def test_late_paid_failure_downgrades_real_executor_and_still_cleans(tmp_path,monkeypatch,kind):
    from decimal import Decimal
    def configure(m):
        if kind!='input':m['limits']['total_input_tokens']=81920
        if kind=='bytes':
            size=len(proxy.payload(body(),m['limits']|{'model':m['model']}))
            m['limits']['request_bytes']=max(size,len(body()))
            m['limits']['total_bytes']=size*4
        if kind=='cost':
            reserve=contract.cost_reservation(m['billing'],m['limits']['output_tokens_per_start'])
            m['billing']['max_cost_usd']=str(Decimal(reserve*4)/Decimal(1000000000))
    def response(req,n):
        if n==4:
            if kind=='unknown':return httpx.Response(200,json={})
            if kind=='transport':raise httpx.ReadTimeout('synthetic timeout')
            if kind=='provider':return httpx.Response(503,json={})
        return httpx.Response(200,json={'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}})
    def exercise(owner,run,reads,**kw):
        for phase in contract.PHASES:
            if phase!='setup':run.set_phase(phase)
            try:run.forward(body())
            except proxy.Refused:
                assert phase=='background' and kind=='transport'
        if kind not in ('unknown','transport','provider'):
            with pytest.raises(proxy.Refused):
                run.forward(b'x'*9000 if kind=='payload_bytes' else body())
        return {'status':'PASS_SHORT_VISIBLE_ONLY','assertions':{'two_dialogues':True}}
    result,state=executor_fixture(tmp_path,monkeypatch,configure=configure,response=response,exercise=exercise)
    assert result['status']=='INCOMPLETE' and result['cleanup']=='CONFIRMED'
    assert result['paid_failure_reason']==state['run'].failure_reason
    assert len(state['calls'])==4 and state['run'].attempts==4
    rows=[json.loads(s) for s in (tmp_path/'run'/'proxy.jsonl').read_text().splitlines()]
    failures=[r for r in rows if r['event']=='run_failed']
    assert len(failures)==1 and failures[0]['reason']==result['paid_failure_reason']
    assert rows[-1]['failure_reason']==result['paid_failure_reason']


@pytest.mark.parametrize('noise',['unauthorized','probe'])
def test_noise_and_normal_close_do_not_latch_paid_failure(tmp_path,noise):
    import io
    run,calls=make_run(tmp_path)
    cls=proxy.handler(run);h=object.__new__(cls)
    h.connection=SimpleNamespace(settimeout=lambda n:None)
    h.path='/v1/responses'
    h.headers={'Authorization':'Bearer '+('wrong' if noise=='unauthorized' else run.token),'Content-Length':'2'}
    if noise=='probe':h.headers['X-T779-Probe']='invalid-nonce'
    h.rfile=io.BytesIO(b'{}');h.wfile=io.BytesIO()
    h.send_response=lambda *a:None;h.send_header=lambda *a:None;h.end_headers=lambda:None
    h.do_POST()
    run.close()
    assert not calls and run.failure_reason is None
    assert proxy.verify_closed_admission(tmp_path/'proxy.jsonl')['failure_reason'] is None


def test_historical_thirty_reads_cannot_create_a_runnable_manifest():
    m=manifest();m['limits']['read_requests']=30
    with pytest.raises(ValueError,match='minimum_complete_path_reads'):
        contract.validate_manifest(m)


def bind_synthetic_count(m, directory):
    import hashlib
    import time
    p = directory / 'input-count-synthetic.json'
    proof = directory / 'input-count-synthetic-evidence.txt'
    proof.write_text('SYNTHETIC MOCK ONLY: not real authorization, pricing or compatibility evidence')
    evidence = {'path':str(proof), 'sha256':hashlib.sha256(proof.read_bytes()).hexdigest()}
    m['input_count_receipt'] = str(p)
    value = dict(run_id=m['run_id'],model=m['model'],account_scope=m['billing']['account_scope'],
        input_count=m['input_count'],endpoint=proxy.COUNT_ENDPOINT,
        exact_final_payload_supported=True,authorized=True,
        receipt_sha256={k:v for k,v in m['receipt_sha256'].items() if k != 'input_count_receipt'},
        valid_from_epoch=time.time()-60,valid_until_epoch=time.time()+3600,
        fee_evidence=evidence,payload_compatibility_evidence=evidence,authorization_evidence=evidence)
    p.write_text(json.dumps(value))
    m['receipt_sha256']['input_count_receipt']=hashlib.sha256(p.read_bytes()).hexdigest()


def test_whole_final_bytes_counted_and_dispatched(tmp_path):
    seen=[]
    def count(req):
        seen.append(req.content)
        return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':8192})
    run,calls=make_run(tmp_path,count_handler=count)
    value=json.loads(body())
    value.update(instructions='system instructions', tools=[{'type':'function','name':'synthetic',
        'parameters':{'type':'object','properties':{'text':{'type':'string'}}}}])
    value['input'] += [{'type':'function_call','name':'synthetic','call_id':'x','arguments':'{"text":"history"}'},
                       {'type':'function_call_output','call_id':'x','output':'tool result'}]
    for phase in contract.PHASES:
        if phase != 'setup':run.set_phase(phase)
        value['instructions'] += phase
        run.forward(json.dumps(value).encode())
    run.close()
    assert len(seen)==4 and seen == [r.content for r in calls]
    for raw in seen:
        v=json.loads(raw)
        assert v['tools']==value['tools'] and v['input']==value['input']
        assert v['store'] is False and v['tool_choice']=='none' and v['max_output_tokens']==300
    assert proxy.verify_closed_admission(tmp_path/'proxy.jsonl')['count_reservations']==4


@pytest.mark.parametrize('kind',['unknown','boolean','negative','oversize','http','timeout','late','overbound','close'])
def test_count_failure_never_dispatches_inference(tmp_path,kind):
    clock=[0]
    def count(req):
        if kind=='timeout':raise httpx.ReadTimeout('synthetic')
        if kind=='late':clock[0]=6
        if kind=='close':run.closing.set()
        if kind=='oversize':return httpx.Response(200,content=b'x'*4097)
        return httpx.Response(503 if kind=='http' else 200,json={
            'object':'unknown' if kind=='unknown' else 'response.input_tokens',
            'input_tokens':True if kind=='boolean' else -1 if kind=='negative' else 8193 if kind=='overbound' else 3})
    run,calls=make_run(tmp_path,count_handler=count,clock=lambda:clock[0])
    with pytest.raises(proxy.Refused):run.forward(body())
    with pytest.raises(proxy.Refused):run.forward(body())
    assert not calls and run.count_requests==1 and run.attempts==0
    run.close()


@pytest.mark.parametrize('field,value',[('previous_response_id','old'),('conversation','old'),('prompt',{'id':'remote'}),
    ('tools',[{'type':'web_search'}])])
def test_hidden_context_rejected_before_count(tmp_path,field,value):
    run,calls=make_run(tmp_path,count_handler=lambda _:pytest.fail('count must not run'))
    v=json.loads(body());v[field]=value
    with pytest.raises(proxy.Refused):run.forward(json.dumps(v).encode())
    assert run.count_requests==0 and not calls
    run.close()


def test_count_fee_consumed_and_request_cap_shared_across_phases(tmp_path):
    m=manifest();m['input_count'].update(max_requests=1,usd_per_request_cap='0.01')
    run,calls=make_run(tmp_path,reviewed_manifest=m)
    run.forward(body());run.set_phase('dialogue_1')
    with pytest.raises(proxy.Refused):run.forward(body())
    run.close()
    receipt=proxy.verify_closed_admission(tmp_path/'proxy.jsonl')
    assert len(calls)==1 and receipt['total_nanodollars_reserved']==18492000
    assert receipt['count_reservations']==1


def test_count_disabled_before_any_executor_side_effect(tmp_path,monkeypatch):
    m=manifest();m['input_count']['enabled']=False
    monkeypatch.setattr(v2.httpx,'Client',lambda *a,**k:pytest.fail('network forbidden'))
    with pytest.raises(RuntimeError,match='disabled'):v2.execute(m,tmp_path/'run')
    assert not (tmp_path/'run').exists()


def test_output_including_reasoning_cannot_exceed_1024():
    m=manifest();m['limits']['output_tokens_per_start']=1025
    with pytest.raises(ValueError,match='reasoning_and_visible'):contract.validate_manifest(m)


def test_count_receipt_expiry_and_model_binding_fail_closed(tmp_path):
    m=manifest();bind_synthetic_count(m,tmp_path)
    with pytest.raises(RuntimeError,match='expired'):receipts.verify_input_count(m,now=0)
    m['model']='gpt-5.2-mini'
    with pytest.raises(RuntimeError,match='binding'):receipts.verify_input_count(m)


def test_count_evidence_revoked_during_exchange_prevents_inference(tmp_path):
    def count(req):
        (tmp_path/'input-count-synthetic-evidence.txt').write_text('revoked')
        return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':3})
    run,calls=make_run(tmp_path,count_handler=count)
    with pytest.raises(proxy.Refused):run.forward(body())
    assert not calls and run.attempts==0
    run.close()


@pytest.mark.parametrize('change', ['expiry', 'evidence'])
def test_count_authority_changed_during_reservation_prevents_all_dispatch(tmp_path, monkeypatch, change):
    counted=[]
    def count(req):
        counted.append(req.content)
        return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':3})
    m=manifest();m['input_count']['usd_per_request_cap']='0.01'
    run,calls=make_run(tmp_path,reviewed_manifest=m,count_handler=count)
    receipt=json.loads(Path(run.manifest['input_count_receipt']).read_text())
    record=run.record
    def delayed_record(row):
        record(row)
        if row['event']=='count_reserved':
            if change=='expiry':
                monkeypatch.setattr(receipts.time,'time',lambda:receipt['valid_until_epoch']+1)
            else:
                Path(receipt['authorization_evidence']['path']).write_text('revoked during reservation')
    run.record=delayed_record
    with pytest.raises(proxy.Refused,match='input_count_failed'):run.forward(body())
    with pytest.raises(proxy.Refused):run.forward(body())
    run.close()
    assert counted==[] and calls==[]
    closed=proxy.verify_closed_admission(tmp_path/'proxy.jsonl')
    assert closed['count_reservations']==1 and closed['send_reservations']==0
    assert closed['total_nanodollars_reserved']==10000000
    assert closed['total_input_tokens_reserved']==0
