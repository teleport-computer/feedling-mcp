"""Authenticated one-shot output application for no-enclave managed claims."""
import genesis_managed_store as persistence
import distillation_ledger
from genesis import managed_artifacts, managed_execution, service


def apply(store, job_id, output, witness):
    state, result = persistence.execution_state(store.user_id, job_id, witness)
    if state == 'done':
        return result
    if state != 'execution_started':
        raise persistence.ClaimRejected('managed_claim_state_changed')
    service._reject_raw_reducer_fields(output)
    output={**output,'job_id':job_id}
    # The marked worker writes its garden under execution guards before apply.
    if output.get('memories') or output.get('facts'):
        raise persistence.ClaimRejected('managed_unbound_memory_outputs')
    managed_execution.admit_apply(store,job_id,witness)
    try:
        service.notices.resolve(store, "genesis:")
        garden=output.get('garden_import') or {}
        if not garden:
            with distillation_ledger.ArtifactAttempt(store, job_id, "memory") as attempt:
                attempt.finish("not_provided")
        dropped = max(0, int(garden.get("dropped") or 0))
        if dropped:
            service.notices.emit(
                store, source="genesis", error_class="genesis_partial", blame="system",
                severity="warning", user_text=service.catalog.user_text_for("genesis_partial"),
                detail=f"dropped {dropped} card(s)", dedupe_key=f"genesis:{job_id}:partial")
        memory_count=max(0,int(garden.get('cards_written') or 0))
        identity=managed_artifacts.write_artifact(store,job_id,witness,output,'identity')
        persona=managed_artifacts.write_artifact(store,job_id,witness,output,'persona')
        voice=managed_artifacts.write_artifact(store,job_id,witness,output,'voice')
        profile=managed_artifacts.write_artifact(store,job_id,witness,output,'profile')
        result={'memory_action_count':memory_count,'memory_results':[],
                'identity_status':identity,'persona_ref':persona[0],'persona_sha256':persona[1],
                'voice_ref':voice[0],'voice_sha256':voice[1],
                'profile_ref':profile[0],'profile_sha256':profile[1],'profile_status':profile[2]}
        with managed_execution.mutation_guard(store,job_id,witness) as (conn,job):
            completed, receipts=persistence.complete_on_connection(conn,job,result,service._safe_reducer_doc(job_id,output))
        persistence.mirror_outputs(receipts)
        persistence._mirror_job(completed)
        service.write_genesis_state(store,completed,status=service.DONE_JOB_STATUS)
        return result
    except Exception:
        # Only this admitted application may fail applying. An HTTP timeout in
        # the worker cannot do so. Already committed artifact/LLM rows survive.
        try:
            failed = persistence.fail(store.user_id,job_id,witness,'managed_apply_failed',server=True)
            service.write_genesis_state(store, failed, status=service.FAILED_JOB_STATUS)
        except persistence.ClaimRejected:
            pass  # done or a successor: never overwrite authoritative state
        raise
