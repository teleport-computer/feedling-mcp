"""Persistent provider circuit: terminal ownership, admission and recovery."""
from pathlib import Path
import sys
import time
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'backend'))

import db
import debug_trace
from model_api_runtime.v2 import jobs_store, wake_circuit
from notices import error_contract
from tee_shadow import mirror
from conftest import seed_user, set_v2_runtime_owner


@pytest.fixture
def uid():
    user_id = 'circuit_' + uuid.uuid4().hex
    seed_user(user_id)
    set_v2_runtime_owner(user_id)
    return user_id


def claim(uid, lane='heartbeat'):
    job_id, coalesced = jobs_store.enqueue_job(uid, lane)
    assert not coalesced
    # Claim the exact user; unrelated pending work must not affect this test.
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE agent_jobs SET status='running',claimed_by='circuit-test',"
                     "claimed_at=clock_timestamp(),started_at=clock_timestamp(),"
                     "lease_expires_at=now()+interval '1 minute' WHERE id=%s", (job_id,))
    return job_id


def fail(uid, code='quota_insufficient', lane='heartbeat'):
    job = claim(uid, lane)
    assert jobs_store.mark_failed(job, 'provider_failure', claimed_by='circuit-test', error_class=code)
    return job


def open_circuit(uid, code='quota_insufficient'):
    for _ in range(wake_circuit.FAILURE_THRESHOLD):
        fail(uid, code)
    assert wake_circuit.is_open(uid)


def test_policy_targets_only_actionable_precise_errors():
    assert wake_circuit.FAILURE_THRESHOLD == 3
    assert wake_circuit.ERROR_CLASSES == {'quota_insufficient', 'auth_invalid', 'provider_account_expired'}
    assert all(error_contract.spec_for(code).blame == 'user_provider' for code in wake_circuit.ERROR_CLASSES)


def test_reset_invalid_reason_still_raises():
    with pytest.raises(ValueError, match='invalid wake circuit reset reason'):
        wake_circuit.reset('unused', reason='not-a-reset-event')


@pytest.mark.parametrize('code', sorted(wake_circuit.ERROR_CLASSES))
def test_threshold_owned_terminalization_notice_and_trace(uid, code, monkeypatch):
    events = []
    monkeypatch.setattr(debug_trace, 'trace_event', lambda store, **event: events.append(event))
    for i in range(wake_circuit.FAILURE_THRESHOLD):
        job = fail(uid, code, 'heartbeat' if i % 2 == 0 else 'screen_watch')
        assert wake_circuit.is_open(uid) == (i + 1 == wake_circuit.FAILURE_THRESHOLD)
        assert jobs_store.get_wake_schedule(uid)['provider_fail_streak'] == i + 1
        assert not jobs_store.mark_failed(job, 'again', claimed_by='circuit-test', error_class=code)
    notices = db.log_read_all(uid, 'user_notices')
    assert len(notices) == 1
    assert notices[0]['error_class'] == wake_circuit.NOTICE_CODE
    assert notices[0]['blame'] == 'user_provider'
    assert notices[0]['severity'] == 'warning'
    assert notices[0]['occurrences'] == 1
    circuit_events = [event for event in events if event['type'].startswith('wake.circuit.')]
    assert circuit_events == [dict(subsystem='v2', type='wake.circuit.open', status='warning',
                           detail={'reason': code, 'streak': wake_circuit.FAILURE_THRESHOLD,
                                   'threshold': wake_circuit.FAILURE_THRESHOLD})]


@pytest.mark.parametrize('code', ['provider_incompatible', 'model_not_found', 'provider_config',
                                  'upstream_unavailable', 'rate_limited', 'unknown', ''])
def test_success_or_non_target_failure_breaks_closed_streak(uid, code):
    for _ in range(wake_circuit.FAILURE_THRESHOLD - 1):
        fail(uid)
    if code:
        fail(uid, code)
    else:
        assert jobs_store.mark_completed(claim(uid), claimed_by='circuit-test')
    assert jobs_store.get_wake_schedule(uid)['provider_fail_streak'] == 0
    fail(uid)
    assert not wake_circuit.is_open(uid)


def test_both_due_queries_and_diagnosis_block_without_timer_recovery(uid):
    jobs_store.upsert_wake_schedule(uid, next_heartbeat_at=1, next_screen_watch_at=1)
    assert uid in jobs_store.due_heartbeat_users()
    assert uid in jobs_store.due_screen_watch_users()
    open_circuit(uid)
    for now in (time.time(), time.time() + 365 * 86400):
        assert uid not in jobs_store.due_heartbeat_users(now=now)
        assert uid not in jobs_store.due_screen_watch_users(now=now)
        assert 'provider_circuit' in jobs_store.heartbeat_due_diagnosis(uid, now=now)['blocked_by']
    assert jobs_store.mark_completed(claim(uid), claimed_by='circuit-test')
    assert wake_circuit.is_open(uid)  # success from already-running work is not a reset


@pytest.mark.parametrize('reason', sorted(wake_circuit.RESET_REASONS))
def test_event_reset_fences_old_job_and_resolves_notice(uid, reason, monkeypatch):
    open_circuit(uid)
    stale_job = claim(uid)
    events = []
    monkeypatch.setattr(debug_trace, 'trace_event', lambda store, **event: events.append(event))
    assert wake_circuit.reset(uid, reason=reason)
    assert not wake_circuit.is_open(uid)
    assert jobs_store.mark_failed(stale_job, 'old-key', claimed_by='circuit-test', error_class='auth_invalid')
    assert jobs_store.get_wake_schedule(uid)['provider_fail_streak'] == 0
    notices = db.log_read_all(uid, 'user_notices')
    assert len(notices) == 1 and notices[0]['resolved'] is True
    assert [e['type'] for e in events] == ['wake.circuit.recover']
    assert not wake_circuit.reset(uid, reason=reason)
    assert len(events) == 1
    fail(uid)
    assert jobs_store.get_wake_schedule(uid)['provider_fail_streak'] == 1


