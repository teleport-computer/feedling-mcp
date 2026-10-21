"""Real send/PG/offload/worker recall timing, with moto and synthetic images."""
import asyncio
import base64
import json
import time
from dataclasses import replace

import boto3
import pytest
from moto import mock_aws

from test_asgi_hosted_chat_send import (
    env, _register, _setup, _asgi_post, _REAL_SHARED_ENVELOPE_BUILDER,
)
import db
from core import envelope
from memory import recall_attachments, recall_contract
from model_api_runtime.v2 import serve_worker, worker
from model_api_runtime.v2 import jobs_store
from test_v2_worker import _deps, _script_provider, _text_round, _BYOK
from test_recall_attachments import source
import object_storage
from psycopg.types.json import Jsonb


@pytest.mark.parametrize("phase", ["before_proof", "during_proof", "before_selection", "before_selection_redirect"])
@pytest.mark.parametrize("execution", ["recall", "job"])
def test_real_send_worker_accepts_only_same_body_offload(env, phase, execution):
    monkeypatch = env
    uid, key = _register()
    _setup(key, monkeypatch)
    monkeypatch.setattr(envelope, "_build_shared_envelope_for_store", _REAL_SHARED_ENVELOPE_BUILDER)
    monkeypatch.setattr(envelope, "resolve_content_encryption", lambda _: "off")
    monkeypatch.setattr(serve_worker.accounts_registry, "effective_content_encryption", lambda _: "off")
    monkeypatch.setenv("FEEDLING_V2_PLAINTEXT_RECALL", "off")
    monkeypatch.setenv("R2_ENDPOINT", "https://s3.amazonaws.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "synthetic")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "synthetic")
    monkeypatch.setenv("R2_CHAT_FILES_BUCKET", "synthetic-chat")
    pending = []
    real_offload = db._offload_chat_body_after_commit
    monkeypatch.setattr(db, "_offload_chat_body_after_commit", lambda *a: pending.append(a))
    images = [b"\xff\xd8\xff-synthetic-one", b"\xff\xd8\xff-synthetic-two"]
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="synthetic-chat")
        status, result = _asgi_post({"message": "compare", "images": [
            {"image_b64": base64.b64encode(raw).decode(), "image_mime": "image/jpeg"}
            for raw in images]}, key)
        assert status == 202, result
        assert len(pending) == 1
        with db.get_pool().connection() as conn:
            job = conn.execute("SELECT id FROM agent_jobs WHERE user_id=%s", (uid,)).fetchone()
        assert job is not None  # queue already visible while upload is pending
        rows = serve_worker._read_tail(uid, 0, 10)
        target = next(row for row in rows if row.get("role") == "user")
        source = json.loads(target["_recall_source_json"])
        assert "body_b64" in source and "body_key" not in source

        def promote():
            real_offload(*pending.pop())
            current = recall_attachments._snapshot(uid, target["id"], time.monotonic()+3)
            assert current.get("body_key") and "body_b64" not in current

        snapshot = recall_attachments._snapshot
        if phase == "before_proof":
            promote()
        elif phase in {"during_proof", "before_selection_redirect"}:
            def read_then_promote(*args):
                row = snapshot(*args)
                monkeypatch.setattr(recall_attachments, "_snapshot", snapshot)
                promote()
                return row
            monkeypatch.setattr(recall_attachments, "_snapshot", read_then_promote)

        def select(*args, **kwargs):
            if phase == "before_selection":
                promote()
            elif phase == "before_selection_redirect":
                current = snapshot(uid, target["id"], time.monotonic()+3)
                old_key = current["body_key"]
                current["body_key"] += "/different"
                boto3.client("s3", region_name="us-east-1").copy_object(
                    Bucket="synthetic-chat", Key=current["body_key"],
                    CopySource={"Bucket": "synthetic-chat", "Key": old_key})
                with db.get_pool().connection() as conn:
                    conn.execute("UPDATE chat_messages SET doc=%s WHERE user_id=%s AND msg_id=%s",
                                 (Jsonb({k: v for k, v in current.items() if k != "seq"}), uid, target["id"]))
            return serve_worker._read_context_memories(*args, **kwargs)

        deps = worker.TurnDeps(read_messages=lambda _: [], resolve_provider=lambda _: None,
                              mint_enclave_token=lambda _: "synthetic",
                              prepare_recall_attachments=serve_worker._prepare_recall_attachments,
                              read_context_memories=select)
        token = recall_contract.bind_operation_mode(uid, "off")
        try:
            if execution == "recall":
                if phase == "before_selection_redirect":
                    with pytest.raises(recall_contract.RecallFailure, match="attachment_changed"):
                        asyncio.run(worker._load_turn_memory_context(
                            deps, uid, target["seq"], asyncio.Semaphore(1),
                            required_rows=[target], required_ids=[target["id"]]))
                    return
                payload = asyncio.run(worker._load_turn_memory_context(
                    deps, uid, target["seq"], asyncio.Semaphore(1),
                    required_rows=[target], required_ids=[target["id"]]))
            else:
                monkeypatch.setenv("FEEDLING_V2_SELF_THINKING", "off")
                monkeypatch.setattr(worker, "_PROFILE_ENABLED", False)
                calls = _script_provider(monkeypatch, [_text_round("synthetic photo reply")])
                errors = []
                safe_code = worker._safe_failure_code
                def record_error(code, exc):
                    errors.append((type(exc).__name__, str(exc)))
                    return safe_code(code, exc)
                monkeypatch.setattr(worker, "_safe_failure_code", record_error)
                with db.get_pool().connection() as conn:
                    conn.execute("UPDATE agent_jobs SET priority=1000000 WHERE id=%s", (job[0],))
                claimed = jobs_store.claim_next_job("synthetic-photo-worker", lanes={"chat"})
                assert claimed["id"] == job[0]
                full_deps = replace(_deps(messages=rows),
                                    read_messages=serve_worker._read_messages,
                                    read_messages_after_seq=serve_worker._read_messages_after_seq,
                                    read_tail=serve_worker._read_tail,
                                    read_tail_after_seq=serve_worker._read_tail_after_seq,
                                    read_summary_with_seq=serve_worker._read_summary_with_seq,
                                    read_recall_mode=serve_worker._read_recall_mode,
                                    prepare_recall_attachments=serve_worker._prepare_recall_attachments,
                                    read_context_memories=select,
                                    apply_pending_effects=serve_worker._apply_pending_effects_for_user,
                                    read_images=serve_worker._read_images)
                outcome = asyncio.run(worker.process_job(
                    claimed, full_deps, provider_config=_BYOK, api_key=None, runtime_token="synthetic"))
                with db.get_pool().connection() as conn:
                    actual = conn.execute("SELECT status,last_error FROM agent_jobs WHERE id=%s", (job[0],)).fetchone()
                if phase == "before_selection_redirect":
                    assert (outcome, actual[0], actual[1]) == ("failed", "failed", "turn_failed:recall_binding_failed")
                    assert len(calls) == 0
                    return
                assert (outcome, actual[0]) == ("completed", "completed"), (actual, errors)
                assert len(calls) == 1
                with db.get_pool().connection() as conn:
                    replies = conn.execute(
                        "SELECT doc FROM chat_messages WHERE user_id=%s AND doc->>'reply_to_message_id'=%s",
                        (uid, target["id"])).fetchall()
                assert len(replies) == 1
                serialized = json.dumps(calls[0]["messages"])
                for raw in images:
                    assert base64.b64encode(raw).decode() in serialized
                return
        finally:
            recall_contract.reset_operation_mode(token)
        assert payload["category"] == "recall_unavailable"
        proof = target["_recall_attachment_proof"]
        native = recall_attachments.image_payload(proof, uid, target["id"])
        assert [base64.b64decode(x["image_b64"]) for x in native["images"]] == images


