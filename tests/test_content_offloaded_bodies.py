"""Content management must SEE chat bodies that live in object storage.

``chat_load`` returns heavy rows (image/file) as slim pointers: nonce/K_user/
K_enclave stay, ``body_ct`` is replaced by ``body_key``. Anything that reasons
about "is this encrypted content" or needs the ciphertext itself has to resolve
that pointer, or it silently concludes the row is not encrypted content at all.

That is not a cosmetic miscount. Three paths, each a different way to lose data:

  * the rotation guard would wave a public-key change through with no rewrap,
  * rewrap would skip the row AND still report a clean run (so the key advances),
  * export would ship the user a message with no ciphertext in it.

All three end with a body wrapped under a retired K_user — unreadable forever.
These tests reload the store from the DB first, because that is when the in-memory
row turns back into a pointer (a worker restart, an eviction, or the backfill).
"""

from __future__ import annotations

import base64
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db  # noqa: E402
import object_storage  # noqa: E402
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import store as core_store  # noqa: E402

from test_chat_file_r2 import _enable_r2  # noqa: E402
from test_frame_r2 import _FakeS3  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    monkeypatch.setattr(
        core_enclave,
        "_get_enclave_info",
        lambda: {"content_pk_hex": ("22" * 32), "compose_hash": "test"},
    )
    with make_client() as c:
        yield c


def _register(client) -> tuple[str, str]:
    res = client.post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x11" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    body = res.get_json()
    return body["user_id"], body["api_key"]


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _seed_offloaded_image(user_id: str, body: bytes = b"\xff\xd8\xff-secret-pixels") -> str:
    """Append one image, let it offload to R2, then drop the in-memory store so the
    row comes back the way a restarted worker would see it: as a pointer."""
    mid = uuid.uuid4().hex
    store = core_store.get_store(user_id)
    msg = {
        "v": 1, "id": mid, "role": "user", "source": "test", "ts": time.time(),
        "content_type": "image", "image_mime": "image/jpeg",
        "body_ct": _b64(body),
        "nonce": _b64(b"\x00" * 12),
        "K_user": _b64(b"\x01" * 48),
        "K_enclave": _b64(b"\x02" * 48),
        "visibility": "shared", "owner_user_id": user_id,
        "enclave_pk_fpr": "old",
    }
    with store.chat_lock:
        store.chat_messages = [msg]
        db.chat_append(user_id, mid, msg["ts"], msg, core_store.MAX_CHAT_MESSAGES)

    core_store._stores.clear()                       # ← worker reload
    reloaded = core_store.get_store(user_id)
    with reloaded.chat_lock:
        row = reloaded.chat_messages[0]
    assert row.get("body_key") and row.get("body_ct") is None   # precondition
    return mid


def test_rotation_guard_counts_an_offloaded_image(client, monkeypatch):
    # Guard reads counts["total"]. If a pointer row reads as "unencrypted", total is
    # 0 and the key rotates with NO rewrap — stranding the image under the old key.
    _enable_r2(monkeypatch, _FakeS3())
    user_id, api_key = _register(client)
    _seed_offloaded_image(user_id)

    res = client.post(
        "/v1/users/public-key",
        json={"public_key": _b64(b"\x33" * 32)},
        headers=_headers(api_key),
    )

    assert res.status_code == 409, res.get_data(as_text=True)
    body = res.get_json()
    assert body["error"] == "public_key_rotation_requires_rewrap"
    assert body["encrypted_content"]["chat"] == 1        # the image is SEEN
    assert registry._get_user_public_key(user_id) == _b64(b"\x11" * 32)   # not rotated


def test_rewrap_rewraps_an_offloaded_image_rather_than_skipping_it(client, monkeypatch):
    # The worst path: a skip is not an error, so the run reports clean and the
    # registered key advances — leaving the image wrapped to a retired K_user.
    _enable_r2(monkeypatch, _FakeS3())
    user_id, api_key = _register(client)
    mid = _seed_offloaded_image(user_id, b"\xff\xd8\xff-original-pixels")
    old_k_user = _b64(b"\x01" * 48)

    seen: list[bytes] = []

    def fake_decrypt(envelope, key, purpose):
        # Proves the enclave got the REAL ciphertext, not an empty pointer.
        seen.append(base64.b64decode(envelope["body_ct"]))
        return b"plaintext"

    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", fake_decrypt)

    res = client.post(
        "/v1/content/rewrap-to-current-key",
        json={"public_key": _b64(b"\x33" * 32)},
        headers=_headers(api_key),
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    summary = res.get_json()["summary"]
    assert summary["total_rewrapped"] == 1              # NOT skipped_unencrypted
    assert summary["total_errors"] == 0
    assert seen == [b"\xff\xd8\xff-original-pixels"]    # real body reached the enclave

    store = core_store.get_store(user_id)
    with store.chat_lock:
        row = next(m for m in store.chat_messages if m.get("id") == mid)
    assert row["K_user"] != old_k_user                  # actually re-wrapped
    assert registry._get_user_public_key(user_id) == _b64(b"\x33" * 32)


def test_export_includes_an_offloaded_image_body(client, monkeypatch):
    # Export ships ciphertext verbatim for client-side decrypt. A pointer row would
    # export as a message with no body at all — a silently incomplete export.
    _enable_r2(monkeypatch, _FakeS3())
    user_id, api_key = _register(client)
    mid = _seed_offloaded_image(user_id, b"\xff\xd8\xff-exported-pixels")

    res = client.get("/v1/content/export", headers=_headers(api_key))
    assert res.status_code == 200, res.get_data(as_text=True)

    chat = res.get_json()["chat"]
    row = next(m for m in chat if m.get("id") == mid)
    assert base64.b64decode(row["body_ct"]) == b"\xff\xd8\xff-exported-pixels"
