"""Exercise the encrypted frame ingest path against local Feedling services.

The payload matches the v1 frame envelope emitted by the broadcast extension:
send it over the WebSocket ingest port, read it back via ``/v1/screen/frames``,
and verify that the server persisted the encrypted envelope.
"""

import asyncio
import base64
import hashlib
import json
import secrets
import urllib.request

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def box_seal(plaintext: bytes, recipient_pk: X25519PublicKey) -> bytes:
    ephemeral_sk = X25519PrivateKey.generate()
    ephemeral_pk = ephemeral_sk.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    shared = ephemeral_sk.exchange(recipient_pk)
    recipient_raw = recipient_pk.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=b"feedling-box-seal-v1",
    ).derive(shared)
    nonce = hashlib.sha256(ephemeral_pk + recipient_raw).digest()[:12]
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, None)
    return ephemeral_pk + ciphertext


async def send_frame(wire: dict, api_key: str) -> None:
    uri = "ws://127.0.0.1:9998/ingest"
    async with websockets.connect(
        uri,
        additional_headers={"Authorization": f"Bearer {api_key}"},
    ) as websocket:
        await websocket.send(json.dumps(wire))
        await asyncio.sleep(1.0)


def require_stored_frame(frames: list[dict], item_id: str, user_id: str) -> dict:
    matching = [
        frame
        for frame in frames
        if frame.get("id") == item_id
        or frame.get("filename", "").startswith(item_id)
    ]
    assert matching, f"frame {item_id} missing from frame list"
    frame = matching[0]
    assert frame.get("encrypted") is True, "frame encrypted flag is not true"
    assert frame.get("owner_user_id") == user_id, "frame owner_user_id mismatch"
    return frame


def main() -> None:
    identity_sk = X25519PrivateKey.generate()
    identity_pk_b64 = base64.b64encode(
        identity_sk.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode()
    response = urllib.request.urlopen(
        urllib.request.Request(
            "http://127.0.0.1:5001/v1/users/register",
            data=json.dumps(
                {"public_key": identity_pk_b64, "platform": "test"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    )
    registration = json.loads(response.read())
    user_id = registration["user_id"]
    api_key = registration["api_key"]
    print("test user:", user_id)

    attestation = json.loads(
        urllib.request.urlopen("http://127.0.0.1:5003/attestation").read()
    )
    enclave_pk = X25519PublicKey.from_public_bytes(
        bytes.fromhex(attestation["enclave_content_pk_hex"])
    )

    user_sk = X25519PrivateKey.generate()
    user_pk = user_sk.public_key()
    frame_json = json.dumps(
        {
            "type": "frame",
            "ts": 1776619999.0,
            "app": "com.apple.Safari",
            "ocr_text": "hello from an encrypted frame test",
            "image": base64.b64encode(b"FAKE_JPEG_BYTES").decode(),
            "w": 960,
            "h": 540,
        }
    ).encode()

    item_id = secrets.token_hex(16)
    content_key = secrets.token_bytes(32)
    nonce = secrets.token_bytes(12)
    aad = f"{user_id}|1|{item_id}".encode()
    body_ct = ChaCha20Poly1305(content_key).encrypt(nonce, frame_json, aad)
    wire = {
        "type": "frame",
        "ts": 1776619999.0,
        "envelope": {
            "v": 1,
            "id": item_id,
            "body_ct": base64.b64encode(body_ct).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "K_user": base64.b64encode(box_seal(content_key, user_pk)).decode(),
            "K_enclave": base64.b64encode(box_seal(content_key, enclave_pk)).decode(),
            "visibility": "shared",
            "owner_user_id": user_id,
            "enclave_pk_fpr": "",
        },
    }

    asyncio.run(send_frame(wire, api_key))
    print("sent. item_id=", item_id)

    response = urllib.request.urlopen(
        urllib.request.Request(
            "http://127.0.0.1:5001/v1/screen/frames",
            headers={"X-API-Key": api_key},
        )
    )
    frames = json.loads(response.read()).get("frames", [])
    print("list returned", len(frames), "frame(s)")
    frame = require_stored_frame(frames, item_id, user_id)
    print("✅ frame stored")
    print("    encrypted =", frame.get("encrypted"))
    print("    filename =", frame.get("filename"))
    print("    owner_user_id =", frame.get("owner_user_id"))


if __name__ == "__main__":
    main()
