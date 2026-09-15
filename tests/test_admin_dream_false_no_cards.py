"""Admin audit + compare-and-set repair of false "no cards" Dream ledgers.

Stuck users are produced through the production path — real ``/v1/dream/tick``
enqueues and ``/v1/proactive/jobs/<id>/status`` reports, with only the newer
backend reclassification switched off (the pre-fix backend that produced the
09-10 / 09-13 ledgers) — then driven only through the admin HTTP routes, the
way an operator without database access will.
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest  # noqa: E402
from psycopg.errors import QueryCanceled  # noqa: E402

import db  # noqa: E402
import debug_trace  # noqa: E402
from accounts import registry  # noqa: E402
from admin import dream_ledger_repair  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from conftest import seed_user  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402
from model_api_runtime.v2 import jobs_store  # noqa: E402
from proactive import dream_ledger_audit as audit  # noqa: E402
from proactive import dream_scheduler  # noqa: E402
from tee_shadow import mirror  # noqa: E402

ADMIN_TOKEN = "admin-dream-repair-token"
AUDIT_PATH = "/v1/admin/memory/dream-false-no-cards"
REPAIR_PATH = "/v1/admin/memory/dream-false-no-cards/repair"
CARD_MARKER = "SECRET_CARD_BODY"


def _memory(user_id, memory_id):
    ts = "2026-06-20T00:00:00Z"
    return {
        "v": 1, "id": memory_id, "type": "fact", "owner_user_id": user_id,
        "visibility": "shared", "body_ct": f"{CARD_MARKER}_{memory_id}",
        "nonce": f"n_{memory_id}", "K_user": f"ku_{memory_id}",
        "K_enclave": f"ke_{memory_id}", "occurred_at": ts, "created_at": ts,
        "updated_at": ts, "status": "active", "importance": 0.6, "pulse": 0.3,
    }


def _legacy_no_cards():
    return {
        "status": "completed",
        "reason": "dream_no_cards_available",
        "dream_result": {"status": "noop", "reason": "dream_no_cards_available",
                         "job_kind": "memory_dream"},
        "cards_merged": 0, "cards_superseded": 0, "questions": [],
        "noop_reason": "dream_no_cards_available",
    }


def _verified_completion():
    return {
        "status": "completed",
        "reason": "dream_memory_actions_applied",
        "dream_result": {"status": "applied", "reason": "dream_memory_actions_applied",
                         "job_kind": "memory_dream", "organized_count": 2},
        "cards_merged": 1, "cards_superseded": 2, "questions": [],
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"  # completed_at uses the naive server clock; CVMs run UTC
    time.tzset()
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("FEEDLING_DREAM_NIGHT_ONLY", "false")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_NEW_CARDS", "1")
    monkeypatch.setenv("FEEDLING_DREAM_MIN_INTERVAL_SEC", "0")
    core_store._stores.clear()
    yield
    if previous_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous_tz
    time.tzset()


class _User:
    def __init__(self, monkeypatch, user_id):
        self.monkeypatch = monkeypatch
        self.user_id = user_id
        api_key = f"test_key_{user_id}"
        registry._key_to_user[registry._hash_api_key(api_key)] = user_id
        seed_user(user_id)
        self.headers = {"X-API-Key": api_key}
        self.client = make_client()
        self.cards = 0

    def add_cards(self, count):
        self.cards += count
        db.memory_replace_all(
            self.user_id, [_memory(self.user_id, f"mem_{i}") for i in range(self.cards)]
        )

    def tick(self):
        return self.client.post(
            "/v1/dream/tick", headers=self.headers, json={"now": time.time()}
        ).get_json()

    def dream(self, payload):
        tick = self.tick()
        assert tick["enqueued"] is True, tick
        job = tick["job"]
        with self.monkeypatch.context() as pre_fix:
            pre_fix.setattr(
                dream_scheduler, "reclassify_unverified_no_cards_completion",
                lambda _store, _job, patch: patch,
            )
            done = self.client.post(
                f"/v1/proactive/jobs/{job['job_id']}/status",
                headers=self.headers, json=payload,
            )
        assert done.status_code == 200, done.get_json()
        return job

    def ledger_doc(self):
        return dict(db.get_blob(self.user_id, "dream_state") or {})

    def job(self, job_id):
        [doc] = [
            job for job in db.log_read(self.user_id, "proactive_jobs", limit=0, since_epoch=0)
            if job.get("job_id") == job_id
        ]
        return doc


def _stuck(monkeypatch, user_id, *, dreamed_before=False):
    """A user whose ledger the pre-fix backend advanced with a false no-cards."""
    user = _User(monkeypatch, user_id)
    user.add_cards(3)
    previous = user.dream(_verified_completion()) if dreamed_before else None
    if dreamed_before:
        user.add_cards(2)
    incident = user.dream(_legacy_no_cards())
    user.add_cards(0)
    assert user.tick()["reason"] == "already_dreamed"  # the incident, reproduced
    return user, previous, incident


def _q(query):
    return f"{AUDIT_PATH}?{urlencode(query)}" if query else AUDIT_PATH


def _admin(token=ADMIN_TOKEN):
    return {"X-Admin-Token": token}


def _window():
    now = datetime.now(timezone.utc)
    return f"{audit.format_instant(now - timedelta(hours=1))}/{audit.format_instant(now + timedelta(hours=1))}"


def _audit(client, user_ids, **extra):
    query = [("window", _window())] + [("user_id", uid) for uid in user_ids]
    query += list(extra.items())
    response = client.get(_q(query), headers=_admin())
    return response.status_code, response.get_json()


def _repair(client, users, **body):
    payload = {"windows": [_window()], "users": users, **body}
    response = client.post(REPAIR_PATH, headers=_admin(), json=payload)
    return response.status_code, response.get_json()


def _target(row):
    """The per-user repair entry an operator builds from one audit row."""
    return {key: row[key] for key in ("user_id", "ledger_fingerprint", "job_id", "rewound_job_ids")}


def _non_ledger(doc):
    return {
        key: value for key, value in doc.items()
        if key not in audit.LEDGER_FIELDS and key not in {"last_dream_trace_at", "last_dream_trace_reason"}
    }


def test_audit_lists_stuck_user_and_repair_rewinds_only_that_ledger_then_dream_enqueues(
    env, monkeypatch,
):
    stuck, previous, incident = _stuck(monkeypatch, "usr_repair_stuck_0915", dreamed_before=True)
    bystander, _prev, _inc = _stuck(monkeypatch, "usr_repair_bystander_0915")
    client = stuck.client
    bystander_before = bystander.ledger_doc()
    traces, mirrored = [], []
    monkeypatch.setattr(debug_trace, "trace_event", lambda store, **kw: traces.append((store.user_id, kw)))
    monkeypatch.setattr(mirror, "execute", lambda sql, params=(): mirrored.append((sql, params)))

    # 1. read-only audit
    status, report = _audit(client, [stuck.user_id, bystander.user_id], statement_timeout_sec="7")
    assert status == 200
    assert report["mode"] == "read_only"
    assert report["verdicts"] == {"candidate": 2}
    assert report["prefilter"]["statement_timeout_sec"] == 7.0
    row = next(c for c in report["candidates"] if c["user_id"] == stuck.user_id)
    assert row["job_id"] == incident["job_id"]
    assert row["restore_from_job_id"] == previous["job_id"]
    assert row["ledger_fingerprint"] == audit.ledger_fingerprint(stuck.ledger_doc())
    assert CARD_MARKER not in json.dumps(report)
    assert mirrored == [] and traces == []

    # 2. dry run (the default) predicts exactly the rewind and writes nothing
    before = stuck.ledger_doc()
    target = [_target(row)]
    status, dry = _repair(client, target)
    assert status == 200 and dry["mode"] == "dry_run"
    [planned] = dry["results"]
    assert planned["action"] == "would_rewind"
    assert planned["job_id"] == incident["job_id"]
    assert planned["changes"]["last_dream_signature"] == {
        "from": before["last_dream_signature"],
        "to": row["restore_ledger"]["last_dream_signature"],
    }
    assert set(planned["changes"]) <= set(audit.LEDGER_FIELDS)
    assert planned["would_reclassify_job_ids"] == [incident["job_id"]]
    assert stuck.ledger_doc() == before
    assert stuck.job(incident["job_id"])["status"] == "completed"
    assert mirrored == [] and traces == []

    # 3. apply: only ledger fields change, only for the listed user, mirrored to TEE
    status, applied = _repair(client, target, dry_run=False)
    assert status == 200 and applied["mode"] == "apply"
    [done] = applied["results"]
    assert done["action"] == "rewound"
    assert done["changes"] == planned["changes"]
    after = stuck.ledger_doc()
    assert audit.canonical_ledger(after) == audit.canonical_ledger(row["restore_ledger"])
    assert _non_ledger(after) == _non_ledger(before)
    assert done["ledger_fingerprint_after"] == audit.ledger_fingerprint(after)
    assert bystander.ledger_doc() == bystander_before
    assert done["reclassified_job_ids"] == [incident["job_id"]]
    assert done["unreclassified_job_ids"] == []
    job = stuck.job(incident["job_id"])
    assert job["status"] == "failed"
    assert job["status_reason"] == dream_scheduler.CONTEXT_UNAVAILABLE_REASON
    assert job["completed_at"] == job["dream_ledger_repair"]["original_completed_at"]
    assert job["dream_ledger_repair"]["original_status"] == "completed"
    assert stuck.job(previous["job_id"])["status"] == "completed"
    [(sql, params)] = [call for call in mirrored if "user_blobs" in call[0]]
    assert params[0] == stuck.user_id and params[1] == "dream_state"
    assert params[2].obj == after
    [(job_sql, job_params)] = [call for call in mirrored if "user_logs" in call[0]]
    assert "doc->>'status' = %s" in job_sql and job_params[-1] == "completed"
    [(trace_user, trace)] = traces
    assert trace_user == stuck.user_id
    assert trace["type"] == "memory.dream.ledger_rewound" and trace["actor"] == "admin"
    assert trace["detail"]["job_id"] == incident["job_id"]
    assert CARD_MARKER not in json.dumps([applied, trace], default=str)

    # 4. idempotent re-run: no write, no trace; the audit reports it as repaired
    status, again = _repair(client, target, dry_run=False)
    assert status == 200
    assert [r["action"] for r in again["results"]] == ["already_repaired"]
    assert again["results"][0]["reclassified_job_ids"] == []
    assert len(mirrored) == 2 and len(traces) == 1
    assert stuck.ledger_doc() == after
    status, report = _audit(client, [stuck.user_id])
    assert report["verdicts"] == {"already_repaired": 1}
    assert report["already_repaired"] == [{
        "user_id": stuck.user_id, "job_id": incident["job_id"], "unreclassified_job_ids": [],
    }]

    # 5. the next scheduler tick re-evaluates the real garden and enqueues
    tick = stuck.tick()
    assert tick["enqueued"] is True, tick
    assert bystander.tick()["reason"] == "already_dreamed"


def test_rewind_alone_leaves_the_key_blocked_and_a_rerun_finishes_the_repair(env, monkeypatch):
    """Same garden, same turn count: the rewound ledger recomputes the incident
    job's dream_key, which a completed job never releases."""
    stuck, _previous, incident = _stuck(monkeypatch, "usr_repair_dupkey_0915")
    client = stuck.client
    _status, report = _audit(client, [stuck.user_id])
    target = [_target(report["candidates"][0])]
    with monkeypatch.context() as lost:
        lost.setattr(db, "log_patch_item", lambda *_a, **_k: None)  # reclassify lost
        _status, applied = _repair(client, target, dry_run=False)
    [done] = applied["results"]
    assert done["action"] == "rewound"
    assert done["unreclassified_job_ids"] == [incident["job_id"]]
    assert stuck.tick()["reason"] == "duplicate_dream_key"

    _status, report = _audit(client, [stuck.user_id])
    assert report["already_repaired"][0]["unreclassified_job_ids"] == [incident["job_id"]]
    _status, rerun = _repair(client, target, dry_run=False)
    [finished] = rerun["results"]
    assert finished["action"] == "already_repaired"
    assert finished["reclassified_job_ids"] == [incident["job_id"]]
    assert stuck.tick()["enqueued"] is True


