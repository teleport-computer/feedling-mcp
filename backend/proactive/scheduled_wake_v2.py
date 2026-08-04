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
import re
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
MIN_SELF_WAKE_LEAD_SEC_V2 = float(os.environ.get("FEEDLING_SELF_WAKE_MIN_LEAD_SEC_V2", "300"))

REPEAT_DAILY = "daily"
REPEAT_WEEKLY = "weekly"
REPEAT_INTERVALS_V2 = {
    REPEAT_DAILY: 86400.0,
    REPEAT_WEEKLY: 604800.0,
}


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


def _coerce_repeat(value: Any) -> str:
    repeat = _coerce_str(value, 20).lower()
    if repeat and repeat not in REPEAT_INTERVALS_V2:
        raise ValueError("invalid_repeat")
    return repeat


def _zone(tz: Any) -> ZoneInfo:
    name = _coerce_str(tz, 80) or DEFAULT_TIMEZONE_V2
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TIMEZONE_V2)


_ENGLISH_RELATIVE_AT_V2 = re.compile(
    r"^(?:in\s+|\+\s*)?(?P<amount>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|hrs?|h|days?|d)"
    r"(?:\s+(?:later|from\s+now))?$",
    re.IGNORECASE,
)
_CHINESE_RELATIVE_AT_V2 = re.compile(
    r"^(?P<amount>\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百半]+)\s*"
    r"(?P<unit>秒钟?|分钟|分|小时|钟头|天)(?:以?后)?$"
)


def _chinese_number_v2(raw: str) -> float | None:
    if raw == "半":
        return 0.5
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if all(char in digits for char in raw):
        return float("".join(str(digits[char]) for char in raw))
    total = 0
    current = 0
    for char in raw:
        if char in digits:
            current = digits[char]
        elif char == "十":
            total += (current or 1) * 10
            current = 0
        elif char == "百":
            total += (current or 1) * 100
            current = 0
        else:
            return None
    return float(total + current)


def _relative_delay_seconds_v2(raw_at: str) -> float | None:
    match = _ENGLISH_RELATIVE_AT_V2.fullmatch(raw_at)
    if match:
        amount = float(match.group("amount"))
        unit = match.group("unit").lower()
        if unit.startswith(("s", "sec")):
            multiplier = 1.0
        elif unit.startswith(("m", "min")):
            multiplier = 60.0
        elif unit.startswith(("h", "hr")):
            multiplier = 3600.0
        else:
            multiplier = 86400.0
        return amount * multiplier

    match = _CHINESE_RELATIVE_AT_V2.fullmatch(raw_at)
    if not match:
        return None
    amount_raw = match.group("amount")
    amount = float(amount_raw) if amount_raw[0].isdigit() else _chinese_number_v2(amount_raw)
    if amount is None:
        return None
    unit = match.group("unit")
    if unit.startswith("秒"):
        multiplier = 1.0
    elif unit in {"分钟", "分"}:
        multiplier = 60.0
    elif unit in {"小时", "钟头"}:
        multiplier = 3600.0
    else:
        multiplier = 86400.0
    return amount * multiplier


def schedule_instant_v2(at: Any, tz: Any, *, now: float | None = None) -> tuple[str, str, float]:
    """Return (wall_time, timezone_name, due_at_epoch).

    The durable record keeps wall-clock time plus event timezone. `due_at` is a
    derived scheduling index and can be recomputed after process restart.
    """
    raw_at = _coerce_str(at, 120)
    if not raw_at:
        raise ValueError("missing_at")
    zone = _zone(tz)
    relative_delay = _relative_delay_seconds_v2(raw_at)
    if relative_delay is not None:
        if relative_delay <= 0:
            raise ValueError("invalid_at")
        due_at = _now(now) + relative_delay
        local = datetime.fromtimestamp(due_at, timezone.utc).astimezone(zone)
        return local.replace(tzinfo=None).isoformat(), zone.key, due_at
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


def _wall_time_for_due_at(due_at: float, tz_name: str) -> str:
    local = datetime.fromtimestamp(float(due_at), timezone.utc).astimezone(_zone(tz_name))
    return local.replace(tzinfo=None).isoformat()


