"""Audited preservation for terminal ciphertext during TEE promotion.

This module never decrypts content.  It identifies the small, explicit set of
source/target row projections whose original ciphertext may be retained when a
terminal replication marker proves the plaintext projection cannot be built.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from typing import Sequence

from psycopg import OperationalError
from psycopg.types.json import Jsonb


PRESERVED_PREFIX = "preserved_ciphertext:v1:"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _Contract:
    source_fetch_sql: str
    destination_fetch_sql: str
    audit_fetch_sql: str
    insert_sql: str
    delete_sql: str
    json_indices: tuple[int, ...]
    by_user_only: bool = False
    # Frame audits deliberately compare compact, database-local transport
    # fingerprints.  The persisted SHA-256 marker remains the plan authority.
    audit_uses_marker_digest: bool = False

    def args(self, user_id: str, item_id: str) -> tuple[str, ...]:
        return (user_id,) if self.by_user_only else (user_id, item_id)

    def insert_args(self, row: tuple) -> tuple:
        values = list(row)
        for index in self.json_indices:
            values[index] = Jsonb(values[index]) if values[index] is not None else None
        return tuple(values)


CONTRACTS: dict[str, _Contract] = {
    "chat_messages": _Contract(
        "SELECT user_id,msg_id,ts,doc,seq,storage_generation "
        "FROM chat_messages WHERE user_id=%s AND msg_id=%s",
        "SELECT user_id,msg_id,ts,doc,seq,storage_generation "
        "FROM chat_messages WHERE user_id=%s AND msg_id=%s",
        "SELECT user_id,msg_id,ts,doc,seq,storage_generation "
        "FROM chat_messages WHERE (user_id,msg_id) IN "
        "(SELECT * FROM unnest(%s::text[],%s::text[]))",
        "INSERT INTO chat_messages "
        "(user_id,msg_id,ts,doc,seq,storage_generation) "
        "OVERRIDING SYSTEM VALUE VALUES (%s,%s,%s,%s,%s,%s)",
        "DELETE FROM chat_messages WHERE user_id=%s AND msg_id=%s",
        (3,),
    ),
    "memory_moments": _Contract(
        "SELECT user_id,moment_id,occurred_at,doc FROM memory_moments "
        "WHERE user_id=%s AND moment_id=%s",
        "SELECT user_id,moment_id,occurred_at,doc FROM memory_moments "
        "WHERE user_id=%s AND moment_id=%s",
        "SELECT user_id,moment_id,occurred_at,doc FROM memory_moments "
        "WHERE (user_id,moment_id) IN "
        "(SELECT * FROM unnest(%s::text[],%s::text[]))",
        "INSERT INTO memory_moments (user_id,moment_id,occurred_at,doc) "
        "VALUES (%s,%s,%s,%s)",
        "DELETE FROM memory_moments WHERE user_id=%s AND moment_id=%s",
        (3,),
    ),
    "identity": _Contract(
        "SELECT user_id,kind,doc FROM user_blobs "
        "WHERE user_id=%s AND kind='identity'",
        "SELECT user_id,kind,doc FROM user_blobs "
        "WHERE user_id=%s AND kind='identity'",
        "SELECT user_id,kind,doc FROM user_blobs "
        "WHERE kind='identity' AND user_id = ANY(%s::text[])",
        "INSERT INTO user_blobs (user_id,kind,doc) VALUES (%s,%s,%s)",
        "DELETE FROM user_blobs WHERE user_id=%s AND kind='identity'",
        (2,),
        by_user_only=True,
    ),
    "frame_envelopes": _Contract(
        "SELECT user_id,frame_id,ts,doc,env_meta,body_key "
        "FROM frame_envelopes WHERE user_id=%s AND frame_id=%s",
        "SELECT user_id,frame_id,ts,doc,env_meta,body_key "
        "FROM frame_envelopes WHERE user_id=%s AND frame_id=%s",
        "SELECT user_id,frame_id,ts,md5(doc::text),octet_length(doc::text),"
        "md5(env_meta::text),octet_length(env_meta::text),body_key "
        "FROM frame_envelopes WHERE (user_id,frame_id) IN "
        "(SELECT * FROM unnest(%s::text[],%s::text[]))",
        "INSERT INTO frame_envelopes "
        "(user_id,frame_id,ts,doc,env_meta,body_key) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        "DELETE FROM frame_envelopes WHERE user_id=%s AND frame_id=%s",
        (3, 4),
        audit_uses_marker_digest=True,
    ),
}


@dataclass(frozen=True)
class PlannedRow:
    table: str
    user_id: str
    item_id: str
    original_reason: str
    current_reason: str
    source_row: tuple
    row_sha256: str
    destination_state: str


@dataclass(frozen=True)
class PreservationPlan:
    rows: tuple[PlannedRow, ...]
    sha256: str
    counts: dict[str, int]
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class PreservedPending:
    user_id: str
    table: str
    item_id: str
    reason: str


@dataclass(frozen=True)
class PreservationAudit:
    preserved: int
    counts: dict[str, int]
    sha256: str
    mismatches: tuple[str, ...]
    valid_keys: frozenset[tuple[str, str, str]]


class PreservationRefused(RuntimeError):
    """The live state no longer matches the operator-approved plan."""


def is_terminal_reason(reason: str) -> bool:
    """Whether ``reason`` is eligible for one-time ciphertext preservation."""
    value = str(reason or "")
    return (
        value.startswith("decrypt_failed:")
        or value.startswith("pdm:")
        or value == "visibility_local_only"
    )


def encode_preserved_reason(row_sha256: str, original_reason: str) -> str:
    """Build a versioned, reversible audit marker for one preserved row."""
    if not _DIGEST_RE.fullmatch(row_sha256) or not is_terminal_reason(original_reason):
        raise ValueError("invalid_preserved_marker_input")
    encoded = base64.urlsafe_b64encode(original_reason.encode()).decode().rstrip("=")
    return f"{PRESERVED_PREFIX}{row_sha256}:{encoded}"


def parse_preserved_reason(reason: str) -> tuple[str, str] | None:
    """Return ``(row digest, original reason)`` for a valid v1 marker."""
    value = str(reason or "")
    if not value.startswith(PRESERVED_PREFIX):
        return None
    digest, separator, encoded = value[len(PRESERVED_PREFIX):].partition(":")
    if not separator or not _DIGEST_RE.fullmatch(digest) or not encoded:
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            encoded + padding, altchars=b"-_", validate=True
        )
        original_reason = raw.decode("utf-8", "strict")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    if not is_terminal_reason(original_reason):
        return None
    return digest, original_reason


def canonical_row_sha256(table: str, row: tuple) -> str:
    """Hash one complete source/target projection without exposing its bytes."""
    payload = json.dumps(
        [table, *row],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _reason_class(reason: str) -> str:
    if reason.startswith("decrypt_failed:"):
        return "decrypt_failed"
    if reason.startswith("pdm:"):
        return "pdm"
    return reason


def _finish_plan(
    rows: list[PlannedRow],
    counts: Counter[str],
    blocker_counts: Counter[tuple[str, str]],
) -> PreservationPlan:
    digest_payload = [
        [row.table, row.row_sha256, _reason_class(row.original_reason)]
        for row in rows
    ]
    plan_sha256 = hashlib.sha256(
        json.dumps(digest_payload, separators=(",", ":")).encode()
    ).hexdigest()
    blockers = tuple(
        f"{code}:{table}:{count}"
        for (code, table), count in sorted(blocker_counts.items())
    )
    return PreservationPlan(
        rows=tuple(rows),
        sha256=plan_sha256,
        counts=dict(sorted(counts.items())),
        blockers=blockers,
    )


def build_plan(source, destination) -> PreservationPlan:
    """Build a stable, read-only plan for all currently terminal markers."""
    pending = destination.execute(
        "SELECT user_id,table_name,item_id,reason "
        "FROM tee_pending_device_migration "
        "ORDER BY table_name,user_id,item_id"
    ).fetchall()
    rows: list[PlannedRow] = []
    counts: Counter[str] = Counter()
    blocker_counts: Counter[tuple[str, str]] = Counter()

    for raw_user_id, raw_table, raw_item_id, raw_reason in pending:
        user_id = str(raw_user_id)
        table = str(raw_table)
        item_id = str(raw_item_id)
        reason = str(raw_reason or "")
        parsed_marker = parse_preserved_reason(reason)
        if is_terminal_reason(reason):
            original_reason = reason
            marker_digest = None
        elif parsed_marker is not None:
            marker_digest, original_reason = parsed_marker
        else:
            continue
        counts[table] += 1
        contract = CONTRACTS.get(table)
        if contract is None:
            blocker_counts[("unknown_table", table)] += 1
            continue
        parent = destination.execute(
            "SELECT 1 FROM users WHERE user_id=%s", (user_id,)
        ).fetchone()
        if parent is None:
            blocker_counts[("missing_parent", table)] += 1
            continue
        args = contract.args(user_id, item_id)
        source_row = source.execute(contract.source_fetch_sql, args).fetchone()
        if source_row is None:
            blocker_counts[("missing_source", table)] += 1
            continue
        source_tuple = tuple(source_row)
        row_sha256 = canonical_row_sha256(table, source_tuple)
        if marker_digest is not None and marker_digest != row_sha256:
            blocker_counts[("marker_digest_mismatch", table)] += 1
            continue
        destination_row = destination.execute(
            contract.destination_fetch_sql, args
        ).fetchone()
        if destination_row is None:
            if marker_digest is not None:
                blocker_counts[("missing_preserved_destination", table)] += 1
                continue
            destination_state = "absent"
        elif tuple(destination_row) == source_tuple:
            if marker_digest is None:
                blocker_counts[("unowned_exact_destination", table)] += 1
                continue
            destination_state = "exact"
        else:
            blocker_counts[("destination_conflict", table)] += 1
            continue
        rows.append(
            PlannedRow(
                table=table,
                user_id=user_id,
                item_id=item_id,
                original_reason=original_reason,
                current_reason=reason,
                source_row=source_tuple,
                row_sha256=row_sha256,
                destination_state=destination_state,
            )
        )

    return _finish_plan(rows, counts, blocker_counts)


def build_revert_plan(source, destination) -> PreservationPlan:
    """Plan an exact pre-cutover revert of rows owned by preservation markers."""
    pending = destination.execute(
        "SELECT user_id,table_name,item_id,reason "
        "FROM tee_pending_device_migration "
        "WHERE reason LIKE 'preserved_ciphertext:%' "
        "ORDER BY table_name,user_id,item_id"
    ).fetchall()
    rows: list[PlannedRow] = []
    counts: Counter[str] = Counter()
    blocker_counts: Counter[tuple[str, str]] = Counter()

    prepared = destination.execute(
        "SELECT 1 FROM server_config WHERE key='phase4_primary_prepared'"
    ).fetchone()
    if prepared is not None:
        blocker_counts[("phase4_already_prepared", "all")] += 1

    for raw_user_id, raw_table, raw_item_id, raw_reason in pending:
        user_id = str(raw_user_id)
        table = str(raw_table)
        item_id = str(raw_item_id)
        reason = str(raw_reason or "")
        counts[table] += 1
        parsed_marker = parse_preserved_reason(reason)
        if parsed_marker is None:
            blocker_counts[("malformed_preserved_marker", table)] += 1
            continue
        marker_digest, original_reason = parsed_marker
        contract = CONTRACTS.get(table)
        if contract is None:
            blocker_counts[("unknown_table", table)] += 1
            continue
        args = contract.args(user_id, item_id)
        source_row = source.execute(contract.source_fetch_sql, args).fetchone()
        if source_row is None:
            blocker_counts[("missing_source", table)] += 1
            continue
        source_tuple = tuple(source_row)
        row_sha256 = canonical_row_sha256(table, source_tuple)
        if marker_digest != row_sha256:
            blocker_counts[("marker_digest_mismatch", table)] += 1
            continue
        destination_row = destination.execute(
            contract.destination_fetch_sql, args
        ).fetchone()
        if destination_row is None:
            blocker_counts[("missing_preserved_destination", table)] += 1
            continue
        if tuple(destination_row) != source_tuple:
            blocker_counts[("destination_conflict", table)] += 1
            continue
        rows.append(
            PlannedRow(
                table=table,
                user_id=user_id,
                item_id=item_id,
                original_reason=original_reason,
                current_reason=reason,
                source_row=source_tuple,
                row_sha256=row_sha256,
                destination_state="exact",
            )
        )

    return _finish_plan(rows, counts, blocker_counts)


def _fetch_keyed_rows(
    connection,
    query: str,
    keys: Sequence[tuple[str, str]],
    *,
    by_user_only: bool = False,
) -> dict[tuple[str, str], tuple]:
    """Fetch one table's keyed projections in a single gateway round trip."""
    fetched: dict[tuple[str, str], tuple] = {}
    if not keys:
        return fetched
    user_ids = [user_id for user_id, _item_id in keys]
    args = (
        (user_ids,)
        if by_user_only
        else (user_ids, [item_id for _user_id, item_id in keys])
    )
    rows = [tuple(row) for row in connection.execute(query, args).fetchall()]
    if by_user_only:
        # Historical identity markers have used both the user id and the blob
        # kind as item_id.  The row itself is keyed only by user_id, so retain
        # each exact requested marker key instead of inventing one from kind.
        by_user = {str(row[0]): row for row in rows}
        for user_id, item_id in keys:
            if user_id in by_user:
                fetched[(user_id, item_id)] = by_user[user_id]
        return fetched
    for row_tuple in rows:
        fetched[(str(row_tuple[0]), str(row_tuple[1]))] = row_tuple
    return fetched


