"""Offline boundary regressions, including real HTTPTransport socket I/O."""
import json
import socket
import threading
import time
from contextlib import nullcontext

import httpx
import pytest
from httpcore._backends.sync import SyncStream

from tools.release_window.coordinator import coordinate
from tools.release_window.deadlines import Deadline, PhaseExpired
from tools.release_window.bounded_v2 import Reads
from tools.release_window import receipts


def test_expired_db_does_not_connect_or_begin():
    now=[10.0]; calls=[]
    d=Deadline(20,wall=lambda:now[0],clock=lambda:now[0],schedule=False)
    now[0]=21
    def connector(): calls.append('CONNECT'); return nullcontext()
    with pytest.raises(PhaseExpired):
        receipts.preflight(connector=connector,read_budget=Reads(10,d))
    assert calls==[]


@pytest.mark.parametrize('boundary',['connect','begin','statement'])
def test_db_late_completion_stops_next_operation(boundary):
    now=[10.0];calls=[]
    d=Deadline(20,wall=lambda:now[0],clock=lambda:now[0],schedule=False)
    class Conn:
        def close(self): pass
        def transaction(self):
            calls.append('BEGIN')
            if boundary=='begin': now[0]=21
            return nullcontext()
        def execute(self,sql,*args):
            calls.append(sql)
            if boundary=='statement': now[0]=21
    def connector():
        calls.append('CONNECT')
        if boundary=='connect': now[0]=21
        return nullcontext(Conn())
    with pytest.raises(PhaseExpired):
        receipts.preflight(connector=connector,read_budget=Reads(10,d))
    assert len(calls)=={'connect':1,'begin':4,'statement':2}[boundary]
    assert not any(s.startswith('SELECT') for s in calls)


def test_db_statement_timeout_uses_remaining_budget():
    now=[10.0];calls=[]
    d=Deadline(10.5,wall=lambda:now[0],clock=lambda:now[0],schedule=False)
    conn=type('Conn',(),{'execute':lambda self,s:calls.append(s)})()
    receipts._ReadConnection(conn,Reads(2,d)).execute('SELECT 1')
    assert calls==["SET LOCAL statement_timeout = '500ms'", "SET LOCAL lock_timeout = '500ms'", 'SELECT 1']


def test_coordinator_real_transport_block_late_result_never_upgrades(tmp_path, monkeypatch):
    from tools.release_window import coordinator

    recovery_written = threading.Event()
    write_private_json = coordinator._write_private_json

    def record_write_completion(path, body):
        write_private_json(path, body)
        if path == tmp_path / 'late-worker-recovery.json':
            recovery_written.set()

    monkeypatch.setattr(coordinator, '_write_private_json', record_write_completion)
    local,peer=socket.socketpair(); headers=threading.Event();release=threading.Event(); done=threading.Event()
    class Backend:
        def connect_tcp(self,*args,**kwargs): return SyncStream(local)
    transport=httpx.HTTPTransport(retries=0);transport._pool._network_backend=Backend()
    start=time.time(); guard=Deadline(start+.6)
    client=guard.bind(httpx.Client(transport=transport,trust_env=False))
    def server():
        try:
            peer.recv(65536)
            peer.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n')
            headers.set();time.sleep(.4);peer.sendall(b'x');release.wait(2)
        finally: peer.close()
    def work(result,state,publish):
        state['stage']='blocked_request'
        try: client.get('http://synthetic.invalid/')
        except Exception: pass
        # Simulate a misleading late success; only recovery can preserve it.
        publish({'ownership':'synthetic-owned-recovery'})
        result.update(status='PASS',cleanup='CONFIRMED');done.set()
    thread=threading.Thread(target=server,daemon=True);thread.start()
    try:
        result=coordinate(work,{'execution_end_epoch':start+.6,'cleanup_end_epoch':start+.8},
                          tmp_path,{'status':'INCOMPLETE'}, {})
        assert headers.is_set() and not done.is_set()
        assert time.time()-start < 1.2
        assert result['status']=='INCOMPLETE' and result['io_status']=='IO_UNCONFIRMED'
        terminal=(tmp_path/'coordinator-terminal.json').read_bytes()
        release.set();assert done.wait(2)
        # File creation precedes serialization; wait for the real writer to return.
        assert recovery_written.wait(2)
        assert json.loads((tmp_path/'late-worker-recovery.json').read_text())['status']=='RECOVERY_ONLY_NOT_ACCEPTANCE'
        assert (tmp_path/'coordinator-terminal.json').read_bytes()==terminal
        assert list(tmp_path.glob('owned-recovery-*.json'))
    finally:
        release.set();thread.join(2);guard.cancel();client.close()


def test_coordinator_confirmed_completion_before_cutoff(tmp_path):
    now=time.time()
    def work(result,state,publish):result.update(status='PASS',cleanup='CONFIRMED')
    result=coordinate(work,{'execution_end_epoch':now+10,'cleanup_end_epoch':now+20},tmp_path,{}, {})
    assert result=={'status':'PASS','cleanup':'CONFIRMED'}


def test_executor_blocked_preflight_cannot_start_fixture_or_cleanup(tmp_path,monkeypatch):
    from tools.release_window import bounded_v2 as v2
    started=threading.Event();release=threading.Event();finished=threading.Event();calls=[]
    now=time.time()
    manifest={'execution_window':{'execution_end_epoch':now+.1,'cleanup_end_epoch':now+.25},
              'limits':{'read_requests':10},'api_url':'https://synthetic.invalid'}
    monkeypatch.setenv('FEEDLING_ADMIN_KEY','synthetic')
    monkeypatch.setattr(v2,'candidate',lambda *a:'worker')
    def preflight(**kw):
        started.set();release.wait(2);return {}
    def resident(*a,**kw):
        finished.set();kw['read_budget'].use()
    monkeypatch.setattr(receipts,'preflight',preflight)
    monkeypatch.setattr(receipts,'resident_guard',resident)
    monkeypatch.setattr(v2.E2EClient,'provision',lambda **kw:calls.append('provision'))
    monkeypatch.setattr(v2,'cleanup',lambda *a,**kw:calls.append('cleanup'))
    result={};progress={}
    try:
        v2._execute_claimed(manifest,tmp_path,None,result,progress)
        assert started.is_set() and not finished.is_set() and calls==[]
        assert result['status']=='INCOMPLETE' and result['cleanup']=='CLEANUP_INCOMPLETE'
        release.set();assert finished.wait(2)
        assert calls==[]
    finally:release.set()


def test_db_fetch_completion_is_checked():
    now=[10.0]
    d=Deadline(20,wall=lambda:now[0],clock=lambda:now[0],schedule=False)
    class Cursor:
        def fetchone(self): now[0]=21;return ('stale',)
    with pytest.raises(PhaseExpired):receipts._ReadCursor(Cursor(),d.remaining).fetchone()
