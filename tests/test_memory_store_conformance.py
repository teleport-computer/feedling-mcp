"""io's real memory write/read path against memgarden's shared write-path scenarios.

Seven 2026-09-14 §4.5 / §7.2: MemGarden's reference Store passing idempotency,
CAS and deletion scenarios says nothing about io's own executor. This module
implements ``memgarden.conformance.Host`` over io's production code and runs
every scenario against a real Postgres database:

    write    backend/memory/memory_core.actions -> memory/actions.py executor
             (the same /v1/memory/actions path io_cli and the V2 memory_write
             capability use); patch payloads come from io_cli's own builder
    archive  hosted.turn._archive_model_api_memory_cards (io's archive executor)
    capture  model_api_runtime.v2.jobs_store prepare/commit_capture_batch
    read     memory_core.index / fetch (backend readside) -> in-process enclave
             routes (/v1/memory/index, /v1/memory/fetch); automatic recall via
             enclave.routes.chat._build_context_memories over the
             /v1/memory/list page; history = /v1/memory/list include_archived
    truth    db.memory_load_strict (raw rows) for ``inspect``

Crypto is the only double: envelopes are "sealed" as base64 JSON and both
decrypt boundaries (backend -> enclave decrypt, enclave decrypt_envelope) are
replaced by an owner-checking decoder. No envelope/AAD/K_enclave logic changes.

Every difference from the shared semantics is declared below as a
``Deviation`` (by design) or a known bug with evidence; an undeclared failure
or a declaration that stops failing turns this module red.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import conftest  # noqa: E402
import db  # noqa: E402
from asgi_test_client import _AsgiTestClient  # noqa: E402
from core import enclave as core_enclave  # noqa: E402
from core import envelope as core_envelope  # noqa: E402
from core import store as core_store  # noqa: E402
from enclave import auth as enclave_auth  # noqa: E402
from enclave import backend_client, keys, readside, routes, state as enclave_state  # noqa: E402
from enclave import envelope as enclave_envelope  # noqa: E402
from enclave.routes import chat as enclave_chat  # noqa: E402
from hosted import turn as hosted_turn  # noqa: E402
from memgarden import conformance as kit  # noqa: E402
from memgarden.conformance import Deviation, Outcome  # noqa: E402
from memory import memory_core  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402
from proactive import capture_daily  # noqa: E402
import io_cli  # noqa: E402


# --------------------------------------------------------------------------- #
# crypto double + in-process enclave
# --------------------------------------------------------------------------- #

def _seal(inner: dict, *, owner: str, item_id: str) -> dict:
    return {
        "id": item_id,
        "owner_user_id": owner,
        "visibility": "shared",
        "body_ct": base64.b64encode(json.dumps(inner, ensure_ascii=False).encode()).decode(),
        "nonce": "conf-nonce",
        "K_user": "conf-k-user",
        "K_enclave": "conf-k-enclave",
        "enclave_pk_fpr": "conf-fpr",
    }


def _unseal(envelope: dict, caller: str) -> bytes:
    if str(envelope.get("owner_user_id") or "") != str(caller):
        raise enclave_envelope.DecryptFailure("owner mismatch")
    if not envelope.get("K_enclave") or not envelope.get("body_ct"):
        raise enclave_envelope.DecryptFailure("not sealed to enclave")
    return base64.b64decode(envelope["body_ct"])


@pytest.fixture
def io_world(monkeypatch):
    """Patch only the crypto boundary and the enclave's key/whoami plumbing."""

    def build_shared(store, plaintext, *, item_id=None, content_kind="text"):
        return _seal(json.loads(plaintext), owner=store.user_id,
                     item_id=item_id or f"mom_{uuid.uuid4().hex[:12]}"), ""

    def backend_decrypt(envelope, api_key, *, purpose, caller_user_id, runtime_token=""):
        try:
            return _unseal(envelope, caller_user_id)
        except enclave_envelope.DecryptFailure as exc:
            raise RuntimeError(f"decrypt_failed:{exc.reason}") from exc

    monkeypatch.setattr(core_envelope, "_build_shared_envelope_for_store", build_shared)
    monkeypatch.setattr(core_enclave, "_decrypt_envelope_via_enclave", backend_decrypt)
    monkeypatch.setattr(enclave_envelope, "decrypt_envelope",
                        lambda env, uid, sk: _unseal(env, uid))

    async def whoami(path, headers, params=None):
        return {"user_id": str(headers.get("X-API-Key") or "").removeprefix("conf-key-")}

    async def content_sk():
        return object()

    monkeypatch.setitem(enclave_state._state, "ready", True)
    monkeypatch.setitem(enclave_state._state, "error", None)
    monkeypatch.setattr(backend_client, "backend_get", whoami)
    monkeypatch.setattr(keys, "get_content_sk", content_sk)
    enclave_auth.reset_cache()
    client = _AsgiTestClient(routes.build_app())

    failing = {"armed": False}
    real_daily = capture_daily.daily_capture_patch

    def daily_patch(*args, **kwargs):
        if failing["armed"]:
            failing["armed"] = False
            raise RuntimeError("injected storage failure after memory inserts")
        return real_daily(*args, **kwargs)

    monkeypatch.setattr(capture_daily, "daily_capture_patch", daily_patch)

    # Pin a non-UTC process timezone: CI and the CVMs run UTC, which would hide a
    # writer that stamps naive local time (updated_at did, until it moved to the
    # same UTC "Z" stamp as created_at).
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"
    time.tzset()
    try:
        yield {"enclave": client, "fail_capture": failing}
    finally:
        if previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()


