"""Managed execution fences. No provider calls may run inside these contexts."""
from contextlib import contextmanager

import db
import genesis_managed_store as persistence
from genesis import managed_eligibility as eligibility
from memory import service as memory_service


@contextmanager
def mutation_guard(store, job_id, witness, *, phases=("applying",), memory=False):
    """Bind the actual primary mutation to its current claim and write policy.

    Memory writers must enter the process-local lock before DB fences; their
    nested persistence helpers then reuse the yielded transaction.
    """
    uid = store.user_id
    if memory:
        if getattr(store, "memory_lock", None) is None:
            raise persistence.DependencyUnavailable("memory_fence")
        with memory_service.mutation_lock(store):
            with db.memory_user_mutation_fence(uid) as conn:
                with persistence.guard(uid, job_id, witness, phases=phases, conn=conn) as pair:
                    eligibility.require_policy(uid)
                    yield pair
                    eligibility.require_policy(uid)
    else:
        with persistence.guard(uid, job_id, witness, phases=phases) as pair:
            eligibility.require_policy(uid)
            yield pair
            eligibility.require_policy(uid)


def begin_execution(store, job_id, witness):
    """Commit before any provider or artifact work; failure gives no permission."""
    with mutation_guard(store, job_id, witness, phases=("prepared",)) as (conn, job):
        changed = persistence._set_phase_on_connection(conn, job, "execution_started")
    persistence._mirror_job(changed)
    return changed


def admit_apply(store, job_id, witness):
    """One admission; duplicate requests must read state rather than replay."""
    with mutation_guard(store, job_id, witness, phases=("execution_started",)) as (conn, job):
        changed = persistence._set_phase_on_connection(conn, job, "applying")
    persistence._mirror_job(changed)
    return changed
