"""Incident T800 only. Secrets remain in Actions; root dispatches each reviewed stage."""
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import yaml
from eth_abi import encode
from eth_utils import keccak

import recover as recovery

REPO = 'teleport-computer/feedling-mcp'
RECONCILED_RUN = 38077149919
RECONCILED_CODE = 'f0d84a4634480fac03a9a6c60049f8e5cad4d694'
RECONCILED_ARTIFACT = 11678942435
RECONCILED_DIGEST = 'sha256:669d80b8b83ace3f9d9608541d8d9b61774b02cf3d35bea595e0b4fb88272a73'
NONCE = 638  # Three failed transactions consumed 635..637. Drift requires new review.
RUNNER = '130fdfc6-5736-4cdc-9d0f-a35af8957cf2'
DIGEST = 'sha256:302a3a02be35f461db2c720ff7f39ede07454cf5777099d3aaf274cc0d578842'
BASELINE = '2d642ec1f54719d8c6088e8cbaf394961cb804a533bd4d7366d48d1d543f5620'
ENCLAVE = 'https://9798850e096d770293c67305c6cfdceed68c1d28-5003s.dstack-pha-prod9.phala.network'
RUNNER_HASH = 'eaa4091cbf5a295abda701cbadc020e0145fbc926bd133437a8d5b71de55d6b2'
RUNNER_NONCE = 640  # nonce639 independently reconciled to Rokku CI38077762030.
RUNNER_PREVIOUS_RUN = '38078351673'
RUNNER_PREVIOUS_CODE = '933bae8f8107cb360c80f5b70c1e6be62dcec4b1'
RUNNER_YAML = 'https://github.com/teleport-computer/feedling-mcp/raw/' + recovery.PIN + '/deploy/docker-compose.phala.prod.runner.yaml'
RUNNER_DATA = '0x' + (keccak(text='addComposeHash(bytes32,string,string)')[:4] + encode(['bytes32','string','string'], [bytes.fromhex(RUNNER_HASH), recovery.PIN, RUNNER_YAML])).hex()
RUNNER_TARGET = (RUNNER_HASH, RUNNER_DATA, '0x' + keccak(text='isAppAllowed(bytes32)')[:4].hex() + RUNNER_HASH)
STAGES = ('plan', 'authorize-main', 'attestation', 'canary', 'runner', 'authorize-runner')
PREVIOUS = {'attestation': 'authorize-main', 'canary': 'attestation', 'runner': 'canary', 'authorize-runner': 'runner'}
OUT = Path('t800-evidence')


def command(args, *, cwd=None, timeout=60):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                          timeout=timeout, check=True).stdout


def api(path):
    return json.loads(command(['gh', 'api', 'repos/' + REPO + '/' + path]))


def save(name, value):
    OUT.mkdir(exist_ok=True, mode=0o700)
    with (OUT / (name + '.json')).open('x') as f:
        json.dump(value, f, indent=2)
        f.flush()
        os.fsync(f.fileno())


def context():
    stage = os.environ['T800_STAGE']
    sha = os.environ['T800_CODE_SHA']
    if stage not in STAGES or not re.fullmatch('[0-9a-f]{40}', sha):
        raise recovery.Stop('invalid_input')
    if (os.environ.get('GITHUB_REPOSITORY') != REPO
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or os.environ.get('GITHUB_RUN_ATTEMPT') != '1'
            or os.environ.get('GITHUB_SHA') != sha
            or command(['git', 'rev-parse', 'HEAD']).strip() != sha):
        raise recovery.Stop('context_mismatch')
    if stage != 'plan' and os.environ.get('T800_CONFIRM') != 'ROOT-T800-' + stage:
        raise recovery.Stop('root_stage_confirmation_required')
    return stage, sha


