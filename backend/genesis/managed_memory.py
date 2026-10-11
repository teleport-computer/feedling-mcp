"""Garden import writer with same-transaction claim/target/policy checks."""
import genesis_managed_store as persistence
from genesis import import_engine, managed_eligibility, managed_execution
from memory import actions, garden_import, service


def store_writer(store, job_id, witness):
    def execute(batch):
        rows = []
        # One garden batch is atomic; no provider or enclave inside this fence.
        with managed_execution.mutation_guard(
            store, job_id, witness, phases=("execution_started",), memory=True,
        ) as (conn, job):
            for action in batch:
                kind = action.get("type")
                if kind == "memory.supersede":
                    targets = actions._memory_supersedes_list(action)
                    for target in targets:
                        row = conn.execute(
                            "SELECT doc FROM memory_moments WHERE user_id=%s AND moment_id=%s",
                            (store.user_id,target),
                        ).fetchone()
                        if row is None:
                            raise persistence.DependencyUnavailable("garden", "dependency_changed")
                        card = service.to_v1_card(row[0])
                        managed_eligibility.object_body(card,store.user_id,"garden")
                        if not actions._memory_supersede_target_is_active(card):
                            raise persistence.DependencyUnavailable("garden", "dependency_changed")
                    body, _, status = actions._memory_supersede_action(
                        store,None,action,local_only=True)
                elif kind == "memory.add":
                    body, _, status = actions._memory_add_action(store,action,local_only=True)
                else:
                    raise persistence.ClaimRejected("managed_memory_action_invalid")
                if status >= 400:
                    # In particular, never convert a changed target into an add.
                    raise persistence.DependencyUnavailable("garden", "write_rejected")
                rows.append(body)
        return rows
    def write(mutations,key):
        # This claim cannot replay after execution started, including lost replies.
        return garden_import.write_with_executor(
            mutations,build_action=lambda m:import_engine.memory_action(m,store=store),
            execute=execute,idempotency_key=key)
    return write