@pytest.mark.parametrize("fault", [
    "caption", "mime", "owner", "visibility", "seq", "delete", "replace",
    "missing_object", "changed_object", "claimed_digest", "claimed_size",
    "foreign_key", "pointer_redirect", "sealed",
])
def test_offload_promotion_does_not_allow_other_source_changes(source, monkeypatch, fault):
    uid, mid = source["owner_user_id"], source["id"]
    monkeypatch.setenv("R2_ENDPOINT", "https://s3.amazonaws.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "synthetic")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "synthetic")
    monkeypatch.setenv("R2_CHAT_FILES_BUCKET", "synthetic-chat")
    monkeypatch.setattr(object_storage, "_cached_client", None)
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="synthetic-chat")
        doc = {k: v for k, v in source.items() if k != "seq"}
        db._offload_chat_body_after_commit(uid, mid, doc, 0)
        current = recall_attachments._snapshot(uid, mid, time.monotonic()+3)
        assert current.get("body_key")
        key = current["body_key"]
        expected = source
        if fault == "pointer_redirect":
            expected = dict(current)
            current["body_key"] = key + "/different"
            s3.put_object(Bucket="synthetic-chat", Key=current["body_key"], Body=b"synthetic-image")
        elif fault in {"missing_object", "changed_object"}:
            if fault == "missing_object":
                s3.delete_object(Bucket="synthetic-chat", Key=key)
            else:
                s3.put_object(Bucket="synthetic-chat", Key=key, Body=b"changed-image")
        elif fault == "delete":
            with db.get_pool().connection() as conn:
                conn.execute("DELETE FROM chat_messages WHERE user_id=%s AND msg_id=%s", (uid, mid))
        elif fault == "seq":
            expected = {**source, "seq": source["seq"]+1}
        else:
            changes = {
                "caption": {"caption_body": "changed caption"},
                "mime": {"image_mime": "image/webp"},
                "owner": {"owner_user_id": "foreign"},
                "visibility": {"visibility": "local_only"},
                "replace": {"body_b64": base64.b64encode(b"replacement").decode()},
                "claimed_digest": {"body_sha256": "0"*64},
                "claimed_size": {"body_size_bytes": 1},
                "foreign_key": {"body_key": "chat-images/foreign/file"},
                "sealed": {"body_ct": "sealed", "K_enclave": "sealed"},
            }
            current.update(changes[fault])
        if fault != "delete":
            with db.get_pool().connection() as conn:
                conn.execute("UPDATE chat_messages SET doc=%s WHERE user_id=%s AND msg_id=%s",
                             (Jsonb({k: v for k, v in current.items() if k != "seq"}), uid, mid))
        with pytest.raises(recall_contract.RecallFailure):
            recall_attachments.read(expected, uid, time.monotonic()+3)


