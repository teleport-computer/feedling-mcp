import json
import os
from pathlib import Path
import subprocess

import pytest
import ci
import recover as r

UID = 'usr_0123456789abcdef'
SUCCESS = '\n'.join([
    '[canary] registered ' + UID,
    '[canary] advertised pk == attested pk ✓',
    '[canary] plaintext effective tier = off ✓',
    '[canary] enclave decrypt round-trip ✓ (attempt 1)',
    '[canary] account reset -> 200', 'CANARY OK'])
SECRET = 'FAKE_UNTRUSTED_API_KEY_OR_PRIVATE_BODY'


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'release/tools').mkdir(parents=True)
    monkeypatch.setenv('GITHUB_RUN_ID','123')
    monkeypatch.setenv('T800_CODE_SHA','a'*40)
    monkeypatch.setenv('GITHUB_SHA','a'*40)
    monkeypatch.setenv('ATTEST_BASE',ci.ENCLAVE)
    monkeypatch.setenv('BASELINE_PK',ci.BASELINE)
    monkeypatch.setenv('FEEDLING_API_URL','https://api.feedling.app')
    monkeypatch.setenv('FEEDLING_ENCLAVE_URL',ci.ENCLAVE)
    monkeypatch.setenv('FEEDLING_CANARY_EXPECT_PLAINTEXT','1')
    monkeypatch.setattr(ci,'assert_release',lambda:None)
    monkeypatch.setattr(ci,'authorized',lambda:None)
    return tmp_path/'release/tools/deploy_canary.py'


def receipt():
    return json.loads((ci.OUT/'canary.json').read_text())


def test_real_subprocess_success_safe_receipt(runtime):
    runtime.write_text('import os\nassert os.environ["GITHUB_SHA"] == '+repr(r.SOURCE)+'\n'
                       'assert os.environ["FEEDLING_CANARY_LABEL"] == '+repr('deploy-canary-'+r.SOURCE[:12]+'-t800-123')+'\n'
                       'print('+repr(SUCCESS)+' )\nprint('+repr(SECRET)+' )\n')
    ci.resume('canary')
    value=receipt()
    assert value['result']=='PASS' and value['fixture_ids_observed']==[UID]
    assert value['reset_status_observed']==200 and value['roundtrip']=='OBSERVED'
    assert value['code_sha']=='a'*40 and value['source']==r.SOURCE
    assert SECRET not in json.dumps(value)


def test_real_subprocess_failure_preserves_known_fixture_and_failed_reset(runtime):
    runtime.write_text('import sys\nprint('+repr(SUCCESS.replace(' -> 200',' -> 503').replace('CANARY OK',''))+')\n'
                       'print('+repr(SECRET)+',file=sys.stderr)\nsys.exit(1)\n')
    with pytest.raises(r.Stop):ci.run_canary()
    value=receipt()
    assert value['fixture_ids_observed']==[UID] and value['reset_status_observed']==503
    assert value['exit_code']==1 and value['result']=='INCOMPLETE_OR_FAILED'
    assert SECRET not in json.dumps(value)


def test_real_subprocess_timeout_retains_partial_output_unknown_cleanup(runtime):
    runtime.write_text('import time\nprint('+repr('[canary] registered '+UID)+')\nprint('+repr(SECRET)+')\ntime.sleep(10)\n')
    with pytest.raises(r.Stop):ci.run_canary(timeout=0.2)
    value=receipt()
    assert value['timed_out'] is True and value['fixture_ids_observed']==[UID]
    assert value['reset_status_observed']=='UNKNOWN' and value['canary_ok']=='UNKNOWN'
    assert SECRET not in json.dumps(value)


def test_missing_output_never_means_no_fixture(runtime):
    runtime.write_text('pass\n')
    with pytest.raises(r.Stop):ci.run_canary()
    value=receipt()
    assert value['fixture_observation']=='UNKNOWN'
    assert value['additional_unobserved_fixtures']=='UNKNOWN'
    assert value['result']=='INCOMPLETE_OR_FAILED'


def test_success_markers_with_nonzero_exit_do_not_pass():
    value=ci.canary_receipt(SUCCESS,returncode=1,timed_out=False)
    assert value['result']=='INCOMPLETE_OR_FAILED'


def test_extra_untrusted_lines_excluded_and_inferred_reset_distinguished():
    stdout=SUCCESS+'\n'+SECRET+'\n[canary] registered '+SECRET+'\n'
    stdout+='[canary] POST https://api.feedling.app/v1/account/reset -> 401 after an outcome-unknown transport failure; API key invalidation proves cleanup ✓\n'
    value=ci.canary_receipt(stdout.encode(),returncode=0,timed_out=False)
    assert value['reset_inferred_from_revoked_key'] is True
    assert SECRET not in json.dumps(value) and value['fixture_ids_observed']==[UID]


def test_launch_failure_also_leaves_safe_unknown_receipt(runtime,monkeypatch):
    def fail(*a,**k):raise OSError(SECRET)
    monkeypatch.setattr(ci.subprocess,'run',fail)
    with pytest.raises(r.Stop):ci.run_canary()
    assert receipt()['error_type']=='OSError'
    assert receipt()['fixture_observation']=='UNKNOWN'
    assert SECRET not in json.dumps(receipt())
