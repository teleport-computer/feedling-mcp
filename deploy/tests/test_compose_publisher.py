"""Offline broadcast boundary tests; no real keys, RPCs, chain writes or model calls."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import os
import pytest
from eth_account import Account
from eth_utils import keccak

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "publisher", ROOT / "deploy/compose_publisher.py"
)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)
SIGNER = Account.from_key(bytes.fromhex("01" * 32))
CONTRACT = "0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F"
HASH = "0x" + "c8" * 32


class Fake:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.sent = 0
        self.nonces = 0
        self.estimates = 0
        self.prices = 0

    def __call__(self, m, a):
        if m == "eth_chainId":
            return hex(1 if self.mode == "chain" else 11155111)
        if m == "eth_call":
            data = a[0]["data"]
            if data == "0x8da5cb5b":
                return (
                    "0x"
                    + "0" * 24
                    + ("0" * 40 if self.mode == "owner" else SIGNER.address[2:].lower())
                )
            if data.startswith("0x90144031"):
                return "0x" + format(
                    int(
                        self.mode == "already"
                        or (self.sent > 0 and self.mode != "readback")
                    ),
                    "064x",
                )
            if self.mode == "simulation":
                raise p.Stop("eth_call_rpc_-32000")
            return "0x"
        if m == "eth_getTransactionCount":
            self.nonces += 1
            return hex(
                639
                if self.mode == "pending"
                and self.nonces == 2
                or self.mode == "nonce"
                and self.nonces >= 3
                else 638
            )
        if m == "eth_estimateGas":
            self.estimates += 1
            return hex(
                3000000
                if self.mode == "gas_cap"
                else (
                    1900000
                    if self.mode == "estimate_change" and self.estimates > 1
                    else 1829803
                )
            )
        if m == "eth_gasPrice":
            self.prices += 1
            return hex(
                100000000
                if self.mode == "fee_cap"
                else (
                    2000000
                    if self.mode == "fee_change" and self.prices > 1
                    else 1000000
                )
            )
        if m == "eth_sendRawTransaction":
            self.sent += 1
            self.hash = "0x" + keccak(bytes.fromhex(a[0][2:])).hex()
            if self.mode == "unknown":
                raise TimeoutError("FAKE_SECRET_DO_NOT_PRINT")
            return "0x" + "0" * 64 if self.mode == "hash" else self.hash
        if m == "eth_getTransactionReceipt":
            if self.mode == "timeout":
                return None
            return {
                "transactionHash": self.hash,
                "status": "0x0" if self.mode in ("revert", "oog") else "0x1",
                "from": SIGNER.address,
                "to": CONTRACT,
                "gasUsed": hex(2195764 if self.mode == "oog" else 1814237),
            }
        raise AssertionError(m)


def run(f, tmp_path, attempt="1", signer=SIGNER, **kwargs):
    return p.publish(
        f,
        signer,
        chain="eth_sepolia",
        contract=CONTRACT,
        compose_hash=HASH,
        commit="a" * 40,
        yaml_url="https://github.com/example/repo/raw/"
        + ("a" * 40)
        + "/deploy/compose.yaml",
        journal=p.Journal(tmp_path),
        sleep=lambda _: None,
        run_attempt=attempt,
        **kwargs
    )


def test_incident_gas_estimate_prevents_original_exhaustion(tmp_path):
    f = Fake()
    assert run(f, tmp_path)["result"] == "AUTHORIZED"
    i = json.loads((tmp_path / "intent.json").read_text())
    assert i["gas"] == 2195764 > i["estimate"] == 1829803 > 1365297
    assert i["gas_price"] == 3000000 and f.sent == 1
    assert (tmp_path / "receipt.json").exists() and (tmp_path / "result.json").exists()
    assert not any("private" in x.lower() or "raw_transaction" in x.lower() for x in i)


@pytest.mark.parametrize(
    "mode",
    [
        "chain",
        "owner",
        "pending",
        "nonce",
        "simulation",
        "gas_cap",
        "fee_cap",
        "estimate_change",
        "fee_change",
    ],
)
def test_pre_send_failures_never_sign_or_send(tmp_path, mode):
    f = Fake(mode)

    class MustNotSign:
        address = SIGNER.address

        def sign_transaction(self, tx):
            raise AssertionError("signed before all prechecks passed")

    with pytest.raises(p.Stop):
        run(f, tmp_path, signer=MustNotSign())
    assert f.sent == 0 and not (tmp_path / "intent.json").exists()


@pytest.mark.parametrize(
    "mode", ["unknown", "hash", "timeout", "revert", "oog", "readback"]
)
def test_post_send_failure_never_resends_and_keeps_intent(tmp_path, mode):
    f = Fake(mode)
    with pytest.raises((p.Stop, TimeoutError)):
        run(f, tmp_path)
    assert f.sent == 1 and (tmp_path / "intent.json").exists()
    with pytest.raises(p.Stop, match="prior_attempt"):
        run(Fake(), tmp_path)


def test_already_authorized_zero_send(tmp_path):
    f = Fake("already")
    assert run(f, tmp_path)["sends"] == 0 and f.sent == 0


def test_rerun_stops_before_send(tmp_path):
    f = Fake()
    with pytest.raises(p.Stop, match="rerun"):
        run(f, tmp_path, "2")
    assert f.sent == 0


def test_intent_journal_failure_prevents_broadcast(tmp_path, monkeypatch):
    f = Fake()
    original = p.Journal.write

    def fail(self, name, value):
        if name == "intent":
            raise OSError("disk full")
        original(self, name, value)

    monkeypatch.setattr(p.Journal, "write", fail)
    with pytest.raises(OSError):
        run(f, tmp_path)
    assert f.sent == 0 and (tmp_path / "claim.json").exists()


def test_gas_cap_and_ceiling():
    assert p.gas_limit(1) == 2 and p.gas_limit(2500000) == 3000000
    with pytest.raises(p.Stop):
        p.gas_limit(2500001)


def test_workflow_no_outer_publish_retries():
    import yaml

    w = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    count = 0
    for job in w["jobs"].values():
        steps = job.get("steps", [])
        for n, step in enumerate(steps):
            if "./deploy/publish-compose-hash.sh" in step.get("run", ""):
                count += 1
                assert (
                    "for attempt" not in step["run"] and "sleep 20" not in step["run"]
                )
                assert job["permissions"]["actions"] == "read"
                if step["env"]["FEEDLING_PUBLISH_PHASE"] == "prepare":
                    assert "-r deploy/publisher-requirements.txt" in step["run"]
                    assert steps[n + 1]["uses"] == "actions/upload-artifact@v4"
                    assert steps[n + 1]["with"]["if-no-files-found"] == "error"
                    assert steps[n + 2]["env"]["FEEDLING_PUBLISH_PHASE"] == "send"
                else:
                    assert "pip install" not in step["run"]
                    assert steps[n - 1]["id"] == "compose-intent"
                assert any(
                    s.get("name") == "Preserve compose publisher evidence"
                    and s.get("if") == "always()"
                    for s in steps
                )
    assert count == 12
    assert any(
        "pytest -q deploy/tests" in s.get("run", "")
        for s in w["jobs"]["compose-publisher-tests"]["steps"]
    )


def test_prepare_archive_send_exact_intent(tmp_path):
    f = Fake()
    assert run(f, tmp_path, phase="prepare")["sends"] == 0
    original = (tmp_path / "intent.json").read_bytes()
    seen = []

    def archive():
        assert f.sent == 0
        seen.append(original)

    assert run(f, tmp_path, phase="send", archived=archive)["sends"] == 1
    assert seen == [original] and (tmp_path / "intent.json").read_bytes() == original


def test_missing_archive_blocks_send(tmp_path):
    f = Fake()
    run(f, tmp_path, phase="prepare")

    def blocked():
        raise p.Stop("intent_not_archived")

    with pytest.raises(p.Stop, match="intent_not_archived"):
        run(f, tmp_path, phase="send", archived=blocked)
    assert f.sent == 0 and not (tmp_path / "claim.json").exists()


def test_prior_ci_intent_blocks_fresh_directory(tmp_path):
    f = Fake()

    def prior():
        raise p.Stop("prior_ci_intent_requires_reconciliation")

    with pytest.raises(p.Stop, match="prior_ci_intent"):
        run(f, tmp_path, phase="prepare", prior=prior)
    assert f.sent == 0 and not (tmp_path / "intent.json").exists()


def test_frozen_nonce_drift_stops(tmp_path):
    run(Fake(), tmp_path, phase="prepare")
    f = Fake()
    base = f.__call__

    def changed(m, a):
        return hex(639) if m == "eth_getTransactionCount" else base(m, a)

    with pytest.raises(p.Stop, match="frozen_nonce_changed"):
        run(changed, tmp_path, phase="send")
    assert f.sent == 0


def test_two_wei_fee_change_fits_bounded_headroom(tmp_path):
    run(Fake(), tmp_path, phase="prepare")
    f = Fake()
    base = f.__call__

    def changed(m, a):
        return hex(1000001) if m == "eth_gasPrice" else base(m, a)

    assert run(changed, tmp_path, phase="send")["sends"] == 1
    assert json.loads((tmp_path / "intent.json").read_text())["gas_price"] == 3000000


def test_ci_direct_publish_fails_before_key_or_rpc(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("FEEDLING_PUBLISH_PHASE", raising=False)
    assert p.main() == 1
    assert "ci_archive_phase_required" in capsys.readouterr().err


def test_historical_failed_calldata_receives_estimated_headroom(tmp_path):
    from eth_abi import decode

    fixture = json.loads(
        (ROOT / "deploy/tests/fixtures/t800-failed-transactions.json").read_text()
    )
    for n, entry in enumerate(fixture["transactions"]):
        tx = entry["tx"]
        raw = bytes.fromhex(tx["input"][2:])
        assert raw[:4] == keccak(text="addComposeHash(bytes32,string,string)")[:4]
        compose, commit, url = decode(["bytes32", "string", "string"], raw[4:])
        f = Fake()
        j = p.Journal(tmp_path / str(n))
        p.publish(
            f,
            SIGNER,
            chain="eth_sepolia",
            contract=CONTRACT,
            compose_hash="0x" + compose.hex(),
            commit=commit,
            yaml_url=url,
            journal=j,
            sleep=lambda _: None,
        )
        intent = json.loads((j.path / "intent.json").read_text())
        assert (
            intent["calldata_sha256"] == __import__("hashlib").sha256(raw).hexdigest()
        )
        assert intent["gas"] > intent["estimate"] > int(tx["gas"], 16)


def test_real_shell_prepare_archive_send_offline(tmp_path):
    """Run the real shell and CLI stages; replace only external RPC/Phala/gh.
    No real credentials or endpoints are provided. Actual signing uses a test key.
    """
    import zipfile
    import yaml

    bindir = tmp_path / "bin"
    bindir.mkdir()
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_text(
        "import runpy,sys\nn=runpy.run_path("
        + repr(str(Path(__file__)))
        + ')\np=n["p"];p.RPC=lambda _: n["Fake"]()\nsys.exit(p.main())\n'
    )

    def exe(name, text):
        path = bindir / name
        path.write_text(text)
        path.chmod(0o700)

    exe(
        "python3",
        '#!/bin/bash\nif [[ "$1" == */compose_publisher.py ]]; then\n exec '
        + sys.executable
        + " "
        + str(bootstrap)
        + "\nfi\nexec "
        + sys.executable
        + ' "$@"\n',
    )
    exe("phala", '#!/bin/sh\nprintf \'{"compose_hash":"' + HASH[2:] + "\"}\\n'\n")
    exe(
        "gh",
        "#!"
        + sys.executable
        + '\nimport json,sys,os\nu=next(a for a in sys.argv if a.startswith("repos/"))\nif u.endswith("/zip"):sys.stdout.buffer.write(open(os.environ["TEST_ARCHIVE"],"rb").read())\nelif u.endswith("/91"):print(json.dumps({"expired":False,"size_in_bytes":500,"name":"compose-intent-"+os.environ["TEST_TARGET"],"workflow_run":{"id":42}}))\nelse:print(json.dumps({"total_count":0,"artifacts":[]}))\n',
    )
    evidence = tmp_path / "evidence"
    key = "eth_sepolia-" + CONTRACT.lower() + "-" + HASH
    env = {
        **os.environ,
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "PRIVATE_KEY": "01" * 32,
        "ETH_SEPOLIA_RPC_URL": "https://offline.invalid",
        "FEEDLING_APP_AUTH_CONTRACT": CONTRACT,
        "FEEDLING_CVM_ID": "offline-cvm",
        "PHALA_CLOUD_API_KEY": "offline-only",
        "FEEDLING_COMPOSE_FILE": "deploy/docker-compose.phala.yaml",
        "FEEDLING_PUBLISH_EVIDENCE_DIR": str(evidence),
        "FEEDLING_PUBLISH_PHASE": "prepare",
        "GITHUB_ACTIONS": "true",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_REPOSITORY": "offline/repo",
        "GITHUB_RUN_ID": "42",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "TEST_TARGET": key,
        "TEST_ARCHIVE": str(tmp_path / "archive.zip"),
    }
    command = ["bash", "deploy/publish-compose-hash.sh", "eth_sepolia"]
    first = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert (
        "PREPARED_NOT_SENT" in first.stdout
        and not (evidence / key / "claim.json").exists()
    )
    intent = (evidence / key / "intent.json").read_bytes()
    with zipfile.ZipFile(tmp_path / "archive.zip", "w") as z:
        z.writestr(key + "/intent.json", intent)
    env.update(FEEDLING_PUBLISH_PHASE="send", FEEDLING_PUBLISH_ARTIFACT_ID="91")
    sent = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert sent.returncode == 0, sent.stderr
    assert '"result": "AUTHORIZED"' in sent.stdout
    assert (evidence / key / "intent.json").read_bytes() == intent
    again = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True)
    assert (
        again.returncode == 1
        and "prior_attempt_requires_reconciliation" in again.stderr
    )
    assert (
        "01" * 32
        not in first.stdout + first.stderr + sent.stdout + sent.stderr + again.stderr
    )


def test_archive_contents_must_match_local_intent(tmp_path, monkeypatch):
    import io, zipfile

    key = "eth_sepolia-" + CONTRACT.lower() + "-" + HASH
    for k, v in {
        "FEEDLING_PUBLISH_CHAIN": "eth_sepolia",
        "FEEDLING_APP_AUTH_CONTRACT": CONTRACT,
        "FEEDLING_COMPOSE_HASH": HASH,
        "FEEDLING_PUBLISH_EVIDENCE_DIR": str(tmp_path),
        "FEEDLING_PUBLISH_ARTIFACT_ID": "91",
        "GITHUB_REPOSITORY": "offline/repo",
        "GITHUB_RUN_ID": "42",
    }.items():
        monkeypatch.setenv(k, v)
    directory = tmp_path / key
    directory.mkdir()
    (directory / "intent.json").write_text("original")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr(key + "/intent.json", "changed")

    def fake(args, **kwargs):
        if args[-1].endswith("/zip"):
            return subprocess.CompletedProcess(args, 0, buffer.getvalue())
        return subprocess.CompletedProcess(
            args,
            0,
            json.dumps(
                {
                    "expired": False,
                    "size_in_bytes": 100,
                    "name": "compose-intent-" + key,
                    "workflow_run": {"id": 42},
                }
            ),
        )

    monkeypatch.setattr(p.subprocess, "run", fake)
    with pytest.raises(p.Stop, match="intent_archive_content_mismatch"):
        p.archived_in_ci()


@pytest.mark.parametrize(
    "history",
    [
        {"total_count": 1, "artifacts": [{"workflow_run": {"id": 41}}]},
        {"total_count": 101, "artifacts": []},
    ],
)
def test_prior_archive_api_refuses_unreconciled_or_incomplete_history(
    monkeypatch, history
):
    for k, v in {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "offline/repo",
        "GITHUB_RUN_ID": "42",
        "FEEDLING_PUBLISH_CHAIN": "eth_sepolia",
        "FEEDLING_APP_AUTH_CONTRACT": CONTRACT,
        "FEEDLING_COMPOSE_HASH": HASH,
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(
        p.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, json.dumps(history)),
    )
    with pytest.raises(p.Stop, match="prior_"):
        p.prior_in_ci()


def test_two_senders_share_one_exclusive_claim(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    run(Fake(), tmp_path, phase="prepare")
    barrier = Barrier(2)
    senders = [Fake(), Fake()]

    def send(f):
        def archived():
            barrier.wait(timeout=5)

        try:
            return run(f, tmp_path, phase="send", archived=archived)
        except (FileExistsError, p.Stop):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, senders))
    assert sum(f.sent for f in senders) == 1
    assert sum(r is not None for r in results) == 1


def test_make_add_hash_routes_to_safe_publisher_without_key_argument():
    r = subprocess.run(
        [
            "make",
            "-n",
            "-C",
            "contracts",
            "add-hash",
            "CHAIN=eth_sepolia",
            "RPC_URL=https://offline.invalid",
            "PRIVATE_KEY=DO_NOT_EXPOSE",
            "COMPOSE_HASH=" + HASH,
            "COMMIT=" + "a" * 40,
            "YAML_URL=https://example.invalid/compose",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "python3 ../deploy/compose_publisher.py" in r.stdout
    assert "DO_NOT_EXPOSE" not in r.stdout and "--broadcast" not in r.stdout
