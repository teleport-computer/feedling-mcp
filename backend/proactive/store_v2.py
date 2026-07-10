"""DB-backed storage substrate for Proactive/Perception Runtime V2.

This module is PR1 infrastructure only. It does not connect hosted or resident
production execution. The goal is to provide persistent wake inbox, turn state,
and reclaimable leases without modeling V2 state on legacy `proactive_jobs`.
"""
from __future__ import annotations

from dataclasses import dataclass
import time
import uuid
from typing import Any, Mapping

from psycopg.types.json import Jsonb

import db
from proactive.controls_v2 import (
    SETTINGS_KIND_V2,
    ProactiveSettingsV2,
    merge_settings_patch_v2,
    now_settings_updated_at_v2,
    resolve_settings_v2,
    settings_v2_to_doc,
)
from proactive.runtime_v2 import (
    BackgroundLeaseRegistryV2,
    LeaseV2,
    MergedWakeContextV2,
    TurnLeaseRegistryV2,
    TurnOutcomeV2,
    WakeEventV2,
)

WAKE_STREAM_V2 = "proactive_wakes_v2"
TURN_STREAM_V2 = "proactive_turns_v2"
TURN_ACTION_STREAM_V2 = "proactive_turn_actions_v2"
BACKGROUND_JOB_STREAM_V2 = "proactive_background_jobs_v2"
LEASE_KIND_PREFIX_V2 = "proactive_v2_lease:"
LEGACY_PROACTIVE_SETTINGS_KIND = "proactive_settings"

WAKE_PENDING = "pending"
WAKE_DRAINED = "drained"
TURN_RUNNING = "running"
TURN_COMPLETED = "completed"
TURN_STALE_RECOVERED = "stale_recovered"
BACKGROUND_PENDING = "pending"
BACKGROUND_RUNNING = "running"
BACKGROUND_COMPLETED = "completed"
BACKGROUND_FAILED = "failed"


def _new_turn_id() -> str:
    return "turn_" + uuid.uuid4().hex[:16]


def _new_background_job_id() -> str:
    return "bg_" + uuid.uuid4().hex[:16]


def _now(now: float | None = None) -> float:
    return time.time() if now is None else float(now)


def wake_event_v2_to_doc(event: WakeEventV2, *, status: str = WAKE_PENDING) -> dict[str, Any]:
    return {
        "kind": "wake_v2",
        "status": status,
        "wake_id": event.wake_id,
        "user_id": event.user_id,
        "source": event.source,
        "trigger": event.trigger,
        "created_at": float(event.created_at),
        "latency_sensitive": bool(event.latency_sensitive),
        "manual": bool(event.manual),
        "change_digest": event.change_digest,
        "presence_hints": dict(event.presence_hints or {}),
        "switches": dict(event.switches or {}),
        "timezone": event.timezone,
        "scheduled_note": event.scheduled_note,
        "origin_refs": list(event.origin_refs or ()),
        "background_payload": dict(event.background_payload or {}),
        "payload": dict(event.payload or {}),
        "dedupe_key": event.dedupe_key,
    }


def wake_event_v2_from_doc(doc: Mapping[str, Any]) -> WakeEventV2:
    return WakeEventV2(
        user_id=str(doc.get("user_id") or ""),
        source=str(doc.get("source") or ""),
        trigger=str(doc.get("trigger") or ""),
        wake_id=str(doc.get("wake_id") or ""),
        created_at=float(doc.get("created_at") or doc.get("ts") or 0.0),
        latency_sensitive=bool(doc.get("latency_sensitive")),
        manual=bool(doc.get("manual")),
        change_digest=str(doc.get("change_digest") or ""),
        presence_hints=dict(doc.get("presence_hints") or {}),
        switches=dict(doc.get("switches") or {}),
        timezone=str(doc.get("timezone") or ""),
        scheduled_note=str(doc.get("scheduled_note") or ""),
        origin_refs=tuple(str(item) for item in (doc.get("origin_refs") or ())),
        background_payload=dict(doc.get("background_payload") or {}),
        payload=dict(doc.get("payload") or {}),
    )


