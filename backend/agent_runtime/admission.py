"""Resident admission serialized with hosted configuration mutations.

The lock spans the caller's acquire or spawn-and-track operation. Deleting a
route prevents later admissions; a process admitted earlier still requires its
owner's confirmed stop. Lease expiry is never a process-exit acknowledgement.
"""
from contextlib import contextmanager
import db
import hashlib
import json


def envelope_digest(envelope):
    return hashlib.sha256(json.dumps(envelope,sort_keys=True,separators=(',', ':')).encode()).hexdigest()


def envelope_matches(binding, envelope):
    return envelope is not None and binding[-1] == envelope_digest(envelope)


def witness(user_id, entry):
    mode, state, generation = db.get_hosted_runtime_control_strict(user_id)
    route = db.model_api_active_route_strict(user_id)
    if mode != 'resident_cli' or state != 'resident' or not route or route.get('test_status') != 'ok':
        return None
    version = db.model_api_active_route_version_strict(user_id)
    if (not version or version.get('route_id') != route.get('id')
            or not version.get('updated_at_token') or not route.get('credential_id')):
        return None
    # Never start a previously resolved credential against a different route.
    for key in ('provider', 'model', 'base_url'):
        if entry.get(key) is not None and entry[key] != route.get(key):
            return None
    return (generation, str(route.get('id')), str(route.get('credential_id')),
            version['updated_at_token'], envelope_digest(route.get('api_key_envelope')))


@contextmanager
def guard(user_id, entry, *, expected=None):
    with db.hosted_runtime_config_mutation_lock(user_id):
        current = witness(user_id, entry)
        resolved = entry.get('_admission_binding')
        yield current if (resolved is not None and tuple(resolved) == current
                          and (expected is None or expected == current)) else None


def capture(user_id, entry):
    """Capture before resolving secrets, never at first admission of an old key."""
    with db.hosted_runtime_config_mutation_lock(user_id):
        return witness(user_id, entry)
