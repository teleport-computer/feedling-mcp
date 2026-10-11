"""Offline recovery state machine, real authorization/archive readers, no real keys."""

import base64
import hashlib
import io
import json
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from eth_utils import to_checksum_address

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compose_recovery as recovery
from compose_publisher import Stop
from compose_recovery_github import GitHubRecovery
import test_compose_publisher as publisher_tests


class RecoveryRPC(publisher_tests.Fake):
    def __call__(self, method, args):
        if method == "eth_getTransactionByHash":
            return None
        if method == "eth_getTransactionReceipt" and self.sent == 0:
            return None
        return super().__call__(method, args)


class Backend:
    def __init__(self, auth, intent):
        self.auth, self.intent = auth, intent
        self.refs, self.tags = {}, {}
        self.lock = threading.Lock()
        self.journal_extra = {}
        self.history_extra = False
        self.live_matches = True
        self.auth_archive_fails = False
        self.claim_calls = 0
        self.active_runs = []
        self.incomplete_runs = False
        self.head = auth["execution_sha"]

    def api(self, path, *, body=None, binary=False, missing=False):
        a = self.auth
        key = GitHubRecovery.original_key(a)
        if path == "git/ref/heads/test":
            return {"object": {"sha": self.head}}
        if path.startswith("actions/runs?"):
            return {
                "total_count": len(self.active_runs) + int(self.incomplete_runs),
                "workflow_runs": self.active_runs,
            }
        if path.startswith("actions/runs/"):
            return {
                "head_sha": a["source_sha"],
                "head_branch": "test",
                "path": ".github/workflows/ci.yml",
                "run_attempt": 1,
                "status": "completed",
                "conclusion": "failure",
            }
        if path.startswith("actions/artifacts?"):
            rows = [{"id": a["intent_artifact"]}]
            if self.history_extra:
                rows.append({"id": 555})
            return {"total_count": len(rows), "artifacts": rows}
        if path.startswith("actions/artifacts/"):
            artifact_id = int(path.split("/")[2])
            is_intent = artifact_id == a["intent_artifact"]
            if not binary:
                return {
                    "expired": False,
                    "name": (
                        "compose-intent-" + key
                        if is_intent
                        else f'compose-publish-{a["source_run"]}-deploy-test-runner-cvm-1'
                    ),
                    "workflow_run": {"id": a["source_run"]},
                    "size_in_bytes": 99,
                }
            entries = {key + "/intent.json": self.intent}
            if not is_intent:
                entries[key + "/failure.json"] = json.dumps(
                    {
                        "result": "STOP",
                        "reason": "frozen_limits_insufficient",
                        "retry_authorized": False,
                    }
                ).encode()
                entries.update(
                    {key + "/" + k: v for k, v in self.journal_extra.items()}
                )
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, raw in entries.items():
                    archive.writestr(name, raw)
            return buffer.getvalue()
        if path == "git/tags":
            if self.auth_archive_fails:
                raise Stop("offline_archive_unavailable")
            oid = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()
            self.tags[oid] = body
            return {"sha": oid}
        if path == "git/refs":
            with self.lock:
                if body["ref"] in self.refs:
                    raise Stop("recovery_github_operation_failed")
                self.refs[body["ref"]] = body["sha"]
                if "claim-" in body["ref"]:
                    self.claim_calls += 1
            return {"ref": body["ref"]}
        if path.startswith("git/ref/"):
            oid = self.refs.get("refs/" + path[len("git/ref/") :])
            if oid is None:
                if missing:
                    return None
                raise Stop("offline_missing_ref")
            return {"object": {"sha": oid}}
        raise AssertionError(path)