def test_repair_skips_a_ledger_that_changed_after_the_audit(env, monkeypatch):
    stuck, _previous, _incident = _stuck(monkeypatch, "usr_repair_stale_0915")
    client = stuck.client
    status, report = _audit(client, [stuck.user_id])
    target = [_target(report["candidates"][0])]

    # (a) a stale fingerprint (the ledger moved before the repair request)
    stale = [{**target[0], "ledger_fingerprint": "0" * 64}]
    status, result = _repair(client, stale, dry_run=False)
    assert status == 200
    assert [(r["action"], r["reason"]) for r in result["results"]] == [
        ("skipped", "ledger_changed_since_audit")
    ]

    # (b) the ledger moves between the request's read and its write
    real_cas = db.patch_blob_if_match_strict

    def dream_lands_first(user_id, kind, patch, **kwargs):
        db.patch_blob_strict(user_id, kind, {"last_dreamed_turn_count": 99})
        return real_cas(user_id, kind, patch, **kwargs)

    monkeypatch.setattr(db, "patch_blob_if_match_strict", dream_lands_first)
    moved = stuck.ledger_doc()
    status, result = _repair(client, target, dry_run=False)
    assert status == 200
    assert [(r["action"], r["reason"]) for r in result["results"]] == [
        ("skipped", "ledger_changed_since_audit")
    ]
    assert stuck.ledger_doc() == {**moved, "last_dreamed_turn_count": 99}
    assert stuck.job(_incident["job_id"])["status"] == "completed"  # nothing reclassified
    assert stuck.tick()["reason"] == "already_dreamed"