def verify_reconciled_zero_send():
    """The only reviewed exception: exact pre-send failed run, no generic retry input."""
    run_id = str(RECONCILED_RUN)
    run = api('actions/runs/' + run_id)
    if (run['id'] != RECONCILED_RUN or run['head_sha'] != RECONCILED_CODE
            or run['run_attempt'] != 1 or run['event'] != 'workflow_dispatch'
            or run['conclusion'] != 'failure'
            or run['display_title'] != 'T800 recovery authorize-main'):
        raise recovery.Stop('reconciled_run_identity_changed')
    jobs = api('actions/runs/' + run_id + '/jobs?per_page=100')['jobs']
    target = [j for j in jobs if j['name'] == 'T800 incident recovery']
    if len(target) != 1 or target[0]['conclusion'] != 'failure':
        raise recovery.Stop('reconciled_job_changed')
    expected = {'Prepare exact main transaction without broadcasting': 'failure',
                'Durably archive intent before any send': 'skipped',
                'Single main transaction send': 'skipped',
                'Existing post-deploy gate or runner stage': 'skipped'}
    for name, conclusion in expected.items():
        steps = [s for s in target[0]['steps'] if s['name'] == name]
        if len(steps) != 1 or steps[0]['conclusion'] != conclusion:
            raise recovery.Stop('reconciled_send_exclusion_missing')
    artifacts = api('actions/runs/' + run_id + '/artifacts?per_page=100')
    if artifacts['total_count'] != 1 or len(artifacts['artifacts']) != 1:
        raise recovery.Stop('reconciled_artifact_set_changed')
    artifact = artifacts['artifacts'][0]
    if (artifact['id'] != RECONCILED_ARTIFACT or artifact['expired']
            or artifact['digest'] != RECONCILED_DIGEST
            or artifact['name'] != 't800-stage-' + run_id):
        raise recovery.Stop('reconciled_artifact_identity_changed')
    with tempfile.TemporaryDirectory(prefix='t800-zero-send-') as tmp:
        command(['gh', 'run', 'download', run_id, '--repo', REPO,
                 '--name', artifact['name'], '--dir', tmp])
        files = sorted(str(p.relative_to(tmp)) for p in Path(tmp).rglob('*'))
        if files != ['failure.json']:
            raise recovery.Stop('reconciled_intent_or_other_file_present')
        if json.loads((Path(tmp) / 'failure.json').read_text()) != {
                'result': 'STOP', 'reason': 'HTTPError', 'retry_authorized': False}:
            raise recovery.Stop('reconciled_failure_changed')
    value = {'run_id': RECONCILED_RUN, 'code_sha': RECONCILED_CODE,
             'artifact_id': RECONCILED_ARTIFACT, 'artifact_digest': RECONCILED_DIGEST,
             'result': 'EXACT_PRIOR_PRE_SEND_FAILURE_RECONCILED',
             'nonce_still_required': NONCE, 'generic_retry_authorized': False}
    path = OUT / 'reconciled.json'
    if path.exists():
        if json.loads(path.read_text()) != value:
            raise recovery.Stop('reconciliation_record_changed')
    else:
        save('reconciled', value)


def no_prior_attempt(stage):
    """Fail closed on any earlier same-stage dispatch, even failed before mutation.

    The title is generated by this workflow, not user free text. Root must prohibit
    deleting workflow history. Fixed nonce additionally prevents replacement sends.
    Same run reruns rejected by context(), and CI deploy-cvm lock serializes recovery.
    """
    current = int(os.environ['GITHUB_RUN_ID'])
    reconciled_seen = False
    for page in range(1, 11):
        data = api('actions/workflows/ci.yml/runs?event=workflow_dispatch&per_page=100'
                   '&created=%3E%3D2026-10-10&page=' + str(page))
        runs = data['workflow_runs']
        for run in runs:
            if (run['id'] != current and run['id'] < current
                    and run['display_title'] == 'T800 recovery ' + stage):
                if stage == 'authorize-main' and run['id'] == RECONCILED_RUN:
                    verify_reconciled_zero_send()
                    reconciled_seen = True
                else:
                    raise recovery.Stop('prior_attempt_reconcile_do_not_retry')
        if len(runs) < 100:
            if stage == 'authorize-main' and not reconciled_seen:
                raise recovery.Stop('required_reconciled_run_missing')
            return
    raise recovery.Stop('history_limit')