class Store(GitHubRecovery):
    def __init__(self, path, backend):
        super().__init__(path)
        self.backend = backend

    def api(self, *args, **kwargs):
        return self.backend.api(*args, **kwargs)

    def verify_live_target(self, frozen):
        if not self.backend.live_matches:
            raise Stop("recovery_live_compose_changed")


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.setattr(
        publisher_tests, "CONTRACT", to_checksum_address(recovery.TEST_CONTRACT)
    )
    monkeypatch.setenv("GITHUB_RUN_ID", "1001")
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    path = tmp_path / "original"
    # Real publisher prepare with synthetic test signer; no wire/network.
    publisher_tests.run(RecoveryRPC(), path, phase="prepare")
    f = json.loads((path / "intent.json").read_text())
    f["yaml_url"] = (
        "https://github.com/teleport-computer/feedling-mcp/raw/"
        + f["commit"]
        + "/deploy/docker-compose.phala.runner.yaml"
    )
    # Build original via real prepare using the exact supported immutable URL.
    path2 = tmp_path / "supported-original"
    publisher_tests.p.publish(
        RecoveryRPC(),
        publisher_tests.SIGNER,
        chain="eth_sepolia",
        contract=publisher_tests.CONTRACT,
        compose_hash=f["compose_hash"],
        commit=f["commit"],
        yaml_url=f["yaml_url"],
        journal=publisher_tests.p.Journal(path2),
        phase="prepare",
    )
    raw = (path2 / "intent.json").read_bytes()
    f = json.loads(raw)
    auth = {
        "version": 1,
        "environment": "test",
        "recovery_id": "10000000-0000-0000-0000-000000000001",
        "issued_at": 100,
        "expires_at": 600,
        "source_run": 123,
        "source_sha": "a" * 40,
        "execution_sha": "b" * 40,
        "intent_artifact": 1,
        "evidence_artifact": 2,
        "intent_sha256": hashlib.sha256(raw).hexdigest(),
        "intent": f,
    }
    return auth, raw, Backend(auth, raw)


def execute(
    tmp_path,
    scenario,
    *,
    rpc=None,
    auth=None,
    digest=None,
    now=lambda: 200,
    signer=None,
):
    original, raw, backend = scenario
    auth = auth or original
    encoded = json.dumps(auth).encode()
    rpc = rpc or RecoveryRPC()
    calls = []

    def factory():
        calls.append(True)
        return signer or publisher_tests.SIGNER

    result = recovery.recover(
        authorization=encoded,
        expected_digest=digest or hashlib.sha256(encoded).hexdigest(),
        store=Store(tmp_path, backend),
        rpc=rpc,
        signer_factory=factory,
        now=now,
        sleep=lambda _: None,
    )
    return result, calls


def test_qualified_unsent_reconstructs_same_transaction_and_claims_globally(
    tmp_path, scenario
):
    rpc = RecoveryRPC()
    result, calls = execute(tmp_path / "run", scenario, rpc=rpc)
    assert result == {
        "result": "AUTHORIZED",
        "transaction_hash": scenario[0]["intent"]["transaction_hash"],
        "sends": 1,
    }
    assert rpc.sent == 1 and calls == [True] and scenario[2].claim_calls == 1
    evidence = json.loads(next(iter(scenario[2].tags.values()))["message"])
    assert base64.b64decode(evidence["intent_base64"]) == scenario[1]
    assert (
        base64.b64decode(evidence["authorization_base64"])
        == json.dumps(scenario[0]).encode()
    )
    with pytest.raises(Stop, match="already_claimed"):
        execute(tmp_path / "different-directory", scenario)


@pytest.mark.parametrize(
    "entry", ["claim.json", "broadcast-started.json", "receipt.json", "result.json"]
)
def test_any_prior_attempt_journal_blocks_before_sign_or_send(
    tmp_path, scenario, entry
):
    scenario[2].journal_extra[entry] = b"{}"
    rpc = RecoveryRPC()
    with pytest.raises(Stop, match="original_broadcast_or_unknown"):
        execute(tmp_path / "run", scenario, rpc=rpc)
    assert rpc.sent == 0 and scenario[2].claim_calls == 0


@pytest.mark.parametrize(
    "method", ["eth_getTransactionReceipt", "eth_getTransactionByHash"]
)
def test_existing_chain_transaction_is_not_permission(tmp_path, scenario, method):
    fake = RecoveryRPC()

    def rpc(m, a):
        return {} if m == method else fake(m, a)

    with pytest.raises(Stop, match="exists"):
        execute(tmp_path / "run", scenario, rpc=rpc)
    assert fake.sent == 0 and scenario[2].claim_calls == 0


def test_pending_nonce_conflict_stops(tmp_path, scenario):
    f = RecoveryRPC("pending")
    with pytest.raises(Stop, match="nonce_conflict"):
        execute(tmp_path / "run", scenario, rpc=f)
    assert f.sent == 0


def test_already_allowed_never_loads_signer_or_claims(tmp_path, scenario):
    result, calls = execute(tmp_path / "run", scenario, rpc=RecoveryRPC("already"))
    assert result == {"result": "ALREADY_AUTHORIZED", "sends": 0}
    assert calls == [] and scenario[2].claim_calls == 0


