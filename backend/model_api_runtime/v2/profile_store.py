"""Content-shape-aware storage contract for the Runtime V2 MEMORY/STYLE profile.

``user_blobs.doc`` is ordinary JSONB.  Only bounded metadata may live outside
the two content records. Each record follows the user's effective content
encryption shape: encrypted tier stores a shared envelope; plaintext tier stores
``body``. The blob CAS and TEE mirror preserve that chosen shape verbatim.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import logging
import math
import re
import threading
from typing import Any, Awaitable, Callable

import db
from core import envelope as core_envelope
from core import store as core_store


log = logging.getLogger("feedling.runtime_v2.profile_store")

PROFILE_BLOB_KIND = "v2_agent_profile"
PROFILE_VERSION = 1
PROFILE_STATES = frozenset({"ok", "pending", "degraded", "empty"})
PROFILE_RETRY_DISPOSITIONS = frozenset(
    {"", "scheduled", "provider_config", "source_change", "terminal"}
)
# These dispositions deliberately stop the ordinary refresh scheduler until an
# operator repairs the metadata.  Keep the rescue CLI and worker scheduler on
# one source of truth: copying these strings into an operator script can leave
# a newly introduced permanent disposition stranded forever.
PROFILE_STUCK_RETRY_DISPOSITIONS = frozenset({"provider_config", "terminal"})
_REJECT_CODE_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,160}$")


class ProfileStorageError(RuntimeError):
    """Content-free profile persistence failure."""


@dataclass(frozen=True)
class ProfilePromptSelection:
    summary: str
    memory: str = ""
    style: str = ""
    used_profile: bool = False
    fallback_reason: str = ""


@dataclass(frozen=True)
class ProfileCasResult:
    status: str
    document: dict
    cas_attempts: int
    recomputations: int


_fallback_lock = threading.Lock()
_fallback_counts: Counter[str] = Counter()


def _record_turn_fallback(reason: str) -> None:
    """Emit one content-free event and increment its process-local counter."""
    safe_reason = str(reason or "unknown")[:80]
    with _fallback_lock:
        _fallback_counts[safe_reason] += 1
        count = int(_fallback_counts[safe_reason])
    log.warning(
        "[v2.profile_store] turn profile fallback reason=%s count=%s",
        safe_reason,
        count,
    )


def profile_turn_fallback_counts() -> dict[str, int]:
    """Snapshot observable strict-read/decrypt fallback counters."""
    with _fallback_lock:
        return dict(_fallback_counts)


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ProfileStorageError(f"invalid_{name}")
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        raise ProfileStorageError(f"invalid_{name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProfileStorageError(f"invalid_{name}") from exc
    if parsed < 0:
        raise ProfileStorageError(f"invalid_{name}")
    return parsed


def _validate_content_field(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise ProfileStorageError(f"invalid_{name}")
    envelope = value.get("envelope")
    if not isinstance(envelope, dict) or not envelope:
        raise ProfileStorageError(f"invalid_{name}_envelope")
    if any(key in envelope for key in ("plaintext", "text", "content")):
        raise ProfileStorageError(f"plaintext_{name}_envelope")
    has_ciphertext = bool(str(envelope.get("body_ct") or ""))
    has_plaintext = isinstance(envelope.get("body"), str)
    if has_ciphertext and has_plaintext:
        raise ProfileStorageError(f"mixed_{name}_content_shape")
    if not has_ciphertext and not has_plaintext:
        raise ProfileStorageError(f"invalid_{name}_content_shape")
    chars = _nonnegative_int(value.get("chars"), f"{name}_chars")
    return {"envelope": deepcopy(envelope), "chars": chars}


def validate_profile_document(value: Any) -> dict:
    """Return a normalized profile document or raise a content-free error."""
    if not isinstance(value, dict):
        raise ProfileStorageError("profile_doc_not_object")
    if value.get("v") != PROFILE_VERSION:
        raise ProfileStorageError("profile_version_invalid")
    state = str(value.get("state") or "")
    if state not in PROFILE_STATES:
        raise ProfileStorageError("profile_state_invalid")
    disabled = value.get("disabled", False)
    if not isinstance(disabled, bool):
        raise ProfileStorageError("profile_disabled_invalid")
    source = value.get("source")
    attempt = value.get("last_attempt")
    if not isinstance(source, dict):
        raise ProfileStorageError("profile_source_invalid")
    if not isinstance(attempt, dict):
        raise ProfileStorageError("profile_last_attempt_invalid")
    reject_code = str(attempt.get("reject_code") or "")
    if not _REJECT_CODE_RE.fullmatch(reject_code):
        raise ProfileStorageError("profile_reject_code_invalid")
    try:
        retry_not_before = float(attempt.get("retry_not_before") or 0.0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ProfileStorageError("profile_retry_not_before_invalid") from exc
    if not math.isfinite(retry_not_before) or retry_not_before < 0:
        raise ProfileStorageError("profile_retry_not_before_invalid")
    retry_disposition = str(attempt.get("retry_disposition") or "")
    if retry_disposition not in PROFILE_RETRY_DISPOSITIONS:
        raise ProfileStorageError("profile_retry_disposition_invalid")
    retry_family = str(attempt.get("retry_family") or "")
    if retry_family not in {
        "",
        "transient",
        "shape",
        "provider_config",
        "source",
        "terminal",
    }:
        raise ProfileStorageError("profile_retry_family_invalid")
    retry_attempts = _nonnegative_int(
        attempt.get("retry_attempts", 0), "last_attempt_retry_attempts"
    )
    if state in {"ok", "empty"} and not reject_code:
        retry_disposition = ""
        retry_family = ""
        retry_attempts = 0
        retry_not_before = 0.0

    normalized = {
        "v": PROFILE_VERSION,
        "state": state,
        "source": {
            "card_count": _nonnegative_int(
                source.get("card_count", 0), "source_card_count"
            ),
            "max_updated_at": str(source.get("max_updated_at") or ""),
            "generated_at": str(source.get("generated_at") or ""),
        },
        "last_attempt": {
            "at": str(attempt.get("at") or ""),
            "reject_code": reject_code,
            "attempts": _nonnegative_int(
                attempt.get("attempts", 0), "last_attempt_attempts"
            ),
            "retry_disposition": retry_disposition,
            "retry_family": retry_family,
            "retry_attempts": retry_attempts,
            "retry_not_before": retry_not_before,
        },
        "disabled": disabled,
    }
    memory = value.get("memory")
    style = value.get("style")
    legacy_user = value.get("user")
    if style is not None and legacy_user is not None:
        raise ProfileStorageError("profile_style_alias_conflict")
    # TODO(profile-style-migration): remove the legacy USER read fallback after
    # every stored profile has completed one successful MEMORY/STYLE redistill.
    style_key = "style" if style is not None else "user"
    style_value = style if style is not None else legacy_user
    if (memory is None) != (style_value is None):
        raise ProfileStorageError("profile_fields_torn")
    if memory is not None:
        normalized["memory"] = _validate_content_field(memory, "memory")
        normalized[style_key] = _validate_content_field(
            style_value,
            style_key,
        )
    if state == "ok" and memory is None:
        raise ProfileStorageError("profile_ok_fields_missing")
    return normalized


def _seal_text(user_id: str, plaintext: str) -> dict:
    store = core_store.get_store(str(user_id))
    envelope, _error = core_envelope._build_shared_envelope_for_store(
        store,
        str(plaintext).encode("utf-8"),
    )
    if envelope is None:
        raise ProfileStorageError("profile_envelope_build_failed")
    return envelope


def build_profile_document(
    user_id: str,
    *,
    state: str,
    source: dict,
    last_attempt: dict,
    memory_text: str | None = None,
    style_text: str | None = None,
    previous: dict | None = None,
    disabled: bool = False,
    seal_text: Callable[[str, str], dict] = _seal_text,
) -> dict:
    """Build one all-or-nothing profile document with shape-routed fields.

    A degraded/pending metadata update may omit both plaintext fields to retain
    the previous winning records. ``seal_text`` is retained as the injection
    parameter name for source compatibility; the production implementation
    returns either a shared envelope or ``body`` according to effective
    ``content_encryption``. Supplying only one field is always a torn write and
    fails before any CAS.
    """
    if (memory_text is None) != (style_text is None):
        raise ProfileStorageError("profile_fields_torn")
    document: dict[str, Any] = {
        "v": PROFILE_VERSION,
        "state": str(state),
        "source": deepcopy(source),
        "last_attempt": deepcopy(last_attempt),
        "disabled": bool(disabled),
    }
    if memory_text is not None and style_text is not None:
        document["memory"] = {
            "envelope": seal_text(str(user_id), memory_text),
            "chars": len(memory_text),
        }
        document["style"] = {
            "envelope": seal_text(str(user_id), style_text),
            "chars": len(style_text),
        }
    elif isinstance(previous, dict):
        if (
            previous.get("memory") is not None
            or previous.get("style") is not None
            or previous.get("user") is not None
        ):
            prior = validate_profile_document(previous)
            if prior.get("memory") is not None:
                document["memory"] = prior["memory"]
                if prior.get("style") is not None:
                    document["style"] = prior["style"]
                else:
                    document["user"] = prior["user"]
    return validate_profile_document(document)


def build_profile_document_patching_fields(
    user_id: str,
    *,
    state: str,
    source: dict,
    last_attempt: dict,
    memory_text: str | None,
    style_text: str | None,
    previous: dict | None,
    disabled: bool | None = None,
    seal_text: Callable[[str, str], dict] = _seal_text,
) -> dict:
    """Write touched fields and byte-preserve untouched content shapes.

    Genesis can derive only MEMORY or only STYLE from one upload.  Requiring it
    to decrypt and reseal the untouched side adds an enclave dependency and can
    change content for data the pass did not own. A missing prior side is
    initialized through ``seal_text`` using the owner's effective content shape
    so the paired-field contract stays atomic.
    """
    prior = (
        validate_profile_document(previous)
        if isinstance(previous, dict) and previous
        else {}
    )
    document: dict[str, Any] = {
        "v": PROFILE_VERSION,
        "state": str(state),
        "source": deepcopy(source),
        "last_attempt": deepcopy(last_attempt),
        "disabled": (
            bool(prior.get("disabled"))
            if disabled is None
            else bool(disabled)
        ),
    }
    if memory_text is not None:
        document["memory"] = {
            "envelope": seal_text(str(user_id), memory_text),
            "chars": len(memory_text),
        }
    elif prior.get("memory") is not None:
        document["memory"] = deepcopy(prior["memory"])
    else:
        document["memory"] = {
            "envelope": seal_text(str(user_id), ""),
            "chars": 0,
        }

    if style_text is not None:
        document["style"] = {
            "envelope": seal_text(str(user_id), style_text),
            "chars": len(style_text),
        }
    elif prior.get("style") is not None:
        document["style"] = deepcopy(prior["style"])
    elif prior.get("user") is not None:
        document["user"] = deepcopy(prior["user"])
    else:
        document["style"] = {
            "envelope": seal_text(str(user_id), ""),
            "chars": 0,
        }
    return validate_profile_document(document)


def _timestamp_rank(value: Any) -> float:
    raw = str(value or "").strip()
    if not raw:
        return float("-inf")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return float("-inf")


def _winner_supersedes(winner: dict, candidate: dict) -> bool:
    """Whether a CAS winner makes our pre-race candidate stale."""
    try:
        winner_doc = validate_profile_document(winner)
        candidate_doc = validate_profile_document(candidate)
    except ProfileStorageError:
        return False
    if candidate_doc.get("memory") is not None and winner_doc.get("memory") is None:
        return False
    winner_source = winner_doc["source"]
    candidate_source = candidate_doc["source"]
    if _timestamp_rank(winner_source["generated_at"]) > _timestamp_rank(
        candidate_source["generated_at"]
    ):
        return True
    return (
        winner_source["card_count"] >= candidate_source["card_count"]
        and _timestamp_rank(winner_source["max_updated_at"])
        >= _timestamp_rank(candidate_source["max_updated_at"])
    )


def update_profile_cas(
    user_id: str,
    recompute: Callable[[dict], dict],
    *,
    allow_freshness_supersede: bool = True,
) -> ProfileCasResult:
    """CAS one profile, recomputing once against the winning race document.

    The callback is invoked again after a stale-snapshot loss; its first result
    is never replayed with a new expected value.  A winner that is already newer
    or covers at least the same garden makes our result obsolete and is kept.
    """
    expected_raw = db.get_blob_strict(str(user_id), PROFILE_BLOB_KIND)
    expected = deepcopy(expected_raw) if isinstance(expected_raw, dict) else {}
    return _update_profile_cas_from_expected(
        str(user_id),
        expected,
        recompute,
        allow_freshness_supersede=allow_freshness_supersede,
    )


def _update_profile_cas_from_expected(
    user_id: str,
    expected_doc: dict,
    recompute: Callable[[dict], dict],
    *,
    allow_freshness_supersede: bool = True,
) -> ProfileCasResult:
    """Internal snapshot form used by the true two-connection race test."""
    expected = deepcopy(expected_doc)
    recomputations = 0
    for cas_attempt in (1, 2):
        candidate = validate_profile_document(recompute(deepcopy(expected)))
        recomputations += 1
        if db.set_blob_if_unchanged(
            str(user_id),
            PROFILE_BLOB_KIND,
            expected,
            candidate,
            insert_if_missing=(expected == {}),
        ):
            return ProfileCasResult(
                status="written",
                document=candidate,
                cas_attempts=cas_attempt,
                recomputations=recomputations,
            )
        winner_raw = db.get_blob_strict(str(user_id), PROFILE_BLOB_KIND)
        winner = deepcopy(winner_raw) if isinstance(winner_raw, dict) else {}
        if (
            allow_freshness_supersede
            and winner
            and _winner_supersedes(winner, candidate)
        ):
            return ProfileCasResult(
                status="superseded",
                document=validate_profile_document(winner),
                cas_attempts=cas_attempt,
                recomputations=recomputations,
            )
        if cas_attempt == 2:
            return ProfileCasResult(
                status="cas_failed",
                document=winner,
                cas_attempts=cas_attempt,
                recomputations=recomputations,
            )
        expected = winner
    raise AssertionError("unreachable")


async def update_profile_cas_async(
    user_id: str,
    recompute: Callable[[dict], Awaitable[dict]],
) -> ProfileCasResult:
    """Async-provider counterpart of :func:`update_profile_cas`.

    A CAS loss awaits ``recompute`` again against the winning document. The
    first provider result is never replayed with a new expected value.
    """

    expected_raw = await asyncio.to_thread(
        db.get_blob_strict,
        str(user_id),
        PROFILE_BLOB_KIND,
    )
    expected = deepcopy(expected_raw) if isinstance(expected_raw, dict) else {}
    recomputations = 0
    for cas_attempt in (1, 2):
        candidate = validate_profile_document(
            await recompute(deepcopy(expected))
        )
        recomputations += 1
        landed = await asyncio.to_thread(
            db.set_blob_if_unchanged,
            str(user_id),
            PROFILE_BLOB_KIND,
            expected,
            candidate,
            insert_if_missing=(expected == {}),
        )
        if landed:
            return ProfileCasResult(
                status="written",
                document=candidate,
                cas_attempts=cas_attempt,
                recomputations=recomputations,
            )
        winner_raw = await asyncio.to_thread(
            db.get_blob_strict,
            str(user_id),
            PROFILE_BLOB_KIND,
        )
        winner = deepcopy(winner_raw) if isinstance(winner_raw, dict) else {}
        if winner and _winner_supersedes(winner, candidate):
            return ProfileCasResult(
                status="superseded",
                document=validate_profile_document(winner),
                cas_attempts=cas_attempt,
                recomputations=recomputations,
            )
        if cas_attempt == 2:
            return ProfileCasResult(
                status="cas_failed",
                document=winner,
                cas_attempts=cas_attempt,
                recomputations=recomputations,
            )
        expected = winner
    raise AssertionError("unreachable")


def select_profile_for_turn(
    user_id: str,
    summary: str,
    *,
    enabled: bool,
    decrypt_envelope: Callable[[dict, str], bytes],
    read_blob: Callable[[str, str], Any] = db.get_blob_strict,
) -> ProfilePromptSelection:
    """Strictly read an enabled profile by field shape or visibly fall back."""
    if not enabled:
        return ProfilePromptSelection(summary=str(summary))
    try:
        raw = read_blob(str(user_id), PROFILE_BLOB_KIND)
    except Exception as exc:  # DB outage must not masquerade as a missing profile.
        reason = f"strict_read_failed:{type(exc).__name__.lower()}"
        _record_turn_fallback(reason)
        return ProfilePromptSelection(
            summary=str(summary), fallback_reason=reason
        )
    if raw is None:
        return ProfilePromptSelection(summary=str(summary), fallback_reason="missing")
    try:
        document = validate_profile_document(raw)
    except ProfileStorageError:
        reason = "invalid_profile_document"
        _record_turn_fallback(reason)
        return ProfilePromptSelection(
            summary=str(summary), fallback_reason=reason
        )
    if document["disabled"]:
        return ProfilePromptSelection(
            summary=str(summary), fallback_reason="disabled"
        )
    if document["state"] != "ok":
        return ProfilePromptSelection(
            summary=str(summary), fallback_reason=f"state:{document['state']}"
        )
    def _read_field(name: str) -> str:
        envelope = document[name]["envelope"]
        if envelope.get("body_ct"):
            return decrypt_envelope(envelope, name).decode("utf-8")
        body = envelope.get("body")
        if isinstance(body, str):
            return body
        raise ProfileStorageError(f"invalid_{name}_content_shape")

    try:
        memory = _read_field("memory")
        # TODO(profile-style-migration): delete the USER fallback after the
        # fleet has naturally rewritten every profile through distillation.
        style_key = "style" if document.get("style") is not None else "user"
        style_field = document[style_key]
        style = _read_field(style_key)
        if len(memory) != document["memory"]["chars"]:
            raise ProfileStorageError("memory_chars_mismatch")
        if len(style) != style_field["chars"]:
            raise ProfileStorageError(f"{style_key}_chars_mismatch")
    except Exception as exc:
        reason = f"decrypt_failed:{type(exc).__name__.lower()}"
        _record_turn_fallback(reason)
        return ProfilePromptSelection(
            summary=str(summary), fallback_reason=reason
        )
    return ProfilePromptSelection(
        summary="",
        memory=memory,
        style=style,
        used_profile=True,
    )
