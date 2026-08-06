"""Hangup memory: distil the FULL call transcript into Memory Garden cards.

After finalize deletes the call's per-turn rows, the normal capture sweep
(``voice_summary.nudge_capture``) only ever sees the ONE summary row — so
memory would distil from the summary, not the conversation. This module runs
the SAME capture interpretation the established pipelines use — the
``capture_prompt_v1`` prompt, ``parse_capture_cards``, the V2
``extraction.cards_to_actions`` mapper and ``memory_core.actions`` — directly
over the client-supplied plaintext transcript, one shot at hangup. The
transcript itself never becomes a chat row and never enters any prompt tail;
Dream consolidates the resulting cards later, unchanged.

Consent is fail-closed on the same gate every capture path honors
(``capture_scheduler._capture_enabled``). Everything here is best-effort: a
False return or a raise makes the finalize route fall back to the normal
summary-window nudge, so memory is degraded, never silently lost.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

import memory_readside_core
import provider_client
from core import envelope as core_envelope
from hosted import config_store as hosted_config_store
from identity import identity_core
from identity import user_naming
from memory import capture_prompt_v1
from memory import memory_core
from model_api_runtime.v2 import extraction as v2_extraction
from proactive import capture_scheduler
from voice import results

log = logging.getLogger("feedling.voice.transcript_memory")

# Same one-shot posture as voice_summary.generate_summary, with extraction.py's
# temperature: capture wants stable, structured card output.
_MAX_TOKENS = 1200
_TIMEOUT_SEC = 60.0
_TEMPERATURE = 0.3
_MAX_ATTEMPTS = 2


def render_transcript_window(
    turns: list[dict], *, user_name: str = "", ai_name: str = ""
) -> str:
    """Render the client transcript in the established capture-window shape.

    Byte-identical line format to the V2 capture handler
    (``worker.py``: ``- {transcript_speaker_label(role,...)}: {text}``) —
    including the hard rule that the literal role never reaches the model
    (the "user:" 标签教坏模型 incident, see ``transcript_speaker_label``).
    Server-side there is no plaintext name source, so names default to "" and
    the labels fall back exactly like V2's hosted path (「对方」/「我」).
    """
    lines: list[str] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        text = str(turn.get("text") or "").strip()
        if not text:
            continue
        role = str(turn.get("role") or "").strip().lower()
        label = user_naming.transcript_speaker_label(
            role, user_name=user_name, ai_name=ai_name
        )
        lines.append(f"- {label}: {text}")
    return "\n".join(lines).strip()


def _memory_context(store, token: str) -> dict:
    """buckets/threads/identity for the capture prompt, per-item degrading.

    Mirrors ``serve_worker._read_memory_context`` (capture variant): every item
    independently degrades to "" — the prompt builder falls back to 「（暂无）」
    — and reads go through the enclave readside with a scoped runtime token
    (the server never decrypts locally). ai_name/user_name have no server-side
    plaintext source and stay "", same as the hosted V2 path.
    """
    ctx = {"ai_name": "", "user_name": "", "buckets": "", "threads": "", "identity": ""}

    def _post(api_key, candidates, *, operation, payload=None):
        return memory_readside_core.post_enclave_readside(
            api_key,
            candidates,
            operation=operation,
            payload=payload,
            runtime_token=token,
        )

    try:
        buckets, threads = memory_core.existing_terms(store, None, post_enclave=_post)
        ctx["buckets"] = ", ".join(str(b) for b in (buckets or []))
        ctx["threads"] = ", ".join(str(t) for t in (threads or []))
    except Exception as e:  # noqa: BLE001 — 单项降级，绝不抛
        log.warning(
            "[voice.transcript_memory] memory terms unavailable user=%s: %s",
            store.user_id[:12],
            str(e)[:120],
        )
    try:
        body, status = identity_core.get_identity(store)
        ident = body.get("identity") if isinstance(body, dict) else None
        if status == 200 and isinstance(ident, dict):
            # E2E 信封：服务器侧只有顶层非敏感明文（如有），否则降级为 ""。
            ctx["identity"] = str(
                ident.get("summary") or ident.get("title") or ""
            ).strip()
    except Exception as e:  # noqa: BLE001 — 单项降级
        log.warning(
            "[voice.transcript_memory] identity unavailable user=%s: %s",
            store.user_id[:12],
            str(e)[:120],
        )
    return ctx


def _build_envelope_factory(store, call_id: str):
    """Deterministic per-card envelope ids: a replayed capture of the same call
    derives the same ``mom_voice_*`` ids (same idea as V2's ``mom_cap_*``
    window-material ids), so an unlikely double-run collapses instead of
    duplicating cards. Raises on a sealing failure — extraction._to_actions
    then fails the whole batch rather than writing half a card."""
    ordinal = {"n": 0}

    def _build(inner: dict) -> dict:
        material = f"{store.user_id}:{call_id}:{ordinal['n']}"
        ordinal["n"] += 1
        item_id = (
            "mom_voice_" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:40]
        )
        payload = json.dumps(inner, ensure_ascii=False).encode("utf-8")
        env, err = core_envelope._build_shared_envelope_for_store(
            store, payload, item_id=item_id
        )
        if env is None:
            raise RuntimeError(f"memory_envelope_build_failed: {err}")
        return env

    return _build


def capture_from_transcript(store, turns: list[dict], *, call_id: str = "") -> bool:
    """Distil the full transcript into memory cards. True = capture handled
    this call's memory (including the legal "nothing worth keeping" outcome);
    False/raise = the caller should fall back to the summary-window nudge.

    Consent gate first, fail-closed, zero model calls when disabled — the same
    ``_capture_enabled`` read every scheduled capture honors.
    """
    if not capture_scheduler._capture_enabled(store):
        log.info(
            "[voice.transcript_memory] capture disabled user=%s", store.user_id[:12]
        )
        return False
    window = render_transcript_window(turns)
    if not window:
        return False

    context_token = results.mint_enclave_token(store.user_id)
    ctx = _memory_context(store, context_token)
    prompt = capture_prompt_v1.build_capture_prompt(
        ai_name=ctx["ai_name"],
        user_name=ctx["user_name"],
        buckets=ctx["buckets"],
        threads=ctx["threads"],
        identity=ctx["identity"],
        window=window,
    )

    runtime = hosted_config_store._load_runtime_provider_config(
        store, None, runtime_token=context_token
    )
    if isinstance(runtime, tuple):
        raise RuntimeError(
            f"voice_capture_provider_unavailable:{runtime[1].get('error')}"
        )
    result = provider_client.reliable_chat_completion(
        runtime,
        [{"role": "user", "content": prompt}],
        max_tokens=_MAX_TOKENS,
        temperature=_TEMPERATURE,
        timeout=_TIMEOUT_SEC,
        include_reasoning=False,
        max_attempts=_MAX_ATTEMPTS,
        base_delay_sec=0.5,
    )
    reply = str((result or {}).get("reply") or "").strip()
    if not reply:
        return False
    # One shot, lenient: keep clean cards, drop dirty ones (the scheduled
    # pipelines earn their strict-then-bounce pass from a durable job; here the
    # fallback path is the summary-window sweep, so a re-ask is not worth it).
    cards, err = capture_prompt_v1.parse_capture_cards(reply, strict=False)
    if err:
        log.warning(
            "[voice.transcript_memory] parse failed user=%s call=%s: %s",
            store.user_id[:12],
            call_id[:24],
            str(err)[:120],
        )
        return False
    occurred_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    actions, added, superseded = v2_extraction.cards_to_actions(
        cards,
        occurred_at=occurred_at,
        # The transcript's per-turn chat rows are deleted at finalize; there is
        # deliberately no chat-row provenance for these cards.
        source_ids=[],
        build_envelope=_build_envelope_factory(store, call_id),
    )
    if not actions:
        # A valid "nothing worth keeping" reply — legal success, no fallback
        # (the summary window would only re-interpret the same call).
        log.info(
            "[voice.transcript_memory] no cards user=%s call=%s",
            store.user_id[:12],
            call_id[:24],
        )
        return True
    # Same server-side application as V2 extraction (serve_worker._apply_memory_
    # actions): api_key=None, prebuilt E2E envelopes, memory sealing/validation
    # inside memory_core.actions, 20-action batches. Fresh token — the model
    # call may have consumed most of the context token's TTL.
    apply_token = results.mint_enclave_token(store.user_id)
    for offset in range(0, len(actions), 20):
        body, status = memory_core.actions(
            store,
            None,
            {"actions": actions[offset : offset + 20]},
            runtime_token=apply_token,
        )
        if status >= 400:
            log.warning(
                "[voice.transcript_memory] actions rejected user=%s call=%s "
                "status=%s body=%s",
                store.user_id[:12],
                call_id[:24],
                status,
                str(body)[:300],
            )
            return False
        if int(body.get("failed_count") or 0) > 0:
            log.warning(
                "[voice.transcript_memory] batch had item failures user=%s "
                "call=%s applied=%s failed=%s",
                store.user_id[:12],
                call_id[:24],
                body.get("applied_count"),
                body.get("failed_count"),
            )
    log.info(
        "[voice.transcript_memory] captured user=%s call=%s added=%d superseded=%d",
        store.user_id[:12],
        call_id[:24],
        added,
        superseded,
    )
    return True
