"""Shared readside memory core for HTTP routes and hosted agent tools."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
import memory_search_contract as search_contract

from core import envelope as core_envelope
from enclave import readside as enclave_readside
from memory import service as memory_service
from memory import recall_metadata
from memgarden import timestamps as memory_timestamps


MEMORY_READSIDE_DEFAULT_HARD_MAX = 1000

_INACTIVE_STATUSES = {
    "archived",
    "deleted",
    "superseded",
}

_TIME_FIELDS = (
    "last_referenced_at",
    "last_active",
    "updated_at",
    "occurred_at",
    "created_at",
)


def _status(moment: dict) -> str:
    return str(moment.get("status") or "active").strip().lower() or "active"


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _time_key(moment: dict) -> tuple[bool, datetime]:
    for key in _TIME_FIELDS:
        value = str(moment.get(key) or "").strip()
        if value:
            return memory_timestamps.sort_key(value)
    return memory_timestamps.sort_key("")


def _has_time_value(moment: dict) -> bool:
    return any(str(moment.get(key) or "").strip() for key in _TIME_FIELDS)


def _time_ts(moment: dict) -> float:
    valid, parsed = _time_key(moment)
    return parsed.timestamp() if valid else float("-inf")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def _decay_multiplier(moment: dict) -> float:
    importance = _float(moment.get("importance"), 0.5)
    if not _has_time_value(moment):
        return 1.0
    last_ref_ts = _time_ts(moment)
    age_days = max(0.0, (_now_ts() - last_ref_ts) / 86400.0)
    # Backend cannot see encrypted bucket, so v1 uses the fact-ish default here.
    # Relationship-specific half-life can be revisited if a safe plaintext class
    # is introduced later.
    half_life = 180.0 if importance >= 0.8 else 90.0
    decay = max(0.0, min(1.0, age_days / half_life))
    return 1.0 - decay


def memory_available(
    moment: dict,
    owner_user_id: str,
    *,
    include_archived: bool = False,
    include_superseded: bool = False,
) -> bool:
    if not isinstance(moment, dict):
        return False
    if moment.get("owner_user_id") != owner_user_id:
        return False
    if moment.get("visibility") == "local_only":
        return False
    if (
        not moment.get("K_enclave")
        and moment.get("body") is None
        and moment.get("body_b64") is None
    ):
        return False
    status = _status(moment)
    if status == "superseded" and not include_superseded:
        return False
    if status in {"archived", "deleted"} and not include_archived:
        return False
    if status in _INACTIVE_STATUSES and status not in {"archived", "superseded"}:
        return False
    if memory_service._memory_is_archived(moment) and not include_archived:
        return False
    return True


def memory_score(moment: dict) -> float:
    importance = _float(moment.get("importance"), 0.5)
    open_bonus = 0.1 if moment.get("is_open_thread") is True else 0.0
    return round(open_bonus + importance * _decay_multiplier(moment), 4)


def ambient_score(moment: dict) -> float:
    importance = _float(moment.get("importance"), 0.5)
    pulse = _float(moment.get("pulse"), 0.3)
    # Recency is a tie-breaker; importance * pulse is the actual ambient weight.
    return round(importance * pulse, 6)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def readside_hard_max() -> int:
    hard_max = _env_int("FEEDLING_MEMORY_READSIDE_HARD_MAX", MEMORY_READSIDE_DEFAULT_HARD_MAX)
    return max(1, hard_max)


def effective_readside_limit(value: Any | None = None) -> int:
    """Return the effective recall candidate window.

    v1 defaults to a full lightweight index. HARD_MAX is a safety valve, not a
    product recall-window knob.
    """
    if value is None or str(value).strip() == "":
        requested = 0
    else:
        try:
            requested = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid limit") from exc
        if requested < 0:
            raise ValueError("invalid limit")
    hard_max = readside_hard_max()
    if requested == 0:
        return hard_max
    return max(1, min(requested, hard_max))


def readside_candidates(
    moments: list,
    owner_user_id: str,
    *,
    limit: int | None = None,
    ambient: bool = False,
    ambient_top_n: int | None = None,
    exact_query: bool = False,
) -> tuple[list[dict], int]:
    candidates = [
        dict(moment, score=memory_score(moment))
        for moment in moments
        if memory_available(moment, owner_user_id)
    ]
    if ambient:
        candidates.sort(
            key=lambda m: (
                ambient_score(m),
                _time_ts(m),
                str(m.get("id") or ""),
            ),
            reverse=True,
        )
    else:
        candidates.sort(
            key=lambda m: (
                memory_score(m),
                _time_ts(m),
                str(m.get("id") or ""),
            ),
            reverse=True,
        )
    if exact_query:
        # Search sends the complete corpus for one global enclave ranking;
        # HARD_MAX only bounds the result count and enclave decrypt chunks.
        return candidates, len(candidates)
    capped_limit = int(ambient_top_n or 0) if ambient and ambient_top_n else effective_readside_limit(limit)
    if capped_limit <= 0:
        capped_limit = effective_readside_limit(limit)
    capped_limit = min(capped_limit, readside_hard_max())
    return candidates[:capped_limit], len(candidates)


def post_enclave_readside(
    api_key: str | None,
    candidates: list[dict],
    *,
    operation: str,
    payload: dict | None = None,
    runtime_token: str | None = None,
) -> dict:
    enclave_url = os.environ.get("FEEDLING_ENCLAVE_URL", "").rstrip("/")
    if not enclave_url:
        raise RuntimeError("enclave_unavailable")
    # Runtime V2 workers authenticate with a scoped runtime token and
    # carry NO per-user api_key. The enclave accepts either credential and resolves
    # the caller from it (enclave_app._forward_auth_headers / _whoami_cached), so
    # forward the runtime token when there's no api_key. Without this, every hosted
    # agent's memory read 503s with api_key_unavailable even though the data is
    # present. Prefer the token when present, mirroring the enclave's own forwarding.
    if runtime_token:
        auth_headers = {"X-Feedling-Runtime-Token": runtime_token}
    elif api_key:
        auth_headers = {"X-API-Key": api_key}
    else:
        raise RuntimeError("api_key_unavailable")
    body = dict(payload or {})
    body["moments"] = candidates
    try:
        with httpx.Client(timeout=20, verify=False) as client:
            resp = client.post(
                f"{enclave_url}/v1/memory/{operation}",
                headers=auth_headers,
                json=body,
            )
    except httpx.HTTPError as e:
        raise RuntimeError(f"enclave_error:{type(e).__name__}") from e
    if resp.status_code >= 400:
        if resp.status_code == 413:
            try:
                if resp.json().get("error") == "memory_search_resource_limit":
                    raise search_contract.SearchLimitExceeded()
            except (ValueError, AttributeError):
                pass
        raise RuntimeError(f"enclave_http_{resp.status_code}:{resp.text[:180]}")
    try:
        response = resp.json()
    except ValueError as exc:
        raise RuntimeError("enclave_invalid_readside_response") from exc
    if not isinstance(response, dict):
        raise RuntimeError("enclave_invalid_readside_response")
    return response


def _partition_memory_candidates(
    candidates: list[dict],
    owner_user_id: str,
) -> tuple[list[dict], list[dict], list[str]]:
    plaintext: list[dict] = []
    sealed: list[dict] = []
    invalid_ids: list[str] = []
    for candidate in candidates:
        shape = core_envelope.classify_envelope_shape(candidate)
        if shape in ("plaintext_text", "plaintext_binary"):
            plaintext.append(candidate)
        elif shape == "sealed":
            projection = dict(candidate)
            projection.pop("body", None)
            projection.pop("body_b64", None)
            sealed.append(projection)
        else:
            memory_id = str(candidate.get("id") or "")
            if memory_id:
                invalid_ids.append(memory_id)
    return plaintext, sealed, invalid_ids


def _local_memory_items(
    candidates: list[dict],
    owner_user_id: str,
    *,
    item_builder,
) -> tuple[list[dict], list[str]]:
    items: list[dict] = []
    unavailable_ids: list[str] = []
    for candidate in candidates:
        memory_id = str(candidate.get("id") or "")
        try:
            raw = core_envelope.read_plaintext_envelope_body(
                candidate, owner_user_id=owner_user_id)
            inner = json.loads(raw.decode("utf-8"))
            if not isinstance(inner, dict):
                raise ValueError("memory plaintext is not an object")
            items.append(item_builder(candidate, inner))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            if memory_id:
                unavailable_ids.append(memory_id)
    return items, unavailable_ids


def _ordered_items(items: list[dict], candidates: list[dict], limit: int) -> list[dict]:
    rank = {str(row.get("id") or ""): index for index, row in enumerate(candidates)}
    return sorted(
        (item for item in items if str(item.get("id") or "") in rank),
        key=lambda item: rank[str(item.get("id") or "")],
    )[:limit]


def _public_memory_item(item: dict) -> dict:
    """Strip retired card-classification fields at the backend boundary.

    During a rolling deploy an older enclave may still return these legacy
    keys. Stored rows may also retain them until they are naturally rewritten.
    Neither case may recreate the removed API concept.
    """
    clean = dict(item)
    for key in ("is_sensitive", "sensitivity_class", "sensitive_scope"):
        clean.pop(key, None)
    return clean


def _memory_index_partition(
    api_key: str | None,
    candidates: list[dict],
    owner_user_id: str,
    payload: dict,
    *,
    post,
) -> list[dict]:
    plaintext, sealed, _invalid_ids = _partition_memory_candidates(
        candidates, owner_user_id)
    builder = (
        enclave_readside.build_memory_search_item
        if str(payload.get("query") or "").strip()
        else enclave_readside.build_memory_index_item
    )
    local_items, _local_unavailable = _local_memory_items(
        plaintext, owner_user_id, item_builder=builder)
    local_items = enclave_readside.memory_index_filter_items(local_items, payload)

    sealed_items: list[dict] = []
    if sealed:
        response = post(
            api_key,
            sealed,
            operation="index",
            payload=payload,
        )
        sealed_items = [
            _public_memory_item(item)
            for item in response.get("items", [])
            if isinstance(item, dict)
        ]
    items = _ordered_items(local_items + sealed_items, candidates, int(payload["limit"]))
    for item in items:
        item.pop("_search_content", None)
    return items


def memory_index_core(
    store,
    api_key: str | None,
    payload: dict | None = None,
    *,
    post_enclave: Callable[..., dict] | None = None,
) -> dict:
    payload = dict(payload or {})
    ambient = _bool_payload(payload.get("ambient"))
    ambient_top_n = None
    if payload.get("ambient_top_n") not in (None, ""):
        try:
            ambient_top_n = max(1, int(str(payload.get("ambient_top_n")).strip()))
        except (TypeError, ValueError):
            raise ValueError("invalid ambient_top_n")
    limit = effective_readside_limit(payload.get("limit"))
    query = str(payload.get("query") or "")[:500]
    # ``limit`` bounds results, never the corpus used for global statistics.
    candidates, user_card_count = readside_candidates(
        memory_service._load_moments(store),
        store.user_id,
        limit=limit,
        ambient=ambient,
        ambient_top_n=ambient_top_n,
        exact_query=bool(query.strip()),
    )
    post = post_enclave or post_enclave_readside
    payload_base = {
            "ambient": ambient,
            "bucket": str(payload.get("bucket") or "")[:120],
            "thread": str(payload.get("thread") or "")[:120],
            "limit": limit,
            "query": query,
    }
    if query.strip():
        result = _memory_search(api_key, candidates, store.user_id, payload_base, post=post)
        items = result.pop("items")
    else:
        items = _memory_index_partition(
            api_key,
            candidates,
            store.user_id,
            payload_base,
            post=post,
        )
    return {
        "items": items,
        "limit": limit,
        "truncated": False if query.strip() else user_card_count > len(candidates),
        "user_card_count": user_card_count,
        **(result if query.strip() else {}),
    }


def _memory_search(api_key, candidates, owner_user_id, payload, *, post) -> dict:
    # Do not build plaintext search projections or rank partitions in backend.
    # Strip shadow plaintext from sealed rows using the existing shape guard.
    plain, sealed, invalid = _partition_memory_candidates(candidates, owner_user_id)
    by_id = {str(row.get("id") or ""): row for row in plain + sealed}
    corpus = [by_id[str(row.get("id") or "")] for row in candidates
              if str(row.get("id") or "") in by_id]
    request = {**payload, "search_protocol": search_contract.VERSION}
    search_contract.check_request({**request, "moments": corpus})
    response = post(api_key, corpus, operation="index", payload=request)
    # A successful previous-protocol response has this exact envelope. Missing
    # ranking on that known shape is rolling compatibility, not permission to
    # swallow HTTP/auth/timeouts or unknown/malformed future protocols.
    if (not isinstance(response, dict) or response.get("user_id") != owner_user_id
            or not isinstance(response.get("items"), list)
            or not isinstance(response.get("unavailable_ids"), list)):
        raise RuntimeError("enclave_invalid_readside_response")
    ranking = response.get("ranking", search_contract.LEGACY)
    if ranking not in (search_contract.VERSION, search_contract.LEGACY):
        raise RuntimeError("enclave_invalid_readside_response")
    items = []
    for item in response["items"]:
        if not isinstance(item, dict) or str(item.get("id") or "") not in by_id:
            raise RuntimeError("enclave_invalid_readside_response")
        clean = _public_memory_item(item)
        clean.pop("content", None)
        clean.pop("_search_content", None)
        clean.pop("_bm25_score", None)
        items.append(clean)
    # Preserve enclave BM25 order; _ordered_items would restore importance order.
    return {"items": items[:int(payload["limit"])], "ranking": ranking,
            "unavailable_count": len(invalid) + len(response["unavailable_ids"])}


def _bool_payload(value: Any) -> bool:
    return str(value or "").lower() in {"1", "true", "yes", "on"}


def memory_fetch_core(
    store,
    api_key: str | None,
    payload: dict | None = None,
    *,
    post_enclave: Callable[..., dict] | None = None,
) -> dict:
    payload = dict(payload or {})
    ids = payload.get("ids")
    if not isinstance(ids, list) or any(not isinstance(mid, str) or not mid.strip() for mid in ids):
        raise ValueError("ids must be a list of non-empty strings")
    limit = effective_readside_limit(payload.get("limit"))
    requested_ids = [mid.strip() for mid in ids]
    ids = requested_ids[:limit]
    include_archived = _bool_payload(payload.get("include_archived"))
    include_superseded = _bool_payload(payload.get("include_superseded"))
    moments = memory_service._load_moments(store)
    by_id = {m.get("id"): m for m in moments if isinstance(m, dict)}
    missing_ids: list[str] = []
    unavailable_ids: list[str] = []
    candidates: list[dict] = []
    for memory_id in ids:
        moment = by_id.get(memory_id)
        if not isinstance(moment, dict) or moment.get("owner_user_id") != store.user_id:
            missing_ids.append(memory_id)
            continue
        if not memory_available(
            moment,
            store.user_id,
            include_archived=include_archived,
            include_superseded=include_superseded,
        ):
            unavailable_ids.append(memory_id)
            continue
        candidates.append(moment)
    plaintext, sealed, invalid_ids = _partition_memory_candidates(
        candidates, store.user_id)
    local_items, local_unavailable = _local_memory_items(
        plaintext,
        store.user_id,
        item_builder=enclave_readside.build_memory_fetch_item,
    )
    response = {"items": [], "unavailable_ids": []}
    if sealed:
        response = (post_enclave or post_enclave_readside)(
            api_key,
            sealed,
            operation="fetch",
            payload={
                "ids": [m.get("id") for m in sealed],
                "limit": limit,
            },
        )
    enclave_unavailable = response.get("unavailable_ids") if isinstance(response.get("unavailable_ids"), list) else []
    unavailable_ids.extend(invalid_ids)
    unavailable_ids.extend(local_unavailable)
    unavailable_ids.extend(str(mid) for mid in enclave_unavailable if isinstance(mid, str))
    response_items = [
        _public_memory_item(item)
        for item in response.get("items", [])
        if isinstance(item, dict)
    ]
    items_by_id = {
        item.get("id"): item
        for item in local_items + response_items
        if isinstance(item, dict)
    }
    unavailable_set = {
        str(memory_id)
        for memory_id in unavailable_ids
        if str(memory_id or "").strip()
    }
    unavailable_ids = [
        memory_id for memory_id in ids if memory_id in unavailable_set
    ]
    related_items: list[dict] = []
    related_status = "not_needed"
    source_items = [items_by_id[mid] for mid in ids if mid in items_by_id]
    if any(item.get("threads") or item.get("anchor_memory_ids") or item.get("supersedes")
           for item in source_items):
        try:
            # Reuse the authenticated, lifecycle-filtered index projection;
            # decrypted bodies stay within the existing enclave boundary.
            linked_ids = {mid for item in source_items for key in ("anchor_memory_ids", "supersedes")
                          for mid in recall_metadata.links(item.get(key))}
            neighbors = [m for m in moments if memory_available(
                m, store.user_id, include_superseded=True)]
            # Explicit links win seats before thread discovery. A superseded
            # card is returned only along an explicit link, marked historical.
            neighbors.sort(key=lambda m: (m.get("id") not in linked_ids, str(m.get("id") or "")))
            bound = readside_hard_max()
            neighbor_items = _memory_index_partition(
                api_key, neighbors[:bound], store.user_id, {"limit": bound},
                post=post_enclave or post_enclave_readside)
            related_items = recall_metadata.one_hop(source_items, neighbor_items, cap=7)
            complete_window = {m.get("id") for m in neighbors[:bound]} <= {i.get("id") for i in neighbor_items}
            related_status = "bounded" if (len(neighbors) > bound or len(related_items) > 6
                                             or not complete_window) else "ok"
            if neighbors and not neighbor_items:
                related_status = "unavailable"
            related_items = related_items[:6]
        except RuntimeError:
            # Primary fetch remains useful, but missing relation evidence is
            # explicitly unknown rather than a false claim of no neighbors.
            related_status = "unavailable"
    referenced_ids = {str(mid) for mid in items_by_id.keys() if str(mid or "").strip()}
    if referenced_ids:
        now = _now_iso()
        if any(isinstance(m, dict) and str(m.get("id") or "") in referenced_ids for m in moments):
            # Touch last_referenced_at with a re-read INSIDE memory_lock. This is
            # a nominally read-only fetch, but the old full-list _save_moments
            # reconciles deletes — on a stale snapshot it would drop a card added
            # by a concurrent same-user write during the (up to 20s) enclave
            # round-trip above. Re-read + touch + save closes that window.
            with memory_service.mutation_lock(store):
                fresh = memory_service._load_moments(store)
                for m in fresh:
                    if isinstance(m, dict) and str(m.get("id") or "") in referenced_ids:
                        m["last_referenced_at"] = now
                        m["updated_at"] = now
                memory_service._save_moments(store, fresh)
    return {
        "items": [items_by_id[mid] for mid in ids if mid in items_by_id],
        "related_items": related_items,
        "related_status": related_status,
        "missing_ids": missing_ids,
        "unavailable_ids": unavailable_ids,
        "truncation": {
            "truncated": len(requested_ids) > len(ids),
            "requested_count": len(requested_ids),
            "processed_count": len(ids),
            "omitted_count": max(0, len(requested_ids) - len(ids)),
        },
    }
