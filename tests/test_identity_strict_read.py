"""HTTP-only strict identity reads, compatibility and fixed-day projections."""
from datetime import datetime, date
import json
from types import SimpleNamespace

import pytest

from identity import identity_core, service


@pytest.mark.parametrize('value', [None, [], 'bad', 3])
def test_present_invalid_storage_is_not_absence(monkeypatch, value):
    import db
    monkeypatch.setattr(db, 'get_blobs_for_users', lambda *a, **k: {('u', 'identity'): value})
    assert identity_core.get_identity_strict(SimpleNamespace(user_id='u')) == (
        {'error': 'identity_storage_invalid'}, 500)


def test_missing_mapping_and_bound_reader(monkeypatch):
    import db
    def read(users, kinds, **kw):
        assert users == ['u'] and kinds == ['identity'] and kw == {'raise_on_error': True}
        return {}
    monkeypatch.setattr(db, 'get_blobs_for_users', read)
    assert identity_core.get_identity_strict(SimpleNamespace(user_id='u')) == ({'identity': None}, 200)


@pytest.mark.parametrize('stage', ['read', 'enrich'])
def test_fixed_error_hides_storage_details(monkeypatch, stage):
    import db
    def fail(*a, **kw):
        raise RuntimeError('private SQL server credential secret')
    monkeypatch.setattr(db, 'get_blobs_for_users', fail if stage == 'read' else
                        lambda *a, **k: {('u', 'identity'): {}})
    if stage == 'enrich':
        monkeypatch.setattr(service, '_live_days_with_user', fail)
    assert identity_core.get_identity_strict(SimpleNamespace(user_id='u')) == (
        {'error': 'identity_read_unavailable'}, 503)


def test_legacy_core_consumers_keep_loader(monkeypatch):
    import db
    def forbidden(*a, **k):
        raise AssertionError('legacy core must not switch to strict reader')
    monkeypatch.setattr(db, 'get_blobs_for_users', forbidden)
    monkeypatch.setattr(service, '_load_identity', lambda store: None)
    assert identity_core.get_identity(SimpleNamespace(user_id='u')) == ({'identity': None}, 200)


@pytest.mark.parametrize('anchor,source,earliest,backend_days,old_days', [
    ('2026-10-10', 'explicit', None, 3, 3),
    (None, 'explicit', None, 0, 99),
    ('invalid', 'explicit', None, 0, 99),
    ('2026-10-10', None, date(2026, 10, 1), 12, 3),
    (None, None, date(2026, 10, 1), 12, 99),
])
def test_backend_days_and_legacy_differences(monkeypatch, anchor, source, earliest, backend_days, old_days):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
    import io_cli
    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 13, 0, 1)
    monkeypatch.setattr(service, 'datetime', Frozen)
    monkeypatch.setattr(service, '_earliest_memory_date', lambda store: earliest)
    stored = {'owner_user_id': 'u', 'v': 1, 'relationship_started_at': anchor,
              'relationship_anchor_source': source, 'body': json.dumps({'days_with_user': 99})}
    stored['days_with_user'] = service._live_days_with_user(stored, SimpleNamespace(user_id='u'))
    view = io_cli._plaintext_identity_view({'identity': stored}, 'u')['identity']
    assert view['days_with_user'] == backend_days
    # Compare the actual legacy enclave projection on the same stored input.
    from asgi_test_client import _AsgiTestClient
    from enclave import auth, backend_client, keys, state
    from enclave.routes import build_app
    from enclave.routes import identity as enclave_identity
    monkeypatch.setitem(state._state, 'ready', True)
    auth.reset_cache()
    async def backend_get(path, headers, params=None):
        return {'user_id': 'u'} if path == '/v1/users/whoami' else {'identity': stored}
    async def content_key():
        return object()  # plaintext path never uses a secret key
    monkeypatch.setattr(backend_client, 'backend_get', backend_get)
    monkeypatch.setattr(keys, 'get_content_sk', content_key)
    monkeypatch.setattr(enclave_identity, '_dt', SimpleNamespace(datetime=Frozen, date=date))
    old_response = _AsgiTestClient(build_app()).get('/v1/identity/get', headers={'X-API-Key': 'fixture'})
    assert old_response.status_code == 200
    old = old_response.get_json()['identity']
    assert old['days_with_user'] == old_days
    auth.reset_cache()
    assert {k:v for k,v in view.items() if k != 'days_with_user'} == {
        k:v for k,v in old.items() if k != 'days_with_user'}
    class Tomorrow(Frozen):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 14, 0, 1)
    monkeypatch.setattr(service, 'datetime', Tomorrow)
    stored['days_with_user'] = service._live_days_with_user(stored, SimpleNamespace(user_id='u'))
    expected = backend_days + 1 if anchor == '2026-10-10' or earliest else backend_days
    assert io_cli._plaintext_identity_view({'identity': stored}, 'u')['identity']['days_with_user'] == expected
