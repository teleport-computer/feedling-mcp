"""T627 alert states, exact offline rendering and network failure boundaries."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
from tools import enclave_decrypt_alert as alert
from tools.strict_yaml import load_yaml_strict

FIXTURE = ROOT / "tests/fixtures/enclave_decrypt_alert/start.json"
GOLDEN = ROOT / "tests/fixtures/enclave_decrypt_alert/start.txt"
WORKFLOW = ROOT / ".github/workflows/enclave-decrypt-monitor.yml"


def fixture():
    return json.loads(FIXTURE.read_text())


def counts(window, *, done=80, timeout=0, transport=0, http403=0):
    unavailable = timeout + transport
    window.update(done=done, timeout=timeout, transport_error=transport,
                  http_401=0,http_403=http403,http_other=0,
                  calls=done+unavailable+http403,unavailable=unavailable,
                  unavailable_rate=unavailable/(done+unavailable) if done+unavailable else None,
                  users_affected=1 if unavailable+http403 else 0,
                  top_purposes=[{"purpose":"memory_action","count":unavailable+http403}] if unavailable+http403 else [])


@pytest.mark.parametrize("cur,prev,minute,expected", [
    (20,0,15,"开始"), (20,20,0,"持续中"), (20,20,15,"静默"),
    (20,20,30,"静默"), (20,20,45,"静默"), (0,20,15,"已恢复"),
    (0,0,0,"静默"), (0,0,15,"静默"),
])
def test_four_branches(cur, prev, minute, expected):
    payload=fixture()
    counts(payload["current"], timeout=cur)
    counts(payload["previous"], timeout=prev)
    alert.validate_health(payload,15)
    assert alert.alert_state(payload,run_minute=minute)==expected


def test_low_volume_timeouts_cannot_trigger_by_rate_alone():
    payload=fixture()
    counts(payload["current"],done=0,timeout=3)
    counts(payload["previous"])
    assert payload["current"]["unavailable_rate"]==1
    assert alert.alert_state(payload,run_minute=0)=="静默"


def test_absolute_count_alone_is_not_enough():
    payload=fixture()
    counts(payload["current"],done=200,timeout=20)
    counts(payload["previous"])
    assert alert.alert_state(payload,run_minute=0)=="静默"


def test_six_hour_403_noise_is_not_unavailable():
    payload=fixture()
    counts(payload["current"],done=24,http403=56)
    counts(payload["previous"])
    alert.validate_health(payload,15)
    assert alert.alert_state(payload,run_minute=0)=="静默"


@pytest.mark.parametrize("minute", [0, 15])
def test_http_only_windows_remain_silent_through_real_fetch_and_validation(minute):
    payload = fixture()
    for period in ("current", "previous"):
        row = payload[period]
        counts(row, done=0, http403=56)
        row.update(http_401=4, http_other=2, calls=62, users_affected=2,
                   top_purposes=[{"purpose": "memory_action", "count": 62}])
        assert row["unavailable_rate"] is None
    alert.validate_health(payload, 15)
    assert alert.alert_state(payload, run_minute=minute) == "静默"
    requests = []

    def opener(request, timeout):
        requests.append(request)
        assert request.get_method() == "GET", "HTTP-only windows must not post to Lark"
        return response(payload)

    out = io.StringIO()
    assert alert.main(["--run-minute", str(minute)], environ=ENV, opener=opener, out=out) == 0
    assert len(requests) == 1
    assert out.getvalue() == "静默：当前无需发送 enclave 告警。\n"


def test_empty_denominator_is_not_claimed_healthy():
    payload=fixture()
    counts(payload["current"],done=0)
    counts(payload["previous"],timeout=20)
    alert.validate_health(payload,15)
    assert alert.alert_state(payload,run_minute=0)=="已恢复"
    assert "不能据此确认服务恢复" in alert.render_message(payload,"已恢复")


def test_fixture_dry_run_matches_golden_byte_for_byte():
    out=io.StringIO()
    assert alert.main(["--fixture",str(FIXTURE),"--dry-run","--run-minute","15"],environ={},out=out)==0
    assert out.getvalue()==GOLDEN.read_text()


def test_tool_is_stdlib_only_in_isolated_interpreter():
    result=subprocess.run([sys.executable,"-S",str(ROOT/'tools/enclave_decrypt_alert.py'),
                           '--fixture',str(FIXTURE),'--dry-run','--run-minute','15'],
                          capture_output=True,text=True,env={"PATH":os.environ["PATH"]})
    assert result.returncode==0,result.stderr
    assert result.stdout==GOLDEN.read_text()


def test_threshold_defaults_and_overrides():
    assert alert.thresholds({})==(15,20,0.20)
    assert alert.thresholds(dict(zip(alert.THRESHOLD_ENV_VARS,("30","40","0.4"))))==(30,40,0.4)
    assert alert.thresholds(dict.fromkeys(alert.THRESHOLD_ENV_VARS,""))==(15,20,0.20)


@pytest.mark.parametrize("name,value", [("ENCLAVE_ALERT_WINDOW_MINUTES","0"),
    ("ENCLAVE_ALERT_WINDOW_MINUTES","1441"),("ENCLAVE_ALERT_MIN_UNAVAILABLE","-1"),
    ("ENCLAVE_ALERT_MIN_UNAVAILABLE","oops"),("ENCLAVE_ALERT_UNAVAILABLE_RATE","nan"),
    ("ENCLAVE_ALERT_UNAVAILABLE_RATE","inf"),("ENCLAVE_ALERT_UNAVAILABLE_RATE","1.1")])
def test_invalid_thresholds_fail_visible(name,value):
    out=io.StringIO()
    assert alert.main(['--fixture',str(FIXTURE),'--dry-run'],environ={name:value},out=out)==1
    assert '[量不到]' in out.getvalue()


class Response(io.BytesIO):
    pass


def response(body):
    return Response(json.dumps(body).encode())


ENV = {'FEEDLING_API_URL':'https://api.invalid','FEEDLING_ADMIN_TOKEN':'admin-secret',
       'LARK_BOT_WEBHOOK':'https://lark.invalid/secret-url','LARK_BOT_SECRET':'signing-secret'}


def test_real_fetch_request_and_signed_delivery_use_separate_credentials():
    calls=[]
    def opener(req,timeout):
        calls.append(req)
        assert timeout==30
        if req.get_method()=='GET':
            assert req.full_url=='https://api.invalid/v1/admin/enclave-decrypt-health?window_minutes=15'
            assert req.get_header('X-admin-token')=='admin-secret'
            return response(fixture())
        assert req.full_url==ENV['LARK_BOT_WEBHOOK']
        assert req.get_header('X-admin-token') is None
        payload=json.loads(req.data)
        assert payload['content']['text']+'\n'==GOLDEN.read_text()
        assert set(payload)=={'msg_type','content','timestamp','sign'}
        assert payload==alert.lark_payload(payload['content']['text'],secret='signing-secret',timestamp=payload['timestamp'])
        return response({'code':0})
    assert alert.main(['--run-minute','15'],environ=ENV,opener=opener,out=io.StringIO())==0
    assert len(calls)==2


def test_signature_matches_existing_lark_scheme():
    from tools import memory_pipeline_daily_report
    assert alert.lark_payload('hello',secret='secret',timestamp='123')==memory_pipeline_daily_report.lark_payload('hello',secret='secret',timestamp='123')


@pytest.mark.parametrize('error',[TimeoutError('secret-body'),urllib.error.URLError('secret-url'),
                                  urllib.error.HTTPError('secret-url',503,'secret-body',{},None),
                                  ValueError('secret-payload')])
def test_fetch_failure_notifies_unmeasured_and_exits_nonzero(error,capsys):
    sent=[]
    def opener(req,timeout):
        if req.get_method()=='GET': raise error
        sent.append(json.loads(req.data)['content']['text'])
        return response({'code':0})
    assert alert.main([],environ=ENV,opener=opener,out=io.StringIO())==1
    assert len(sent)==1 and '[量不到]' in sent[0]
    assert type(error).__name__ in sent[0]
    assert 'secret-' not in sent[0]+capsys.readouterr().err


@pytest.mark.parametrize('reply',[{},[],{'code':1},{'code':'0'},{'code':False},
                                  {'StatusCode':0,'code':1}])
def test_lark_rejection_is_not_delivery(reply,capsys):
    assert alert.main(['--fixture',str(FIXTURE)],environ=ENV,
                      opener=lambda *a:response(reply),out=io.StringIO())==3
    assert 'lark post failed' in capsys.readouterr().err


@pytest.mark.parametrize('reply',[{'code':0},{'StatusCode':0},{'code':0,'StatusCode':0}])
def test_lark_explicit_success(reply):
    assert alert.main(['--fixture',str(FIXTURE)],environ=ENV,
                      opener=lambda *a:response(reply),out=io.StringIO())==0


def test_lark_network_error_and_missing_config_are_nonzero(capsys):
    def fail(*a): raise OSError('webhook-secret')
    assert alert.main(['--fixture',str(FIXTURE)],environ=ENV,opener=fail)==3
    assert 'webhook-secret' not in capsys.readouterr().err
    assert alert.main(['--fixture',str(FIXTURE)],environ={})==2


@pytest.mark.parametrize('corrupt',[
    lambda p:p.pop('previous'),
    lambda p:p['current'].update(unavailable_rate=0),
    lambda p:p['current'].update(calls=1),
    lambda p:p['current'].update(users_affected=999),
    lambda p:p['current'].update(top_purposes=[{'purpose':'usr_secret','count':1}]),
    lambda p:p['current'].update(top_purposes=[{'purpose':'memory_action','count':1,'detail':'secret'}]),
    lambda p:p['previous'].update(end_at=p['current']['end_at']),
    lambda p:p.update(user_id='secret'),
])
def test_bad_api_evidence_notifies_instead_of_silent_zero(corrupt):
    payload=fixture();corrupt(payload)
    sent=[]
    def opener(req,timeout):
        if req.get_method()=='GET': return response(payload)
        sent.append(json.loads(req.data)['content']['text'])
        return response({'code':0})
    assert alert.main([],environ=ENV,opener=opener,out=io.StringIO())==1
    assert len(sent)==1 and '[量不到]' in sent[0] and 'secret' not in sent[0]


def test_silent_branch_makes_no_lark_request(tmp_path):
    payload=fixture()
    counts(payload['current']);counts(payload['previous'])
    path=tmp_path/'quiet.json';path.write_text(json.dumps(payload))
    def forbidden(*a): pytest.fail('silent branch sent a message')
    assert alert.main(['--fixture',str(path)],environ={},opener=forbidden,out=io.StringIO())==0


def test_current_minute_is_only_manual_default(tmp_path):
    payload=fixture();counts(payload['current'],timeout=20);counts(payload['previous'],timeout=20)
    path=tmp_path/'ongoing.json';path.write_text(json.dumps(payload))
    out=io.StringIO()
    assert alert.main(['--fixture',str(path),'--dry-run'],environ={},out=out,
                      now=datetime(2026,9,17,2,0,tzinfo=timezone.utc))==0
    assert '[持续中]' in out.getvalue()


def test_workflow_schedule_and_environment_contract(tmp_path):
    workflow=load_yaml_strict(WORKFLOW.read_text())
    assert [s['cron'] for s in workflow.get('on',workflow.get(True))['schedule']]==['0 * * * *','15,30,45 * * * *']
    assert workflow['permissions']=={'contents':'read'}
    step=workflow['jobs']['monitor']['steps'][-1]
    for name in alert.THRESHOLD_ENV_VARS:
        assert step['env'][name]=='${{ vars.'+name+' }}'
    # Execute the real workflow shell with a fake python command that records argv.
    fake=tmp_path/'python3'
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    fake.chmod(0o755)
    for schedule,expected in [('0 * * * *','0'),('15,30,45 * * * *','15'),('',None)]:
        env={'PATH':str(tmp_path)+os.pathsep+os.environ['PATH'],
             'ALERT_SCHEDULE':schedule,'ALERT_DRY_RUN':'true'}
        r=subprocess.run(['bash','-c',step['run']],env=env,capture_output=True,text=True)
        assert r.returncode==0,r.stderr
        assert r.stdout.splitlines()==['tools/enclave_decrypt_alert.py']+(
            ['--run-minute',expected] if expected else [])+['--dry-run']
