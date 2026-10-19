import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

from tools.release_window.deadlines import Deadline, PhaseExpired, validate_window
from tools.release_window import bounded_v2 as v2


def test_absolute_window_order_and_missing():
    for value in [None, {}, dict(latest_start_epoch=1, execution_end_epoch=1, cleanup_end_epoch=2),
                  dict(latest_start_epoch=True, execution_end_epoch=2, cleanup_end_epoch=3)]:
        with pytest.raises(ValueError):
            validate_window(value)


def test_stalled_preflight_cannot_restart_remaining_time():
    now = [10.0]
    d = Deadline(20, wall=lambda:now[0], clock=lambda:now[0], schedule=False)
    now[0] = 21
    reads = v2.Reads(10, d)
    with pytest.raises(PhaseExpired):
        reads.use()
    assert reads.remaining == 10


def test_wall_clock_rollback_cannot_extend_deadline():
    wall, mono = [10.0], [10.0]
    d = Deadline(20, wall=lambda:wall[0], clock=lambda:mono[0], schedule=False)
    wall[0], mono[0] = 1, 21
    with pytest.raises(PhaseExpired): d.remaining()


def test_active_transport_abort_does_not_wait_for_forward_lock():
    started, closed = threading.Event(), threading.Event()
    admission_lock = threading.Lock()
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            started.set()
            assert closed.wait(2), 'deadline did not abort active stream'
            raise httpx.ReadError('synthetic local abort')
            yield b''
        def close(self): closed.set()
    d = Deadline(time.time()+60)
    client = d.bind(httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200, stream=Stream()))))
    def request():
        with admission_lock:
            with pytest.raises(httpx.ReadError): client.get('https://synthetic.invalid')
    t=threading.Thread(target=request);t.start();assert started.wait(2)
    assert not admission_lock.acquire(blocking=False)
    d.expire()
    assert closed.wait(1)
    t.join(2);assert not t.is_alive();d.cancel();client.close()


def test_request_phase_boundary_refuses_without_transport_call():
    now=[10.0];calls=[]
    d=Deadline(20,wall=lambda:now[0],clock=lambda:now[0],schedule=False)
    client=d.bind(httpx.Client(transport=httpx.MockTransport(lambda r:calls.append(r))))
    now[0]=20
    with pytest.raises(PhaseExpired):client.get('https://synthetic.invalid')
    assert calls==[]
    client.close()


def test_cleanup_deadline_retains_fixture_no_reset(tmp_path):
    calls=[]
    owner=SimpleNamespace(manifest={'limits':{'cleanup_seconds':180,'drain_seconds':120},
        'execution_window':{'cleanup_end_epoch':time.time()-1}},verify=lambda:calls.append('verify'))
    with pytest.raises(RuntimeError,match='retain fixture'):
        v2.cleanup(owner,None,None)
    assert calls==[]


def test_late_start_before_claim_or_output(tmp_path,monkeypatch):
    m={'execution_window':{'latest_start_epoch':time.time()-1}}
    monkeypatch.setattr(v2,'validate_manifest',lambda value:value)
    monkeypatch.setattr(v2.receipts,'verify_input_count',lambda _:pytest.fail('receipt or network must not run'))
    with pytest.raises(RuntimeError,match='latest start'):v2.execute(m,tmp_path/'out')
    assert not(tmp_path/'out').exists()


def test_provision_timeout_keeps_known_recovery_record(tmp_path, monkeypatch):
    from tools.e2e import client as c
    now=[10.0]; d=Deadline(20,wall=lambda:now[0],clock=lambda:now[0],schedule=False)
    calls=[]
    def dispatch(req):
        calls.append(req.url.path)
        if req.url.path.endswith('/register'):
            return httpx.Response(200,json={'user_id':'synthetic-owned','api_key':'synthetic'})
        now[0]=21
        return httpx.Response(200,json={})
    real=httpx.Client
    monkeypatch.setattr(c.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(dispatch),**kw))
    recovery=tmp_path/'recovery.json'
    def persist(*args,**kwargs):
        recovery.write_text('synthetic recovery retained');return recovery
    monkeypatch.setattr(c,'_write_orphan_manifest',persist)
    with pytest.raises(PhaseExpired):
        c.E2EClient.provision(route='model_api',api_url='https://test-api.feedling.app',
                             defer_failure_cleanup=True,http_guard=d)
    assert recovery.exists() and len(calls)==2


