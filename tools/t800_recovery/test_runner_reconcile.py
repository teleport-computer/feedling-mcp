import json
from pathlib import Path
import subprocess
import sys
import pytest
import ci
import recover as r
from test_runner_authorization import RunnerRPC

@pytest.fixture
def evidence(monkeypatch,tmp_path):
 monkeypatch.setattr(ci,'OUT',tmp_path/'out');monkeypatch.setenv('ETH_SEPOLIA_RPC_URL','https://invalid')
 run={'id':ci.RUNNER_FAILED_RUN,'head_sha':ci.RUNNER_FAILED_CODE,'run_attempt':1,'event':'workflow_dispatch','conclusion':'failure','display_title':'T800 recovery authorize-runner'}
 steps=[{'name':n,'conclusion':c} for n,c in [('Prepare exact main transaction without broadcasting','success'),('Durably archive intent before any send','success'),('Single main transaction send','failure'),('Existing post-deploy gate or runner stage','skipped')]]
 jobs={'jobs':[{'name':'T800 incident recovery','conclusion':'failure','steps':steps}]}
 arts={'total_count':2,'artifacts':[{'name':n,'id':i,'digest':d,'expired':False} for n,(i,d) in ci.RUNNER_FAILED_ARTIFACTS.items()]}
 def api(path):return jobs if '/jobs?' in path else arts if '/artifacts?' in path else run
 monkeypatch.setattr(ci,'api',api)
 def download(args,**kw):
  dest=Path(args[-1]);(dest/'intent.json').write_text(json.dumps(ci.RUNNER_FAILED_INTENT))
  if args[args.index('--name')+1].startswith('t800-stage'):(dest/'failure.json').write_text(json.dumps({'result':'STOP','reason':'CalledProcessError','retry_authorized':False}))
 monkeypatch.setattr(ci,'command',download)
 state={'present':False,'nonce':640,'allowed':False}
 def rpc(method,params):
  if method=='eth_chainId':return hex(r.CHAIN)
  if method in ('eth_getTransactionByHash','eth_getTransactionReceipt'):return {'hash':ci.RUNNER_FAILED_INTENT['hash']} if state['present'] else None
  if method=='eth_getTransactionCount':return hex(state['nonce'])
  if method=='eth_call':return '0x'+format(int(state['allowed']),'064x')
  raise AssertionError(method)
 monkeypatch.setattr(r,'RPC',lambda _:rpc)
 return run,jobs,arts,state

def test_exact_runner_reconciliation_revalidated_twice(evidence):
 ci.verify_runner_zero_send();ci.verify_runner_zero_send()
 assert json.loads((ci.OUT/'runner-reconciled.json').read_text())['intent_hash']==ci.RUNNER_FAILED_INTENT['hash']

@pytest.mark.parametrize('change',['sha','attempt','step','digest','id','count','present','nonce','allowed'])
def test_runner_reconciliation_drift_stops(evidence,change):
 run,jobs,arts,state=evidence
 if change=='sha':run['head_sha']='0'*40
 elif change=='attempt':run['run_attempt']=2
 elif change=='step':jobs['jobs'][0]['steps'][2]['conclusion']='success'
 elif change in ('digest','id'):arts['artifacts'][0][change]='wrong'
 elif change=='count':arts['total_count']=3
 elif change=='present':state['present']=True
 elif change=='nonce':state['nonce']=641
 elif change=='allowed':state['allowed']=True
 with pytest.raises(r.Stop):ci.verify_runner_zero_send()

def test_missing_failed_runner_history_stops(monkeypatch):
 monkeypatch.setenv('GITHUB_RUN_ID','99999999999');monkeypatch.setattr(ci,'api',lambda _:{'workflow_runs':[]})
 with pytest.raises(r.Stop,match='required_runner'):ci.no_prior_attempt('authorize-runner')

def test_two_phase_previous_uses_real_exclusive_filesystem_download(monkeypatch,tmp_path):
 # Actual subprocess extractor creates files exclusively, as gh does; previous()
 # itself is real and executes in both phases, never skipped/cached/overwritten.
 monkeypatch.setattr(ci,'OUT',tmp_path/'out');monkeypatch.setenv('T800_PREVIOUS_RUN',ci.RUNNER_PREVIOUS_RUN);monkeypatch.setenv('GITHUB_RUN_ID','99999999999')
 monkeypatch.setattr(ci,'context',lambda:('authorize-runner','a'*40))
 monkeypatch.setattr(ci,'no_prior_attempt',lambda _:None);monkeypatch.setattr(ci,'assert_release',lambda:None)
 monkeypatch.setattr(ci,'api',lambda path:{'jobs':[{'name':'T800 incident recovery','conclusion':'success'}]} if '/jobs?' in path else {'event':'workflow_dispatch','head_sha':ci.RUNNER_PREVIOUS_CODE,'run_attempt':1,'display_title':'T800 recovery runner'})
 stage={'stage':'runner','code_sha':ci.RUNNER_PREVIOUS_CODE,'source':r.SOURCE,'pin':r.PIN,'result':'STAGE_COMPLETE'}
 runner={'runner':ci.RUNNER,'compose_hash':ci.RUNNER_HASH,'result':'FREEZE_AND_REVIEW_RUNNER_AUTHORIZATION','deployment_acceptance':False}
 destinations=[]
 def command(args,**kw):
  dest=args[-1];destinations.append(dest)
  subprocess.run([sys.executable,'-c',"import pathlib,sys;d=pathlib.Path(sys.argv[1]);d.mkdir(exist_ok=True);(d/'stage.json').open('x').write(sys.argv[2]);(d/'runner-next.json').open('x').write(sys.argv[3])",dest,json.dumps(stage),json.dumps(runner)],check=True,capture_output=True,text=True)
  return ''
 monkeypatch.setattr(ci,'command',command)
 phases=[];monkeypatch.setattr(ci,'main_transaction',lambda phase,**kw:phases.append(phase))
 ci.execute('prepare');ci.execute('send')
 assert phases==['prepare','send'] and len(set(destinations))==2
 assert all(not Path(d).exists() for d in destinations)