def test_scheduled_results_do_not_arm_or_clear_circuit(uid):
    for _ in range(wake_circuit.FAILURE_THRESHOLD + 1):
        fail(uid, lane='scheduled')
    assert not wake_circuit.is_open(uid)
    open_circuit(uid)
    assert jobs_store.mark_completed(claim(uid, 'scheduled'), claimed_by='circuit-test')
    assert wake_circuit.is_open(uid)


def test_recovery_does_not_mutate_v1(uid):
    open_circuit(uid)
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE v2_runtime_state SET hosted_runtime_state='resident' WHERE user_id=%s", (uid,))
    assert not wake_circuit.reset(uid, reason='credential_saved')
    assert wake_circuit.is_open(uid)


def test_rds_tee_schema_parity():
    expected = {'provider_fail_streak': ('integer', 'NO'),
                'wake_circuit_reason': ('text', 'NO'),
                'wake_circuit_opened_at': ('timestamp with time zone', 'YES'),
                'wake_circuit_reset_at': ('timestamp with time zone', 'YES')}
    for pool in (db.get_pool(), mirror.get_tee_pool()):
        with pool.connection() as conn:
            rows = conn.execute("SELECT column_name,data_type,is_nullable FROM information_schema.columns "
                                "WHERE table_schema='public' AND table_name='v2_wake_schedule' "
                                "AND column_name=ANY(%s)", (list(expected),)).fetchall()
        assert {name: (kind, nullable) for name, kind, nullable in rows} == expected


def test_admin_snapshot_reads_open_state(uid):
    def read():
        return db.admin_data_track_snapshot([uid], include_legacy_background=False,
                                           statement_timeout_ms=1000)[uid]
    assert read()['wake_provider_circuit_open'] is False
    open_circuit(uid)
    assert read()['wake_provider_circuit_open'] is True
    wake_circuit.reset(uid, reason='new_chat')
    assert read()['wake_provider_circuit_open'] is False


def test_lost_lease_cannot_advance_failure_streak(uid):
    job = claim(uid)
    assert not jobs_store.mark_failed(job, 'failed', claimed_by='another-worker', error_class='quota_insufficient')
    assert jobs_store.get_wake_schedule(uid) is None
    with db.get_pool().connection() as conn:
        conn.execute("UPDATE agent_jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (job,))
    assert not jobs_store.mark_failed(job, 'failed', claimed_by='circuit-test', error_class='quota_insufficient')
    assert jobs_store.get_wake_schedule(uid) is None


def test_dual_migration_adds_each_column_in_autocommit(monkeypatch):
    import contextlib
    from types import SimpleNamespace
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    backend = Path(__file__).resolve().parents[1] / 'backend'
    statements = []
    active = False
    @contextlib.contextmanager
    def autocommit():
        nonlocal active
        active = True
        yield
        active = False
    def execute(sql):
        if sql.startswith('ALTER TABLE'):
            assert active
            assert 'ADD COLUMN IF NOT EXISTS' in sql
            statements.append(sql)
    for folder, head in [('alembic', '0113_v2_wake_circuit'), ('alembic_tee', '0048_v2_wake_circuit')]:
        cfg = Config(str(backend / (folder + '.ini')))
        cfg.set_main_option('script_location', str(backend / folder))
        module = ScriptDirectory.from_config(cfg).get_revision(head).module
        monkeypatch.setattr(module, 'op', SimpleNamespace(
            get_context=lambda: SimpleNamespace(autocommit_block=autocommit), execute=execute))
        module.upgrade()
    assert len(statements) == 8
    assert statements[:4] == statements[4:]


def test_notice_contract_localizes_without_provider_details(uid, monkeypatch):
    from notices import catalog
    from core import store as core_store
    def unexpected_hydration(*_args, **_kwargs):
        raise AssertionError('notice publication must not hydrate user store sections')
    monkeypatch.setattr(core_store.UserStore, 'ensure_sections', unexpected_hydration)
    monkeypatch.setattr(wake_circuit.accounts_registry, '_get_user_archive_language', lambda _uid: 'en')
    open_circuit(uid)
    notice = db.log_read_all(uid, 'user_notices')[0]
    spec = error_contract.spec_for(wake_circuit.NOTICE_CODE)
    assert wake_circuit.NOTICE_CODE in catalog.ERROR_CLASSES
    assert spec.public and spec.safe_text_zh and spec.safe_text_en
    assert spec.domain == 'workflow' and spec.family == 'wake'
    assert spec.matcher_pattern == '' and spec.matcher() is None
    assert spec.code not in {item.code for item in error_contract.consumer_specs()}
    assert notice['user_text'] == spec.safe_text_en
    assert notice['detail'] == ''


@pytest.mark.parametrize('status,detail', [(401, 'invalid api key'), (402, 'payment required'),
                                           (400, 'account expired')])
def test_actionable_provider_responses_open_after_threshold(uid, status, detail):
    import provider_client
    from model_api_runtime.v2 import worker
    code = worker._turn_failure_error_class(provider_client.ProviderError(detail, status_code=status))
    for _ in range(wake_circuit.FAILURE_THRESHOLD):
        fail(uid, code)
    assert wake_circuit.is_open(uid)