@pytest.mark.parametrize("mode", ["unknown", "timeout"])
def test_broadcast_unknown_or_receipt_timeout_never_resends(tmp_path, scenario, mode):
    fake = RecoveryRPC("unknown" if mode == "unknown" else "ok")

    def rpc(m, a):
        if m == "eth_getTransactionByHash":
            return None
        if m == "eth_getTransactionReceipt" and mode == "timeout":
            return None
        return fake(m, a)

    with pytest.raises((Stop, TimeoutError)):
        execute(tmp_path / "run", scenario, rpc=rpc)
    assert fake.sent == 1
    with pytest.raises(Stop, match="already_claimed"):
        execute(tmp_path / "new-dir", scenario)
    assert fake.sent == 1


def test_concurrent_recoveries_only_one_can_sign_and_send(tmp_path, scenario):
    barrier = threading.Barrier(2)
    backend = scenario[2]
    original = backend.api

    def api(path, **kw):
        if path.startswith("git/ref/tags/compose-recovery-claim-") and kw.get(
            "missing"
        ):
            barrier.wait(timeout=5)
        return original(path, **kw)

    backend.api = api
    fakes = [RecoveryRPC(), RecoveryRPC()]

    def invoke(i):
        auth = dict(scenario[0], recovery_id=f"10000000-0000-0000-0000-{i+2:012d}")
        try:
            return execute(tmp_path / str(i), scenario, rpc=fakes[i], auth=auth)[0]
        except Stop:
            return {"result": "STOP"}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(invoke, [0, 1]))
    assert sum(f.sent for f in fakes) == 1 and backend.claim_calls == 1
    assert sorted(x["result"] for x in results) == ["AUTHORIZED", "STOP"]


@pytest.mark.parametrize(
    "change",
    ["expiry", "digest", "intent", "price", "gas", "live", "history", "archive"],
)
def test_invalid_or_drifted_preconditions_never_broadcast(tmp_path, scenario, change):
    auth = dict(scenario[0])
    kw = {}
    fake = RecoveryRPC()
    if change == "expiry":
        kw["now"] = lambda: 601
    if change == "digest":
        kw["digest"] = "0" * 64
    if change == "intent":
        auth["intent"] = dict(auth["intent"], nonce=999)
    if change == "live":
        scenario[2].live_matches = False
    if change == "history":
        scenario[2].history_extra = True
    if change == "archive":
        scenario[2].auth_archive_fails = True

    def rpc(m, a):
        if m == "eth_getTransactionByHash":
            return None
        if change == "price" and m == "eth_gasPrice":
            return hex(2000000)
        if change == "gas" and m == "eth_estimateGas":
            return hex(1900000)
        return fake(m, a)

    with pytest.raises(Stop):
        execute(tmp_path / "run", scenario, rpc=rpc, auth=auth, **kw)
    assert fake.sent == 0 and scenario[2].claim_calls == 0


def test_expiration_after_remote_broadcast_marker_stops_without_releasing_claim(
    tmp_path, scenario
):
    original = scenario[2].api
    clock = [200]

    def api(path, **kw):
        result = original(path, **kw)
        if path == "git/refs" and "broadcast-" in kw["body"]["ref"]:
            clock[0] = 601
        return result

    scenario[2].api = api
    rpc = RecoveryRPC()
    with pytest.raises(Stop, match="expired"):
        execute(tmp_path / "first", scenario, rpc=rpc, now=lambda: clock[0])
    assert rpc.sent == 0 and scenario[2].claim_calls == 1
    with pytest.raises(Stop, match="already_claimed"):
        execute(tmp_path / "fresh-directory", scenario)


def test_hash_mismatch_after_claim_never_broadcasts(tmp_path, scenario):
    class WrongSigner:
        address = publisher_tests.SIGNER.address

        def sign_transaction(self, tx):
            return publisher_tests.SIGNER.sign_transaction(
                dict(tx, nonce=tx["nonce"] + 1)
            )

    rpc = RecoveryRPC()
    with pytest.raises(Stop, match="signed_hash_mismatch"):
        execute(tmp_path / "run", scenario, rpc=rpc, signer=WrongSigner())
    assert rpc.sent == 0 and scenario[2].claim_calls == 1


