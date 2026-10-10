"""Fixed T800 Sepolia authorization recovery. PLAN_ONLY unless root runs --run."""
import argparse, hashlib, json, os, signal, subprocess, time, urllib.request
from pathlib import Path
from eth_account import Account
from eth_abi import encode
from eth_utils import keccak
CHAIN=11155111
CONTRACT='0x6c8A6f1e3eD4180B2048B808f7C4b2874649b88F'
OWNER='0xf504c2868a958f2874a406081972053c50d64f44'
HASH='c8623ff881076d067875a6643cb894f504476d5346338077a5b686fab4469ebe'
PIN='1962fba419350d5bb33738cda94342df81ccd931'
SOURCE='08b2629670ab753413bfe804b99b90697bc1c579'
YAML='https://github.com/teleport-computer/feedling-mcp/raw/'+PIN+'/deploy/docker-compose.phala.yaml'
DATA='0x'+(keccak(text='addComposeHash(bytes32,string,string)')[:4]+encode(['bytes32','string','string'],[bytes.fromhex(HASH),PIN,YAML])).hex()
ALLOWED='0x'+keccak(text='isAppAllowed(bytes32)')[:4].hex()+HASH
MAX_GAS=3000000
MAX_GAS_PRICE=100000000 # 0.1 gwei => maximum .0003 Sepolia ETH, not gas-used estimate
class Stop(Exception):pass
class RPC:
 def __init__(self,url):
  if not url.startswith('https://'):raise Stop('https_required')
  self.url=url;self.count=0
  self.open=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
 def __call__(self,method,params):
  self.count+=1
  if self.count>50:raise Stop('rpc_cap')
  req=urllib.request.Request(self.url,data=json.dumps({'jsonrpc':'2.0','id':self.count,'method':method,'params':params}).encode(),headers={'Content-Type':'application/json'})
  with self.open.open(req,timeout=10) as r:
   raw=r.read(65537)
   if len(raw)>65536:raise Stop('rpc_body_cap')
  d=json.loads(raw)
  if 'error' in d:raise Stop('rpc_error')
  if 'result' not in d:raise Stop('rpc_shape')
  return d['result']
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*a,**k):raise Stop('rpc_redirect')
def gas_limit(estimate):
 if type(estimate) is not int or estimate<=0:raise Stop('invalid_estimate')
 gas=(estimate*120+99)//100
 if gas>MAX_GAS:raise Stop('gas_cap')
 return gas
def live_preflight():
 # Root must serialize the rollout: independent services cannot offer atomic cross-service CAS.
 head=subprocess.run(['gh','api','repos/teleport-computer/feedling-mcp/git/ref/heads/main','--jq','.object.sha'],capture_output=True,text=True,timeout=20,check=True).stdout.strip()
 if head!=PIN:raise Stop('main_version_changed')
 cvm=subprocess.run(['phala','cvms','get','0711c9a4-afdc-40c6-ba49-d8cb95f7e850','-j','--api-key',os.environ['PHALA_CLOUD_API_KEY']],capture_output=True,text=True,timeout=20,check=True)
 if json.loads(cvm.stdout).get('compose_hash')!=HASH:raise Stop('live_compose_changed')
 opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
 with opener.open('https://api.feedling.app/healthz',timeout=10) as res:
  raw=res.read(65537)
  if len(raw)>65536:raise Stop('health_body_cap')
 health=json.loads(raw)
 if health.get('release',{}).get('git_commit')!=SOURCE:raise Stop('live_source_changed')