@pytest.mark.parametrize("fault", [None, "missing", "corrupt", "change_during_read", "redirect_after_validation"])
def test_final_snapshot_promotion_verifies_actual_object(source, monkeypatch, fault):
    uid, mid = source["owner_user_id"], source["id"]
    monkeypatch.setenv("R2_ENDPOINT", "https://s3.amazonaws.com")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "synthetic")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "synthetic")
    monkeypatch.setenv("R2_CHAT_FILES_BUCKET", "synthetic-chat")
    monkeypatch.setattr(object_storage, "_cached_client", None)
    original_snapshot = recall_attachments._snapshot
    reads = []
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="synthetic-chat")
        def snapshot(*args):
            reads.append(1)
            promotion_read = 2 if fault == "redirect_after_validation" else 3
            if len(reads) == promotion_read:
                db._offload_chat_body_after_commit(uid, mid, {k: v for k, v in source.items() if k != "seq"}, 0)
                actual = original_snapshot(*args)
                key = actual["body_key"]
                if fault == "missing":
                    s3.delete_object(Bucket="synthetic-chat", Key=key)
                elif fault == "corrupt":
                    s3.put_object(Bucket="synthetic-chat", Key=key, Body=b"corrupt")
            if len(reads) == 3 and fault == "redirect_after_validation":
                actual = original_snapshot(*args)
                actual["body_key"] += "/different"
                s3.put_object(Bucket="synthetic-chat", Key=actual["body_key"], Body=b"synthetic-image")
                with db.get_pool().connection() as conn:
                    conn.execute("UPDATE chat_messages SET doc=%s WHERE user_id=%s AND msg_id=%s",
                                 (Jsonb({k: v for k, v in actual.items() if k != "seq"}), uid, mid))
            return original_snapshot(*args)
        monkeypatch.setattr(recall_attachments, "_snapshot", snapshot)
        real_object_bytes = recall_attachments._object_bytes
        def object_bytes(*args):
            raw = real_object_bytes(*args)
            if fault == "change_during_read":
                with db.get_pool().connection() as conn:
                    conn.execute("UPDATE chat_messages SET doc=jsonb_set(doc,'{caption_body}',%s) WHERE user_id=%s AND msg_id=%s",
                                 (Jsonb("different caption"), uid, mid))
            return raw
        monkeypatch.setattr(recall_attachments, "_object_bytes", object_bytes)
        if fault is None:
            proof = recall_attachments.read(source, uid, time.monotonic()+3)
            assert proof.body == b"synthetic-image"
        else:
            with pytest.raises(recall_contract.RecallFailure):
                recall_attachments.read(source, uid, time.monotonic()+3)
