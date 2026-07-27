"""Framework-neutral Extended Perception route operations (ASGI-migration plan §5.3).

A pure relocation of the Flask ``/v1/perception/*`` route bodies so both the Flask
adapter (``perception.routes``) and the native FastAPI router
(``perception.routes_asgi``) share one implementation and return byte-identical
responses. No ``flask.request`` here — every function takes the already-resolved
store, the already-parsed params (and, for ``/report``, the caller's api key) as
explicit arguments, and delegates to ``perception.service`` (the business logic).

E2E boundary (unchanged): perception signals/photos are v1 E2E envelopes. The
server NEVER decrypts them in this process EXCEPT via the enclave.

  - ``report`` writes encrypted perception. On the ingress-v2 path it may call the
    ENCLAVE to decrypt sensitive signal envelopes inside the trusted boundary —
    exactly as Flask did — forwarding the caller's ``api_key`` verbatim to
    ``service.ingest_snapshot_v2`` (which owns the enclave decrypt call). No
    plaintext is produced here; decryption happens inside the enclave.
  - ``photo_evaluate`` stores the encrypted image envelope (ciphertext) as-is; it
    performs NO decryption and makes NO enclave call.
  - ``photo_content`` returns JSON metadata + a ``decrypt_path`` pointer to the
    enclave's frame-decrypt endpoint; it performs NO decryption and makes NO
    enclave call. Pixels are decrypted later, by the enclave, on that path.

Every function returns ``(body, status)`` — a JSON-able dict and an HTTP status —
so the two adapters render identical ``jsonify`` / ``JSONResponse`` bodies. The
``snapshot`` read has no error branch, so it always returns status 200.

All store / service / enclave work is blocking, so ASGI callers run these through
``threadpool.run_db`` off the event loop (plan §5.2).
"""

from __future__ import annotations

from typing import Any

from . import service


def report(store, payload: dict, *, api_key: str | None) -> tuple[dict, int]:
    """Single multiplexed ingest. Mirrors the Flask ``/report`` body exactly.

    Body may carry any of ``context_snapshot`` / ``items`` / ``config``; at least
    one must be present (else 400). ``context_snapshot`` always takes the v2
    ingest (which forwards ``api_key`` to the enclave for sensitive-signal
    decrypt) — see the hotfix note below.
    """
    user_store = store
    uid = user_store.user_id
    payload = payload or {}
    results: dict = {}
    provided = False
    status = 200

    cs = payload.get("context_snapshot")
    if isinstance(cs, list) and cs:
        provided = True
        # HOTFIX 2026-07-25: context_snapshot ALWAYS takes the v2 ingest (which
        # owns the enclave decrypt of sensitive-signal envelopes). The iOS
        # report contract is fully v2-encrypted (location/calendar/playback/
        # health ride E2E envelopes) regardless of which chat runtime the user
        # is on — decryption is a data-integrity concern, not a runtime-lane
        # concern. PR #107 tied this fork to the chat runtime fence
        # (perception_ingress_runtime_v2_enabled); every resident-chat user
        # (≈ all of prod) fell to the legacy no-decrypt path, so agents saw
        # null location/calendar/playback while freshness timestamps kept
        # advancing (usr_450e report; traces showed zero perception:* enclave
        # decrypts fleet-wide). The fence keeps governing WAKE-lane forks
        # (e.g. the photo_added differ split in service.photo_evaluate) —
        # just not decrypt.
        results.update(service.ingest_snapshot_v2(
            uid,
            cs,
            client_ts=payload.get("client_ts"),
            api_key=api_key,
        ))

    items = payload.get("items")
    if isinstance(items, dict) and items:
        provided = True
        item_results: dict = {}
        for kind, rows in items.items():
            out, code = service.items_ingest(uid, str(kind), rows)
            item_results[kind] = out
            if code != 200:
                status = 400  # surface rejected/malformed collection uploads, don't 200 them
        results["items"] = item_results

    config = payload.get("config")
    if isinstance(config, dict) and config:
        provided = True
        results["config"] = service.set_config(uid, config)

    if not provided:
        return {"error": "non-empty context_snapshot / items / config required"}, 400
    return {"results": results}, status


def snapshot(store) -> tuple[dict, int]:
    """Current authorized+fresh state for the agent. Always 200 (no error branch)."""
    return service.snapshot(store.user_id), 200


def photo_evaluate(store, payload: dict) -> tuple[dict, int]:
    """Single-step photo ingest: metadata + (if usable) the encrypted image.

    Stores ciphertext only; no decryption / no enclave call here.
    """
    p = payload or {}
    return service.photo_evaluate(
        store.user_id,
        p.get("metadata") or {},
        p.get("content_envelope"),
        p.get("exif_gps"),
        p.get("meta_envelope"),
    )


def photos_recent(store, limit_raw: Any) -> tuple[dict, int]:
    limit = int(limit_raw if limit_raw is not None else 20)
    return service.photos_recent(store.user_id, limit=limit)


def photo_content(store, photo_id: str) -> tuple[dict, int]:
    """Permission + status gate for one confirmed photo. Returns JSON metadata +
    a ``decrypt_path`` to the enclave's frame-decrypt endpoint. No plaintext held
    here — only the enclave decrypts pixels, on that path."""
    return service.photo_content(store.user_id, photo_id)


def items_recent(store, kind: str, limit_raw: Any) -> tuple[dict, int]:
    limit = int(limit_raw if limit_raw is not None else 20)
    return service.items_recent(store.user_id, kind, limit=limit)


def _app_event_params(query) -> tuple[str, str | None, str | None]:
    """Extract (app, category, client_ts) from an iOS Shortcut GET.

    ALL params (incl. the api key, already consumed by auth) arrive in the query
    string. The fallback order (``app``/``bundle_id``, ``ts``/``client_ts``) lives
    HERE so both endpoints and every adapter extract identically. ``query`` is a
    mapping with ``.get`` (Starlette ``query_params``; a plain dict in tests)."""
    app = query.get("app") or query.get("bundle_id") or ""
    category = query.get("category")
    client_ts = query.get("ts") or query.get("client_ts")
    return app, category, client_ts


def app_open(store, query) -> tuple[dict, int]:
    """Record one app-open event from the iOS Shortcut GET."""
    app, category, client_ts = _app_event_params(query)
    return service.app_open(store.user_id, app, category=category, client_ts=client_ts)


def app_close(store, query) -> tuple[dict, int]:
    """Record one app-close event from the iOS Shortcut GET (the automation's
    "is closed" trigger)."""
    app, category, client_ts = _app_event_params(query)
    return service.app_close(store.user_id, app, category=category, client_ts=client_ts)
