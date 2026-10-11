"""Connection-bound persistence for the managed plaintext Genesis lane.

This module owns no provider/network work. Prepared inputs never leave memory;
only a private claim capability hash and content-free lifecycle metadata persist.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets

from psycopg.types.json import Jsonb

import db

MARKER = "managed_worker_execution"


class DependencyUnavailable(RuntimeError):
    def __init__(self, dependency: str, reason: str = "dependency_unavailable"):
        self.dependency = dependency
        self.reason = reason
        super().__init__(f"{reason}:{dependency}")


class ClaimRejected(RuntimeError):
    """No authority to mutate this claim; never route to generic mark_failed."""


def digest(value) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def marker(job: dict) -> dict | None:
    metadata = job.get("metadata")
    value = metadata.get(MARKER) if isinstance(metadata, dict) else None
    return value if isinstance(value, dict) else None


def read_job(conn, user_id: str, job_id: str, *, lock=False):
    cur = conn.execute(
        "SELECT * FROM genesis_import_jobs WHERE user_id=%s AND job_id=%s"
        + (" FOR UPDATE SKIP LOCKED" if lock else ""), (user_id, job_id),
    )
    return db._genesis_row(cur, cur.fetchone())


def read_blob(conn, user_id: str, kind: str):
    row = conn.execute("SELECT doc FROM user_blobs WHERE user_id=%s AND kind=%s",
                       (user_id, kind)).fetchone()
    return row[0] if row else None


def input_snapshot(conn, job: dict) -> dict:
    uid, jid = job["user_id"], job["job_id"]
    cur = conn.execute(
        "SELECT * FROM genesis_import_chunks WHERE user_id=%s AND job_id=%s ORDER BY seq",
        (uid, jid),
    )
    columns = [col.name for col in cur.description]
    chunks = [dict(zip(columns, row)) for row in cur.fetchall()]
    for chunk in chunks:
        chunk["encrypted_body"] = bytes(chunk["encrypted_body"])
    route = conn.execute(
        f"SELECT {db._ROUTE_COLUMNS}, c.api_key_envelope FROM model_api_routes r "
        "JOIN model_api_credentials c ON c.id=r.credential_id "
        "WHERE r.user_id=%s AND r.is_active", (uid,),
    ).fetchone()
    config = db._route_row_to_dict(route) if route else None
    if config is not None:
        config["api_key_envelope"] = route[-1]
    user = conn.execute("SELECT doc FROM users WHERE user_id=%s", (uid,)).fetchone()
    return {
        "job": job, "chunks": chunks, "route": config,
        "user": user[0] if user else None,
        "blobs": {kind: read_blob(conn, uid, kind) for kind in (
            "genesis_persona", "genesis_voice", "identity",
        )},
        "moments": [r[0] for r in conn.execute(
            "SELECT doc FROM memory_moments WHERE user_id=%s ORDER BY occurred_at,moment_id",
            (uid,),
        ).fetchall()],
    }


def scan_page(cursor=None, highwater=None, *, limit=16):
    """A finite sweep, including deferred rows; arrivals wait for the next sweep."""
    with db.get_pool().connection() as conn:
        if highwater is None:
            highwater = conn.execute(
                "SELECT created_at,user_id,job_id FROM genesis_import_jobs "
                "WHERE status='uploaded' ORDER BY created_at DESC,user_id DESC,job_id DESC LIMIT 1",
            ).fetchone()
        if highwater is None:
            return [], None
        args = list(highwater)
        after = ""
        if cursor is not None:
            after = " AND (created_at,user_id,job_id)>(%s,%s,%s)"
            args.extend(cursor)
        args.append(max(1, min(int(limit), 64)))
        rows = conn.execute(
            "SELECT created_at,user_id,job_id FROM genesis_import_jobs WHERE status='uploaded' "
            "AND (created_at,user_id,job_id)<=(%s,%s,%s)" + after
            + " ORDER BY created_at,user_id,job_id LIMIT %s", args,
        ).fetchall()
    return rows, highwater


def _mirror_job(job):
    # Exact primary result; never repeat a picker query on the shadow.
    from tee_shadow import mirror
    fields = ("status", "metadata", "output", "error", "worker_claimed_by",
              "worker_claimed_at", "updated_at", "completed_at", "failed_phase", "memory_action_count",
              "identity_status", "persona_ref", "persona_sha256")
    params = [Jsonb(job[f]) if f in ("metadata", "output") else job.get(f) for f in fields]
    mirror.execute(
        "UPDATE genesis_import_jobs SET " + ",".join(f"{f}=%s" for f in fields)
        + " WHERE user_id=%s AND job_id=%s", (*params, job["user_id"], job["job_id"]),
    )


def _record_unavailable(conn, job, exc, *, now=None):
    now = now or datetime.now(timezone.utc)
    output = dict(job.get("output") or {})
    prior = output.get("managed_unavailable") or {}
    if prior.get("reason") == exc.reason and prior.get("dependency") == exc.dependency:
        try:
            if (now - datetime.fromisoformat(prior["checked_at"])).total_seconds() < 60:
                return None
        except (KeyError, TypeError, ValueError):
            pass
    output["managed_unavailable"] = {
        "reason": exc.reason, "dependency": exc.dependency, "checked_at": now.isoformat(),
    }
    cur = conn.execute(
        "UPDATE genesis_import_jobs SET output=%s WHERE user_id=%s AND job_id=%s "
        "AND status='uploaded' RETURNING *",
        (Jsonb(output), job["user_id"], job["job_id"]),
    )
    return db._genesis_row(cur, cur.fetchone())


def prepare_claim(user_id, job_id, worker_id, authenticated_envelope, prepare):
    """Validate and admit one coherent snapshot. No HTTP under row locks."""
    changed = None
    result = None
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            job = read_job(conn, user_id, job_id, lock=True)
            if not job or job["status"] != "uploaded":
                return None
            raw = input_snapshot(conn, job)
            try:
                prepared = prepare(raw, authenticated_envelope)
            except DependencyUnavailable as exc:
                changed = _record_unavailable(conn, job, exc)
            else:
                capability = secrets.token_urlsafe(32)
                claim = {
                    "mode": "local_plaintext", "generation": secrets.token_hex(16),
                    "worker_id": worker_id, "phase": "prepared",
                    "capability_hash": hashlib.sha256(capability.encode()).hexdigest(),
                    "identity_witness": digest(raw["blobs"]["identity"]),
                }
                metadata = {**(job.get("metadata") or {}), MARKER: claim}
                output = {k: v for k, v in (job.get("output") or {}).items()
                          if k != "managed_unavailable"}
                output["stage"] = "worker_claimed"
                cur = conn.execute(
                    "UPDATE genesis_import_jobs SET status='processing',error='',metadata=%s,"
                    "output=%s,worker_claimed_by=%s,worker_claimed_at=now(),updated_at=now() "
                    "WHERE user_id=%s AND job_id=%s AND status='uploaded' RETURNING *",
                    (Jsonb(metadata), Jsonb(output), worker_id, user_id, job_id),
                )
                changed = db._genesis_row(cur, cur.fetchone())
                result = (changed, prepared, {"generation": claim["generation"],
                                             "capability": capability})
    if changed:
        _mirror_job(changed)
    return result


def verify(job, witness, *, phases=()):
    claim = marker(job or {})
    if not claim or not isinstance(witness, dict):
        raise ClaimRejected("managed_claim_required")
    capability = witness.get("capability")
    if not isinstance(capability, str) or not capability or len(capability) > 128:
        raise ClaimRejected("managed_claim_invalid")
    if (claim.get("mode") != "local_plaintext"
            or not claim.get("generation") or not claim.get("worker_id")
            or witness.get("generation") != claim.get("generation")
            or claim.get("worker_id") != job.get("worker_claimed_by")
            or not hmac.compare_digest(hashlib.sha256(capability.encode()).hexdigest(),
                                       str(claim.get("capability_hash") or ""))):
        raise ClaimRejected("managed_claim_invalid")
    if phases and (job.get("status") != "processing" or claim.get("phase") not in phases):
        raise ClaimRejected("managed_claim_state_changed")
    return claim


@contextmanager
def guard(user_id, job_id, witness, *, phases, conn=None):
    """Primary mutation must use the yielded connection, never a second pool slot."""
    if conn is None:
        with db.get_pool().connection() as owned:
            with owned.transaction():
                db._lock_chat_user_fence_on_cursor(owned.cursor(), user_id)
                with guard(user_id, job_id, witness, phases=phases, conn=owned) as pair:
                    yield pair
        return
    cur = conn.execute(
        "SELECT * FROM genesis_import_jobs WHERE user_id=%s AND job_id=%s FOR UPDATE",
        (user_id, job_id),
    )
    job = db._genesis_row(cur, cur.fetchone())
    verify(job, witness, phases=phases)
    if not conn.execute("SELECT 1 FROM users WHERE user_id=%s", (user_id,)).fetchone():
        raise DependencyUnavailable("account_policy")
    yield conn, job


def set_phase(user_id, job_id, witness, before, after):
    transitions = {"execution_started": ("prepared",), "applying": ("execution_started",)}
    if transitions.get(after) != tuple(before):
        raise ClaimRejected("managed_transition_invalid")
    with guard(user_id, job_id, witness, phases=before) as (conn, job):
        changed = _set_phase_on_connection(conn, job, after)
    _mirror_job(changed)
    return changed


def _set_phase_on_connection(conn, job, phase):
    metadata = {**job["metadata"], MARKER: {**marker(job), "phase": phase}}
    cur = conn.execute(
        "UPDATE genesis_import_jobs SET metadata=%s,updated_at=now() "
        "WHERE user_id=%s AND job_id=%s RETURNING *",
        (Jsonb(metadata), job["user_id"], job["job_id"]),
    )
    return db._genesis_row(cur, cur.fetchone())


def defer_prepared(user_id, job_id, witness, exc):
    """Only a current pre-execution claim can be returned to uploaded."""
    with guard(user_id, job_id, witness, phases=("prepared",)) as (conn, job):
        metadata = {**job["metadata"], MARKER: {**marker(job), "phase": "deferred"}}
        output = {**job["output"], "managed_unavailable": {
            "reason": exc.reason, "dependency": exc.dependency,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }}
        cur = conn.execute(
            "UPDATE genesis_import_jobs SET status='uploaded',metadata=%s,output=%s,"
            "worker_claimed_by='',worker_claimed_at=NULL WHERE user_id=%s AND job_id=%s RETURNING *",
            (Jsonb(metadata), Jsonb(output), user_id, job_id),
        )
        changed = db._genesis_row(cur, cur.fetchone())
    _mirror_job(changed)
    return changed


def execution_state(user_id, job_id, witness):
    """Authenticate readback without granting any permission to replay artifacts."""
    with db.get_pool().connection() as conn:
        job = read_job(conn, user_id, job_id)
        claim = verify(job, witness)
        if job["status"] == "done" and claim.get("phase") == "done":
            return "done", job.get("output") or {}
        if job["status"] == "failed":
            return "failed", {"failed_phase": job.get("failed_phase") or ""}
        if job["status"] == "processing" and claim.get("phase") in (
            "prepared", "execution_started", "applying",
        ):
            return claim["phase"], None
        raise ClaimRejected("managed_claim_state_changed")


def fail(user_id, job_id, witness, reason, *, server=False):
    phases = ("execution_started", "applying") if server else ("execution_started",)
    with guard(user_id, job_id, witness, phases=phases) as (conn, job):
        cur = conn.execute(
            "UPDATE genesis_import_jobs SET status='failed',error=%s,failed_phase=%s,updated_at=now() "
            "WHERE user_id=%s AND job_id=%s RETURNING *",
            (str(reason)[:240], marker(job)["phase"], user_id, job_id),
        )
        changed = db._genesis_row(cur, cur.fetchone())
    _mirror_job(changed)
    return changed


def cas_blob(conn, user_id, kind, expected, document):
    """Strict primary CAS on the caller's claim transaction; DB errors propagate."""
    if expected is None:
        row = conn.execute(
            "INSERT INTO user_blobs(user_id,kind,doc) VALUES(%s,%s,%s) "
            "ON CONFLICT (user_id,kind) DO NOTHING RETURNING kind",
            (user_id, kind, Jsonb(document)),
        ).fetchone()
    else:
        row = conn.execute(
            "UPDATE user_blobs SET doc=%s WHERE user_id=%s AND kind=%s "
            "AND doc=%s RETURNING kind",
            (Jsonb(document), user_id, kind, Jsonb(expected)),
        ).fetchone()
    if row is None:
        raise DependencyUnavailable(kind, "dependency_changed")


