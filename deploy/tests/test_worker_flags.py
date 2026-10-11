import ast
import copy
import importlib.util
import json
from pathlib import Path
import types
import pytest

ROOT = Path(__file__).resolve().parents[2]
def load(name, path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
flags=load('process_flags','backend/model_api_runtime/v2/process_flags.py')
collector=load('collect_flags','tools/collect_test_worker_flags.py')
SHA='a'*40

def make_proc(root,pid,ppid,child=False):
    p=root/str(pid);p.mkdir()
    (p/'stat').write_text(f'{pid} (python) S {ppid} '+'0 '*17+'123 0')
    (p/'cmdline').write_bytes(b'python\0-c\0from multiprocessing.spawn import spawn_main\0' if child else b'python\0backend/model_api_runtime/v2/serve_worker.py\0')
    (p/'environ').write_bytes(b'SECRET=never-output\0FEEDLING_GIT_COMMIT='+SHA.encode()+b'\0FEEDLING_V2_CAPTURE_ENABLED=1\0FEEDLING_V2_PLAINTEXT_RECALL=on\0')
    return p

def test_parent_child_projection_no_secrets(tmp_path):
    make_proc(tmp_path,1,0);make_proc(tmp_path,2,1,True)
    parent=flags.sample(1,1,SHA,proc_root=tmp_path);child=flags.sample(2,1,SHA,proc_root=tmp_path)
    assert parent['status']==child['status']=='observed'
    assert child['ppid']==parent['pid'];assert parent['capture_enabled'] is True
    assert 'never-output' not in json.dumps([parent,child])

@pytest.mark.parametrize('fault',['wrong_parent','wrong_sha','wrong_process','duplicate','oversized','missing'])
def test_proc_failure_is_unavailable(tmp_path,fault):
    p=make_proc(tmp_path,2,1,True)
    if fault=='wrong_parent':(p/'stat').write_text('2 (python) S 99 '+'0 '*17+'123 0')
    if fault=='wrong_process':(p/'cmdline').write_bytes(b'python\0unrelated.py\0')
    if fault=='duplicate':(p/'environ').write_bytes((p/'environ').read_bytes()+b'FEEDLING_GIT_COMMIT='+SHA.encode())
    if fault=='oversized':(p/'environ').write_bytes(b'x'*131073)
    if fault=='missing':(p/'stat').unlink()
    r=flags.sample(2,1,'b'*40 if fault=='wrong_sha' else SHA,proc_root=tmp_path)
    assert r['status']=='unavailable';assert set(r)=={'status','error_type'}

def test_proc_replacement_rejected(tmp_path,monkeypatch):
    make_proc(tmp_path,1,0);states=iter([{'pid':1,'ppid':0,'start_ticks':1},{'pid':1,'ppid':0,'start_ticks':2}])
    monkeypatch.setattr(flags,'_identity',lambda p:next(states))
    assert flags.sample(1,1,SHA,proc_root=tmp_path)['status']=='unavailable'

def valid():
    process={'status':'observed','pid':1,'ppid':0,'start_ticks':2,'observed_at_epoch':100.,'full_sha':SHA,'capture_enabled':True,'plaintext_recall':'on'}
    child={**process,'pid':2,'ppid':1}
    memory={'release':SHA[:12],'sampled_at':100.,'parent':{'pid':1},'process_flags':{'schema':1,'environment':'test','cvm_id':flags.TEST_CVM_ID,'source':'owned_process_initial_environment','parent_capture_runtime_enabled':True,'parent':process,'container_namespace_hostname':'container123'},'slots':[{'slot':'foreground:0','pid':2,'process_flags':child}]}
    worker={'pool':'foreground','worker_id':f'v2-fleet-cvm-{flags.TEST_CVM_ID}-build-{SHA[:7]}-boot-123456abcdef:foreground','age_sec':1,'runtime_state':{'memory':memory,'slots':{'configured':1}}}
    return {'status':'healthy','release':{'git_commit':SHA}},{'worker_heartbeats':[worker]}

def test_collector_allowlist_and_real_child(tmp_path):
    h,m=valid();m['secret']='never-output';m['worker_heartbeats'][0]['runtime_state']['memory']['slots'][0]['process_flags']['raw_env']='never-output'
    r=collector.project(h,m,SHA,now=101)
    assert r['status']=='OBSERVED_REQUIRED_FLAGS';assert r['launcher_receipt'] is False
    assert 'never-output' not in json.dumps(r);assert r['foreground_children'][0]['ppid']==r['parent']['pid']

@pytest.mark.parametrize('fault',['prod','sha','worker','stale','stale_proc','false','unknown','wrong_ppid','missing_child','duplicate_slot','unavailable','bad_capture_runtime','wrong_parent_pid'])
def test_collector_refuses_incomplete_or_cross_target(fault):
    h,m=valid();w=m['worker_heartbeats'][0];mem=w['runtime_state']['memory'];f=mem['process_flags'];child=mem['slots'][0]['process_flags']
    if fault=='prod':f['environment']='prod'
    if fault=='sha':h['release']['git_commit']='b'*40
    if fault=='worker':w['worker_id']=w['worker_id'].replace(flags.TEST_CVM_ID,'prod')
    if fault=='stale':w['age_sec']=31
    if fault=='stale_proc':child['observed_at_epoch']=1
    if fault=='false':child['capture_enabled']=False
    if fault=='unknown':child['plaintext_recall']='unknown'
    if fault=='wrong_ppid':child['ppid']=99
    if fault=='missing_child':mem['slots']=[]
    if fault=='duplicate_slot':mem['slots']*=2;w['runtime_state']['slots']['configured']=2
    if fault=='unavailable':child['status']='unavailable'
    if fault=='bad_capture_runtime':f['parent_capture_runtime_enabled']=False
    if fault=='wrong_parent_pid':mem['parent']['pid']=99
    with pytest.raises(ValueError):collector.project(h,m,SHA,now=101)

def test_producer_gates_other_inventory_and_uses_supervisor_pid(monkeypatch):
    path=ROOT/'backend/model_api_runtime/v2/serve_worker.py'
    tree=ast.parse(path.read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_fleet_memory_snapshot')
    module=types.ModuleType('model_api_runtime.v2');module.process_flags=flags
    monkeypatch.setitem(__import__('sys').modules,'model_api_runtime.v2',module)
    key=types.SimpleNamespace(pool='foreground',index=0)
    fleet=types.SimpleNamespace(keys=lambda:[key],supervisor=lambda k:types.SimpleNamespace(child_pid=lambda:22))
    ns={'os':types.SimpleNamespace(getpid=lambda:11,environ={'FEEDLING_GIT_COMMIT':SHA}),'time':types.SimpleNamespace(time=lambda:100.),'_CAPTURE_ENABLED':True,'v2_process_memory':types.SimpleNamespace(process=lambda p:{},cgroup_kb=lambda:0)}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),str(path),'exec'),ns)
    monkeypatch.setenv('FEEDLING_V2_RUNNER_CVM_ID','prod')
    assert 'process_flags' not in ns['_fleet_memory_snapshot'](fleet)
    calls=[];monkeypatch.setattr(flags,'sample',lambda *args:calls.append(args) or {'status':'unavailable'})
    monkeypatch.setenv('FEEDLING_V2_RUNNER_CVM_ID',flags.TEST_CVM_ID)
    result=ns['_fleet_memory_snapshot'](fleet)
    assert calls==[(22,11,SHA),(11,11,SHA)]
    assert result['process_flags']['parent']['status']=='unavailable'