def audit_preserved(
    source,
    destination,
    markers: Sequence[PreservedPending],
) -> PreservationAudit:
    """Audit every preserved marker against its exact raw source and target."""
    valid_rows: list[PlannedRow] = []
    counts: Counter[str] = Counter()
    mismatch_counts: Counter[tuple[str, str]] = Counter()
    valid_keys: set[tuple[str, str, str]] = set()
    candidates: list[tuple[PreservedPending, str, str, _Contract]] = []
    keys_by_table: dict[str, list[tuple[str, str]]] = {}

    for marker in sorted(
        markers, key=lambda row: (row.table, row.user_id, row.item_id)
    ):
        parsed = parse_preserved_reason(marker.reason)
        if parsed is None:
            mismatch_counts[("malformed_preserved_marker", marker.table)] += 1
            continue
        marker_digest, original_reason = parsed
        contract = CONTRACTS.get(marker.table)
        if contract is None:
            mismatch_counts[("unknown_table", marker.table)] += 1
            continue
        candidates.append((marker, marker_digest, original_reason, contract))
        keys_by_table.setdefault(marker.table, []).append(
            (marker.user_id, marker.item_id)
        )

    source_rows: dict[str, dict[tuple[str, str], tuple]] = {}
    destination_rows: dict[str, dict[tuple[str, str], tuple]] = {}
    for table, keys in sorted(keys_by_table.items()):
        contract = CONTRACTS[table]
        source_rows[table] = _fetch_keyed_rows(
            source,
            contract.audit_fetch_sql,
            keys,
            by_user_only=contract.by_user_only,
        )
        destination_rows[table] = _fetch_keyed_rows(
            destination,
            contract.audit_fetch_sql,
            keys,
            by_user_only=contract.by_user_only,
        )

    frame_keys = keys_by_table.get("frame_envelopes", [])
    duplicate_frames = _fetch_keyed_rows(
        destination,
        "SELECT user_id,frame_id FROM frames WHERE (user_id,frame_id) IN "
        "(SELECT * FROM unnest(%s::text[],%s::text[]))",
        frame_keys,
    )

    for marker, marker_digest, original_reason, contract in candidates:
        key = (marker.user_id, marker.item_id)
        source_row = source_rows[marker.table].get(key)
        if source_row is None:
            mismatch_counts[("missing_source", marker.table)] += 1
            continue
        if contract.audit_uses_marker_digest:
            row_sha256 = marker_digest
        else:
            row_sha256 = canonical_row_sha256(marker.table, source_row)
            if marker_digest != row_sha256:
                mismatch_counts[("marker_digest_mismatch", marker.table)] += 1
                continue
        destination_row = destination_rows[marker.table].get(key)
        if destination_row is None:
            mismatch_counts[("missing_preserved_destination", marker.table)] += 1
            continue
        if destination_row != source_row:
            mismatch_counts[("destination_conflict", marker.table)] += 1
            continue
        if marker.table == "frame_envelopes" and key in duplicate_frames:
            mismatch_counts[("duplicate_frame_representation", marker.table)] += 1
            continue
        valid_rows.append(
            PlannedRow(
                table=marker.table,
                user_id=marker.user_id,
                item_id=marker.item_id,
                original_reason=original_reason,
                current_reason=marker.reason,
                source_row=source_row,
                row_sha256=row_sha256,
                destination_state="exact",
            )
        )
        counts[marker.table] += 1
        valid_keys.add((marker.table, marker.user_id, marker.item_id))

    plan = _finish_plan(valid_rows, counts, mismatch_counts)
    return PreservationAudit(
        preserved=len(valid_rows),
        counts=plan.counts,
        sha256=plan.sha256,
        mismatches=plan.blockers,
        valid_keys=frozenset(valid_keys),
    )


