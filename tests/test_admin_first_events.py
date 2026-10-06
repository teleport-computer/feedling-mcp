"""Fixed-window accuracy and physical read-volume guards for T797 first events."""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

import psycopg
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'backend'))
import db

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)
DAY = 86400


@pytest.fixture
def sample(monkeypatch):
    # Session-local copies cannot affect another test's users or live tables.
    with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as conn:
        for table in ('users', 'chat_messages', 'user_logs', 'user_blobs',
                      'memory_moments', 'genesis_import_jobs'):
            conn.execute(f'CREATE TEMP TABLE {table} (LIKE public.{table} INCLUDING ALL)')
        # The lightweight RDS test bootstrap omits concurrent indexes; mirror
        # existing production migrations 0079/0101 on these TEMP tables only.
        conn.execute('CREATE INDEX first_event_user_time ON chat_messages(user_id,ts,seq)')
        conn.execute('CREATE INDEX first_event_time ON chat_messages(ts)')
        class Pool:
            @contextlib.contextmanager
            def connection(self, **_kwargs):
                yield conn
        monkeypatch.setattr(db, 'get_pool', lambda: Pool())
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.astimezone(tz) if tz else NOW.replace(tzinfo=None)
        monkeypatch.setattr(db, 'datetime', Clock)
        monkeypatch.setattr(db.time, 'time', lambda: NOW.timestamp())
        yield conn


def user(conn, uid, registered):
    conn.execute('INSERT INTO users(user_id,created_at,doc) VALUES (%s,%s,%s)',
                 (uid, datetime.fromtimestamp(registered, timezone.utc).isoformat(), '{}'))


def message(conn, uid, mid, ts, role='agent', source=None):
    doc = {'role': role}
    if source is not None:
        doc['source'] = source
    conn.execute('INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES (%s,%s,%s,%s)',
                 (uid, mid, ts, json.dumps(doc)))


def log(conn, uid, ts, stream='proactive_jobs'):
    conn.execute('INSERT INTO user_logs(user_id,stream,ts,doc) VALUES (%s,%s,%s,%s)',
                 (uid, stream, ts, '{"type":"app_session_end"}'))


