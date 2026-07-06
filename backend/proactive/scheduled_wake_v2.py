"""Durable agent-owned scheduled wakes for Runtime V2.

The scheduler owns timer persistence and mechanical firing. It never writes chat
directly: due timers become V2 wake events, and disabled scheduled work becomes
a background_result transparency wake so the agent can explain the state.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import os
import threading
import time
import uuid
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from psycopg.types.json import Jsonb

import db
from proactive.controls_v2 import (
    DEFAULT_TIMEZONE_V2,
    ProactiveSettingsV2,
    evaluate_scheduled_action_v2,
    evaluate_wake_control_v2,
    resolve_settings_v2,
)
from proactive.runtime_v2 import WakeEventV2

log = logging.getLogger("proactive.scheduled_wake_v2")

SCHEDULED_WAKE_STREAM_V2 = "proactive_scheduled_wakes_v2"

SCHEDULED_PENDING = "pending"
SCHEDULED_CLAIMED = "claimed"
SCHEDULED_FIRED = "fired"
SCHEDULED_CANCELED = "canceled"
SCHEDULED_BLOCKED = "blocked"

DEFAULT_PENDING_TIMER_CAP_V2 = int(os.environ.get("FEEDLING_SCHEDULED_WAKE_PENDING_CAP_V2", "20"))
DEFAULT_CLAIM_TTL_SEC_V2 = float(os.environ.get("FEEDLING_SCHEDULED_WAKE_CLAIM_TTL_SEC_V2", "60"))


def _new_timer_id() -> str:
    return "sched_" + uuid.uuid4().hex[:16]


def _new_claim_id() -> str:
    return "sched_claim_" + uuid.uuid4().hex[:16]


def _now(now: float | None = None) -> float:
    return time.time() if now is None else float(now)


def _coerce_str(value: Any, limit: int = 1000) -> str:
    text = str(value or "").strip()
    return text[:limit]


def _coerce_origin_refs(value: Any, fallback: Sequence[str] = ()) -> tuple[str, ...]:
    raw = value if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)) else fallback
    refs: list[str] = []
    for item in raw or ():
        ref = _coerce_str(item, 200)
        if ref and ref not in refs:
            refs.append(ref)
    return tuple(refs[:50])


def _zone(tz: Any) -> ZoneInfo:
    name = _coerce_str(tz, 80) or DEFAULT_TIMEZONE_V2
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE_V2)


def schedule_instant_v2(at: Any, tz: Any) -> tuple[str, str, float]:
    """Return (wall_time, timezone_name, due_at_epoch).

    The durable record keeps wall-clock time plus event timezone. `due_at` is a
    derived scheduling index and can be recomputed after process restart.
    """
    raw_at = _coerce_str(at, 120)
    if not raw_at:
        raise ValueError("missing_at")
    zone = _zone(tz)
    normalized = raw_at[:-1] + "+00:00" if raw_at.endswith("Z") else raw_at
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("invalid_at") from exc
    if parsed.tzinfo is None:
        local = parsed.replace(tzinfo=zone)
    else:
        local = parsed.astimezone(zone)
    wall_time = local.replace(tzinfo=None).isoformat()
    due_at = local.astimezone(timezone.utc).timestamp()
    return wall_time, zone.key, due_at


@dataclass(frozen=True)
class ScheduledWakeRecordV2:
    timer_id: str
    user_id: str
    status: str
    at: str
    timezone: str
    due_at: float
    note: str = ""
    origin_refs: tuple[str, ...] = ()
    created_at: float = 0.0
    updated_at: float = 0.0
    turn_id: str = ""
    wake_ids: tuple[str, ...] = ()
    claim_id: str = ""
    claim_owner_id: str = ""
    claim_expires_at: float = 0.0
    fired_at: float = 0.0
    fired_wake_id: str = ""
    canceled_at: float = 0.0
    cancel_reason: str = ""
    blocked_at: float = 0.0
    block_reason: str = ""
    transparency_wake_id: str = ""
    doc: Mapping[str, Any] = field(default_factory=dict)

    @property
    def pending_like(self) -> bool:
        return self.status in {SCHEDULED_PENDING, SCHEDULED_CLAIMED}


@dataclass(frozen=True)
class ScheduledWakeActionResultV2:
    action_type: str
    status: str
    timer_id: str = ""
    reason: str = ""
    transparency_required: bool = False
    transparency_wake_id: str = ""
    evicted_timer_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        out = {
            "type": f"{self.action_type}_result",
            "status": self.status,
        }
        if self.timer_id:
            out["timer_id"] = self.timer_id
        if self.reason:
            out["reason"] = self.reason
        if self.transparency_required:
            out["transparency_required"] = True
        if self.transparency_wake_id:
            out["transparency_wake_id"] = self.transparency_wake_id
        if self.evicted_timer_ids:
            out["evicted_timer_ids"] = list(self.evicted_timer_ids)
        return out


@dataclass(frozen=True)
class ScheduledWakeFireResultV2:
    status: str
    timer_id: str
    wake_id: str = ""
    reason: str = ""
    transparency_wake_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {"status": self.status, "timer_id": self.timer_id}
        if self.wake_id:
            out["wake_id"] = self.wake_id
        if self.reason:
            out["reason"] = self.reason
        if self.transparency_wake_id:
            out["transparency_wake_id"] = self.transparency_wake_id
        return out


def scheduled_record_to_doc_v2(record: ScheduledWakeRecordV2) -> dict[str, Any]:
    return {
        "kind": "scheduled_wake_v2",
        "timer_id": record.timer_id,
        "user_id": record.user_id,
        "status": record.status,
        "at": record.at,
        "timezone": record.timezone,
        "due_at": float(record.due_at),
        "note": record.note,
        "origin_refs": list(record.origin_refs),
        "created_at": float(record.created_at),
        "updated_at": float(record.updated_at),
        "turn_id": record.turn_id,
        "wake_ids": list(record.wake_ids),
        "claim_id": record.claim_id,
        "claim_owner_id": record.claim_owner_id,
        "claim_expires_at": float(record.claim_expires_at),
        "fired_at": float(record.fired_at),
        "fired_wake_id": record.fired_wake_id,
        "canceled_at": float(record.canceled_at),
        "cancel_reason": record.cancel_reason,
        "blocked_at": float(record.blocked_at),
        "block_reason": record.block_reason,
        "transparency_wake_id": record.transparency_wake_id,
    }


def scheduled_record_from_doc_v2(doc: Mapping[str, Any]) -> ScheduledWakeRecordV2:
    return ScheduledWakeRecordV2(
        timer_id=str(doc.get("timer_id") or ""),
        user_id=str(doc.get("user_id") or ""),
        status=str(doc.get("status") or ""),
        at=str(doc.get("at") or ""),
        timezone=str(doc.get("timezone") or ""),
        due_at=float(doc.get("due_at") or 0.0),
        note=str(doc.get("note") or ""),
        origin_refs=tuple(str(item) for item in (doc.get("origin_refs") or ())),
        created_at=float(doc.get("created_at") or 0.0),
        updated_at=float(doc.get("updated_at") or 0.0),
        turn_id=str(doc.get("turn_id") or ""),
        wake_ids=tuple(str(item) for item in (doc.get("wake_ids") or ())),
        claim_id=str(doc.get("claim_id") or ""),
        claim_owner_id=str(doc.get("claim_owner_id") or ""),
        claim_expires_at=float(doc.get("claim_expires_at") or 0.0),
        fired_at=float(doc.get("fired_at") or 0.0),
        fired_wake_id=str(doc.get("fired_wake_id") or ""),
        canceled_at=float(doc.get("canceled_at") or 0.0),
        cancel_reason=str(doc.get("cancel_reason") or ""),
        blocked_at=float(doc.get("blocked_at") or 0.0),
        block_reason=str(doc.get("block_reason") or ""),
        transparency_wake_id=str(doc.get("transparency_wake_id") or ""),
        doc=dict(doc),
    )


class InMemoryScheduledWakeStoreV2:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, dict[str, Any]]] = {}

    def create(self, record: ScheduledWakeRecordV2) -> ScheduledWakeRecordV2:
        with self._lock:
            self._records.setdefault(record.user_id, {})[record.timer_id] = scheduled_record_to_doc_v2(record)
            return scheduled_record_from_doc_v2(self._records[record.user_id][record.timer_id])

    def list_records(self, user_id: str) -> list[ScheduledWakeRecordV2]:
        with self._lock:
            rows = list((self._records.get(user_id) or {}).values())
        return [scheduled_record_from_doc_v2(row) for row in rows]

    def cancel(self, user_id: str, timer_id: str, *, reason: str, now: float) -> ScheduledWakeRecordV2 | None:
        with self._lock:
            doc = (self._records.get(user_id) or {}).get(timer_id)
            if not doc or str(doc.get("status") or "") not in {SCHEDULED_PENDING, SCHEDULED_CLAIMED}:
                return None
            doc.update({
                "status": SCHEDULED_CANCELED,
                "cancel_reason": reason,
                "canceled_at": now,
                "updated_at": now,
            })
            return scheduled_record_from_doc_v2(doc)

    def due_candidates(self, user_id: str, *, now: float) -> list[ScheduledWakeRecordV2]:
        records = self.list_records(user_id)
        out = [
            record for record in records
            if record.due_at <= now and (
                record.status == SCHEDULED_PENDING
                or (record.status == SCHEDULED_CLAIMED and record.claim_expires_at <= now)
            )
        ]
        return sorted(out, key=lambda item: (item.due_at, item.created_at, item.timer_id))

    def claim_due(
        self,
        user_id: str,
        timer_id: str,
        *,
        owner_id: str,
        now: float,
        ttl_sec: float,
    ) -> ScheduledWakeRecordV2 | None:
        with self._lock:
            doc = (self._records.get(user_id) or {}).get(timer_id)
            if not doc:
                return None
            status = str(doc.get("status") or "")
            claim_expired = float(doc.get("claim_expires_at") or 0.0) <= now
            if float(doc.get("due_at") or 0.0) > now:
                return None
            if status != SCHEDULED_PENDING and not (status == SCHEDULED_CLAIMED and claim_expired):
                return None
            doc.update({
                "status": SCHEDULED_CLAIMED,
                "claim_id": _new_claim_id(),
                "claim_owner_id": owner_id,
                "claim_expires_at": now + ttl_sec,
                "updated_at": now,
            })
            return scheduled_record_from_doc_v2(doc)

    def mark_fired(self, user_id: str, timer_id: str, claim_id: str, *, wake_id: str, now: float) -> ScheduledWakeRecordV2 | None:
        return self._mark_terminal(
            user_id,
            timer_id,
            claim_id,
            {
                "status": SCHEDULED_FIRED,
                "fired_at": now,
                "fired_wake_id": wake_id,
                "updated_at": now,
            },
        )

    def mark_blocked(
        self,
        user_id: str,
        timer_id: str,
        claim_id: str,
        *,
        reason: str,
        transparency_wake_id: str = "",
        now: float,
    ) -> ScheduledWakeRecordV2 | None:
        return self._mark_terminal(
            user_id,
            timer_id,
            claim_id,
            {
                "status": SCHEDULED_BLOCKED,
                "blocked_at": now,
                "block_reason": reason,
                "transparency_wake_id": transparency_wake_id,
                "updated_at": now,
            },
        )

    def _mark_terminal(self, user_id: str, timer_id: str, claim_id: str, patch: Mapping[str, Any]) -> ScheduledWakeRecordV2 | None:
        with self._lock:
            doc = (self._records.get(user_id) or {}).get(timer_id)
            if not doc or str(doc.get("status") or "") != SCHEDULED_CLAIMED:
                return None
            if str(doc.get("claim_id") or "") != claim_id:
                return None
            doc.update(dict(patch))
            return scheduled_record_from_doc_v2(doc)


class DBScheduledWakeStoreV2:
    def create(self, record: ScheduledWakeRecordV2) -> ScheduledWakeRecordV2:
        doc = scheduled_record_to_doc_v2(record)
        db.log_append(record.user_id, SCHEDULED_WAKE_STREAM_V2, doc, ts=record.created_at, item_key=record.timer_id)
        return scheduled_record_from_doc_v2(doc)

    def list_records(self, user_id: str) -> list[ScheduledWakeRecordV2]:
        return [scheduled_record_from_doc_v2(doc) for doc in db.log_read_all(user_id, SCHEDULED_WAKE_STREAM_V2)]

    def cancel(self, user_id: str, timer_id: str, *, reason: str, now: float) -> ScheduledWakeRecordV2 | None:
        doc = self._patch_guarded(
            user_id,
            timer_id,
            {
                "status": SCHEDULED_CANCELED,
                "cancel_reason": reason,
                "canceled_at": now,
                "updated_at": now,
            },
            statuses={SCHEDULED_PENDING, SCHEDULED_CLAIMED},
        )
        return scheduled_record_from_doc_v2(doc) if doc else None

    def due_candidates(self, user_id: str, *, now: float) -> list[ScheduledWakeRecordV2]:
        out = [
            record for record in self.list_records(user_id)
            if record.due_at <= now and (
                record.status == SCHEDULED_PENDING
                or (record.status == SCHEDULED_CLAIMED and record.claim_expires_at <= now)
            )
        ]
        return sorted(out, key=lambda item: (item.due_at, item.created_at, item.timer_id))

    def claim_due(
        self,
        user_id: str,
        timer_id: str,
        *,
        owner_id: str,
        now: float,
        ttl_sec: float,
    ) -> ScheduledWakeRecordV2 | None:
        patch = {
            "status": SCHEDULED_CLAIMED,
            "claim_id": _new_claim_id(),
            "claim_owner_id": owner_id,
            "claim_expires_at": now + ttl_sec,
            "updated_at": now,
        }
        params = [Jsonb(patch), user_id, SCHEDULED_WAKE_STREAM_V2, user_id, SCHEDULED_WAKE_STREAM_V2, timer_id, now, now]
        sql = (
            "UPDATE user_logs SET doc = doc || %s "
            "WHERE user_id = %s AND stream = %s AND seq = ("
            "  SELECT seq FROM user_logs WHERE user_id = %s AND stream = %s AND item_key = %s "
            "  ORDER BY seq DESC LIMIT 1"
            ") "
            "AND COALESCE(NULLIF(doc->>'due_at','')::float8, 0) <= %s "
            "AND (doc->>'status' = 'pending' OR (doc->>'status' = 'claimed' "
            "AND COALESCE(NULLIF(doc->>'claim_expires_at','')::float8, 0) <= %s)) "
            "RETURNING doc"
        )
        with db.get_pool().connection() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return scheduled_record_from_doc_v2(row[0]) if row is not None else None

    def mark_fired(self, user_id: str, timer_id: str, claim_id: str, *, wake_id: str, now: float) -> ScheduledWakeRecordV2 | None:
        doc = self._patch_guarded(
            user_id,
            timer_id,
            {
                "status": SCHEDULED_FIRED,
                "fired_at": now,
                "fired_wake_id": wake_id,
                "updated_at": now,
            },
            statuses={SCHEDULED_CLAIMED},
            claim_id=claim_id,
        )
        return scheduled_record_from_doc_v2(doc) if doc else None

    def mark_blocked(
        self,
        user_id: str,
        timer_id: str,
        claim_id: str,
        *,
        reason: str,
        transparency_wake_id: str = "",
        now: float,
    ) -> ScheduledWakeRecordV2 | None:
        doc = self._patch_guarded(
            user_id,
            timer_id,
            {
                "status": SCHEDULED_BLOCKED,
                "blocked_at": now,
                "block_reason": reason,
                "transparency_wake_id": transparency_wake_id,
                "updated_at": now,
            },
            statuses={SCHEDULED_CLAIMED},
            claim_id=claim_id,
        )
        return scheduled_record_from_doc_v2(doc) if doc else None

    def _patch_guarded(
        self,
        user_id: str,
        timer_id: str,
        patch: Mapping[str, Any],
        *,
        statuses: set[str],
        claim_id: str = "",
    ) -> Mapping[str, Any] | None:
        params: list[Any] = [Jsonb(dict(patch)), user_id, SCHEDULED_WAKE_STREAM_V2, user_id, SCHEDULED_WAKE_STREAM_V2, timer_id]
        guards = ["doc->>'status' = ANY(%s)"]
        params.append(list(statuses))
        if claim_id:
            guards.append("doc->>'claim_id' = %s")
            params.append(claim_id)
        sql = (
            "UPDATE user_logs SET doc = doc || %s "
            "WHERE user_id = %s AND stream = %s AND seq = ("
            "  SELECT seq FROM user_logs WHERE user_id = %s AND stream = %s AND item_key = %s "
            "  ORDER BY seq DESC LIMIT 1"
            ") AND "
            + " AND ".join(guards)
            + " RETURNING doc"
        )
        with db.get_pool().connection() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
        return row[0] if row is not None else None


WakeSubmitterV2 = Callable[[WakeEventV2], Any]


class ScheduledWakeServiceV2:
    def __init__(
        self,
        store: Any | None = None,
        *,
        pending_cap: int = DEFAULT_PENDING_TIMER_CAP_V2,
        claim_ttl_sec: float = DEFAULT_CLAIM_TTL_SEC_V2,
        owner_id: str = "scheduled_wake_v2",
    ) -> None:
        self.store = store or InMemoryScheduledWakeStoreV2()
        self.pending_cap = max(1, int(pending_cap))
        self.claim_ttl_sec = float(claim_ttl_sec)
        self.owner_id = owner_id

    def agent_context_for_user(self, user_id: str) -> dict[str, Any]:
        pending = [record for record in self.store.list_records(user_id) if record.pending_like]
        pending.sort(key=lambda item: (item.due_at, item.created_at, item.timer_id))
        return {
            "pending_count": len(pending),
            "pending_cap": self.pending_cap,
            "timers": [
                {
                    "wake_id": record.timer_id,
                    "at": record.at,
                    "tz": record.timezone,
                    "note": record.note,
                    "origin_refs": list(record.origin_refs),
                }
                for record in pending[:10]
            ],
        }

    def apply_turn_actions(
        self,
        user_id: str,
        actions: Sequence[Mapping[str, Any]],
        *,
        settings: ProactiveSettingsV2 | Mapping[str, Any] | None = None,
        turn_id: str = "",
        wake_ids: Sequence[str] = (),
        origin_refs: Sequence[str] = (),
        now: float | None = None,
        submit_wake: WakeSubmitterV2 | None = None,
    ) -> tuple[ScheduledWakeActionResultV2, ...]:
        now = _now(now)
        resolved = resolve_settings_v2(settings)
        results: list[ScheduledWakeActionResultV2] = []
        for action in actions or ():
            if not isinstance(action, Mapping):
                continue
            action_type = str(action.get("type") or "")
            if action_type not in {"schedule_wake", "cancel_wake"}:
                continue
            decision = evaluate_scheduled_action_v2(action, settings=resolved)
            if not decision.accepted:
                transparency_wake_id = ""
                if decision.transparency_required:
                    transparency_wake_id = self._submit_transparency_wake(
                        user_id,
                        reason=decision.reason,
                        action=action,
                        now=now,
                        origin_refs=origin_refs or wake_ids,
                        submit_wake=submit_wake,
                    )
                results.append(ScheduledWakeActionResultV2(
                    action_type=action_type,
                    status="rejected",
                    reason=decision.reason,
                    transparency_required=decision.transparency_required,
                    transparency_wake_id=transparency_wake_id,
                ))
                continue
            if action_type == "schedule_wake":
                results.append(self._schedule_action(
                    user_id,
                    action,
                    settings=resolved,
                    turn_id=turn_id,
                    wake_ids=wake_ids,
                    origin_refs=origin_refs,
                    now=now,
                ))
            else:
                timer_id = _coerce_str(action.get("wake_id") or action.get("timer_id") or action.get("id"), 200)
                reason = _coerce_str(action.get("reason") or "agent_cancel", 500)
                canceled = self.store.cancel(user_id, timer_id, reason=reason, now=now) if timer_id else None
                results.append(ScheduledWakeActionResultV2(
                    action_type=action_type,
                    status="canceled" if canceled else "not_found",
                    timer_id=timer_id,
                    reason=reason if canceled else "timer_not_found",
                ))
        return tuple(results)

    def _schedule_action(
        self,
        user_id: str,
        action: Mapping[str, Any],
        *,
        settings: ProactiveSettingsV2,
        turn_id: str,
        wake_ids: Sequence[str],
        origin_refs: Sequence[str],
        now: float,
    ) -> ScheduledWakeActionResultV2:
        try:
            wall_time, tz_name, due_at = schedule_instant_v2(action.get("at"), action.get("tz") or settings.timezone)
        except ValueError as exc:
            return ScheduledWakeActionResultV2("schedule_wake", "invalid", reason=str(exc))
        timer_id = _new_timer_id()
        refs = _coerce_origin_refs(action.get("origin_refs"), origin_refs or wake_ids)
        record = ScheduledWakeRecordV2(
            timer_id=timer_id,
            user_id=user_id,
            status=SCHEDULED_PENDING,
            at=wall_time,
            timezone=tz_name,
            due_at=due_at,
            note=_coerce_str(action.get("note"), 1000),
            origin_refs=refs,
            created_at=now,
            updated_at=now,
            turn_id=turn_id,
            wake_ids=tuple(str(item) for item in (wake_ids or ())),
        )
        self.store.create(record)
        evicted = self._enforce_pending_cap(user_id, now=now, keep_timer_id=timer_id)
        return ScheduledWakeActionResultV2(
            "schedule_wake",
            "scheduled",
            timer_id=timer_id,
            evicted_timer_ids=tuple(record.timer_id for record in evicted),
        )

    def _enforce_pending_cap(self, user_id: str, *, now: float, keep_timer_id: str) -> list[ScheduledWakeRecordV2]:
        pending = [record for record in self.store.list_records(user_id) if record.pending_like]
        if len(pending) <= self.pending_cap:
            return []
        pending.sort(key=lambda item: (item.created_at, item.timer_id))
        evicted: list[ScheduledWakeRecordV2] = []
        while len([record for record in pending if record.pending_like]) > self.pending_cap:
            victim = next((record for record in pending if record.timer_id != keep_timer_id and record.pending_like), None)
            if victim is None:
                break
            canceled = self.store.cancel(user_id, victim.timer_id, reason="pending_cap_evicted", now=now)
            if canceled is not None:
                evicted.append(canceled)
            pending = [record for record in self.store.list_records(user_id) if record.pending_like]
            pending.sort(key=lambda item: (item.created_at, item.timer_id))
        return evicted

    def fire_due_timers(
        self,
        user_id: str,
        *,
        settings: ProactiveSettingsV2 | Mapping[str, Any] | None = None,
        now: float | None = None,
        submit_wake: WakeSubmitterV2,
        owner_id: str | None = None,
    ) -> tuple[ScheduledWakeFireResultV2, ...]:
        now = _now(now)
        owner = owner_id or self.owner_id
        resolved = resolve_settings_v2(settings)
        results: list[ScheduledWakeFireResultV2] = []
        for candidate in self.store.due_candidates(user_id, now=now):
            claimed = self.store.claim_due(
                user_id,
                candidate.timer_id,
                owner_id=owner,
                now=now,
                ttl_sec=self.claim_ttl_sec,
            )
            if claimed is None:
                continue
            event = self._wake_event_from_record(claimed, now=now)
            decision = evaluate_wake_control_v2(event.source, trigger=event.trigger, settings=resolved)
            if decision.accepted:
                submitted = submit_wake(event)
                if getattr(submitted, "accepted", True):
                    self.store.mark_fired(user_id, claimed.timer_id, claimed.claim_id, wake_id=event.wake_id, now=now)
                    results.append(ScheduledWakeFireResultV2("fired", claimed.timer_id, wake_id=event.wake_id))
                    continue
                decision = submitted
            reason = str(getattr(decision, "reason", "") or "wake_rejected")
            transparency_wake_id = ""
            if getattr(decision, "transparency_required", False):
                try:
                    transparency_wake_id = self._submit_transparency_wake(
                        user_id,
                        reason=str(getattr(decision, "reason", "") or "scheduled_disabled"),
                        action={"type": "scheduled_wake", "wake_id": claimed.timer_id, "note": claimed.note},
                        now=now,
                        origin_refs=claimed.origin_refs,
                        submit_wake=submit_wake,
                        timer=claimed,
                    )
                except Exception as exc:
                    # The transparency notification could not be enqueued (transient
                    # submit/DB failure). Leave the timer claimed so it retries on a
                    # later tick instead of being marked terminally blocked with the
                    # explanation silently dropped. A deliberate policy rejection
                    # (accepted=False) does NOT raise and falls through to mark_blocked.
                    log.warning(
                        "scheduled wake transparency submit failed, deferring timer %s (%s): %s",
                        claimed.timer_id, reason, exc,
                    )
                    results.append(ScheduledWakeFireResultV2(
                        "deferred",
                        claimed.timer_id,
                        reason=reason,
                    ))
                    continue
            self.store.mark_blocked(
                user_id,
                claimed.timer_id,
                claimed.claim_id,
                reason=reason,
                transparency_wake_id=transparency_wake_id,
                now=now,
            )
            results.append(ScheduledWakeFireResultV2(
                "blocked",
                claimed.timer_id,
                reason=reason,
                transparency_wake_id=transparency_wake_id,
            ))
        return tuple(results)

    def _wake_event_from_record(self, record: ScheduledWakeRecordV2, *, now: float) -> WakeEventV2:
        return WakeEventV2(
            user_id=record.user_id,
            source="scheduled_wake",
            trigger="scheduled_wake",
            created_at=now,
            change_digest=record.note,
            timezone=record.timezone,
            scheduled_note=record.note,
            origin_refs=record.origin_refs,
            payload={
                "scheduled_wake": {
                    "wake_id": record.timer_id,
                    "at": record.at,
                    "tz": record.timezone,
                    "due_at": record.due_at,
                }
            },
        )

    def _submit_transparency_wake(
        self,
        user_id: str,
        *,
        reason: str,
        action: Mapping[str, Any],
        now: float,
        origin_refs: Sequence[str],
        submit_wake: WakeSubmitterV2 | None,
        timer: ScheduledWakeRecordV2 | None = None,
    ) -> str:
        if submit_wake is None:
            return ""
        payload = {
            "type": "scheduled_wake_transparency",
            "reason": reason,
            "action": dict(action or {}),
        }
        if timer is not None:
            payload["timer"] = {
                "wake_id": timer.timer_id,
                "at": timer.at,
                "tz": timer.timezone,
                "note": timer.note,
            }
        event = WakeEventV2(
            user_id=user_id,
            source="background_result",
            trigger="scheduled_transparency",
            created_at=now,
            origin_refs=_coerce_origin_refs(origin_refs),
            background_payload=payload,
        )
        decision = submit_wake(event)
        return event.wake_id if getattr(decision, "accepted", True) else ""
