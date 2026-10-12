"""The chat-poll response advertises the commit a resident consumer should run,
so a self-hosted consumer can self-update to the backend's deployed commit.

Run:  python -m pytest tests/test_chat_poll_client_release.py -q
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from accounts import registry  # noqa: E402
from asgi_test_client import make_client  # noqa: E402
from core import config as core_config  # noqa: E402
from core import store as core_store  # noqa: E402


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@pytest.fixture()
def api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(core_config, "FEEDLING_DIR", tmp_path)
    registry._users[:] = []
    registry._key_to_user.clear()
    core_store._stores.clear()
    registry._save_users()
    res = make_client().post(
        "/v1/users/register",
        json={"public_key": _b64(b"\x22" * 32), "archive_language": "en"},
    )
    assert res.status_code == 201, res.get_data(as_text=True)
    return res.get_json()["api_key"]


def test_poll_response_advertises_expected_consumer_commit(api_key, monkeypatch):
    monkeypatch.setenv("FEEDLING_EXPECTED_CONSUMER_COMMIT", "")  # use fallback
    monkeypatch.setenv("FEEDLING_GIT_COMMIT", "deadbeefcafe")
    res = make_client().get(
        "/v1/chat/poll?since=0&timeout=0",
        headers={"X-API-Key": api_key},
    )
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    assert body["client_release"]["expected_consumer_commit"] == "deadbeefcafe"


def test_consumer_poll_version_roundtrip_is_per_identity(api_key, monkeypatch):
    """Real poll sender -> ASGI parser -> PostgreSQL CAS -> admin reader."""
    import importlib.util
    import subprocess
    from urllib.parse import urlencode
    import db
    from admin import data_track
    monkeypatch.setenv('FEEDLING_API_URL', 'http://local-version-test')
    monkeypatch.setenv('FEEDLING_API_KEY', api_key)
    monkeypatch.setenv('FEEDLING_GIT_COMMIT', 'consumer-image-a')
    monkeypatch.delenv('FEEDLING_CONSUMER_COMMIT', raising=False)
    monkeypatch.setenv('CONSUMER_ID', 'agent-runner:version-a')
    spec = importlib.util.spec_from_file_location('version_test_consumer', Path(__file__).parents[1] / 'tools/chat_resident_consumer.py')
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    client = make_client()
    class Transport:
        def get(self, url, *, params, headers, timeout):
            response = client.get('/v1/chat/poll?' + urlencode(params), headers=headers)
            assert response.status_code == 200, response.get_data(as_text=True)
            class Result:
                def raise_for_status(self): pass
                def json(self): return response.get_json()
            return Result()
    monkeypatch.setattr(c, '_HTTP', Transport())
    monkeypatch.setenv('FEEDLING_GIT_COMMIT', 'backend-only-do-not-report')
    c.poll_chat(0, timeout=1, claim=False)
    uid = client.get('/v1/users/whoami', headers={'X-API-Key': api_key}).get_json()['user_id']
    state = db.get_blob(uid, 'consumer_state')
    first = state['poll_consumers']['agent-runner:version-a']
    assert first['consumer_commit'] == 'consumer-image-a'
    assert first['consumer_commit_source'] == 'image_env'
    assert first['consumer_version'] == 'resident-v1'
    # A response is activity, not a new versioned poll observation.
    import threading
    from types import SimpleNamespace
    from chat import consumer
    consumer._record_consumer_event(
        SimpleNamespace(user_id=uid, consumer_state_lock=threading.Lock()),
        'response', info={'consumer_id': 'agent-runner:version-a',
                          'consumer_commit': 'response-only', 'official': True})
    assert db.get_blob(uid, 'consumer_state')['poll_consumers']['agent-runner:version-a'] == first
    # Another identity must not overwrite the first version observation.
    monkeypatch.delenv('FEEDLING_GIT_COMMIT')
    monkeypatch.setenv('CONSUMER_ID', 'vps-b')
    c = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(c)
    monkeypatch.setattr(c, '_HTTP', Transport())
    checkout = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD'], cwd=Path(__file__).parents[1], text=True).strip()
    c.poll_chat(0, timeout=1, claim=False)
    state = db.get_blob(uid, 'consumer_state')
    report = data_track._effective_responder(route='resident', consumer_state=state, runtime={}, now_epoch=first['last_poll_epoch'] + 1000)
    observations = {x['identity']: x for x in report['poll_observations']}
    assert observations['agent-runner:version-a']['consumer_commit'] == 'consumer-image-a'
    assert observations['vps-b']['consumer_commit'] == checkout
    assert observations['vps-b']['consumer_commit_source'] == 'git_checkout'
    assert not observations['agent-runner:version-a']['recent']
    # An old client on the same identity clears its previous version report.
    c._HEADERS.pop('X-Feedling-Consumer-Commit')
    c._HEADERS.pop('X-Feedling-Consumer-Commit-Source')
    c.poll_chat(0, timeout=1, claim=False)
    state = db.get_blob(uid, 'consumer_state')
    assert state['poll_consumers']['vps-b']['consumer_commit'] == ''
    assert state['poll_consumers']['vps-b']['consumer_commit_source'] == 'unknown'
    assert state['poll_consumers']['agent-runner:version-a'] == first
    # Old per-identity rows cannot borrow a newer global writer's commit.
    state['poll_consumers']['old-row'] = {'last_poll_epoch': first['last_poll_epoch']}
    state['consumer_commit'] = 'unrelated-global'
    report = data_track._effective_responder(route='resident', consumer_state=state, runtime={})
    old = next(x for x in report['poll_observations'] if x['identity'] == 'old-row')
    assert old['consumer_commit'] == '' and old['consumer_commit_source'] == 'unknown'


@pytest.mark.parametrize('build', ['runner-image', None])
def test_supervisor_main_heartbeat_database_roundtrip(monkeypatch, build):
    import uuid
    import db
    from agent_runtime import supervisor as s
    owner = 'version-test-' + uuid.uuid4().hex
    for key in ['FEEDLING_GENESIS_WORKER_ENABLED', 'FEEDLING_RUNTIME_TOKEN_SECRET', 'FEEDLING_HOST_ALL', 'AGENT_RUNTIME_AUTODISCOVER', 'FEEDLING_GIT_COMMIT']:
        monkeypatch.delenv(key, raising=False)
    if build:
        monkeypatch.setenv('FEEDLING_GIT_COMMIT', build)
    monkeypatch.setattr(s, '_runner_heartbeat_owner', lambda *a, **k: owner)
    monkeypatch.setattr(s, '_load_roster', lambda: [])
    monkeypatch.setattr(s.spawners, 'get_spawner', lambda _: (None, None, None))
    class StopMain(BaseException): pass
    class OneBeat:
        done = False
        def is_set(self): return self.done
        def wait(self, _): self.done = True
    class InlineHeartbeat:
        def __init__(self, *, target, daemon, kwargs):
            self.target, self.kwargs = target, kwargs
        def start(self):
            assert self.target is s._heartbeat_loop
            self.kwargs['stop_event'] = OneBeat()
            self.target(**self.kwargs)
            raise StopMain
    monkeypatch.setattr(s.threading, 'Thread', InlineHeartbeat)
    with pytest.raises(StopMain): s.main()
    row = next(x for x in db.list_supervisor_instance_heartbeats() if x['owner'] == owner)
    assert row['version'] == build
    assert row['version_source'] == ('image_env' if build else 'unknown')
    # An old heartbeat replaces the old provenance, rather than inheriting it.
    db.set_supervisor_instance_heartbeat(owner, {'version': 'old-runner', 'ts': row['ts']})
    row = next(x for x in db.list_supervisor_instance_heartbeats() if x['owner'] == owner)
    assert row['version'] == 'old-runner' and row['version_source'] == 'unknown'


@pytest.mark.parametrize('commit,source,expected_commit,expected_source', [
    ('old-build', None, 'old-build', 'legacy'),
    ('', 'image_env', '', 'unknown'),
    ('unknown', 'image_env', '', 'unknown'),
    ('declared-build', 'loaded-code', 'declared-build', 'unknown'),
])
def test_legacy_and_unknown_version_headers(api_key, commit, source, expected_commit, expected_source):
    import db
    client = make_client()
    headers = {'X-API-Key': api_key, 'X-Feedling-Consumer': 'feedling-chat-resident',
               'X-Feedling-Consumer-Id': 'legacy-version-test', 'X-Feedling-Consumer-Commit': commit}
    if source is not None:
        headers['X-Feedling-Consumer-Commit-Source'] = source
    response = client.get('/v1/chat/poll?since=0&timeout=0&claim=false', headers=headers)
    assert response.status_code == 200
    uid = client.get('/v1/users/whoami', headers={'X-API-Key': api_key}).get_json()['user_id']
    row = db.get_blob(uid, 'consumer_state')['poll_consumers']['legacy-version-test']
    assert row['consumer_commit'] == expected_commit
    assert row['consumer_commit_source'] == expected_source