def _lease_to_doc(lease: LeaseV2) -> dict[str, Any]:
    return {
        "kind": "lease_v2",
        "scope": lease.scope,
        "user_id": lease.user_id,
        "owner_id": lease.owner_id,
        "lease_id": lease.lease_id,
        "acquired_at": float(lease.acquired_at),
        "expires_at": float(lease.expires_at),
    }


def _lease_from_doc(doc: Mapping[str, Any]) -> LeaseV2:
    return LeaseV2(
        scope=str(doc.get("scope") or ""),
        owner_id=str(doc.get("owner_id") or ""),
        lease_id=str(doc.get("lease_id") or ""),
        acquired_at=float(doc.get("acquired_at") or 0.0),
        expires_at=float(doc.get("expires_at") or 0.0),
        user_id=str(doc.get("user_id") or ""),
    )


def _lease_kind(scope: str) -> str:
    return LEASE_KIND_PREFIX_V2 + str(scope)


def _patch_log_item_guarded(
    user_id: str,
    stream: str,
    item_key: str,
    patch: Mapping[str, Any],
    *,
    status: str | None = None,
    lease_id: str | None = None,
    lease_expired_at_or_before: float | None = None,
) -> dict[str, Any] | None:
    params: list[Any] = [Jsonb(dict(patch)), user_id, stream, user_id, stream, item_key]
    guards: list[str] = []
    if status is not None:
        guards.append("doc->>'status' = %s")
        params.append(status)
    if lease_id is not None:
        guards.append("doc->>'lease_id' = %s")
        params.append(lease_id)
    if lease_expired_at_or_before is not None:
        guards.append("COALESCE(NULLIF(doc->>'lease_expires_at','')::float8, 0) <= %s")
        params.append(float(lease_expired_at_or_before))
    guard_sql = (" AND " + " AND ".join(guards)) if guards else ""
    sql = (
        "UPDATE user_logs SET doc = doc || %s "
        "WHERE user_id = %s AND stream = %s AND seq = ("
        "  SELECT seq FROM user_logs WHERE user_id = %s AND stream = %s AND item_key = %s "
        "  ORDER BY seq DESC LIMIT 1"
        ")"
        + guard_sql
        + " RETURNING doc"
    )
    try:
        with db.get_pool().connection() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
    except Exception as e:
        import logging

        logging.getLogger("proactive.store_v2").error(
            "patch_log_item_guarded(%s,%s,%s) failed: %s", user_id, stream, item_key, e
        )
        return None
    if row is not None:
        # Only mirror when the primary's guard actually matched a row (same
        # convention as db.chat_try_claim_reply / scheduled_wake claim_due):
        # a rejected guarded update is not a state transition and must not be
        # replayed against TEE. Missed real updates are caught by the
        # reconciler.
        from tee_shadow import mirror
        mirror.execute(sql, tuple(params))
    return row[0] if row is not None else None


class DBWakeInboxV2:
    """Persistent wake inbox over `user_logs`, independent from old jobs."""

    def push(self, event: WakeEventV2) -> None:
        doc = wake_event_v2_to_doc(event)
        db.log_append(
            event.user_id,
            WAKE_STREAM_V2,
            doc,
            ts=event.created_at,
            item_key=event.wake_id,
        )

    def pending_events(self, user_id: str) -> list[WakeEventV2]:
        docs = db.log_read_all(user_id, WAKE_STREAM_V2)
        events = [
            wake_event_v2_from_doc(doc)
            for doc in docs
            if str(doc.get("status") or "") == WAKE_PENDING
        ]
        return sorted(events, key=lambda item: (item.created_at, item.wake_id))

    def drain_ready(
        self,
        user_id: str,
        *,
        now: float | None = None,
        merge_window_sec: float = 2.0,
        drain_id: str = "",
    ) -> list[WakeEventV2]:
        now = _now(now)
        items = self.pending_events(user_id)
        if not items:
            return []
        latency_indices = [idx for idx, event in enumerate(items) if event.latency_sensitive]
        if latency_indices:
            end = latency_indices[0] + 1
        else:
            first_ts = items[0].created_at
            if now - first_ts < merge_window_sec:
                return []
            end = 0
            cutoff = first_ts + merge_window_sec
            while end < len(items) and items[end].created_at <= cutoff:
                end += 1

        claimed: list[WakeEventV2] = []
        for event in items[:end]:
            patch = {
                "status": WAKE_DRAINED,
                "drained_at": now,
            }
            if drain_id:
                patch["drain_id"] = drain_id
            doc = db.log_patch_item(
                user_id,
                WAKE_STREAM_V2,
                event.wake_id,
                patch,
                only_if_status=WAKE_PENDING,
            )
            if doc is not None:
                claimed.append(wake_event_v2_from_doc(doc))
        return claimed


