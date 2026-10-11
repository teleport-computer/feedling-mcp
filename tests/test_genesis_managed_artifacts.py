"""Actual artifact CAS and policy checks on isolated PostgreSQL."""
import json
import pytest
from psycopg.types.json import Jsonb
import db
import genesis_managed_store as persistence
from core import envelope
from genesis import managed_artifacts as artifacts, managed_execution as execution
from test_genesis_managed_admission import account
from test_genesis_managed_execution import prepared, start_applying

@pytest.mark.parametrize('kind', ['persona', 'voice', 'identity'])
def test_rejected_claim_cannot_write_artifact(prepared, kind):
    store, _, witness = prepared
    start_applying(store, witness)
    with pytest.raises(persistence.ClaimRejected):
        artifacts.write_artifact(store, 'j', {**witness, 'capability':'wrong'}, {}, kind)


def test_strict_blob_cas_absent_conflict_rolls_back(prepared):
    store, _, witness = prepared
    start_applying(store, witness)
    with db.get_pool().connection() as conn:
        conn.execute("INSERT INTO user_blobs(user_id,kind,doc) VALUES(%s,'genesis_persona',%s)",
                     (store.user_id,Jsonb({'external':True})))
    with pytest.raises(persistence.DependencyUnavailable):
        with execution.mutation_guard(store,'j',witness) as (conn,job):
            persistence.cas_blob(conn,store.user_id,'genesis_persona',None,{'overwritten':True})
    assert db.get_blob_strict(store.user_id,'genesis_persona')=={'external':True}


def test_actual_identity_write_rejects_absent_to_sealed(prepared):
    store, _, witness = prepared
    start_applying(store, witness)
    sealed={'body_ct':'opaque','owner_user_id':store.user_id}
    db.set_blob_strict(store.user_id,'identity',sealed)
    with pytest.raises(persistence.DependencyUnavailable,match='identity'):
        artifacts.write_artifact(store,'j',witness,{'identity':{'agent_name':'A'}},'identity')
    assert db.get_blob_strict(store.user_id,'identity')==sealed


def test_local_only_builder_never_falls_through_to_enclave(prepared,monkeypatch):
    store, _, _=prepared
    monkeypatch.setattr(envelope,'resolve_content_encryption',lambda _: 'on')
    monkeypatch.setattr(envelope,'_model_api_key_encryption_material',lambda _:pytest.fail('enclave accessed'))
    with pytest.raises(persistence.DependencyUnavailable):
        envelope._build_shared_envelope_for_store(store,b'hello',local_only=True)


def test_actual_persona_and_profile_share_claim_transaction(prepared):
    store,_,witness=prepared
    start_applying(store,witness)
    output={'source_kind':'ai_persona','source_family':'ai_persona','persona':{'content':'A careful companion.'},
            'profile':{'memory':'','style':'Patient and concise.','memory_touched':False,'style_touched':True,'provider_calls':1}}
    # Exact persona input contract is exercised, no replacement writer.
    result=artifacts.write_artifact(store,'j',witness,output,'profile')
    assert result[2]=='written'
    doc=db.get_blob_strict(store.user_id,artifacts.service.v2_profile_store.PROFILE_BLOB_KIND)
    assert doc['style']['envelope']['body']=='Patient and concise.'


def test_actual_memory_add_and_later_target_conflict(prepared):
    from core import store as stores
    from genesis import managed_memory
    store,_,witness=prepared
    store=stores.get_store(store.user_id)
    execution.begin_execution(store,'j',witness)
    writer=managed_memory.store_writer(store,'j',witness)
    ids=writer([{'op':'add','card':{'type':'fact','summary':'Project Alpha','content':'Long term project Alpha','bucket':'Projects'}}],'k1')
    assert len(ids)==1 and ids[0]
    with pytest.raises(persistence.DependencyUnavailable):
        writer([{'op':'supersede','target_id':'missing','card':{'type':'fact','summary':'Project Beta','content':'Long term project Beta','bucket':'Projects'}}],'k2')
    assert len(db.memory_load_strict(store.user_id))==1

@pytest.mark.parametrize('phase,status',[('prepared','uploaded'),('execution_started','failed'),('applying','failed')])
def test_actual_orphan_recovery_no_paid_replay(prepared,phase,status):
    store,_,witness=prepared
    if phase!='prepared':execution.begin_execution(store,'j',witness)
    if phase=='applying':execution.admit_apply(store,'j',witness)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE genesis_import_jobs SET worker_claimed_at=now()-interval '1 hour' WHERE user_id=%s",(store.user_id,))
    assert db.genesis_reclaim_orphaned_processing_jobs([],dead_sec=60,error='old')==[]
    rows=persistence.recover(live_workers=[],older_than=60)
    assert len(rows)==1 and rows[0]['status']==status
    assert rows[0]['output'].get('keep_output')==2