def test_milestones_counts_denominators_and_rates_at_fixed_beijing_window(sample):
    c = sample
    now = NOW.timestamp()
    today = NOW.astimezone(ZoneInfo('Asia/Shanghai')).replace(hour=0, minute=0, second=0)
    floor = (today - timedelta(days=28)).timestamp()
    vps, api = now - 20 * DAY, now - 18 * DAY
    for uid, t0 in [('vps', vps), ('api', api), ('missing', now-10*DAY),
                    ('boundary', floor), ('before', floor-1)]:
        user(c, uid, t0)
    c.execute("INSERT INTO user_blobs(user_id,kind,doc) VALUES ('api','onboarding_route','{\"route\":\"MODEL_API\"}')")
    log(c, 'vps', vps+1)
    log(c, 'vps', None)
    message(c, 'vps', 'human', vps+2, 'human')
    message(c, 'vps', 'fallback1', vps+3, source='foreground_fallback')
    message(c, 'vps', 'fallback2', vps+4, source='proactive_fallback')
    # Onboarding/feed intentionally count proactive agent replies.
    message(c, 'vps', 'first', vps+5, 'openclaw', 'agent_initiated_proactive')
    message(c, 'vps', 'duplicate', vps+5)
    message(c, 'vps', 'later', vps+9)
    c.execute('INSERT INTO memory_moments(user_id,moment_id,occurred_at,doc) VALUES (%s,%s,%s,%s)',
              ('vps', 'm', datetime.fromtimestamp(vps+30, timezone.utc).isoformat(),
               json.dumps({'created_at': datetime.fromtimestamp(vps+3, timezone.utc).isoformat()})))
    for jid, status, offset, mode in [('excluded', 'done', 0, 'other'),
                                     ('start', 'processing', 1, ''), ('done', 'completed', 2, 'onboarding')]:
        c.execute('INSERT INTO genesis_import_jobs(user_id,job_id,status,updated_at,metadata) VALUES (%s,%s,%s,to_timestamp(%s),%s)',
                  ('api', jid, status, api+offset, json.dumps({'mode': mode})))
    message(c, 'api', 'fallback', api+3, source='foreground_fallback')
    message(c, 'api', 'first', api+4)
    # Exact W1 lower bound is included; upper bound is excluded.
    log(c, 'vps', vps+7*DAY, 'tracking_events')
    log(c, 'api', api+14*DAY, 'tracking_events')
    rows = {r['user_id']: r for r in db.admin_onboarding_funnel(registered_cutoff_ts=floor)}
    assert set(rows) == {'vps', 'api', 'missing', 'boundary'}
    assert rows['vps'] == dict(user_id='vps', route='resident', t0=vps, t1=vps+1, t2=vps+3, t3=vps+5)
    assert rows['api'] == dict(user_id='api', route='model_api', t0=api, t1=api+1, t2=api+2, t3=api+4)
    assert all(rows['missing'][k] is None for k in ('t1', 't2', 't3'))
    stages = db.admin_funnel_snapshot(tz='Asia/Shanghai')['stages']
    assert [s['count'] for s in stages] == [4, 2, 2, 2, 1]
    assert stages[-1]['eligible'] == 2
    assert stages[-1]['count'] / stages[-1]['eligible'] == .5
    cohorts = db.admin_product_health_activation_weekly(tz='Asia/Shanghai', weeks=4)['cohorts']
    week = next(r for r in cohorts if r['cohort_week'] == '2026-09-14')
    assert (week['n'], week['t1'], week['t2'], week['t3'], week['t3_rate']) == (2, 2, 2, 2, 1.0)
    assert week['coverage_complete'] is True
    empty = next(r for r in cohorts if r['cohort_week'] == '2026-10-05')
    assert empty['n'] == 0 and empty['t3_rate'] is None


def test_feed_checks_true_historical_first_reply_before_48h_filter(sample):
    c = sample
    now = NOW.timestamp()
    floor = now - 2 * DAY
    for uid in ('old', 'boundary', 'recent', 'proactive', 'null'):
        user(c, uid, now-20*DAY)
    message(c, 'old', 'first', floor-1)
    message(c, 'old', 'again', now-30)
    message(c, 'boundary', 'first', floor, 'openclaw')
    message(c, 'recent', 'user', floor-10, 'user')
    message(c, 'recent', 'fallback', floor-5, source='foreground_fallback')
    message(c, 'recent', 'first', now-100)
    message(c, 'recent', 'duplicate', now-100)
    message(c, 'proactive', 'first', now-200, source='agent_initiated_proactive')
    events = db.admin_home_feed()['events']
    assert [(r['user_id'], r['epoch'], r['kind']) for r in events] == [
        ('recent', now-100, 'first_reply'), ('proactive', now-200, 'first_reply'),
        ('boundary', floor, 'first_reply')]


class ExplainConnection:
    """Explain actual production SQL against the small fixed test population."""
    def __init__(self, conn):
        self.conn = conn
        self.plans = []
    def __getattr__(self, name):
        return getattr(self.conn, name)
    def execute(self, query, params=None):
        if query.lstrip().upper().startswith(('SELECT', 'WITH')):
            self.plans.append(self.conn.execute('EXPLAIN (ANALYZE, FORMAT JSON) '+query, params).fetchone()[0][0]['Plan'])
        return self.conn.execute(query, params)


def chat_rows_examined(node):
    count = 0
    if node.get('Relation Name') == 'chat_messages':
        count = (node['Actual Rows'] + node.get('Rows Removed by Filter', 0)) * node['Actual Loops']
    return count + sum(chat_rows_examined(p) for p in node.get('Plans', []))


