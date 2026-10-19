"""Bounded canonical attachment proof; no cache, retry or enclave fallback."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from dataclasses import dataclass

import db
import object_storage
from core import chat_images, plaintext_row
from memory import recall_contract

# Existing accepted-upload limits (hosted.turn); this lower-layer module must
# not import the hosted assembly layer.
IMAGE_BYTES = 2_000_000
FILE_BYTES = 26_214_400
BUNDLE_BYTES = IMAGE_BYTES * chat_images.MAX_CHAT_IMAGES_PER_MESSAGE + 64


@dataclass(frozen=True)
class AttachmentProof:
    user_id: str
    message_id: str
    seq: int
    source_json: str
    body: bytes

    @property
    def source(self):
        return json.loads(self.source_json)


class HistoryPage(list):
    """Private assembly channel; raw storage coordinates never enter the wire."""
    def __init__(self, projected, original):
        super().__init__(projected)
        self.original_json = json.dumps(original, sort_keys=True, ensure_ascii=True)


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise recall_contract.RecallFailure('attachment_deadline')
    return remaining


def _snapshot(user_id, mid, deadline):
    remaining = _remaining(deadline)
    with db.get_pool().connection(timeout=remaining) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            conn.execute("SELECT set_config('statement_timeout', %s, true)",
                         (str(max(1, int(_remaining(deadline) * 1000))),))
            found = conn.execute('SELECT seq, msg_id, doc FROM chat_messages WHERE user_id=%s AND msg_id=%s',
                                 (user_id, mid)).fetchone()
    if not found or not isinstance(found[2], dict):
        raise recall_contract.RecallFailure('attachment_missing')
    seq, actual_id, doc = found
    row = dict(doc)
    row.setdefault('id', str(actual_id))
    row['seq'] = int(seq)
    recall_contract.owner(row, user_id)
    if row['id'] != mid:
        raise recall_contract.RecallFailure('attachment_binding_invalid', category='binding_error')
    return row


def _object_bytes(row, user_id, maximum, deadline):
    # A per-operation client inherits existing configuration but has no retry.
    # Do not mutate the process-wide client/configuration.
    import boto3
    from botocore.config import Config
    key = row.get('body_key')
    size = row.get('body_size_bytes')
    digest = row.get('body_sha256')
    if (row.get('body_object_format') != 'plaintext_v1'
            or not isinstance(key, str) or not object_storage.chat_key_owned_by(key, user_id)
            or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= maximum
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in '0123456789abcdef' for c in digest)):
        raise recall_contract.RecallFailure('attachment_storage_invalid')
    remaining = _remaining(deadline)
    client = boto3.client('s3', endpoint_url=object_storage._endpoint(),
                         aws_access_key_id=object_storage._access_key(),
                         aws_secret_access_key=object_storage._secret_key(), region_name='auto',
                         config=Config(signature_version='s3v4',
                                       retries={'total_max_attempts': 1},
                                       connect_timeout=min(3.0, remaining),
                                       read_timeout=min(10.0, remaining)))
    try:
        response = client.get_object(Bucket=object_storage._chat_files_bucket(), Key=key)
        stream = response['Body']
        try:
            if response.get('ContentLength') != size:
                raise recall_contract.RecallFailure('attachment_storage_invalid')
            _remaining(deadline)
            body = stream.read(size + 1)
        finally:
            stream.close()
    finally:
        client.close()
    _remaining(deadline)
    if len(body) != size or hashlib.sha256(body).hexdigest() != digest:
        raise recall_contract.RecallFailure('attachment_storage_invalid')
    return body


def validate_bytes(row, body):
    ctype = row.get('content_type')
    maximum = BUNDLE_BYTES if row.get('image_bundle_version') else IMAGE_BYTES
    if ctype == 'file':
        maximum = FILE_BYTES
    if ctype not in {'image', 'file'} or not isinstance(body, bytes) or not 0 < len(body) <= maximum:
        raise recall_contract.RecallFailure('attachment_storage_invalid')
    if ctype == 'image':
        allowed = {'image/jpeg', 'image/jpg', 'image/png', 'image/webp', 'image/gif'}
        if row.get('image_bundle_version'):
            if row['image_bundle_version'] != chat_images.CHAT_IMAGE_BUNDLE_VERSION:
                raise recall_contract.RecallFailure('attachment_storage_invalid')
            images = chat_images.decode_image_bundle(body)
            if row.get('image_count') != len(images) or row.get('image_mimes') != [mime for _, mime in images]:
                raise recall_contract.RecallFailure('attachment_storage_invalid')
            if any(len(data) > IMAGE_BYTES for data, _ in images):
                raise recall_contract.RecallFailure('attachment_storage_invalid')
        elif str(row.get('image_mime') or 'image/jpeg') not in allowed:
            raise recall_contract.RecallFailure('attachment_storage_invalid')


def read(snapshot, user_id, deadline):
    """Verify source before/after hydration, returning the bytes consumers use."""
    recall_contract.owner(snapshot, user_id)
    mid = snapshot.get('id')
    if not isinstance(mid, str) or not mid or not isinstance(snapshot.get('seq'), int):
        raise recall_contract.RecallFailure('attachment_binding_invalid', category='binding_error')
    try:
        current = _snapshot(user_id, mid, deadline)
        if current != snapshot:
            raise recall_contract.RecallFailure('attachment_changed', category='binding_error')
        if (snapshot.get('visibility') == 'local_only' or snapshot.get('body_ct')
                or snapshot.get('K_enclave')
                or (snapshot.get('body_key') and snapshot.get('body_object_format') != 'plaintext_v1')):
            raise recall_contract.RecallFailure('attachment_sealed')
        maximum = FILE_BYTES if snapshot.get('content_type') == 'file' else (
            BUNDLE_BYTES if snapshot.get('image_bundle_version') else IMAGE_BYTES)
        if snapshot.get('body_key'):
            body = _object_bytes(snapshot, user_id, maximum, deadline)
        else:
            encoded = snapshot.get('body_b64')
            text = snapshot.get('body')
            if ((isinstance(encoded, str) and len(encoded) > (maximum + 2)//3*4)
                    or (isinstance(text, str) and len(text) > maximum)):
                raise recall_contract.RecallFailure('attachment_storage_invalid')
            body = plaintext_row.read_plaintext_row(snapshot, user_id, ValueError)
        validate_bytes(snapshot, body)
        if _snapshot(user_id, mid, deadline) != snapshot:
            raise recall_contract.RecallFailure('attachment_changed', category='binding_error')
        _remaining(deadline)
        return AttachmentProof(user_id, mid, snapshot['seq'],
                               json.dumps(snapshot, sort_keys=True, ensure_ascii=True), body)
    except recall_contract.RecallFailure:
        raise
    except Exception as exc:
        raise recall_contract.RecallFailure('attachment_read_failed') from exc


def image_payload(proof, user_id, message_id):
    if (not isinstance(proof, AttachmentProof) or proof.user_id != user_id
            or proof.message_id != message_id or proof.source.get('content_type') != 'image'):
        raise recall_contract.RecallFailure('attachment_binding_invalid', category='binding_error')
    source = proof.source
    if source.get('image_bundle_version'):
        return {'images': [{'image_b64': base64.b64encode(body).decode('ascii'), 'image_mime': mime}
                           for body, mime in chat_images.decode_image_bundle(proof.body)]}
    return {'image_b64': base64.b64encode(proof.body).decode('ascii'),
            'image_mime': source.get('image_mime') or 'image/jpeg'}
