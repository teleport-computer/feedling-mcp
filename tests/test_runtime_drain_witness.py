"""Offline parent generation and causal post-fence drain checks."""
from copy import deepcopy
from types import SimpleNamespace
import pytest
from tools.release_window import drain
from model_api_runtime.v2 import child_supervisor, slot_protocol


def scope():
    return {'parent_generation':'parent-1','worker_ids':{p:'synthetic:'+p for p in drain.POOLS},
            'pool_slots':{p:1 for p in drain.POOLS}}


def metrics(beat, sample, progress, state='idle'):
    return {'worker_heartbeat_count':3,'worker_heartbeats':[
        {'worker_id':'synthetic:'+p,'pool':p,'kind':'turn','age_sec':0,'beat_at_epoch':beat,
         'runtime_state':{'slots':{'configured':1,'healthy':1,'restarting':0},
            'drain':{'parent_generation':'parent-1','slots':[{'slot_id':p+'-0',
                'slot_generation':'child-1','pid':123,'sample_monotonic':sample,
                'progress_monotonic':progress,'state':state}]}}} for p in drain.POOLS]}


def test_fresh_heartbeat_with_old_idle_is_not_a_post_fence_ack():
    proof=drain.Proof(metrics(90,10,9),scope(),100)
    assert proof.observe(metrics(101,11,9)) is None
    assert proof.observe(metrics(102,12,9)) is None
    assert proof.observe(metrics(103,13,9)) is None
    assert proof.observe(metrics(104,14,13)) is not None


@pytest.mark.parametrize('bad',['missing','unknown','restart','busy','stale','truncated','extra_parent'])
def test_drain_rejects_unproven_parent_or_slot(bad):
    proof=drain.Proof(metrics(90,10,9),scope(),100)
    proof.observe(metrics(101,11,10));proof.observe(metrics(102,12,11))
    value=metrics(103,14,13)
    row=value['worker_heartbeats'][1]
    slot=row['runtime_state']['drain']['slots'][0]
    if bad=='missing':value['worker_heartbeats'].pop()
    if bad=='unknown':slot['state']='unknown'
    if bad=='restart':slot['slot_generation']='new-child'
    if bad=='busy':slot['state']='busy'
    if bad=='stale':row['age_sec']=31
    if bad=='truncated':value['worker_heartbeat_count']=4
    if bad=='extra_parent':
        extra=deepcopy(row);extra['worker_id']='unreviewed:wake'
        value['worker_heartbeats'].append(extra);value['worker_heartbeat_count']=4
    if bad=='busy':assert proof.observe(value) is None
    else:
        with pytest.raises(RuntimeError):proof.observe(value)


def test_real_parent_projection_never_treats_missing_snapshot_as_idle():
    parent=child_supervisor.ChildSupervisor(lambda:None,slot_id='foreground-0',liveness_timeout_sec=30)
    parent._slot_generation='generation'
    parent._proc=SimpleNamespace(pid=123,is_alive=lambda:True)
    assert parent.drain_witness()['state']=='unknown'
    parent._snapshot=slot_protocol.SlotProgress('foreground-0','generation',1,None,'idle',None)
    assert parent.drain_witness()['state']=='idle'
    parent._snapshot=slot_protocol.SlotProgress('other-slot','generation',1,None,'idle',None)
    assert parent.drain_witness()['state']=='unknown'
    parent._snapshot=slot_protocol.SlotProgress('foreground-0','old',1,None,'idle',None)
    assert parent.drain_witness()['state']=='unknown'

