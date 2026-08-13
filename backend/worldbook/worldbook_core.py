"""Framework-neutral world book operations (ASGI-migration plan §5.3).

A pure relocation of the Flask ``/v1/worldbook/*`` route bodies so both the
Flask adapter (``worldbook.routes``) and the native FastAPI router
(``worldbook.routes_asgi``) share one implementation and return byte-identical
responses.

E2E boundary (unchanged): world book ``content`` fields are v1 E2E envelopes.
The server NEVER decrypts them. ``list``/``delete`` are plain store operations;
``upsert``/``match`` forward the caller's credential (api key OR runtime token)
to the enclave, which owns decryption + the plaintext content-length cap. These
functions take already-parsed params + the store and the credential as
arguments — they never read ``flask.request`` — so no new server-side plaintext
is ever introduced here.
"""

from __future__ import annotations

from datetime import datetime
import json
import os

from content.content_core import _apply_envelope_fields, _swap_envelope_missing
from core import envelope as core_envelope
import debug_trace
import worldbook_readside_core


def _request_envelope(payload: dict) -> tuple[dict | None, str | None]:
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    nested = payload.get("envelope")
    if nested is not None:
        if not isinstance(nested, dict):
            return None, "envelope must be an object"
        outer_id = str(payload.get("id") or "").strip()
        inner_id = str(nested.get("id") or "").strip()
        if outer_id and inner_id and outer_id != inner_id:
            return None, "top-level id must match envelope id"
        return nested, None
    return payload, None


def _validate_envelope(env: dict, owner_user_id: str) -> str | None:
    # 明文形状的准入（_swap_envelope_missing 只管字段齐不齐，不管准不准）。
    _, shape_err = core_envelope.upload_shape_gate(env, user_id=owner_user_id)
    if shape_err is not None:
        return str(shape_err.get("error") or "envelope_shape_rejected")
    missing = _swap_envelope_missing(env)
    if missing:
        return f"envelope missing {missing}"
    entry_id = str(env.get("id") or "").strip()
    if not entry_id:
        return "id required"
    if str(env.get("visibility") or "") not in {"shared", "local_only"}:
        return "envelope.visibility must be 'shared' or 'local_only'"
    if core_envelope.requires_enclave_key(env):
        return "shared visibility requires K_enclave"
    if env.get("owner_user_id") != owner_user_id:
        return "owner_user_id does not match caller"
    return None


