import json
from pathlib import Path
import pytest
import recover as r
from eth_account import Account
from eth_utils import keccak

class Signer:
 address=r.OWNER
 def sign_transaction(self,tx):
  self.tx=tx
  return Account.from_key(bytes.fromhex('01'*32)).sign_transaction(tx)
class Fake:
 def __init__(self,mode='ok'):
  self.mode=mode;self.calls=[];self.saved={};self.nonces=0;self.allowed_reads=0;self.estimates=0;self.sent=0
 def save(self,name,value):self.saved[name]=value
 def __call__(self,m,p):
  self.calls.append((m,p))
  if m=='eth_chainId':return hex(1 if self.mode=='chain' else r.CHAIN)
  if m=='eth_call':
   if p[0]['data']=='0x8da5cb5b':return '0x'+'0'*24+('0'*40 if self.mode=='owner' else r.OWNER[2:])
   if p[0]['data']==r.ALLOWED:
    self.allowed_reads+=1
    if self.mode=='shape':return '0x'
    value=self.mode=='already' or (self.mode=='race_allowed' and self.allowed_reads==2) or (self.sent and self.mode!='readback')
    return '0x'+format(int(value),'064x')
   if self.mode=='simulation':raise r.Stop('rpc_error')
   return '0x'
  if m=='eth_getTransactionCount':
   self.nonces+=1
   return hex(639 if (self.mode=='nonce' or (self.mode=='nonce_race' and self.nonces==3)) else 638)
  if m=='eth_estimateGas':
   self.estimates+=1
   return hex(3000000 if self.mode=='cap' else 1900000 if self.mode=='estimate_race' and self.estimates==2 else 1829803)
  if m=='eth_gasPrice':return hex(100000001 if self.mode=='price' else 1000000)
  if m=='eth_sendRawTransaction':
   assert 'intent' in self.saved
   self.sent+=1;self.hash='0x'+keccak(bytes.fromhex(p[0][2:])).hex()
   if self.mode=='uncertain':raise TimeoutError('secret should not be logged')
   return '0x'+'0'*64 if self.mode=='send_hash' else self.hash
  if m=='eth_getTransactionReceipt':
   if self.mode=='timeout':return None
   return {'transactionHash':self.hash,'status':'0x0' if self.mode=='receipt' else '0x1','from':r.OWNER,'to':r.CONTRACT,'blockNumber':'0x1','blockHash':'0x'+'1'*64,'gasUsed':'0x1bebab'}
  raise AssertionError(m)
def run(fake):
 signer=Signer()
 return r.recover(fake,signer,638,fake.save,sleep=lambda _:None,preflight=lambda:None),signer

def test_exact_calldata_matches_all_failed_real_transactions():
 data=json.loads(Path(__file__).with_name('failed-transactions.json').read_text())
 assert all(t['tx']['input']==r.DATA for t in data['transactions'])
 assert len(r.PIN)==40 and len(r.SOURCE)==40

def test_measured_failure_regression():
 f=Fake();result,s=run(f)
 assert result['result']=='HASH_AUTHORIZED_ONLY' and result['deployment_acceptance'] is False
 assert s.tx['gas']==2195764 and s.tx['gas']>1829803>1365297
 assert s.tx['chainId']==11155111 and s.tx['nonce']==638 and s.tx['data']==r.DATA
 assert f.sent==1 and f.estimates==2 and f.allowed_reads==3
 assert set(f.saved)=={'intent','receipt'}
 assert f.saved['intent']['maximum_cost_wei']<=300000000000000

@pytest.mark.parametrize('mode',['chain','owner','shape','nonce','nonce_race','cap','simulation','price','estimate_race'])
def test_failures_before_send(mode):
 f=Fake(mode)
 with pytest.raises(r.Stop):run(f)
 assert f.sent==0 and 'intent' not in f.saved
@pytest.mark.parametrize('mode',['already','race_allowed'])
def test_idempotent_no_send(mode):
 f=Fake(mode);result,_=run(f)
 assert result['sends']==0 and f.sent==0
@pytest.mark.parametrize('mode',['uncertain','send_hash','receipt','timeout','readback'])
def test_after_send_never_retries(mode):
 f=Fake(mode)
 with pytest.raises((r.Stop,TimeoutError)):run(f)
 assert f.sent==1 and 'intent' in f.saved
 assert sum(m=='eth_getTransactionReceipt' for m,p in f.calls)<=12
@pytest.mark.parametrize('estimate',[0,-1,True,'12',2500001])
def test_invalid_or_over_cap(estimate):
 with pytest.raises(r.Stop):r.gas_limit(estimate)
def test_rounds_up_with_hard_cap():
 assert r.gas_limit(1)==2
 assert r.gas_limit(2500000)==3000000

def test_wrong_signer_cannot_touch_rpc():
 f=Fake();a=Account.from_key(bytes.fromhex('01'*32))
 with pytest.raises(r.Stop,match='signer'):r.recover(f,a,638,f.save)
 assert not f.calls

def test_default_plan_no_credentials_network(tmp_path,monkeypatch,capsys):
 monkeypatch.setattr('sys.argv',['recover.py'])
 monkeypatch.delenv('PRIVATE_KEY',raising=False)
 assert r.main()==0
 assert capsys.readouterr().out=='PLAN_ONLY no credentials/network/send\n'


def test_preflight_rechecked_before_send():
 f=Fake();calls=[]
 def check():
  calls.append(1)
  if len(calls)==2:raise r.Stop('main_version_changed')
 with pytest.raises(r.Stop,match='version_changed'):
  r.recover(f,Signer(),638,f.save,preflight=check)
 assert len(calls)==2 and f.sent==0

@pytest.mark.parametrize('changed',['main','compose','source'])
def test_live_version_guard(monkeypatch,changed):
 from types import SimpleNamespace
 def process(args,**kwargs):
  if args[0]=='gh':return SimpleNamespace(stdout=('0'*40 if changed=='main' else r.PIN))
  return SimpleNamespace(stdout=json.dumps({'compose_hash':'0'*64 if changed=='compose' else r.HASH}))
 class Response:
  def __enter__(self):return self
  def __exit__(self,*args):pass
  def read(self,*args):return json.dumps({'release':{'git_commit':'0'*40 if changed=='source' else r.SOURCE}}).encode()
 monkeypatch.setattr(r.subprocess,'run',process)
 monkeypatch.setattr(r.urllib.request,'build_opener',lambda *a:SimpleNamespace(open=lambda *a,**k:Response()))
 monkeypatch.setenv('PHALA_CLOUD_API_KEY','fake-not-secret')
 with pytest.raises(r.Stop,match='changed'):r.live_preflight()

def test_attempt_latch_rejects_before_network(tmp_path,monkeypatch,capsys):
 (tmp_path/'ATTEMPT-CLAIMED').write_text('claimed')
 monkeypatch.setattr(r,'__file__',str(tmp_path/'recover.py'))
 monkeypatch.setattr('sys.argv',['recover.py','--run','--expected-nonce','638','--out',str(tmp_path/'new')])
 monkeypatch.delenv('PRIVATE_KEY',raising=False)
 assert r.main()==2
 assert not (tmp_path/'new').exists()
 assert 'do not resend' in capsys.readouterr().out