def recover(rpc,account,expected_nonce,save,sleep=time.sleep,preflight=live_preflight, prepare_only=False, frozen_intent=None):
 if account.address.lower()!=OWNER:raise Stop('signer_mismatch')
 preflight()
 if int(rpc('eth_chainId',[]),16)!=CHAIN:raise Stop('chain_mismatch')
 def gates():
  owner=rpc('eth_call',[{'to':CONTRACT,'data':'0x8da5cb5b'},'latest'])
  if owner.lower()!='0x'+'0'*24+OWNER[2:]:raise Stop('owner_mismatch')
  allowed=rpc('eth_call',[{'to':CONTRACT,'data':ALLOWED},'latest'])
  if allowed not in ('0x'+'0'*64,'0x'+'0'*63+'1'):raise Stop('allowed_shape')
  return allowed.endswith('1')
 if gates():return {'result':'ALREADY_ALLOWED_NO_SEND','sends':0}
 latest=int(rpc('eth_getTransactionCount',[OWNER,'latest']),16)
 pending=int(rpc('eth_getTransactionCount',[OWNER,'pending']),16)
 if latest!=pending or pending!=expected_nonce:raise Stop('nonce_conflict')
 call={'from':OWNER,'to':CONTRACT,'data':DATA,'value':'0x0','gas':hex(MAX_GAS)}
 estimate=int(rpc('eth_estimateGas',[call,'latest']),16);gas=gas_limit(estimate)
 call['gas']=hex(gas)
 if rpc('eth_call',[call,'latest'])!='0x':raise Stop('simulation_shape')
 price=int(rpc('eth_gasPrice',[]),16)*2
 if not 0<price<=MAX_GAS_PRICE:raise Stop('gas_price_cap')
 if frozen_intent is not None:
  if not price<=frozen_intent['gas_price']<=MAX_GAS_PRICE or not gas<=frozen_intent['gas']<=MAX_GAS:raise Stop('frozen_limits_insufficient')
  price=frozen_intent['gas_price'];gas=frozen_intent['gas'];call['gas']=hex(gas)
 preflight()
 # Re-read authorization and nonce immediately before signing; no concurrent sender allowed.
 if gates():return {'result':'ALREADY_ALLOWED_NO_SEND','sends':0}
 if int(rpc('eth_getTransactionCount',[OWNER,'pending']),16)!=expected_nonce:raise Stop('nonce_changed')
 # Re-estimate right before signing; changing conditions must not bypass the cap.
 fresh=int(rpc('eth_estimateGas',[call,'latest']),16)
 if gas_limit(fresh)>gas:raise Stop('estimate_increased')
 tx={'chainId':CHAIN,'nonce':expected_nonce,'to':CONTRACT,'data':DATA,'value':0,'gas':gas,'gasPrice':price}
 signed=account.sign_transaction(tx);txhash='0x'+signed.hash.hex().removeprefix('0x')
 # Persist exact identity before the only send. Never save key/raw signed transaction.
 save('intent',{'hash':txhash,'chain':CHAIN,'contract':CONTRACT,'compose_hash':HASH,'source':SOURCE,'pin':PIN,'calldata_sha256':hashlib.sha256(bytes.fromhex(DATA[2:])).hexdigest(),'nonce':expected_nonce,'estimate':estimate if frozen_intent is None else frozen_intent['estimate'],'fresh_estimate':fresh if frozen_intent is None else frozen_intent['fresh_estimate'],'gas':gas,'gas_price':price,'maximum_cost_wei':gas*price})
 if prepare_only:return {'result':'INTENT_ONLY','sends':0,'hash':txhash}
 result=rpc('eth_sendRawTransaction',['0x'+signed.raw_transaction.hex().removeprefix('0x')])
 if result.lower()!=txhash.lower():raise Stop('send_hash_mismatch')
 for _ in range(12):
  receipt=rpc('eth_getTransactionReceipt',[txhash])
  if receipt is not None:
   safe={k:receipt.get(k) for k in ['transactionHash','status','blockHash','blockNumber','gasUsed','from','to']};save('receipt',safe)
   if receipt.get('transactionHash','').lower()!=txhash.lower() or receipt.get('to','').lower()!=CONTRACT.lower() or receipt.get('from','').lower()!=OWNER:raise Stop('receipt_identity')
   if receipt.get('status')!='0x1':raise Stop('receipt_failed')
   if not gates():raise Stop('authorization_readback_failed')
   return {'result':'HASH_AUTHORIZED_ONLY','hash':txhash,'sends':1,'deployment_acceptance':False}
  sleep(5)
 raise Stop('receipt_timeout_do_not_resend')
def main():
 p=argparse.ArgumentParser();p.add_argument('--run',action='store_true');p.add_argument('--expected-nonce',type=int);p.add_argument('--out',type=Path);a=p.parse_args()
 if not a.run:print('PLAN_ONLY no credentials/network/send');return 0
 if a.expected_nonce is None or a.expected_nonce<0 or not a.out:p.error('fresh --out and root-reviewed --expected-nonce required')
 os.umask(0o077)
 # Incident-wide durable single-attempt latch; never delete automatically, even on failure.
 lock=Path(__file__).resolve().parent/'ATTEMPT-CLAIMED'
 try:
  with lock.open('x') as f:f.write('root recovery attempt claimed; reconcile before any new authorization\n');f.flush();os.fsync(f.fileno())
 except FileExistsError:
  print('STOP prior attempt exists; reconcile, do not resend');return 2
 a.out.mkdir(exist_ok=False)
 def save(n,v):
  with (a.out/(n+'.json')).open('x') as f:json.dump(v,f,indent=2);f.flush();os.fsync(f.fileno())
 def expire(*a):raise Stop('operation_timeout_do_not_resend')
 signal.signal(signal.SIGALRM,expire);signal.alarm(150)
 try:
  rpc=RPC(os.environ['ETH_SEPOLIA_RPC_URL']);account=Account.from_key(os.environ['PRIVATE_KEY'])
  result=recover(rpc,account,a.expected_nonce,save)
 except BaseException as e:
  result={'result':'STOP','reason':str(e) if isinstance(e,Stop) else type(e).__name__,'retry_authorized':False}
 finally:signal.alarm(0)
 save('result',result);print(json.dumps(result));return 0 if result['result'] in ('HASH_AUTHORIZED_ONLY','ALREADY_ALLOWED_NO_SEND') else 2
if __name__=='__main__':raise SystemExit('Use reviewed CI entrypoint; direct execution disabled')
