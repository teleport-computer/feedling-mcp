"""Claim-bound primary artifacts, with mirror callbacks only after commit.

The guard is per artifact: earlier committed artifacts survive a later failure.
This deliberately does not promise all-or-nothing application or free replay.
"""
from core import envelope
from genesis import managed_eligibility as eligibility, managed_execution, service
import genesis_managed_store as persistence
import distillation_ledger


class ArtifactTransaction:
    def __init__(self, store, conn, job):
        self.store, self.conn, self.job = store, conn, job
        self.expected = {}
        self.written = {}
        self.after_identity = None

    def read(self, kind):
        if kind not in self.expected:
            self.expected[kind] = persistence.read_blob(self.conn, self.store.user_id, kind)
        return self.expected[kind]

    def read_identity(self):
        value = self.read("identity")
        if persistence.digest(value) != persistence.marker(self.job)["identity_witness"]:
            raise persistence.DependencyUnavailable("identity", "dependency_changed")
        if value is not None:
            self.identity_plain(value)
        return value

    def identity_plain(self, value):
        return eligibility.object_body(value, self.store.user_id, "identity")

    def relationship_anchor(self, output, days):
        parse = service.identity_service._parse_iso_calendar_date
        explicit = parse(service._text(output.get("relationship_started_at"), 80))
        if explicit:
            return explicit.isoformat()
        rows = self.conn.execute(
            "SELECT doc FROM memory_moments WHERE user_id=%s", (self.store.user_id,),
        ).fetchall()
        dates = [parse(doc.get("occurred_at", "")) for (doc,) in rows if isinstance(doc, dict)]
        dates = [value for value in dates if value is not None]
        return min(dates).isoformat() if dates else service.identity_service._anchor_from_days(days)

    def build_envelope(self, store, body, **kwargs):
        eligibility.require_policy(store.user_id)
        return envelope._build_shared_envelope_for_store(store, body, local_only=True, **kwargs)

    def write(self, user_id, kind, document):
        if user_id != self.store.user_id:
            raise persistence.ClaimRejected("managed_artifact_owner")
        persistence.cas_blob(self.conn, user_id, kind, self.read(kind), document)
        self.written[kind] = document

    def after_commit(self):
        for kind, document in self.written.items():
            persistence.mirror_blob(self.store.user_id, kind, document)
        if self.after_identity:
            service.boot_gates._log_bootstrap_event(
                self.store, "genesis_identity_written_v1", success=True)
            service.identity_service._append_identity_change(self.store, {
                "action": "replace" if self.after_identity == "updated" else "init",
                "reason": "Identity updated from Genesis import.",
            })


def write_artifact(store, job_id, witness, output, kind):
    phase, _ = persistence.execution_state(store.user_id, job_id, witness)
    if phase != "applying":
        raise persistence.ClaimRejected("managed_claim_state_changed")
    with distillation_ledger.ArtifactAttempt(store, job_id, kind) as attempt:
        with managed_execution.mutation_guard(store, job_id, witness) as (conn, job):
            tx = ArtifactTransaction(store, conn, job)
            if kind == "identity":
                result = service.init_identity_if_absent(store, output, managed=tx)
                outcome = result
            elif kind == "persona":
                result = service._write_persona_artifact(store, job_id, output, managed=tx)
                outcome = result[2]
            elif kind == "voice":
                result = service._write_voice_artifact(store, job_id, output, managed=tx)
                outcome = result[2]
            elif kind == "profile":
                result = write_profile(tx, output)
                outcome = result[2]
            else:
                raise ValueError("unsupported_managed_artifact")
        # Never mirror a transaction that was rolled back by policy/CAS failure.
        attempt.finish(outcome)
        tx.after_commit()
    return result


def write_profile(tx, output):
    """Preserve opaque untouched sides using the existing profile builder."""
    profile = service._profile_output_from_reducer(output)
    if not profile:
        return "", "", "not_provided"
    if not profile["memory_touched"] and not profile["style_touched"]:
        return "", "", "skipped"
    kind = service.v2_profile_store.PROFILE_BLOB_KIND
    def seal(uid, text):
        if uid != tx.store.user_id:
            raise persistence.ClaimRejected("managed_artifact_owner")
        doc, error = tx.build_envelope(tx.store, text.encode("utf-8"))
        if doc is None:
            raise persistence.DependencyUnavailable("profile", "dependency_invalid")
        return doc

    # Match the ordinary profile writer's two-attempt recomputation. The claim
    # row stays locked on tx.conn throughout; no provider call or job replay is
    # involved. A CAS miss alone permits refreshing the profile dependency.
    for attempt in (1, 2):
        prior = tx.read(kind)
        row = tx.conn.execute(
            "SELECT count(*),COALESCE(max(doc->>'updated_at'),'') "
            "FROM memory_moments WHERE user_id=%s", (tx.store.user_id,),
        ).fetchone()
        now = service._now_iso()
        doc = service.v2_profile_store.build_profile_document_patching_fields(
            tx.store.user_id, state="ok",
            source={"card_count": int(row[0]), "max_updated_at": str(row[1]), "generated_at": now},
            last_attempt={"at": now, "reject_code": "", "attempts": profile["provider_calls"]},
            memory_text=profile["memory"] if profile["memory_touched"] else None,
            style_text=profile["style"] if profile["style_touched"] else None,
            previous=prior, seal_text=seal,
        )
        try:
            tx.write(tx.store.user_id, kind, doc)
            break
        except persistence.DependencyUnavailable as exc:
            if attempt == 2 or exc.dependency != kind or exc.reason != "dependency_changed":
                raise
            # Discard the candidate as well as the stale expected document.
            # Rebuilding above preserves the concurrent winner's untouched side.
            tx.expected[kind] = persistence.read_blob(tx.conn, tx.store.user_id, kind)
    digest = service._stable_json_sha256({
        "memory": profile["memory"] if profile["memory_touched"] else "",
        "style": profile["style"] if profile["style_touched"] else "",
        "memory_touched":profile["memory_touched"],"style_touched":profile["style_touched"],
    })
    return service.GENESIS_PROFILE_REF,digest,"written"