def test_repair_skips_users_that_are_not_candidates(env, monkeypatch):
    user = _User(monkeypatch, "usr_repair_healthy_0915")
    user.add_cards(3)
    user.dream(_verified_completion())
    status, result = _repair(
        user.client, [{"user_id": user.user_id, "ledger_fingerprint": "a" * 64,
                       "job_id": "job_x", "rewound_job_ids": ["job_x"]}], dry_run=False,
    )
    assert status == 200
    assert result["results"] == [{
        "user_id": user.user_id, "action": "skipped", "reason": "no_incident_completion",
    }]


def test_admin_token_is_required(env, monkeypatch):
    client = make_client()
    body = {"windows": [_window()], "users": [_entry()]}
    for headers in ({}, _admin("wrong-token"), {"X-API-Key": "test_key_usr_x"}):
        assert client.get(_q([("window", _window())]), headers=headers).status_code == 401
        assert client.post(REPAIR_PATH, headers=headers, json=body).status_code == 401
    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN")
    assert client.get(_q([("window", _window())]), headers=_admin()).status_code == 503


def _entry(**overrides):
    return {"user_id": "usr_x", "ledger_fingerprint": "a" * 64, "job_id": "job_x",
            "rewound_job_ids": ["job_x"], **overrides}


@pytest.mark.parametrize(
    "body, detail",
    [
        pytest.param({"windows": ["W"], "users": []}, "users_required", id="no-users"),
        pytest.param({"windows": ["W"]}, "users_required", id="bulk-without-ids"),
        pytest.param({"windows": ["W"], "users": "all"}, "users_required", id="users-all"),
        pytest.param({"windows": ["W"], "all_users": True,
                      "users": [_entry()]},
                     "unknown_fields:all_users", id="unknown-field"),
        pytest.param({"windows": ["W"], "users": [{"user_id": "usr_x"}]},
                     "invalid_user_entry", id="missing-fingerprint"),
        pytest.param({"windows": ["W"], "users": [
            {k: v for k, v in _entry().items() if k != "job_id"}]},
            "invalid_user_entry", id="missing-job-id"),
        pytest.param({"windows": ["W"], "users": [_entry(rewound_job_ids=[])]},
                     "invalid_rewound_job_ids", id="empty-rewound-job-ids"),
        pytest.param({"windows": ["W"], "users": [_entry(job_id="bad id!")]},
                     "invalid_job_id", id="bad-job-id"),
        pytest.param({"windows": ["W"], "users": [_entry(ledger_fingerprint="zz")]},
                     "invalid_ledger_fingerprint", id="bad-fingerprint"),
        pytest.param({"windows": ["W"], "dry_run": "false",
                      "users": [_entry()]},
                     "invalid_dry_run", id="string-dry-run"),
        pytest.param({"windows": ["W"], "users": [
            _entry(),
            _entry(ledger_fingerprint="b" * 64)]},
            "duplicate_user_id", id="duplicate-user"),
        pytest.param({"users": [_entry()]},
                     "window_required", id="no-window"),
        pytest.param({"windows": ["2026-09-01T00:00:00Z/2026-09-10T00:00:00Z"],
                      "users": [_entry()]},
                     "window_too_long", id="window-too-long"),
    ],
)
def test_repair_rejects_bulk_or_malformed_requests(env, body, detail):
    body = {**body}
    if body.get("windows") == ["W"]:
        body["windows"] = [_window()]
    response = make_client().post(REPAIR_PATH, headers=_admin(), json=body)
    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_dream_ledger_request", "detail": detail}


