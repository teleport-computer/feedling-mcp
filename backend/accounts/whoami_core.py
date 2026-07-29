"""Framework-neutral /v1/users/whoami payload (ASGI-migration plan §7 / §9.4).

The whoami response builder, lifted out of the Flask route so the native ASGI
route (plan §9.4, a high-frequency thin read) reuses the exact same payload. No
Flask/FastAPI request object here — the caller resolves the store and passes it
in. Contains a blocking enclave fetch (``enclave._get_enclave_info``, 60s
cached), so ASGI callers must run this on the threadpool, not the event loop.
"""

from __future__ import annotations

from accounts import access as accounts_access
from accounts import registry
from core import enclave
from core.store import UserStore


def whoami_payload(store: UserStore) -> dict:
    """Public material the caller needs to wrap content for itself.

    ``public_key`` — caller's X25519 content pubkey; ``enclave_content_public_key_hex``
    — the live enclave's content pubkey (from cached /attestation);
    ``archive_language`` — registration locale; ``timezone`` — IANA zone (record
    value, or perception-snapshot fallback), omitted when unknown.
    """
    access = accounts_access._access_modes_payload(store)
    resp: dict = {
        "user_id": store.user_id,
        "principal_id": access.get("principal_id", ""),
        "active_route": access.get("active_route", ""),
        "access_modes": access.get("access_modes", []),
    }
    pk = registry._get_user_public_key(store.user_id)
    if pk:
        resp["public_key"] = pk
    info = enclave._get_enclave_info()
    if info:
        resp["enclave_content_public_key_hex"] = info["content_pk_hex"]
        resp["enclave_compose_hash"] = info["compose_hash"]
    archive_language = registry._get_user_archive_language(store.user_id)
    if archive_language:
        resp["archive_language"] = archive_language
    tz = registry._get_user_timezone(store.user_id)
    if not tz:
        try:
            from perception import service as perception_service  # lazy: avoid import cycle
            tz = perception_service.stable_context_timezone(store.user_id)
        except Exception:
            tz = None
    if tz:
        resp["timezone"] = tz
    # v6 加密开关：未设置 == 明文档。显式下发 "off" 而不是省略字段，免得各客户端
    # 自己猜默认值。⚠️ 这里**不**按偏好裁剪 enclave_content_public_key_hex——
    # 现役 iOS 用它封双收件人信封，Phase 3 发版前停发 = 全量写入中断。
    resp["content_encryption"] = registry._get_user_content_encryption(store.user_id) or "off"
    return resp