@pytest.mark.parametrize('blocked,budget', [(False,40), (True,40), (False,8)])
def test_cleanup_waits_for_fence_parent_progress_and_resident_exit(tmp_path, monkeypatch, blocked, budget):
    from tools.release_window import bounded_v2 as v2, receipts, contract
    events=[]
    now=[0]
    m={'drain_scope':scope(),'limits':{'drain_seconds':5,'cleanup_seconds':10,'poll_seconds':1}}
    state={'fenced':False,'deleted':False,'metric':0}
    class Client:
        def delete(self,path,**kw):
            assert path=='/v1/model_api/delete'
            events.append('fence');state['fenced']=True
            return SimpleNamespace(status_code=200)
        def post(self,path,**kw):
            assert path=='/v1/account/reset'
            assert state['metric']>=4 and not blocked
            events.append('reset');state['deleted']=True
            return SimpleNamespace(status_code=200)
        def get(self,path,**kw):return SimpleNamespace(status_code=401)
    class Admin:
        def get(self,path,**kw):
            state['metric']+=1
            n=state['metric']
            value=metrics(90 if n==1 else 99+n,8+n,7+n)
            return SimpleNamespace(raise_for_status=lambda:None,json=lambda:value)
    def observe(*a,**kw):
        return dict(member=not state['deleted'],pending=0,claimed=0,running=0,
            resident_instance_seen=False,resident_lease_active=blocked,job_leases=0,route_count=0 if state['fenced'] else 1,
            runtime_mode='resident_cli' if state['fenced'] else 'db_action_v2',
            runtime_state='resident' if state['fenced'] else 'v2',
            generation=2 if state['fenced'] else 1,observed_at='1970-01-01T00:01:40+00:00')
    monkeypatch.setattr(receipts,'resident_guard',lambda *a,**kw:{'fresh':True})
    monkeypatch.setattr(receipts,'verify_environment',lambda *a,**kw:{})
    monkeypatch.setattr(v2,'snapshot',observe)
    monkeypatch.setattr(v2.proxy,'verify_closed_admission',lambda p:{'manifest_digest':contract.digest(m)})
    receipts._write_private_json(tmp_path/'scenario-result.json',{'status':'PASS_SHORT_VISIBLE_ONLY'})
    recovery=tmp_path/'private-original';recovery.write_text('synthetic recovery')
    owner=SimpleNamespace(out=tmp_path,manifest=m,client=Client(),binding=lambda:{},
        verify=lambda:None,save=lambda name,data:events.append(name))
    run=SimpleNamespace(close=lambda:events.append('relay-close'))
    def sleep(n):now[0]+=n
    if blocked or budget==8:
        with pytest.raises(RuntimeError,match='retain fixture'):
            v2.cleanup(owner,run,v2.Reads(budget),admin=Admin(),clock=lambda:now[0],sleep=sleep)
        assert 'reset' not in events
    else:
        assert v2.cleanup(owner,run,v2.Reads(40),admin=Admin(),clock=lambda:now[0],sleep=sleep)=='CONFIRMED'
        assert events.index('relay-close')<events.index('fence')<events.index('runtime-drained')<events.index('reset')
    assert recovery.exists()

@pytest.mark.parametrize('bad',[None,'old_release','missing','extra','stale'])
def test_resident_guard_checks_actual_separate_runner_inventory(bad):
    from tools.release_window import receipts
    manifest={'drain_scope':{'resident_owners':['resident-1']},'expected_release':'a'*40}
    result=[('resident-1','a'*40,1)]
    if bad=='old_release':result=[('resident-1','b'*40,1)]
    if bad=='missing':result=[]
    if bad=='extra':result.append(('unreviewed','a'*40,1))
    if bad=='stale':result=[('resident-1','a'*40,31)]
    queries=[]
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def transaction(self):return self
        def execute(self,sql,params=None):
            queries.append((sql,params))
            return SimpleNamespace(fetchall=lambda:result)
    if bad:
        with pytest.raises(RuntimeError,match='unconfirmed'):receipts.resident_guard(manifest,connector=Conn)
    else:
        assert receipts.resident_guard(manifest,connector=Conn)['fresh']
    assert queries[0][0]=='SET TRANSACTION READ ONLY'
    assert queries[1][1]==(['resident-1'],)


def test_explicit_select_budget_is_charged_before_each_database_read():
    from tools.release_window import receipts, bounded_v2
    executed=[]
    connection=SimpleNamespace(execute=lambda sql:executed.append(sql))
    budget=bounded_v2.Reads(2)
    guarded=receipts._ReadConnection(connection,budget)
    guarded.execute('SET TRANSACTION READ ONLY')
    guarded.execute('SELECT 1')
    guarded.execute('  select 2')
    with pytest.raises(RuntimeError,match='read budget exhausted'):
        guarded.execute('SELECT 3')
    assert executed==['SET TRANSACTION READ ONLY','SELECT 1','  select 2']
    assert budget.remaining==0
