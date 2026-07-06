"""Decrypt-window derivation for redelivered poll messages.

The consumer fetches plaintext with get_decrypted_history(since=...). Deriving
`since` from its own cursor breaks the server's unanswered-turn redelivery
backstop: a redelivered message has ts <= last_ts, so the decrypt fetch never
returns it, _filter_messages_to_poll_ids comes back empty, and the wedge-skip
path burns the claim. The window must instead open far enough back to cover
the OLDEST message in the poll batch.

Run:  python -m pytest tests/test_consumer_decrypt_since.py -q
"""

import os
import sys
import types
from pathlib import Path

_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_decrypt_since_checkpoint.json",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules.setdefault("content_encryption", _fake_enc)

import tools.chat_resident_consumer as crc  # noqa: E402  (after env setup)


def test_empty_batch_keeps_cursor():
    assert crc._poll_decrypt_since(100.0, []) == 100.0


def test_all_newer_than_cursor_keeps_cursor():
    msgs = [{"ts": 101.0}, {"ts": 105.5}]
    assert crc._poll_decrypt_since(100.0, msgs) == 100.0


def test_redelivered_older_message_opens_window_before_it():
    msgs = [{"ts": 42.5}, {"ts": 101.0}]
    since = crc._poll_decrypt_since(100.0, msgs)
    assert since < 42.5  # decrypt fetch must include the redelivered message


def test_unparseable_ts_is_ignored():
    msgs = [{"ts": "bogus"}, {"ts": None}, {}]
    assert crc._poll_decrypt_since(100.0, msgs) == 100.0


def test_decrypt_limit_default_when_window_not_pulled_back():
    assert crc._poll_decrypt_limit(100.0, 100.0, [{"ts": 101.0}]) == 20


def test_decrypt_limit_covers_whole_claimed_batch():
    # Every claimed message must be decryptable in one fetch: a truncated fetch
    # drops claimed messages, and redelivery claims can't be retried until the
    # TTL expires.
    msgs = [{"ts": float(i)} for i in range(60)]
    assert crc._poll_decrypt_limit(0.5, 100.0, msgs) >= 80
