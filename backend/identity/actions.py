"""Identity write actions (profile patch / nudge / days set) + executor.

写卡原则：只有蒸馏任务可 identity.replace，其余一切写卡走 profile_patch（局部合并）。
replace/patch 合一（patch+版本参数）是 V2 开放问题，归架构层。——spec 2026-07-22 §3.5
"""

import json
import re
import uuid


from core.store import UserStore

from bootstrap import gates as boot_gates
from core import util as core_util
from core import enclave as core_enclave
from core import envelope as core_envelope
from identity import service as identity_service

def _identity_action_text(value, max_chars: int) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars].strip()


class ListOpConflict(Exception):
    """Raised when a patch specifies more than one operation (legacy
    direct-list assign, add_*, remove_*, replace_*) for the SAME list field
    in a single request — ambiguous intent, rejected rather than guessed at."""


class ListOpBlank(Exception):
    """Raised when an add_*/replace_* op's items are all blank after
    stripping. Deliberately NOT treated as "clear the list" — that stays the
    legacy direct-list-assign behavior only (back-compat), so a caller can't
    accidentally wipe a list field by sending whitespace-only items on the
    new op keys."""


class ListOpTooManyItems(Exception):
    """Raised when an add_* op's merged result (existing items + deduped
    additions), or a replace_*'s deduped item list, would exceed the
    12-item cap. Explicit reject, NOT silent truncation — unlike the
    legacy direct-list-assign path (e.g. bare `signature`, which keeps
    capping via `_clean_list_items`'s `raw[:12]` for back-compat), add_*
    and replace_* are both brand-new op keys with no compat concern, so a
    request that would blow the cap is rejected outright rather than
    silently dropping whichever items didn't fit.

    I4 follow-up (review): this used to only apply to add_* in practice,
    because `apply_list_ops` cleaned add_*/replace_* input through
    `_clean_list_items`, which ALREADY truncates to 12 items via
    `raw[:12]` before either op's own length check ever ran — so e.g. an
    empty list + add of 13 distinct values silently became 12 items and
    never raised. Both ops now clean through the uncapped
    `_clean_list_items_uncapped` first so the length check sees the TRUE
    requested count."""


# field -> (add_key, remove_key, replace_key). Drives apply_list_ops below so
# the four list fields (signature/boundaries/do_not_say/stable_definitions)
# share one merge code path instead of four hand-copied ones — only this
# lookup table is per-field, never the logic. Note "signature" pluralizes to
# "replace_signatures" for the whole-group op (replacing the WHOLE list of
# signature phrases) while add_/remove_ stay singular (one phrase at a time);
# the other three fields are already plural/compound nouns so all three keys
# match the field name.
#
# V2 migration note: pre 分支 backend/capabilities/identity.py 的 patch 能力与
# tool_schema.py 的 identity_patch 参数需在 0727 合并时同步支持这些操作键
# (add_/remove_/replace_ + 四个 list 字段) —— 取本分支超集，别只挑一半迁过去。
_LIST_OP_FIELDS: dict[str, tuple[str, str, str]] = {
    "signature": ("add_signature", "remove_signature", "replace_signatures"),
    "boundaries": ("add_boundaries", "remove_boundaries", "replace_boundaries"),
    "do_not_say": ("add_do_not_say", "remove_do_not_say", "replace_do_not_say"),
    "stable_definitions": (
        "add_stable_definitions", "remove_stable_definitions", "replace_stable_definitions"),
}


def _clean_list_items(raw) -> list[str]:
    """Normalize a raw patch value into a stripped/truncated/blank-filtered
    list[str], the same shape _identity_profile_patch has always produced
    for these fields (max 12 items, 240 chars each, blanks dropped).

    ONLY for the legacy direct-list-assign path (e.g. a bare `signature`
    key) — that path silently truncates to 12 items for back-compat. The
    newer add_*/replace_* op keys must NOT use this: see
    `_clean_list_items_uncapped` and `ListOpTooManyItems`."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [item for item in (_identity_action_text(v, 240) for v in raw[:12]) if item]


def _clean_list_items_uncapped(raw) -> list[str]:
    """Same per-item normalization as `_clean_list_items` (stripped,
    240-char truncated, blanks dropped) but WITHOUT truncating the item
    COUNT to 12. Used by add_*/replace_* so their own 12-item cap check
    sees the caller's TRUE requested count and can reject the whole op
    (`ListOpTooManyItems`) instead of `_clean_list_items` silently
    dropping the overflow before that check ever runs (I4 review)."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [item for item in (_identity_action_text(v, 240) for v in raw) if item]


def validate_list_ops_shape(patch: dict) -> None:
    """Structural (existing-card-INDEPENDENT) half of the list-op validation:
    conflict detection + blank-after-clean detection for add_/replace_ ops.
    Only looks at `patch` — never needs the decrypted card — so callers
    (``_identity_profile_patch``) can run this BEFORE acquiring the
    identity_mutation_lock / doing the enclave read, failing fast on a
    malformed request without touching either. ``apply_list_ops`` below also
    calls this first, so it stays fully self-validating when used standalone
    (e.g. directly from tests).

    Raises:
        ListOpConflict: more than one of {legacy key, add_, remove_,
            replace_} present for the same field in this patch.
        ListOpBlank: an add_/replace_ op's items are all blank after
            stripping (remove has no such guard — removing nothing is a
            harmless no-op, not a clear).
    """
    if not isinstance(patch, dict):
        return
    for field, (add_key, remove_key, replace_key) in _LIST_OP_FIELDS.items():
        present_count = sum((
            field in patch, add_key in patch, remove_key in patch, replace_key in patch,
        ))
        if present_count > 1:
            raise ListOpConflict(field)
        if replace_key in patch and not _clean_list_items_uncapped(patch.get(replace_key)):
            raise ListOpBlank(field)
        if add_key in patch and not _clean_list_items_uncapped(patch.get(add_key)):
            raise ListOpBlank(field)


