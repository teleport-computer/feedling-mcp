"""Required turn proof and sealed projection, with enclave calls forbidden."""
from pathlib import Path
import copy
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'backend'))
from memory import recall_contract, recall_projection
from model_api_runtime.v2 import serve_worker

UID = 'recall-projection-owner'


def row(**extra):
    return dict(id='current', owner_user_id=UID, seq=1, role='user', ts=1,
                v=1, **extra)


@pytest.fixture
def off_reader(monkeypatch):
    monkeypatch.setattr(serve_worker.accounts_registry, 'effective_content_encryption', lambda _: 'off')
    monkeypatch.setattr(serve_worker, '_mint_runtime_token', lambda _: 'synthetic-unused')
    def forbidden(*args, **kwargs):
        pytest.fail('effective-off projection attempted an envelope/enclave read')
    monkeypatch.setattr(serve_worker.core_envelope, 'read_envelope_body', forbidden)
    return serve_worker._decrypt_chat_rows_inner


def test_sealed_body_keeps_independent_caption_and_no_body_derived_metadata(off_reader):
    source = row(content_type='image', body_ct='sealed', K_enclave='sealed-key',
                 body='stale plaintext', caption_body='the current question',
                 quoted_memory_ids='secret-card', vision_route_id='sealed-route',
                 content='stale decoded text')
    original = copy.deepcopy(source)
    proof = recall_projection.turn([source], UID, 1, 'current')
    projected = proof.projected[0]
    assert projected['content'] == 'the current question'
    assert 'quoted_memory_ids' not in projected
    assert 'vision_route_id' not in projected
    result = off_reader(UID, [source], user_only=True)
    assert result[0]['content'] == 'the current question'
    assert not result[0].get('unreadable')
    assert 'quoted_memory_ids' not in result[0]
    assert not result[0].get('has_image')
    recall_contract.validate_prompt_rows(result, UID, 1, required_ids=['current'])
    assert source == original


@pytest.mark.parametrize('extra', [dict(caption_body_ct='sealed-caption'), {},
                                    dict(visibility='local_only', caption_body='hidden')])
def test_sealed_current_without_readable_caption_is_hard(off_reader, extra):
    source = row(body_ct='sealed', **extra)
    with pytest.raises(recall_contract.RecallFailure, match='unreadable_current_message'):
        recall_projection.turn([source], UID, 1)
    with pytest.raises(recall_contract.RecallFailure, match='unreadable_current_message'):
        off_reader(UID, [source], user_only=True)


def test_historical_sealed_row_has_no_reference_or_media_route(off_reader):
    source = row(body_ct='sealed', quoted_memory_ids='must-not-expand',
                 content_type='image', vision_route_id='must-not-fetch')
    result = off_reader(UID, [source], user_only=False, preserve_unreadable=True)
    assert result[0]['unreadable']
    assert result[0]['content'] == ''
    assert 'quoted_memory_ids' not in result[0]
    assert 'vision_route_id' not in result[0]
    recall_contract.validate_prompt_rows(result, UID, 1)


@pytest.mark.parametrize('extra', [dict(owner_user_id='foreign'),
    dict(caption_body='caption', caption_owner_user_id='foreign'),
    dict(caption_body='\ud800')])
def test_exclusion_cannot_hide_invalid_owner_or_plain_caption(extra):
    source = row(body_ct='sealed')
    source.update(extra)
    with pytest.raises(recall_contract.RecallFailure):
        recall_projection.turn([source], UID, 1)


def test_row_proof_rejects_modified_prompt_content(off_reader):
    result = off_reader(UID, [row(body_ct='sealed', caption_body='valid')], user_only=True)
    result[0]['content'] = 'different prompt'
    with pytest.raises(recall_contract.RecallFailure, match='turn_content_unconfirmed'):
        recall_contract.validate_prompt_rows(result, UID, 1, required_ids=['current'])


@pytest.mark.parametrize('mode', ['on', 'legacy-unknown'])
def test_operation_mode_change_cannot_route_off_turn_to_enclave(monkeypatch, mode):
    token = recall_contract.bind_operation_mode(UID, 'off')
    monkeypatch.setattr(serve_worker.accounts_registry, 'effective_content_encryption', lambda _: mode)
    monkeypatch.setattr(serve_worker, '_read_context_memories_enclave',
                        lambda *a, **k: pytest.fail('mode drift reached enclave'))
    try:
        with pytest.raises(recall_contract.RecallFailure, match='account_mode_changed'):
            serve_worker._read_context_memories(UID, through_seq=1)
        with pytest.raises(recall_contract.RecallFailure, match='account_mode_changed'):
            serve_worker._decrypt_chat_rows_inner(UID, [row(body='valid')], user_only=True)
    finally:
        recall_contract.reset_operation_mode(token)
    assert recall_contract.checked_operation_mode(UID, mode) == ('on' if mode == 'on' else 'unknown')


def test_off_worker_rejects_missing_binding_instead_of_empty_context():
    import asyncio
    from model_api_runtime.v2 import worker
    token = recall_contract.bind_operation_mode(UID, 'off')
    try:
        deps = worker.TurnDeps(read_messages=lambda _: [], resolve_provider=lambda _: None,
                               mint_enclave_token=lambda _: "synthetic",
                               read_context_memories=lambda *a, **k: {})
        with pytest.raises(recall_contract.RecallFailure, match='recall_binding_invalid'):
            asyncio.run(worker._load_turn_memory_context(deps, UID, 1, asyncio.Semaphore(1)))
    finally:
        recall_contract.reset_operation_mode(token)


def test_duplicate_prompt_identity_cannot_mix_source_versions():
    proof = recall_contract.TurnRowProof(UID, 'current', 1, 'question')
    first = dict(id='current', seq=1, content='question', _recall_row_proof=proof, _recall_source_json='one')
    second = {**first, '_recall_source_json':'two'}
    with pytest.raises(recall_contract.RecallFailure, match='turn_version_changed'):
        recall_contract.validate_prompt_rows([first, second], UID, 1)
