"""Admin-triggered TEE replication guardrails (spec §5 执行防护，P2T8).

Endpoint-layer only: monkeypatches ``tee_replicator.worker.run_table`` /
``tee_shadow.reconciler.reconcile_table`` with in-process stubs so no test
here ever calls the enclave or performs a real reconcile/replicate pass. What
is under test is the guardrail wiring in ``backend/admin/tee_replication.py``
+ ``backend/admin/routes_asgi.py`` — the confirm gate, the dry-run reconcile
"plan" short-circuit, table validation, the admin-token auth parity, and the
non-blocking concurrency guard. The replication logic itself is covered by
test_tee_reconciler.py / test_tee_replicator_worker.py / test_tee_verify.py.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from tee_shadow import reconciler as tee_reconciler  # noqa: E402

ADMIN_TOKEN = "tee-replication-admin-token"


def _admin_headers():
    return {"X-Admin-Token": ADMIN_TOKEN}


def _set_admin_token(monkeypatch):
    monkeypatch.setenv("FEEDLING_ADMIN_TOKEN", ADMIN_TOKEN)


# --------------------------------------------------------------------------- #
# ① confirm gate: non-dry_run reconcile/replicate need a literal MIGRATE
# --------------------------------------------------------------------------- #

def test_non_dry_run_reconcile_without_confirm_is_400(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "reconcile", "dry_run": False},
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "confirm_required"}


def test_non_dry_run_replicate_without_confirm_is_400(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "replicate", "table": "chat_messages", "dry_run": False},
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "confirm_required"}


def test_non_dry_run_reflow_without_confirm_is_400(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "reflow", "table": "chat_messages", "dry_run": False},
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "confirm_required"}


def test_non_dry_run_replicate_wrong_confirm_literal_is_400(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={
            "action": "replicate",
            "table": "chat_messages",
            "dry_run": False,
            "confirm": "migrate",  # wrong case — must be literal MIGRATE
        },
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "confirm_required"}


def test_verify_never_needs_confirm_even_with_dry_run_false(client, monkeypatch):
    _set_admin_token(monkeypatch)
    calls = []

    def _stub(**kwargs):
        calls.append(kwargs)
        return {"tables": [], "failures": 0}

    # This is an endpoint guardrail test, not a verifier integration test.
    # Keep it independent of rows left by earlier tests in the session and
    # prove that the requested sampling policy reaches the verifier boundary.
    monkeypatch.setattr("tee_shadow.verify.run", _stub)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "verify", "dry_run": False, "sample_rate": 0.0},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["action"] == "verify"
    assert body["dry_run"] is False
    assert calls == [{"sample_rate": 0.0}]


def test_dry_run_must_be_json_bool(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "replicate", "table": "chat_messages", "dry_run": "false"},
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "invalid_dry_run"}


# --------------------------------------------------------------------------- #
# ①b authorized path: confirm=MIGRATE actually executes the stubbed operation
# --------------------------------------------------------------------------- #

def test_confirmed_replicate_executes_stub_once(client, monkeypatch):
    _set_admin_token(monkeypatch)
    calls = []

    def _stub(table, *, qps=2.0, dry_run=False):
        calls.append({"table": table, "qps": qps, "dry_run": dry_run})
        return {"copied": 7, "pending": 1, "errors": 0}

    monkeypatch.setattr("tee_replicator.worker.run_table", _stub)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={
            "action": "replicate",
            "table": "chat_messages",
            "dry_run": False,
            "confirm": "MIGRATE",
            "qps": 4.0,
        },
    )
    assert res.status_code == 200
    assert calls == [{"table": "chat_messages", "qps": 4.0, "dry_run": False}]
    body = res.get_json()
    assert body["action"] == "replicate"
    assert body["dry_run"] is False
    # The stub's report is echoed back verbatim.
    assert body["copied"] == 7
    assert body["pending"] == 1
    assert body["errors"] == 0


def test_confirmed_reconcile_all_invokes_stub(client, monkeypatch):
    _set_admin_token(monkeypatch)
    calls = []

    def _stub(table, **kwargs):
        calls.append(table)
        return {"table": table, "copied": 0, "pruned": 0, "rds_rows": 0, "tee_rows": 0}

    # reconcile_all() iterates TABLES calling reconcile_table — stub the leaf
    # so the endpoint's non-dry-run path really drives the reconciler entry.
    monkeypatch.setattr("tee_shadow.reconciler.reconcile_table", _stub)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "reconcile", "dry_run": False, "confirm": "MIGRATE"},
    )
    assert res.status_code == 200
    assert calls == list(tee_reconciler.TABLES)
    body = res.get_json()
    assert body["action"] == "reconcile"
    assert body["dry_run"] is False
    assert [t["table"] for t in body["tables"]] == list(tee_reconciler.TABLES)


def test_confirmed_reconcile_single_table_invokes_stub_once(client, monkeypatch):
    _set_admin_token(monkeypatch)
    calls = []

    def _stub(table, **kwargs):
        calls.append(table)
        return {"table": table, "copied": 3, "pruned": 0, "rds_rows": 3, "tee_rows": 3}

    monkeypatch.setattr("tee_shadow.reconciler.reconcile_table", _stub)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "reconcile", "table": "users", "dry_run": False, "confirm": "MIGRATE"},
    )
    assert res.status_code == 200
    assert calls == ["users"]
    body = res.get_json()
    assert body["tables"] == [
        {"table": "users", "copied": 3, "pruned": 0, "rds_rows": 3, "tee_rows": 3}
    ]


# --------------------------------------------------------------------------- #
# ② dry_run reconcile → plan only, zero writes to either DB
# --------------------------------------------------------------------------- #

def test_dry_run_reconcile_all_returns_plan_without_executing(client, monkeypatch):
    _set_admin_token(monkeypatch)
    called = []
    monkeypatch.setattr(
        "tee_shadow.reconciler.reconcile_table",
        lambda *a, **k: called.append((a, k)) or {},
    )
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "reconcile", "dry_run": True},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["dry_run"] is True
    assert body["action"] == "reconcile"
    assert set(body["plan"]) == set(tee_reconciler.TABLES)
    assert called == []  # reconcile_table never actually invoked


def test_dry_run_reconcile_single_table_plan(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "reconcile", "table": "users", "dry_run": True},
    )
    assert res.status_code == 200
    assert res.get_json()["plan"] == ["users"]


def test_reconcile_unknown_table_is_400(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "reconcile", "table": "not_a_real_table", "dry_run": True},
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "unknown_table"}


# --------------------------------------------------------------------------- #
# ③ status shape + admin-auth failure path (parity with existing admin routes)
# --------------------------------------------------------------------------- #

def test_status_shape(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.get("/v1/admin/tee-replication/status", headers=_admin_headers())
    assert res.status_code == 200
    body = res.get_json()
    assert set(body.keys()) == {
        "cursors", "pending_count", "pending_by_table",
        "mirror_failures", "dual_write_enabled", "running",
        # observability additions (migration 0015): live health probe + persisted
        # sync-run history for the cut-read soak decision.
        "health", "latest_run", "recent_runs",
    }
    assert isinstance(body["cursors"], list)
    assert isinstance(body["pending_count"], int)
    assert isinstance(body["pending_by_table"], dict)
    assert isinstance(body["mirror_failures"], int)
    assert isinstance(body["dual_write_enabled"], bool)
    assert body["running"] is False
    assert isinstance(body["health"], dict) and "ok" in body["health"]
    assert isinstance(body["recent_runs"], list)


def test_status_without_admin_token_is_401(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.get("/v1/admin/tee-replication/status")
    assert res.status_code == 401
    assert res.get_json() == {"error": "unauthorized"}


def test_run_without_admin_token_is_401(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post("/v1/admin/tee-replication/run", json={"action": "verify"})
    assert res.status_code == 401
    assert res.get_json() == {"error": "unauthorized"}


def test_run_unconfigured_admin_token_is_503(client, monkeypatch):
    monkeypatch.delenv("FEEDLING_ADMIN_TOKEN", raising=False)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "verify"},
    )
    assert res.status_code == 503


# --------------------------------------------------------------------------- #
# M2: TEE_DATABASE_URL unset → clean 503 (not a KeyError 500) on status + run
# --------------------------------------------------------------------------- #

def test_status_tee_database_unconfigured_is_503(client, monkeypatch):
    _set_admin_token(monkeypatch)
    monkeypatch.delenv("TEE_DATABASE_URL", raising=False)
    res = client.get("/v1/admin/tee-replication/status", headers=_admin_headers())
    assert res.status_code == 503
    assert res.get_json() == {"error": "tee_database_unconfigured"}


def test_run_verify_tee_database_unconfigured_is_503(client, monkeypatch):
    _set_admin_token(monkeypatch)
    monkeypatch.delenv("TEE_DATABASE_URL", raising=False)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "verify", "sample_rate": 0.0},
    )
    assert res.status_code == 503
    assert res.get_json() == {"error": "tee_database_unconfigured"}


def test_dry_run_reconcile_plan_still_works_without_tee_database(client, monkeypatch):
    # The plan-only short-circuit never touches the TEE pool, so it must NOT 503
    # even with TEE_DATABASE_URL unset.
    _set_admin_token(monkeypatch)
    monkeypatch.delenv("TEE_DATABASE_URL", raising=False)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "reconcile", "dry_run": True},
    )
    assert res.status_code == 200
    assert set(res.get_json()["plan"]) == set(tee_reconciler.TABLES)


# --------------------------------------------------------------------------- #
# ④ replicate: param passthrough to the stub + table validation
# --------------------------------------------------------------------------- #

def test_replicate_passes_through_table_qps_dry_run(client, monkeypatch):
    _set_admin_token(monkeypatch)
    captured = {}

    def _stub(table, *, qps=2.0, dry_run=False):
        captured["table"] = table
        captured["qps"] = qps
        captured["dry_run"] = dry_run
        return {"copied": 0, "pending": 0, "errors": 0}

    monkeypatch.setattr("tee_replicator.worker.run_table", _stub)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "replicate", "table": "chat_messages", "qps": 5.0, "dry_run": True},
    )
    assert res.status_code == 200
    assert captured == {"table": "chat_messages", "qps": 5.0, "dry_run": True}
    body = res.get_json()
    assert body["action"] == "replicate"
    assert body["dry_run"] is True
    assert body["copied"] == 0


def test_replicate_missing_table_is_400(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "replicate", "dry_run": True},
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "table_required"}


def test_replicate_unknown_table_is_400(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "replicate", "table": "not_a_table", "dry_run": True},
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "unknown_table"}


def test_reflow_passes_exact_stale_preview_to_recovery_boundary(client, monkeypatch):
    _set_admin_token(monkeypatch)
    captured = {}

    def _stub(table, *, qps=2.0, dry_run=True, expected_stale=None):
        captured.update(
            table=table, qps=qps, dry_run=dry_run, expected_stale=expected_stale
        )
        return {"scanned": 21, "would_copy": 21, "stale": 15001}

    monkeypatch.setattr("tee_replicator.reflow.reflow_table", _stub)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={
            "action": "reflow",
            "table": "voice_transcripts",
            "dry_run": True,
            "qps": 20.0,
            "expected_stale": 15001,
        },
    )
    assert res.status_code == 200
    assert captured == {
        "table": "voice_transcripts",
        "qps": 20.0,
        "dry_run": True,
        "expected_stale": 15001,
    }
    assert res.get_json()["action"] == "reflow"


def test_reflow_requires_a_known_table_and_non_negative_expected_stale(client, monkeypatch):
    _set_admin_token(monkeypatch)
    for payload in (
        {"action": "reflow", "dry_run": True},
        {"action": "reflow", "table": "not-a-table", "dry_run": True},
        {
            "action": "reflow",
            "table": "chat_messages",
            "dry_run": True,
            "expected_stale": -1,
        },
    ):
        res = client.post(
            "/v1/admin/tee-replication/run", headers=_admin_headers(), json=payload
        )
        assert res.status_code == 400


def test_reflow_rejects_table_without_typed_recovery_contract(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "reflow", "table": "v2_trajectory_events", "dry_run": True},
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "unsupported_reflow_table"}


def test_reflow_rejects_invalid_qps_before_dispatch(client, monkeypatch):
    _set_admin_token(monkeypatch)
    for qps in (True, "20", -1):
        res = client.post(
            "/v1/admin/tee-replication/run",
            headers=_admin_headers(),
            json={
                "action": "reflow",
                "table": "voice_transcripts",
                "dry_run": True,
                "qps": qps,
            },
        )
        assert res.status_code == 400
        assert res.get_json() == {"error": "invalid_qps"}
    from admin import tee_replication as admin_tee_replication

    for qps in (float("nan"), float("inf")):
        with pytest.raises(admin_tee_replication.BadRequest) as exc:
            admin_tee_replication.run_action(
                action="reflow",
                table="voice_transcripts",
                dry_run=True,
                qps=qps,
            )
        assert exc.value.error == "invalid_qps"


def test_prune_dispatches_exact_stale_guard(client, monkeypatch):
    _set_admin_token(monkeypatch)
    captured = {}

    def _stub(table, *, dry_run=False, expected_stale=None):
        captured.update(table=table, dry_run=dry_run, expected_stale=expected_stale)
        return {"table": table, "stale": 15001, "deleted": 0, "error": None, "refused": None}

    monkeypatch.setattr("tee_shadow.ciphertext_prune.prune_table", _stub)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={
            "action": "prune",
            "table": "v2_trajectory_events",
            "dry_run": True,
            "expected_stale": 15001,
        },
    )
    assert res.status_code == 200
    assert captured == {
        "table": "v2_trajectory_events",
        "dry_run": True,
        "expected_stale": 15001,
    }
    assert res.get_json()["ok"] is True


def test_unknown_action_is_400(client, monkeypatch):
    _set_admin_token(monkeypatch)
    res = client.post(
        "/v1/admin/tee-replication/run",
        headers=_admin_headers(),
        json={"action": "nuke_everything"},
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "unknown_action"}


# --------------------------------------------------------------------------- #
# ⑤ concurrency guard: a second run while one is in flight → 409
# --------------------------------------------------------------------------- #

def test_concurrent_run_returns_409(client, monkeypatch):
    _set_admin_token(monkeypatch)

    def _slow_stub(table, *, qps=2.0, dry_run=False):
        time.sleep(0.3)
        return {"copied": 0}

    monkeypatch.setattr("tee_replicator.worker.run_table", _slow_stub)

    results: list[int] = []

    def _fire():
        res = client.post(
            "/v1/admin/tee-replication/run",
            headers=_admin_headers(),
            json={"action": "replicate", "table": "chat_messages", "dry_run": True},
        )
        results.append(res.status_code)

    t1 = threading.Thread(target=_fire)
    t2 = threading.Thread(target=_fire)
    t1.start()
    time.sleep(0.05)  # give t1 a head start so it wins the lock
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert sorted(results) == [200, 409]
