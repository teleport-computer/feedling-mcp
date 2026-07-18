"""Chat history items, thinking metadata, poll/claim helpers."""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import db
from core import envelope as core_envelope
from core.store import UserStore


MODEL_API_PROVIDER_REASONING_MAX_CHARS = max(400, min(6000, int(os.environ.get("FEEDLING_MODEL_API_PROVIDER_REASONING_MAX_CHARS", "2400"))))


def _sanitize_visible_thinking_summary(text: str) -> str:
    raw = str(text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    blocked = re.compile(
        r"(system prompt|developer message|chain[-\s]*of[-\s]*thought|"
        r"modelUsage|terminal_reason|permission_denials|cache_read|"
        r"cache_creation|session_id|uuid|costUSD|input_tokens|output_tokens)",
        re.IGNORECASE,
    )
    lines: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or blocked.search(line):
            continue
        line = re.sub(r"^[`#>*\-\s]+", "", line).strip()
        if line:
            lines.append(line[:220])
        if len(lines) >= 4:
            break
    return "\n".join(lines).strip()[:700]


def _sanitize_provider_reasoning_text(text: str) -> str:
    raw = str(text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""
    blocked = re.compile(
        r"(system prompt|developer message|api[_\s-]*key|authorization|bearer\s+|"
        r"sk-[A-Za-z0-9]|sk-or-[A-Za-z0-9]|x-api-key|password|secret|session_id|"
        r"input_tokens|output_tokens|cache_creation|cache_read|costUSD)",
        re.IGNORECASE,
    )
    lines: list[str] = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line or blocked.search(line):
            continue
        line = re.sub(r"^[`#>*\-\s]+", "", line).strip()
        if line:
            lines.append(line[:700])
        if len("\n".join(lines)) >= MODEL_API_PROVIDER_REASONING_MAX_CHARS:
            break
    return "\n".join(lines).strip()[:MODEL_API_PROVIDER_REASONING_MAX_CHARS]


CHAT_HISTORY_INLINE_BODY_CT_MAX = int(os.environ.get("FEEDLING_CHAT_HISTORY_INLINE_BODY_CT_MAX", "262144"))
# Fan-out for the per-page R2 body pre-fetch (hydrate_history_page). Bounded so a
# page of images can't open an unbounded number of sockets from a request thread.
_HISTORY_HYDRATE_MAX_WORKERS = int(os.environ.get("FEEDLING_CHAT_HISTORY_HYDRATE_WORKERS", "8"))
# With the lost-turn redelivery backstop live, an expired claim means a second
# agent turn for the same message (double provider burn — the already-answered
# 409 only blocks the double APPEND), so the TTL must sit above the longest
# normal turn. It doubles as the redelivery retry cadence: a crashed turn is
# retried one TTL later, not never.
CHAT_POLL_CLAIM_TTL_SEC = int(os.environ.get("FEEDLING_CHAT_POLL_CLAIM_TTL_SEC", "600"))
# Lost-turn redelivery backstop: how far back a poll may resurrect an
# unanswered user message whose ts the consumer's cursor already passed
# (respawn re-seed, checkpoint advanced before the reply landed, wedge skip).
# 0 disables redelivery entirely. See _pending_chat_messages_for_poll.
CHAT_REDELIVERY_WINDOW_SEC = int(os.environ.get("FEEDLING_CHAT_REDELIVERY_WINDOW_SEC", "3600"))
# Max redelivered (backstop) messages per poll response. The consumer runs one
# agent turn per message (30-90s each), so an uncapped recovery batch would let
# the tail's claims expire before processing (duplicate turns) and outgrow the
# consumer's decrypt fetch. Unclaimed leftovers roll into the NEXT poll
# immediately — a rolling recovery, never a TTL stall. Sized so
# batch × turn-time stays under CHAT_POLL_CLAIM_TTL_SEC.
CHAT_REDELIVERY_BATCH_MAX = int(os.environ.get("FEEDLING_CHAT_REDELIVERY_BATCH_MAX", "5"))
NON_CONVERSATION_USER_SOURCES = frozenset({"verify_ping", "resident_maintenance"})


def is_conversation_user_message(msg: dict) -> bool:
    return (
        isinstance(msg, dict)
        and msg.get("role") == "user"
        and str(msg.get("source") or "") not in NON_CONVERSATION_USER_SOURCES
    )


def _chat_thinking_extra_from_envelope(envelope: dict | None) -> dict:
    if not isinstance(envelope, dict):
        return {}
    out = {
        "thinking_v": str(envelope.get("v", 1)),
        "thinking_id": str(envelope.get("id") or ""),
        "thinking_body_ct": str(envelope.get("body_ct") or ""),
        "thinking_nonce": str(envelope.get("nonce") or ""),
        "thinking_K_user": str(envelope.get("K_user") or ""),
        "thinking_visibility": str(envelope.get("visibility") or "shared"),
        "thinking_owner_user_id": str(envelope.get("owner_user_id") or ""),
        "thinking_enclave_pk_fpr": str(envelope.get("enclave_pk_fpr") or ""),
    }
    if envelope.get("K_enclave"):
        out["thinking_K_enclave"] = str(envelope.get("K_enclave") or "")
    return {k: v for k, v in out.items() if str(v).strip()}


def _chat_caption_extra_from_envelope(envelope: dict | None) -> dict:
    """Image caption (user text) envelope → caption_* extra fields for store.

    Mirrors _chat_thinking_extra_from_envelope; prefix is caption_ instead of
    thinking_.  caption_K_enclave is included only when present because
    _decrypt_envelope requires it — if K_enclave is absent the caption cannot
    be decrypted by the enclave, so we faithfully forward whatever the builder
    produced.
    """
    if not isinstance(envelope, dict):
        return {}
    out = {
        "caption_v": str(envelope.get("v", 1)),
        "caption_id": str(envelope.get("id") or ""),
        "caption_body_ct": str(envelope.get("body_ct") or ""),
        "caption_nonce": str(envelope.get("nonce") or ""),
        "caption_K_user": str(envelope.get("K_user") or ""),
        "caption_visibility": str(envelope.get("visibility") or "shared"),
        "caption_owner_user_id": str(envelope.get("owner_user_id") or ""),
        "caption_enclave_pk_fpr": str(envelope.get("enclave_pk_fpr") or ""),
    }
    if envelope.get("K_enclave"):
        out["caption_K_enclave"] = str(envelope.get("K_enclave") or "")
    return {k: v for k, v in out.items() if str(v).strip()}


_CHAT_THINKING_KINDS = {
    "provider_reasoning",
    "provider_reasoning_summary",
    "runtime_trace",
    "agent_summary",
    "context_summary",
}


def _bounded_chat_metadata(value: object, *, max_len: int = 96) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"[\r\n\t]+", " ", text)[:max_len].strip()


def _boolish_chat_metadata(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _chat_thinking_metadata_from_payload(payload: dict) -> dict:
    """Metadata for a separately encrypted reasoning or trace envelope.

    IO stores and renders this metadata; it does not infer or manufacture
    reasoning. Upstream runtimes must label whether they are sending
    provider-native reasoning, runtime trace, or agent-authored summary.
    """
    if not isinstance(payload, dict):
        return {}
    raw_kind = _bounded_chat_metadata(
        payload.get("thinking_kind") or payload.get("reasoning_kind"),
        max_len=64,
    ).lower()
    out: dict = {}
    if raw_kind in _CHAT_THINKING_KINDS:
        out["thinking_kind"] = raw_kind
    source = _bounded_chat_metadata(
        payload.get("thinking_source") or payload.get("reasoning_source"),
        max_len=80,
    )
    if source:
        out["thinking_source"] = source
    model = _bounded_chat_metadata(
        payload.get("thinking_model") or payload.get("reasoning_model"),
        max_len=96,
    )
    if model:
        out["thinking_model"] = model
    native = _boolish_chat_metadata(
        payload.get("thinking_native", payload.get("reasoning_native"))
    )
    if native is not None:
        out["thinking_native"] = native
    return out


_CHAT_PLAINTEXT_THINKING_FIELDS = (
    ("provider_reasoning", "provider_reasoning"),
    ("reasoning_text", "provider_reasoning_summary"),
    ("reasoning", "provider_reasoning_summary"),
    ("reasoning_summary", "provider_reasoning_summary"),
    ("visible_reasoning", "provider_reasoning_summary"),
    ("thought_summary", "provider_reasoning_summary"),
    ("runtime_trace", "runtime_trace"),
    ("thinking_summary", "agent_summary"),
    ("thinking", "provider_reasoning_summary"),
)


def _chat_plaintext_thinking_from_payload(payload: dict) -> tuple[str, dict, str]:
    """Compatibility bridge for callers that post reasoning as plaintext.

    The preferred /v1/chat/response contract is a separately encrypted
    `thinking_envelope`. Some resident consumers already have provider-native
    reasoning as plaintext and post it as `reasoning_text`; accept that shape by
    sealing it server-side so iOS still sees the canonical thinking_* metadata.
    """
    if not isinstance(payload, dict):
        return "", {}, ""
    for field, default_kind in _CHAT_PLAINTEXT_THINKING_FIELDS:
        raw = payload.get(field)
        if raw is None:
            continue
        text = str(raw or "").strip()
        if not text:
            continue
        if field == "thinking_summary":
            text = _sanitize_visible_thinking_summary(text)
        else:
            text = _sanitize_provider_reasoning_text(text)
        if not text:
            return "", {}, ""
        metadata = _chat_thinking_metadata_from_payload(payload)
        metadata.setdefault("thinking_kind", default_kind)
        metadata.setdefault("thinking_source", f"chat_response.{field}")
        if field == "provider_reasoning" and "thinking_native" not in metadata:
            metadata["thinking_native"] = True
        return text, metadata, field
    return "", {}, ""


def _chat_plaintext_thinking_extra_for_store(store: UserStore, payload: dict) -> dict:
    text, metadata, field = _chat_plaintext_thinking_from_payload(payload)
    if not text:
        return {}
    envelope, err = core_envelope._build_shared_envelope_for_store(store, text.encode("utf-8"))
    if envelope is None:
        print(
            f"[chat:{store.user_id}] plaintext_thinking_envelope_failed "
            f"field={field} detail={err}"
        )
        return {
            "thinking_error": "plaintext_envelope_failed",
            "thinking_error_detail": str(err or "")[:160],
        }
    extra = _chat_thinking_extra_from_envelope(envelope)
    extra.update(metadata)
    return extra


def _is_r2_pointer(m: dict) -> bool:
    """A row whose heavy body_ct lives in R2 (see db._is_chat_file_pointer)."""
    return bool(m.get("body_key")) and m.get("body_ct") is None


def _body_omit_decision(m: dict, *, include_image_body: bool) -> tuple[bool, str, int]:
    """(omit?, reason, body_ct_len) for one message. Shared by the page pre-fetch
    and the item renderer so the two can never disagree about which bodies this
    page delivers. A pointer's length is read off the row (``body_ct_len``), so
    the omit path can report a size without fetching from R2."""
    is_pointer = _is_r2_pointer(m)
    body_ct_len = int(m.get("body_ct_len") or 0) if is_pointer else len(m.get("body_ct") or "")
    if not include_image_body:
        if m.get("content_type", "text") == "image":
            return True, "image_body", body_ct_len
        if body_ct_len > CHAT_HISTORY_INLINE_BODY_CT_MAX:
            return True, "large_body_ct", body_ct_len
    return False, "", body_ct_len


def hydrate_history_page(msgs: list[dict], *, include_image_body: bool) -> list[dict]:
    """Pre-fetch — CONCURRENTLY — the R2 bodies this page will actually deliver.

    Images live in R2 alongside files, so a page carrying N of them would
    otherwise cost N SERIAL round-trips (~50-200ms each) inside the request, all
    from ``_chat_history_item``'s lazy hydrate. Bodies this page omits are never
    fetched. The lazy hydrate stays in place as the correctness backstop: it
    no-ops on a doc this pre-fetch already filled, and still covers a row that
    failed here (or any caller that skips this function)."""
    targets = [
        i for i, m in enumerate(msgs)
        if _is_r2_pointer(m)
        and not _body_omit_decision(m, include_image_body=include_image_body)[0]
    ]
    if len(targets) < 2:
        return msgs  # 0 or 1 body → the serial lazy path is already optimal

    out = list(msgs)
    workers = min(len(targets), _HISTORY_HYDRATE_MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                db.hydrate_chat_file_body,
                str(msgs[i].get("owner_user_id") or ""),
                dict(msgs[i]),
            ): i
            for i in targets
        }
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                out[i] = fut.result()
            except Exception as e:  # noqa: BLE001
                # Leave the pointer in place — the lazy hydrate downstream retries
                # it serially, and a genuinely absent body degrades that one item.
                print(f"[chat/history] r2_prefetch_failed id={msgs[i].get('id')} err={e}")
    return out


def _chat_history_item(m: dict, *, include_image_body: bool = True) -> dict:
    item = dict(m)
    # iOS ChatMessage.content is non-optional. v1 envelope messages are
    # ciphertext-only at rest and may omit plaintext `content`; always
    # include an empty string so Decodable succeeds and client-side decrypt
    # can populate content later.
    item.setdefault("content", "")

    content_type = item.get("content_type", "text")
    is_pointer = _is_r2_pointer(item)
    should_omit_body, body_omitted_reason, body_ct_len = _body_omit_decision(
        item, include_image_body=include_image_body
    )

    if should_omit_body:
        item["body_ct_len"] = body_ct_len
        item["body_omitted"] = True
        item["body_omitted_reason"] = body_omitted_reason
        for key in ("body_ct", "nonce", "K_user", "K_enclave", "body_key"):
            item.pop(key, None)
    else:
        # Body is being delivered — if it lives in R2, fetch it now. This is the
        # per-page read exit (bounded by the history limit), NOT the full ring, so
        # a bulk/metadata-only load never downloads every historical file.
        if is_pointer:
            item = dict(db.hydrate_chat_file_body(str(item.get("owner_user_id") or ""), item))
            item.setdefault("content", "")
        if content_type == "image" or body_ct_len > CHAT_HISTORY_INLINE_BODY_CT_MAX:
            item["body_ct_len"] = body_ct_len
            item["body_omitted"] = False

    role = item.get("role")
    if role == "openclaw":
        item["sender"] = "assistant"
        item["is_from_openclaw"] = True
    elif role == "user":
        item["sender"] = "user"
        item["is_from_openclaw"] = False
    elif role == "system":
        # 老版 iOS 的 sender 解码不能见到未知值；新版按 role=="system" 渲染
        item["sender"] = "assistant"
        item["is_from_openclaw"] = False
    return item


def _parse_consumer_id(headers, query) -> str:
    """Framework-neutral: stable responder id from headers/query mappings.

    Both Flask (request.headers/request.args) and ASGI (request.headers/
    request.query_params) pass case-insensitive-header + .get()-query objects,
    so the ASGI poll route reuses this exact parsing (plan §9.1)."""
    raw = (
        headers.get("X-Feedling-Consumer-Id")
        or query.get("consumer_id")
        or headers.get("X-Feedling-Consumer")
        or "anonymous"
    )
    consumer = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(raw).strip())[:160].strip("-")
    return consumer or "anonymous"