@dataclass(frozen=True)
class ScheduledWakeRecordV2:
    timer_id: str
    user_id: str
    status: str
    at: str
    timezone: str
    due_at: float
    repeat: str = ""
    repeat_series_id: str = ""
    repeat_canceled_at: float = 0.0
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
    fired_job_id: int = 0
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
    next_trigger_at: str = ""
    timezone: str = ""
    repeat: str = ""

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
        if self.next_trigger_at:
            out["next_trigger_at"] = self.next_trigger_at
        if self.timezone:
            out["timezone"] = self.timezone
        if self.repeat:
            out["repeat"] = self.repeat
        return out


@dataclass(frozen=True)
class ScheduledWakeFireResultV2:
    status: str
    timer_id: str
    wake_id: str = ""
    reason: str = ""
    transparency_wake_id: str = ""
    next_timer_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        out = {"status": self.status, "timer_id": self.timer_id}
        if self.wake_id:
            out["wake_id"] = self.wake_id
        if self.reason:
            out["reason"] = self.reason
        if self.transparency_wake_id:
            out["transparency_wake_id"] = self.transparency_wake_id
        if self.next_timer_id:
            out["next_timer_id"] = self.next_timer_id
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
        "repeat": record.repeat,
        "repeat_series_id": record.repeat_series_id,
        "repeat_canceled_at": float(record.repeat_canceled_at),
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
        "fired_job_id": int(record.fired_job_id),
        "canceled_at": float(record.canceled_at),
        "cancel_reason": record.cancel_reason,
        "blocked_at": float(record.blocked_at),
        "block_reason": record.block_reason,
        "transparency_wake_id": record.transparency_wake_id,
    }


def scheduled_record_from_doc_v2(doc: Mapping[str, Any]) -> ScheduledWakeRecordV2:
    repeat = str(doc.get("repeat") or "")
    timer_id = str(doc.get("timer_id") or "")
    return ScheduledWakeRecordV2(
        timer_id=timer_id,
        user_id=str(doc.get("user_id") or ""),
        status=str(doc.get("status") or ""),
        at=str(doc.get("at") or ""),
        timezone=str(doc.get("timezone") or ""),
        due_at=float(doc.get("due_at") or 0.0),
        repeat=repeat,
        repeat_series_id=str(doc.get("repeat_series_id") or (timer_id if repeat else "")),
        repeat_canceled_at=float(doc.get("repeat_canceled_at") or 0.0),
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
        fired_job_id=int(doc.get("fired_job_id") or 0),
        canceled_at=float(doc.get("canceled_at") or 0.0),
        cancel_reason=str(doc.get("cancel_reason") or ""),
        blocked_at=float(doc.get("blocked_at") or 0.0),
        block_reason=str(doc.get("block_reason") or ""),
        transparency_wake_id=str(doc.get("transparency_wake_id") or ""),
        doc=dict(doc),
    )


def _same_pending_schedule(
    record: ScheduledWakeRecordV2,
    *,
    due_at: float,
    repeat: str,
) -> bool:
    return (
        record.pending_like
        and float(record.due_at) == float(due_at)
        and record.repeat == repeat
    )


def _scheduled_lock_key(user_id: str) -> str:
    return f"scheduled-wake:{user_id}"


