"""End-to-end v1 envelope roundtrip using the same wire format iOS emits.

Matches testapp/FeedlingTest/ContentEncryption.swift:
- body: ChaCha20-Poly1305 IETF (12-byte nonce), AAD = owner|v|id UTF-8
- K_user / K_enclave: BoxSeal with HKDF-SHA256(info="feedling-box-seal-v1")
  and ChaChaPoly AEAD; wire format ek_pub(32) || ct || tag(16)
"""
import base64, json, secrets, urllib.request

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305

B64 = lambda b: base64.b64encode(b).decode()
UNB64 = lambda s: base64.b64decode(s)

# --- load registered user ---
users = json.load(open("/tmp/fl-live2/users.json"))
u = users[0]
user_id = u["user_id"]
# We need the RAW api key, not the hash — that's in Keychain on the device.
# Short-circuit: look at recent backend log for X-API-Key header? Not logged.
# Better: register a NEW test user here so we own its api key.
print("registered user:", user_id, " — registering fresh test user")

ident_sk = X25519PrivateKey.generate()
ident_pk_b64 = B64(ident_sk.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw))
r = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:5001/v1/users/register",
    data=json.dumps({"public_key": ident_pk_b64, "platform": "test"}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST"))
d = json.loads(r.read())
test_user_id = d["user_id"]
test_api_key = d["api_key"]
print("test user:", test_user_id, "api_key=", test_api_key[:12]+"...")

# --- fetch enclave content pk ---
att = json.loads(urllib.request.urlopen("http://127.0.0.1:5003/attestation").read())
enc_pk_hex = att["enclave_content_pk_hex"]
enc_pk = X25519PublicKey.from_public_bytes(bytes.fromhex(enc_pk_hex))
print("enclave content pk:", enc_pk_hex[:32]+"...")

# --- user content keypair ---
user_sk = X25519PrivateKey.generate()
user_pk = user_sk.public_key()
user_pk_bytes = user_pk.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

# --- build v1 envelope ---
plaintext = b"Hello from a v1 ChaCha envelope ASCII only"
item_id = secrets.token_hex(16)

# body key K: random 32-byte ChaCha key
K = secrets.token_bytes(32)
body_nonce = secrets.token_bytes(12)
aad = f"{test_user_id}|1|{item_id}".encode()
body_ct = ChaCha20Poly1305(K).encrypt(body_nonce, plaintext, aad)

def box_seal(pt: bytes, recipient_pk: X25519PublicKey) -> bytes:
    """HKDF-SHA256(info='feedling-box-seal-v1') + ChaChaPoly. Returns ek_pub || ct || tag16."""
    ek = X25519PrivateKey.generate()
    ek_pub = ek.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    shared = ek.exchange(recipient_pk)
    # salt = ek_pub || recipient_pk (matches ContentEncryption.swift)
    recipient_raw = recipient_pk.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=ek_pub + recipient_raw,
               info=b"feedling-box-seal-v1").derive(shared)
    # ChaChaPoly with 12-byte zero nonce (ephemeral key = key commitment)
    nonce = b"\x00" * 12
    ct = ChaCha20Poly1305(key).encrypt(nonce, pt, None)
    return ek_pub + ct

K_user = box_seal(K, user_pk)
K_enclave = box_seal(K, enc_pk)

envelope = {
    "id": item_id,
    "v": 1,
    "owner_user_id": test_user_id,
    "visibility": "shared",
    "body_ct": B64(body_ct),
    "nonce": B64(body_nonce),
    "K_user": B64(K_user),
    "K_enclave": B64(K_enclave),
    "enclave_pk_fpr": "",
}
print("envelope id:", item_id, "body_ct_len:", len(body_ct))

# --- POST chat message ---
r = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:5001/v1/chat/message",
    data=json.dumps({"envelope": envelope}).encode(),
    headers={"Content-Type": "application/json", "X-API-Key": test_api_key},
    method="POST"))
print("POST status:", r.status, "body:", r.read()[:200])

# --- GET history and verify envelope comes back v1 ---
r = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:5001/v1/chat/history?since=0&limit=10",
    headers={"X-API-Key": test_api_key}))
hist = json.loads(r.read())
msgs = hist["messages"]
print("history count:", len(msgs))
for m in msgs:
    print(" msg id=%s role=%s v=%s body_ct_len=%s" % (
        m.get("id",""), m.get("role",""), m.get("v"),
        len(m.get("body_ct") or "") if m.get("body_ct") else None))
    if m.get("id") == item_id:
        assert m.get("v") == 1, "expected v=1"
        assert m.get("body_ct") == envelope["body_ct"], "body_ct mismatch"
        assert m.get("nonce") == envelope["nonce"], "nonce mismatch"
        assert m.get("K_user") == envelope["K_user"], "K_user mismatch"
        print("✅ envelope roundtripped intact")

# --- verify we can decrypt with user_sk (what iOS does) ---
def box_open(blob: bytes, sk: X25519PrivateKey, recipient_pk: X25519PublicKey) -> bytes:
    ek_pub_bytes = blob[:32]
    ct = blob[32:]
    ek_pub = X25519PublicKey.from_public_bytes(ek_pub_bytes)
    shared = sk.exchange(ek_pub)
    recipient_raw = recipient_pk.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=ek_pub_bytes + recipient_raw,
               info=b"feedling-box-seal-v1").derive(shared)
    return ChaCha20Poly1305(key).decrypt(b"\x00"*12, ct, None)

K_recovered = box_open(UNB64(envelope["K_user"]), user_sk, user_pk)
assert K_recovered == K, "K recovery failed"
pt_recovered = ChaCha20Poly1305(K_recovered).decrypt(UNB64(envelope["nonce"]), UNB64(envelope["body_ct"]), aad)
assert pt_recovered == plaintext, "plaintext mismatch"
print("✅ user-side decrypt recovered plaintext: %r" % pt_recovered.decode())

# --- verify enclave can also decrypt (server-side path) ---
r = urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:5001/v1/chat/history?since=0&limit=10&decrypt=true",
    headers={"X-API-Key": test_api_key}))
dec = json.loads(r.read())
for m in dec["messages"]:
    if m.get("id") == item_id:
        print("enclave decrypt content:", repr(m.get("content","<none>")[:80]))