@pytest.mark.parametrize(
    "query, detail",
    [
        pytest.param([], "window_required", id="no-window"),
        pytest.param([("window", "2026-09-10T20:00:00Z/2026-09-10T18:00:00Z")],
                     "invalid_window", id="reversed-window"),
        pytest.param([("window", "W"), ("apply", "1")], "unknown_query_params:apply", id="unknown"),
        pytest.param([("window", "W"), ("statement_timeout_sec", "600")],
                     "invalid_statement_timeout_sec", id="timeout-unbounded"),
    ],
)
def test_audit_rejects_malformed_queries(env, query, detail):
    query = [(k, _window() if v == "W" else v) for k, v in query]
    response = make_client().get(_q(query), headers=_admin())
    assert response.status_code == 400
    assert response.get_json() == {"error": "invalid_dream_ledger_request", "detail": detail}


def test_audit_query_timeout_is_a_503(env, monkeypatch):
    def cancelled(_params):
        raise QueryCanceled("canceling statement due to statement timeout")

    monkeypatch.setattr(dream_ledger_repair, "audit_payload", cancelled)
    response = make_client().get(_q([("window", _window())]), headers=_admin())
    assert response.status_code == 503
    assert response.get_json() == {"error": "dream_ledger_query_timeout"}


def test_repair_stops_starting_writes_when_the_request_budget_is_spent(env, monkeypatch):
    stuck, _previous, _incident = _stuck(monkeypatch, "usr_repair_budget_0915")
    _status, report = _audit(stuck.client, [stuck.user_id])
    before = stuck.ledger_doc()
    ticks = iter([0.0, dream_ledger_repair.REPAIR_WRITE_BUDGET_SEC + 1])
    result = dream_ledger_repair.repair_payload(
        dream_ledger_repair.parse_repair_body({
            "windows": [_window()], "dry_run": False,
            "users": [_target(report["candidates"][0])],
        }),
        clock=lambda: next(ticks),
    )
    assert [r["action"] for r in result["results"]] == ["not_attempted"]
    assert stuck.ledger_doc() == before


