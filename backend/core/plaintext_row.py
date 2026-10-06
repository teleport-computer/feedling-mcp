"""Read one plaintext-tier row exactly as the enclave does (T779 step 2b).

Moved from ``enclave/envelope.read_envelope`` so a plaintext account's rows are
read by the same rule wherever they are read. ``is_sealed_row`` is that
function's routing test (anything sealed, or with no body at all, is not
plaintext); ``read_plaintext_row`` is its plaintext branch: owner binding,
``body`` before ``body_b64``, and the same failure reasons, raised as
``failure_exc(reason)``. Imports only the standard library so the enclave's
crypto module can use it.
"""
from __future__ import annotations

import base64


def is_sealed_row(env: dict) -> bool:
    return bool(
        env.get("body_ct")
        or env.get("K_enclave")
        or (env.get("body") is None and env.get("body_b64") is None)
    )


def read_plaintext_row(env: dict, authorized_user_id: str, failure_exc) -> bytes:
    owner = env.get("owner_user_id")
    if not owner:
        raise failure_exc("envelope missing owner_user_id")
    if owner != authorized_user_id:
        raise failure_exc(
            f"owner mismatch: envelope claims owner={owner} "
            f"but caller is {authorized_user_id}"
        )
    if env.get("body") is not None:
        body = env["body"]
        if not isinstance(body, str):
            raise failure_exc("plaintext body must be a string")
        return body.encode("utf-8")
    if env.get("body_b64") is not None:
        body_b64 = env["body_b64"]
        if not isinstance(body_b64, str):
            raise failure_exc("plaintext body_b64 must be a string")
        try:
            return base64.b64decode(body_b64, validate=True)
        except Exception as e:
            raise failure_exc(f"body_b64 decode: {e}") from e
    raise failure_exc("envelope has no supported body shape")
