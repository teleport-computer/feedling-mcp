"""Per-user unknown admission must not become deletion or stop a batch."""
from contextlib import contextmanager
import pytest
import db
from agent_runtime import supervisor as sm


@pytest.fixture
def fleet(monkeypatch):
    class State:
        busy=set()
        spawned=[]
        renewed=[]
        acquired=set()
    w=State()
    w.busy=set();w.spawned=[];w.renewed=[];w.acquired=set()
    w.sup=sm.Supervisor(owner='synthetic',lease_ttl=60,data_root='/synthetic',
        spawn_fn=lambda entry,uid,home:w.spawned.append(uid) or (123 if uid=='a' else 124),
        alive_fn=lambda pid:True,kill_fn=lambda pid:pytest.fail('unknown is not deletion'))
    @contextmanager
    def guard(uid,entry,*,expected=None):
        if uid in w.busy:raise db.HostedRuntimeConfigBusyError('synthetic')
        yield ('binding',)
    monkeypatch.setattr(sm.admission,'guard',guard)
    monkeypatch.setattr(sm,'_genesis_ready_to_spawn',lambda uid:True)
    monkeypatch.setattr(sm.leases,'acquire',lambda uid,**kw:not w.acquired.add(uid))
    monkeypatch.setattr(sm.leases,'renew',lambda *a,**kw:True)
    monkeypatch.setattr(sm.leases,'release',lambda *a,**kw:pytest.fail('unknown must not release'))
    monkeypatch.setattr(sm.leases,'renew_many',lambda rows,*a,**kw:w.renewed.extend(uid for uid,pid in rows) or {uid for uid,pid in rows})
    for name in ('_mark_child_healthy','_resolve_after_spawn','_enqueue_introduction','_write_token'):
        monkeypatch.setattr(w.sup,name,lambda *a,**kw:None)
    w.roster=[{'user_id':uid,'api_key':'synthetic','driver':'codex'} for uid in ('a','b')]
    return w


def test_busy_renewal_preserves_live_ownership_and_recovers(fleet):
    w=fleet
    w.sup.tick(w.roster)
    w.busy={'a'}
    before=dict(w.sup.children)
    w.sup.renew_live()
    assert set(w.renewed)=={'a','b'} and w.sup.children==before
    w.renewed.clear();w.busy.clear()
    w.sup.renew_live()
    assert set(w.renewed)=={'a','b'} and w.sup.children==before


@pytest.mark.parametrize('phase',['acquire','deferred'])
def test_busy_user_does_not_strand_other_acquisitions_and_recovers(fleet,phase):
    w=fleet
    # a is acquired before b's initial lock failure; in deferred mode both
    # are acquired, then a fails its second guard. Both batch orders matter.
    if phase=='acquire':w.busy={'b'}
    def pre_spawn(ids):
        if phase=='deferred':w.busy.add('a')
    w.sup.tick(w.roster,pre_spawn=pre_spawn)
    healthy='a' if phase=='acquire' else 'b'
    assert w.spawned==[healthy]
    assert w.acquired==({'a'} if phase=='acquire' else {'a','b'})
    w.busy.clear()
    w.sup.tick(w.roster)
    assert sorted(w.spawned)==['a','b']
    assert set(w.sup.children)=={'a','b'}


def test_guarded_body_errors_are_not_silently_swallowed(fleet):
    with pytest.raises(ValueError,match='body failure'):
        with sm._isolated_admission('a',{}):
            raise ValueError('body failure')


@pytest.mark.parametrize('failed',[{'a'},{'a','b'}])
def test_discovery_resolution_unknown_preserves_chain_then_recovers(fleet,monkeypatch,failed):
    w=fleet;unknown=set()
    def capture(uid,entry):
        if uid in unknown:raise db.HostedRuntimeConfigBusyError('synthetic')
        return ('binding',)
    monkeypatch.setattr(sm.admission,'capture',capture)
    monkeypatch.setattr(sm.admission,'envelope_matches',lambda *a:True)
    monkeypatch.setattr(sm,'_fetch_key_envelope',lambda *a,**kw:{'synthetic':True})
    monkeypatch.setattr(sm,'_decrypt_provider_key',lambda *a,**kw:'synthetic-key')
    enabled={e['user_id']:e for e in w.roster}
    def tick():
        resolved=sm._resolve_discovered(enabled,mint_token=lambda uid:'synthetic',api_url='unused',enclave_url='unused',cache={})
        roster=sm._effective_roster([],autodiscover=False,host_all_discovered=resolved)
        w.sup.tick(roster)
        return resolved
    tick();assert set(w.sup.children)=={'a','b'}
    unknown.update(failed);unknown.add('c');enabled['c']={'driver':'codex'}
    resolved=tick()
    assert set(w.sup.children)=={'a','b'} and len(w.spawned)==2
    assert {e['user_id'] for e in resolved if e.get('_admission_unresolved')}==failed|{'c'}
    enabled.pop('c');unknown.clear();tick()
    assert set(w.sup.children)=={'a','b'} and len(w.spawned)==2
    # A successful discovery that actually omits a is confirmed removal.
    stops=[];released=[]
    monkeypatch.setattr(w.sup,'_stop_child',lambda uid,child:stops.append(uid) or True)
    monkeypatch.setattr(sm.leases,'release',lambda uid,*a,**kw:released.append(uid))
    enabled.pop('a');tick()
    assert set(w.sup.children)=={'b'} and stops==released==['a']


def test_whole_discovery_db_failure_is_not_successful_empty_inventory(monkeypatch):
    from types import SimpleNamespace
    def fail():raise RuntimeError('synthetic unavailable')
    monkeypatch.setattr(db,'get_pool',lambda:SimpleNamespace(connection=fail))
    assert db.list_agent_runtime_enabled_users()==[]  # compatibility only
    with pytest.raises(RuntimeError,match='synthetic unavailable'):
        sm._discover_enabled()  # outer loop must skip tick, not hand it []