class DBLeaseRegistryV2:
    def try_acquire(
        self,
        user_id: str,
        scope: str,
        *,
        owner_id: str,
        now: float | None = None,
        ttl_sec: float,
    ) -> LeaseV2 | None:
        now = _now(now)
        lease = LeaseV2(
            user_id=str(user_id or ""),
            scope=str(scope or ""),
            owner_id=str(owner_id or "unknown"),
            acquired_at=now,
            expires_at=now + float(ttl_sec),
        )
        try:
            with db.get_pool().connection() as conn:
                # tee-mirror: 有意不镜像（TTL lease，reconciler 收敛）
                row = conn.execute(
                    "INSERT INTO user_blobs (user_id, kind, doc) VALUES (%s, %s, %s) "
                    "ON CONFLICT (user_id, kind) DO UPDATE SET doc = EXCLUDED.doc "
                    "WHERE COALESCE(NULLIF(user_blobs.doc->>'expires_at','')::float8, 0) <= %s "
                    "RETURNING doc",
                    (lease.user_id, _lease_kind(lease.scope), Jsonb(_lease_to_doc(lease)), now),
                ).fetchone()
            return _lease_from_doc(row[0]) if row is not None else None
        except Exception as e:
            import logging

            logging.getLogger("proactive.store_v2").error(
                "try_acquire_lease(%s,%s) failed: %s", user_id, scope, e
            )
            return None

    def current(self, user_id: str, scope: str, *, now: float | None = None) -> LeaseV2 | None:
        now = _now(now)
        doc = db.get_blob(user_id, _lease_kind(scope))
        if not isinstance(doc, dict):
            return None
        lease = _lease_from_doc(doc)
        if lease.expired(now):
            self.release(lease)
            return None
        return lease

    def release(self, lease: LeaseV2) -> bool:
        if not lease.user_id or not lease.scope or not lease.lease_id:
            return False
        try:
            with db.get_pool().connection() as conn:
                row = conn.execute(
                    "DELETE FROM user_blobs "
                    "WHERE user_id = %s AND kind = %s AND doc->>'lease_id' = %s "
                    "RETURNING doc",
                    (lease.user_id, _lease_kind(lease.scope), lease.lease_id),
                ).fetchone()
            return row is not None
        except Exception as e:
            import logging

            logging.getLogger("proactive.store_v2").error(
                "release_lease(%s,%s) failed: %s", lease.user_id, lease.scope, e
            )
            return False


class DBTurnLeaseRegistryV2(TurnLeaseRegistryV2):
    def __init__(self) -> None:
        self._db = DBLeaseRegistryV2()

    def try_acquire_user(
        self,
        user_id: str,
        *,
        owner_id: str,
        now: float | None = None,
        ttl_sec: float,
    ) -> LeaseV2 | None:
        return self._db.try_acquire(
            user_id,
            f"turn:{user_id}",
            owner_id=owner_id,
            now=now,
            ttl_sec=ttl_sec,
        )

    def release(self, lease: LeaseV2) -> bool:
        return self._db.release(lease)

    def current(self, scope: str, *, now: float | None = None) -> LeaseV2 | None:
        user_id = scope.split(":", 1)[1] if scope.startswith("turn:") else ""
        if not user_id:
            return None
        return self._db.current(user_id, scope, now=now)