def test_compare_and_merge_primitive_never_creates_or_blindly_writes(env, monkeypatch):
    user_id = "usr_repair_cas_0915"
    seed_user(user_id)
    mirrored = []
    monkeypatch.setattr(mirror, "execute", lambda sql, params=(): mirrored.append(params))

    assert db.patch_blob_if_match_strict(
        user_id, "dream_state", {"a": 1}, precondition=lambda _doc, _conn: True,
    ) == (False, None)
    assert db.get_blob(user_id, "dream_state") is None

    db.set_blob(user_id, "dream_state", {"a": 0, "keep": "x"})
    mirrored.clear()
    assert db.patch_blob_if_match_strict(
        user_id, "dream_state", {"a": 1}, precondition=lambda doc, _conn: doc["a"] == 5,
    ) == (False, {"a": 0, "keep": "x"})
    assert db.get_blob(user_id, "dream_state") == {"a": 0, "keep": "x"}
    assert mirrored == []

    applied, doc = db.patch_blob_if_match_strict(
        user_id, "dream_state", {"a": 1}, precondition=lambda doc, _conn: doc["a"] == 0,
        statement_timeout_ms=3000,
    )
    assert applied is True and doc == {"a": 1, "keep": "x"}
    assert db.get_blob(user_id, "dream_state") == doc
    assert [(p[0], p[1], p[2].obj) for p in mirrored] == [(user_id, "dream_state", doc)]

    with pytest.raises(ValueError):
        db.patch_blob_if_match_strict(
            user_id, "model_api_runtime", {"a": 1}, precondition=lambda _doc, _conn: True,
        )


def _apply(client, row, **body):
    status, result = _repair(client, [_target(row)], dry_run=False, **body)
    assert status == 200, result
    [only] = result["results"]
    return only


def test_scheduler_and_audit_agree_on_the_ledger_fields():
    assert audit.LEDGER_FIELDS == dream_scheduler.DREAM_LEDGER_FIELDS