def previous(stage, sha):
    if stage not in PREVIOUS:
        return
    run_id = os.environ['T800_PREVIOUS_RUN']
    if not run_id.isdecimal() or int(run_id) >= int(os.environ['GITHUB_RUN_ID']):
        raise recovery.Stop('invalid_prior_run')
    if stage == 'authorize-runner':
        if run_id != RUNNER_PREVIOUS_RUN:
            raise recovery.Stop('runner_prior_run_changed')
        sha = RUNNER_PREVIOUS_CODE
    run = api('actions/runs/' + run_id)
    if (run['event'] != 'workflow_dispatch' or run['head_sha'] != sha
            or run['run_attempt'] != 1
            or run['display_title'] != 'T800 recovery ' + PREVIOUS[stage]):
        raise recovery.Stop('prior_run_identity')
    # Unrelated CI jobs may fail; require the recovery job itself, not a global green.
    jobs = api('actions/runs/' + run_id + '/jobs?per_page=100')['jobs']
    matching = [j for j in jobs if j['name'] == 'T800 incident recovery']
    if len(matching) != 1 or matching[0]['conclusion'] != 'success':
        raise recovery.Stop('prior_stage_not_successful')
    dest = OUT / 'previous'
    command(['gh', 'run', 'download', run_id, '--repo', REPO, '--name',
             't800-stage-' + run_id, '--dir', str(dest)])
    value = json.loads((dest / 'stage.json').read_text())
    expected = {'stage': PREVIOUS[stage], 'code_sha': sha, 'source': recovery.SOURCE,
                'pin': recovery.PIN, 'result': 'STAGE_COMPLETE'}
    if any(value.get(k) != v for k, v in expected.items()):
        raise recovery.Stop('prior_evidence_mismatch')
    if stage == 'authorize-runner':
        frozen = json.loads((dest / 'runner-next.json').read_text())
        if frozen != {'runner': RUNNER, 'compose_hash': RUNNER_HASH, 'result': 'FREEZE_AND_REVIEW_RUNNER_AUTHORIZATION', 'deployment_acceptance': False}:
            raise recovery.Stop('runner_prior_hash_changed')


def assert_release():
    if command(['git', 'rev-parse', 'HEAD'], cwd='release').strip() != recovery.PIN:
        raise recovery.Stop('release_checkout_mismatch')
    command(['git', 'diff', '--exit-code', 'HEAD', '--'], cwd='release')
    recovery.live_preflight()


def authorized():
    rpc = recovery.RPC(os.environ['ETH_SEPOLIA_RPC_URL'])
    if int(rpc('eth_chainId', []), 16) != recovery.CHAIN:
        raise recovery.Stop('chain_mismatch')
    if rpc('eth_call', [{'to': recovery.CONTRACT, 'data': recovery.ALLOWED}, 'latest']) != '0x' + '0'*63 + '1':
        raise recovery.Stop('main_hash_not_authorized')


def archived_intent():
    artifact_id = os.environ['T800_INTENT_ARTIFACT_ID']
    if not artifact_id.isdecimal():
        raise recovery.Stop('intent_not_archived')
    value = api('actions/artifacts/' + artifact_id)
    if (value['expired'] or value['size_in_bytes'] <= 0
            or value['name'] != 't800-intent-' + os.environ['GITHUB_RUN_ID']
            or str(value['workflow_run']['id']) != os.environ['GITHUB_RUN_ID']):
        raise recovery.Stop('intent_artifact_mismatch')


def runner_preflight():
    # Main remains an independently authorized prerequisite, never the runner target.
    recovery.live_preflight()
    authorized()
    value = json.loads(command(['phala', 'cvms', 'get', RUNNER, '-j', '--api-key', os.environ['PHALA_CLOUD_API_KEY']]))
    if value.get('vm_uuid') != RUNNER or value.get('status') != 'running' or value.get('compose_hash') != RUNNER_HASH:
        raise recovery.Stop('runner_live_identity_changed')
    compose = value['compose_file']
    if isinstance(compose, str):
        compose = json.loads(compose)
    services = yaml.safe_load(compose['docker_compose_file'])['services']
    image = 'ghcr.io/teleport-computer/feedling-agent-runner:08b2629'
    if set(services) != {'agent-runner'} or services['agent-runner']['image'] != image:
        raise recovery.Stop('runner_live_image_changed')
    manifest = command(['docker', 'buildx', 'imagetools', 'inspect', image])
    if re.findall(r'^Digest:\s+(sha256:[0-9a-f]{64})\s*$', manifest, re.MULTILINE) != [DIGEST]:
        raise recovery.Stop('runner_image_digest_changed')


