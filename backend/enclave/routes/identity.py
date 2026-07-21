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

# Pure-stdlib policy module (no DB deps) — safe to import inside the enclave, and
# the reason this route can't drift from the backend's writable-field list again.
from identity import card_policy

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

    v = int(identity.get("v", 0))
    base = {
        "v": v,
        "created_at": identity.get("created_at"),
        "updated_at": identity.get("updated_at"),
    }
    # P5 concurrency baseline (Task 3's outer replaced_at, stamped only by full
    # init/replace) — outer field, not inside the ciphertext, so it's available even
    # before decrypt. Forwarded additively/guarded-truthy (older cards predate it) so
    # the resident consumer can refresh its baseline after an identity_base_stale
    # conflict (Task 5).
    if identity.get("replaced_at"):
        base["replaced_at"] = identity.get("replaced_at")
    if identity.get("visibility") == "local_only":
        base.update({
            "visibility": "local_only",
            "decrypt_status": "local_only_agent_cannot_read",
        })
        return JSONResponse({"identity": base, "user_id": user_id})

    content_sk, err_response = await content_sk_or_503()
    if err_response is not None:
        return err_response

    def _work():
        try:
            plaintext = envelope.decrypt_envelope(identity, user_id, content_sk)
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

            base.update({
                "agent_name": inner.get("agent_name"),
                "self_introduction": inner.get("self_introduction"),
                "dimensions": inner.get("dimensions", []),
                "days_with_user": live_days,
                "category": inner.get("category", ""),
                "signature": inner.get("signature", []),
                "visibility": identity.get("visibility", "shared"),
                "decrypt_status": "ok",
            })
            # Remaining writable profile fields: forward only when present and
            # non-empty so the response shape stays additive (no empty keys added
            # for older cards that predate a field). These feed the read-modify-write
            # merge in identity.profile_patch / dimension_nudge, which rebuilds the
            # card from THIS response and re-encrypts it — so a field missing here is
            # not just hidden, it is ERASED on the next partial update.
            #
            # Driven off card_policy's canonical list rather than hand-listed: the
            # hand-listed version covered 4 of these and silently dropped 5, taking
            # the user-authored custom_persona_prompt with it.
            for key in card_policy.PROFILE_FIELDS:
                if key in base:
                    continue  # already set unconditionally above
                if inner.get(key):
                    base[key] = inner.get(key)
            return {"identity": base, "user_id": user_id}
        except (envelope.DecryptFailure, json.JSONDecodeError) as e:
            reason = e.reason if isinstance(e, envelope.DecryptFailure) else f"json: {e}"
            base.update({"decrypt_status": f"error: {reason}"})
            return {"identity": base, "user_id": user_id,
                     "decrypt_errors": [{"reason": reason}]}

    result = await anyio.to_thread.run_sync(_work)
    return JSONResponse(result)
