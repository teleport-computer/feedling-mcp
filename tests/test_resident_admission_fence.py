"""Offline interleavings through real discovery/delete/supervisor orchestration."""
from contextlib import contextmanager
from types import SimpleNamespace
import threading

import pytest
import db
from agent_runtime import supervisor as sm
from hosted import setup_core


@pytest.fixture
def world(monkeypatch):
    w = SimpleNamespace(route=True, generation=1, spawned=[], leases=set(), lock=threading.RLock())
    entry = dict(user_id='fixture', driver='codex', provider='openai', model='model',
                 base_url='https://fixture.invalid', supports_responses=True)
    @contextmanager
    def config_lock(uid):
        assert uid == 'fixture'
        with w.lock:
            yield
    monkeypatch.setattr(db, 'hosted_runtime_config_mutation_lock', config_lock)
    monkeypatch.setattr(db, 'list_agent_runtime_enabled_users_strict', lambda: [entry.copy()] if w.route else [])
    def fence(store):
        w.generation += 1
        return {'hosted_runtime_mode':'resident_cli'}
    def delete(uid):
        w.route = False
        return 1
    monkeypatch.setattr(setup_core.hosted_config_store, 'prepare_model_api_delete', fence)
    monkeypatch.setattr(db, 'model_api_config_delete_strict', delete)
    monkeypatch.setattr(setup_core.notices_core, 'resolve', lambda *a: None)
    monkeypatch.setattr(sm, '_genesis_ready_to_spawn', lambda uid: True)
    monkeypatch.setattr(sm.leases, 'acquire', lambda uid, **kw: not w.leases.add(uid))
    monkeypatch.setattr(sm.leases, 'renew', lambda *a, **kw: True)
    monkeypatch.setattr(sm.leases, 'release', lambda uid, *a, **kw: w.leases.discard(uid))
    monkeypatch.setattr(sm.Supervisor, '_resolve_after_spawn', lambda *a: None)
    monkeypatch.setattr(sm.Supervisor, '_enqueue_introduction', lambda *a: None)
    monkeypatch.setattr(db, 'get_hosted_runtime_control_strict', lambda uid: ('resident_cli','resident',w.generation))
    monkeypatch.setattr(db, 'model_api_active_route_strict', lambda uid: dict(entry, id='route', credential_id='credential', updated_at='version',test_status='ok') if w.route else None)
    monkeypatch.setattr(db, 'model_api_active_route_version_strict', lambda uid: {'route_id':'route','updated_at_token':'version'} if w.route else None)
    w.roster = [dict(value, user_id=uid, api_key='synthetic') for uid,value in sm._discover_enabled().items()]
    for row in w.roster:
        row['_admission_binding'] = sm.admission.capture(row['user_id'], row)
    w.sup = sm.Supervisor(owner='owner',lease_ttl=60,data_root='/synthetic',
        spawn_fn=lambda *a: w.spawned.append(a) or 123, alive_fn=lambda pid: True,
        kill_fn=lambda pid: None)
    w.delete = lambda: setup_core.model_api_delete(SimpleNamespace(user_id='fixture'))
    return w


@pytest.mark.parametrize('when', ['after_discovery','after_acquire','normal'])
def test_deleted_roster_cannot_spawn(world, when):
    if when == 'after_discovery':
        assert world.delete()[1] == 200
    world.sup.tick(world.roster, pre_spawn=(lambda ids: world.delete()) if when == 'after_acquire' else None)
    assert len(world.spawned) == (1 if when == 'normal' else 0)
    if when != 'normal':
        assert not world.leases


def test_generation_change_between_acquire_and_spawn_cancels_lease(world):
    def rotate(ids):
        world.generation += 1
    world.sup.tick(world.roster, pre_spawn=rotate)
    assert world.spawned == []
    assert not world.leases


