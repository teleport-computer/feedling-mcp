"""Real local blob persistence: failed reads and losing writers preserve identity."""
import json
import threading
from types import SimpleNamespace

import pytest

import db
import distillation_ledger
from conftest import seed_user
from genesis import service as genesis, plaintext_garden
from identity import service as identity


OUTPUT = {'identity': {'agent_name': 'New', 'dimensions': []},
          'relationship_started_at': '2026-01-01'}


@pytest.fixture
def lane(monkeypatch):
    uid = 'usr_genesis_init_safety'
    seed_user(uid)
    store = SimpleNamespace(user_id=uid, identity_lock=threading.Lock())
    events = []
    def build(store, raw, item_id=None):
        return {'id': item_id or 'identity_new', 'body': raw.decode(),
                'owner_user_id': store.user_id, 'visibility': 'shared'}, ''
    monkeypatch.setattr(genesis.core_envelope, '_build_shared_envelope_for_store', build)
    monkeypatch.setattr(genesis.boot_gates, '_log_bootstrap_event',
                        lambda *a, **kw: events.append('bootstrap'))
    monkeypatch.setattr(identity, '_append_identity_change',
                        lambda *a, **kw: events.append('change'))
    def sealed(source=genesis.GENESIS_SOURCE):
        return {'id': 'identity_old', 'owner_user_id': uid, 'visibility': 'shared',
                'body_ct': 'opaque-original', 'relationship_anchor_source': source,
                'created_at': '2025-01-01'}
    return store, events, sealed


@pytest.mark.parametrize('entry', ['init', 'replace'])
def test_initial_database_read_failure_never_means_absent(lane, monkeypatch, entry):
    store, events, sealed = lane
    before = sealed()
    db.set_blob(store.user_id, 'identity', before)
    real = db.get_blob_strict
    def failed(uid, kind):
        if kind == 'identity':
            raise RuntimeError('synthetic database unavailable')
        return real(uid, kind)
    with monkeypatch.context() as m:
        m.setattr(db, 'get_blob_strict', failed)
        call = genesis.init_identity_if_absent if entry == 'init' else genesis.replace_identity_preserving_anchor
        with pytest.raises(RuntimeError):
            call(store, OUTPUT, 'fixture')
    assert real(store.user_id, 'identity') == before
    assert events == []


@pytest.mark.parametrize('source', [genesis.GENESIS_SOURCE, 'user_explicit'])
@pytest.mark.parametrize('reply,error', [(None, 'enclave_http_502'), ({'identity': None}, ''),
                                      ({'identity': {'decrypt_status': 'failed'}}, '')])
def test_sealed_read_failure_never_builds_defaults(lane, monkeypatch, source, reply, error):
    store, events, sealed = lane
    before = sealed(source)
    db.set_blob(store.user_id, 'identity', before)
    monkeypatch.setattr(genesis.core_enclave, '_enclave_get_json_for_gate', lambda *a, **k: (reply, error))
    result = genesis.init_identity_if_absent(store, OUTPUT, 'fixture')
    assert result == 'identity_plain_unavailable'
    assert db.get_blob_strict(store.user_id, 'identity') == before
    assert events == []
    assert distillation_ledger.terminal_result_for(result) == 'failed'


@pytest.mark.parametrize('initial', ['absent', 'sealed', 'plaintext'])
def test_concurrent_writer_wins_without_false_success(lane, monkeypatch, initial):
    store, events, sealed = lane
    before = sealed()
    if initial == 'plaintext':
        before.pop('body_ct')
        before['body'] = json.dumps({'agent_name': 'Old', 'signature': ['keep']})
    if initial != 'absent':
        db.set_blob(store.user_id, 'identity', before)
    winner = {**sealed(), 'body_ct': 'concurrent-winner'}
    original_build = genesis.core_envelope._build_shared_envelope_for_store
    def build(*args, **kwargs):
        # Real SQL write in the window before this operation's final write.
        db.set_blob(store.user_id, 'identity', winner)
        return original_build(*args, **kwargs)
    monkeypatch.setattr(genesis.core_envelope, '_build_shared_envelope_for_store', build)
    monkeypatch.setattr(genesis.core_enclave, '_enclave_get_json_for_gate',
                        lambda *a, **k: ({'identity': {'agent_name': 'Old', 'signature': ['keep']}}, ''))
    assert genesis.init_identity_if_absent(store, OUTPUT, 'fixture') == 'identity_write_conflict'
    assert db.get_blob_strict(store.user_id, 'identity') == winner
    assert events == []


def test_race_during_enclave_read_cannot_mix_snapshot_and_plaintext(lane, monkeypatch):
    store, events, sealed = lane
    db.set_blob(store.user_id, 'identity', sealed())
    winner = {**sealed(), 'body_ct': 'new-version'}
    def read(*a, **k):
        db.set_blob(store.user_id, 'identity', winner)
        return {'identity': {'agent_name': 'Winner', 'signature': ['new signature']}}, ''
    monkeypatch.setattr(genesis.core_enclave, '_enclave_get_json_for_gate', read)
    assert genesis.init_identity_if_absent(store, OUTPUT, 'fixture') == 'identity_write_conflict'
    assert db.get_blob_strict(store.user_id, 'identity') == winner
    assert not events


