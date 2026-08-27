"""End-to-end v1 envelope roundtrip using the same wire format iOS emits.

Matches testapp/FeedlingTest/ContentEncryption.swift:
- body: ChaCha20-Poly1305 IETF (12-byte nonce), AAD = owner|v|id UTF-8
- K_user / K_enclave: BoxSeal with HKDF-SHA256(info="feedling-box-seal-v1")
  and ChaChaPoly AEAD; wire format ek_pub(32) || ct || tag(16)
"""
import base64
import hashlib
import json
import secrets
import urllib.request

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


def unb64(value: str) -> bytes:
    return base64.b64decode(value)


def box_seal(pt: bytes, recipient_pk: X25519PublicKey) -> bytes:
    """Current iOS/backend BoxSeal: salt=None and a key-bound nonce."""
    ek = X25519PrivateKey.generate()
    ek_pub = ek.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    shared = ek.exchange(recipient_pk)
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
    nonce = hashlib.sha256(ek_pub + recipient_raw).digest()[:12]
    ct = ChaCha20Poly1305(key).encrypt(nonce, pt, None)
    return ek_pub + ct


def box_open(blob: bytes, sk: X25519PrivateKey, recipient_pk: X25519PublicKey) -> bytes:
    ek_pub_bytes = blob[:32]
    ct = blob[32:]
    ek_pub = X25519PublicKey.from_public_bytes(ek_pub_bytes)
    shared = sk.exchange(ek_pub)
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
    nonce = hashlib.sha256(ek_pub_bytes + recipient_raw).digest()[:12]
    return ChaCha20Poly1305(key).decrypt(nonce, ct, None)


def require_message(messages: list[dict], item_id: str, stage: str) -> dict:
    matching = [message for message in messages if message.get("id") == item_id]
    assert matching, f"envelope {item_id} missing from {stage}"
    return matching[0]


def main() -> None:
    ident_sk = X25519PrivateKey.generate()
    ident_pk_b64 = b64(
        ident_sk.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    )
    response = urllib.request.urlopen(
        urllib.request.Request(
            "http://127.0.0.1:5001/v1/users/register",
            data=json.dumps({"public_key": ident_pk_b64, "platform": "test"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    )
    registration = json.loads(response.read())
    test_user_id = registration["user_id"]
    test_api_key = registration["api_key"]
    print("test user:", test_user_id, "api_key=", test_api_key[:12] + "...")

    attestation = json.loads(
        urllib.request.urlopen("http://127.0.0.1:5003/attestation").read()
    )
    enc_pk_hex = attestation["enclave_content_pk_hex"]
    enc_pk = X25519PublicKey.from_public_bytes(bytes.fromhex(enc_pk_hex))
    print("enclave content pk:", enc_pk_hex[:32] + "...")

    user_sk = X25519PrivateKey.generate()
    user_pk = user_sk.public_key()

    plaintext = b"Hello from a v1 ChaCha envelope ASCII only"
    item_id = secrets.token_hex(16)
    content_key = secrets.token_bytes(32)
    body_nonce = secrets.token_bytes(12)
    aad = f"{test_user_id}|1|{item_id}".encode()
    body_ct = ChaCha20Poly1305(content_key).encrypt(body_nonce, plaintext, aad)

    envelope = {
        "id": item_id,
        "v": 1,
        "owner_user_id": test_user_id,
        "visibility": "shared",
        "body_ct": b64(body_ct),
        "nonce": b64(body_nonce),
        "K_user": b64(box_seal(content_key, user_pk)),
        "K_enclave": b64(box_seal(content_key, enc_pk)),
        "enclave_pk_fpr": "",
    }
    print("envelope id:", item_id, "body_ct_len:", len(body_ct))

    response = urllib.request.urlopen(
        urllib.request.Request(
            "http://127.0.0.1:5001/v1/chat/message",
            data=json.dumps({"envelope": envelope}).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": test_api_key},
            method="POST",
        )
    )
    print("POST status:", response.status, "body:", response.read()[:200])

    response = urllib.request.urlopen(
        urllib.request.Request(
            "http://127.0.0.1:5001/v1/chat/history?since=0&limit=10",
            headers={"X-API-Key": test_api_key},
        )
    )
    history = json.loads(response.read())
    print("history count:", len(history["messages"]))
    for message in history["messages"]:
        print(
            " msg id=%s role=%s v=%s body_ct_len=%s"
            % (
                message.get("id", ""),
                message.get("role", ""),
                message.get("v"),
                len(message.get("body_ct") or "") if message.get("body_ct") else None,
            )
        )
    stored = require_message(history["messages"], item_id, "chat history")
    assert stored.get("v") == 1, "expected v=1"
    assert stored.get("owner_user_id") == test_user_id, "owner_user_id mismatch"
    assert stored.get("body_ct") == envelope["body_ct"], "body_ct mismatch"
    assert stored.get("nonce") == envelope["nonce"], "nonce mismatch"
    assert stored.get("K_user") == envelope["K_user"], "K_user mismatch"
    assert stored.get("K_enclave") == envelope["K_enclave"], "K_enclave mismatch"
    print("✅ envelope roundtripped intact")

    recovered_key = box_open(unb64(envelope["K_user"]), user_sk, user_pk)
    assert recovered_key == content_key, "K recovery failed"
    recovered_plaintext = ChaCha20Poly1305(recovered_key).decrypt(
        unb64(envelope["nonce"]),
        unb64(envelope["body_ct"]),
        aad,
    )
    assert recovered_plaintext == plaintext, "plaintext mismatch"
    print("✅ user-side decrypt recovered plaintext: %r" % recovered_plaintext.decode())

    response = urllib.request.urlopen(
        urllib.request.Request(
            "http://127.0.0.1:5001/v1/chat/history?since=0&limit=10&decrypt=true",
            headers={"X-API-Key": test_api_key},
        )
    )
    decrypted = json.loads(response.read())
    decrypted_message = require_message(
        decrypted["messages"],
        item_id,
        "decrypted chat history",
    )
    assert decrypted_message.get("owner_user_id") == test_user_id, (
        "decrypted owner_user_id mismatch"
    )
    assert decrypted_message.get("content") == plaintext.decode(), (
        "enclave plaintext mismatch"
    )
    print("enclave decrypt content:", repr(decrypted_message["content"][:80]))


if __name__ == "__main__":
    main()