@pytest.mark.parametrize("when", ["v1_queued", "v2_queued", "v2_queued_during_repair"])
def test_repair_refuses_while_the_user_has_an_active_dream_job(env, monkeypatch, when):
    """Codex r4 I1: no rewind while a Dream job of that user may still record a result."""
    stuck, _previous, incident = _stuck(monkeypatch, f"usr_repair_active_{when}_0915")
    client = stuck.client
    _status, report = _audit(client, [stuck.user_id])
    [row] = report["candidates"]
    if when == "v1_queued":
        stuck.add_cards(1)
        tick = stuck.tick()
        assert tick["enqueued"] is True, tick
        active_id = tick["job"]["job_id"]
    elif when == "v2_queued":
        job_id, _merged = jobs_store.enqueue_job(stuck.user_id, "dream", reason="nightly_dream")
        active_id = f"v2:{job_id}"
    else:
        real_cas = db.patch_blob_if_match_strict
        queued = []

        def job_lands_first(user_id, kind, patch, **kwargs):
            # After the request's read and dry-run style checks, before the write.
            queued.append(jobs_store.enqueue_job(user_id, "dream", reason="nightly_dream")[0])
            return real_cas(user_id, kind, patch, **kwargs)

        monkeypatch.setattr(db, "patch_blob_if_match_strict", job_lands_first)
    before = stuck.ledger_doc()
    assert _audit(client, [stuck.user_id])[1]["candidates"][0]["ledger_fingerprint"] == \
        row["ledger_fingerprint"]

    if when != "v2_queued_during_repair":
        _status, dry = _repair(client, [_target(row)])
        assert [(r["action"], r["reason"], r["active_job_ids"]) for r in dry["results"]] == [
            ("skipped", "dream_job_active", [active_id])
        ]
    done = _apply(client, row)
    if when == "v2_queued_during_repair":
        active_id = f"v2:{queued[0]}"
    assert (done["action"], done["reason"]) == ("skipped", "dream_job_active")
    assert done["active_job_ids"] == [active_id]
    assert audit.canonical_ledger(stuck.ledger_doc()) == audit.canonical_ledger(before)
    assert stuck.job(incident["job_id"])["status"] == "completed"


class _PausedRead:
    """Pause the next Dream state read until the test releases it."""

    def __init__(self, monkeypatch):
        self.read = threading.Event()
        self.release = threading.Event()
        self._armed = True
        self._lock = threading.Lock()
        real = dream_scheduler.load_dream_state

        def load(store):
            state = real(store)
            with self._lock:
                pause, self._armed = self._armed, False
            if pause:
                self.read.set()
                assert self.release.wait(30)
            return state

        monkeypatch.setattr(dream_scheduler, "load_dream_state", load)


@pytest.mark.parametrize("writer", ["job_status_report", "scheduler_tick"])
def test_a_dream_writer_that_read_before_the_repair_cannot_put_the_old_ledger_back(
    env, monkeypatch, writer,
):
    """Codex r4 I1: the writer read the incident ledger, the repair committed, the
    writer then saved. Its save must not resurrect the incident ledger, so the
    repair's reported ``rewound`` stays true."""
    stuck, _previous, incident = _stuck(monkeypatch, f"usr_repair_stale_{writer}_0915")
    client = stuck.client
    _status, report = _audit(client, [stuck.user_id])
    [row] = report["candidates"]
    incident_ledger = audit.canonical_ledger(stuck.ledger_doc())
    stuck.add_cards(1)
    if writer == "job_status_report":
        tick = stuck.tick()
        assert tick["enqueued"] is True, tick
        job_id = tick["job"]["job_id"]

        def write():
            return stuck.client.post(
                f"/v1/proactive/jobs/{job_id}/status", headers=stuck.headers,
                json={"status": "failed", "reason": "dream_agent_timeout"},
            ).get_json()
    else:
        write = stuck.tick

    paused = _PausedRead(monkeypatch)
    out = {}
    thread = threading.Thread(target=lambda: out.setdefault("writer", write()))
    thread.start()
    assert paused.read.wait(30)
    try:
        done = _apply(client, row)
    finally:
        paused.release.set()
        thread.join(30)
    assert not thread.is_alive()

    after = stuck.ledger_doc()
    assert done["action"] == "rewound", done
    assert audit.canonical_ledger(after) != incident_ledger
    assert audit.canonical_ledger(after) == audit.canonical_ledger(row["restore_ledger"])
    assert done["ledger_fingerprint_after"] == audit.ledger_fingerprint(after)
    # The writer's own write did land (it was not simply dropped).
    if writer == "job_status_report":
        assert out["writer"]["job"]["status"] == "failed"
        assert after["dream_fail_streak"] == 1
    else:
        assert out["writer"]["enqueued"] is True, out
        assert after["pending_dream_key"] == out["writer"]["job"]["dream_key"]
    assert stuck.job(incident["job_id"])["status"] == "failed"


