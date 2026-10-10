import json
from pathlib import Path
import pytest
import ci
import recover as r
from test_recover import Signer
from test_runner_authorization import RunnerRPC

@pytest.fixture
def limits_evidence(monkeypatch,tmp_path):
 monkeypatch.setattr(ci,'OUT',tmp_path/'out')
 run={'id':ci.RUNNER_LIMITS_RUN,'head_sha':ci.RUNNER_LIMITS_CODE,'run_attempt':1,'event':'workflow_dispatch','conclusion':'failure','display_title':'T800 recovery authorize-runner'}
 jobs={'jobs':[{'name':'T800 incident recovery','conclusion':'failure','steps':[{'name':n,'conclusion':c} for n,c in [('Prepare exact main transaction without broadcasting','failure'),('Durably archive intent before any send','skipped'),('Single main transaction send','skipped'),('Existing post-deploy gate or runner stage','skipped')]]}]}
 arts={'total_count':1,'artifacts':[{'id':ci.RUNNER_LIMITS_ARTIFACT,'digest':ci.RUNNER_LIMITS_DIGEST,'expired':False,'name':'t800-stage-'+str(ci.RUNNER_LIMITS_RUN)}]}
 failure={'result':'STOP','reason':'frozen_limits_insufficient','retry_authorized':False}
 prior={'run_id':ci.RUNNER_FAILED_RUN,'code_sha':ci.RUNNER_FAILED_CODE,'intent_hash':ci.RUNNER_FAILED_INTENT['hash'],'nonce':640,'result':'EXACT_DOWNLOAD_FAILURE_RECONCILED','generic_retry_authorized':False}
 extra=[]
 monkeypatch.setattr(ci,'api',lambda p:jobs if '/jobs?' in p else arts if '/artifacts?' in p else run)
 def download(args,**kw):
  d=Path(args[-1]);(d/'failure.json').write_text(json.dumps(failure));(d/'runner-reconciled.json').write_text(json.dumps(prior))
  for f in extra:(d/f).write_text('{}')
 monkeypatch.setattr(ci,'command',download)
 return run,jobs,arts,failure,prior,extra

def test_exact_limits_failure_twice(limits_evidence):
 ci.verify_runner_limits_failure();ci.verify_runner_limits_failure()
 assert json.loads((ci.OUT/'runner-limits-reconciled.json').read_text())['gas_price']==3000000

@pytest.mark.parametrize('kind',['sha','attempt','send','digest','count','intent','reason','extra'])
def test_limits_evidence_drift_stops(limits_evidence,kind):
 run,jobs,arts,failure,prior,extra=limits_evidence
 if kind=='sha':run['head_sha']='0'*40
 elif kind=='attempt':run['run_attempt']=2
 elif kind=='send':jobs['jobs'][0]['steps'][2]['conclusion']='success'
 elif kind=='digest':arts['artifacts'][0]['digest']='wrong'
 elif kind=='count':arts['total_count']=2
 elif kind=='intent':prior['intent_hash']='0x'+'0'*64
 elif kind=='reason':failure['reason']='other'
 else:extra.append('intent.json')
 with pytest.raises(r.Stop):ci.verify_runner_limits_failure()

class FeeRPC(RunnerRPC):
 def __init__(self,fee):super().__init__();self.fee=fee
 def __call__(self,m,p):
  if m=='eth_gasPrice':return hex(self.fee)
  return super().__call__(m,p)

def test_measured_two_wei_drift_new_fixed_limits():
 old=FeeRPC(1000016)
 with pytest.raises(r.Stop,match='frozen_limits_insufficient'):
  r.recover(old,Signer(),640,old.save,target=ci.RUNNER_TARGET,preflight=lambda:None,prepare_only=True,frozen_intent=ci.RUNNER_FAILED_INTENT)
 assert old.sent==0 and not old.saved
 f=FeeRPC(1000016);s=Signer()
 r.recover(f,s,640,f.save,target=ci.RUNNER_TARGET,preflight=lambda:None,prepare_only=True,frozen_intent=ci.RUNNER_NEW_INTENT)
 assert s.tx['gas']==2400000 and s.tx['gasPrice']==3000000 and f.sent==0
 assert s.tx['gas']*s.tx['gasPrice']==7200000000000<r.MAX_GAS*r.MAX_GAS_PRICE

def test_new_fixed_price_exceeded_stops_no_adaptation():
 f=FeeRPC(1500001);s=Signer()
 with pytest.raises(r.Stop,match='frozen_limits_insufficient'):
  r.recover(f,s,640,f.save,target=ci.RUNNER_TARGET,preflight=lambda:None,prepare_only=True,frozen_intent=ci.RUNNER_NEW_INTENT)
 assert f.sent==0 and not f.saved and not hasattr(s,'tx')
