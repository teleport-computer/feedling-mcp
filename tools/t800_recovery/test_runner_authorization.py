import json
import pytest
from eth_abi import decode
import ci
import recover as r
from test_recover import Fake, Signer

class RunnerRPC(Fake):
 def __call__(self,m,p):
  if m=='eth_getTransactionCount':
   self.nonces+=1
   return hex(641 if self.mode=='drift' and self.nonces>=5 else 640)
  if m=='eth_call' and p[0]['data']==ci.RUNNER_TARGET[2]:
   self.allowed_reads+=1
   return '0x'+format(int(self.sent>0),'064x')
  return super().__call__(m,p)

def test_runner_exact_target_and_nonce_not_main():
 f=RunnerRPC();s=Signer()
 result=r.recover(f,s,ci.RUNNER_NONCE,f.save,target=ci.RUNNER_TARGET,preflight=lambda:None,sleep=lambda _:None)
 assert result['sends']==1 and s.tx['nonce']==640
 h,pin,url=decode(['bytes32','string','string'],bytes.fromhex(s.tx['data'][10:]))
 assert h.hex()==ci.RUNNER_HASH!=r.HASH and pin==r.PIN and url==ci.RUNNER_YAML
 assert f.saved['intent']['compose_hash']==ci.RUNNER_HASH
 assert s.tx['data']!=r.DATA

def test_nonce_drift_after_final_estimate_is_zero_send():
 f=RunnerRPC('drift');s=Signer()
 with pytest.raises(r.Stop,match='nonce_changed'):
  r.recover(f,s,640,f.save,target=ci.RUNNER_TARGET,preflight=lambda:None)
 assert f.sent==0 and not f.saved and not hasattr(s,'tx')

@pytest.mark.parametrize('field,value',[('vm_uuid','wrong'),('status','updating'),('compose_hash',r.HASH),('image','bad'),('digest','sha256:'+'0'*64)])
def test_runner_preflight_rejects_identity_drift(monkeypatch,field,value):
 monkeypatch.setenv('PHALA_CLOUD_API_KEY','fake')
 monkeypatch.setattr(r,'live_preflight',lambda:None);monkeypatch.setattr(ci,'authorized',lambda:None)
 d={'vm_uuid':ci.RUNNER,'status':'running','compose_hash':ci.RUNNER_HASH,'compose_file':{'docker_compose_file':'services:\n  agent-runner:\n    image: ghcr.io/teleport-computer/feedling-agent-runner:08b2629\n'}}
 digest=ci.DIGEST
 if field in d:d[field]=value
 elif field=='image':d['compose_file']['docker_compose_file']='services:\n  agent-runner:\n    image: bad\n'
 else:digest=value
 monkeypatch.setattr(ci,'command',lambda args,**kw:json.dumps(d) if args[0]=='phala' else 'Digest: '+digest)
 with pytest.raises(r.Stop):ci.runner_preflight()

def test_runner_previous_is_fixed_old_reviewed_stage(monkeypatch,tmp_path):
 monkeypatch.setattr(ci,'OUT',tmp_path);monkeypatch.setenv('T800_PREVIOUS_RUN',ci.RUNNER_PREVIOUS_RUN);monkeypatch.setenv('GITHUB_RUN_ID','99999999999')
 monkeypatch.setattr(ci,'api',lambda path:{'jobs':[{'name':'T800 incident recovery','conclusion':'success'}]} if '/jobs?' in path else {'event':'workflow_dispatch','head_sha':ci.RUNNER_PREVIOUS_CODE,'run_attempt':1,'display_title':'T800 recovery runner'})
 def download(*a,**kw):
  from pathlib import Path
  d=Path(a[0][-1]);d.mkdir(exist_ok=True)
  (d/'stage.json').write_text(json.dumps({'stage':'runner','code_sha':ci.RUNNER_PREVIOUS_CODE,'source':r.SOURCE,'pin':r.PIN,'result':'STAGE_COMPLETE'}))
  (d/'runner-next.json').write_text(json.dumps({'runner':ci.RUNNER,'compose_hash':ci.RUNNER_HASH,'result':'FREEZE_AND_REVIEW_RUNNER_AUTHORIZATION','deployment_acceptance':False}))
 monkeypatch.setattr(ci,'command',download)
 ci.previous('authorize-runner','a'*40)
 monkeypatch.setenv('T800_PREVIOUS_RUN','38078351674')
 with pytest.raises(r.Stop,match='runner_prior_run_changed'):ci.previous('authorize-runner','a'*40)

def test_runner_history_has_no_main_zero_send_exception(monkeypatch):
 monkeypatch.setenv('GITHUB_RUN_ID','99999999999')
 monkeypatch.setattr(ci,'api',lambda path:{'workflow_runs':[{'id':ci.RECONCILED_RUN,'display_title':'T800 recovery authorize-runner'}]})
 with pytest.raises(r.Stop,match='prior_attempt'):ci.no_prior_attempt('authorize-runner')

def test_ci_runner_prepare_archive_send(monkeypatch,tmp_path):
 from types import SimpleNamespace
 monkeypatch.setattr(ci,'OUT',tmp_path)
 monkeypatch.setenv('PRIVATE_KEY','fake');monkeypatch.setenv('ETH_SEPOLIA_RPC_URL','https://fake.invalid')
 f=RunnerRPC();s=Signer();monkeypatch.setattr(r,'Account',SimpleNamespace(from_key=lambda _:s));monkeypatch.setattr(r,'RPC',lambda _:f)
 monkeypatch.setattr(ci,'runner_preflight',lambda:None)
 seed=RunnerRPC();r.recover(seed,s,640,seed.save,target=ci.RUNNER_TARGET,preflight=lambda:None,prepare_only=True)
 monkeypatch.setattr(ci,'RUNNER_FAILED_INTENT',seed.saved['intent'])
 ci.main_transaction('prepare',runner=True)
 assert f.sent==0
 f.saved['intent']=json.loads((tmp_path/'intent.json').read_text())
 assert f.saved['intent']['compose_hash']==ci.RUNNER_HASH and f.saved['intent']['nonce']==640
 archived=[];monkeypatch.setattr(ci,'archived_intent',lambda:archived.append(True))
 ci.main_transaction('send',runner=True)
 assert f.sent==1 and archived==[True] and s.tx['data']==ci.RUNNER_DATA
 assert json.loads((tmp_path/'transaction.json').read_text())['sends']==1
