"""Real PostgreSQL + fake model: the actual worker scan and vector lifecycle."""
import json
import logging
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import pytest
import db
from accounts import registry
from core import store as core_store
from memory.embedding import fake, sweep
from model_api_runtime.v2 import serve_worker


@pytest.fixture()
def garden(monkeypatch):
    registry.load_users()
    uid = registry._register_user()["user_id"]
    encoder = fake.FakeEmbedder()
    monkeypatch.setenv("FEEDLING_MEMORY_EMBEDDING_ENABLED", "1")
    monkeypatch.setattr(sweep, "_embedder", encoder)
    monkeypatch.setattr(sweep, "_unavailable_logged", False)
    return uid, encoder


def card(uid, mid, text="garden plant", **extra):
    return {"id": mid, "owner_user_id": uid, "visibility": "shared", "status": "active",
            "body": json.dumps({"summary": text, "content": text}), **extra}


def write(uid, mid, text="garden plant", **extra):
    assert db.memory_upsert(uid, mid, "2026-09-19", card(uid, mid, text, **extra))


def tick(uid):
    return serve_worker._tick_embedding_for_user(uid)


def test_disabled_never_constructs_or_loads_store(monkeypatch):
    monkeypatch.delenv("FEEDLING_MEMORY_EMBEDDING_ENABLED", raising=False)
    def forbidden(*a, **kw):
        raise AssertionError("disabled path must be inert")
    monkeypatch.setattr(sweep, "get_embedder", forbidden)
    monkeypatch.setattr(core_store, "get_store_per_load_mode", forbidden)
    assert tick("unknown") == 0


def test_first_scan_idempotence_and_changed_text(garden):
    uid, encoder = garden
    write(uid, "a"); write(uid, "b", "train ticket")
    assert tick(uid) == 2
    before = db.memory_vectors_load(uid, encoder.model_id)
    assert tick(uid) == 0
    write(uid, "a", "new fact")
    assert tick(uid) == 1
    after = db.memory_vectors_load(uid, encoder.model_id)
    assert before["b"] == after["b"]
    assert before["a"][0] != after["a"][0]


@pytest.mark.parametrize("bad_value", [0.0, float("nan"), float("inf")])
def test_corrupt_derived_vector_is_rebuilt(garden, caplog, bad_value):
    uid, encoder = garden
    write(uid, "bad"); write(uid, "good")
    assert tick(uid) == 2
    expected = db.memory_vectors_load(uid, encoder.model_id)
    blob = struct.pack("<" + "f" * encoder.dim, *([bad_value] * encoder.dim))
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE memory_vectors SET vector=%s WHERE user_id=%s AND moment_id='bad'",
                     (blob, uid))
    with caplog.at_level(logging.WARNING):
        assert tick(uid) == 1
    assert "invalid_vectors=1" in caplog.text
    assert "sweep_failed" not in caplog.text
    assert db.memory_vectors_load(uid, encoder.model_id) == expected


def test_embedding_store_failure_preserves_capture_dream(garden, monkeypatch, caplog):
    uid, _ = garden
    calls = []
    monkeypatch.setattr(serve_worker, "_CAPTURE_ENABLED", True)
    monkeypatch.setattr(serve_worker, "_DREAM_ENABLED", True)
    monkeypatch.setattr(serve_worker, "_tick_capture_for_user", lambda _: calls.append("capture") or 1)
    monkeypatch.setattr(serve_worker, "_tick_dream_for_user", lambda _: calls.append("dream") or 1)
    def failed(*args, **kwargs):
        calls.append("embedding")
        raise RuntimeError("private store error marker")
    monkeypatch.setattr(core_store, "get_store_per_load_mode", failed)
    with caplog.at_level(logging.WARNING):
        assert serve_worker._tick_extraction_for_user(uid) == 2
    assert calls == ["capture", "dream", "embedding"]
    assert "unavailable_reason=entry_failed" in caplog.text
    assert "private store error marker" not in caplog.text


def test_delete_retire_local_only_and_encryption_prune(garden, caplog):
    uid, encoder = garden
    for mid in ("delete", "retire", "private", "encrypted"):
        write(uid, mid)
    assert tick(uid) == 4
    db.memory_delete(uid, "delete")
    write(uid, "retire", status="superseded")
    write(uid, "private", visibility="local_only")
    write(uid, "encrypted", body=None, body_ct="secret", K_enclave="key")
    with caplog.at_level(logging.INFO, logger=sweep.log.name):
        assert tick(uid) == 0
    assert db.memory_vectors_load(uid, encoder.model_id) == {}
    assert "skipped_encrypted=1" in caplog.text
    assert "pruned=4" in caplog.text
    assert "secret" not in caplog.text


def test_limit_33_cards_in_two_ticks(garden):
    uid, encoder = garden
    db.memory_replace_all(uid, [card(uid, str(i)) for i in range(33)])
    assert tick(uid) == 32
    assert tick(uid) == 1
    assert len(db.memory_vectors_load(uid, encoder.model_id)) == 33