def _parse_bool_arg(query, name: str, default: bool = True) -> bool:
    """Framework-neutral bool query arg (shared by Flask + ASGI poll routes)."""
    raw = query.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _float_meta(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _chat_message_claimable(msg: dict, consumer_id: str, now: float) -> bool:
    if msg.get("role") != "user":
        return False
    if msg.get("reply_status") == "replied" or msg.get("reply_message_id"):
        return False
    claimed_by = str(msg.get("reply_claimed_by") or "").strip()
    expires_at = _float_meta(msg.get("reply_claim_expires_at"), 0.0)
    return (not claimed_by) or claimed_by == consumer_id or expires_at <= now


def _redelivery_floor(store: UserStore, now: float) -> float:
    """Exclusive ts lower bound for redelivering messages behind the cursor.

    A message with ts <= floor is never redelivered. The floor is the later of
    the window edge (now - CHAT_REDELIVERY_WINDOW_SEC) and the newest REPLIED
    visible user message: if the conversation already moved past an unanswered
    message (the user re-asked and got an answer), a late reply to the old one
    would land out of order — it is superseded, not lost. Synthetic
    verify_ping rows and resident-maintenance prompts are not conversation;
    their replies don't supersede anything. Caller holds store.chat_lock.
    """
    if CHAT_REDELIVERY_WINDOW_SEC <= 0:
        return float("inf")
    floor = now - CHAT_REDELIVERY_WINDOW_SEC
    for msg in store.chat_messages:
        if not is_conversation_user_message(msg):
            continue
        if msg.get("reply_status") == "replied" or msg.get("reply_message_id"):
            floor = max(floor, _float_meta(msg.get("ts"), 0.0))
    return floor


def _pending_chat_messages_for_poll(
    store: UserStore,
    *,
    since: float,
    consumer_id: str,
    claim: bool,
) -> list[dict]:
    now = time.time()
    claimed: list[dict] = []
    redelivered = 0
    with store.chat_lock:
        redelivery_floor = _redelivery_floor(store, now)
        for msg in store.chat_messages:
            ts = _float_meta(msg.get("ts"), 0.0)
            if ts <= since:
                if redelivered >= CHAT_REDELIVERY_BATCH_MAX:
                    continue  # budget spent — leftovers roll into the next poll unclaimed
                # Redelivery backstop: an unanswered turn the cursor already
                # passed is still deliverable while it's inside the window and
                # on the unanswered tail (see _redelivery_floor). verify_ping
                # probes are excluded — verify_loop GC'd theirs long ago and a
                # late reply would only 409. Unlike the fresh path below, a
                # LIVE claim blocks redelivery even to its own consumer:
                # re-handing the message to its claimer on every poll while
                # the turn is still running would double-burn; retry waits for
                # TTL expiry instead.
                if ts <= redelivery_floor:
                    continue
                if str(msg.get("source") or "") in NON_CONVERSATION_USER_SOURCES:
                    continue
                claimed_by = str(msg.get("reply_claimed_by") or "").strip()
                expires_at = _float_meta(msg.get("reply_claim_expires_at"), 0.0)
                if claimed_by and expires_at > now:
                    continue
            # Gate on role / already-replied / claimable from cache (these don't
            # race the way the claim itself does). For a claiming poll the
            # cache's claimed_by read is only an early filter — the authoritative
            # decision is the DB CAS below.
            if not _chat_message_claimable(msg, consumer_id, now):
                continue
            if not claim:
                # read-only peek — hydrate an R2-offloaded file body so the
                # delivered message carries its ciphertext (claim path below gets
                # this via chat_try_claim_reply).
                claimed.append(db.hydrate_chat_file_body(store.user_id, dict(msg)))
                if ts <= since:
                    redelivered += 1
                continue
            # The claim must be atomic in the DB, not decided from this worker's
            # cache — otherwise two workers polling the same reply would each read
            # "unclaimed" and both deliver it. chat_try_claim_reply is a
            # conditional UPDATE; only the winner gets the merged doc back.
            msg_id = str(msg.get("id") or "")
            if not msg_id:
                continue
            fields = {
                "reply_claimed_by": consumer_id,
                "reply_claimed_at": f"{now:.3f}",
                "reply_claim_expires_at": f"{now + max(10, CHAT_POLL_CLAIM_TTL_SEC):.3f}",
            }
            # Backstop (ts <= since) claims are strict: the cache-side checks
            # above (live claim, _redelivery_floor) may be stale on this
            # worker, so the CAS itself re-decides both authoritatively — see
            # chat_try_claim_reply's redelivery mode.
            merged = db.chat_try_claim_reply(
                store.user_id, msg_id, consumer_id, now, fields,
                redelivery=ts <= since,
            )
            if merged is None:
                continue  # lost the claim to another consumer/worker — skip
            msg.update(fields)  # keep this worker's cache copy consistent
            claimed.append(dict(merged))
            if ts <= since:
                redelivered += 1
    return claimed
