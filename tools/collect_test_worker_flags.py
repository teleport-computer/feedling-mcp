"""Fixed-Test, read-only process observation collector. No model/fixture/exec API."""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import math
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = 'https://test-api.feedling.app'
TEST_CVM_ID = '5bfa1543-c5b4-42ca-842d-fd88984e5edf'


def _fresh(value, now):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= now-value <= 30


def _process(value, expected_sha, now, *, parent=None):
    if not isinstance(value, dict) or value.get('status') != 'observed':
        raise ValueError('process_unmeasured')
    if (value.get('full_sha') != expected_sha or not _fresh(value.get('observed_at_epoch'), now)
            or value.get('capture_enabled') is not True or value.get('plaintext_recall') != 'on'):
        raise ValueError('flags_release_or_time_mismatch')
    if any(type(value.get(k)) is not int or value[k] < (0 if k=='ppid' else 1) for k in ('pid','ppid','start_ticks')):
        raise ValueError('invalid_process_identity')
    if parent is not None and (value['ppid'] != parent['pid'] or value['pid'] == parent['pid']):
        raise ValueError('wrong_child_parent')
    return {k:value[k] for k in ('pid','ppid','start_ticks','full_sha','capture_enabled','plaintext_recall','observed_at_epoch')}


def project(health, metrics, expected_sha, *, now=None):
    now = time.time() if now is None else now
    if not re.fullmatch('[0-9a-f]{40}', expected_sha):
        raise ValueError('expected_sha_required')
    if health.get('status') != 'healthy' or health.get('release', {}).get('git_commit') != expected_sha:
        raise ValueError('health_release_mismatch')
    workers = [w for w in metrics.get('worker_heartbeats', []) if w.get('pool')=='foreground']
    if len(workers) != 1:
        raise ValueError('ambiguous_foreground')
    worker = workers[0]
    expected_id = r'v2-fleet-cvm-'+re.escape(TEST_CVM_ID)+'-build-'+expected_sha[:7]+r'-boot-[0-9a-f]{12}:foreground'
    if not re.fullmatch(expected_id, str(worker.get('worker_id',''))):
        raise ValueError('wrong_inventory_or_worker_boot')
    age = worker.get('age_sec')
    if type(age) not in (int,float) or not math.isfinite(age) or not 0 <= age <= 30:
        raise ValueError('stale_worker')
    state = worker.get('runtime_state', {})
    memory = state.get('memory', {})
    flags = memory.get('process_flags', {})
    if (flags.get('schema') != 1 or flags.get('environment') != 'test' or flags.get('cvm_id') != TEST_CVM_ID
            or flags.get('source') != 'owned_process_initial_environment'
            or flags.get('parent_capture_runtime_enabled') is not True
            or memory.get('release') != expected_sha[:12] or not _fresh(memory.get('sampled_at'),now)):
        raise ValueError('missing_process_source')
    parent = _process(flags.get('parent'), expected_sha, now)
    if memory.get('parent',{}).get('pid') != parent['pid']:
        raise ValueError('parent_identity_mismatch')
    children = []
    for slot in memory.get('slots', []):
        name = slot.get('slot','')
        if isinstance(name,str) and name.startswith('foreground:'):
            if not re.fullmatch(r'foreground:\d{1,2}',name):
                raise ValueError('invalid_slot')
            child = _process(slot.get('process_flags'), expected_sha, now, parent=parent)
            if child['pid'] != slot.get('pid'):
                raise ValueError('slot_pid_mismatch')
            children.append({'slot':name, **child})
    configured = state.get('slots',{}).get('configured')
    if (type(configured) is not int or not 1 <= configured <= 16 or len(children)!=configured
            or len({c['pid'] for c in children})!=len(children)
            or len({c['slot'] for c in children})!=len(children)):
        raise ValueError('incomplete_foreground_processes')
    hostname = flags.get('container_namespace_hostname','')
    if not isinstance(hostname,str) or not re.fullmatch('[A-Za-z0-9_.-]{1,128}',hostname) or hostname=='unknown':
        raise ValueError('namespace_identity_missing')
    return {'schema':'T793-reviewed-process-observation-v1','environment':'test','full_sha':expected_sha,
            'worker_id':worker['worker_id'],'observed_at_epoch':now,'cvm_id':TEST_CVM_ID,
            'container_namespace_hostname':hostname,'parent':parent,'foreground_children':children,
            'source':'owned_process_initial_environment','status':'OBSERVED_REQUIRED_FLAGS',
            'launcher_receipt':False,'limits':'Authenticated worker diagnostic projection, not hardware attestation or Docker container ID. Root reviews fresh boot/PID/start binding before launcher admission.'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def collect(expected_sha, password):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
              urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())
    def request(path, data=None):
        req = urllib.request.Request(BASE+path, data=data)
        try:
            with opener.open(req, timeout=15) as response:
                body = response.read(1_048_577)
                if len(body)>1_048_576:
                    raise ValueError('oversized_response')
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            if path=='/admin/login' and exc.code==303:
                return None
            raise RuntimeError('fixed_endpoint_failed') from None
    before = request('/healthz')
    if before.get('status')!='healthy' or before.get('release',{}).get('git_commit')!=expected_sha:
        raise ValueError('health_release_mismatch')
    # Stateless existing admin authentication. No account/fixture/config writes.
    request('/admin/login', urllib.parse.urlencode({'password':password}).encode())
    metrics = request('/v1/admin/v2-metrics')
    result = project(before, metrics, expected_sha)
    after = request('/healthz')
    if after.get('status')!='healthy' or after.get('release',{}).get('git_commit')!=expected_sha:
        raise ValueError('release_changed')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-sha',required=True)
    parser.add_argument('--run',action='store_true')
    args = parser.parse_args(argv)
    if not re.fullmatch('[0-9a-f]{40}',args.expected_sha):
        raise ValueError('invalid_sha')
    if not args.run:
        print('PLAN_ONLY: fixed test target, no authentication or requests')
        return 0
    if os.environ.get('GITHUB_REF')!='refs/heads/test' or os.environ.get('GITHUB_EVENT_NAME')!='workflow_dispatch':
        raise ValueError('only_test_manual_dispatch')
    if Path('deploy/test-cvm-id.txt').read_text().strip()!=TEST_CVM_ID:
        raise ValueError('test_inventory_mismatch')
    password = os.environ.get('TEST_ADMIN_PASSWORD')
    if not password:
        raise ValueError('test_admin_password_missing')
    result = collect(args.expected_sha,password)
    with Path('test-worker-flags.json').open('x') as out:
        json.dump(result,out,indent=2)
    print('OBSERVED_REQUIRED_FLAGS: review test-worker-flags.json; no execution admission implied')
    return 0

if __name__=='__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status':'UNMEASURED','error_type':type(exc).__name__}))
        raise SystemExit(2)
