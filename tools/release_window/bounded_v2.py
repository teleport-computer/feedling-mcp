"""Adapted bounded V2 executor. Explicit manifest, no default live run.

Safe projections only; the original raw reply/history persistence is removed.
Only a freshly created in-process fixture is accepted; no arbitrary UID resume.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import threading
import time
import uuid

import httpx
from tools.e2e.client import E2EClient, _write_private_json, FALLBACK_TEXTS
from tools.e2e.continuity_probe import _replies_for
from . import bounded_proxy as proxy, receipts, drain
from .contract import validate_manifest, digest, RECEIPTS

def check_candidate(health, metrics, expected):
    if health.get("status") != "healthy" or health.get("release", {}).get("git_commit") != expected:
        raise RuntimeError("candidate health mismatch")
    workers = [w for w in metrics.get("worker_heartbeats", []) if w.get("pool") == "foreground"]
    if len(workers) != 1:
        raise RuntimeError("ambiguous V2 foreground")
    worker = workers[0]
    if not isinstance(worker.get("worker_id"), str) or not worker["worker_id"].strip():
        raise RuntimeError("V2 process identity unmeasured")
    state = worker.get("runtime_state", {})
    age = worker.get("age_sec")
    healthy = state.get("slots", {}).get("healthy")
    if (type(age) not in (int, float) or not 0 <= age <= 30
            or state.get("memory", {}).get("release") != expected[:12]
            or type(healthy) is not int or healthy < 1
            or state.get("configuration", {}).get("trajectory_review_enabled") is not False):
        raise RuntimeError("V2 version/freshness/configuration missing")
    return worker


class Reads:
    def __init__(self, maximum):
        self.remaining = maximum
    def use(self):
        if self.remaining <= 0:
            raise RuntimeError('read budget exhausted')
        self.remaining -= 1


def candidate(h, manifest, reads):
    receipts.verify_environment(manifest)
    reads.use()
    health = h.get('/healthz'); health.raise_for_status()
    reads.use()
    metrics = h.get('/v1/admin/v2-metrics'); metrics.raise_for_status()
    worker = check_candidate(health.json(), metrics.json(), manifest['expected_release'])
    drain.rows(metrics.json(), manifest['drain_scope'])
    return worker['worker_id']


def snapshot(owner, reads, *, connector=receipts.connect):
    return receipts.observe(owner, connector=connector, read_budget=reads)


def selected(row):
    return row['member'] and row['runtime_mode'] == 'db_action_v2' and row['runtime_state'] == 'v2' and not row['resident_lease_active']


def exercise(owner, run, reads, *, connector=receipts.connect, sleep=time.sleep):
    c = owner.client
    owner.verify()
    if c.content_encryption_effective != 'off':
        raise RuntimeError('plaintext fixture required')
    r = c.post('/v1/model_api/setup', json={'provider': 'openai', 'model': run.manifest['model'],
        'api_key': run.token, 'base_url': run.manifest['relay_origin'] + '/v1'}, _retries=1, timeout=30)
    r.raise_for_status()
    setup = r.json()
    status = (setup.get('config') or {}).get('test_status') or setup.get('test_status')
    owner.save('setup', {'phase': 'setup', 'http': r.status_code, 'assertions': {'selftest_ok': status == 'ok'}})
    if status != 'ok':
        raise RuntimeError('setup failed')
    while run.clock() < run.deadline:
        observation = snapshot(owner, reads, connector=connector)
        if selected(observation):
            owner.save('selection', {'observation': observation})
            break
        sleep(run.manifest['limits']['poll_seconds'])
    else:
        raise RuntimeError('V2 convergence deadline')
    marker = 'birch-' + secrets.token_hex(4)
    prompts = [f'Remember the code {marker}. Reply with only that exact code.',
               'What was the code? Reply with only that exact code.']
    for index, prompt in enumerate(prompts, 1):
        run.set_phase('dialogue_' + str(index))
        body = {'message': prompt, 'client_msg_id': str(uuid.uuid4())}
        sent = c.post('/v1/model_api/chat/send', json=body, _retries=1, timeout=15)
        sent.raise_for_status()
        row = sent.json().get('user_message') or {}
        mid = row.get('id')
        if sent.status_code != 202 or not isinstance(mid, str) or not mid or type(row.get('ts')) not in (int,float):
            raise RuntimeError('send not identified')
        duplicate = c.post('/v1/model_api/chat/send', json=body, _retries=1, timeout=15)
        dedup = duplicate.status_code == 202 and (duplicate.json().get('user_message') or {}).get('id') == mid
        owner.save(f'send-{index}', {'phase': f'dialogue_{index}', 'message_id': mid,
                                     'client_msg_id': body['client_msg_id'], 'assertions': {'uuid_dedup': dedup}})
        if not dedup:
            raise RuntimeError('visible dedup failed')
        while run.clock() < run.deadline:
            reads.use()
            history = c.get('/v1/chat/history', params={'since': row['ts']-1, 'limit': 200}, _retries=1, timeout=10)
            history.raise_for_status()
            rows = history.json().get('messages')
            if not isinstance(rows, list) or len(rows) >= 200:
                raise RuntimeError('history missing or truncated')
            replies = _replies_for(rows, mid)
            if replies:
                if len(replies) != 1:
                    raise RuntimeError('multiple correlated replies')
                turn_id = replies[0].get('activity_turn_id') or replies[0].get('reply_to_message_id')
                if turn_id != mid:
                    raise RuntimeError('turn activity identity mismatch')
                reads.use()
                activity = c.get('/v1/chat/turn-activity/' + mid, _retries=1, timeout=10)
                activity.raise_for_status()
                activity = activity.json()
                if not isinstance(activity, dict) or activity.get('complete') is not True:
                    sleep(run.manifest['limits']['poll_seconds'])
                    continue
                reads.use()
                settled = c.get('/v1/chat/history', params={'since':row['ts']-1,'limit':200}, _retries=1, timeout=10)
                settled.raise_for_status()
                rows = settled.json().get('messages')
                if not isinstance(rows, list) or len(rows)>=200:
                    raise RuntimeError('settled history unavailable')
                replies = _replies_for(rows, mid)
                if len(replies)!=1:
                    raise RuntimeError('settled reply identity mismatch')
                text = c.read_reply_strict(replies[0])
                failure = activity.get('failure')
                verdict_ok = failure in (None, {}) and bool(FALLBACK_TEXTS) and text.strip() not in FALLBACK_TEXTS
                assertions = {'readable_known_answer': text.strip() == marker and verdict_ok,
                    'one_reply': True, 'no_system_error': not any(x.get('role') == 'system' for x in rows),
                    'one_user': sum(x.get('id') == mid and x.get('role') == 'user' for x in rows) == 1}
                owner.save(f'reply-{index}', {'phase': f'dialogue_{index}', 'trace_id': mid,
                                             'assertions': assertions})
                if not all(assertions.values()):
                    raise RuntimeError('reply assertion failed')
                break
            sleep(run.manifest['limits']['poll_seconds'])
        else:
            raise RuntimeError('reply deadline')
    run.set_phase('background')
    return {'status': 'PASS_SHORT_VISIBLE_ONLY', 'assertions': {'two_dialogues': True}}


def runtime_drain(owner, reads, h, *, deadline=None, connector=receipts.connect, clock=time.monotonic, sleep=time.sleep):
    """Fence only the owned account, then require owner/parent process evidence."""
    until = min(clock() + owner.manifest['limits']['drain_seconds'],
                deadline if deadline is not None else float('inf'))
    def remaining():
        receipts.verify_environment(owner.manifest)
        left = until - clock()
        if left <= 0:
            raise RuntimeError('runtime drain deadline; retain fixture')
        return left
    def metrics():
        reads.use()
        response = h.get('/v1/admin/v2-metrics', timeout=min(10, remaining()))
        response.raise_for_status()
        return response.json()
    owner.save('resident-guard-before', receipts.resident_guard(owner.manifest, connector=connector, read_budget=reads))
    initial = metrics()
    drain.rows(initial, owner.manifest['drain_scope'])
    before = snapshot(owner, reads, connector=connector)
    owner.save('runtime-before-fence', {'observation':before})
    if before['resident_instance_seen']:
        raise RuntimeError('Resident execution history requires owner exit proof; retain fixture')
    owner.verify()
    response = owner.client.delete('/v1/model_api/delete', _retries=1, timeout=min(15, remaining()))
    owner.save('runtime-fence-http', {'http':response.status_code})
    if response.status_code != 200:
        raise RuntimeError('runtime config fence failed')
    fenced = snapshot(owner, reads, connector=connector)
    if (fenced['runtime_mode'] != 'resident_cli' or fenced['runtime_state'] != 'resident'
            or fenced['route_count'] != 0 or not fenced['member']
            or fenced['generation'] < before['generation']
            or (before['runtime_state']=='v2' and fenced['generation'] <= before['generation'])):
        raise RuntimeError('runtime fence not proven')
    owner.save('runtime-fenced', {'observation':fenced})
    from datetime import datetime
    proof = drain.Proof(initial, owner.manifest['drain_scope'],
                        datetime.fromisoformat(fenced['observed_at']).timestamp())
    while clock() < until:
        observed = proof.observe(metrics())
        if observed:
            current = snapshot(owner, reads, connector=connector)
            if (current['runtime_state'] != 'resident' or current['runtime_mode'] != 'resident_cli'
                    or current['generation'] != fenced['generation'] or current['route_count'] != 0
                    or current['resident_instance_seen']):
                raise RuntimeError('runtime fence changed')
            if not any(current[k] for k in ('pending','claimed','running','resident_lease_active','job_leases')):
                owner.save('resident-guard-after', receipts.resident_guard(owner.manifest, connector=connector, read_budget=reads))
                remaining()
                owner.save('runtime-drained', {'observation':current, 'parents':observed})
                return current
        sleep(min(owner.manifest['limits']['poll_seconds'], remaining()))
    raise RuntimeError('owned runtime drain unconfirmed; retain fixture')


def cleanup(owner, run, reads, *, admin=None, connector=receipts.connect, clock=time.monotonic, sleep=time.sleep):
    """No model admission can reopen; preserve evidence before reset/revoke."""
    deadline = clock() + owner.manifest['limits']['cleanup_seconds']
    drain_deadline = min(deadline, clock() + owner.manifest['limits']['drain_seconds'])
    def remaining():
        left = deadline - clock()
        if left <= 0:
            raise RuntimeError('cleanup deadline; retain fixture')
        return left
    owner.verify()
    # Require the executor's already-written safe scenario receipt, not a caller boolean.
    scenario = owner.out / 'scenario-result.json'
    if scenario.is_symlink() or not scenario.is_file() or scenario.stat().st_mode & 0o077:
        raise RuntimeError('safe scenario evidence missing')
    saved = json.loads(scenario.read_text())
    if any(saved.get(k) != v for k,v in owner.binding().items()) or saved.get('status') not in {'PASS_SHORT_VISIBLE_ONLY','INCOMPLETE'}:
        raise RuntimeError('scenario evidence binding invalid')
    run.close()  # closes new paid admission, then drains actual relay-held HTTP.
    closed = proxy.verify_closed_admission(owner.out / 'proxy.jsonl')
    if closed.get('manifest_digest') != digest(owner.manifest):
        raise RuntimeError('wrong admission journal')
    if admin is None:
        raise RuntimeError('runtime drain observer unavailable; retain fixture')
    observed = runtime_drain(owner, reads, admin, deadline=drain_deadline, connector=connector, clock=clock, sleep=sleep)
    owner.save('pre-reset', {'observation': observed, 'admission': closed, 'phase': 'drained'})
    # Four explicit SELECTs plus old-key HTTP verification must remain before
    # performing the destructive step. Exhaustion retains the fixture instead.
    if reads.remaining < 5:
        raise RuntimeError('cleanup verification read budget unavailable; retain fixture')
    reset = owner.client.post('/v1/account/reset', json={'confirm':'delete-all-data'}, _retries=1, timeout=min(15,remaining()))
    owner.save('reset', {'http': reset.status_code, 'phase': 'reset'})
    if reset.status_code not in (200,401):
        raise RuntimeError('reset failed')
    after = snapshot(owner, reads, connector=connector)
    reads.use()
    key_status = owner.client.get('/v1/users/whoami', _retries=1, timeout=min(10,remaining())).status_code
    remaining()
    passed = not any(after[k] for k in ('member','pending','claimed','running','resident_lease_active','job_leases')) and key_status == 401
    owner.save('cleanup', {'observation': after, 'old_key_http': key_status,
                           'status': 'CONFIRMED' if passed else 'FAILED'})
    if not passed:
        raise RuntimeError('cleanup not proven')
    # Preserve the private original/recovery marker as evidence; no history sweep.
    owner.client._deleted = True
    return 'CONFIRMED'


def execute(manifest, out, *, connector=receipts.connect):
    manifest = validate_manifest(manifest)
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    _write_private_json(out/'manifest.json', manifest)
    # Named receipts must exist; their authority/applicability is independently
    # reviewed before invoking --run. Shapes/hashes here do not create approval.
    for name in RECEIPTS:
        source = Path(manifest[name])
        if not source.is_file() or source.is_symlink():
            raise RuntimeError('reviewed receipt unavailable')
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual != manifest['receipt_sha256'][name]:
            raise RuntimeError('reviewed receipt changed')
        _write_private_json(out/(name+'.json'), {'sha256':actual})
    billing_receipt = json.loads(Path(manifest['billing_contract_receipt']).read_text())
    if billing_receipt != {
            'run_id':manifest['run_id'], 'billing':manifest['billing'],
            'funded_budget_sha256':manifest['receipt_sha256']['funded_budget_receipt'],
            'model_availability_sha256':manifest['receipt_sha256']['model_availability_receipt']}:
        raise RuntimeError('billing contract/funding binding changed')
    receipts.verify_environment(manifest)
    _write_private_json(Path(manifest['claim_path']), {'run_id':manifest['run_id'], 'manifest_digest':digest(manifest), 'state':'CONSUMED_NO_REUSE'})
    result = {'status':'INCOMPLETE', 'cleanup':'NOT_ATTEMPTED',
              'run_id':manifest['run_id'], 'manifest_digest':digest(manifest)}
    progress = {'stage':'post_claim'}
    try:
        _execute_claimed(manifest, out, connector, result, progress)
    except Exception as exc:
        result['status'] = 'INCOMPLETE'
        result.setdefault('error_type', type(exc).__name__)
        result.setdefault('error_stage', progress['stage'])
    finally:
        # Every path after the exclusive claim has one durable safe outcome.
        # Never refund/remove the consumed claim, including preflight failures.
        _write_private_json(out/'result.json', result)
    return result


def _execute_claimed(manifest, out, connector, result, progress):
    reads = Reads(manifest['limits']['read_requests'])
    progress['stage'] = 'credentials'
    admin_key = os.environ['FEEDLING_ADMIN_KEY']
    owner = run = server = thread = None
    with httpx.Client(base_url=manifest['api_url'], headers={'X-Admin-Token':admin_key},
                      trust_env=False, follow_redirects=False, timeout=10) as h:
        progress['stage'] = 'candidate_preflight'
        before_worker = candidate(h, manifest, reads)
        progress['stage'] = 'db_preflight'
        _write_private_json(out/'db-preflight.json', receipts.preflight(connector=connector, read_budget=reads))
        _write_private_json(out/'resident-guard-preflight.json', receipts.resident_guard(manifest, connector=connector, read_budget=reads))
        try:
            progress['stage'] = 'relay_start'
            run = proxy.Run(token=secrets.token_urlsafe(32), upstream_key=os.environ['E2E_KEY_OPENAI'],
                            journal=out/'proxy.jsonl', manifest=manifest)
            server = proxy.ThreadingHTTPServer(('127.0.0.1', manifest['relay_port']), proxy.handler(run))
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=10) as relay:
                reads.use()
                probe = relay.post(manifest['relay_origin']+'/v1/responses', content=b'{}',
                    headers={'Authorization':'Bearer '+run.token,'X-T779-Probe':run.probe_nonce})
                with run.lock:
                    if not (run.probe_received and run.attempts == 0 and probe.status_code == 200
                            and probe.json() == {'nonce':run.probe_nonce,'ack':run.probe_ack}):
                        raise RuntimeError('relay binding failed')
            progress['stage'] = 'provision'
            # provision performs one whoami GET internally. Reserve it before
            # registration; a failed registration does not refund the read.
            reads.use()
            c = E2EClient.provision(route='model_api', api_url=manifest['api_url'], defer_failure_cleanup=True)
            progress['stage'] = 'ownership'
            owner = receipts.Owner(c, manifest, out)
            owner.verify()
            selection = h.post('/v1/admin/runtime-allowlist', json={'user_id':c.user_id,'desired':'v2','note':'T779 fresh bounded fixture'})
            selection.raise_for_status()
            progress['stage'] = 'exercise'
            result.update(exercise(owner, run, reads, connector=connector))
            if candidate(h, manifest, reads) != before_worker:
                raise RuntimeError('candidate worker changed')
        except Exception as exc:
            result['status'] = 'INCOMPLETE'
            result['error_type'] = type(exc).__name__
            result['error_stage'] = progress['stage']
        finally:
            # Fixed safe projection, even when a scenario failed before a step receipt.
            try:
                if owner is not None:
                    progress['stage'] = 'safe_diagnostics'
                    owner.save('scenario-result', result)
                    if candidate(h, manifest, reads) != before_worker:
                        raise RuntimeError('cleanup environment changed')
                    progress['stage'] = 'cleanup'
                    result['cleanup'] = cleanup(owner, run, reads, admin=h, connector=connector)
                elif run is not None:
                    run.close()
            except Exception as exc:
                result['cleanup'] = 'FAILED'
                result['cleanup_error_type'] = type(exc).__name__
            finally:
                if run is not None:
                    try:
                        run.close()
                        if run.failure_reason is not None:
                            result['status'] = 'INCOMPLETE'
                            result['paid_failure_reason'] = run.failure_reason
                        if run.billing_bound_breached:
                            result['status'] = 'INCOMPLETE'
                            result['billing_bound_breached'] = True
                    except Exception:
                        result['cleanup'] = 'FAILED_DRAIN'
                        result['status'] = 'INCOMPLETE'
                if server is not None:
                    server.shutdown(); server.server_close()
                if thread is not None:
                    thread.join(timeout=2)
                if owner is not None:
                    owner.client._http.close()



def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--run', action='store_true')
    args = p.parse_args()
    manifest = validate_manifest(json.loads(args.manifest.read_text()))
    if not args.run:
        print('PLAN_ONLY: manifest validated; no network or fixture changes')
        return 0
    try:
        result = execute(manifest, args.out)
    except Exception as exc:
        result = {'status':'INCOMPLETE','error_type':type(exc).__name__}
    print(json.dumps(result))
    return 0 if result['status'] == 'PASS_SHORT_VISIBLE_ONLY' and result.get('cleanup') == 'CONFIRMED' else 2

if __name__ == '__main__':
    raise SystemExit(main())
