# backend/enclave/routes/identity.py
"""身份卡 decrypt-and-serve（旧 enclave_app L1700-1799，模式同 Task 10 memory）。
days_with_user 从服务端锚点实时计算，覆盖信封内旧值；单条解密经 to_thread。"""

from __future__ import annotations

import datetime as _dt
import json

import anyio.to_thread
from fastapi import APIRouter
from starlette.requests import Request
from starlette.responses import JSONResponse

from enclave import auth, backend_client, envelope
from enclave.routes._errors import backend_call_or_error, content_sk_or_503

# Pure-stdlib policy/view modules (no DB deps) — safe to import inside the
# enclave, and the reason this route can't drift from the backend's writable-field
# list again. card_view holds the assembly this route shares with the V2 identity
# capability, which decrypts the same card through /v1/envelope/decrypt.
from identity import card_view

router = APIRouter()


def _parse_iso_calendar_date(value: str) -> _dt.date | None:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        norm = raw.replace("Z", "+00:00")
        if "T" not in norm:
            norm = norm + "T00:00:00"
        return _dt.datetime.fromisoformat(norm).date()
    except Exception:
        return None


# HEAD 显式声明（同 frames.py）：Flask 自动给 GET 挂 HEAD，FastAPI 不会。
@router.api_route("/v1/identity/get", methods=["GET", "HEAD"])
async def v1_identity_get(request: Request):
    """Decrypt-and-serve the identity card for the authenticated user.

    Returns the same shape as /v1/identity/get (agent_name, self_introduction,
    dimensions[]), assembled from decrypted ciphertext when stored as v1.
    """
    ctx = auth.extract_auth(request)
    user_id, error = await auth.resolve_read_caller(ctx)
    if error is not None:
        body, status = error
        return JSONResponse(body, status_code=status)

    resp, err_response = await backend_call_or_error(
        backend_client.backend_get("/v1/identity/get", ctx.forward_headers))
    if err_response is not None:
        return err_response

    identity = resp.get("identity")
    if identity is None:
        return JSONResponse({"identity": None, "user_id": user_id})

    base = card_view.envelope_base(identity)
    if identity.get("visibility") == "local_only":
        return JSONResponse(
            {"identity": card_view.local_only_view(base), "user_id": user_id})

    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    def _work():
        try:
            plaintext = envelope.read_envelope(identity, user_id, content_sk)
            inner = json.loads(plaintext.decode("utf-8"))

            # days_with_user is computed live from the server-side anchor.
            # This makes the count auto-increment daily without the agent ever
            # writing it again (the old envelope-embedded value is ignored).
            # Legacy fallback: if no anchor on file, use the embedded value
            # so users that bootstrapped before this migration still see something.
            anchor = identity.get("relationship_started_at")
            if anchor:
                started = _parse_iso_calendar_date(anchor)
                live_days = (
                    max(0, (_dt.datetime.now().date() - started).days)
                    if started else inner.get("days_with_user", 0)
                )
            else:
                live_days = inner.get("days_with_user", 0)

            view = card_view.plaintext_view(
                base, inner, identity, days_with_user=live_days)
            return {"identity": view, "user_id": user_id}
        except (envelope.DecryptFailure, json.JSONDecodeError) as e:
            reason = e.reason if isinstance(e, envelope.DecryptFailure) else f"json: {e}"
            base.update({"decrypt_status": f"error: {reason}"})
            return {"identity": base, "user_id": user_id,
                     "decrypt_errors": [{"reason": reason}]}

    result = await anyio.to_thread.run_sync(_work)
    return JSONResponse(result)
