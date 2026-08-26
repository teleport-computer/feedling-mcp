"""Content-free per-artifact attempt accounting for distillation writes.

The producer owns each artifact's outcome vocabulary.  This module only adds
the cross-producer terminal classification needed for rates; it never derives
an attempt from a job summary and never stores artifact content or exceptions.
"""

from __future__ import annotations

import logging
import uuid

import db


log = logging.getLogger("feedling.distillation_ledger")

ARTIFACT_OUTCOMES = {
    "memory": frozenset({"written", "partial", "not_provided", "write_failed"}),
    "identity": frozenset({
        "initialized", "updated", "written", "already_initialized",
        "not_provided", "unchanged", "locked", "identity_update_empty",
        "identity_not_initialized", "identity_plain_unavailable",
        "identity_write_conflict", "write_failed",
    }),
    "persona": frozenset({"written", "preserved", "not_provided", "write_failed"}),
    "voice": frozenset({"written", "not_provided", "write_failed"}),
    "profile": frozenset({
        "written", "superseded", "skipped", "not_provided", "write_failed",
    }),
    "greeting": frozenset({"written", "not_provided", "write_failed"}),
}

_SUCCEEDED = frozenset({"written", "partial", "initialized", "updated"})
_FAILED = frozenset({
    "write_failed", "identity_update_empty", "identity_not_initialized",
    "identity_plain_unavailable", "identity_write_conflict",
})


def terminal_result_for(outcome: str) -> str:
    if outcome in _SUCCEEDED:
        return "succeeded"
    if outcome in _FAILED:
        return "failed"
    return "no_write"


def _genesis_dimensions(user_id: str, job_id: str) -> tuple[str, str] | None:
    """Return (distill_kind, access_path) from producer-owned job metadata."""
    try:
        job = db.genesis_get_job(user_id, job_id)
    except Exception as exc:  # accounting must not break the artifact write
        log.warning("[distillation-ledger] job lookup failed: %s", type(exc).__name__)
        return None
    if not isinstance(job, dict) or not job:
        log.warning("[distillation-ledger] job lookup returned no row")
        return None
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    output = job.get("output") if isinstance(job.get("output"), dict) else {}
    mode = str(metadata.get("mode") or job.get("source_kind") or "onboarding")
    distill_kind = (
        "redistill"
        if mode in {"add_memory", "update_identity", "resident_redistill"}
        else "onboarding"
    )
    if str(metadata.get("ingest") or "") == "plaintext":
        stage = str(output.get("stage") or "")
        access_path = "apikey_v2" if stage.startswith("genesis_v2") else "apikey_v1"
    else:
        access_path = "self_hosted"
    return distill_kind, access_path


class ArtifactAttempt:
    """One durable attempt boundary. Observability failures are best-effort."""

    def __init__(
        self, store, job_id: str, artifact: str, *, flow: str = "genesis",
        distill_kind: str = "", access_path: str = "",
    ) -> None:
        if artifact not in ARTIFACT_OUTCOMES:
            raise ValueError(f"unknown distillation artifact: {artifact}")
        dimensions_known = True
        if flow == "genesis" and (not distill_kind or not access_path):
            inferred = _genesis_dimensions(store.user_id, job_id)
            if inferred is None:
                dimensions_known = False
            else:
                inferred_kind, inferred_path = inferred
                distill_kind = distill_kind or inferred_kind
                access_path = access_path or inferred_path
        self.attempt_id = f"distill_attempt_{uuid.uuid4().hex}"
        self.store = store
        self.job_id = str(job_id)
        self.artifact = artifact
        self.flow = flow
        self.dimensions_known = dimensions_known
        self.distill_kind = distill_kind or "onboarding"
        self.access_path = access_path or "apikey_v1"
        self.started = False
        self.finished = False

    def __enter__(self) -> "ArtifactAttempt":
        if not self.dimensions_known:
            return self
        try:
            self.started = bool(db.distillation_start_artifact_attempt(
                attempt_id=self.attempt_id,
                user_id=self.store.user_id,
                job_id=self.job_id,
                flow=self.flow,
                distill_kind=self.distill_kind,
                artifact=self.artifact,
                access_path=self.access_path,
            ))
        except Exception as exc:  # noqa: BLE001
            log.warning("[distillation-ledger] attempt start failed: %s", type(exc).__name__)
        return self

    def finish(self, outcome: str) -> None:
        if outcome not in ARTIFACT_OUTCOMES[self.artifact]:
            raise ValueError(
                f"invalid {self.artifact} producer outcome: {outcome}"
            )
        if self.finished:
            raise RuntimeError("distillation artifact attempt already finished")
        self.finished = True
        if not self.started:
            return
        try:
            db.distillation_finish_artifact_attempt(
                self.attempt_id,
                outcome=outcome,
                terminal_result=terminal_result_for(outcome),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("[distillation-ledger] attempt finish failed: %s", type(exc).__name__)

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None and not self.finished:
            self.finish("write_failed")
        elif exc_type is None and not self.finished:
            # A producer forgot to classify a successful return.  Keep the row
            # open rather than inventing an outcome; the stale started row is a
            # visible instrumentation defect and never enters success rates.
            log.error(
                "[distillation-ledger] successful %s operation omitted outcome",
                self.artifact,
            )
        return False


def history_attempt(store, job_id: str, artifact: str) -> ArtifactAttempt:
    return ArtifactAttempt(
        store,
        job_id,
        artifact,
        flow="history_import",
        distill_kind="onboarding",
        access_path="apikey_v1",
    )
