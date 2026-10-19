"""Run-owned, content-free receipts and bounded read-only test DB observation.

Connector adapted from t801-live-r21/owned_history.py; no seed/write operation.
Schema: agent_jobs (0014/0023), agent_runtime_instances (0052).
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import time
import math
from contextlib import contextmanager

from tools.e2e.client import _write_private_json
from .contract import digest

DB_HOST = 'ca2317945ce4c95f5d5a9df60676a74d59197dcc-5432s.dstack-pha-prod9.phala.network'
CONFIG = Path('/Users/xiaotingtan/fleet/secrets/test-tee-db.env')
CERT = Path('/Users/xiaotingtan/fleet/secrets/test-tee-ca.crt')


def verify_environment(manifest, *, now=None):
    """Check Supervisor-bound local evidence, never infer process exclusivity.

    Hashes bind reviewed material; they do not authenticate its author or prove
    the facts inside it. Independent execution approval remains mandatory.
    """
    source = Path(manifest['execution_environment_receipt'])
    if source.is_symlink() or not source.is_file():
        raise RuntimeError('execution environment evidence missing')
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['receipt_sha256']['execution_environment_receipt']:
        raise RuntimeError('execution environment evidence changed')
    data = json.loads(raw)
    for key in ('run_id', 'api_url', 'expected_release', 'drain_scope'):
        if data.get(key) != manifest[key]:
            raise RuntimeError('execution environment binding mismatch')
    at = time.time() if now is None else now
    start, end = data.get('valid_from_epoch'), data.get('valid_until_epoch')
    if (type(start) not in (int,float) or type(end) not in (int,float)
            or not math.isfinite(start) or not math.isfinite(end)
            or not start <= at < end):
        raise RuntimeError('execution environment evidence expired/not yet valid')
    evidence = data.get('evidence')
    if not isinstance(evidence,dict) or set(evidence) != {'deployment_inventory','process_inventory','exclusive_ownership'}:
        raise RuntimeError('execution environment evidence incomplete')
    for item in evidence.values():
        if not isinstance(item,dict) or not isinstance(item.get('path'),str):
            raise RuntimeError('execution environment evidence malformed')
        path = Path(item['path'])
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != item.get('sha256')):
            raise RuntimeError('execution environment source missing/changed')
    return data


def connect(*, deadline=None):
    import psycopg
    if deadline is not None:
        deadline.remaining()
    cfg = {}
    for line in CONFIG.read_text().splitlines():
        parts = shlex.split(line, comments=True)
        if parts and parts[0] == 'export':
            parts = parts[1:]
        if len(parts) == 1 and '=' in parts[0]:
            key, value = parts[0].split('=', 1)
            if key in cfg:
                raise RuntimeError('duplicate test DB setting')
            cfg[key] = value
    if (cfg.get('TEST_TEE_PGHOST') != DB_HOST or not CERT.is_file() or CERT.is_symlink()
            or cfg.get('TEST_TEE_SSLROOTCERT') not in {str(CERT), '$HOME/fleet/secrets/test-tee-ca.crt', '${HOME}/fleet/secrets/test-tee-ca.crt'}):
        raise RuntimeError('test DB identity/certificate mismatch')
    connect_seconds, statement_ms = 5, 3000
    if deadline is not None:
        remaining = deadline.remaining()
        # libpq rounds a positive connect_timeout below two seconds up to two.
        # Refuse instead of issuing a connection which cannot fit this budget.
        if remaining < 2:
            raise RuntimeError('DB connection cannot fit remaining phase')
        connect_seconds = min(5, int(remaining))
        statement_ms = max(1, min(3000, int(remaining*1000)))
    connection = psycopg.connect(host=DB_HOST, port=443, dbname='feedling', user='feedling_owner',
        password=cfg['TEST_TEE_PGPASSWORD'], sslrootcert=str(CERT), sslmode='verify-full',
        sslnegotiation='direct', connect_timeout=connect_seconds,
        options=f'-c default_transaction_read_only=on -c statement_timeout={statement_ms} -c lock_timeout={min(1000,statement_ms)} -c idle_in_transaction_session_timeout=5000',
        autocommit=True)
    if deadline is not None:
        deadline.on_expire(connection.close)
        deadline.remaining()
    return connection


@contextmanager
def _connection(connector, budget):
    deadline = getattr(budget, 'deadline', None)
    if deadline is not None:
        deadline.remaining()  # Before connector invocation, not merely SELECT.
    resource = connector(deadline=deadline) if connector is connect else connector()
    if deadline is not None:
        if hasattr(resource, 'close'):
            deadline.on_expire(resource.close)
        deadline.remaining()
    with resource as connection:
        if deadline is not None:
            # Cancellation/close are attempts, never completion evidence.
            if hasattr(connection, 'cancel_safe'):
                deadline.on_expire(lambda: connection.cancel_safe(timeout=1))
            if hasattr(connection, "close"):
                deadline.on_expire(connection.close)
            deadline.remaining()
        yield _ReadConnection(connection, budget)
        if deadline is not None:
            deadline.remaining()


class _ReadConnection:
    def __init__(self, connection, budget):
        self.connection, self.budget = connection, budget
        self.deadline = getattr(budget, 'deadline', None)
    def __getattr__(self, name):
        return getattr(self.connection, name)
    def check(self):
        return self.deadline.remaining() if self.deadline is not None else None
    @contextmanager
    def transaction(self):
        self._timeouts(local=False)
        self.check()
        transaction = self.connection.transaction()
        self.check()
        with transaction:
            self.check()
            yield
            self.check()
        self.check()
    def _timeouts(self, *, local=True):
        left = self.check()
        if left is None:
            return
        scope = 'LOCAL ' if local else ''
        for name, cap in (('statement_timeout', 3000), ('lock_timeout', 1000)):
            ms = max(1, min(cap, int(self.check()*1000)))
            self.connection.execute(f"SET {scope}{name} = '{ms}ms'")
            self.check()
    def execute(self, statement, *args, **kwargs):
        self.check()
        if str(statement).lstrip().upper().startswith('SELECT') and self.budget is not None:
            self.budget.use()
        self._timeouts()
        self.check()
        value = self.connection.execute(statement, *args, **kwargs)
        self.check()
        return _ReadCursor(value, self.check) if self.deadline is not None else value


class _ReadCursor:
    def __init__(self, cursor, check):
        self.cursor, self.check = cursor, check
    def fetchone(self):
        self.check()
        value = self.cursor.fetchone()
        self.check()
        return value
    def fetchall(self):
        self.check()
        value = self.cursor.fetchall()
        self.check()
        return value


def preflight(*, connector=connect, read_budget=None):
    """Existing test read-only connection identity/schema check; zero model calls."""
    with _connection(connector, read_budget) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            identity = conn.execute("SELECT current_database(),current_user,current_setting('transaction_read_only'),ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone()
            if identity != ('feedling', 'feedling_owner', 'on', True):
                raise RuntimeError('DB identity/read-only/TLS unverified')
            # No account rows or content. Fail before setup if receipt schema is missing.
            conn.execute('SELECT user_id FROM users WHERE false')
            conn.execute('SELECT user_id FROM model_api_routes WHERE false')
            conn.execute('SELECT user_id,status,lease_expires_at FROM agent_jobs WHERE false')
            conn.execute('SELECT user_id,lease_owner,lease_expires_at FROM agent_runtime_instances WHERE false')
            conn.execute('SELECT user_id,hosted_runtime_state,runtime_generation FROM v2_runtime_state WHERE false')
    return {'readonly': True, 'tls': True, 'schema_checked': True, 'model_calls': 0}


SQL = '''SELECT EXISTS(SELECT 1 FROM users WHERE user_id=%s),
 (SELECT count(*) FROM agent_jobs WHERE user_id=%s AND status='pending'),
 (SELECT count(*) FROM agent_jobs WHERE user_id=%s AND status='claimed'),
 (SELECT count(*) FROM agent_jobs WHERE user_id=%s AND status='running'),
 EXISTS(SELECT 1 FROM agent_runtime_instances WHERE user_id=%s
        AND lease_owner IS NOT NULL),
 (SELECT count(*) FROM agent_jobs WHERE user_id=%s AND status IN ('claimed','running')
        AND lease_expires_at >= now()), clock_timestamp()'''


def observe(owner, *, connector=connect, read_budget=None):
    owner.verify()
    with _connection(connector, read_budget) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            identity = conn.execute("SELECT current_database(),current_user,current_setting('transaction_read_only'),ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone()
            if identity != ('feedling', 'feedling_owner', 'on', True):
                raise RuntimeError('DB identity/read-only/TLS unverified')
            row = conn.execute(SQL, (owner.client.user_id,) * 6).fetchone()
            control = conn.execute(
                "SELECT COALESCE(b.doc->>'hosted_runtime_mode','resident_cli'), "
                "COALESCE(s.hosted_runtime_state,'resident'), COALESCE(s.runtime_generation,1) "
                "FROM users u LEFT JOIN user_blobs b ON b.user_id=u.user_id AND b.kind='model_api_runtime' "
                "LEFT JOIN v2_runtime_state s ON s.user_id=u.user_id WHERE u.user_id=%s",
                (owner.client.user_id,)).fetchone()
            routes = conn.execute('SELECT (SELECT count(*) FROM model_api_routes WHERE user_id=%s), '
                                  'EXISTS(SELECT 1 FROM agent_runtime_instances WHERE user_id=%s)',
                                  (owner.client.user_id,owner.client.user_id)).fetchone()
    if not routes or len(routes) != 2 or type(routes[0]) is not int or routes[0] < 0 or type(routes[1]) is not bool:
        raise RuntimeError('route absence unmeasured')
    if (not row or len(row) != 7 or type(row[0]) is not bool or type(row[4]) is not bool
            or any(type(row[i]) is not int or row[i] < 0 for i in (1,2,3,5))
            or not isinstance(row[6], datetime) or row[6].tzinfo is None):
        raise RuntimeError('DB receipt shape invalid')
    if control is not None and (control[0] not in {'resident_cli','db_action_v2'}
            or control[1] not in {'resident','draining','v2'} or type(control[2]) is not int):
        raise RuntimeError('runtime control unmeasured')
    # An expired Resident lease still represents an unconfirmed process.
    return dict(owner.binding(), route_count=routes[0], resident_instance_seen=routes[1], runtime_mode=control[0] if control else None,
                runtime_state=control[1] if control else None, generation=control[2] if control else None, observed_at=row[6].isoformat(), member=row[0],
                pending=row[1], claimed=row[2], running=row[3], resident_lease_active=row[4],
                job_leases=row[5], source='test-readonly-DB', readonly=True, tls=True)


class Owner:
    """Constructed only by the executor immediately after fresh registration.

    No resume/arbitrary-UID entry. Private original manifest and creation receipt
    are exclusive and checked before every observation or destructive action.
    """
    def __init__(self, client, manifest, out):
        self.client, self.manifest, self.out = client, manifest, out
        self.recovery = client._orphan_file
        if self.recovery is None or not self.recovery.is_file() or self.recovery.is_symlink():
            raise RuntimeError('fresh creation recovery missing')
        self.recovery_digest = hashlib.sha256(self.recovery.read_bytes()).hexdigest()
        self.creation = out / 'creation.json'
        registration = getattr(client, '_registration_receipt', None)
        if not isinstance(registration, dict) or registration.get('http') != 201 or registration.get('user_id') != client.user_id:
            raise RuntimeError('actual successful registration receipt missing')
        body = dict(self.binding(), registration=registration, registered_at=datetime.now(timezone.utc).isoformat(),
                    registration_source='E2EClient.provision returned fresh account',
                    key_digest=hashlib.sha256(client.api_key.encode()).hexdigest())
        _write_private_json(self.creation, body)
        self.creation_digest = hashlib.sha256(self.creation.read_bytes()).hexdigest()
        self.verify()

    def binding(self):
        return {'run_id': self.manifest['run_id'], 'environment': self.manifest['api_url'],
                'expected_release': self.manifest['expected_release'], 'user_id': self.client.user_id,
                'manifest_digest': digest(self.manifest), 'creation_credential_digest': self.recovery_digest}

    def verify(self):
        if self.client.api_url != self.manifest['api_url']:
            raise RuntimeError('owner environment mismatch')
        for path, expected in ((self.recovery, self.recovery_digest), (self.creation, self.creation_digest)):
            if path.is_symlink() or path.stat().st_mode & 0o077 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise RuntimeError('owner original mismatch')
        record = json.loads(self.recovery.read_text())
        if record.get('api_url') != self.client.api_url or record.get('user_id') != self.client.user_id or record.get('api_key') != self.client.api_key:
            raise RuntimeError('owner identity mismatch')

    def save(self, name, fields):
        # Callers supply fixed assertion/status/ID/count projections only.
        _write_private_json(self.out / (name + '.json'), dict(self.binding(), **fields))


def resident_guard(manifest, *, connector=connect, read_budget=None):
    """Verify the reviewed Resident inventory runs the same guarded candidate.

    The V2 worker's release cannot establish the separate runner's source.
    Only process metadata is selected, never user or credential content.
    """
    expected = set(manifest['drain_scope']['resident_owners'])
    with _connection(connector, read_budget) as conn:
        with conn.transaction():
            conn.execute('SET TRANSACTION READ ONLY')
            rows = conn.execute(
                "SELECT owner,version,EXTRACT(EPOCH FROM (clock_timestamp()-updated_at)), "
                "updated_at,clock_timestamp() "
                "FROM agent_runtime_supervisor_heartbeats "
                "WHERE updated_at >= clock_timestamp()-interval '90 seconds' "
                "OR owner=ANY(%s)", (sorted(expected),)).fetchall()
    if (len(rows) != len(expected) or {r[0] for r in rows} != expected
            or any(r[1] != manifest['expected_release'] or not math.isfinite(float(r[2]))
                   or not 0 <= float(r[2]) <= 30 for r in rows)):
        raise RuntimeError('Resident guarded release/inventory unconfirmed')
    return {'owners': sorted(expected), 'release': manifest['expected_release'], 'fresh': True,
            'received_at_local_utc': datetime.now(timezone.utc).isoformat(),
            'age_source': 'DB clock_timestamp minus updated_at; not local clock age',
            'samples': [{'owner': r[0], 'version': r[1], 'age_sec': float(r[2]),
                         'updated_at_db': r[3].isoformat(), 'sampled_at_db': r[4].isoformat()}
                        for r in rows]}


def verify_input_count(manifest, *, now=None):
    """Review-bound authorization/fee facts; hashes do not create authority.

    The fixed OpenRouter lane verifies a context-bound receipt instead.
    For OpenAI, disabled is the default operational posture until the endpoint's fee and
    exact-payload compatibility have actually been established and reviewed.
    """
    if manifest.get('lane') == 'openrouter-v2-text':
        from .openrouter_bound import verify
        return verify(manifest, now=now)
    if manifest['input_count']['enabled'] is not True:
        raise RuntimeError('input counting disabled: authorization/fee facts required')
    path = Path(manifest['input_count_receipt'])
    if path.is_symlink() or not path.is_file():
        raise RuntimeError('input count receipt missing')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest['receipt_sha256']['input_count_receipt']:
        raise RuntimeError('input count receipt changed')
    value = json.loads(raw)
    expected = dict(run_id=manifest['run_id'], model=manifest['model'],
        account_scope=manifest['billing']['account_scope'],
        input_count=manifest['input_count'], endpoint='https://api.openai.com/v1/responses/input_tokens',
        exact_final_payload_supported=True, authorized=True,
        receipt_sha256={k:v for k,v in manifest['receipt_sha256'].items() if k != 'input_count_receipt'})
    if any(value.get(k) != v for k,v in expected.items()):
        raise RuntimeError('input count authorization binding mismatch')
    at = time.time() if now is None else now
    start, end = value.get('valid_from_epoch'), value.get('valid_until_epoch')
    if (type(start) not in (int,float) or type(end) not in (int,float)
            or not math.isfinite(start) or not math.isfinite(end) or not start <= at < end):
        raise RuntimeError('input count authorization expired')
    # Keep reviewable source facts separate from assertions in this receipt.
    for name in ('fee_evidence', 'payload_compatibility_evidence', 'authorization_evidence'):
        item = value.get(name)
        if not isinstance(item, dict) or not isinstance(item.get('path'), str):
            raise RuntimeError('input count evidence missing')
        source = Path(item['path'])
        if (not source.is_absolute() or source.is_symlink() or not source.is_file()
                or hashlib.sha256(source.read_bytes()).hexdigest() != item.get('sha256')):
            raise RuntimeError('input count evidence changed')
    return value