def test_delete_waits_for_spawn_and_owner_confirms_stop(world, monkeypatch):
    entered, finish, deleted = threading.Event(), threading.Event(), threading.Event()
    alive = {123: True}
    def spawn(*args):
        entered.set()
        assert finish.wait(2)
        world.spawned.append(args)
        return 123
    world.sup.spawn_fn = spawn
    world.sup.alive_fn = lambda pid: alive[pid]
    runner = threading.Thread(target=world.sup.tick, args=(world.roster,))
    runner.start()
    assert entered.wait(2)
    def delete():
        world.delete()
        deleted.set()
    deleter = threading.Thread(target=delete)
    deleter.start()
    assert not deleted.wait(.05), 'delete crossed the guarded spawn'
    finish.set()
    runner.join(2); deleter.join(2)
    assert not runner.is_alive() and not deleter.is_alive()
    assert deleted.is_set() and world.leases == {'fixture'}
    # An unsuccessful kill retains the owned lease and process record.
    world.sup.renew_live()
    assert world.leases == {'fixture'} and 'fixture' in world.sup.children
    world.sup.kill_fn = lambda pid: alive.update({pid:False})
    world.sup.renew_live()
    assert not world.leases and not world.sup.children


def test_pre_resolved_old_key_cannot_adopt_new_generation(world):
    world.roster[0]['provider_key']='synthetic-A'
    world.generation += 1
    world.sup.tick(world.roster)
    assert not world.spawned and not world.leases


def test_unbound_resolved_entry_is_not_blessed_at_first_guard(world):
    world.roster[0].pop('_admission_binding')
    world.sup.tick(world.roster)
    assert not world.spawned and not world.leases


@pytest.mark.parametrize('change',['before_resolve','during_decrypt','envelope_mismatch','normal'])
def test_resolution_cache_is_bound_to_exact_credential_version(world,monkeypatch,change):
    state={'credential':'A','version':'version-A','envelope':{'body_ct':'synthetic-A'}}
    info=world.roster[0].copy()
    def route(uid):
        return dict(info,id='route',credential_id=state['credential'],
                    api_key_envelope=state['envelope'],test_status='ok')
    monkeypatch.setattr(db,'model_api_active_route_strict',route)
    monkeypatch.setattr(db,'model_api_active_route_version_strict',lambda uid:{'route_id':'route','updated_at_token':state['version']})
    fetched=[]
    monkeypatch.setattr(sm,'_fetch_key_envelope',lambda *a,**kw:fetched.append(1) or state['envelope'].copy())
    monkeypatch.setattr(sm,'_decrypt_provider_key',lambda *a,**kw:'synthetic-key-A')
    cache={}
    def resolve():
        return sm._resolve_one('fixture',info,mint_token=lambda uid:'synthetic-token',api_url='synthetic',
            enclave_url='synthetic',cache=cache,refetch_sec=300,clock=10)
    first=resolve()
    assert first['provider_key']=='synthetic-key-A' and len(fetched)==1
    if change=='normal':
        assert resolve()['provider_key']=='synthetic-key-A' and len(fetched)==1
        world.sup.tick([first]);assert len(world.spawned)==1
        return
    if change=='before_resolve':
        state.update(credential='B',version='version-B',envelope={'body_ct':'synthetic-B'})
        monkeypatch.setattr(sm,'_fetch_key_envelope',lambda *a,**kw:None)
        second=resolve()
        assert second is None or 'provider_key' not in second
        world.sup.tick([first]);assert not world.spawned
    else:
        cache.clear()
        if change=='during_decrypt':
            def rotate(*a,**kw):
                state.update(credential='B',version='version-B',envelope={'body_ct':'synthetic-B'})
                return 'synthetic-key-A'
            monkeypatch.setattr(sm,'_decrypt_provider_key',rotate)
        else:
            monkeypatch.setattr(sm,'_fetch_key_envelope',lambda *a,**kw:{'body_ct':'synthetic-other'})
        assert resolve() is None
        assert not cache