def test_unavailable_logged_once(garden, caplog):
    uid, encoder = garden
    encoder.available = False
    encoder.unavailable_reason = "model_files_missing"
    with caplog.at_level(logging.WARNING, logger=sweep.log.name):
        assert tick(uid) == tick(uid) == 0
    assert caplog.text.count("unavailable_reason=model_files_missing") == 1


def test_stale_inflight_deleted_card_not_restored(garden, monkeypatch):
    uid, encoder = garden
    write(uid, "a")
    encode = encoder.encode_passages
    def during(texts):
        db.memory_delete(uid, "a")
        return encode(texts)
    monkeypatch.setattr(encoder, "encode_passages", during)
    assert tick(uid) == 0
    assert db.memory_vectors_load(uid, encoder.model_id) == {}


def test_stale_inflight_edit_retried_next_tick(garden, monkeypatch):
    uid, encoder = garden
    write(uid, "a")
    encode = encoder.encode_passages
    def during(texts):
        write(uid, "a", "changed while encoding")
        return encode(texts)
    monkeypatch.setattr(encoder, "encode_passages", during)
    assert tick(uid) == 0
    monkeypatch.setattr(encoder, "encode_passages", encode)
    assert tick(uid) == 1


def test_cross_user_and_model_isolation(garden):
    uid, encoder = garden
    uid2 = registry._register_user()["user_id"]
    write(uid, "a"); write(uid2, "a", "other user")
    assert tick(uid) == tick(uid2) == 1
    second = db.memory_vectors_load(uid2, encoder.model_id)
    db.memory_vectors_upsert(uid, "other-model", [("a", "a"*16, [1., 0.])])
    assert db.memory_vectors_prune(uid, encoder.model_id, []) == 1
    assert db.memory_vectors_load(uid2, encoder.model_id) == second
    assert db.memory_vectors_load(uid, "other-model")["a"] == ("a"*16, [1., 0.])


def test_account_delete_purges_vectors(garden):
    uid, encoder = garden
    write(uid, "a"); assert tick(uid) == 1
    assert db.delete_user(uid)
    assert db.memory_vectors_load(uid, encoder.model_id) == {}
    with pytest.raises(ValueError, match="user_missing"):
        db.memory_vectors_upsert(uid, encoder.model_id, [("a", "a"*16, [1., 0.])])


def test_invalid_batch_is_atomic(garden):
    uid, encoder = garden
    with pytest.raises(ValueError):
        db.memory_vectors_upsert(uid, encoder.model_id,
            [("a", "a"*16, [1., 0.]), ("b", "b"*16, [float('nan'), 1.])])
    assert db.memory_vectors_load(uid, encoder.model_id) == {}


def test_scheduler_enrolls_embedding_with_capture_dream_off(garden, monkeypatch):
    uid, encoder = garden
    monkeypatch.setattr(serve_worker, "_CAPTURE_ENABLED", False)
    monkeypatch.setattr(serve_worker, "_DREAM_ENABLED", False)
    mode = serve_worker.hosted_config_store.HOSTED_RUNTIME_MODE_DB_ACTION_V2
    monkeypatch.setattr(serve_worker.admin_core, "list_runtime_modes", lambda: {mode: [uid]})
    deps = serve_worker._build_scheduler_deps()
    assert deps.extraction_users() == [uid]
    write(uid, "a")
    deps.tick_extraction(uid)
    assert "a" in db.memory_vectors_load(uid, encoder.model_id)


def test_account_reset_purges_vectors(garden):
    uid, encoder = garden
    write(uid, "a"); assert tick(uid) == 1
    db.delete_user_data(uid)
    assert db.memory_vectors_load(uid, encoder.model_id) == {}


def test_failed_inference_does_not_leak_content(garden, monkeypatch, caplog):
    uid, encoder = garden
    write(uid, "a", "private content marker")
    def failed(texts):
        raise RuntimeError(texts[0])
    monkeypatch.setattr(encoder, "encode_passages", failed)
    with caplog.at_level(logging.WARNING, logger=sweep.log.name):
        assert tick(uid) == 0
    assert "private content marker" not in caplog.text
    assert "unavailable_reason=sweep_failed" in caplog.text


def test_cues_and_legacy_shapes_are_projected(garden):
    uid, encoder = garden
    old = card(uid, "old")
    old['body'] = {'title':'old title','description':'old description','retrieval_cues':['unique cue']}
    assert db.memory_upsert(uid, 'old', '', old)
    wanted, _ = sweep._eligible(db.memory_load_strict(uid), uid)
    assert 'old description' in wanted['old'][1]
    assert 'unique cue' in wanted['old'][1]
    assert tick(uid) == 1


def test_storage_is_float32_little_endian(garden):
    uid, encoder = garden
    db.memory_vectors_upsert(uid, encoder.model_id, [('a','a'*16,[1.0,0.0])])
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT dim,encode(vector,'hex') FROM memory_vectors WHERE user_id=%s",
                            (uid,)).fetchone() == (2,'0000803f00000000')
