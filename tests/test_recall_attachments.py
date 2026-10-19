"""Canonical required attachment validation; synthetic DB and object bytes only."""
from pathlib import Path
import base64
import copy
import sys
import time

import pytest
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).parent.parent / 'backend'))
import db
from accounts import registry
from memory import recall_attachments as attachments, recall_contract, recall_projection


@pytest.fixture
def source():
    registry.load_users()
    uid = registry._register_user()['user_id']
    row = dict(id='image', owner_user_id=uid, role='user', ts=1.0, v=1,
               content_type='image', image_mime='image/png', body_b64=base64.b64encode(b'synthetic-image').decode())
    with db.get_pool().connection() as conn:
        seq = conn.execute('INSERT INTO chat_messages (user_id,msg_id,ts,doc) VALUES (%s,%s,%s,%s) RETURNING seq',
                           (uid,row['id'],row['ts'],Jsonb(row))).fetchone()[0]
    row['seq'] = seq
    return row


def test_image_only_canonical_proof_does_not_change_query_or_page(source):
    original = copy.deepcopy(source)
    public = {key:value for key,value in source.items() if key != 'body_b64'}
    public.update(body_omitted=True, body_omitted_reason='image')
    page = attachments.HistoryPage([public], [source])
    proofs=[]
    def read(row):
        proof = attachments.read(row, source['owner_user_id'], time.monotonic()+2)
        proofs.append(proof)
        return proof
    result = recall_projection.turn(page, source['owner_user_id'], source['seq'], attachment_reader=read)
    assert result.projected[0]['content'] == ''
    assert result.original == [public]
    assert result.message_id == source['id']
    assert proofs[0].body == b'synthetic-image'
    assert source == original
    assert attachments.image_payload(proofs[0], source['owner_user_id'], source['id'])['image_b64'] == source['body_b64']


@pytest.mark.parametrize('fault', ['missing','foreign','seq','changed','sealed','oversize','invalid_b64'])
def test_attachment_fault_is_hard(source, fault):
    uid = source['owner_user_id']
    if fault == 'missing':
        with db.get_pool().connection() as conn:
            conn.execute('DELETE FROM chat_messages WHERE user_id=%s',(uid,))
    elif fault == 'foreign':
        source['owner_user_id'] = 'foreign'
    elif fault == 'seq':
        source['seq'] += 1
    elif fault == 'changed':
        source['caption_body'] = 'different'
    else:
        if fault == 'sealed':
            source['body_ct'] = 'sealed'
        elif fault == 'oversize':
            source['body_b64'] = 'A'*(attachments.IMAGE_BYTES*2)
        else:
            source['body_b64'] = '!'
        with db.get_pool().connection() as conn:
            conn.execute('UPDATE chat_messages SET doc=%s WHERE user_id=%s',
                         (Jsonb({k:v for k,v in source.items() if k != 'seq'}),uid))
    with pytest.raises(recall_contract.RecallFailure):
        attachments.read(source, uid, time.monotonic()+2)


def test_changed_source_during_object_read_is_hard(source, monkeypatch):
    source.pop('body_b64')
    source.update(body_key='synthetic-key',body_object_format='plaintext_v1')
    uid=source['owner_user_id']
    with db.get_pool().connection() as conn:
        conn.execute('UPDATE chat_messages SET doc=%s WHERE user_id=%s',
                     (Jsonb({k:v for k,v in source.items() if k != 'seq'}),uid))
    def read(*args):
        with db.get_pool().connection() as conn:
            conn.execute("UPDATE chat_messages SET doc=jsonb_set(doc,'{caption_body}',%s) WHERE user_id=%s",
                         (Jsonb('changed during read'),uid))
        return b'synthetic-image'
    monkeypatch.setattr(attachments,'_object_bytes',read)
    with pytest.raises(recall_contract.RecallFailure, match='attachment_changed'):
        attachments.read(source,uid,time.monotonic()+2)


def test_expired_attachment_never_reads_storage(source,monkeypatch):
    monkeypatch.setattr(db,'get_pool',lambda:pytest.fail('expired proof read storage'))
    with pytest.raises(recall_contract.RecallFailure,match='attachment_deadline'):
        attachments.read(source,source['owner_user_id'],time.monotonic()-1)


@pytest.mark.parametrize('fault', ['none', 'length', 'digest', 'oversized_metadata'])
def test_object_reader_is_bounded_and_never_retries(source, monkeypatch, fault):
    import boto3
    import hashlib
    import io
    uid=source['owner_user_id']
    body=b'synthetic-image'
    source.pop('body_b64')
    source.update(body_key='synthetic-owned-key',body_object_format='plaintext_v1',
                  body_size_bytes=len(body),body_sha256=hashlib.sha256(body).hexdigest())
    if fault == 'oversized_metadata':
        source['body_size_bytes']=attachments.IMAGE_BYTES+1
    if fault == 'digest':
        source['body_sha256']='0'*64
    with db.get_pool().connection() as conn:
        conn.execute('UPDATE chat_messages SET doc=%s WHERE user_id=%s',
                     (Jsonb({k:v for k,v in source.items() if k != 'seq'}),uid))
    calls=[]
    class Stream(io.BytesIO):
        def read(self, amount=-1):
            calls.append(('read',amount))
            return super().read(amount)
    class Client:
        def get_object(self, **kwargs):
            calls.append(('get',kwargs))
            return {'ContentLength': len(body)+(1 if fault=='length' else 0), 'Body':Stream(body)}
        def close(self):
            calls.append(('close',))
    def make_client(*args, **kwargs):
        assert kwargs['config'].retries == {'total_max_attempts':1}
        assert 0 < kwargs['config'].read_timeout <= 2
        return Client()
    monkeypatch.setattr(boto3,'client',make_client)
    monkeypatch.setattr(attachments.object_storage,'chat_key_owned_by',lambda key,user: key=='synthetic-owned-key' and user==uid)
    if fault == 'none':
        proof=attachments.read(source,uid,time.monotonic()+2)
        assert proof.body == body
        assert ('read',len(body)+1) in calls
    else:
        with pytest.raises(recall_contract.RecallFailure):
            attachments.read(source,uid,time.monotonic()+2)
    assert len([c for c in calls if c[0]=='get']) == (0 if fault=='oversized_metadata' else 1)


def test_attachment_timeout_retains_permit_until_reader_finishes(source, monkeypatch):
    import threading
    from memory import recall_operation
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    permit = threading.BoundedSemaphore(1)
    original = attachments._snapshot
    def blocked(*args):
        entered.set()
        release.wait(2)
        try:
            return original(*args)
        finally:
            finished.set()
    monkeypatch.setattr(attachments, '_snapshot', blocked)
    deadline = time.monotonic()+0.03
    try:
        with pytest.raises(recall_contract.RecallFailure, match='input_read_unconfirmed'):
            recall_operation._stage(lambda: attachments.read(source, source['owner_user_id'], deadline),
                                    deadline, permit)
        assert entered.is_set()
        assert not permit.acquire(blocking=False)
    finally:
        release.set()
    assert finished.wait(2)
    assert permit.acquire(timeout=2)
    permit.release()
