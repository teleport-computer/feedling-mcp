"""T779 step 2a: the history decrypt view moved to core.history_view unchanged.

``tests/data/history_view_447b222d.json`` is the enclave view of ``history_rows``
produced by the pre-change code (reports/T780/gen_history_fixture.py replays it
from the base commit). Keys are random per run but never reach the view.
"""
from __future__ import annotations

import ast
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent))

import pytest  # noqa: E402

from core import chat_images  # noqa: E402
from test_enclave_envelope_core import _make_envelope  # noqa: E402

BACKEND = Path(__file__).parent.parent / "backend"
FIXTURE = Path(__file__).parent / "data" / "history_view_447b222d.json"

OWNER = "usr_a"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 40
JPG = b"\xff\xd8\xff" + b"y" * 40


def base(i, role="user", **extra):
    row = {"seq": i, "role": role, "ts": 1790000000.0 + i, "source": "chat", "v": 1,
           "owner_user_id": OWNER}
    row.update(extra)
    return row


def sealed(pk, i, text, **extra):
    env = _make_envelope(OWNER, f"m{i}", text.encode(), pk)
    return {**base(i, **extra), **env}


def sealed_caption(pk, cid, text):
    env = _make_envelope(OWNER, cid, text.encode(), pk)
    return {"caption_id": cid, "caption_body_ct": env["body_ct"], "caption_nonce": env["nonce"],
            "caption_K_enclave": env["K_enclave"], "caption_v": 1}


def history_rows(pk: bytes) -> list[dict]:
    """One page covering every branch of the history view (sealed/plaintext text, image,
    bundle, file, captions, body_omitted, local_only, a tampered sealed row)."""
    return [
        sealed(pk, 1, "担心咪咪最近不吃饭"),
        {**base(2, role="agent"), "id": "m2", "body": "是咪咪吗？吃饭情况持续几天了？"},
        {**base(3), "id": "m3", "content_type": "image", "body_b64": base64.b64encode(PNG).decode(),
         "image_mime": "image/png", "caption_body": "这是它今天的饭盆"},
        {**base(4), "id": "m4", "content_type": "image", "image_bundle_version": 1,
         "body_b64": base64.b64encode(chat_images.encode_image_bundle([(PNG, "image/png"), (JPG, "image/jpeg")])).decode(),
         "image_mimes": ["image/png", "image/jpeg"], "vision_route_id": "vr1"},
        {**base(5), "id": "m5", "content_type": "file", "body_b64": base64.b64encode(b"%PDF-1.4 x").decode(),
         "file_mime": "application/pdf", "file_name": "vet.pdf", "file_display_title": "化验单",
         "caption_body": "兽医给的化验单"},
        {**sealed(pk, 6, "sealed image bytes are not an image", content_type="image", image_mime="image/jpeg"),
         **sealed_caption(pk, "cap6", "封存的图片说明")},
        {**base(7), "id": "m7", "content_type": "image", "body_omitted": True,
         "body_omitted_reason": "include_image_body_false", "image_mime": "image/jpeg",
         "caption_body": "省略正文的图片说明", "quoted_memory_ids": " card_1,card_2 "},
        {**base(8), "id": "m8", "content_type": "file", "body_omitted": True, "file_mime": "text/plain",
         "file_name": "notes.txt", **sealed_caption(pk, "cap8", "省略正文的文件说明")},
        {**base(9), "id": "m9", "body_omitted": True},
        {**base(10), "id": "m10", "visibility": "local_only", "body": "local only text"},
        {**sealed(pk, 11, "tampered"), "body_ct": base64.b64encode(b"\x00" * 40).decode()},
        {**base(12, role="user"), "id": "m12", "body": "我每天练 guitar 多久？",
         "voice_call_id": "call-1", "voice_turn_count": 3, "reply_to_message_id": "m2"},
    ]


def test_enclave_view_matches_the_pre_change_output():
    import nacl.public
    from enclave.routes import chat

    sk = nacl.public.PrivateKey.generate()
    decrypted, errors = chat._decrypt_history_items(history_rows(bytes(sk.public_key)), OWNER, sk)
    frozen = json.loads(FIXTURE.read_text())
    assert frozen["generated_from"].startswith("447b222d")
    assert {"decrypted": decrypted, "errors": errors} == frozen["view"]


@pytest.mark.parametrize("module", ["core/history_view.py", "memory/embedding/recall_policy.py",
                                    "memory/recall_select.py"])
def test_shared_modules_do_not_import_the_enclave_package(module):
    tree = ast.parse((BACKEND / module).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported, "parsed no imports; the scan is not reading the module"
    assert not [m for m in imported if m == "enclave" or m.startswith("enclave.")]
