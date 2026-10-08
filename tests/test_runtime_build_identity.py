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
subprocess.run = lambda *a, **k: SimpleNamespace(returncode=0, stdout=git_value)
import tools.chat_resident_consumer as c
before = c.RUNNING_COMMIT
os.environ['FEEDLING_GIT_COMMIT'] = 'changed-after-start'
c._consumer_commit = lambda: 'changed-checkout'
print(json.dumps([before, c.RUNNING_COMMIT, c._HEADERS['X-Feedling-Consumer-Commit']]))
'''
    result = subprocess.run([sys.executable, "-c", script, git_value], cwd=ROOT,
                            env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == [expected] * 3


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