class DBBackgroundLeaseRegistryV2(BackgroundLeaseRegistryV2):
    def __init__(self) -> None:
        self._db = DBLeaseRegistryV2()

    def try_acquire_job(
        self,
        job_id: str,
        *,
        user_id: str = "",
        owner_id: str,
        now: float | None = None,
        ttl_sec: float,
    ) -> LeaseV2 | None:
        if not user_id:
            return None
        return self._db.try_acquire(
            user_id,
            f"background:{job_id}",
            owner_id=owner_id,
            now=now,
            ttl_sec=ttl_sec,
        )

    def release(self, lease: LeaseV2) -> bool:
        return self._db.release(lease)

    def current_for_job(self, user_id: str, job_id: str, *, now: float | None = None) -> LeaseV2 | None:
        return self._db.current(user_id, f"background:{job_id}", now=now)


class DBProactiveSettingsStoreV2:
    """Persistent V2 settings shape.

    Existing `proactive_settings` is read only as a migration fallback. New V2
    settings are stored under `proactive_settings_v2` so the three-switch shape
    does not inherit old enabled/dnd/user_state semantics.
    """

    def load(self, user_id: str) -> ProactiveSettingsV2:
        try:
            doc = db.get_blob(user_id, SETTINGS_KIND_V2)
            if isinstance(doc, dict):
                return resolve_settings_v2(doc)
            legacy = db.get_blob(user_id, LEGACY_PROACTIVE_SETTINGS_KIND)
            if isinstance(legacy, dict):
                return resolve_settings_v2(legacy)
        except Exception as e:
            import logging

            logging.getLogger("proactive.store_v2").error(
                "load_settings_v2(%s) failed: %s", user_id, e
            )
        return resolve_settings_v2(None)

    def save(self, user_id: str, patch: Mapping[str, Any]) -> ProactiveSettingsV2:
        settings = merge_settings_patch_v2(
            self.load(user_id),
            patch if isinstance(patch, Mapping) else {},
            updated_at=now_settings_updated_at_v2(),
        )
        db.set_blob(user_id, SETTINGS_KIND_V2, settings_v2_to_doc(settings))
        return settings


@dataclass(frozen=True)
class BackgroundJobRecordV2:
    job_id: str
    user_id: str
    status: str
    request: Mapping[str, Any]
    turn_id: str
    wake_ids: tuple[str, ...]
    origin_refs: tuple[str, ...]
    lease_id: str
    lease_expires_at: float
    doc: Mapping[str, Any]


