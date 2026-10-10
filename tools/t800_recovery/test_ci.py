import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
import yaml
import ci
import recover as r
from test_recover import Fake, Signer


@pytest.fixture
def context(monkeypatch,tmp_path):
    monkeypatch.chdir(tmp_path)
    for key,value in {'T800_STAGE':'authorize-main','T800_CODE_SHA':'a'*40,
       'GITHUB_SHA':'a'*40,'GITHUB_REPOSITORY':ci.REPO,'GITHUB_EVENT_NAME':'workflow_dispatch',
       'GITHUB_RUN_ATTEMPT':'1','GITHUB_RUN_ID':'100','T800_CONFIRM':'ROOT-T800-authorize-main'}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.setattr(ci,'command',lambda *a,**k:'a'*40)


def test_context_binds_exact_code(context):
    assert ci.context()==('authorize-main','a'*40)


@pytest.mark.parametrize('key,value',[('GITHUB_SHA','b'*40),('GITHUB_RUN_ATTEMPT','2'),
    ('T800_CONFIRM',''),('GITHUB_EVENT_NAME','push'),('T800_CODE_SHA','main'),
    ('GITHUB_REPOSITORY','wrong/repo')])
def test_context_rejects(context,monkeypatch,key,value):
    monkeypatch.setenv(key,value)
    with pytest.raises(r.Stop):ci.context()


def test_plan_no_network_or_key(context,monkeypatch,capsys):
    monkeypatch.setenv('T800_STAGE','plan')
    monkeypatch.setattr(ci,'api',lambda *a:pytest.fail('no network'))
    monkeypatch.delenv('PRIVATE_KEY',raising=False)
    ci.execute('stage')
    assert 'PLAN_ONLY' in capsys.readouterr().out
    assert not ci.OUT.exists()


def test_prior_attempt_blocks_across_ref_even_failure(context,monkeypatch):
    monkeypatch.setattr(ci,'api',lambda *a:{'workflow_runs':[{'id':99,'display_title':'T800 recovery authorize-main','conclusion':'failure','head_sha':'b'*40}]})
    with pytest.raises(r.Stop,match='prior_attempt'):ci.no_prior_attempt('authorize-main')


def test_history_budget_fail_closed(context,monkeypatch):
    calls=[]
    def api(*a):
        calls.append(1)
        return {'workflow_runs':[{'id':1,'display_title':'unrelated'}]*100}
    monkeypatch.setattr(ci,'api',api)
    with pytest.raises(r.Stop,match='history_limit'):ci.no_prior_attempt('authorize-main')
    assert len(calls)==10


def test_prepare_never_broadcasts_and_second_phase_matches_intent(context,monkeypatch):
    f=Fake();signer=Signer()
    real_signer=r.Account.from_key(bytes.fromhex('01'*32))
    signer.sign_transaction=real_signer.sign_transaction
    monkeypatch.setenv('PRIVATE_KEY','fake')
    monkeypatch.setenv('ETH_SEPOLIA_RPC_URL','https://fake.invalid')
    monkeypatch.setattr(r,'Account',SimpleNamespace(from_key=lambda *a:signer))
    monkeypatch.setattr(r,'RPC',lambda *a:f)
    # recover captures default preflight at definition; supply through wrapper in test.
    original=r.recover
    monkeypatch.setattr(r,'recover',lambda *a,**k:original(*a,**k,preflight=lambda:None,sleep=lambda _:None))
    ci.main_transaction('prepare')
    assert f.sent==0 and (ci.OUT/'intent.json').exists()
    f.saved['intent']=json.loads((ci.OUT/'intent.json').read_text())
    calls=[]
    monkeypatch.setattr(ci,'archived_intent',lambda:calls.append('archived'))
    ci.main_transaction('send')
    assert f.sent==1 and calls==['archived']


def test_cannot_send_before_archive(context,monkeypatch):
    monkeypatch.setenv('T800_INTENT_ARTIFACT_ID','')
    with pytest.raises(r.Stop):ci.archived_intent()


@pytest.mark.parametrize('change',[{'expired':True},{'size_in_bytes':0},{'name':'wrong'},
                                  {'workflow_run':{'id':99}}])
def test_archive_bound_to_current_attempt(context,monkeypatch,change):
    monkeypatch.setenv('T800_INTENT_ARTIFACT_ID','123')
    value={'expired':False,'size_in_bytes':999,'name':'t800-intent-100','workflow_run':{'id':100}}
    value.update(change)
    monkeypatch.setattr(ci,'api',lambda *a:value)
    with pytest.raises(r.Stop):ci.archived_intent()


def test_every_exception_is_sanitized(context,monkeypatch,capsys):
    def fail(*a):raise RuntimeError('FAKE_SECRET_IN_ARGV_OR_RESPONSE')
    monkeypatch.setattr(ci,'execute',fail)
    monkeypatch.setattr(sys,'argv',['ci.py','stage'])
    assert ci.entry()==2
    output=capsys.readouterr()
    assert 'FAKE_SECRET' not in output.err+output.out and 'Traceback' not in output.err


def test_prior_evidence_is_exact_and_completed(context,monkeypatch):
    monkeypatch.setenv('T800_PREVIOUS_RUN','99')
    def api(path):
        if '/jobs?' in path:return {'jobs':[{'name':'T800 incident recovery','conclusion':'failure'}]}
        return {'event':'workflow_dispatch','head_sha':'a'*40,'run_attempt':1,'display_title':'T800 recovery authorize-main'}
    monkeypatch.setattr(ci,'api',api)
    with pytest.raises(r.Stop,match='not_successful'):ci.previous('attestation','a'*40)


def test_workflow_archives_before_send_and_secrets_stay_in_ci():
    root=Path(__file__).resolve().parents[2]
    workflow=yaml.load((root/'.github/workflows/ci.yml').read_text(),Loader=yaml.BaseLoader)
    assert workflow['on']['workflow_dispatch']['inputs']['recovery_stage']['default']=='plan'
    job=workflow['jobs']['t800-incident-recovery'];steps=job['steps']
    names=[s.get('name') for s in steps]
    assert names.index('Prepare exact main transaction without broadcasting') < names.index('Durably archive intent before any send') < names.index('Single main transaction send')
    assert job['permissions']=={'contents':'read','actions':'read','packages':'read'}
    assert job['concurrency']['group']=='deploy-cvm'
    for name in ['forge-test','python-tests','runtime-v2-coverage','docker-build','docker-build-worker-deps','lint','dcap-python','ci-execution-evidence-report']:
        assert "inputs.operation != 't800-recover'" in workflow['jobs'][name]['if']
    for step in steps:
        assert '--private-key' not in step.get('run','')
        assert 'pin-runtime-release' not in step.get('run','')
    assert all('PRIVATE_KEY' not in s.get('env',{}) for s in steps if s.get('name') not in ['Prepare exact main transaction without broadcasting','Single main transaction send'])


def test_canary_receipt_uploaded_on_failure():
    root=Path(__file__).resolve().parents[2]
    workflow=yaml.load((root/'.github/workflows/ci.yml').read_text(),Loader=yaml.BaseLoader)
    steps=workflow['jobs']['t800-incident-recovery']['steps']
    upload=next(s for s in steps if s.get('name')=='Preserve safe recovery evidence on success or failure')
    assert 'always()' in upload['if']
    assert 't800-evidence/canary.json' in upload['with']['path'].splitlines()
