"""Real strict SQL helper control flow with local connection fault injection."""
from contextlib import nullcontext
from types import SimpleNamespace
import pytest
import db
from agent_runtime import supervisor as sm


@pytest.fixture
def reader_world(monkeypatch):
    w=SimpleNamespace(failure=None,missing=None,queries=[],killed=[],released=[],renewed=[],alive=True)
    row=[None]*26
    row[0:4]=['route','credential','openai','model']
    row[6]='https://synthetic.invalid';row[13]='ok';row[25]='version'
    route=tuple(row)+({'body_ct':'synthetic'},)
    class Conn:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def execute(self,sql,params):
            assert params==('fixture',)
            kind='route' if 'c.api_key_envelope' in sql else 'version'
            w.queries.append(kind)
            if w.failure==kind:raise RuntimeError('synthetic read unavailable')
            result=None if w.missing==kind else route if kind=='route' else ('route','openai','model','https://synthetic.invalid','version.US')
            return SimpleNamespace(fetchone=lambda:result)
    monkeypatch.setattr(db,'get_pool',lambda:SimpleNamespace(connection=Conn))
    monkeypatch.setattr(db,'hosted_runtime_config_mutation_lock',lambda uid:nullcontext())
    monkeypatch.setattr(db,'get_hosted_runtime_control_strict',lambda uid:('resident_cli','resident',1))
    entry={'user_id':'fixture','provider':'openai','model':'model','base_url':'https://synthetic.invalid','api_key':'synthetic'}
    entry['_admission_binding']=sm.admission.capture('fixture',entry)
    def kill(pid):w.killed.append(pid);w.alive=False
    w.sup=sm.Supervisor(owner='synthetic',lease_ttl=60,data_root='/synthetic',
        spawn_fn=lambda *a:pytest.fail('must not spawn'),alive_fn=lambda pid:w.alive,kill_fn=kill)
    w.sup.children['fixture']={'pid':123,'entry':entry,'home':'/synthetic','started_at':0,
        'admission_witness':entry['_admission_binding']}
    monkeypatch.setattr(sm.leases,'renew_many',lambda rows,*a,**kw:w.renewed.extend(rows) or {'fixture'})
    monkeypatch.setattr(sm.leases,'renew',lambda *a,**kw:True)
    monkeypatch.setattr(sm.leases,'release',lambda *a,**kw:w.released.append(a))
    monkeypatch.setattr(w.sup,'_mark_child_healthy',lambda *a,**kw:None)
    w.entry=entry
    return w


@pytest.mark.parametrize('reader',['route','version'])
@pytest.mark.parametrize('phase',['tick','renew'])
def test_real_read_failure_preserves_child_then_recovers_and_real_absence_revokes(reader_world,reader,phase):
    w=reader_world
    def run():
        if phase=='tick':w.sup.tick([w.entry])
        else:w.sup.renew_live()
    w.failure=reader
    run()
    assert reader in w.queries and not w.killed and not w.released
    assert 'fixture' in w.sup.children
    w.failure=None
    run()
    assert not w.killed and not w.released and 'fixture' in w.sup.children
    w.missing=reader
    run()
    assert w.killed==[123] and len(w.released)==1 and not w.sup.children


@pytest.mark.parametrize('reader',['route','version'])
def test_best_effort_compatibility_and_strict_error_are_distinct(reader_world,reader):
    w=reader_world;w.failure=reader
    name='model_api_active_route'+('_version' if reader=='version' else '')
    assert getattr(db,name)('fixture') is None
    with pytest.raises(RuntimeError,match='synthetic read unavailable'):
        getattr(db,name+'_strict')('fixture')