def main_transaction(phase, *, runner=False):
    def expire(*args):
        raise recovery.Stop('transaction_deadline_reconcile_no_retry')
    signal.signal(signal.SIGALRM, expire)
    signal.alarm(150)
    account = recovery.Account.from_key(os.environ['PRIVATE_KEY'])
    rpc = recovery.RPC(os.environ['ETH_SEPOLIA_RPC_URL'])
    nonce = RUNNER_NONCE if runner else NONCE
    options = {'target': RUNNER_TARGET, 'preflight': runner_preflight} if runner else {}
    if phase == 'prepare':
        value = recovery.recover(rpc, account, nonce, save, prepare_only=True, **options)
        if value['result'] != 'INTENT_ONLY':
            raise recovery.Stop('already_allowed_reconcile_without_send')
        return
    archived_intent()
    frozen = json.loads((OUT / 'intent.json').read_text())
    def verify(name, value):
        if name == 'intent':
            # Every signed parameter must equal the already uploaded intent. No repricing.
            if value != frozen:
                raise recovery.Stop('intent_changed')
        else:
            save(name, value)
    result = recovery.recover(rpc, account, nonce, verify, frozen_intent=frozen, **options)
    if result['result'] != 'HASH_AUTHORIZED_ONLY':
        raise recovery.Stop('authorization_not_this_attempt')
    save('transaction', result)


def canary_receipt(stdout, *, returncode, timed_out, error_type=None):
    """Only fixed markers and constrained fixture/status fields leave the process.

    Missing output never means no fixture. Markers are observations of the pinned
    script; only normal exit + every required marker permits this stage to pass.
    """
    if isinstance(stdout, bytes):
        stdout = stdout.decode('utf-8', errors='replace')
    lines = (stdout or '').splitlines()
    fixture_ids = sorted({m.group(1) for line in lines
                          if (m := re.fullmatch(r'\[canary\] registered (usr_[0-9a-f]{16})', line))})
    statuses = [int(m.group(1)) for line in lines
                if (m := re.fullmatch(r'\[canary\] account reset -> ([0-9]{1,3})', line))]
    reset_status = statuses[-1] if len(statuses) == 1 else 'UNKNOWN'
    inferred = ('[canary] POST https://api.feedling.app/v1/account/reset -> 401 '
                'after an outcome-unknown transport failure; API key invalidation proves cleanup ✓') in lines
    receipt = {
        'source': recovery.SOURCE, 'pin': recovery.PIN,
        'code_sha': os.environ.get('T800_CODE_SHA', 'UNKNOWN'),
        'run_id': os.environ.get('GITHUB_RUN_ID', 'UNKNOWN'),
        'fixture_ids_observed': fixture_ids,
        'fixture_observation': 'OBSERVED' if fixture_ids else 'UNKNOWN',
        'additional_unobserved_fixtures': 'UNKNOWN',
        'key_match': 'OBSERVED' if '[canary] advertised pk == attested pk ✓' in lines else 'UNKNOWN',
        'plaintext_off': 'OBSERVED' if '[canary] plaintext effective tier = off ✓' in lines else 'UNKNOWN',
        'roundtrip': 'OBSERVED' if any(re.fullmatch(r'\[canary\] enclave decrypt round-trip ✓ \(attempt [1-9][0-9]*\)', line) for line in lines) else 'UNKNOWN',
        'reset_status_observed': reset_status,
        'reset_inferred_from_revoked_key': inferred,
        'canary_ok': 'OBSERVED' if 'CANARY OK' in lines else 'UNKNOWN',
        'exit_code': returncode, 'timed_out': timed_out, 'error_type': error_type,
        'deployment_acceptance': False,
    }
    receipt['result'] = 'PASS' if (returncode == 0 and not timed_out
        and error_type is None and len(fixture_ids) == 1 and reset_status == 200
        and all(receipt[key] == 'OBSERVED' for key in
                ['key_match', 'plaintext_off', 'roundtrip', 'canary_ok'])) else 'INCOMPLETE_OR_FAILED'
    return receipt


def run_canary(*, timeout=480):
    env = dict(os.environ, GITHUB_SHA=recovery.SOURCE,
               FEEDLING_CANARY_LABEL='deploy-canary-' + recovery.SOURCE[:12]
               + '-t800-' + os.environ['GITHUB_RUN_ID'])
    stdout, returncode, timed_out, error_type = '', None, False, None
    try:
        process = subprocess.run([sys.executable, '-u', 'tools/deploy_canary.py'],
                                 cwd='release', env=env, capture_output=True,
                                 text=True, timeout=timeout, check=False)
        stdout, returncode = process.stdout, process.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, timed_out, error_type = exc.stdout, True, 'TimeoutExpired'
    except subprocess.CalledProcessError as exc:
        stdout, returncode, error_type = exc.stdout, exc.returncode, 'CalledProcessError'
    except BaseException as exc:
        error_type = type(exc).__name__
    # Never persist stderr, raw stdout, URLs, payloads, keys, or exception messages.
    receipt = canary_receipt(stdout, returncode=returncode, timed_out=timed_out,
                             error_type=error_type)
    save('canary', receipt)
    if receipt['result'] != 'PASS':
        raise recovery.Stop('canary_evidence_incomplete_or_failed')