# --------------------------------------------------------------------------- #
# io host adapter
# --------------------------------------------------------------------------- #

_NOT_FOUND = {"not_found", "not_owned", "memory_id_required"}
_CONFLICT = {"supersede_targets_unavailable", "supersede_targets_changed", "memory_id_conflict"}


class IoHost:
    name = "io (Postgres, actions executor, V2 capture commit)"
    sources = ("history_import", "chat")

    def __init__(self, world: dict) -> None:
        self._world = world
        self._run = uuid.uuid4().hex[:10]
        self._users: dict[str, str] = {}
        self._capture_jobs: dict[str, int] = {}
        self._capture_windows: dict[tuple[str, str], int] = {}

    # -- identity -------------------------------------------------------- #

    def _uid(self, owner: str) -> str:
        if owner not in self._users:
            uid = f"u_conf_{owner}_{self._run}"
            conftest.seed_user(uid)
            conftest.set_v2_runtime_owner(uid, generation=1)
            self._users[owner] = uid
        return self._users[owner]

    def _store(self, owner: str):
        return core_store.get_store(self._uid(owner))

    # -- receipts -------------------------------------------------------- #

    @staticmethod
    def _outcome(body: dict, status: int) -> Outcome:
        results = body.get("results") if isinstance(body.get("results"), list) else []
        item = results[0] if results else body
        if status < 400 and int(item.get("http_status") or status) < 400:
            memory = item.get("memory") if isinstance(item.get("memory"), dict) else {}
            rid = str(memory.get("id") or "")
            return Outcome(ok=True, record_ids=(rid,) if rid else ())
        code = str(item.get("error") or body.get("error") or "")
        http = int(item.get("http_status") or status)
        if code in _NOT_FOUND or http == 404:
            kind = "not_found"
        elif code in _CONFLICT:
            kind = "conflict"
        elif http == 400:
            kind = "invalid"
        elif http >= 500 or code == "db_write_failed":
            kind = "storage_failed"
        else:
            kind = code or f"http_{http}"
        return Outcome(ok=False, error=kind, detail=f"{http}:{code}")

    def _act(self, owner: str, action: dict) -> Outcome:
        body, status = memory_core.actions(self._store(owner), None, {"actions": [action]})
        return self._outcome(body, status)

    @staticmethod
    def _memory(card: dict) -> dict:
        return {
            "type": "fact",
            "summary": card.get("summary", ""),
            "content": card.get("content", ""),
            "bucket": card.get("bucket", ""),
            "threads": list(card.get("threads") or []),
            "occurred_at": card.get("occurred_at", ""),
            "source": card.get("source", ""),
        }

    # -- writes ---------------------------------------------------------- #

    def add(self, owner, card, *, request_id="", record_id=""):
        if not (request_id or record_id):
            return self._act(owner, {"type": "memory.add", "memory": self._memory(card)})
        # io's only caller-held identity on this path is the client-sealed
        # envelope id (the id is bound into the AEAD AAD). Public docs:
        # "a stable unique client-generated ID is easier to reconcile".
        uid = self._uid(owner)
        item_id = record_id or f"mom_req_{request_id}"
        inner = {k: card.get(k) for k in ("summary", "content", "bucket", "threads")}
        envelope = {**_seal(inner, owner=uid, item_id=item_id), "type": "fact",
                    "occurred_at": card.get("occurred_at"), "source": card.get("source")}
        out = self._act(owner, {"type": "memory.add", "envelope": envelope})
        if request_id and out.detail == "409:memory_id_conflict":
            # The envelope id is the request identity here, so a taken id with a
            # different card is exactly the kit's idempotency conflict.
            return Outcome(ok=False, error="idempotency_conflict", detail=out.detail)
        return out

    def patch(self, owner, record_id, changes, *, based_on=None):
        # Exactly what `io_cli memory-patch` sends (the V2 memory_write 'update'
        # schema carries the same fields: no occurred_at, no source).
        current = self.inspect(owner, record_id) or {}
        payload = io_cli._memory_patch_payload(
            memory_id=record_id,
            summary=changes.get("summary") or current.get("summary") or "",
            content=changes.get("content") or current.get("content") or "",
            bucket=None, threads=None, importance=None, pulse=None,
            mem_type="fact", source=None, reason="conformance patch")
        return self._act(owner, payload["actions"][0])

    def supersede(self, owner, target_ids, card, *, based_on=None):
        return self._act(owner, {"type": "memory.supersede", "supersedes": list(target_ids),
                                 "memory": self._memory(card)})

    def archive(self, owner, record_id, *, reason=""):
        archived = hosted_turn._archive_model_api_memory_cards(
            self._store(owner), [record_id], reason=reason or "conformance",
            job_id=f"conf-{self._run}")
        if archived:
            return Outcome(ok=True, record_ids=(record_id,))
        return Outcome(ok=False, error="not_found", detail="archived=0")

    def delete(self, owner, record_id, *, requested_by):
        return self._act(owner, {"type": "memory.delete", "id": record_id,
                                 "reason": f"requested_by:{requested_by}"})

    def observe(self, owner):
        return None  # io has no revision token; conflicts come from the target fence

    def tick(self):
        time.sleep(1.05)  # io stamps real wall-clock times at second precision

    # -- capture (V2 batch protocol) ------------------------------------- #

    def _capture_job(self, uid: str) -> int:
        job_id = self._capture_jobs.get(uid)
        if job_id is not None:
            with db.get_pool().connection() as conn:
                row = conn.execute("SELECT status FROM agent_jobs WHERE id=%s",
                                   (job_id,)).fetchone()
            if row and row[0] in {"claimed", "running"}:
                return job_id
        job_id, _coalesced = jobs_store.enqueue_job(uid, "capture")
        # Claim this exact row: claim_next_job is global and could take another
        # test's pending job in the shared session database.
        with db.get_pool().connection() as conn:
            conn.execute(
                "UPDATE agent_jobs SET status='running', claimed_by='conf-worker', "
                "claimed_at=now(), started_at=now(), "
                "lease_expires_at=now() + interval '10 minutes' WHERE id=%s",
                (job_id,))
        self._capture_jobs[uid] = job_id
        return job_id

    def capture_progress(self, owner):
        uid = self._uid(owner)
        doc = db.get_blob(uid, jobs_store._CAPTURE_STATE_KIND) or {}
        return int(doc.get("last_captured_until_seq") or 0)

    def commit_capture(self, owner, cards, *, request_id, fail_storage=False):
        uid = self._uid(owner)
        after = self._capture_windows.setdefault((uid, request_id), self.capture_progress(owner))
        window = {"after_seq": after, "through_seq": after + 1,
                  "after_message_id": "" if after == 0 else f"m{after}",
                  "until_message_id": f"m{after + 1}", "until_ts": float(after + 1)}
        actions = []
        for index, card in enumerate(cards):
            inner = {k: card.get(k) for k in ("summary", "content", "bucket", "threads")}
            envelope = {**_seal(inner, owner=uid, item_id=f"mom_cap_{request_id}_{index}"),
                        "type": "fact", "occurred_at": card.get("occurred_at"),
                        "source": "memory_capture", "importance": 0.5, "pulse": 0.3}
            actions.append({"type": "memory.add", "envelope": envelope})
        job_id = self._capture_job(uid)
        prepared = jobs_store.prepare_capture_batch(
            job_id=job_id, user_id=uid, claimed_by="conf-worker", window=window,
            actions=actions)
        if not isinstance(prepared, dict) or prepared.get("id") is None:
            return Outcome(ok=False, error="conflict", detail=str((prepared or {}).get("reason")))
        self._world["fail_capture"]["armed"] = bool(fail_storage)
        try:
            committed = jobs_store.commit_capture_batch(
                job_id=job_id, user_id=uid, claimed_by="conf-worker", batch_id=prepared["id"])
        except Exception as exc:  # noqa: BLE001 — the worker fails the job on this
            return Outcome(ok=False, error="storage_failed", detail=type(exc).__name__)
        finally:
            self._world["fail_capture"]["armed"] = False
        if committed.get("committed"):
            ids = tuple(sorted(i for i in committed.get("affected_memory_ids") or []))
            return Outcome(ok=True, record_ids=ids, reason="" if ids else "nothing_to_keep")
        reason = str(committed.get("reason") or "")
        kind = "conflict" if reason in {"frontier_changed", "batch_unavailable"} else reason
        return Outcome(ok=False, error=kind, detail=reason)

    # -- reads ------------------------------------------------------------- #

    def _post_enclave(self, owner: str):
        uid = self._uid(owner)

        def post(api_key, candidates, *, operation, payload=None):
            response = self._world["enclave"].post(
                f"/v1/memory/{operation}", headers={"X-API-Key": f"conf-key-{uid}"},
                json={**dict(payload or {}), "moments": candidates})
            if response.status_code >= 400:
                raise RuntimeError(f"enclave_http_{response.status_code}:{response.get_data(as_text=True)[:180]}")
            return response.get_json()
        return post

    @staticmethod
    def _view_from_row(row: dict) -> dict:
        inner = json.loads(base64.b64decode(row["body_ct"])) if row.get("body_ct") else {}
        status = str(row.get("status") or "active").lower()
        if status == "superseded" or str(row.get("superseded_by") or ""):
            status = "superseded"
        elif (row.get("is_archived") is True or str(row.get("archived_at") or "")
              or str(row.get("archive_reason") or "")):
            status = "archived"
        return {
            "id": str(row.get("id") or ""),
            "summary": inner.get("summary"), "content": inner.get("content"),
            "bucket": inner.get("bucket"), "threads": list(inner.get("threads") or []),
            "source": row.get("source"), "occurred_at": row.get("occurred_at"),
            "created_at": row.get("created_at"), "updated_at": row.get("updated_at"),
            "status": status, "superseded_by": str(row.get("superseded_by") or ""),
        }

    def inspect(self, owner, record_id):
        for row in db.memory_load_strict(self._uid(owner)):
            if str(row.get("id") or "") == record_id:
                return self._view_from_row(row)
        return None

    def fetch(self, owner, ids, *, include_history=False):
        body, status = memory_core.fetch(
            self._store(owner), None,
            {"ids": list(ids), "include_archived": include_history,
             "include_superseded": include_history},
            post_enclave=self._post_enclave(owner))
        assert status == 200, body
        return list(body["items"])

    def _related_body(self, owner, ids):
        body, status = memory_core.fetch(self._store(owner), None, {"ids": list(ids)},
                                         post_enclave=self._post_enclave(owner))
        assert status == 200, body
        return body

    def index(self, owner):
        body, status = memory_core.index(self._store(owner), None, {"limit": 0},
                                         post_enclave=self._post_enclave(owner))
        assert status == 200, body
        return list(body["items"])

    def search(self, owner, query):
        body, status = memory_core.index(self._store(owner), None, {"query": query},
                                         post_enclave=self._post_enclave(owner))
        assert status == 200, body
        return [str(item["id"]) for item in body["items"]]

    def recall(self, owner, query):
        uid = self._uid(owner)
        listing, status = memory_core.list_moments(
            self._store(owner), limit_raw=readside.memory_readside_model_api_limit(),
            since="", include_archived_raw="")
        assert status == 200, listing
        picked, _trace, _log = enclave_chat._build_context_memories(
            listing["moments"], [{"role": "user", "content": query}],
            {"want_trace": False, "authorized_user_id": uid, "content_sk": object(),
             "context_recent": False, "context_mode": ""})
        return [str(card.get("id") or "") for card in picked]

    def related(self, owner, ids):
        return [str(item.get("id") or "") for item in self._related_body(owner, ids)["related_items"]]

    def history(self, owner):
        # The Garden list the app renders (include_archived), decrypted client-side.
        listing, status = memory_core.list_moments(
            self._store(owner), limit_raw=500, since="", include_archived_raw="1")
        assert status == 200, listing
        return [self._view_from_row(row) for row in listing["moments"]]