def _assert_approved(
    plan: PreservationPlan, *, expected_count: int, expected_plan_sha256: str
) -> None:
    if plan.blockers:
        raise PreservationRefused("preservation_plan_blocked")
    if expected_count != len(plan.rows):
        raise PreservationRefused("preservation_count_changed")
    if expected_plan_sha256 != plan.sha256:
        raise PreservationRefused("preservation_plan_changed")


def apply_plan(
    source,
    destination,
    plan: PreservationPlan,
    *,
    expected_count: int,
    expected_plan_sha256: str,
) -> dict:
    """Copy an exactly approved plan and atomically replace terminal markers."""
    _assert_approved(
        plan,
        expected_count=expected_count,
        expected_plan_sha256=expected_plan_sha256,
    )
    result = None
    try:
        with source.transaction():
            source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            live_plan = build_plan(source, destination)
            _assert_approved(
                live_plan,
                expected_count=expected_count,
                expected_plan_sha256=expected_plan_sha256,
            )
            inserted = exact = marked = 0
            with destination.transaction():
                for row in live_plan.rows:
                    contract = CONTRACTS[row.table]
                    if row.destination_state == "absent":
                        destination.execute(
                            contract.insert_sql, contract.insert_args(row.source_row)
                        )
                        inserted += 1
                    else:
                        exact += 1
                    marker = encode_preserved_reason(
                        row.row_sha256, row.original_reason
                    )
                    if row.current_reason == marker:
                        marked += 1
                    else:
                        changed = destination.execute(
                            "UPDATE tee_pending_device_migration SET reason=%s,marked_at=now() "
                            "WHERE user_id=%s AND table_name=%s AND item_id=%s AND reason=%s",
                            (
                                marker,
                                row.user_id,
                                row.table,
                                row.item_id,
                                row.current_reason,
                            ),
                        ).rowcount
                        if changed != 1:
                            raise PreservationRefused("preservation_marker_changed")
                        marked += changed
            result = {
                "ok": True,
                "preserved": marked,
                "inserted": inserted,
                "already_exact": exact,
                "counts": live_plan.counts,
                "plan_sha256": live_plan.sha256,
            }
    except OperationalError:
        # The source transaction is read-only.  Once the destination context
        # has exited and ``result`` exists, TEE has committed atomically; a
        # later EOF while committing/cleaning up the source snapshot cannot
        # invalidate that destination commit and must not report false failure.
        if result is None:
            raise
    assert result is not None
    return result


