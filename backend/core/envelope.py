"""v1 envelope construction helpers shared by every write path.

The user's content pubkey lives in the accounts registry, which sits
ABOVE core in the dependency stack — the assembly layer (app.py) injects
``get_user_public_key`` at startup instead of core importing accounts.
"""

import base64
import hashlib

from content_encryption import build_envelope

from core import enclave


# Injected by the assembly layer: returns the user's base64 X25519 content
# pubkey, or "" if the user predates v1 registration.
def get_user_public_key(user_id: str) -> str:
    raise RuntimeError("core.envelope.get_user_public_key not wired by assembly layer")


def _decode_content_public_key(public_key: str) -> tuple[bytes | None, str]:
    raw = (public_key or "").strip()
    if not raw:
        return None, "public_key required"
    try:
        decoded = base64.b64decode(raw, validate=True)
    except Exception:
        return None, "public_key invalid base64"
    if len(decoded) != 32:
        return None, "public_key must decode to 32 bytes"
    return decoded, ""


def _content_public_key_fingerprint(public_key: str | bytes | None) -> str:
    if public_key is None:
        return ""
    if isinstance(public_key, str):
        key_bytes, err = _decode_content_public_key(public_key)
        if err or key_bytes is None:
            return "invalid"
    else:
        key_bytes = public_key
    return hashlib.sha256(key_bytes).hexdigest()[:16]


def _model_api_key_encryption_material(store) -> tuple[bytes, bytes] | tuple[None, str]:
    user_pk_b64 = get_user_public_key(store.user_id)
    if not user_pk_b64:
        return None, "user_content_public_key_missing"
    try:
        user_pk = base64.b64decode(user_pk_b64)
    except Exception:
        return None, "user_content_public_key_invalid_base64"
    if len(user_pk) != 32:
        return None, "user_content_public_key_invalid_length"

    enclave_info = enclave._get_enclave_info()
    if not enclave_info:
        return None, "enclave_info_unavailable"
    try:
        enclave_pk = bytes.fromhex(str(enclave_info.get("content_pk_hex") or ""))
    except Exception:
        return None, "enclave_content_public_key_invalid_hex"
    if len(enclave_pk) != 32:
        return None, "enclave_content_public_key_invalid_length"
    return user_pk, enclave_pk


def _build_shared_envelope_for_store(
    store,
    plaintext: bytes,
    *,
    item_id: str | None = None,
) -> tuple[dict | None, str]:
    material = _model_api_key_encryption_material(store)
    if material[0] is None:
        return None, str(material[1])
    user_pk, enclave_pk = material  # type: ignore[misc]
    try:
        return build_envelope(
            plaintext=plaintext,
            owner_user_id=store.user_id,
            user_pk_bytes=user_pk,  # type: ignore[arg-type]
            enclave_pk_bytes=enclave_pk,  # type: ignore[arg-type]
            visibility="shared",
            item_id=item_id,
        ), ""
    except Exception as e:
        return None, f"envelope_build_failed:{type(e).__name__}:{str(e)[:160]}"


def _enclave_content_public_key_material() -> tuple[bytes | None, str, str]:
    enclave_info = enclave._get_enclave_info()
    if not enclave_info:
        return None, "", "enclave_info_unavailable"
    raw_hex = str(enclave_info.get("content_pk_hex") or "")
    try:
        enclave_pk = bytes.fromhex(raw_hex)
    except Exception:
        return None, "", "enclave_content_public_key_invalid_hex"
    if len(enclave_pk) != 32:
        return None, "", "enclave_content_public_key_invalid_length"
    return enclave_pk, _content_public_key_fingerprint(enclave_pk), ""