# --------------------------------------------------------------------------- #
# declared differences
# --------------------------------------------------------------------------- #

DEVIATIONS: dict[str, Deviation] = {
    # ---- by design --------------------------------------------------------
    "patch.in_place/same_id": Deviation(
        "by_design",
        "io has no in-place content update: memory.patch, io_cli memory-patch and the V2 "
        "memory_write 'update' op all run memory.supersede, so a correction mints a new "
        "id and retires the old card (history kept, created_at of the old card kept)."),
    "patch.preserves_provenance/source": Deviation(
        "by_design",
        "a correction records who edited: io_cli defaults source=resident_patch, the "
        "server defaults hosted_runtime_state; the original source stays on the "
        "superseded card."),
}


@pytest.fixture
def results(io_world):
    return {r.scenario: r for r in kit.run_all(lambda: IoHost(io_world), deviations=DEVIATIONS)}


def test_io_write_path_meets_shared_scenarios_or_declares_why(results):
    kit.assert_conformant(list(results.values()))
    print("\n" + kit.results_table(list(results.values()), host="io"))


# --------------------------------------------------------------------------- #
# io-specific evidence the host-agnostic kit cannot express
# --------------------------------------------------------------------------- #

def test_concurrent_supersede_of_one_target_through_real_advisory_locks(io_world):
    """Two threads, same target, real Postgres fence: one successor, one 409."""
    import threading

    host = IoHost(io_world)
    src = host.sources[0]
    target = host.add("alice", kit.card("kitthreads", source=src)).record_ids[0]
    barrier = threading.Barrier(2)
    outcomes: list[Outcome] = []

    def race(token):
        barrier.wait()
        outcomes.append(host.supersede("alice", [target], kit.card(token, source=src)))

    threads = [threading.Thread(target=race, args=(t,)) for t in ("kitwinone", "kitwintwo")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(o.ok for o in outcomes) == [False, True], outcomes
    assert [o.error for o in outcomes if not o.ok] == ["conflict"]
    winner = next(o for o in outcomes if o.ok).record_ids[0]
    assert (host.inspect("alice", target) or {})["superseded_by"] == winner
    active = [v for v in host.history("alice") if v["status"] == "active"]
    assert [v["id"] for v in active] == [winner]


def test_history_written_by_io_supersede_is_readable_through_include_superseded(io_world):
    """Regression (fixed here): io's supersede writers also stamp archive markers,
    and memory_available let those markers veto include_superseded — so
    fetch(include_superseded) and the related read's explicit link never returned
    the documented "superseded" history for any card io itself retired."""
    host = IoHost(io_world)
    src = host.sources[0]
    old = host.add("alice", kit.card("kitlineage", source=src)).record_ids[0]
    new = host.supersede("alice", [old], kit.card("kitlineagenew", source=src)).record_ids[0]
    row = next(r for r in db.memory_load_strict(host._uid("alice")) if r["id"] == old)
    assert row["is_archived"] is True and row["archive_reason"] == f"superseded_by:{new}"

    store = host._store("alice")
    body, status = memory_core.fetch(store, None, {"ids": [old], "include_superseded": True},
                                     post_enclave=host._post_enclave("alice"))
    assert status == 200 and [i["id"] for i in body["items"]] == [old], body
    assert body["items"][0]["status"] == "superseded"
    body, status = memory_core.fetch(store, None, {"ids": [old]},
                                     post_enclave=host._post_enclave("alice"))
    assert body["items"] == [] and body["unavailable_ids"] == [old]

    related = host._related_body("alice", [new])
    assert [(i["id"], i["relation"], i["status"]) for i in related["related_items"]] == [
        (old, "supersedes", "superseded")]
    # A plain archive (no supersede) still needs include_archived.
    shelved = host.add("alice", kit.card("kitshelved", source=src)).record_ids[0]
    assert host.archive("alice", shelved).ok
    body, _ = memory_core.fetch(store, None, {"ids": [shelved], "include_superseded": True},
                                post_enclave=host._post_enclave("alice"))
    assert body["items"] == [] and body["unavailable_ids"] == [shelved]


def test_v2_capture_commit_rejects_supersede_of_a_card_retired_after_prepare(io_world):
    """Regression (fixed here): a prepared batch (durable across retries) that
    supersedes a card the user corrected meanwhile used to re-point
    superseded_by and leave two active successors. The commit now re-checks the
    locked target the way memory/actions.py fences supersede_targets_changed."""
    host = IoHost(io_world)
    src = host.sources[0]
    uid = host._uid("alice")
    target = host.add("alice", kit.card("kitprepared", source=src)).record_ids[0]
    job_id = host._capture_job(uid)
    inner = {"summary": "kitcapturesucc summary line",
             "content": "kitcapturesucc body: capture's successor.",
             "bucket": "Conformance", "threads": ["kit-thread"]}
    envelope = {**_seal(inner, owner=uid, item_id="mom_cap_successor"), "type": "fact",
                "occurred_at": "2026-03-01T08:00:00Z", "source": "memory_capture"}
    prepared = jobs_store.prepare_capture_batch(
        job_id=job_id, user_id=uid, claimed_by="conf-worker",
        window={"after_seq": 0, "through_seq": 1, "after_message_id": "",
                "until_message_id": "m1", "until_ts": 1.0},
        actions=[{"type": "memory.supersede", "supersedes": [target], "envelope": envelope}])
    # Between prepare and commit the user corrects the same card.
    user_edit = host.supersede("alice", [target], kit.card("kituseredit", source=src))
    assert user_edit.ok
    committed = jobs_store.commit_capture_batch(
        job_id=job_id, user_id=uid, claimed_by="conf-worker", batch_id=prepared["id"])
    active = sorted(v["summary"] for v in host.history("alice") if v["status"] == "active")
    assert committed == {"committed": False, "reason": "capture_supersede_target_inactive",
                         "rejected": True}, (committed, active)
    assert (host.inspect("alice", target) or {})["superseded_by"] == user_edit.record_ids[0]
    assert [v["id"] for v in host.history("alice") if v["status"] == "active"] == [
        user_edit.record_ids[0]]
    assert host.inspect("alice", "mom_cap_successor") is None
    # Same rejection as the other semantic branches: the journal is dropped and the
    # window travels with the failure so the escape valve can count it.
    with db.get_pool().connection() as conn:
        assert conn.execute("SELECT count(*) FROM v2_capture_batches WHERE id=%s",
                            (prepared["id"],)).fetchone()[0] == 0
    assert host.capture_progress("alice") == 0



def _raw_row(host, owner: str, record_id: str) -> dict:
    return next(r for r in db.memory_load_strict(host._uid(owner)) if r["id"] == record_id)


def test_fetch_marks_reference_without_changing_the_card_or_waking_profile_refresh(
        io_world, monkeypatch):
    """Regression: memory_fetch stamped updated_at on every fetched card. updated_at
    is the V2 profile refresh witness, so one read made the next post-turn check
    regenerate the profile. A read stamps last_referenced_at only."""
    from model_api_runtime.v2 import profile_refresh, profile_store

    host = IoHost(io_world)
    rid = host.add("alice", kit.card("kitfetchquiet", source=host.sources[0])).record_ids[0]
    uid = host._uid("alice")
    before = _raw_row(host, "alice", rid)
    count, max_updated = db.memory_profile_source_stats(uid)
    profile = profile_store.build_profile_document(
        uid, state="ok",
        source={"card_count": count, "max_updated_at": max_updated, "generated_at": "x"},
        last_attempt={"at": "x", "reject_code": "", "attempts": 1, "retry_not_before": 0},
        memory_text="m", style_text="s",
        seal_text=lambda _uid, text: {"body_ct": "ct", "nonce": "n"})
    monkeypatch.setattr(profile_refresh.db, "get_blob_strict", lambda *_a: profile)
    assert profile_refresh.refresh_due(uid, enabled=True) is False
    host.tick()

    assert [i["id"] for i in host.fetch("alice", [rid])] == [rid]

    after = _raw_row(host, "alice", rid)
    assert after["updated_at"] == before["updated_at"]
    assert after["last_referenced_at"] != before.get("last_referenced_at")
    assert after["last_referenced_at"].endswith("Z")
    assert profile_refresh.refresh_due(uid, enabled=True) is False


def test_correction_keeps_the_cards_date_on_v1_and_v2_unless_one_is_given(io_world):
    """Regression: every correction moved a dated card to today, because
    memory.supersede took occurred_at from the payload or now() and neither
    io_cli memory-patch (V1) nor memory_write 'update' (V2) carries one."""
    from model_api_runtime.v2 import worker

    host = IoHost(io_world)
    src = host.sources[0]
    dated = "2024-06-15T12:00:00Z"

    # V1: io_cli memory-patch payload through /v1/memory/actions.
    v1_old = host.add("alice", kit.card("kitdatedvone", source=src, occurred_at=dated)).record_ids[0]
    v1 = host.patch("alice", v1_old, {"content": "kitdatedvone body: fixed wording."})
    assert v1.ok, v1
    assert host.inspect("alice", v1.record_ids[0])["occurred_at"] == dated

    # V2: memory_write op=update, translated by the worker, same executor.
    v2_old = host.add("alice", kit.card("kitdatedvtwo", source=src, occurred_at="2024-06-15")).record_ids[0]
    actions = worker._memory_tool_actions([{
        "op": "update", "target_id": v2_old, "summary": "kitdatedvtwo summary fixed",
        "content": "kitdatedvtwo body: fixed wording."}])
    v2 = host._act("alice", actions[0])
    assert v2.ok, v2
    assert host.inspect("alice", v2.record_ids[0])["occurred_at"] == "2024-06-15"
    new_row = _raw_row(host, "alice", v2.record_ids[0])
    assert new_row["last_referenced_at"] != "2024-06-15"  # a correction is a fresh reference

    # An explicit occurred_at on the supersede still wins.
    explicit_old = host.add("alice", kit.card("kitdatedexp", source=src, occurred_at=dated)).record_ids[0]
    explicit = host.supersede("alice", [explicit_old],
                              kit.card("kitdatedexpnew", source=src, occurred_at="2025-01-02T03:04:05Z"))
    assert explicit.ok, explicit
    assert host.inspect("alice", explicit.record_ids[0])["occurred_at"] == "2025-01-02T03:04:05Z"