def revert_plan(
    source,
    destination,
    plan: PreservationPlan,
    *,
    expected_count: int,
    expected_plan_sha256: str,
) -> dict:
    """Atomically remove owned preserved rows and restore terminal markers."""
    _assert_approved(
        plan,
        expected_count=expected_count,
        expected_plan_sha256=expected_plan_sha256,
    )
    with source.transaction():
        source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        with destination.transaction():
            destination.execute("LOCK TABLE server_config IN SHARE MODE")
            live_plan = build_revert_plan(source, destination)
            _assert_approved(
                live_plan,
                expected_count=expected_count,
                expected_plan_sha256=expected_plan_sha256,
            )
            reverted = 0
            for row in live_plan.rows:
                contract = CONTRACTS[row.table]
                args = contract.args(row.user_id, row.item_id)
                marker_row = destination.execute(
                    "SELECT reason FROM tee_pending_device_migration "
                    "WHERE user_id=%s AND table_name=%s AND item_id=%s FOR UPDATE",
                    (row.user_id, row.table, row.item_id),
                ).fetchone()
                if marker_row is None or str(marker_row[0]) != row.current_reason:
                    raise PreservationRefused("preservation_marker_changed")
                destination_row = destination.execute(
                    f"{contract.destination_fetch_sql} FOR UPDATE", args
                ).fetchone()
                if (
                    destination_row is None
                    or tuple(destination_row) != row.source_row
                    or canonical_row_sha256(row.table, tuple(destination_row))
                    != row.row_sha256
                ):
                    raise PreservationRefused("preserved_destination_changed")
                if destination.execute(contract.delete_sql, args).rowcount != 1:
                    raise PreservationRefused("preserved_destination_changed")
                changed = destination.execute(
                    "UPDATE tee_pending_device_migration SET reason=%s,marked_at=now() "
                    "WHERE user_id=%s AND table_name=%s AND item_id=%s AND reason=%s",
                    (
                        row.original_reason,
                        row.user_id,
                        row.table,
                        row.item_id,
                        row.current_reason,
                    ),
                ).rowcount
                if changed != 1:
                    raise PreservationRefused("preservation_marker_changed")
                reverted += 1
    return {
        "ok": True,
        "reverted": reverted,
        "counts": live_plan.counts,
        "plan_sha256": live_plan.sha256,
    }