@pytest.mark.parametrize('ref,event',[('refs/heads/main','workflow_dispatch'),('refs/heads/pre','workflow_dispatch'),('refs/heads/test','push')])
def test_dispatch_gate_before_any_access(monkeypatch,ref,event):
    monkeypatch.setenv('GITHUB_REF',ref);monkeypatch.setenv('GITHUB_EVENT_NAME',event)
    monkeypatch.setattr(collector,'collect',lambda *a:pytest.fail('network entered'))
    with pytest.raises(ValueError):collector.main(['--expected-sha',SHA,'--run'])

def test_workflow_fixed_target_and_secret():
    import yaml
    path=ROOT/'.github/workflows/memory-pipeline-daily.yml'
    w=yaml.load(path.read_text(),Loader=yaml.BaseLoader)
    inputs=w['on']['workflow_dispatch']['inputs']
    assert inputs['operation']['options']==['daily-report','test-worker-flags']
    assert inputs['expected_sha']['type']=='string'
    job=w['jobs']['test-worker-flags']
    assert job['if']=="github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/test' && inputs.operation == 'test-worker-flags'"
    assert "inputs.operation != 'test-worker-flags'" in w['jobs']['report']['if']
    step=next(s for s in job['steps'] if 'run' in s)
    assert step['env']['TEST_ADMIN_PASSWORD']=='${{ secrets.TEST_FEEDLING_ADMIN_PASSWORD }}'
    assert step['run']=='python tools/collect_test_worker_flags.py --expected-sha "$EXPECTED_SHA" --run'
    assert w['permissions']=={'contents':'read'}
    assert not any('LARK' in str(s) or 'deploy' in str(s) for s in job['steps'])

