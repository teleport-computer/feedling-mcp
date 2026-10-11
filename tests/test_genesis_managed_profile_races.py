"""R1 reviewer F1: two-attempt profile recomputation on real PG conflicts.

Extends claude's independent valid-profile race probe with existing/absent rows,
opaque preservation, refreshed source metadata and an exhausted second race.
"""
from copy import deepcopy

import pytest
from psycopg.types.json import Jsonb

import db
import genesis_managed_store as persistence
from genesis import managed_artifacts as artifacts
from test_genesis_managed_admission import account
from test_genesis_managed_execution import prepared, start_applying


@pytest.mark.parametrize('initial_exists', [False, True])
@pytest.mark.parametrize('conflicts', [1, 2])
def test_profile_recomputes_once_with_same_claim_connection(prepared, monkeypatch, initial_exists, conflicts):
    store, _, witness = prepared
    start_applying(store, witness)
    profile_store = artifacts.service.v2_profile_store
    kind = profile_store.PROFILE_BLOB_KIND
    build = profile_store.build_profile_document_patching_fields
    output = {'profile': {'memory': '', 'style': 'Genesis style', 'memory_touched': False,
                          'style_touched': True, 'provider_calls': 1}}
    if initial_exists:
        initial = build(store.user_id, state='ok', source={}, last_attempt={},
                        memory_text='Initial memory', style_text='Initial style', previous=None,
                        seal_text=lambda uid, text: {'body': text})
        db.set_blob_strict(store.user_id, kind, initial)
    builds, winners, connections = [], [], []
    original_read, original_cas = persistence.read_blob, persistence.cas_blob
    def read(conn, uid, read_kind):
        connections.append(conn.info.backend_pid)
        return original_read(conn, uid, read_kind)
    def cas(conn, uid, write_kind, expected, doc):
        connections.append(conn.info.backend_pid)
        return original_cas(conn, uid, write_kind, expected, doc)
    monkeypatch.setattr(persistence, 'read_blob', read)
    monkeypatch.setattr(persistence, 'cas_blob', cas)
    def racing_build(*args, **kwargs):
        candidate = build(*args, **kwargs)
        builds.append(deepcopy(kwargs))
        if len(builds) <= conflicts:
            winner = deepcopy(candidate)
            winner['memory'] = {'envelope': {'body_ct': f'opaque-winner-{len(builds)}'}, 'chars': 7}
            winner['style'] = {'envelope': {'body': f'Concurrent style {len(builds)}'}, 'chars': 18}
            profile_store.validate_profile_document(winner)
            with db.get_pool().connection() as other:
                assert other.info.backend_pid != connections[0]
                other.execute("SET LOCAL statement_timeout='1500ms'")
                other.execute('INSERT INTO user_blobs(user_id,kind,doc) VALUES(%s,%s,%s) '
                              'ON CONFLICT(user_id,kind) DO UPDATE SET doc=EXCLUDED.doc',
                              (store.user_id, kind, Jsonb(winner)))
                if len(builds) == 1:
                    other.execute('INSERT INTO memory_moments(user_id,moment_id,doc) VALUES(%s,%s,%s)',
                                  (store.user_id, 'race-card', Jsonb({'id':'race-card','updated_at':'2026-10-11T14:00:00'})))
            winners.append(winner)
        return candidate
    monkeypatch.setattr(profile_store, 'build_profile_document_patching_fields', racing_build)
    if conflicts == 1:
        assert artifacts.write_artifact(store, 'j', witness, output, 'profile')[2] == 'written'
    else:
        with pytest.raises(persistence.DependencyUnavailable) as error:
            artifacts.write_artifact(store, 'j', witness, output, 'profile')
        assert error.value.dependency == kind and error.value.reason == 'dependency_changed'
    assert len(builds) == 2
    assert len(set(connections)) == 1
    assert builds[1]['previous'] == winners[0]
    assert builds[0]['source']['card_count'] == 0
    assert builds[1]['source']['card_count'] == 1
    assert builds[1]['source']['max_updated_at'] == '2026-10-11T14:00:00'
    persisted = db.get_blob_strict(store.user_id, kind)
    if conflicts == 1:
        assert persisted['memory'] == winners[0]['memory']
        assert persisted['style']['envelope']['body'] == 'Genesis style'
    else:
        assert persisted == winners[1]
    assert persistence.execution_state(store.user_id, 'j', witness) == ('applying', None)