@pytest.mark.parametrize('builder', ['funnel', 'feed'])
def test_first_event_reads_do_not_scale_with_each_users_chat_history(sample, monkeypatch, builder):
    c = sample
    now = NOW.timestamp()
    # 12 users x 1,000 history rows is a deterministic read-volume guard, not
    # a timing benchmark. All have first replies well before the feed window.
    for i in range(12):
        uid = f'volume{i}'
        user(c, uid, now-20*DAY)
        c.execute("INSERT INTO chat_messages(user_id,msg_id,ts,doc) SELECT %s, n::text, %s+n, '{\"role\":\"agent\"}'::jsonb FROM generate_series(1,1000) n",
                  (uid, now-10*DAY))
        message(c, uid, 'current', now-1)
    c.execute('ANALYZE chat_messages')
    c.execute('ANALYZE users')
    explained = ExplainConnection(c)
    class Pool:
        @contextlib.contextmanager
        def connection(self, **_kwargs):
            yield explained
    monkeypatch.setattr(db, 'get_pool', lambda: Pool())
    if builder == 'funnel':
        result = db.admin_onboarding_funnel(registered_cutoff_ts=now-28*DAY)
        assert len(result) == 12
    else:
        assert db.admin_home_feed()['events'] == []
    examined = sum(chat_rows_examined(p) for p in explained.plans)
    assert examined <= 100, f'{builder} examined {examined} chat rows for 12 first events'


@pytest.mark.parametrize(('chat', 'proactive', 'expected'), [
    (10, None, 10), (None, 20, 20), (30, 20, 20), (10, 20, 10),
    (None, None, None),
])
def test_first_activity_single_source_nulls_and_unrelated_stream(sample, chat, proactive, expected):
    c = sample
    t0 = NOW.timestamp()-DAY
    user(c, 'one', t0)
    log(c, 'one', t0-100, 'tracking_events')  # not activity for this milestone
    log(c, 'one', None)
    if chat is not None:
        message(c, 'one', 'chat', t0+chat, 'human')
    if proactive is not None:
        log(c, 'one', t0+proactive)
    row = db.admin_onboarding_funnel(registered_cutoff_ts=t0)[0]
    assert row['t1'] == (t0+expected if expected is not None else None)
    assert row['t3'] is None


def test_failed_first_event_queries_do_not_become_zero_metrics(sample, monkeypatch):
    class Broken:
        info = sample.info
        def execute(self, *_args, **_kwargs):
            raise psycopg.errors.QueryCanceled('known query timeout')
    class Pool:
        @contextlib.contextmanager
        def connection(self, **_kwargs):
            yield Broken()
    monkeypatch.setattr(db, 'get_pool', lambda: Pool())
    assert db.admin_onboarding_funnel(registered_cutoff_ts=0) is None
    with pytest.raises(RuntimeError, match='unavailable'):
        db.admin_funnel_snapshot()
    with pytest.raises(psycopg.errors.QueryCanceled):
        db.admin_home_feed()


@pytest.mark.parametrize('builder', ['funnel', 'feed'])
def test_first_event_lease_bounds_and_restores_settings_on_failure(sample, monkeypatch, builder):
    import admin_first_events
    c = sample
    c.execute("SET statement_timeout = '0'")
    c.execute('SET jit = on')
    seen = []
    def fail(*_args, **_kwargs):
        seen.append((c.execute('SHOW statement_timeout').fetchone()[0],
                     c.execute('SHOW jit').fetchone()[0]))
        raise psycopg.errors.QueryCanceled('known timeout')
    if builder == 'funnel':
        monkeypatch.setattr(admin_first_events, 'onboarding_rows', fail)
        assert db.admin_onboarding_funnel() is None
    else:
        monkeypatch.setattr(admin_first_events, 'recent_first_reply_rows', fail)
        with pytest.raises(psycopg.errors.QueryCanceled):
            db.admin_home_feed()
    assert seen == [('5s', 'off')]
    assert c.execute('SHOW statement_timeout').fetchone()[0] == '0'
    assert c.execute('SHOW jit').fetchone()[0] == 'on'