def test_named_user_is_found_however_long_its_dream_waited_before_completing(env, monkeypatch):
    """Codex r4 I2: a Dream enqueued 91 days before completing inside the window
    (self-hosted consumer offline) is past the global scan bound and even the
    90-day request cap, but naming the user finds and repairs it."""
    user = _User(monkeypatch, "usr_repair_old_enqueue_0915")
    user.add_cards(3)
    enqueued_at = time.time() - 91 * 86400
    with monkeypatch.context() as late:
        late.setattr(dream_scheduler, "_trace_now", lambda: enqueued_at)
        incident = user.dream(_legacy_no_cards())
    with db.get_pool().connection() as conn:
        [ts] = conn.execute(
            "SELECT ts FROM user_logs WHERE user_id=%s AND stream='proactive_jobs' "
            "AND doc->>'job_id'=%s", (user.user_id, incident["job_id"]),
        ).fetchone()
    assert abs(float(ts) - enqueued_at) < 5
    assert user.tick()["reason"] == "already_dreamed"

    _status, fleet = _audit(user.client, [], max_job_age_days="90")
    assert user.user_id not in {row["user_id"] for row in fleet["candidates"]}
    assert fleet["partial"] is True and fleet["scan_bound"]["max_job_age_days"] == 90.0

    _status, named = _audit(user.client, [user.user_id])
    assert named["partial"] is False and named["scan_bound"] is None
    [row] = named["candidates"]
    assert row["job_id"] == incident["job_id"]
    done = _apply(user.client, row)
    assert done["action"] == "rewound", done
    assert user.tick()["enqueued"] is True


def test_a_garden_emptied_before_the_legacy_report_is_left_alone(env, monkeypatch):
    """Codex r4 I3: cards deleted after enqueue, then an old consumer's (true)
    "no cards" lands in the window. It matches the incident shape, but the repair
    does not rewind or reclassify it."""
    user = _User(monkeypatch, "usr_repair_emptied_0915")
    user.add_cards(3)
    tick = user.tick()
    assert tick["enqueued"] is True, tick
    job_id = tick["job"]["job_id"]
    db.memory_replace_all(user.user_id, [])  # the user deleted every card
    # Current backend: zero live cards, so the legacy report stays a completion.
    reported = user.client.post(
        f"/v1/proactive/jobs/{job_id}/status", headers=user.headers, json=_legacy_no_cards(),
    ).get_json()
    assert reported["job"]["status"] == "completed"

    _status, report = _audit(user.client, [user.user_id])
    [row] = report["candidates"]  # the selector alone cannot tell
    before = user.ledger_doc()
    _status, dry = _repair(user.client, [_target(row)])
    assert [(r["action"], r["reason"]) for r in dry["results"]] == [
        ("skipped", "ambiguous_legacy_no_cards")
    ]
    done = _apply(user.client, row)
    assert (done["action"], done["reason"]) == ("skipped", "ambiguous_legacy_no_cards")
    assert user.ledger_doc() == before
    assert user.job(job_id)["status"] == "completed"
    assert CARD_MARKER not in json.dumps([dry, done])


def test_repair_is_bound_to_the_job_ids_the_audit_reported(env, monkeypatch):
    stuck, previous, incident = _stuck(monkeypatch, "usr_repair_bound_jobs_0915", dreamed_before=True)
    _status, report = _audit(stuck.client, [stuck.user_id])
    [row] = report["candidates"]
    before = stuck.ledger_doc()
    for wrong in ({**row, "job_id": previous["job_id"]},
                  {**row, "rewound_job_ids": [incident["job_id"], previous["job_id"]]}):
        done = _apply(stuck.client, wrong)
        assert (done["action"], done["reason"]) == ("skipped", "jobs_changed_since_audit")
    assert stuck.ledger_doc() == before
    assert stuck.job(incident["job_id"])["status"] == "completed"
    assert _apply(stuck.client, row)["action"] == "rewound"
