"""T688: real PostgreSQL proves options apply without rewriting live log rows."""
import importlib.util
import json
import os
from pathlib import Path
import uuid

from alembic.migration import MigrationContext
from alembic.operations import Operations
import psycopg
import pytest
from sqlalchemy import create_engine

MIGRATION = Path(__file__).parents[1] / 'backend/alembic_tee/versions/0050_user_logs_autovacuum.py'
EXPECTED = {
    'autovacuum_vacuum_scale_factor': '0.005',
    'autovacuum_vacuum_insert_threshold': '1000',
    'autovacuum_vacuum_insert_scale_factor': '0.005',
}


def _migration():
    spec = importlib.util.spec_from_file_location('user_logs_autovacuum', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _options(conn):
    rows = conn.exec_driver_sql("SELECT reloptions FROM pg_class WHERE oid='user_logs'::regclass").scalar()
    return dict(item.split('=', 1) for item in (rows or []))


def test_tee_head_installs_calibrated_options():
    with psycopg.connect(os.environ['TEE_DATABASE_URL']) as conn:
        options = conn.execute("SELECT reloptions FROM pg_class WHERE oid='public.user_logs'::regclass").fetchone()[0]
        assert dict(item.split('=', 1) for item in options) == EXPECTED


@pytest.mark.parametrize('prepared', [True, False])
def test_upgrade_retry_downgrade_preserves_rows_and_unrelated_options(prepared):
    """Run migration code on a local isolated schema, with readback as oracle."""
    module = _migration()
    schema = 't688_' + uuid.uuid4().hex
    url = os.environ['TEE_DATABASE_URL'].replace('postgresql://', 'postgresql+psycopg://', 1)
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql(f'CREATE SCHEMA {schema}')
            conn.exec_driver_sql(f'SET search_path TO {schema}')
            conn.exec_driver_sql('CREATE TABLE user_logs (id int PRIMARY KEY, doc jsonb) WITH (fillfactor=80)')
            conn.exec_driver_sql("INSERT INTO user_logs VALUES (1, '{\"state\":\"queued\"}'),(2, '{\"state\":\"running\"}')")
            conn.exec_driver_sql('CREATE TABLE server_config (key text PRIMARY KEY, value bytea)')
            marker = {'prepared': prepared, 'tee_heads': [module.down_revision], 'other': 'retain'}
            conn.exec_driver_sql('INSERT INTO server_config VALUES (%s, %s)', ('phase4_primary_prepared', json.dumps(marker).encode()))
            conn.commit()
            context = MigrationContext.configure(conn)
            def apply(function):
                with context.begin_transaction():
                    with Operations.context(context):
                        function()
            def snapshot():
                rows = conn.exec_driver_sql('SELECT id,doc FROM user_logs ORDER BY id').fetchall()
                head = json.loads(bytes(conn.exec_driver_sql('SELECT value FROM server_config').scalar()))
                options = _options(conn)
                conn.commit()
                return rows, head, options
            before, _, _ = snapshot()
            for _ in range(2):
                apply(module.upgrade)
                rows, head, options = snapshot()
                assert rows == before
                assert options == {'fillfactor': '80', **EXPECTED}
                assert head == {**marker, 'tee_heads': [module.revision if prepared else module.down_revision]}
            apply(module.downgrade)
            rows, head, options = snapshot()
            assert rows == before
            assert options == {'fillfactor': '80'}
            assert head == marker
    finally:
        with engine.begin() as conn:
            conn.exec_driver_sql(f'DROP SCHEMA IF EXISTS {schema} CASCADE')
        engine.dispose()