@pytest.mark.parametrize('entry', ['init', 'replace'])
def test_legitimate_first_initialization(lane, entry):
    store, events, _ = lane
    call = genesis.init_identity_if_absent if entry == 'init' else genesis.replace_identity_preserving_anchor
    assert call(store, OUTPUT) == 'initialized'
    assert json.loads(db.get_blob_strict(store.user_id, 'identity')['body'])['agent_name'] == 'New'
    assert events == ['bootstrap', 'change']


@pytest.mark.parametrize('source', [genesis.GENESIS_SOURCE, 'user_explicit'])
def test_valid_existing_plaintext_preserves_unaddressed_fields(lane, source):
    store, events, sealed = lane
    before = sealed(source)
    before.pop('body_ct')
    before['body'] = json.dumps({'agent_name': 'Old', 'signature': ['keep'],
                                'self_introduction': 'keep this'})
    db.set_blob(store.user_id, 'identity', before)
    assert genesis.init_identity_if_absent(store, OUTPUT) == 'updated'
    after = db.get_blob_strict(store.user_id, 'identity')
    assert after['id'] == before['id'] and after['created_at'] == before['created_at']
    assert json.loads(after['body'])['signature'] == ['keep']
    assert json.loads(after['body'])['self_introduction'] == 'keep this'
    assert events == ['bootstrap', 'change']


@pytest.mark.parametrize("failure", ["plain_read", "database_read", "write_conflict"])
def test_real_artifact_attempt_records_failed_read(lane, monkeypatch, failure):
    store, events, sealed = lane
    before = sealed()
    db.set_blob(store.user_id, 'identity', before)
    monkeypatch.setattr(genesis.core_enclave, '_enclave_get_json_for_gate', lambda *a, **k: (None, 'unavailable'))
    if failure == "database_read":
        def unavailable(*a):
            raise RuntimeError("synthetic database unavailable")
        monkeypatch.setattr(db, 'get_blob_strict', unavailable)
    elif failure == "write_conflict":
        monkeypatch.setattr(genesis.core_enclave, '_enclave_get_json_for_gate',
                            lambda *a, **k: ({'identity': {'agent_name': 'Old'}}, ''))
        monkeypatch.setattr(db, 'set_blob_if_unchanged', lambda *a, **k: False)
    # Keep the real producer and ArtifactAttempt mapping; intercept ledger storage only.
    monkeypatch.setattr(db, 'genesis_get_job', lambda *a: {'metadata': {'ingest': 'plaintext'}})
    recorded = []
    monkeypatch.setattr(db, 'distillation_start_artifact_attempt', lambda **k: True)
    monkeypatch.setattr(db, 'distillation_finish_artifact_attempt', lambda *a, **k: recorded.append(k))
    for name in ('write_persona_artifact', 'write_voice_artifact'):
        monkeypatch.setattr(genesis, name, lambda *a: ('', ''))
    monkeypatch.setattr(genesis, 'write_profile_artifact', lambda *a: ('', '', 'not_provided'))
    monkeypatch.setattr(db, 'genesis_upsert_output', lambda *a, **k: None)
    if failure == "database_read":
        with pytest.raises(RuntimeError, match="synthetic database unavailable"):
            plaintext_garden._apply_non_memory(store, 'fixture', 'job_fixture', OUTPUT, memory_action_count=0)
        outcome = 'write_failed'
    else:
        result = plaintext_garden._apply_non_memory(store, 'fixture', 'job_fixture', OUTPUT, memory_action_count=0)
        outcome = 'identity_plain_unavailable' if failure == 'plain_read' else 'identity_write_conflict'
        assert result['identity_status'] == outcome
        assert db.get_blob_strict(store.user_id, 'identity') == before
    assert recorded == [{'outcome': outcome, 'terminal_result': 'failed'}]
    assert not events


@pytest.mark.parametrize('value', [{}, [], 'invalid', False])
def test_present_invalid_blob_is_not_absence(lane, monkeypatch, value):
    store, events, _ = lane
    monkeypatch.setattr(db, 'get_blob_strict', lambda *a: value)
    with pytest.raises(ValueError, match='identity_snapshot_invalid'):
        genesis.init_identity_if_absent(store, OUTPUT)
    assert not events


def test_plaintext_uses_one_authoritative_snapshot(lane, monkeypatch):
    store, events, sealed = lane
    before = sealed()
    before.pop('body_ct')
    before['body'] = json.dumps({'agent_name': 'Old', 'signature': ['keep']})
    db.set_blob(store.user_id, 'identity', before)
    real = db.get_blob_strict
    reads = []
    def once(uid, kind):
        if kind == 'identity':
            reads.append(kind)
            assert len(reads) == 1, 'plaintext must use the original snapshot'
        return real(uid, kind)
    monkeypatch.setattr(db, 'get_blob_strict', once)
    assert genesis.init_identity_if_absent(store, OUTPUT) == 'updated'
    assert reads == ['identity']
    assert json.loads(real(store.user_id, 'identity')['body'])['signature'] == ['keep']


def test_write_rejection_does_not_emit_success(lane, monkeypatch):
    store, events, _ = lane
    monkeypatch.setattr(db, 'set_blob_if_unchanged', lambda *a, **k: False)
    result = genesis.init_identity_if_absent(store, OUTPUT)
    assert result == 'identity_write_conflict'
    assert distillation_ledger.terminal_result_for(result) == 'failed'
    assert db.get_blob_strict(store.user_id, 'identity') is None
    assert not events