def test_marker_first_outputs_and_finalize_do_not_mutate_claim(prepared):
    from genesis import genesis_core
    store,before,witness=prepared
    for payload in [{'reducer_output':{}},{'reducer_output':{},'managed_execution':{'generation':'fake','capability':'fake'}}]:
        body,status=genesis_core.apply_outputs(store,'j',payload,api_key=None,runtime_token='test')
        assert status==409
        assert db.genesis_get_job(store.user_id,'j')==before
    assert genesis_core.finalize(store,'j',{'reducer_output':{}},api_key=None)[1]==409
    assert 'managed_worker_execution' not in genesis_core._public_job(before)['metadata']
    assert db.genesis_get_job(store.user_id,'j')==before


def test_managed_complete_and_duplicate_readback(prepared):
    from core import store as stores
    from genesis import managed_apply
    store,_,witness=prepared
    store=stores.get_store(store.user_id)
    execution.begin_execution(store,'j',witness)
    result=managed_apply.apply(store,'j',{},witness)
    assert persistence.execution_state(store.user_id,'j',witness)==('done',result)
    assert managed_apply.apply(store,'j',{'identity':{'agent_name':'duplicate'}},witness)==result
    assert db.get_blob_strict(store.user_id,'identity') is None


def test_legacy_mutation_and_failed_finalization_cannot_overwrite_managed(prepared):
    store,before,witness=prepared
    assert db.genesis_set_job_status(store.user_id,'j',status='failed',reject_managed=True) is None
    execution.begin_execution(store,'j',witness)
    persistence.fail(store.user_id,'j',witness,'test_failure')
    failed=db.genesis_get_job(store.user_id,'j')
    assert db.genesis_mark_finalized(store.user_id,'j') is None
    assert db.genesis_get_job(store.user_id,'j')==failed


def test_prepared_recovery_preserves_upload_timestamps(prepared):
    store,before,witness=prepared
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE genesis_import_jobs SET worker_claimed_at=now()-interval '1 hour' WHERE user_id=%s",(store.user_id,))
    recovered=persistence.recover(live_workers=[],older_than=60)[0]
    assert recovered['updated_at']==before['updated_at']
    assert recovered['finalized_at']==before['finalized_at']


def test_client_metadata_cannot_mint_private_markers():
    from genesis import service
    assert service._safe_job_metadata({'managed_worker_execution':{},'managed_worker_capability_hash':'forged','window_count':3})=={'window_count':3}


def test_actual_identity_commit_survives_later_artifact_failure(prepared,monkeypatch):
    from core import store as stores
    from genesis import managed_apply
    store,_,witness=prepared
    store=stores.get_store(store.user_id)
    execution.begin_execution(store,'j',witness)
    monkeypatch.setattr(artifacts,'write_profile',lambda *_: (_ for _ in ()).throw(RuntimeError('profile_injected')))
    output={'identity':{'agent_name':'Companion','dimensions':[{'name':'kind','value':'patient'}]}}
    with pytest.raises(RuntimeError,match='profile_injected'):
        managed_apply.apply(store,'j',output,witness)
    identity=db.get_blob_strict(store.user_id,'identity')
    assert identity and json.loads(identity['body'])['agent_name']=='Companion'
    with pytest.raises(persistence.ClaimRejected):
        managed_apply.apply(store,'j',output,witness)
    assert db.get_blob_strict(store.user_id,'identity')==identity
    with db.get_pool().connection() as conn:
        outcomes=dict(conn.execute('SELECT artifact,outcome FROM distillation_artifact_attempts WHERE user_id=%s',(store.user_id,)).fetchall())
    assert outcomes['identity']=='initialized' and outcomes['profile']=='write_failed'


def test_opaque_untouched_profile_side_preserved(prepared):
    store,_,witness=prepared
    kind=artifacts.service.v2_profile_store.PROFILE_BLOB_KIND
    opaque={'envelope':{'body_ct':'opaque-memory-ciphertext','K_user':'opaque-key'},'chars':7}
    db.set_blob_strict(store.user_id,kind,{'v':1,'state':'ok','source':{},'last_attempt':{},'memory':opaque,'style':{'envelope':{'body':''},'chars':0}})
    start_applying(store,witness)
    artifacts.write_artifact(store,'j',witness,{'profile':{'memory':'','style':'patient',
                             'memory_touched':False,'style_touched':True,'provider_calls':1}},'profile')
    assert db.get_blob_strict(store.user_id,kind)['memory']==opaque


def test_managed_memory_can_supersede_its_own_committed_card(prepared):
    from core import store as stores
    from genesis import managed_memory
    store,_,witness=prepared
    store=stores.get_store(store.user_id)
    execution.begin_execution(store,'j',witness)
    writer=managed_memory.store_writer(store,'j',witness)
    original=writer([{'op':'add','card':{'type':'fact','summary':'Project Alpha','content':'Long term project Alpha','bucket':'Projects'}}],'add')
    updated=writer([{'op':'supersede','target_id':original[0],'card':{'type':'fact','summary':'Project Beta','content':'Project renamed to Beta','bucket':'Projects'}}],'replace')
    assert len(updated)==1 and updated[0]!=original[0]


def test_managed_apply_cannot_use_legacy_deferred_completion(prepared):
    from genesis import service
    store,before,witness=prepared
    with pytest.raises(persistence.ClaimRejected):
        service.apply_reducer_output(store,None,'j',{},complete_job=False,managed_witness=witness)
    assert db.genesis_get_job(store.user_id,'j')==before
