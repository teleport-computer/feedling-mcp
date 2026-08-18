"""Consumer whoami key-cache guards (usr_f13f 2026-07-16).

The resident consumer sealed two days of replies to a retired user content
key because ``_refresh_whoami_for_encrypted_reply``'s cached-keys fallback has
no age bound — one successful whoami at startup + chronically failing
refreshes = stale keys forever.

Two guards under test:

1. The cached-keys fallback refuses a cache older than
   ``WHOAMI_STALE_KEYS_MAX_AGE_SEC`` (skip the write loudly instead of sealing
   to a possibly-rotated key).
2. ``post_reply`` handles the backend's ``content_pk_fpr_mismatch`` 409 by
   force-refreshing whoami and re-sealing + retrying ONCE with the fresh key.
"""

import hashlib
import os
import sys
import time
from pathlib import Path

import pytest

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_keyguard_checkpoint.json",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import tools.chat_resident_consumer as crc  # noqa: E402

from _fake_clock import freeze_monotonic  # noqa: E402


OLD_PK = b"\x11" * 32
NEW_PK = b"\x22" * 32
ENC_PK = b"\x02" * 32


def _fpr(pk: bytes) -> str:
    return hashlib.sha256(pk).hexdigest()[:16]


@pytest.fixture(autouse=True)
def _reset_whoami_cache(monkeypatch):
    # These tests age the cache by subtracting from time.monotonic(); on a
    # freshly booted host that goes NEGATIVE, and the over-age guard's
    # `_whoami_cache_loaded_at > 0` ("never loaded") check then skips the
    # rejection under test. Pin the clock (see tests/_fake_clock.py).
    freeze_monotonic(monkeypatch)
    monkeypatch.setitem(crc._whoami_cache, "user_id", "usr_keyguard")
    monkeypatch.setitem(crc._whoami_cache, "user_pk", OLD_PK)
    monkeypatch.setitem(crc._whoami_cache, "enclave_pk", ENC_PK)
    monkeypatch.setattr(crc, "_whoami_cache_loaded_at", time.monotonic())
    yield


class _FakeResp:
    def __init__(self, status_code: int, body: dict):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


# --------------------------------------------------------------------------- #
# 1. cached-keys fallback age bound
# --------------------------------------------------------------------------- #

def test_fallback_rejects_cache_beyond_max_age(monkeypatch):
    monkeypatch.setattr(crc, "_load_whoami_with_retries", lambda **kw: False)
    monkeypatch.setattr(
        crc, "_whoami_cache_loaded_at",
        time.monotonic() - (crc.WHOAMI_STALE_KEYS_MAX_AGE_SEC + 5))
    assert crc._refresh_whoami_for_encrypted_reply() is False


def test_ttl_shortcut_does_not_bypass_max_age_cap(monkeypatch):
    """With the cap configured BELOW the TTL, a cache older than the cap must
    not be trusted via the TTL fast path — a refresh is attempted, and when it
    fails the over-age fallback refuses the keys."""
    refresh_calls: list = []

    def failing_refresh(**kw):
        refresh_calls.append(kw)
        return False

    monkeypatch.setattr(crc, "_load_whoami_with_retries", failing_refresh)
    monkeypatch.setattr(crc, "WHOAMI_STALE_KEYS_MAX_AGE_SEC", 60.0)
    # age 120s: inside the 300s TTL, beyond the 60s cap.
    monkeypatch.setattr(crc, "_whoami_cache_loaded_at", time.monotonic() - 120)
    assert crc._refresh_whoami_for_encrypted_reply() is False
    assert refresh_calls, "must attempt a refresh instead of trusting the TTL shortcut"


def test_fallback_allows_cache_within_max_age(monkeypatch):
    monkeypatch.setattr(crc, "_load_whoami_with_retries", lambda **kw: False)
    # TTL expired (so a refresh is attempted and fails) but well inside the
    # stale bound — the fallback must still allow the cached keys.
    monkeypatch.setattr(
        crc, "_whoami_cache_loaded_at",
        time.monotonic() - (crc.WHOAMI_REFRESH_TTL_SEC + 5))
    assert crc._refresh_whoami_for_encrypted_reply() is True


# --------------------------------------------------------------------------- #
# 2. post_reply re-seals + retries once on content_pk_fpr_mismatch
# --------------------------------------------------------------------------- #

def _install_refreshing_loader(monkeypatch, calls: list):
    def fake_refresh(**kw):
        calls.append(kw)
        crc._whoami_cache.update(user_pk=NEW_PK)
        monkeypatch.setattr(crc, "_whoami_cache_loaded_at", time.monotonic())
        return True
    monkeypatch.setattr(crc, "_load_whoami_with_retries", fake_refresh)


def test_post_reply_reseals_and_retries_on_fpr_mismatch(monkeypatch):
    refresh_calls: list = []
    _install_refreshing_loader(monkeypatch, refresh_calls)

    posts: list = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append(json)
        if len(posts) == 1:
            return _FakeResp(409, {
                "error": "content_pk_fpr_mismatch",
                "current_public_key_fpr": _fpr(NEW_PK),
                "envelope_content_pk_fpr": _fpr(OLD_PK),
            })
        return _FakeResp(200, {"id": "reply1", "ts": 1.0})

    monkeypatch.setattr(crc._HTTP, "post", fake_post)

    result = crc.post_reply("你好")

    assert len(posts) == 2, "should retry exactly once after the 409"
    assert posts[0]["envelope"]["content_pk_fpr"] == _fpr(OLD_PK)
    assert posts[1]["envelope"]["content_pk_fpr"] == _fpr(NEW_PK)
    assert len(refresh_calls) == 1, "409 must force a whoami refresh"
    assert result.get("id") == "reply1"


