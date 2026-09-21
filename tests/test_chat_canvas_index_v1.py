"""V1/self-hosted Canvas cards join the metadata index without body copies."""
import base64
import importlib.util
import os
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import psycopg
from psycopg.types.json import Jsonb
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
import db
from asgi_test_client import make_client
from chat import chat_core
from conftest import seed_user
from core import store as core_store
from model_api_runtime.v2 import jobs_store

ROOT = Path(__file__).parent.parent
INDEX = "ix_chat_messages_agent_canvas_cards"


@pytest.fixture
def uid():
    value = 'usr_t674_' + uuid.uuid4().hex[:12]
    seed_user(value)
    return value


def _card(uid, filename, ts, *, role='agent', content_type='file', title='Title'):
    mid = uuid.uuid4().hex
    doc = {'id': mid, 'role': role, 'content_type': content_type,
           'file_name': filename, 'file_display_title': title,
           'file_display_subtitle': 'Subtitle', 'body_key': 'must-not-be-read',
           'envelope': {'body_ct': 'private' * 1500}}
    with db.get_pool().connection() as conn:
        conn.execute('INSERT INTO chat_messages(user_id,msg_id,ts,doc) VALUES(%s,%s,%s,%s)',
                     (uid, mid, ts, Jsonb(doc)))
    return mid


def _workspace(uid, filename, ts):
    row = jobs_store.put_workspace_entry_cas(
        uid, '/workspace/' + filename, kind='workspace',
        content_envelope={'body_ct': 'must-not-be-copied'}, mime_type='text/html',
        source_ref='', expected_revision=0)
    with db.get_pool().connection() as conn:
        conn.execute('UPDATE v2_workspace_entries SET created_at=to_timestamp(%s),'
                     'updated_at=to_timestamp(%s) WHERE user_id=%s AND path=%s',
                     (ts, ts, uid, '/workspace/' + filename))
    return row


def _index(uid):
    result, status = chat_core.canvas_index(SimpleNamespace(user_id=uid))
    assert status == 200
    return result['canvases']


def test_v1_store_delivery_visible_through_authenticated_endpoint():
    client = make_client()
    registration = client.post('/v1/users/register', json={
        'public_key': base64.b64encode(os.urandom(32)).decode(), 'archive_language': 'en'})
    assert registration.status_code == 201
    user = registration.get_json()
    store = core_store.get_store(user['user_id'])
    msg = store.append_chat('openclaw', 'chat', {'body_ct': 'opaque'},
                            content_type='file', extra={'file_name': 'Resident.io.html'},
                            strict=True)
    response = client.get('/v1/chat/canvases', headers={'X-API-Key': user['api_key']})
    assert response.status_code == 200
    cards = response.get_json()['canvases']
    assert len(cards) == 1
    assert cards[0]['filename'] == 'Resident.io.html'
    assert cards[0]['message_id'] == msg['id']
    assert cards[0]['revision'] == 1
    assert cards[0]['mime_type'] == 'text/html'
    assert jobs_store.list_canvas_workspace_entries(user['user_id']) == []
    assert 'body_ct' not in str(cards)


def test_latest_ts_earliest_creation_and_deleted_card(uid):
    first = _card(uid, 'Journal.IO.HTML', 100, role='openclaw')
    latest = _card(uid, 'Journal.IO.HTML', 300, title='Newest')
    _card(uid, 'Journal.IO.HTML', 200, title='Later insertion, older timestamp')
    cards = _index(uid)
    assert len(cards) == 1
    card = cards[0]
    assert card['message_id'] == latest
    assert card['display_title'] == 'Newest'
    assert card['display_subtitle'] == 'Subtitle'
    assert datetime.fromisoformat(card['created_at']) == datetime.fromtimestamp(100, timezone.utc)
    assert datetime.fromisoformat(card['updated_at']) == datetime.fromtimestamp(300, timezone.utc)
    assert set(card) == {'filename','revision','mime_type','created_at','updated_at',
                         'message_id','display_title','display_subtitle'}
    with db.get_pool().connection() as conn:
        conn.execute('DELETE FROM chat_messages WHERE user_id=%s', (uid,))
    assert _index(uid) == []
    assert first != latest


def test_workspace_precedence_and_existing_latest_seq_metadata(uid):
    _card(uid, 'same.io.html', 300)
    newest_seq = _card(uid, 'same.io.html', 100, title='Latest delivery')
    workspace = _workspace(uid, 'same.io.html', 200)
    rows = _index(uid)
    assert len(rows) == 1
    assert rows[0]['revision'] == workspace['revision']
    assert rows[0]['message_id'] == newest_seq
    assert datetime.fromisoformat(rows[0]['updated_at']).timestamp() == 200
    assert rows[0]['display_title'] == 'Latest delivery'
    assert db.chat_latest_agent_canvas_cards(uid) == []


def test_filters_role_type_suffix_and_isolates_users(uid):
    other = 'usr_t674_' + uuid.uuid4().hex[:12]
    seed_user(other)
    _card(other, 'foreign.io.html', 100)
    _card(uid, 'user.io.html', 100, role='user')
    _card(uid, 'text.io.html', 100, content_type='text')
    _card(uid, 'plain.html', 100)
    _card(uid, 'fake.io.html.txt', 100)
    _card(uid, 'Case.IO.HTML', 100)
    _card(uid, 'case.io.html', 100)
    assert [r['filename'] for r in _index(uid)] == ['Case.IO.HTML', 'case.io.html']