def mirror_blob(user_id, kind, document):
    """Postcommit only; preserve existing singleton mirror/requeue policy."""
    if kind == "identity":
        from tee_shadow import mirror
        mirror.mark_pending(user_id, "identity", "identity", "requeue")
    else:
        db._mirror_persisted_blob(user_id, kind, document)


def complete_on_connection(conn, job, output, reducer):
    """Completion and sanitized receipts commit with the same claim lock."""
    uid, jid = job['user_id'], job['job_id']
    receipts = []
    for typ, doc, status, ref in [('reducer',reducer,'applied','sanitized'),('apply',output,'done','inline')]:
        receipt = conn.execute(
            'INSERT INTO genesis_import_outputs(user_id,job_id,output_type,doc,status,ref) '
            'VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,job_id,output_type) '
            'DO UPDATE SET doc=EXCLUDED.doc,status=EXCLUDED.status,ref=EXCLUDED.ref,updated_at=now() RETURNING *',
            (uid,jid,typ,Jsonb(doc),status,ref))
        receipts.append(db._genesis_row(receipt,receipt.fetchone()))
    metadata={**job['metadata'],MARKER:{**marker(job),'phase':'done'}}
    cur=conn.execute(
        "UPDATE genesis_import_jobs SET status='done',output=%s,metadata=%s,"
        "memory_action_count=%s,identity_status=%s,persona_ref=%s,persona_sha256=%s,"
        "completed_at=COALESCE(completed_at,now()),updated_at=now(),error='' "
        "WHERE user_id=%s AND job_id=%s RETURNING *",
        (Jsonb(output),Jsonb(metadata),output['memory_action_count'],output['identity_status'],
         output['persona_ref'],output['persona_sha256'],uid,jid))
    return db._genesis_row(cur,cur.fetchone()), receipts