class InMemoryScheduledWakeStoreV2:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, dict[str, Any]]] = {}

    def create(self, record: ScheduledWakeRecordV2) -> ScheduledWakeRecordV2:
        with self._lock:
            self._records.setdefault(record.user_id, {})[record.timer_id] = scheduled_record_to_doc_v2(record)
            return scheduled_record_from_doc_v2(self._records[record.user_id][record.timer_id])

    def create_unique_pending(
        self,
        record: ScheduledWakeRecordV2,
    ) -> tuple[ScheduledWakeRecordV2, bool]:
        with self._lock:
            records = self._records.setdefault(record.user_id, {})
            for doc in records.values():
                existing = scheduled_record_from_doc_v2(doc)
                if _same_pending_schedule(
                    existing,
                    due_at=record.due_at,
                    repeat=record.repeat,
                ):
                    return existing, False
            records[record.timer_id] = scheduled_record_to_doc_v2(record)
            return scheduled_record_from_doc_v2(records[record.timer_id]), True

    def list_records(self, user_id: str) -> list[ScheduledWakeRecordV2]:
        with self._lock:
            rows = list((self._records.get(user_id) or {}).values())
        return [scheduled_record_from_doc_v2(row) for row in rows]

    def cancel(self, user_id: str, timer_id: str, *, reason: str, now: float) -> ScheduledWakeRecordV2 | None:
        with self._lock:
            records = self._records.get(user_id) or {}
            doc = records.get(timer_id)
            if not doc:
                return None
            repeat = str(doc.get("repeat") or "")
            series_id = str(doc.get("repeat_series_id") or "")
            if repeat and series_id:
                for candidate in records.values():
                    if str(candidate.get("repeat_series_id") or "") != series_id:
                        continue
                    candidate.update({
                        "repeat_canceled_at": now,
                        "cancel_reason": reason,
                        "updated_at": now,
                    })
                    if str(candidate.get("status") or "") in {
                        SCHEDULED_PENDING,
                        SCHEDULED_CLAIMED,
                    }:
                        candidate.update({
                            "status": SCHEDULED_CANCELED,
                            "canceled_at": now,
                        })
                return scheduled_record_from_doc_v2(records[timer_id])
            if str(doc.get("status") or "") not in {
                SCHEDULED_PENDING,
                SCHEDULED_CLAIMED,
            }:
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

    def mark_fired(
        self,
        user_id: str,
        timer_id: str,
        claim_id: str,
        *,
        wake_id: str,
        job_id: int = 0,
        now: float,
        next_record: ScheduledWakeRecordV2 | None = None,
    ) -> tuple[ScheduledWakeRecordV2 | None, ScheduledWakeRecordV2 | None]:
        with self._lock:
            records = self._records.get(user_id) or {}
            doc = records.get(timer_id)
            if not doc or str(doc.get("status") or "") != SCHEDULED_CLAIMED:
                return None, None
            if str(doc.get("claim_id") or "") != claim_id:
                return None, None
            if float(doc.get("repeat_canceled_at") or 0.0) > 0:
                return None, None
            doc.update({
                "status": SCHEDULED_FIRED,
                "fired_at": now,
                "fired_wake_id": wake_id,
                "fired_job_id": int(job_id),
                "updated_at": now,
            })
            fired = scheduled_record_from_doc_v2(doc)
            if next_record is None:
                return fired, None
            for candidate_doc in records.values():
                candidate = scheduled_record_from_doc_v2(candidate_doc)
                if _same_pending_schedule(
                    candidate,
                    due_at=next_record.due_at,
                    repeat=next_record.repeat,
                ):
                    return fired, candidate
            records[next_record.timer_id] = scheduled_record_to_doc_v2(next_record)
            return fired, scheduled_record_from_doc_v2(records[next_record.timer_id])

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

    def create_unique_pending(
        self,
        record: ScheduledWakeRecordV2,
    ) -> tuple[ScheduledWakeRecordV2, bool]:
        doc = scheduled_record_to_doc_v2(record)
        inserted_seq: int | None = None
        with db.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (_scheduled_lock_key(record.user_id),),
                    )
                    cur.execute(
                        "SELECT doc FROM user_logs WHERE user_id=%s AND stream=%s "
                        "AND doc->>'status'=ANY(%s) "
                        "AND COALESCE(NULLIF(doc->>'due_at','')::float8, 0)=%s "
                        "AND COALESCE(doc->>'repeat','')=%s "
                        "ORDER BY seq DESC LIMIT 1",
                        (
                            record.user_id,
                            SCHEDULED_WAKE_STREAM_V2,
                            [SCHEDULED_PENDING, SCHEDULED_CLAIMED],
                            float(record.due_at),
                            record.repeat,
                        ),
                    )
                    existing = cur.fetchone()
                    if existing is not None:
                        return scheduled_record_from_doc_v2(existing[0]), False
                    cur.execute(
                        "INSERT INTO user_logs (user_id, stream, ts, item_key, doc) "
                        "VALUES (%s, %s, %s, %s, %s) RETURNING seq",
                        (
                            record.user_id,
                            SCHEDULED_WAKE_STREAM_V2,
                            record.created_at,
                            record.timer_id,
                            Jsonb(doc),
                        ),
                    )
                    inserted_seq = int(cur.fetchone()[0])
        from tee_shadow import mirror
        mirror.execute(
            "INSERT INTO user_logs (user_id, stream, seq, ts, item_key, doc) "
            "OVERRIDING SYSTEM VALUE VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (user_id, stream, seq) DO NOTHING",
            (
                record.user_id,
                SCHEDULED_WAKE_STREAM_V2,
                inserted_seq,
                record.created_at,
                record.timer_id,
                Jsonb(doc),
            ),
        )
        return scheduled_record_from_doc_v2(doc), True

    def list_records(self, user_id: str) -> list[ScheduledWakeRecordV2]:
        return [scheduled_record_from_doc_v2(doc) for doc in db.log_read_all(user_id, SCHEDULED_WAKE_STREAM_V2)]

    def cancel(self, user_id: str, timer_id: str, *, reason: str, now: float) -> ScheduledWakeRecordV2 | None:
        repeat_target: Mapping[str, Any] | None = None
        repeat_sql = (
            "UPDATE user_logs SET doc = doc || %s || "
            "CASE WHEN doc->>'status'=ANY(%s) THEN %s ELSE '{}'::jsonb END "
            "WHERE user_id=%s AND stream=%s "
            "AND doc->>'repeat_series_id'=%s RETURNING item_key,doc"
        )
        repeat_params: tuple[Any, ...] | None = None
        with db.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (_scheduled_lock_key(user_id),),
                    )
                    cur.execute(
                        "SELECT doc FROM user_logs WHERE user_id=%s AND stream=%s "
                        "AND item_key=%s ORDER BY seq DESC LIMIT 1 FOR UPDATE",
                        (user_id, SCHEDULED_WAKE_STREAM_V2, timer_id),
                    )
                    target_row = cur.fetchone()
                    if target_row is None:
                        return None
                    target = dict(target_row[0] or {})
                    repeat = str(target.get("repeat") or "")
                    series_id = str(target.get("repeat_series_id") or "")
                    if repeat and series_id:
                        common_patch = {
                            "repeat_canceled_at": now,
                            "cancel_reason": reason,
                            "updated_at": now,
                        }
                        pending_patch = {
                            "status": SCHEDULED_CANCELED,
                            "canceled_at": now,
                        }
                        repeat_params = (
                            Jsonb(common_patch),
                            [SCHEDULED_PENDING, SCHEDULED_CLAIMED],
                            Jsonb(pending_patch),
                            user_id,
                            SCHEDULED_WAKE_STREAM_V2,
                            series_id,
                        )
                        cur.execute(repeat_sql, repeat_params)
                        for item_key, candidate_doc in cur.fetchall():
                            if str(item_key or "") == timer_id:
                                repeat_target = candidate_doc
        if repeat_params is not None:
            from tee_shadow import mirror
            mirror.execute(repeat_sql, repeat_params)
            return scheduled_record_from_doc_v2(repeat_target or target)
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
        if row is not None:
            # 只在 primary 真抢到 claim 时才镜像（与 db.chat_try_claim_reply 同规）。
            from tee_shadow import mirror
            mirror.execute(sql, tuple(params))
        return scheduled_record_from_doc_v2(row[0]) if row is not None else None

    def mark_fired(
        self,
        user_id: str,
        timer_id: str,
        claim_id: str,
        *,
        wake_id: str,
        job_id: int = 0,
        now: float,
        next_record: ScheduledWakeRecordV2 | None = None,
    ) -> tuple[ScheduledWakeRecordV2 | None, ScheduledWakeRecordV2 | None]:
        patch = {
            "status": SCHEDULED_FIRED,
            "fired_at": now,
            "fired_wake_id": wake_id,
            "fired_job_id": int(job_id),
            "updated_at": now,
        }
        update_params = (
            Jsonb(patch),
            user_id,
            SCHEDULED_WAKE_STREAM_V2,
            user_id,
            SCHEDULED_WAKE_STREAM_V2,
            timer_id,
            claim_id,
        )
        update_sql = (
            "UPDATE user_logs SET doc=doc || %s "
            "WHERE user_id=%s AND stream=%s AND seq=("
            "SELECT seq FROM user_logs WHERE user_id=%s AND stream=%s "
            "AND item_key=%s ORDER BY seq DESC LIMIT 1) "
            "AND doc->>'status'='claimed' AND doc->>'claim_id'=%s "
            "AND COALESCE(NULLIF(doc->>'repeat_canceled_at','')::float8, 0)=0 "
            "RETURNING doc"
        )
        fired_doc: Mapping[str, Any] | None = None
        next_doc: Mapping[str, Any] | None = None
        next_inserted_seq: int | None = None
        with db.get_pool().connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (_scheduled_lock_key(user_id),),
                    )
                    cur.execute(update_sql, update_params)
                    fired_row = cur.fetchone()
                    if fired_row is None:
                        return None, None
                    fired_doc = fired_row[0]
                    if next_record is not None:
                        cur.execute(
                            "SELECT doc FROM user_logs WHERE user_id=%s AND stream=%s "
                            "AND doc->>'status'=ANY(%s) "
                            "AND COALESCE(NULLIF(doc->>'due_at','')::float8, 0)=%s "
                            "AND COALESCE(doc->>'repeat','')=%s "
                            "ORDER BY seq DESC LIMIT 1",
                            (
                                user_id,
                                SCHEDULED_WAKE_STREAM_V2,
                                [SCHEDULED_PENDING, SCHEDULED_CLAIMED],
                                float(next_record.due_at),
                                next_record.repeat,
                            ),
                        )
                        existing = cur.fetchone()
                        if existing is not None:
                            next_doc = existing[0]
                        else:
                            next_doc = scheduled_record_to_doc_v2(next_record)
                            cur.execute(
                                "INSERT INTO user_logs "
                                "(user_id,stream,ts,item_key,doc) "
                                "VALUES (%s,%s,%s,%s,%s) RETURNING seq",
                                (
                                    user_id,
                                    SCHEDULED_WAKE_STREAM_V2,
                                    next_record.created_at,
                                    next_record.timer_id,
                                    Jsonb(next_doc),
                                ),
                            )
                            next_inserted_seq = int(cur.fetchone()[0])
        from tee_shadow import mirror
        mirror.execute(update_sql, update_params)
        if next_record is not None and next_inserted_seq is not None:
            mirror.execute(
                "INSERT INTO user_logs (user_id,stream,seq,ts,item_key,doc) "
                "OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (user_id,stream,seq) DO NOTHING",
                (
                    user_id,
                    SCHEDULED_WAKE_STREAM_V2,
                    next_inserted_seq,
                    next_record.created_at,
                    next_record.timer_id,
                    Jsonb(next_doc),
                ),
            )
        return (
            scheduled_record_from_doc_v2(fired_doc),
            scheduled_record_from_doc_v2(next_doc) if next_doc is not None else None,
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
        if row is not None:
            # 只在 primary 真命中守卫（状态/claim_id 匹配）时才镜像，与
            # claim_due 同规：被拒绝/无操作的守卫更新不是真实状态迁移，不应
            # 回放到 TEE；真实漏写由 reconciler 收敛。
            from tee_shadow import mirror
            mirror.execute(sql, tuple(params))
        return row[0] if row is not None else None


WakeSubmitterV2 = Callable[[WakeEventV2], Any]


class ScheduledWakeServiceV2:
    def __init__(
        self,
        store: Any | None = None,
        *,
        pending_cap: int = DEFAULT_PENDING_TIMER_CAP_V2,
        claim_ttl_sec: float = DEFAULT_CLAIM_TTL_SEC_V2,
        self_wake_min_lead_sec: float = MIN_SELF_WAKE_LEAD_SEC_V2,
        owner_id: str = "scheduled_wake_v2",
    ) -> None:
        self.store = store or InMemoryScheduledWakeStoreV2()
        self.pending_cap = max(1, int(pending_cap))
        self.claim_ttl_sec = float(claim_ttl_sec)
        self.self_wake_min_lead_sec = max(0.0, float(self_wake_min_lead_sec))
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
                    "repeat": record.repeat,
                    "note": record.note,
                    "origin_refs": list(record.origin_refs),
                }
                for record in pending[:self.pending_cap]
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
        self_wake: bool = False,
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
                    self_wake=self_wake,
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
                    next_trigger_at=canceled.at if canceled else "",
                    timezone=canceled.timezone if canceled else "",
                    repeat=canceled.repeat if canceled else "",
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
        self_wake: bool,
        now: float,
    ) -> ScheduledWakeActionResultV2:
        try:
            wall_time, tz_name, due_at = schedule_instant_v2(
                action.get("at"),
                action.get("tz") or settings.timezone,
                now=now,
            )
            repeat = _coerce_repeat(action.get("repeat"))
        except ValueError as exc:
            return ScheduledWakeActionResultV2("schedule_wake", "invalid", reason=str(exc))
        clamped = False
        min_due_at = now + self.self_wake_min_lead_sec
        if self_wake and self.self_wake_min_lead_sec > 0 and due_at < min_due_at:
            due_at = min_due_at
            wall_time = _wall_time_for_due_at(due_at, tz_name)
            clamped = True
        timer_id = _new_timer_id()
        refs = _coerce_origin_refs(action.get("origin_refs"), origin_refs or wake_ids)
        record = ScheduledWakeRecordV2(
            timer_id=timer_id,
            user_id=user_id,
            status=SCHEDULED_PENDING,
            at=wall_time,
            timezone=tz_name,
            due_at=due_at,
            repeat=repeat,
            repeat_series_id=timer_id if repeat else "",
            note=_coerce_str(action.get("note"), 1000),
            origin_refs=refs,
            created_at=now,
            updated_at=now,
            turn_id=turn_id,
            wake_ids=tuple(str(item) for item in (wake_ids or ())),
        )
        persisted, created = self.store.create_unique_pending(record)
        evicted = (
            self._enforce_pending_cap(
                user_id,
                now=now,
                keep_timer_id=persisted.timer_id,
            )
            if created
            else []
        )
        return ScheduledWakeActionResultV2(
            "schedule_wake",
            "scheduled",
            timer_id=persisted.timer_id,
            reason=(
                "already_scheduled"
                if not created
                else "self_wake_min_lead_clamped" if clamped else ""
            ),
            evicted_timer_ids=tuple(record.timer_id for record in evicted),
            next_trigger_at=persisted.at,
            timezone=persisted.timezone,
            repeat=persisted.repeat,
        )

    def _next_repeat_record(
        self,
        record: ScheduledWakeRecordV2,
        *,
        now: float,
    ) -> ScheduledWakeRecordV2 | None:
        interval = REPEAT_INTERVALS_V2.get(record.repeat)
        if interval is None or record.repeat_canceled_at > 0:
            return None
        due_at = float(record.due_at) + interval
        timer_id = _new_timer_id()
        return ScheduledWakeRecordV2(
            timer_id=timer_id,
            user_id=record.user_id,
            status=SCHEDULED_PENDING,
            at=_wall_time_for_due_at(due_at, record.timezone),
            timezone=record.timezone,
            due_at=due_at,
            repeat=record.repeat,
            repeat_series_id=record.repeat_series_id or record.timer_id,
            note=record.note,
            origin_refs=record.origin_refs,
            created_at=now,
            updated_at=now,
            turn_id=record.turn_id,
            wake_ids=record.wake_ids,
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
                    fired_record, next_record = self.store.mark_fired(
                        user_id,
                        claimed.timer_id,
                        claimed.claim_id,
                        wake_id=event.wake_id,
                        job_id=int(getattr(submitted, "job_id", 0) or 0),
                        now=now,
                        next_record=self._next_repeat_record(claimed, now=now),
                    )
                    if fired_record is not None:
                        if next_record is not None:
                            self._enforce_pending_cap(
                                user_id,
                                now=now,
                                keep_timer_id=next_record.timer_id,
                            )
                        results.append(ScheduledWakeFireResultV2(
                            "fired",
                            claimed.timer_id,
                            wake_id=event.wake_id,
                            next_timer_id=(
                                next_record.timer_id if next_record is not None else ""
                            ),
                        ))
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
                    "repeat": record.repeat,
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
                "repeat": timer.repeat,
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