def resume(stage):
    assert_release()
    authorized()
    env = os.environ
    if env.get('ATTEST_BASE') != ENCLAVE or env.get('BASELINE_PK') != BASELINE:
        raise recovery.Stop('trust_config_changed')
    if stage == 'attestation':
        command(['bash', 'deploy/attestation-gate.sh', 'prod',
                 str((OUT / 'attestation.json').resolve())], cwd='release', timeout=480)
        data = json.loads((OUT / 'attestation.json').read_text())
        if data.get('enclave_content_pk_hex') != BASELINE:
            raise recovery.Stop('attestation_baseline_mismatch')
    elif stage == 'canary':
        if env.get('FEEDLING_API_URL') != 'https://api.feedling.app' or env.get('FEEDLING_ENCLAVE_URL') != ENCLAVE or env.get('FEEDLING_CANARY_EXPECT_PLAINTEXT') != '1':
            raise recovery.Stop('canary_config_changed')
        run_canary()
    elif stage == 'runner':
        if env.get('FEEDLING_DATABASE_SCHEMA') != 'tee' or env.get('FEEDLING_PLAINTEXT_WRITES_ACCEPTED') != '1' or env.get('FEEDLING_API_URL') != 'https://api.feedling.app' or env.get('FEEDLING_ENCLAVE_URL') != ENCLAVE:
            raise recovery.Stop('runner_config_changed')
        ids = command(['bash', 'deploy/list-prod-runner-cvm-ids.sh', 'deploy/prod-runner-cvm-ids.txt'], cwd='release').strip()
        if ids != RUNNER:
            raise recovery.Stop('runner_ids_changed')
        # Existing tag must still resolve to A's independently captured immutable digest.
        image = 'ghcr.io/teleport-computer/feedling-agent-runner:08b2629'
        manifest = command(['docker', 'buildx', 'imagetools', 'inspect', image])
        digests = re.findall(r'^Digest:\s+(sha256:[0-9a-f]{64})\s*$', manifest, re.MULTILINE)
        if digests != [DIGEST]:
            raise recovery.Stop('runner_image_digest_changed')
        env['IDS'] = RUNNER
        command(['bash', str(Path(__file__).with_name('runner-existing.sh').resolve())], cwd='release', timeout=1100)
        value = json.loads(command(['phala', 'cvms', 'get', RUNNER, '-j', '--api-key', env['PHALA_CLOUD_API_KEY']]))
        compose = value.get('compose_hash', '')
        if not re.fullmatch('[0-9a-f]{64}', compose):
            raise recovery.Stop('runner_hash_missing')
        save('runner-next', {'runner': RUNNER, 'compose_hash': compose,
                            'result': 'FREEZE_AND_REVIEW_RUNNER_AUTHORIZATION',
                            'deployment_acceptance': False})


def execute(phase):
    stage, sha = context()
    if stage == 'plan':
        print('PLAN_ONLY; no credentials, network or mutation')
        return
    OUT.mkdir(exist_ok=True, mode=0o700)
    no_prior_attempt(stage)
    previous(stage, sha)
    if stage in ('authorize-main', 'authorize-runner'):
        assert_release()
        if phase not in ('prepare', 'send'):
            raise recovery.Stop('invalid_phase')
        try:
            main_transaction(phase, runner=True) if stage == 'authorize-runner' else main_transaction(phase)
        finally:
            signal.alarm(0)
        if phase == 'prepare':
            return
    else:
        if phase != 'stage':
            raise recovery.Stop('invalid_phase')
        resume(stage)
    save('stage', {'stage': stage, 'code_sha': sha, 'source': recovery.SOURCE,
                   'pin': recovery.PIN, 'result': 'STAGE_COMPLETE',
                   'deployment_acceptance': False})


def entry():
    os.umask(0o077)
    try:
        execute(sys.argv[1])
    except BaseException as exc:
        # Stop messages are fixed application reasons; all external errors expose only type.
        reason = str(exc) if isinstance(exc, recovery.Stop) else type(exc).__name__
        try:
            save('failure', {'result': 'STOP', 'reason': reason, 'retry_authorized': False})
        except BaseException:
            pass  # Diagnostic failure cannot authorize progression or reveal raw errors.
        print('STOP T800 recovery; reconcile evidence, no automatic retry', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(entry())