@pytest.mark.parametrize('failure', ['cleanup', 'provision'])
def test_cleanup_failure_records_incomplete_and_recovery(tmp_path,monkeypatch,failure):
    from tools.release_window import bounded_proxy as proxy, receipts
    now=time.time();events=[]
    m={'execution_window':dict(latest_start_epoch=now+10,execution_end_epoch=now+30,cleanup_end_epoch=now+60),
       'limits':{'read_requests':50},'api_url':'https://test-api.feedling.app','lane':'openrouter-v2-text',
       'relay_origin':'https://synthetic.invalid','relay_port':12345}
    run=SimpleNamespace(token='synthetic',probe_nonce='nonce',probe_ack='ack',probe_received=True,
        attempts=0,lock=threading.Lock(),relay_path='/v1/chat/completions',closing=threading.Event(),
        close=lambda:events.append('admission_closed'),failure_reason=None,billing_bound_breached=False)
    monkeypatch.setattr(proxy,'Run',lambda **kw:run)
    class Server:
        def __init__(self,*a):pass
        def serve_forever(self):pass
        def shutdown(self):pass
        def server_close(self):pass
    monkeypatch.setattr(proxy,'ThreadingHTTPServer',Server)
    original=httpx.Client
    def dispatch(req):
        return httpx.Response(200,json={'nonce':'nonce','ack':'ack'})
    monkeypatch.setattr(v2,'httpx',SimpleNamespace(Client=lambda **kw:original(transport=httpx.MockTransport(dispatch),**kw)))
    monkeypatch.setattr(v2,'candidate',lambda *a:'worker')
    monkeypatch.setattr(receipts,'preflight',lambda **kw:{})
    monkeypatch.setattr(receipts,'resident_guard',lambda *a,**kw:{})
    client=SimpleNamespace(user_id='synthetic',_http=original(transport=httpx.MockTransport(dispatch)))
    def provision(**kw):
        if failure=='provision':raise PhaseExpired('registration outcome unknown')
        return client
    monkeypatch.setattr(v2.E2EClient,'provision',provision)
    owner=SimpleNamespace(client=client,verify=lambda:None,save=lambda name,value:events.append(name))
    monkeypatch.setattr(receipts,'Owner',lambda *a:owner)
    monkeypatch.setattr(v2,'exercise',lambda *a,**kw:{'status':'PASS_SHORT_VISIBLE_ONLY'})
    def expire(*a,**kw):raise PhaseExpired('cleanup boundary')
    monkeypatch.setattr(v2,'cleanup',expire)
    monkeypatch.setenv('FEEDLING_ADMIN_KEY','synthetic');monkeypatch.setenv('E2E_KEY_OPENROUTER','synthetic')
    result={'status':'INCOMPLETE','cleanup':'NOT_ATTEMPTED'};progress={}
    v2._execute_claimed(m,tmp_path,None,result,progress)
    assert result['status']=='INCOMPLETE' and result['recovery_required'] is True
    assert 'admission_closed' in events
    if failure=='cleanup':
        assert result['cleanup']=='FAILED' and result['cleanup_error_type']=='PhaseExpired'
        assert 'scenario-result' in events
    else:
        assert result['cleanup']=='INCOMPLETE_REGISTRATION_OUTCOME'
        assert result['fixture_ownership']=='REGISTRATION_RESULT_UNCERTAIN_UNTIL_OWNED_RECEIPT'