def apply_list_ops(existing: dict, patch: dict) -> dict:
    """Pure merge for the 4 list-shaped profile fields.

    ``existing`` is the current card's relevant fields (a superset dict is
    fine — only the 4 list fields are read). ``patch`` is the raw action
    patch, which may carry the legacy direct-list key (e.g. ``signature``)
    and/or one of ``add_<field>`` / ``remove_<field>`` / ``replace_<field>``.

    Returns ``{field: merged_list}`` — only for fields the patch actually
    touches; untouched fields are omitted so the caller's own "did this
    field change" bookkeeping stays correct.

    Raises ListOpConflict / ListOpBlank via ``validate_list_ops_shape`` (see
    there for exact conditions) — called first, so this function is fully
    self-validating even when called without the earlier fail-fast check.
    Also raises ListOpTooManyItems if the final result would exceed the
    12-item cap: for add_* that's existing + deduped additions (genuinely
    needs ``existing``, so it can't move into the existing-independent
    pre-check); for replace_* it's the deduped replacement list itself.
    Neither op truncates to fit — see ListOpTooManyItems.
    """
    if not isinstance(existing, dict):
        existing = {}
    if not isinstance(patch, dict):
        patch = {}

    validate_list_ops_shape(patch)

    result: dict[str, list[str]] = {}
    for field, (add_key, remove_key, replace_key) in _LIST_OP_FIELDS.items():
        legacy_present = field in patch
        add_present = add_key in patch
        remove_present = remove_key in patch
        replace_present = replace_key in patch
        if not (legacy_present or add_present or remove_present or replace_present):
            continue

        old_list = existing.get(field) if isinstance(existing.get(field), list) else []
        old_list = [str(item) for item in old_list]

        if legacy_present:
            # Back-compat: unchanged behavior, including "empty list clears
            # the field" — no ListOpBlank here, that's the new op keys only.
            result[field] = _clean_list_items(patch.get(field))
            continue

        if replace_present:
            # Already validated non-blank by validate_list_ops_shape above.
            # I4: clean through the UNCAPPED normalizer + dedupe FIRST, then
            # reject the whole op if the true count exceeds 12 — using
            # `_clean_list_items` here would truncate to 12 items before this
            # check ever saw the real count, silently hiding a 13+-item
            # request instead of rejecting it (same bug class as add_* below).
            cleaned = _clean_list_items_uncapped(patch.get(replace_key))
            deduped = list(dict.fromkeys(cleaned))
            if len(deduped) > 12:
                raise ListOpTooManyItems(field)
            result[field] = deduped
            continue

        if add_present:
            # Already validated non-blank by validate_list_ops_shape above.
            # I4: uncapped clean so the merged-length check below sees the
            # caller's TRUE requested count, not a pre-truncated-to-12 one
            # (see `_clean_list_items_uncapped` / `ListOpTooManyItems`).
            additions = _clean_list_items_uncapped(patch.get(add_key))
            merged = list(old_list)
            for item in additions:
                if item not in merged:
                    merged.append(item)
            if len(merged) > 12:
                raise ListOpTooManyItems(field)
            result[field] = merged
            continue

        # remove_present
        removals = set(_clean_list_items(patch.get(remove_key)))
        result[field] = [item for item in old_list if item not in removals]

    return result


def _identity_plain_for_action(store: UserStore, api_key: str | None,
                               runtime_token: str = "") -> tuple[dict | None, str]:
    # Only pass runtime_token when present, so the api_key path keeps the original
    # 2-arg call shape (mocks/monkeypatches that predate the runtime_token param).
    if runtime_token:
        data, err = core_enclave._enclave_get_json_for_gate(
            "/v1/identity/get", api_key, runtime_token=runtime_token)
    else:
        data, err = core_enclave._enclave_get_json_for_gate("/v1/identity/get", api_key)
    if err:
        return None, err
    if not isinstance(data, dict) or not isinstance(data.get("identity"), dict):
        return None, "identity_not_initialized"
    identity = data["identity"]
    status = identity.get("decrypt_status")
    if status and status != "ok":
        return None, str(status)
    return identity, ""


def _identity_payload_from_plain(identity: dict) -> dict:
    from identity import card_policy
    # Single chokepoint for init / profile_patch / dimension_nudge: run the
    # dimensions through card_policy.sanitize so no non-integer value (BYOK weak
    # models emit 0–1-scale floats like 0.95) is ever re-encrypted into the card.
    # sanitize also drops malformed dims and normalizes to the 0–100 int contract,
    # self-healing an already-poisoned existing card on the next write.
    raw_dims = identity.get("dimensions") if isinstance(identity.get("dimensions"), list) else []
    payload = {
        "agent_name": str(identity.get("agent_name") or "")[:80],
        "self_introduction": str(identity.get("self_introduction") or "")[:1200],
        "dimensions": card_policy.sanitize_identity_card({"dimensions": raw_dims})["dimensions"],
    }
    for key in identity_service._IDENTITY_PROFILE_STRING_FIELDS:
        if key in {"agent_name", "self_introduction"}:
            continue
        if identity.get(key):
            payload[key] = str(identity.get(key) or "")[:1200 if key in {"relationship_anchor", "tone_style", "custom_persona_prompt"} else 240]
    for key in identity_service._IDENTITY_PROFILE_LIST_FIELDS:
        if isinstance(identity.get(key), list):
            payload[key] = [str(item)[:240] for item in identity[key][:12] if str(item or "").strip()]
    return payload