def test_post_reply_gives_up_after_one_retry(monkeypatch):
    refresh_calls: list = []
    _install_refreshing_loader(monkeypatch, refresh_calls)

    posts: list = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append(json)
        return _FakeResp(409, {
            "error": "content_pk_fpr_mismatch",
            "current_public_key_fpr": _fpr(NEW_PK),
            "envelope_content_pk_fpr": _fpr(OLD_PK),
        })

    monkeypatch.setattr(crc._HTTP, "post", fake_post)

    with pytest.raises(Exception):
        crc.post_reply("你好")
    assert len(posts) == 2, "persistent mismatch must not retry forever"


def test_post_reply_passes_through_other_409(monkeypatch):
    """bootstrap_incomplete keeps its existing no-crash contract."""
    posts: list = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append(json)
        return _FakeResp(409, {"error": "bootstrap_incomplete", "stage": "x"})

    monkeypatch.setattr(crc._HTTP, "post", fake_post)
    result = crc.post_reply("你好")
    assert len(posts) == 1
    assert result.get("error") == "bootstrap_incomplete"


def test_post_reply_retries_explicitly_retryable_bootstrap_and_succeeds(monkeypatch):
    responses = [
        _FakeResp(409, {
            "error": "bootstrap_incomplete",
            "stage": "needs_resident_consumer",
            "retryable": True,
        }),
        _FakeResp(409, {
            "error": "bootstrap_incomplete",
            "stage": "needs_resident_consumer",
            "retryable": True,
        }),
        _FakeResp(200, {"id": "reply-after-retry"}),
    ]
    posts: list = []
    sleeps: list[float] = []
    monkeypatch.setattr(crc, "CHAT_RESPONSE_MAX_RETRIES", 3)
    monkeypatch.setattr(crc, "CHAT_RESPONSE_RETRY_BASE_SEC", 0.5)
    monkeypatch.setattr(crc, "CHAT_RESPONSE_RETRY_MAX_ELAPSED_SEC", 10.0)
    monkeypatch.setattr(crc.time, "sleep", sleeps.append)

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append(json)
        return responses.pop(0)

    monkeypatch.setattr(crc._HTTP, "post", fake_post)
    result = crc.post_reply("你好")
    assert result == {"id": "reply-after-retry"}
    assert len(posts) == 3
    assert sleeps == [0.5, 1.0]


def test_post_reply_retryable_bootstrap_has_hard_attempt_bound(monkeypatch):
    posts: list = []
    sleeps: list[float] = []
    monkeypatch.setattr(crc, "CHAT_RESPONSE_MAX_RETRIES", 3)
    monkeypatch.setattr(crc, "CHAT_RESPONSE_RETRY_BASE_SEC", 0.25)
    monkeypatch.setattr(crc, "CHAT_RESPONSE_RETRY_MAX_ELAPSED_SEC", 10.0)
    monkeypatch.setattr(crc.time, "sleep", sleeps.append)

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append(json)
        return _FakeResp(409, {
            "error": "bootstrap_incomplete",
            "stage": "needs_resident_consumer",
            "retryable": True,
        })

    monkeypatch.setattr(crc._HTTP, "post", fake_post)
    result = crc.post_reply("你好")
    assert result["error"] == "bootstrap_incomplete"
    assert len(posts) == 4, "initial request plus exactly three retries"
    assert sleeps == [0.25, 0.5, 1.0]


def test_post_reply_retry_budget_can_disable_retry_wait(monkeypatch):
    posts: list = []
    sleeps: list[float] = []
    monkeypatch.setattr(crc, "CHAT_RESPONSE_MAX_RETRIES", 3)
    monkeypatch.setattr(crc, "CHAT_RESPONSE_RETRY_BASE_SEC", 0.25)
    monkeypatch.setattr(crc, "CHAT_RESPONSE_RETRY_MAX_ELAPSED_SEC", 0.0)
    monkeypatch.setattr(crc.time, "sleep", sleeps.append)

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append(json)
        return _FakeResp(409, {
            "error": "bootstrap_incomplete",
            "stage": "needs_resident_consumer",
            "retryable": True,
        })

    monkeypatch.setattr(crc._HTTP, "post", fake_post)
    result = crc.post_reply("你好")
    assert result["error"] == "bootstrap_incomplete"
    assert len(posts) == 1
    assert sleeps == []


def test_post_reply_retryable_false_remains_single_terminal_attempt(monkeypatch):
    posts: list = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append(json)
        return _FakeResp(409, {
            "error": "bootstrap_incomplete",
            "stage": "needs_live_connection",
            "retryable": False,
        })

    monkeypatch.setattr(crc._HTTP, "post", fake_post)
    result = crc.post_reply("你好")
    assert len(posts) == 1
    assert result["retryable"] is False


def test_reply_envelope_is_labeled_with_seal_key_fpr(monkeypatch):
    """build_envelope's label rides on the consumer reply wire shape."""
    posts: list = []

    def fake_post(url, json=None, headers=None, timeout=None):
        posts.append(json)
        return _FakeResp(200, {"id": "reply2", "ts": 2.0})

    monkeypatch.setattr(crc._HTTP, "post", fake_post)
    crc.post_reply("你好", thinking_summary="想了想")
    assert posts[0]["envelope"]["content_pk_fpr"] == _fpr(OLD_PK)
    assert posts[0]["thinking_envelope"]["content_pk_fpr"] == _fpr(OLD_PK)