@pytest.mark.parametrize(
    "status,rc,absent",
    [(404, 1, True), (200, 0, False), (403, 1, None), (500, 1, None)],
)
def test_actual_github_reader_distinguishes_missing_from_unknown(
    tmp_path, monkeypatch, status, rc, absent
):
    from types import SimpleNamespace
    import compose_recovery_github as adapter

    def fake(*args, **kwargs):
        return SimpleNamespace(
            returncode=rc,
            stdout=f'HTTP/2.0 {status} status\r\nContent-Type: application/json\r\n\r\n{{"object":{{"sha":"abc"}}}}'.encode(),
        )

    monkeypatch.setattr(adapter.subprocess, "run", fake)
    store = GitHubRecovery(tmp_path / "dir")
    if absent is None:
        with pytest.raises(Stop, match="read_unknown"):
            store.claim_exists("test")
    else:
        assert store.claim_exists("test") is (not absent)


def test_cli_rejects_non_test_or_rerun_before_private_key_access(
    tmp_path, monkeypatch, capsys
):
    import reconcile_compose as entry

    monkeypatch.setenv("RECOVERY_EVIDENCE_DIR", str(tmp_path / "cli"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "teleport-computer/feedling-mcp")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_WORKFLOW", "CI")
    assert entry.main() == 1
    assert "recovery_reviewed_test_dispatch_required" in capsys.readouterr().out
    monkeypatch.setenv("GITHUB_REF", "refs/heads/test")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    assert entry.main() == 1
    assert "recovery_reviewed_test_dispatch_required" in capsys.readouterr().out


def test_actual_workflow_reader_binds_test_operation_lock_and_artifacts():
    import yaml

    doc = yaml.load(
        (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert (
        "reconcile-test-compose"
        in doc["on"]["workflow_dispatch"]["inputs"]["operation"]["options"]
    )
    job = doc["jobs"]["reconcile-test-compose"]
    assert "github.event_name == 'workflow_dispatch'" in job["if"]
    assert "github.ref == 'refs/heads/test'" in job["if"]
    assert "inputs.operation == 'reconcile-test-compose'" in job["if"]
    assert job["concurrency"] == doc["jobs"]["deploy-test-runner-cvm"]["concurrency"]
    assert job["permissions"] == {"actions": "read", "contents": "write"}
    execute_step = next(
        s for s in job["steps"] if s.get("run") == "python deploy/reconcile_compose.py"
    )
    assert (
        execute_step["env"]["RECOVERY_AUTHORIZATION"]
        == "${{ inputs.recovery_authorization }}"
    )
    assert (
        execute_step["env"]["RECOVERY_AUTHORIZATION_SHA256"]
        == "${{ inputs.recovery_authorization_sha256 }}"
    )
    artifact = job["steps"][-1]
    assert (
        artifact["if"] == "always()"
        and artifact["with"]["if-no-files-found"] == "error"
    )


def test_cli_stale_execution_sha_stops_before_store_and_signer(
    tmp_path, monkeypatch, capsys
):
    import reconcile_compose as entry

    for k, v in {
        "RECOVERY_EVIDENCE_DIR": str(tmp_path / "cli"),
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "teleport-computer/feedling-mcp",
        "GITHUB_REF": "refs/heads/test",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW": "CI",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_SHA": "b" * 40,
        "RECOVERY_OPERATION": "reconcile-test-compose",
        "RECOVERY_HOLD": "true",
    }.items():
        monkeypatch.setenv(k, v)
    raw = json.dumps({"execution_sha": "a" * 40})
    monkeypatch.setenv("RECOVERY_AUTHORIZATION", raw)
    monkeypatch.setenv(
        "RECOVERY_AUTHORIZATION_SHA256", hashlib.sha256(raw.encode()).hexdigest()
    )
    assert entry.main() == 1
    assert "recovery_execution_sha_changed" in capsys.readouterr().out


def test_authorization_mutation_after_digest_is_rejected_before_archive_read(
    tmp_path, scenario
):
    auth = dict(scenario[0])
    raw = json.dumps(auth).encode()
    auth["expires_at"] += 1
    called = []
    backend = scenario[2]
    original = backend.api

    def spy(*args, **kwargs):
        called.append(True)
        return original(*args, **kwargs)

    backend.api = spy
    with pytest.raises(Stop, match="authorization_digest"):
        execute(
            tmp_path / "dir",
            scenario,
            auth=auth,
            digest=hashlib.sha256(raw).hexdigest(),
        )
    assert not called


def test_recovery_hold_is_first_fail_closed_step_for_both_test_deploy_jobs():
    import yaml

    doc = yaml.load(
        (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    for key in ("deploy-test-cvm", "deploy-test-runner-cvm"):
        guard = doc["jobs"][key]["steps"][0]
        assert guard["if"] == "vars.TEST_COMPOSE_RECOVERY_HOLD == 'true'"
        assert "exit 1" in guard["run"] and "::error::" in guard["run"]
        assert "continue-on-error" not in guard
    recovery_job = doc["jobs"]["reconcile-test-compose"]
    assert "TEST_COMPOSE_RECOVERY_HOLD" not in recovery_job["if"]


@pytest.mark.parametrize("change", ["active", "incomplete", "head"])
def test_other_workflow_or_unknown_window_stops_before_signing(
    tmp_path, scenario, change
):
    backend = scenario[2]
    if change == "active":
        backend.active_runs = [{"id": 999, "head_branch": "pre", "status": "queued"}]
    elif change == "incomplete":
        backend.incomplete_runs = True
    else:
        backend.head = "c" * 40
    calls = []
    raw = json.dumps(scenario[0]).encode()
    rpc = RecoveryRPC()
    with pytest.raises(
        Stop,
        match="recovery_(concurrent_workflow|workflow_history_unknown|test_head_changed)",
    ):
        recovery.recover(
            authorization=raw,
            expected_digest=hashlib.sha256(raw).hexdigest(),
            store=Store(tmp_path / "blocked", backend),
            rpc=rpc,
            signer_factory=lambda: calls.append(True),
            now=lambda: 200,
        )
    assert not calls and rpc.sent == 0 and not backend.refs


def test_new_workflow_after_marker_stops_without_second_attempt(tmp_path, scenario):
    backend = scenario[2]
    original_api = backend.api

    def api(path, **kwargs):
        result = original_api(path, **kwargs)
        if path == "git/refs" and "broadcast-" in kwargs["body"]["ref"]:
            backend.active_runs = [{"id": 777}]
        return result

    backend.api = api
    rpc = RecoveryRPC()
    with pytest.raises(Stop, match="concurrent_workflow"):
        execute(tmp_path / "blocked", scenario, rpc=rpc)
    assert rpc.sent == 0 and backend.claim_calls == 1


@pytest.mark.parametrize(
    "operation,hold",
    [
        (None, "true"),
        ("validate", "true"),
        ("reconcile-test-compose", None),
        ("reconcile-test-compose", "false"),
    ],
)
def test_cli_requires_explicit_operation_and_hold(
    tmp_path, monkeypatch, capsys, operation, hold
):
    import reconcile_compose as entry

    for key, value in {
        "RECOVERY_EVIDENCE_DIR": str(tmp_path / "cli"),
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "teleport-computer/feedling-mcp",
        "GITHUB_REF": "refs/heads/test",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_WORKFLOW": "CI",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
    }.items():
        monkeypatch.setenv(key, value)
    for key, value in {"RECOVERY_OPERATION": operation, "RECOVERY_HOLD": hold}.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    assert entry.main() == 1
    assert "recovery_reviewed_test_dispatch_required" in capsys.readouterr().out


def test_dispatch_cannot_enter_normal_deploy_and_skips_unrelated_suites():
    import yaml

    jobs = yaml.load(
        (Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml").read_text(),
        Loader=yaml.BaseLoader,
    )["jobs"]
    for name in (
        "deploy-cvm",
        "deploy-test-cvm",
        "deploy-test-runner-cvm",
        "deploy-pre-cvm",
        "deploy-pre-runner-cvm",
        "deploy-prod-runner-cvm",
    ):
        assert "github.event_name == 'push'" in jobs[name]["if"]
    for name in (
        "forge-test",
        "python-tests",
        "runtime-v2-coverage",
        "docker-build",
        "docker-build-worker-deps",
        "lint",
        "dcap-python",
        "ci-execution-evidence-report",
    ):
        assert "inputs.operation != 'reconcile-test-compose'" in jobs[name]["if"]
    execute = next(
        s
        for s in jobs["reconcile-test-compose"]["steps"]
        if s.get("run") == "python deploy/reconcile_compose.py"
    )
    assert execute["env"]["RECOVERY_OPERATION"] == "${{ inputs.operation }}"
    assert execute["env"]["RECOVERY_HOLD"] == "${{ vars.TEST_COMPOSE_RECOVERY_HOLD }}"
