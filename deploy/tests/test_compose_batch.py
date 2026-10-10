"""Execute the CI matrix unit's real shell steps with offline external services."""

import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = "0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F"


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]


def test_matrix_serializes_whole_units_and_propagates_failure():
    jobs = workflow()
    job = jobs["publish-prod-runner-compose"]
    assert job["strategy"]["max-parallel"] == 1
    assert job["strategy"]["fail-fast"] is True
    assert not job.get("continue-on-error", False)
    assert job["needs"] == ["deploy-prod-runner-cvm"]
    assert (
        job["steps"][0]["with"]["ref"]
        == "${{ needs.deploy-prod-runner-cvm.outputs.publisher_sha }}"
    )
    assert (
        job["strategy"]["matrix"]["cvm_id"]
        == "${{ fromJSON(needs.deploy-prod-runner-cvm.outputs.ids_json) }}"
    )
    deploy = jobs["deploy-prod-runner-cvm"]
    ids = next(s for s in deploy["steps"] if s.get("id") == "ids")
    assert "git rev-parse HEAD" in ids["run"] and "len(ids) <= 256" in ids["run"]
    assert not any(
        "publish-compose-hash.sh" in s.get("run", "") for s in deploy["steps"]
    )
    notification = jobs["notify-lark-prod-deploy"]
    assert "publish-prod-runner-compose" in notification["needs"]
    step = notification["steps"][0]
    assert (
        step["env"]["RUNNER_AUTH_STATUS"]
        == "${{ needs.publish-prod-runner-compose.result }}"
    )
    # Run the actual status calculation, stopping before notification/network.
    status_script = (
        step["run"].split("          RUN_URL=")[0]
        if "          RUN_URL=" in step["run"]
        else step["run"].split("RUN_URL=")[0]
    )
    for state in ("failure", "cancelled", "skipped"):
        result = subprocess.run(
            ["bash", "-e", "-c", status_script + '\nprintf "%s" "$STATUS"'],
            env={
                **os.environ,
                "LARK_BOT_WEBHOOK": "offline",
                "MAIN_STATUS": "success",
                "RUNNER_STATUS": "success",
                "RUNNER_AUTH_STATUS": state,
            },
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout in ("failure", "cancelled")


@pytest.mark.parametrize(
    "hashes,mode,expected",
    [
        (["c8" * 32, "d9" * 32], "ok", 2),
        (["c8" * 32, "c8" * 32], "ok", 1),
        (["c8" * 32, "d9" * 32], "unknown", 1),
        (["c8" * 32, "d9" * 32], "revert", 1),
        (["c8" * 32, "d9" * 32], "tampered_archive", 0),
    ],
)
def test_real_matrix_units_distinct_dedup_and_stop(tmp_path, hashes, mode, expected):
    jobs = workflow()
    job = jobs["publish-prod-runner-compose"]
    assert job["strategy"]["max-parallel"] == 1 and job["strategy"]["fail-fast"] is True
    units = [
        s
        for s in job["steps"]
        if s.get("env", {}).get("FEEDLING_PUBLISH_PHASE") in ("prepare", "send")
    ]
    assert len(units) == 2
    archive_step = next(s for s in job["steps"] if s.get("id") == "compose-intent")
    assert archive_step["uses"] == "actions/upload-artifact@v4"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    store = tmp_path / "archives"
    store.mkdir()
    state = tmp_path / "chain.json"
    state.write_text(
        json.dumps({"nonce": 638, "allowed": [], "nonces": [], "sends": []})
    )
    bootstrap = tmp_path / "bootstrap.py"
    bootstrap.write_text("""import json,os,runpy,sys
from pathlib import Path
n=runpy.run_path(TEST_MODULE)
p=n['p'];Base=n['Fake']
class Chain(Base):
 def __call__(self,m,a):
  path=Path(os.environ['TEST_CHAIN']);s=json.loads(path.read_text());h=os.environ['FEEDLING_COMPOSE_HASH']
  if m=='eth_getTransactionCount':return hex(s['nonce'])
  if m=='eth_call' and a[0]['data'].startswith('0x90144031'):
   return '0x'+format(int('0x'+a[0]['data'][10:] in s['allowed']),'064x')
  if m=='eth_sendRawTransaction':
   answer=super().__call__(m,a)
   key=p.target_key();intent=json.loads((Path(os.environ['FEEDLING_PUBLISH_EVIDENCE_DIR'])/key/'intent.json').read_text())
   assert intent['nonce']==s['nonce']
   s['sends'].append(h);s['nonces'].append(intent['nonce']);s['nonce']+=1
   if os.environ['TEST_MODE']=='ok':s['allowed'].append(h)
   path.write_text(json.dumps(s))
   if os.environ['TEST_MODE']=='unknown':raise TimeoutError('offline timeout')
   if os.environ['TEST_MODE']=='revert':self.mode='revert'
   return answer
  return super().__call__(m,a)
p.RPC=lambda _:Chain()
sys.exit(p.main())
""".replace("TEST_MODULE", repr(str(ROOT / "deploy/tests/test_compose_publisher.py"))))

    def exe(name, body):
        path = bindir / name
        path.write_text(body)
        path.chmod(0o700)

    exe(
        "python3",
        '#!/bin/bash\nif [[ "$1" == */compose_publisher.py ]]; then\nexec '
        + sys.executable
        + " "
        + str(bootstrap)
        + "\nfi\nexec "
        + sys.executable
        + ' "$@"\n',
    )
    exe(
        "phala",
        "#!"
        + sys.executable
        + '\nimport os,json; print(json.dumps({"compose_hash":os.environ["TEST_HASH"]}))\n',
    )
    exe(
        "gh",
        "#!" + sys.executable + """\nimport sys,json,os
from pathlib import Path
root=Path(os.environ['TEST_ARCHIVES']);url=next(a for a in sys.argv if a.startswith('repos/'))
if url.endswith('/zip'):sys.stdout.buffer.write((root/(url.split('/')[-2]+'.zip')).read_bytes())
elif url.endswith('/artifacts'):
 name=next(a[5:] for a in sys.argv if a.startswith('name=')); artifacts=[json.loads(f.read_text()) for f in root.glob('*.json')]; artifacts=[a for a in artifacts if a['name']==name];print(json.dumps({'total_count':len(artifacts),'artifacts':artifacts}))
else:print((root/(url.split('/')[-1]+'.json')).read_text())
""",
    )
    stopped = False
    prepared = []
    for index, h in enumerate(hashes):
        # Model max-parallel=1/fail-fast=true, verified against parsed workflow.
        evidence = tmp_path / f"evidence-{index}"
        output = tmp_path / f"output-{index}"
        env = {
            **os.environ,
            "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
            "PRIVATE_KEY": "01" * 32,
            "ETH_SEPOLIA_RPC_URL": "https://offline.invalid",
            "FEEDLING_APP_AUTH_CONTRACT": CONTRACT,
            "FEEDLING_CVM_ID": f"offline-{index}",
            "PHALA_CLOUD_API_KEY": "offline",
            "FEEDLING_COMPOSE_FILE": "deploy/docker-compose.phala.prod.runner.yaml",
            "FEEDLING_PUBLISH_EVIDENCE_DIR": str(evidence),
            "GITHUB_ACTIONS": "true",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_REPOSITORY": "offline/repo",
            "GITHUB_RUN_ID": "42",
            "GITHUB_OUTPUT": str(output),
            "TEST_CHAIN": str(state),
            "TEST_ARCHIVES": str(store),
            "TEST_HASH": h,
            "TEST_MODE": mode,
        }
        for step in units:
            phase = step["env"]["FEEDLING_PUBLISH_PHASE"]
            env["FEEDLING_PUBLISH_PHASE"] = phase
            # Dependencies are already installed by the clean test environment.
            script = "\n".join(
                line for line in step["run"].splitlines() if "pip install" not in line
            )
            assert script.strip() == "./deploy/publish-compose-hash.sh eth_sepolia"
            run = subprocess.run(
                ["bash", "-e", "-o", "pipefail", "-c", script],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )
            if run.returncode:
                assert phase == "send", run.stderr
                assert any(
                    reason in run.stderr
                    for reason in (
                        "TimeoutError",
                        "receipt_reverted",
                        "intent_archive_content_mismatch",
                    )
                ), run.stderr
                stopped = True
                break
            if phase == "prepare" and output.exists():
                key = output.read_text().strip().split("=", 1)[1]
                intent = evidence / key / "intent.json"
                prepared.append(json.loads(intent.read_text()))
                artifact_id = str(91 + index)
                raw = intent.read_bytes()
                if mode == "tampered_archive":
                    raw += b" "
                with zipfile.ZipFile(store / (artifact_id + ".zip"), "w") as z:
                    z.writestr(key + "/intent.json", raw)
                (store / (artifact_id + ".json")).write_text(
                    json.dumps(
                        {
                            "id": int(artifact_id),
                            "expired": False,
                            "size_in_bytes": 500,
                            "name": "compose-intent-" + key,
                            "workflow_run": {"id": 42},
                        }
                    )
                )
                env["FEEDLING_PUBLISH_ARTIFACT_ID"] = artifact_id
        if stopped:
            break
    chain = json.loads(state.read_text())
    assert len(chain["sends"]) == expected
    assert chain["nonces"] == list(range(638, 638 + expected))
    assert [i["nonce"] for i in prepared] == list(range(638, 638 + len(prepared)))
    assert stopped == (mode != "ok")
    if stopped:
        assert len(prepared) == 1  # no second intent was pre-signed