def test_limit_after_union_and_workspace_precedence_beyond_500(uid):
    # A newest card whose workspace is older than the workspace top 500 must
    # not bypass workspace precedence. Likewise duplicates cannot starve V1.
    for n in range(501):
        _workspace(uid, f'w{n:03}.io.html', 1000+n)
    _card(uid, 'w000.io.html', 9000)
    for n in range(505):
        _card(uid, f'v{n:03}.io.html', 1250+n)
    rows = _index(uid)
    expected = [(1000+n, f'w{n:03}.io.html') for n in range(501)]
    expected += [(1250+n, f'v{n:03}.io.html') for n in range(505)]
    expected.sort(key=lambda item: (-item[0], item[1]))
    assert [r['filename'] for r in rows] == [name for _, name in expected[:500]]
    assert len(rows) == 500
    assert 'w000.io.html' not in {r['filename'] for r in rows}
    assert len(db.chat_latest_agent_canvas_cards(uid, limit=3)) == 3


def _migration(chain):
    name = '0114_agent_canvas_cards' if chain == 'alembic' else '0049_agent_canvas_cards'
    spec = importlib.util.spec_from_file_location(name, ROOT/'backend'/chain/'versions'/f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_predicate_parity_and_read_sql():
    main, tee = _migration('alembic'), _migration('alembic_tee')
    assert main.CREATE_INDEX_SQL == tee.CREATE_INDEX_SQL
    assert main.CREATE_INDEX_SQL.split(' WHERE ')[1] == db._AGENT_CANVAS_CARD_PREDICATE
    assert db._AGENT_CANVAS_CARD_PREDICATE in db._CHAT_LATEST_AGENT_CANVAS_CARDS_SQL.replace('%%','%')
    assert 'INCLUDE' not in main.CREATE_INDEX_SQL


@pytest.mark.parametrize('dsn_env', ['DATABASE_URL','TEE_DATABASE_URL'])
def test_index_exists_valid_on_both_migrated_chains(dsn_env):
    with psycopg.connect(os.environ[dsn_env]) as conn:
        row = conn.execute('SELECT i.indisvalid,pg_get_indexdef(i.indexrelid) '
                           'FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid '
                           'WHERE c.relname=%s', (INDEX,)).fetchone()
    assert row and row[0]
    assert '(user_id, ts DESC)' in row[1]
    assert 'INCLUDE' not in row[1]
    for text in ['agent','openclaw','content_type','file_name','%.io.html']:
        assert text in row[1]


@pytest.mark.parametrize('chain', ['alembic','alembic_tee'])
@pytest.mark.parametrize('validity', [None,False,True])
def test_migration_retry_invalid_cleanup_and_symmetric_drop(monkeypatch,chain,validity):
    mod = _migration(chain)
    operations = []
    active = False
    @contextmanager
    def autocommit():
        nonlocal active
        active = True
        yield
        active = False
    def execute(sql):
        if 'INDEX' in sql:
            assert active
        operations.append(sql)
    monkeypatch.setattr(mod,'_index_validity',lambda: validity)
    monkeypatch.setattr(mod,'op',SimpleNamespace(execute=execute,
        get_context=lambda: SimpleNamespace(autocommit_block=autocommit)))
    mod.upgrade()
    ddl = [s for s in operations if 'INDEX' in s]
    drop = f'DROP INDEX CONCURRENTLY IF EXISTS {INDEX}'
    assert ddl == ([drop] if validity is False else []) + ([mod.CREATE_INDEX_SQL] if validity is not True else [])
    operations.clear()
    mod.downgrade()
    assert operations == [drop]


def test_actual_reader_uses_partial_index_with_large_docs():
    """The production query, default planner, large opaque envelopes; no GUC hint."""
    with db.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute('CREATE TEMP TABLE chat_messages '
                         '(user_id text,msg_id text,ts double precision,seq bigint,doc jsonb) ON COMMIT DROP')
            conn.execute('CREATE TEMP TABLE v2_workspace_entries '
                         '(user_id text,path text,kind text,PRIMARY KEY(user_id,path)) ON COMMIT DROP')
            conn.execute("INSERT INTO chat_messages SELECT 'heavy','m-'||n,n,n,"
                         "jsonb_build_object('role','agent','content_type','file',"
                         "'file_name',CASE WHEN n<=33 THEN 'c-'||(n%11)||'.io.html' ELSE 'other.txt' END,"
                         "'body_key','synthetic/r2/key','envelope',jsonb_build_object('body_ct',repeat('opaque',1600))) "
                         "FROM generate_series(1,4000) n")
            # Execute real migration DDL, removing CONCURRENTLY only because a
            # transaction-local TEMP table cannot use concurrent index builds.
            conn.execute(_migration('alembic').CREATE_INDEX_SQL.replace(' CONCURRENTLY',''))
            conn.execute("ANALYZE chat_messages")
            assert conn.execute('SELECT max(octet_length(doc::text)) FROM chat_messages').fetchone()[0] > 9000
            plan = conn.execute('EXPLAIN (ANALYZE, FORMAT JSON) ' + db._CHAT_LATEST_AGENT_CANVAS_CARDS_SQL,
                                ('heavy',500)).fetchone()[0][0]['Plan']
            nodes = []
            def walk(node):
                nodes.append(node)
                for child in node.get('Plans',[]):
                    walk(child)
            walk(plan)
            scans = [node for node in nodes if node.get('Index Name') == INDEX]
            assert scans, plan
            assert scans[0]['Actual Rows'] == 33
            assert scans[0].get('Rows Removed by Filter',0) == 0
            rows = conn.execute(db._CHAT_LATEST_AGENT_CANVAS_CARDS_SQL,('heavy',500)).fetchall()
            assert len(rows) == 11