def _validate_content_cap_with_enclave(
    record: dict, *, api_key: str | None, runtime_token: str | None
) -> tuple[dict, int] | None:
    """Fail closed on deploys that have the enclave configured.

    The upsert endpoint receives ciphertext, so it cannot inspect plaintext
    length locally. The enclave decrypt path owns that check; this call makes the
    write path reject over-cap world book content before it is persisted.
    """
    shape = core_envelope.classify_envelope_shape(record)
    if shape in ("plaintext_text", "plaintext_binary"):
        try:
            raw = core_envelope.read_plaintext_envelope_body(
                record, owner_user_id=str(record.get("owner_user_id") or ""))
            inner = json.loads(raw.decode("utf-8"))
            if not isinstance(inner, dict):
                raise ValueError("world book plaintext is not an object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return {"error": "worldbook_validate_failed", "id": record.get("id")}, 400
        if len(str(inner.get("content") or "")) > worldbook_readside_core.WORLD_BOOK_CONTENT_CAP:
            return {
                "error": "content_too_long",
                "id": str(record.get("id") or ""),
                "max_chars": worldbook_readside_core.WORLD_BOOK_CONTENT_CAP,
            }, 400
        return None
    if not os.environ.get("FEEDLING_ENCLAVE_URL", "").strip():
        return None
    try:
        result = worldbook_readside_core.post_enclave_worldbook_match(
            api_key, [record], [], runtime_token=runtime_token)
    except RuntimeError as e:
        return {"error": "worldbook_validate_unavailable", "detail": str(e)}, 503
    rejected = {str(item) for item in result.get("rejected_over_cap") or []}
    entry_id = str(record.get("id") or "").strip()
    if entry_id in rejected:
        return {
            "error": "content_too_long",
            "id": entry_id,
            "max_chars": worldbook_readside_core.WORLD_BOOK_CONTENT_CAP,
        }, 400
    unavailable = {str(item) for item in result.get("unavailable_ids") or []}
    if entry_id in unavailable:
        return {"error": "worldbook_validate_failed", "id": entry_id}, 400
    return None


def list_envelopes(store) -> tuple[dict, int]:
    with store.world_books_lock:
        envelopes = [dict(item) for item in store.world_books]
    return {"envelopes": envelopes}, 200


def upsert(
    store, payload: dict, *, api_key: str | None, runtime_token: str | None
) -> tuple[dict, int]:
    env, parse_error = _request_envelope(payload)
    if parse_error:
        return {"error": parse_error}, 400
    validation_error = _validate_envelope(env or {}, store.user_id)
    if validation_error:
        return {"error": validation_error}, 400

    record = {"id": str(env.get("id") or "").strip(), "updated_at": datetime.now().isoformat()}
    _apply_envelope_fields(record, env)
    cap_error = _validate_content_cap_with_enclave(
        record, api_key=api_key, runtime_token=runtime_token)
    if cap_error:
        body, status = cap_error
        return body, status
    saved = store.upsert_world_book(record)
    return {"id": saved["id"]}, 200


def match(
    store, payload: dict, *, api_key: str | None, runtime_token: str | None
) -> tuple[dict, int]:
    messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
    current = str(payload.get("message") or "").strip()
    if current:
        messages = list(messages) + [{"role": "user", "content": current}]
    with store.world_books_lock:
        world_books = [dict(item) for item in store.world_books]
    if not world_books:
        return {"block": "", "matched_names": [], "rejected_over_cap": [], "unavailable_ids": []}, 200
    parts: list[dict] = []
    index = 0
    while index < len(world_books):
        row = world_books[index]
        shape = core_envelope.classify_envelope_shape(row)
        plaintext = shape in ("plaintext_text", "plaintext_binary")
        end = index + 1
        while end < len(world_books):
            next_shape = core_envelope.classify_envelope_shape(world_books[end])
            if (next_shape in ("plaintext_text", "plaintext_binary")) != plaintext:
                break
            end += 1
        group = world_books[index:end]
        if plaintext:
            entries: list[dict] = []
            unavailable_ids: list[str] = []
            for entry in group:
                try:
                    raw = core_envelope.read_plaintext_envelope_body(
                        entry, owner_user_id=store.user_id)
                    inner = json.loads(raw.decode("utf-8"))
                    if not isinstance(inner, dict):
                        raise ValueError("world book plaintext is not an object")
                    entries.append(inner)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    entry_id = str(entry.get("id") or "")
                    if entry_id:
                        unavailable_ids.append(entry_id)
            result = worldbook_readside_core.build_block(entries, messages)
            result["unavailable_ids"] = unavailable_ids
        else:
            sealed_group = []
            unavailable_ids = []
            for entry in group:
                if core_envelope.classify_envelope_shape(entry) != "sealed":
                    entry_id = str(entry.get("id") or "")
                    if entry_id:
                        unavailable_ids.append(entry_id)
                    continue
                projection = dict(entry)
                projection.pop("body", None)
                projection.pop("body_b64", None)
                sealed_group.append(projection)
            try:
                result = (
                    worldbook_readside_core.post_enclave_worldbook_match(
                        api_key,
                        sealed_group,
                        messages,
                        runtime_token=runtime_token,
                    )
                    if sealed_group
                    else {"block": "", "matched_names": [],
                          "rejected_over_cap": [], "unavailable_ids": []}
                )
            except RuntimeError as e:
                return {"error": "worldbook_match_unavailable", "detail": str(e)}, 503
            result["unavailable_ids"] = [
                *unavailable_ids,
                *(result.get("unavailable_ids") or []),
            ]
        parts.append(result)
        index = end
    result = worldbook_readside_core.merge_match_results(parts)
    block = str(result.get("block") or "")
    matched_names = result.get("matched_names") if isinstance(result.get("matched_names"), list) else []
    if block:
        debug_trace.trace_event(
            store,
            subsystem="worldbook",
            type="worldbook_injected",
            actor="host_agent_runtime",
            summary=f"worldbook injected {len(matched_names)} entries",
            detail={"names": matched_names},
        )
    return {
        "block": block,
        "matched_names": matched_names,
        "rejected_over_cap": result.get("rejected_over_cap") if isinstance(result.get("rejected_over_cap"), list) else [],
        "unavailable_ids": result.get("unavailable_ids") if isinstance(result.get("unavailable_ids"), list) else [],
    }, 200


def delete(store, entry_id_raw) -> tuple[dict, int]:
    entry_id = str(entry_id_raw or "").strip()
    if not entry_id:
        return {"error": "id required"}, 400
    store.delete_world_book(entry_id)
    return {"ok": True}, 200