def _save_identity_action_payload(
    store: UserStore,
    payload: dict,
    *,
    existing: dict,
    audit: dict,
    event_type: str,
    relationship_override: dict | None = None,
) -> tuple[dict | None, dict | None, str]:
    # `existing` is a caller-supplied SNAPSHOT of the raw identity blob,
    # taken BEFORE the enclave plaintext read `payload` was merged from (see
    # _load_identity_snapshot_for_write and the two _read_merge_save
    # closures below) — NOT loaded fresh here. This function used to call
    # identity_service._load_identity(store) itself at this point, which was
    # a real bug (Codex C1 follow-up): that load happens AFTER the enclave
    # round trip, so a concurrent write landing between the enclave read and
    # that later load would make `existing` reflect the concurrent write
    # while `payload` was still merged from data before it — the CAS below
    # would then wrongly SUCCEED (expected matches current) and silently
    # clobber the concurrent write instead of catching it. Snapshotting
    # first and threading it through here closes that whole window: ANY
    # write landing after the snapshot — including during the enclave round
    # trip — now makes the CAS fail and the caller retries from a fresh
    # snapshot.
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        item_id=existing.get("id") or None,
    )
    if envelope is None:
        return None, None, err

    now = core_util._now_iso()
    identity = {
        "v": 1,
        "id": envelope.get("id") or existing.get("id") or uuid.uuid4().hex,
        "enclave_pk_fpr": "",
        **core_envelope.envelope_storage_fields(envelope),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        # replaced_at is the P5 concurrency baseline: it is stamped ONLY by
        # full-card writes (identity.init / identity.replace). Partial
        # mutations (profile_patch / dimension_nudge, both routed through
        # this helper) must carry it forward untouched, not drop it — this
        # dict is an explicit field list, not a copy of `existing`, so it
        # must be listed here or the raw blob overwrite in _save_identity
        # would silently erase it.
        "replaced_at": existing.get("replaced_at", ""),
        # Relationship anchor: carried forward from `existing` UNLESS this patch
        # recalibrated it (profile_patch with relationship_days — see
        # _identity_profile_patch). The override is threaded through here so the
        # anchor rewrite rides the SAME CAS write as every other profile field,
        # instead of a separate non-CAS _save_identity (which is the residual the
        # legacy identity.relationship_days_set action still carries).
        "relationship_started_at": (relationship_override or {}).get(
            "relationship_started_at", existing.get("relationship_started_at", "")),
        "relationship_anchor_source": (relationship_override or {}).get(
            "relationship_anchor_source", existing.get("relationship_anchor_source", "")),
        "relationship_anchor_evidence": (relationship_override or {}).get(
            "relationship_anchor_evidence", existing.get("relationship_anchor_evidence", "")),
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    # CAS, not a plain overwrite: `existing` IS the exact blob this payload
    # was merged from (see the parameter comment above — snapshotted by the
    # caller before the enclave read, not re-loaded here). gunicorn runs
    # multiple worker processes (see deploy/docker-compose.yaml);
    # identity_mutation_lock is a process-local threading.Lock, so it cannot
    # stop a DIFFERENT worker process (handling the same user) from writing
    # between our snapshot and our write — including during the enclave
    # round trip. A plain `identity_service._save_identity` here would
    # silently clobber that worker's change (lost update) instead of
    # detecting it. Audit / effect / TEE-requeue side effects below only run
    # once this CAS has actually won.
    if not identity_service._save_identity_cas(store, existing, identity):
        raise identity_service.IdentityWriteConflict()
    boot_gates._log_bootstrap_event(store, event_type, success=True)
    change = identity_service._append_identity_change(store, audit)
    return identity, change, ""


def _resolve_relationship_anchor(days: int, *, trusted_frozen: str | None = None) -> str:
    """Resolve the absolute ``relationship_started_at`` for a relationship_days
    recalibration, resolving the relative day count to a fixed calendar anchor
    exactly once (item 1).

    ``trusted_frozen`` is the FROZEN anchor the V2 producer resolved at ENQUEUE
    time and threaded down here as an EXPLICIT KEYWORD ARGUMENT — from
    worker._write_tool_effect_payload -> capabilities.identity.patch ->
    identity_core.run_actions(trusted_relationship_anchor=...) ->
    _execute_identity_actions -> _execute_identity_action -> _identity_profile_patch.
    It travels the call path, NOT the action dict / request body, so it is trusted
    *by the path it arrived on*, not by whether a date happens to sit in the
    payload. The public ``POST /v1/identity/actions`` handler never passes it, so
    a caller who stuffs ``relationship_started_at`` into the request body cannot
    forge a frozen anchor — on that path ``trusted_frozen`` is None and the value
    is always recomputed from ``days`` (Important 1 / round-4 fix).

    When a trusted value IS supplied by the sink path, use it VERBATIM under two
    defensive bounds (defense-in-depth against a producer bug) — and, crucially,
    once trusted it is returned as-is, never re-aged against the replay day:

      (a) it parses to a real calendar date that is NOT in the future
          (``frozen <= today``); and
      (b) the accompanying ``days`` is within ``[0, MAX_RELATIONSHIP_DAYS]``.

    Returning the frozen date verbatim (instead of an ``0 <= today-frozen <= MAX``
    age check) is what makes a delayed/retried replay idempotent: at
    ``relationship_days == MAX`` the enqueue-day anchor is ``today-MAX``, so a
    next-day replay would have ``today-frozen == MAX+1`` and an age check would
    wrongly reject it → recompute from the replay day → anchor drifts one day
    forward. A verbatim return cannot drift. A future date, an unparseable
    string, an out-of-range ``days``, or no trusted value at all falls back to
    the day-count computation below.

    1-BASED INPUT: ``days`` (relationship_days) is the USER-FACING day count —
    the "第 N 天" the app shows, where the day you met is 第 1 天, not 第 0 天
    (iOS adds +1 for display; this is the inverse on the way in). The card's
    stored ``days_with_user`` stays ELAPSED (0 = met today), so 第 N 天 → elapsed
    N-1. Only this recalibration input is 1-based; onboarding derives elapsed
    from a date and is untouched. The frozen path is consistent — the producer
    (worker._frozen_relationship_anchor) already froze ``_anchor_from_days(N-1)``,
    so a verbatim frozen return and this fallback resolve to the same anchor."""
    if trusted_frozen:
        d = identity_service._parse_iso_calendar_date(trusted_frozen.strip())
        if d is not None:
            from datetime import date as _date
            from identity import card_policy
            if d <= _date.today() and 0 <= int(days) <= card_policy.MAX_RELATIONSHIP_DAYS:
                return d.isoformat()  # verbatim — cross-day replay cannot drift it
    return identity_service._anchor_from_days(max(0, int(days) - 1))


def _create_identity_action_payload(
    store: UserStore,
    payload: dict,
    *,
    audit: dict,
    event_type: str,
    relationship_override: dict | None = None,
) -> tuple[dict | None, dict | None, str]:
    """Fix A: CREATE a brand-new identity card from an agent action.

    ``_save_identity_action_payload`` is UPDATE-ONLY — it 409s when no card
    exists, which wedges the V2 conversation on a fresh-start user's very first
    identity write (no persona → no card → agent calls profile_patch → 409 →
    effect retries forever). This mints the minimal valid card so the agent can
    establish its own identity. Stamps a fresh relationship anchor (today, or the
    earliest memory date) — this is NOT a Genesis import, so the anchor source is
    ``agent_bootstrap``.

    A create is a fresh insert with no prior blob to compare against, so it uses
    the ATOMIC create-if-absent ``_save_identity_create_if_absent`` (Codex C2),
    NOT the plain ``_save_identity``. identity_mutation_lock only serializes
    writers inside THIS gunicorn worker process; two DIFFERENT worker processes
    both bootstrapping the same fresh user would each run the plain
    INSERT-ON-CONFLICT-DO-UPDATE and the second would clobber the first (a lost
    update where BOTH report success). The atomic create makes exactly one
    caller win the insert; a loser raises ``IdentityWriteConflict`` so
    ``_with_identity_mutation_lock_and_retry`` re-reads the now-existing card and
    merges this patch onto it via the normal CAS UPDATE path. The audit/effect
    below run ONLY for the winner — the loser never reaches them.
    """
    from identity import card_policy
    ok, err = card_policy.validate_full_identity_card(payload)
    if not ok:
        return None, None, err
    envelope, err = core_envelope._build_shared_envelope_for_store(
        store,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        item_id=None,
    )
    if envelope is None:
        return None, None, err
    now = core_util._now_iso()
    identity = {
        "v": 1,
        "id": envelope.get("id") or core_util._new_public_id("identity"),
        "enclave_pk_fpr": "",
        **core_envelope.envelope_storage_fields(envelope),
        "created_at": now,
        "updated_at": now,
        # A create IS a full-card write, so it stamps the replaced_at baseline
        # (P5 optimistic-concurrency anchor), same as init / replace.
        "replaced_at": now,
        # Anchor: default is the auto tier (today or earliest memory,
        # source=agent_bootstrap). When the SAME bootstrap patch carried an
        # explicit relationship_days (item 2a), relationship_override stamps the
        # user_calibrated anchor instead — user-filled days beat auto-inference,
        # and previously they were silently dropped at card creation.
        "relationship_started_at": (relationship_override or {}).get(
            "relationship_started_at",
            identity_service._anchor_from_days(0, store=store, prefer_memory=True)),
        "relationship_anchor_source": (relationship_override or {}).get(
            "relationship_anchor_source", "agent_bootstrap"),
        "relationship_anchor_evidence": (relationship_override or {}).get(
            "relationship_anchor_evidence",
            "identity established by the agent via profile_patch"),
    }
    if envelope.get("K_enclave"):
        identity["K_enclave"] = envelope["K_enclave"]
    # Atomic create-if-absent, NOT a plain overwrite. Losing the insert race
    # (another worker already bootstrapped this user, OR a genuine DB error)
    # raises IdentityWriteConflict so the mutation retry loop re-reads and takes
    # the CAS UPDATE path — instead of clobbering the winner and reporting a
    # false success, or masking a DB failure as a 200 (Codex C2). Audit/effect
    # below run only once the insert has actually won.
    if not identity_service._save_identity_create_if_absent(store, identity):
        raise identity_service.IdentityWriteConflict()
    boot_gates._log_bootstrap_event(store, event_type, success=True)
    change = identity_service._append_identity_change(store, audit)
    return identity, change, ""


def _load_identity_snapshot_for_write(store: UserStore) -> tuple[dict | None, str]:
    """Load the raw identity blob — the exact snapshot that must later be
    passed as `existing=` to `_save_identity_action_payload`'s CAS write.

    MUST be called BEFORE the enclave plaintext read
    (`_identity_plain_for_action`) in both `_identity_profile_patch` and
    `_identity_dimension_nudge`'s `_read_merge_save` closures — not after.
    Reading it after the enclave round trip (the original, buggy ordering)
    leaves a window where a concurrent writer's change lands AFTER the
    enclave read but BEFORE this load: the merge is computed from stale
    (pre-write) plaintext, yet `existing` would then equal the concurrent
    writer's ALREADY-current blob, so the CAS would wrongly match and
    silently clobber it. Reading first means any write landing after this
    point — including during the enclave round trip that follows — makes
    the eventual CAS fail instead, and the caller retries from a fresh
    snapshot (see identity/actions.py::_with_identity_mutation_lock_and_retry).

    Also runs the two existing-card preconditions previously checked inside
    _save_identity_action_payload (identity must exist; must carry a
    relationship anchor) — moved here since they're snapshot-dependent and
    this is now the FIRST thing that touches the raw blob. Same error codes
    and callers' response status (409) as before this reorder — this is a
    timing/ordering fix, not a behavior change for the single-writer case.
    """
    existing = identity_service._load_identity(store)
    if not existing:
        return None, "identity_not_initialized"
    if not existing.get("relationship_started_at"):
        return None, "identity_relationship_anchor_missing"
    return existing, ""


# Bounded retry count for identity_mutation_lock's CAS-conflict recovery
# (see _with_identity_mutation_lock_and_retry). 3 is generous for two
# gunicorn workers racing on the same user — a losing worker's retry re-reads
# the row the winner just committed, so a 3rd collision would need a THIRD
# concurrent writer to also land in that same narrow window.
_IDENTITY_WRITE_MAX_ATTEMPTS = 3


def _with_identity_mutation_lock_and_retry(
    store: UserStore, action_name: str, work,
) -> tuple[dict, list[dict], int]:
    """Run `work()` — a zero-arg closure doing the full
    read-existing-card -> merge -> re-encrypt -> CAS-write span — under the
    per-user identity_mutation_lock, retrying the WHOLE span from a fresh
    read if identity_service._save_identity_cas loses the DB-level
    compare-and-swap to a concurrent writer.

    The in-process lock (identity_mutation_lock) alone only serializes
    writers within THIS worker process; it does nothing for two different
    gunicorn worker processes writing the same user's card (each has its own
    independent Lock instance — see identity/service.py). The CAS write is
    what actually detects that race; this loop is what turns a detected race
    into a successful retry instead of a client-visible failure, up to
    _IDENTITY_WRITE_MAX_ATTEMPTS attempts. Exhausting all attempts (sustained
    contention from 3+ concurrent writers on one user) surfaces as a 409
    `identity_write_conflict` — same shape as this file's other conflict/
    error responses, so callers can already handle it."""
    with identity_service.identity_mutation_lock(store.user_id):
        for attempt in range(_IDENTITY_WRITE_MAX_ATTEMPTS):
            try:
                return work()
            except identity_service.IdentityWriteConflict:
                if attempt == _IDENTITY_WRITE_MAX_ATTEMPTS - 1:
                    return {"status": "error", "error": "identity_write_conflict",
                            "action": action_name}, [], 409
    # Unreachable (the loop above always returns), but keeps this an
    # explicit tuple-returning function for anything that inspects it.
    return {"status": "error", "error": "identity_write_conflict", "action": action_name}, [], 409


def _identity_profile_patch(
    store: UserStore,
    api_key: str | None,
    action: dict,
    *,
    runtime_token: str = "",
    trusted_relationship_anchor: str | None = None,
) -> tuple[dict, list[dict], int]:
    patch = action.get("patch") if isinstance(action.get("patch"), dict) else {}
    for key in identity_service._IDENTITY_PROFILE_FIELDS:
        if key in action and key not in patch:
            patch[key] = action[key]
    if not patch:
        return {"status": "error", "error": "patch_required", "action": "identity.profile_patch"}, [], 400

    from identity import card_policy
    ok, err = card_policy.validate_profile_patch(patch)
    if not ok:
        return {"status": "error", "error": err, "action": "identity.profile_patch"}, [], 400

    # 成对闸只拦 agent 来源(runtime token):App 用户在 UI 里单独改名是正常路径,不受此约束。
    # agent 的另两条路径(consumer 夹带通道、io_cli 预检)在各自漏斗处执行同规则——见 spec 3.1/3.4。
    if runtime_token:
        ok, err = card_policy.validate_rename_pairing(patch)
        if not ok:
            return {"status": "error", "error": err,
                    "hint": "介绍无需变化时读旧卡原样带回 --self-introduction",
                    "action": "identity.profile_patch"}, [], 400

    # Pre-check the legacy direct-list keys' shape here (outside the lock —
    # pure validation, same as the card_policy checks above): preserves the
    # existing "{field}_must_be_list" 400 for malformed input before we ever
    # touch the enclave / lock. apply_list_ops itself is lenient (treats a
    # bad type as empty) because add_/remove_/replace_ op keys never had this
    # guard to begin with.
    for key in identity_service._IDENTITY_PROFILE_LIST_FIELDS:
        if key in patch and not isinstance(patch.get(key), (list, str)):
            return {"status": "error", "error": f"{key}_must_be_list", "action": "identity.profile_patch"}, [], 400

    # Fail fast on a malformed list-op request (conflicting ops on the same
    # field, or an add_/replace_ whose items are all blank) BEFORE acquiring
    # the lock or doing the enclave read — this check only looks at `patch`,
    # never the existing card, so there's no reason to pay for either first.
    try:
        validate_list_ops_shape(patch)
    except ListOpConflict as exc:
        return {"status": "error", "error": "list_op_conflict", "field": str(exc),
                "action": "identity.profile_patch"}, [], 400
    except ListOpBlank as exc:
        return {"status": "error", "error": "list_op_blank", "field": str(exc),
                "action": "identity.profile_patch"}, [], 400

    # Everything below is a read-existing-card -> merge -> re-encrypt -> save
    # span. It must run under the per-user identity_mutation_lock so two
    # concurrent profile_patch calls (e.g. two different add_signature ops)
    # can't both read the same pre-mutation card and lost-update each other —
    # see identity/service.py::identity_mutation_lock for why UserStore's own
    # identity_lock (which only wraps the final save) isn't enough.
    def _read_merge_save() -> tuple[dict, list[dict], int]:
        # Snapshot the raw blob FIRST — before the enclave plaintext read
        # below — so it covers the ENTIRE span as the CAS `expected` value,
        # including the enclave round trip itself. See
        # _load_identity_snapshot_for_write's docstring for why the ordering
        # matters (Codex C1 follow-up: reading it after the enclave call left
        # a window where a concurrent write could be silently clobbered).
        existing, err = _load_identity_snapshot_for_write(store)
        bootstrap = False
        if existing is None:
            # Fix A (fresh-start bootstrap): a user with no persona → no identity
            # card whose agent calls profile_patch has nothing to UPDATE. Rather
            # than 409-wedging the V2 conversation on the very first identity
            # write, CREATE the card from this patch. Only "not initialized"
            # bootstraps — a missing relationship anchor (or any other snapshot
            # error) still 409s; do not paper over a half-formed card. The
            # create path has no prior blob to CAS against, so it skips the
            # snapshot-first enclave read below.
            if err != "identity_not_initialized":
                return {"status": "error", "error": err, "action": "identity.profile_patch"}, [], 409
            bootstrap = True
            plain = {}  # empty base: every field comes from the patch
        else:
            plain, err = _identity_plain_for_action(store, api_key, runtime_token=runtime_token)
            if plain is None:
                return {"status": "error", "error": err, "action": "identity.profile_patch"}, [], 409

        payload = _identity_payload_from_plain(plain)
        changed: list[str] = []
        audit_old = ""
        audit_new = ""

        if "agent_name" in patch:
            new_name = card_policy.stripped_agent_name(_identity_action_text(patch.get("agent_name"), 80))
            if not new_name:
                return {"status": "error", "error": "agent_name_empty", "action": "identity.profile_patch"}, [], 400
            if new_name.lower() in identity_service._IDENTITY_RUNTIME_LABELS:
                # Same error code as card_policy.validate_profile_patch (raw pre-check above) —
                # unify so punctuation-wrapped runtime labels ("`hermes`") that slip past the raw
                # pre-check still fail with the identical code once normalized here.
                return {"status": "error", "error": "agent_name_is_runtime_label", "action": "identity.profile_patch"}, [], 400
            old_name = str(payload.get("agent_name") or "")
            if new_name != old_name:
                payload["agent_name"] = new_name
                changed.append("agent_name")
                audit_old = old_name
                audit_new = new_name

        if "self_introduction" in patch:
            intro = str(patch.get("self_introduction") or "").strip()[:1200]
            if not intro:
                return {"status": "error", "error": "self_introduction_empty", "action": "identity.profile_patch"}, [], 400
            old_intro = str(payload.get("self_introduction") or "")
            if intro != old_intro:
                payload["self_introduction"] = intro
                changed.append("self_introduction")
                if not audit_old and not audit_new:
                    audit_old = old_intro[:120]
                    audit_new = intro[:120]

        for key in identity_service._IDENTITY_PROFILE_STRING_FIELDS:
            if key in {"agent_name", "self_introduction"} or key not in patch:
                continue
            max_len = 1200 if key in {"relationship_anchor", "tone_style", "custom_persona_prompt"} else 240
            new_value = _identity_action_text(patch.get(key), max_len)
            old_value = str(payload.get(key) or "")
            if new_value != old_value:
                if new_value:
                    payload[key] = new_value
                else:
                    payload.pop(key, None)
                changed.append(key)
                if not audit_old and not audit_new:
                    audit_old = old_value[:120]
                    audit_new = new_value[:120]

        try:
            list_updates = apply_list_ops(payload, patch)
        except ListOpConflict as exc:
            return {"status": "error", "error": "list_op_conflict", "field": str(exc),
                    "action": "identity.profile_patch"}, [], 400
        except ListOpBlank as exc:
            return {"status": "error", "error": "list_op_blank", "field": str(exc),
                    "action": "identity.profile_patch"}, [], 400
        except ListOpTooManyItems as exc:
            return {"status": "error", "error": "list_op_too_many_items", "field": str(exc),
                    "action": "identity.profile_patch"}, [], 400

        for key, values in list_updates.items():
            old_values = payload.get(key) if isinstance(payload.get(key), list) else []
            if values != old_values:
                if values:
                    payload[key] = values
                else:
                    payload.pop(key, None)
                changed.append(key)
                if not audit_old and not audit_new:
                    audit_old = ", ".join(old_values)[:120]
                    audit_new = ", ".join(values)[:120]

        # relationship_days: NOT a stored profile field. days_with_user is always
        # DERIVED from the relationship_started_at anchor (see
        # identity_service._live_days_with_user), so "set the day count to N" means
        # "move the anchor to today-N days". We translate here and thread the new
        # anchor through _save_identity_action_payload / _create_identity_action_payload
        # via relationship_override so the anchor rewrite rides the SAME CAS write as
        # the rest of this patch — making identity_patch the CAS-safe canonical path
        # for recalibration (the legacy identity.relationship_days_set action and the
        # relationship_anchor endpoint now share the same CAS core, see
        # _update_relationship_anchor_cas).
        #
        # Anchor date is resolved ONCE (item 1): on the V2 sink path the producer
        # froze the absolute date at enqueue time and threaded it down the call
        # path as the trusted keyword arg ``trusted_relationship_anchor`` (NOT via
        # the action dict / request body — see _resolve_relationship_anchor); on
        # the direct request path there is no trusted frozen date so it is computed
        # inline from today. Either way the write value is fixed before the CAS, so
        # a delayed replay is idempotent.
        #
        # source = user_calibrated is the TOP tier (user_calibrated > material_stated
        # > auto): an explicit relationship_days is always the user deliberately
        # setting the count — at bootstrap (item 2a) it beats the auto default, and
        # on an existing card it beats any lower-tier anchor.
        relationship_override = None
        if "relationship_days" in patch:
            raw_days = patch.get("relationship_days")
            shape_err = card_policy.relationship_days_shape_error(raw_days)
            if shape_err:
                return {"status": "error", "error": shape_err,
                        "action": "identity.profile_patch"}, [], 400
            new_started_at = _resolve_relationship_anchor(
                raw_days, trusted_frozen=trusted_relationship_anchor)
            evidence = _identity_action_text(
                action.get("relationship_anchor_evidence")
                or action.get("reason")
                or (existing.get("relationship_anchor_evidence") if existing else "")
                or "Relationship day count recalibrated via profile_patch.",
                500,
            )
            if bootstrap:
                # item 2a: an explicit user-filled day count at card creation must
                # win over _create_identity_action_payload's auto default
                # (agent_bootstrap / today). Stamp user_calibrated so a later
                # redistill can't lower-tier-override it. Previously this whole
                # branch was gated `and not bootstrap`, silently dropping the days
                # while the other fields created the card.
                relationship_override = {
                    "relationship_started_at": new_started_at,
                    "relationship_anchor_source": "user_calibrated",
                    "relationship_anchor_evidence": evidence,
                }
                changed.append("days_with_user")
                if not audit_old and not audit_new:
                    audit_old = "0"
                    audit_new = str(raw_days)
            else:
                old_started_at = str(existing.get("relationship_started_at") or "")
                old_source = str(existing.get("relationship_anchor_source") or "")
                # Idempotent: re-setting the same day count on the same calendar day
                # yields the same anchor, so it's a no-op unless the anchor date OR the
                # source (was it explicitly user-calibrated?) actually changes.
                if new_started_at != old_started_at or old_source != "user_calibrated":
                    relationship_override = {
                        "relationship_started_at": new_started_at,
                        "relationship_anchor_source": "user_calibrated",
                        "relationship_anchor_evidence": evidence,
                    }
                    changed.append("days_with_user")
                    if not audit_old and not audit_new:
                        audit_old = str(identity_service._live_days_with_user(existing, store=store))
                        audit_new = str(raw_days)

        if not changed:
            if bootstrap:
                # A non-empty patch that resolves to zero writable fields against
                # an empty card (e.g. an empty list value) — nothing to
                # bootstrap. Fall back to the original 409 instead of a
                # misleading no-op "ok".
                return {"status": "error", "error": "identity_not_initialized", "action": "identity.profile_patch"}, [], 409
            return {
                "status": "ok",
                "action": "identity.profile_patch",
                "changed_fields": [],
                "noop": True,
            }, [], 200

        reason = _identity_action_text(
            action.get("reason") or f"Identity profile updated: {', '.join(changed)}.",
            500,
        )
        audit = {
            "action": "profile_patch",
            "dimension": "profile",
            "old_value": audit_old,
            "new_value": audit_new,
            "reason": reason,
        }
        if bootstrap:
            # No existing card yet — atomically mint one (see
            # _create_identity_action_payload). It uses the create-if-absent
            # write, so losing the fresh-user insert race raises
            # IdentityWriteConflict and _with_identity_mutation_lock_and_retry
            # retries down the UPDATE path against the winner's card.
            identity, change, err = _create_identity_action_payload(
                store, payload, audit=audit,
                event_type="identity_action_bootstrap",
                relationship_override=relationship_override)
        else:
            identity, change, err = _save_identity_action_payload(
                store,
                payload,
                existing=existing,
                audit=audit,
                event_type="identity_action_profile_patch",
                relationship_override=relationship_override,
            )
        if identity is None:
            return {"status": "error", "error": err, "action": "identity.profile_patch"}, [], 409

        effect = {
            "type": "identity_updated",
            "action": "identity.profile_patch",
            "fields": changed,
            "identity_id": identity.get("id", ""),
            "change_id": change.get("id", "") if change else "",
        }
        return {
            "status": "ok",
            "action": "identity.profile_patch",
            "changed_fields": changed,
            "identity": {
                "id": identity.get("id", ""),
                "updated_at": identity.get("updated_at", ""),
                "days_with_user": identity_service._live_days_with_user(identity, store=store),
            },
            "change": change or {},
        }, [effect], 200

    return _with_identity_mutation_lock_and_retry(store, "identity.profile_patch", _read_merge_save)


def _identity_dimension_nudge(
    store: UserStore,
    api_key: str | None,
    action: dict,
    *,
    runtime_token: str = "",
) -> tuple[dict, list[dict], int]:
    dimension_name = _identity_action_text(action.get("dimension") or action.get("dimension_name"), 80)
    if not dimension_name:
        return {"status": "error", "error": "dimension_required", "action": "identity.dimension_nudge"}, [], 400
    try:
        delta = int(action.get("delta"))
    except Exception:
        return {"status": "error", "error": "delta_required", "action": "identity.dimension_nudge"}, [], 400

    # Same read-existing-card -> merge -> re-encrypt -> save span as
    # _identity_profile_patch, on the SAME per-user identity card — must run
    # under the same identity_mutation_lock, or a concurrent profile_patch
    # (e.g. add_signature) and a dimension_nudge can each read the
    # pre-mutation card and lost-update each other's change on save.
    def _read_merge_save() -> tuple[dict, list[dict], int]:
        # Snapshot the raw blob FIRST — before the enclave plaintext read
        # below — see _load_identity_snapshot_for_write's docstring and the
        # matching comment in _identity_profile_patch's _read_merge_save.
        existing, err = _load_identity_snapshot_for_write(store)
        if existing is None:
            return {"status": "error", "error": err, "action": "identity.dimension_nudge"}, [], 409

        plain, err = _identity_plain_for_action(store, api_key, runtime_token=runtime_token)
        if plain is None:
            return {"status": "error", "error": err, "action": "identity.dimension_nudge"}, [], 409

        payload = _identity_payload_from_plain(plain)
        dims = list(payload.get("dimensions") or [])
        matched = None
        for dim in dims:
            if isinstance(dim, dict) and str(dim.get("name") or "").strip().lower() == dimension_name.lower():
                matched = dim
                break
        if matched is None:
            return {"status": "error", "error": "dimension_not_found", "action": "identity.dimension_nudge"}, [], 404
        try:
            old_value = int(matched.get("value", 0))
        except Exception:
            old_value = 0
        new_value = old_value + delta
        from identity import card_policy
        ok, err = card_policy.validate_dimension_nudge(dimension_name, new_value)
        if not ok:
            return {"status": "error", "error": err, "action": "identity.dimension_nudge"}, [], 400
        # Defense in depth: single nudge |delta| must not exceed 10 (request-level
        # batch validation is the primary gate in _execute_identity_actions).
        # V2 migration note: pre 侧 backend/capabilities/identity.py nudge 能力与
        # tool_schema.py 需在 0727 合并时同步此限幅。
        if abs(delta) > 10:
            return {"status": "error", "error": "nudge_delta_exceeds_cap", "action": "identity.dimension_nudge"}, [], 400
        if new_value == old_value:
            return {
                "status": "ok",
                "action": "identity.dimension_nudge",
                "changed_fields": [],
                "noop": True,
            }, [], 200
        matched["value"] = new_value
        reason = _identity_action_text(action.get("reason") or f"{dimension_name} adjusted by {delta:+d}.", 500)
        if reason:
            matched["last_nudge_reason"] = reason
        payload["dimensions"] = dims

        identity, change, err = _save_identity_action_payload(
            store,
            payload,
            existing=existing,
            audit={
                "action": "nudge",
                "dimension": dimension_name,
                "old_value": old_value,
                "new_value": new_value,
                "delta": delta,
                "reason": reason,
            },
            event_type="identity_action_dimension_nudge",
        )
        if identity is None:
            return {"status": "error", "error": err, "action": "identity.dimension_nudge"}, [], 409
        effect = {
            "type": "identity_updated",
            "action": "identity.dimension_nudge",
            "fields": ["dimensions"],
            "identity_id": identity.get("id", ""),
            "change_id": change.get("id", "") if change else "",
        }
        return {
            "status": "ok",
            "action": "identity.dimension_nudge",
            "changed_fields": ["dimensions"],
            "identity": {
                "id": identity.get("id", ""),
                "updated_at": identity.get("updated_at", ""),
                "days_with_user": identity_service._live_days_with_user(identity, store=store),
            },
            "change": change or {},
        }, [effect], 200

    return _with_identity_mutation_lock_and_retry(store, "identity.dimension_nudge", _read_merge_save)


def _relationship_anchor_cas_write(
    store: UserStore, *, days, source: str, evidence: str,
) -> tuple[dict | None, int, str]:
    """Single authoritative, metadata-only relationship-anchor writer (item 5).

    Both legacy non-CAS anchor paths — ``identity.relationship_days_set`` (this
    module) and the ``/v1/identity/relationship_anchor`` endpoint
    (identity_core.update_relationship_anchor) — used to do a plain
    ``_save_identity`` (whole-blob overwrite, no lock, no compare-and-swap) from
    their OWN independently-taken snapshot. Either could land between a concurrent
    profile_patch/dimension_nudge's CAS win and that caller observing success,
    silently clobbering it (the KNOWN RESIDUAL the old comment flagged). They now
    both funnel through here so there is ONE anchor writer and it is CAS-safe.

    Only the 3 ``relationship_*`` fields (+ ``updated_at``) change; the encrypted
    envelope is copied forward untouched, so NO enclave decrypt/re-encrypt is
    needed — this is a plaintext-metadata mutation. Runs under
    ``identity_mutation_lock`` + the same bounded ``_save_identity_cas`` retry as
    profile_patch, so a mid-span concurrent CAS write makes this retry from a
    fresh read instead of clobbering it.

    Input validation is UNIFIED with the identity_patch path via
    ``card_policy.relationship_days_shape_error`` (rejects bool/str/float,
    negative, over-cap) — the legacy ``int(...)`` coercion that accepted
    ``int("300")`` / ``True`` is gone.

    Returns ``(identity, old_days, "")`` on success, or ``(None, 0, err_code)``
    where err_code is one of the shape codes, ``identity_not_initialized``, or
    ``identity_write_conflict``. Deliberately does NOT append the audit change,
    log a bootstrap event, or build an effect — the two callers own those so each
    keeps its own distinct response contract."""
    from identity import card_policy
    shape_err = card_policy.relationship_days_shape_error(days)
    if shape_err:
        return None, 0, shape_err
    with identity_service.identity_mutation_lock(store.user_id):
        for attempt in range(_IDENTITY_WRITE_MAX_ATTEMPTS):
            existing = identity_service._load_identity(store)
            if not existing:
                return None, 0, "identity_not_initialized"
            old_days = identity_service._live_days_with_user(existing, store=store)
            identity = dict(existing)
            identity["updated_at"] = core_util._now_iso()
            identity["relationship_started_at"] = identity_service._anchor_from_days(int(days))
            identity["relationship_anchor_source"] = source
            if evidence:
                identity["relationship_anchor_evidence"] = evidence
            if identity_service._save_identity_cas(store, existing, identity):
                return identity, old_days, ""
            # CAS lost the race to a concurrent writer — retry from a fresh read.
    return None, 0, "identity_write_conflict"


def _identity_relationship_days_set(store: UserStore, action: dict) -> tuple[dict, list[dict], int]:
    raw_days = action.get("days_with_user")
    if raw_days is None:
        return {"status": "error", "error": "days_with_user_required", "action": "identity.relationship_days_set"}, [], 400
    evidence = _identity_action_text(action.get("relationship_anchor_evidence") or action.get("reason") or "", 500)
    identity, old_days, err = _relationship_anchor_cas_write(
        store, days=raw_days, source="user_calibrated", evidence=evidence)
    if err:
        status = 409 if err in ("identity_not_initialized", "identity_write_conflict") else 400
        return {"status": "error", "error": err, "action": "identity.relationship_days_set"}, [], status
    days = int(raw_days)
    boot_gates._log_bootstrap_event(store, "identity_action_relationship_days_set", success=True)
    change = identity_service._append_identity_change(store, {
        "action": "relationship_days",
        "dimension": "relationship_days",
        "old_value": old_days,
        "new_value": days,
        "delta": days - old_days,
        "reason": evidence or "Relationship day count recalibrated.",
    })
    effect = {
        "type": "identity_updated",
        "action": "identity.relationship_days_set",
        "fields": ["days_with_user"],
        "identity_id": identity.get("id", ""),
        "change_id": change.get("id", "") if change else "",
    }
    return {
        "status": "ok",
        "action": "identity.relationship_days_set",
        "changed_fields": ["days_with_user"],
        "identity": {
            "id": identity.get("id", ""),
            "updated_at": identity.get("updated_at", ""),
            "days_with_user": days,
        },
        "change": change or {},
    }, [effect], 200


def _replace_relationship_anchor(action: dict) -> dict:
    """B2: build a relationship anchor from an identity.replace action ONLY when it carries
    an explicit relationship time (translating days_with_user → an ISO start date). The
    service layer applies the legality guard (valid date + evidence); an empty dict here
    means 'preserve the existing anchor'."""
    evidence = str(action.get("relationship_anchor_evidence") or "").strip()
    started = str(action.get("relationship_started_at") or "").strip()
    if not started:
        days = action.get("days_with_user")
        if isinstance(days, bool) or not isinstance(days, int) or days < 0:
            return {}
        from datetime import date, timedelta
        started = (date.today() - timedelta(days=days)).isoformat()
    if not started or not evidence:
        return {}
    return {
        "relationship_started_at": started,
        "relationship_anchor_source": "genesis_resident_distill",
        "relationship_anchor_evidence": evidence,
    }


def _identity_replace_action(
    store: UserStore, api_key: str | None, action: dict, *, runtime_token: str = ""
) -> tuple[dict, list[dict], int]:
    """Full-card identity replace, server-build (reuses genesis
    ``replace_identity_preserving_anchor`` — server builds the shared envelope, agent sends
    plaintext). Used by the VPS resident-distill path when the agent locally re-derives
    identity from a persona doc. The relationship anchor is preserved unless the action
    carries an explicit relationship time (B2, see ``_replace_relationship_anchor``).

    HIGH-RISK (full overwrite) — gated so it is NOT a normal agent action (Codex P1):
      * server-build only: payload must NOT carry an `envelope`;
      * must run inside a resident-distill job context: `source=genesis_resident_distill`
        + `job_id` + `reason`, and that job must belong to the caller and be a live resident
        job (status=processing, resident_consumer_id set).
    """
    if action.get("envelope") is not None:
        return {"status": "error", "error": "envelope_not_allowed", "action": "identity.replace"}, [], 400
    source = str(action.get("source") or "").strip()
    job_id = str(action.get("job_id") or "").strip()
    reason = str(action.get("reason") or "").strip()
    if source != "genesis_resident_distill" or not job_id or not reason:
        return {"status": "error", "error": "identity_replace_requires_resident_distill_context",
                "action": "identity.replace"}, [], 403
    import db  # lazy — avoid import cycle
    job = db.genesis_get_job(store.user_id, job_id)
    if not job or job.get("status") != "processing" or not str(job.get("resident_consumer_id") or "").strip():
        return {"status": "error", "error": "not_a_live_resident_distill_job",
                "action": "identity.replace"}, [], 403
    identity_payload = action.get("identity")
    if not isinstance(identity_payload, dict) or not identity_payload:
        return {"status": "error", "error": "identity_required", "action": "identity.replace"}, [], 400
    from identity import card_policy
    ok, err = card_policy.validate_full_identity_card(identity_payload)
    if not ok:
        return {"status": "error", "error": err, "action": "identity.replace"}, [], 400
    # P5 optimistic concurrency (Task 5): the resident consumer snapshots the outer
    # replaced_at baseline at job-creation time (Task 4's base_identity_replaced_at) and
    # forwards it here. If ANOTHER full init/replace has moved replaced_at since then, this
    # job's derive is stale (built off an outdated card) and must not clobber the newer one.
    # Omitted or "" (every existing caller, and legacy jobs pre-dating the baseline) skips the
    # check entirely — back-compat, not a security gate.
    base_identity_replaced_at = str(action.get("base_identity_replaced_at") or "").strip()
    if base_identity_replaced_at:
        current_identity = identity_service._load_identity(store)
        current_replaced_at = str((current_identity or {}).get("replaced_at") or "")
        if base_identity_replaced_at != current_replaced_at:
            return {"status": "error", "error": "identity_base_stale", "action": "identity.replace"}, [], 409
    # Cloud plaintext role-card uploads use the shared service helper as an
    # upsert. Resident identity.replace remains replace-only: its create path is
    # owned by the resident consumer protocol and must not leak through here.
    if not identity_service._load_identity(store):
        return {"status": "error", "error": "identity_not_initialized",
                "action": "identity.replace"}, [], 409
    # T12 (spec 3.6 / D5): replace_identity_preserving_anchor now reads the LATEST
    # decrypted card at write time and key-level merges the distilled payload onto
    # it under identity_mutation_lock + a CAS retry loop — the base_identity_replaced_at
    # check above stays as the coarser replace-vs-replace guard (P5), but it is no
    # longer the only concurrency protection: a profile_patch/dimension_nudge landing
    # between this function's reads and its write can no longer be silently clobbered
    # (closes the KNOWN RESIDUAL this comment used to document).
    from genesis import service as genesis_service  # lazy — avoid import cycle
    result = genesis_service.replace_identity_preserving_anchor(
        store,
        {"identity": identity_payload, "relationship_anchor": _replace_relationship_anchor(action)},
        api_key,
        runtime_token=runtime_token,
    )
    if result != "updated":
        status = 409 if result in (
            "identity_not_initialized", "identity_update_empty", "not_provided",
            "identity_plain_unavailable", "identity_write_conflict",
        ) else 400
        return {"status": "error", "error": result, "action": "identity.replace"}, [], status
    return {"status": "ok", "action": "identity.replace", "job_id": job_id}, [], 200


def _execute_identity_action(
    store: UserStore,
    api_key: str | None,
    action: dict,
    *,
    runtime_token: str = "",
    trusted_relationship_anchor: str | None = None,
) -> tuple[dict, list[dict], int]:
    if not isinstance(action, dict):
        return {"status": "error", "error": "action_must_be_object"}, [], 400
    action_type = str(action.get("type") or action.get("action") or "").strip()
    if action_type == "identity.profile_patch":
        return _identity_profile_patch(
            store, api_key, action, runtime_token=runtime_token,
            trusted_relationship_anchor=trusted_relationship_anchor)
    if action_type == "identity.dimension_nudge":
        return _identity_dimension_nudge(store, api_key, action, runtime_token=runtime_token)
    if action_type == "identity.relationship_days_set":
        return _identity_relationship_days_set(store, action)
    if action_type == "identity.replace":
        return _identity_replace_action(store, api_key, action, runtime_token=runtime_token)
    return {
        "status": "error",
        "error": "unsupported_identity_action",
        "action": action_type,
        "supported": [
            "identity.profile_patch",
            "identity.dimension_nudge",
            "identity.relationship_days_set",
            "identity.replace",
        ],
    }, [], 400


def _execute_identity_actions(
    store: UserStore,
    api_key: str | None,
    actions: list[dict],
    *,
    runtime_token: str = "",
    trusted_relationship_anchor: str | None = None,
) -> tuple[dict, int]:
    if not isinstance(actions, list) or not actions:
        return {"status": "error", "error": "actions_required", "results": [], "effects": []}, 400

    # Batch-level nudge sum validation (同请求内同维度归一求和): collect all
    # identity.dimension_nudge actions and validate that no normalized dimension's
    # |sum(deltas)| > 10. Reject the whole batch with 400 if validation fails.
    # V2 migration note: pre 侧 backend/capabilities/identity.py nudge 能力与
    # tool_schema.py 需在 0727 合并时同步此限幅。
    nudges: list[tuple[str, float]] = []
    for action in actions[:10]:
        if not isinstance(action, dict):
            continue  # Non-dict items will be caught by _execute_identity_action
        action_type = str(action.get("type") or action.get("action") or "").strip()
        if action_type == "identity.dimension_nudge":
            dimension_name = _identity_action_text(action.get("dimension") or action.get("dimension_name"), 80)
            try:
                delta = int(action.get("delta"))
                if dimension_name and delta != 0:
                    nudges.append((dimension_name, delta))
            except Exception:
                pass  # Invalid delta will be caught by _execute_identity_action

    if nudges:
        from identity import card_policy
        ok, err = card_policy.validate_nudge_sum(nudges)
        if not ok:
            return {
                "status": "error",
                "error": err,
                "results": [],
                "effects": [],
            }, 400

    results: list[dict] = []
    effects: list[dict] = []
    for action in actions[:10]:
        result, action_effects, status = _execute_identity_action(
            store,
            api_key,
            action,
            runtime_token=runtime_token,
            trusted_relationship_anchor=trusted_relationship_anchor,
        )
        results.append(result)
        effects.extend(action_effects)
        if status >= 400:
            return {
                "status": "error",
                "error": result.get("error", "identity_action_failed"),
                "results": results,
                "effects": effects,
            }, status
    return {"status": "ok", "results": results, "effects": effects}, 200
