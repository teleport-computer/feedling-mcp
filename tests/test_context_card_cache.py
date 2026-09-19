"""Tests for the /v1/chat/history context-memory decrypt cache
(readside.moments_to_cards_cached).

Uses the real enclave crypto (content_encryption + envelope) and a real nacl content_sk;
no build_app / jieba needed. Regression guard for the 09-15 incident: the enclave was
re-decrypting the whole memory pool on every history load.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import nacl.public  # noqa: E402
import content_encryption as ce  # noqa: E402
from enclave import readside, envelope  # noqa: E402

UID = "usr_cache_test"


@pytest.fixture
def keys():
    content_sk = nacl.public.PrivateKey.generate()
    return {
        "content_sk": content_sk,
        "enclave_pk": bytes(content_sk.public_key),
        "user_pk": bytes(nacl.public.PrivateKey.generate().public_key),
    }


@pytest.fixture
def count_decrypts(monkeypatch):
    """Count real read_envelope calls (each is an X25519 unwrap + AEAD)."""
    calls = {"n": 0}
    real = envelope.read_envelope

    def counting(env, uid, sk):
        calls["n"] += 1
        return real(env, uid, sk)

    # readside imported the symbol into its own namespace.
    monkeypatch.setattr(readside.envelope, "read_envelope", counting)
    return calls


@pytest.fixture(autouse=True)
def clear_cache():
    readside._CARD_CACHE.clear()
    yield
    readside._CARD_CACHE.clear()


def _moment(k, keys, *, text=None, updated_at=0, item_id=None, visibility="shared"):
    inner = {"summary": f"summary {k}", "content": text or f"content body number {k}",
             "importance": 0.6}
    env = ce.build_envelope(plaintext=json.dumps(inner).encode(), owner_user_id=UID,
                            user_pk_bytes=keys["user_pk"], enclave_pk_bytes=keys["enclave_pk"],
                            visibility=visibility, item_id=item_id)
    env["updated_at"] = updated_at
    return env


def test_warm_load_skips_all_decrypts(keys, count_decrypts):
    moments = [_moment(k, keys) for k in range(50)]
    cold = readside.moments_to_cards_cached(moments, UID, keys["content_sk"])
    assert count_decrypts["n"] == 50
    assert len(cold) == 50 and all(c["content"] for c in cold)

    count_decrypts["n"] = 0
    warm = readside.moments_to_cards_cached(moments, UID, keys["content_sk"])
    assert count_decrypts["n"] == 0            # the whole point
    assert warm == cold


def test_cached_equals_fresh(keys):
    moments = [_moment(k, keys) for k in range(30)]
    cached = readside.moments_to_cards_cached(moments, UID, keys["content_sk"])
    fresh = readside.moments_to_cards(moments, UID, keys["content_sk"])
    assert cached == fresh


def test_edit_invalidates_and_serves_new(keys, count_decrypts):
    moments = [_moment(k, keys, updated_at=k) for k in range(20)]
    readside.moments_to_cards_cached(moments, UID, keys["content_sk"])  # warm

    # Re-seal card 0 with the SAME item_id (AEAD aad binds owner|v|item_id) and new body.
    edited = _moment(0, keys, text="EDITED brand new body", updated_at=1000,
                     item_id=moments[0]["id"])
    moments2 = [edited] + moments[1:]

    count_decrypts["n"] = 0
    cards2 = readside.moments_to_cards_cached(moments2, UID, keys["content_sk"])
    assert count_decrypts["n"] == 20            # miss -> full re-decrypt
    assert any(c["content"] == "EDITED brand new body" for c in cards2)  # no stale


def test_add_and_delete_invalidate(keys, count_decrypts):
    moments = [_moment(k, keys, updated_at=k) for k in range(10)]
    readside.moments_to_cards_cached(moments, UID, keys["content_sk"])

    count_decrypts["n"] = 0
    added = moments + [_moment(99, keys, updated_at=99)]
    readside.moments_to_cards_cached(added, UID, keys["content_sk"])
    assert count_decrypts["n"] == 11            # add -> miss

    count_decrypts["n"] = 0
    deleted = moments[:-1]
    readside.moments_to_cards_cached(deleted, UID, keys["content_sk"])
    assert count_decrypts["n"] == 9             # delete -> miss


def test_local_only_never_decrypted(keys, count_decrypts):
    lo = _moment(0, keys, visibility="local_only")
    moments = [_moment(k, keys) for k in range(1, 6)] + [lo]
    cards = readside.moments_to_cards_cached(moments, UID, keys["content_sk"])
    assert count_decrypts["n"] == 5             # local_only skipped, not decrypted
    assert all(c["id"] != lo["id"] for c in cards)


def test_cache_keyed_per_user(keys, count_decrypts):
    moments = [_moment(k, keys) for k in range(8)]
    readside.moments_to_cards_cached(moments, "usr_A", keys["content_sk"])
    count_decrypts["n"] = 0
    # Same moment bytes, different authorized user -> different fingerprint -> miss.
    readside.moments_to_cards_cached(moments, "usr_B", keys["content_sk"])
    assert count_decrypts["n"] == 8


def test_lru_eviction(keys, monkeypatch):
    monkeypatch.setattr(readside, "_CARD_CACHE_MAX_USERS", 3)
    for u in ("u1", "u2", "u3", "u4"):
        readside.moments_to_cards_cached([_moment(0, keys, item_id="x")], u, keys["content_sk"])
    assert len(readside._CARD_CACHE) == 3
    assert "u1" not in readside._CARD_CACHE     # oldest evicted


def test_concurrent_hits_and_evictions_do_not_raise(keys, monkeypatch):
    """Regression: on anyio's threadpool an eviction popitem() must not race a
    hit/insert move_to_end() into a KeyError (which drops the turn's context memories)."""
    import threading

    monkeypatch.setattr(readside, "_CARD_CACHE_MAX_USERS", 2)
    warm = [_moment(0, keys, item_id="warm")]
    readside.moments_to_cards_cached(warm, "reader", keys["content_sk"])  # prime a hit target
    errors: list = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                readside.moments_to_cards_cached(warm, "reader", keys["content_sk"])
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def churner(n):
        try:
            i = 0
            while not stop.is_set():
                readside.moments_to_cards_cached(
                    [_moment(0, keys, item_id=f"c{n}")], f"user_{n}_{i}", keys["content_sk"])
                i += 1
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=reader) for _ in range(4)] + \
              [threading.Thread(target=churner, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    time.sleep(2.0)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert errors == []
