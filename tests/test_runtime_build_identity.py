"""Build identity at the real consumer import and supervisor heartbeat boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = "3bd36fcb5fc650fc9fe6dc707fc3140ff6d012ce"


@pytest.mark.parametrize("override,image,git_value,expected", [
    (None, BUILD, "", BUILD),  # shipped image has no .git
    ("abcdef1234", BUILD, "", "abcdef1234"),
    ("", BUILD, "abcdef0", ""),  # explicit unknown stays unknown
    (None, None, "abcdef0", "abcdef0"),  # VPS startup checkout
    (None, None, "", ""),
    (None, "dev", "abcdef0", ""),
    (None, "unknown", "abcdef0", ""),
    (None, " ", "abcdef0", ""),
])
def test_consumer_import_identity(override, image, git_value, expected):
    env = dict(os.environ, FEEDLING_API_URL="http://localhost:5001",
               FEEDLING_API_KEY="test_key_00000000")
    for key, value in [("FEEDLING_CONSUMER_COMMIT", override),
                       ("FEEDLING_GIT_COMMIT", image)]:
        env.pop(key, None)
        if value is not None:
            env[key] = value
    script = '''
import json, os, subprocess, sys
from types import SimpleNamespace
sys.path.insert(0, 'backend')
git_value = sys.argv[1]
subprocess.run = lambda *a, **k: SimpleNamespace(returncode=0, stdout=os.getcwd() if '--show-toplevel' in a[0] else git_value)
import tools.chat_resident_consumer as c
before = c.RUNNING_COMMIT
os.environ['FEEDLING_GIT_COMMIT'] = 'changed-after-start'
c._consumer_commit = lambda: 'changed-checkout'
print(json.dumps([before, c.RUNNING_COMMIT, c._HEADERS['X-Feedling-Consumer-Commit'], c._HEADERS['X-Feedling-Consumer-Commit-Source']]))
'''
    result = subprocess.run([sys.executable, "-c", script, git_value], cwd=ROOT,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    source = ('consumer_env' if override is not None else 'image_env' if image is not None else 'git_checkout') if expected else 'unknown'
    assert json.loads(result.stdout.splitlines()[-1]) == [expected] * 3 + [source]


@pytest.mark.parametrize("build,expected", [(BUILD, BUILD), (None, None),
                                         ("", None), ("dev", None), ("unknown", None)])
def test_main_wires_frozen_build_to_heartbeat(monkeypatch, build, expected):
    from agent_runtime import supervisor as s

    if build is None:
        monkeypatch.delenv("FEEDLING_GIT_COMMIT", raising=False)
    else:
        monkeypatch.setenv("FEEDLING_GIT_COMMIT", build)
    for key in ["FEEDLING_GENESIS_WORKER_ENABLED", "FEEDLING_RUNTIME_TOKEN_SECRET",
                "FEEDLING_HOST_ALL", "AGENT_RUNTIME_AUTODISCOVER"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(s, "_load_roster", lambda: [])
    monkeypatch.setattr(s.spawners, "get_spawner", lambda _: (None, None, None))
    captured = []

    class StopMain(BaseException):
        pass

    class HeartbeatThread:
        def __init__(self, *, target, daemon, kwargs):
            assert target is s._heartbeat_loop
            self.kwargs = kwargs

        def start(self):
            callback = self.kwargs["instance_payload_fn"]
            captured.append(callback(100))
            monkeypatch.setenv("FEEDLING_GIT_COMMIT", "changed-after-start")
            captured.append(callback(101))
            raise StopMain

    monkeypatch.setattr(s.threading, "Thread", HeartbeatThread)
    with pytest.raises(StopMain):
        s.main()
    assert [p["version"] for p in captured] == [expected, expected]
    assert [p["ts"] for p in captured] == [100, 101]
    assert [p["version_source"] for p in captured] == ["image_env" if expected else "unknown"] * 2


def test_archive_does_not_borrow_parent_repository(monkeypatch):
    import tempfile
    monkeypatch.setenv('FEEDLING_API_URL', 'http://localhost:5001')
    monkeypatch.setenv('FEEDLING_API_KEY', 'test_key_00000000')
    import tools.chat_resident_consumer as c
    with tempfile.TemporaryDirectory(dir=ROOT) as directory:
        monkeypatch.setattr(c, '__file__', str(Path(directory) / 'tools/chat_resident_consumer.py'))
        assert c._consumer_commit() == ''


def test_container_environment_does_not_borrow_supervisor_identity(monkeypatch):
    from agent_runtime import spawners
    monkeypatch.setenv('FEEDLING_GIT_COMMIT', BUILD)
    monkeypatch.setenv('FEEDLING_CONSUMER_COMMIT', 'supervisor-override')
    content = spawners.container_env_file_content({'driver': 'claude'}, user_id='usr_version_test')
    assert 'FEEDLING_GIT_COMMIT=' not in content
    assert 'FEEDLING_CONSUMER_COMMIT=' not in content


def test_process_spawner_passes_its_own_image_declaration(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from agent_runtime import spawners
    monkeypatch.setenv('FEEDLING_GIT_COMMIT', BUILD)
    monkeypatch.delenv('FEEDLING_CONSUMER_COMMIT', raising=False)
    monkeypatch.setattr(spawners, 'materialize_home', lambda *a, **k: None)
    monkeypatch.setattr(spawners, '_genesis_persona_content', lambda *a, **k: '')
    monkeypatch.setattr(spawners, '_web_visible_for_user', lambda *a: False)
    captured = {}
    def launch(argv, *, env):
        captured.update(argv=argv, env=env)
        return SimpleNamespace(pid=54321)
    monkeypatch.setattr(spawners.subprocess, 'Popen', launch)
    assert spawners.ProcessSpawner().spawn({'driver': 'claude'}, 'usr_version_test', str(tmp_path)) == 54321
    assert captured['env']['FEEDLING_GIT_COMMIT'] == BUILD
    assert captured['argv'][-1].endswith('tools/chat_resident_consumer.py')
