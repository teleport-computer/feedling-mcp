import io
import json
from pathlib import Path
from types import SimpleNamespace
import urllib.error

import pytest
import recover as r
import ci


def test_rpc_application_agent_and_safe_http_status(monkeypatch):
    seen=[]
    class Transport:
        def open(self,request,**kwargs):
            seen.append(request.get_header('User-agent'))
            raise urllib.error.HTTPError('https://fake.invalid/SECRET_QUERY',403,'SECRET_BODY',{'Secret':'TOKEN'},io.BytesIO(b'SECRET'))
    rpc=r.RPC('https://fake.invalid/SECRET_QUERY');rpc.open=Transport()
    with pytest.raises(r.Stop) as exc:rpc('eth_chainId',[])
    assert str(exc.value)=='rpc_eth_chainId_http_403'
    assert seen==['feedling-t800-recovery/1']
    assert 'SECRET' not in str(exc.value)


def test_health_error_identifies_only_static_operation(monkeypatch):
    monkeypatch.setenv('PHALA_CLOUD_API_KEY','SECRET')
    monkeypatch.setattr(r.subprocess,'run',lambda args,**k:SimpleNamespace(stdout=r.PIN if args[0]=='gh' else json.dumps({'compose_hash':r.HASH})))
    def fail(*a,**k):raise urllib.error.HTTPError('https://fake.invalid/SECRET',503,'SECRET',{},io.BytesIO(b'SECRET'))
    monkeypatch.setattr(r.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=fail))
    with pytest.raises(r.Stop) as exc:r.live_preflight()
    assert str(exc.value)=='health_http_503'


@pytest.fixture
def reconciled(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    data={
      'run':{'id':ci.RECONCILED_RUN,'head_sha':ci.RECONCILED_CODE,'run_attempt':1,
             'event':'workflow_dispatch','conclusion':'failure','display_title':'T800 recovery authorize-main'},
      'jobs':{'jobs':[{'name':'T800 incident recovery','conclusion':'failure','steps':[
        {'name':name,'conclusion':result} for name,result in [
          ('Prepare exact main transaction without broadcasting','failure'),
          ('Durably archive intent before any send','skipped'),
          ('Single main transaction send','skipped'),
          ('Existing post-deploy gate or runner stage','skipped')]]}]},
      'artifacts':{'total_count':1,'artifacts':[{'id':ci.RECONCILED_ARTIFACT,'expired':False,
       'digest':ci.RECONCILED_DIGEST,'name':'t800-stage-'+str(ci.RECONCILED_RUN)}]},
      'files':{'failure.json':{'result':'STOP','reason':'HTTPError','retry_authorized':False}}}
    def api(path):
        if '/jobs?' in path:return data['jobs']
        if '/artifacts?' in path:return data['artifacts']
        return data['run']
    def command(args,**kw):
        dest=Path(args[-1])
        for name,value in data['files'].items():(dest/name).write_text(json.dumps(value))
        return ''
    monkeypatch.setattr(ci,'api',api);monkeypatch.setattr(ci,'command',command)
    return data


def test_only_exact_zero_send_evidence_allows_exception(reconciled):
    ci.verify_reconciled_zero_send()
    value=json.loads((ci.OUT/'reconciled.json').read_text())
    assert value['nonce_still_required']==638 and not value['generic_retry_authorized']
    ci.verify_reconciled_zero_send() # Second pre-send check revalidates, preserves receipt.


@pytest.mark.parametrize('change',['sha','attempt','id','send','prepare','artifact_id','digest','count','intent','reason'])
def test_zero_send_exception_fails_on_any_evidence_drift(reconciled,change):
    d=reconciled
    if change=='sha':d['run']['head_sha']='b'*40
    if change=='attempt':d['run']['run_attempt']=2
    if change=='id':d['run']['id']+=1
    if change=='send':d['jobs']['jobs'][0]['steps'][2]['conclusion']='success'
    if change=='prepare':d['jobs']['jobs'][0]['steps'][0]['conclusion']='success'
    if change=='artifact_id':d['artifacts']['artifacts'][0]['id']+=1
    if change=='digest':d['artifacts']['artifacts'][0]['digest']='sha256:'+'0'*64
    if change=='count':d['artifacts']['total_count']=2
    if change=='intent':d['files']['intent.json']={'hash':'0xunknown'}
    if change=='reason':d['files']['failure.json']['reason']='receipt_timeout'
    with pytest.raises(r.Stop):ci.verify_reconciled_zero_send()
    assert not (ci.OUT/'reconciled.json').exists()


def test_history_exception_never_allows_a_second_failed_run(reconciled,monkeypatch):
    original=ci.api
    monkeypatch.setenv('GITHUB_RUN_ID',str(ci.RECONCILED_RUN+100))
    def api(path):
        if '/workflows/' in path:return {'workflow_runs':[
          {'id':ci.RECONCILED_RUN,'display_title':'T800 recovery authorize-main'},
          {'id':ci.RECONCILED_RUN+1,'display_title':'T800 recovery authorize-main'}]}
        return original(path)
    monkeypatch.setattr(ci,'api',api)
    with pytest.raises(r.Stop,match='prior_attempt'):ci.no_prior_attempt('authorize-main')


def test_deleted_or_missing_original_failure_cannot_admit_new_attempt(monkeypatch):
    monkeypatch.setenv('GITHUB_RUN_ID',str(ci.RECONCILED_RUN+100))
    monkeypatch.setattr(ci,'api',lambda *a:{'workflow_runs':[]})
    with pytest.raises(r.Stop,match='required_reconciled_run_missing'):
        ci.no_prior_attempt('authorize-main')