class DBBackgroundJobStoreV2:
    def create_job(
        self,
        user_id: str,
        request: Mapping[str, Any],
        *,
        turn_id: str = "",
        wake_ids: tuple[str, ...] = (),
        origin_refs: tuple[str, ...] = (),
        now: float | None = None,
        job_id: str | None = None,
    ) -> BackgroundJobRecordV2:
        now = _now(now)
        job_id = job_id or _new_background_job_id()
        doc = {
            "kind": "background_job_v2",
            "job_id": job_id,
            "user_id": user_id,
            "status": BACKGROUND_PENDING,
            "request": dict(request or {}),
            "turn_id": str(turn_id or ""),
            "wake_ids": list(wake_ids or ()),
            "origin_refs": list(origin_refs or wake_ids or ()),
            "created_at": now,
            "updated_at": now,
        }
        db.log_append(user_id, BACKGROUND_JOB_STREAM_V2, doc, ts=now, item_key=job_id)
        return self._background_record_from_doc(doc)

    def get_job(self, user_id: str, job_id: str) -> BackgroundJobRecordV2 | None:
        for record in self.list_jobs(user_id):
            if record.job_id == job_id:
                return record
        return None

    def mark_running(
        self,
        user_id: str,
        job_id: str,
        lease: LeaseV2,
        *,
        now: float | None = None,
    ) -> BackgroundJobRecordV2 | None:
        now = _now(now)
        patch = {
            "status": BACKGROUND_RUNNING,
            "updated_at": now,
            "lease_id": lease.lease_id,
            "lease_owner_id": lease.owner_id,
            "lease_expires_at": lease.expires_at,
        }
        doc = _patch_log_item_guarded(
            user_id,
            BACKGROUND_JOB_STREAM_V2,
            job_id,
            patch,
            status=BACKGROUND_PENDING,
        )
        if doc is None:
            doc = _patch_log_item_guarded(
                user_id,
                BACKGROUND_JOB_STREAM_V2,
                job_id,
                patch,
                status=BACKGROUND_RUNNING,
                lease_expired_at_or_before=now,
            )
        return self._background_record_from_doc(doc) if doc else None

    def complete_job(
        self,
        user_id: str,
        job_id: str,
        lease: LeaseV2,
        result: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> BackgroundJobRecordV2 | None:
        now = _now(now)
        doc = _patch_log_item_guarded(
            user_id,
            BACKGROUND_JOB_STREAM_V2,
            job_id,
            {
                "status": BACKGROUND_COMPLETED,
                "completed_at": now,
                "updated_at": now,
                "result": dict(result or {}),
            },
            status=BACKGROUND_RUNNING,
            lease_id=lease.lease_id,
        )
        return self._background_record_from_doc(doc) if doc else None

    def fail_job(
        self,
        user_id: str,
        job_id: str,
        lease: LeaseV2,
        error: Mapping[str, Any],
        *,
        now: float | None = None,
    ) -> BackgroundJobRecordV2 | None:
        now = _now(now)
        doc = _patch_log_item_guarded(
            user_id,
            BACKGROUND_JOB_STREAM_V2,
            job_id,
            {
                "status": BACKGROUND_FAILED,
                "failed_at": now,
                "updated_at": now,
                "error": dict(error or {}),
            },
            status=BACKGROUND_RUNNING,
            lease_id=lease.lease_id,
        )
        return self._background_record_from_doc(doc) if doc else None

    def list_jobs(self, user_id: str) -> list[BackgroundJobRecordV2]:
        return [self._background_record_from_doc(doc) for doc in db.log_read_all(user_id, BACKGROUND_JOB_STREAM_V2)]

    @staticmethod
    def _background_record_from_doc(doc: Mapping[str, Any]) -> BackgroundJobRecordV2:
        return BackgroundJobRecordV2(
            job_id=str(doc.get("job_id") or ""),
            user_id=str(doc.get("user_id") or ""),
            status=str(doc.get("status") or ""),
            request=dict(doc.get("request") or {}),
            turn_id=str(doc.get("turn_id") or ""),
            wake_ids=tuple(str(item) for item in (doc.get("wake_ids") or ())),
            origin_refs=tuple(str(item) for item in (doc.get("origin_refs") or ())),
            lease_id=str(doc.get("lease_id") or ""),
            lease_expires_at=float(doc.get("lease_expires_at") or 0.0),
            doc=dict(doc),
        )


@dataclass(frozen=True)
class TurnRecordV2:
    turn_id: str
    status: str
    lease_id: str
    wake_ids: tuple[str, ...]
    doc: Mapping[str, Any]


@dataclass(frozen=True)
class TurnActionRecordV2:
    action_id: str
    turn_id: str
    action_type: str
    doc: Mapping[str, Any]


class DBTurnStoreV2:
    def start_turn(
        self,
        user_id: str,
        context: MergedWakeContextV2,
        lease: LeaseV2,
        *,
        now: float | None = None,
        turn_id: str | None = None,
    ) -> TurnRecordV2 | None:
        now = _now(now)
        current_lease = DBLeaseRegistryV2().current(user_id, lease.scope, now=now)
        if current_lease is None or current_lease.lease_id != lease.lease_id:
            return None
        self.recover_stale_running(user_id, now=now)
        if any(record.status == TURN_RUNNING for record in self.list_turns(user_id)):
            return None
        turn_id = turn_id or _new_turn_id()
        doc = {
            "kind": "turn_v2",
            "turn_id": turn_id,
            "user_id": user_id,
            "status": TURN_RUNNING,
            "lease_id": lease.lease_id,
            "lease_owner_id": lease.owner_id,
            "lease_expires_at": lease.expires_at,
            "created_at": now,
            "updated_at": now,
            "trigger": context.trigger,
            "merged_triggers": list(context.merged_triggers),
            "wake_ids": list(context.wake_ids),
            "latency_sensitive": bool(context.latency_sensitive),
            "manual": bool(context.manual),
            "delivery_committed": False,
        }
        db.log_append(user_id, TURN_STREAM_V2, doc, ts=now, item_key=turn_id)
        return self._record_from_doc(doc)

    def complete_turn(
        self,
        user_id: str,
        turn_id: str,
        lease: LeaseV2,
        *,
        outcome: TurnOutcomeV2 | None = None,
        delivery_committed: bool = False,
        now: float | None = None,
    ) -> TurnRecordV2 | None:
        now = _now(now)
        patch: dict[str, Any] = {
            "status": TURN_COMPLETED,
            "completed_at": now,
            "updated_at": now,
            "delivery_committed": bool(delivery_committed),
        }
        if outcome is not None:
            patch["outcome"] = {
                "messages": list(outcome.messages),
                "actions": [dict(item) for item in outcome.actions],
                "needs_background": bool(outcome.needs_background),
                "background_request": dict(outcome.background_request or {}),
            }
        doc = _patch_log_item_guarded(
            user_id,
            TURN_STREAM_V2,
            turn_id,
            patch,
            status=TURN_RUNNING,
            lease_id=lease.lease_id,
        )
        return self._record_from_doc(doc) if doc else None

    def record_actions(
        self,
        user_id: str,
        turn_id: str,
        actions: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
        *,
        now: float | None = None,
    ) -> list[TurnActionRecordV2]:
        now = _now(now)
        records: list[TurnActionRecordV2] = []
        if not turn_id:
            return records
        for idx, action in enumerate(actions):
            if not isinstance(action, Mapping):
                continue
            action_type = str(action.get("type") or "")
            if not action_type:
                continue
            action_id = str(action.get("action_id") or f"act_{uuid.uuid4().hex[:16]}")
            doc = {
                "kind": "turn_action_v2",
                "turn_id": turn_id,
                "user_id": user_id,
                "action_id": action_id,
                "action_index": idx,
                "action_type": action_type,
                "status": "recorded",
                "created_at": now,
                "action": dict(action),
            }
            db.log_append(
                user_id,
                TURN_ACTION_STREAM_V2,
                doc,
                ts=now,
                item_key=f"{turn_id}:{idx}:{action_id}",
            )
            records.append(self._action_record_from_doc(doc))
        return records

    def recover_stale_running(
        self,
        user_id: str,
        *,
        now: float | None = None,
        reason: str = "lease_expired",
    ) -> list[TurnRecordV2]:
        now = _now(now)
        recovered: list[TurnRecordV2] = []
        for doc in db.log_read_all(user_id, TURN_STREAM_V2):
            if str(doc.get("status") or "") != TURN_RUNNING:
                continue
            turn_id = str(doc.get("turn_id") or "")
            if not turn_id:
                continue
            patched = _patch_log_item_guarded(
                user_id,
                TURN_STREAM_V2,
                turn_id,
                {
                    "status": TURN_STALE_RECOVERED,
                    "recovered_at": now,
                    "updated_at": now,
                    "recovery_reason": reason,
                },
                status=TURN_RUNNING,
                lease_expired_at_or_before=now,
            )
            if patched is not None:
                recovered.append(self._record_from_doc(patched))
        return recovered

    def list_turns(self, user_id: str) -> list[TurnRecordV2]:
        return [self._record_from_doc(doc) for doc in db.log_read_all(user_id, TURN_STREAM_V2)]

    def list_actions(self, user_id: str, *, turn_id: str = "") -> list[TurnActionRecordV2]:
        records = [self._action_record_from_doc(doc) for doc in db.log_read_all(user_id, TURN_ACTION_STREAM_V2)]
        if turn_id:
            records = [record for record in records if record.turn_id == turn_id]
        return records

    @staticmethod
    def _record_from_doc(doc: Mapping[str, Any]) -> TurnRecordV2:
        return TurnRecordV2(
            turn_id=str(doc.get("turn_id") or ""),
            status=str(doc.get("status") or ""),
            lease_id=str(doc.get("lease_id") or ""),
            wake_ids=tuple(str(item) for item in (doc.get("wake_ids") or ())),
            doc=dict(doc),
        )

    @staticmethod
    def _action_record_from_doc(doc: Mapping[str, Any]) -> TurnActionRecordV2:
        return TurnActionRecordV2(
            action_id=str(doc.get("action_id") or ""),
            turn_id=str(doc.get("turn_id") or ""),
            action_type=str(doc.get("action_type") or ""),
            doc=dict(doc),
        )