def recover(*, live_workers=None, older_than=1800, limit=50):
    """Only pre-execution claims can be requeued; started work never replays."""
    changed=[]
    with db.get_pool().connection() as conn:
        with conn.transaction():
            if live_workers is None:
                clause='updated_at < now()-make_interval(secs => %s)'
                args=[max(60,int(older_than))]
            else:
                clause="worker_claimed_by <> ALL(%s) AND worker_claimed_at < now()-make_interval(secs => %s)"
                args=[list(live_workers),max(60,int(older_than))]
            cur=conn.execute(
                "SELECT * FROM genesis_import_jobs WHERE status='processing' "
                "AND COALESCE(metadata,'{}'::jsonb) ? 'managed_worker_execution' AND "+clause+
                " ORDER BY updated_at LIMIT %s FOR UPDATE SKIP LOCKED",(*args,max(1,min(limit,200))))
            jobs=[db._genesis_row(cur,row) for row in cur.fetchall()]
            for job in jobs:
                claim=marker(job) or {}
                prepared=claim.get('mode')=='local_plaintext' and claim.get('phase')=='prepared'
                phase=str(claim.get('phase') or 'unknown')
                metadata={**job['metadata'],MARKER:{**claim,'phase':'deferred' if prepared else phase}}
                cur=conn.execute(
                    "UPDATE genesis_import_jobs SET status=%s,metadata=%s,error=%s,failed_phase=%s,"
                    "worker_claimed_by=%s,worker_claimed_at=%s,updated_at=CASE WHEN %s THEN updated_at ELSE now() END "
                    "WHERE user_id=%s AND job_id=%s RETURNING *",
                    ('uploaded' if prepared else 'failed',Jsonb(metadata),
                     '' if prepared else 'managed_interrupted_possible_effects',
                     job.get('failed_phase') if prepared else phase,
                     '' if prepared else job['worker_claimed_by'],
                     None if prepared else job['worker_claimed_at'],prepared,job['user_id'],job['job_id']))
                changed.append(db._genesis_row(cur,cur.fetchone()))
    for job in changed:
        _mirror_job(job)
    return changed


def mirror_outputs(receipts):
    """Mirror committed receipt values, including their authoritative timestamp."""
    from tee_shadow import mirror
    for row in receipts:
        mirror.execute(
            "INSERT INTO genesis_import_outputs(user_id,job_id,output_type,doc,status,ref,updated_at) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id,job_id,output_type) "
            "DO UPDATE SET doc=EXCLUDED.doc,status=EXCLUDED.status,ref=EXCLUDED.ref,updated_at=EXCLUDED.updated_at",
            (row['user_id'],row['job_id'],row['output_type'],Jsonb(row['doc']),
             row['status'],row['ref'],row['updated_at']))


def find_managed_backfill(user_id, source_kind, material_hash):
    """A terminal managed attempt still blocks automatic same-material replay.

    Do not use the UI's bounded recent-job list for this durable lookup.
    """
    with db.get_pool().connection() as conn:
        cur = conn.execute(
            "SELECT * FROM genesis_import_jobs WHERE user_id=%s AND source_kind=%s "
            "AND metadata->>'material_sha256'=%s AND metadata ? %s "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, source_kind, material_hash, MARKER))
        return db._genesis_row(cur, cur.fetchone())