def transport(monkeypatch, *, login_status=303, health_status=200, oversized=False):
    """Exercise urllib's real cookie/redirect processors without opening sockets."""
    import io
    import email.message
    import urllib.response
    import urllib.request
    h,m=valid(); seen=[]
    class FixedHTTPS(urllib.request.HTTPSHandler):
        def https_open(self, request):
            seen.append(request)
            assert request.full_url.startswith(collector.BASE+'/')
            path=request.full_url[len(collector.BASE):]
            headers=email.message.Message()
            if path=='/admin/login':
                assert request.get_method()=='POST'
                assert request.data==b'password=secret-sentinel'
                code=login_status;body=b'secret-sentinel'
                headers['Location']='https://evil.invalid/secret-sentinel'
                headers['Set-Cookie']='admin_session=cookie-sentinel; Path=/; Secure; HttpOnly'
            else:
                code=health_status if path=='/healthz' else 200
                if path=='/v1/admin/v2-metrics':
                    assert request.get_header('Cookie')=='admin_session=cookie-sentinel'
                body=json.dumps(h if path=='/healthz' else m).encode()
                if oversized:body=b'secret-sentinel'+b'x'*1_048_577
            response=urllib.response.addinfourl(io.BytesIO(body),headers,request.full_url,code)
            response.msg='fixture'
            return response
    real_build=urllib.request.build_opener
    monkeypatch.setattr(collector.urllib.request,'build_opener',lambda *a:real_build(*a,FixedHTTPS()))
    monkeypatch.setattr(collector.time,'time',lambda:101.)
    return seen

def test_transport_cookie_303_and_fixed_endpoints(monkeypatch):
    seen=transport(monkeypatch)
    result=collector.collect(SHA,'secret-sentinel')
    assert [r.full_url for r in seen]==[collector.BASE+p for p in ['/healthz','/admin/login','/v1/admin/v2-metrics','/healthz']]
    assert 'sentinel' not in json.dumps(result)

@pytest.mark.parametrize('fault',['redirect','unauthorized','oversized'])
def test_transport_rejects_response_without_secret_or_redirect(monkeypatch,fault):
    seen=transport(monkeypatch,login_status=302 if fault=='redirect' else 303,
                   health_status=401 if fault=='unauthorized' else 200,oversized=fault=='oversized')
    with pytest.raises((RuntimeError,ValueError)) as error:
        collector.collect(SHA,'secret-sentinel')
    assert 'sentinel' not in str(error.value)
    assert all(r.full_url.startswith(collector.BASE+'/') for r in seen)
