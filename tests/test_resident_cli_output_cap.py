"""T625: bounded capture exercises real pipes and both former call branches."""
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest

os.environ.setdefault('FEEDLING_API_URL', 'http://localhost:5001')
os.environ.setdefault('FEEDLING_API_KEY', 'test_key_00000000')
os.environ.setdefault('AGENT_MODE', 'http')
os.environ.setdefault('AGENT_HTTP_URL', 'http://localhost:8080/chat')
os.environ.setdefault('CHECKPOINT_FILE', '/tmp/t625-checkpoint.json')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from tools import chat_resident_consumer as c
from notices import error_contract


def run(script, callback=None, **kwargs):
    return c._run_cli_subprocess(
        [sys.executable, '-c', script],
        {'capture_output': True, 'text': True, 'encoding': 'utf-8',
         'errors': 'replace', 'timeout': 3, **kwargs}, stdout_line=callback,
    )


@pytest.mark.parametrize('callback', [False, True])
@pytest.mark.parametrize('stream', ['stdout', 'stderr', 'combined'])
def test_real_cli_flood_is_bounded(monkeypatch, callback, stream):
    monkeypatch.setenv('FEEDLING_CLI_MAX_OUTPUT_BYTES', '8192')
    # No newline: a line-based limit would read the whole record first.
    script = 'import os,time\n'
    if stream == 'combined':
        script += 'os.write(1,b"a"*4096); os.write(2,b"b"*4097)\n'
    else:
        script += f'os.write({1 if stream == "stdout" else 2},b"a"*16384)\n'
    script += 'time.sleep(30)'
    with pytest.raises(c.CliOutputTooLarge) as caught:
        run(script, (lambda line: None) if callback else None)
    exc = caught.value
    assert exc.limit_bytes == 8192 and exc.observed_bytes > 8192
    notice = c.classify_agent_error(exc)
    assert notice.error_class == 'cli_output_too_large' and notice.blame == 'system'
    assert notice.user_text == c._notice_for_code('unknown', '').user_text
    assert not hasattr(exc, 'stdout') and not hasattr(exc, 'stderr')
    # Observe the actual capture buffers retained by the traceback, not a mock.
    tb = exc.__traceback__
    while tb and tb.tb_frame.f_code.co_name != '_run_cli_subprocess':
        tb = tb.tb_next
    assert tb is not None
    assert all(len(buffer) == 0 for buffer in tb.tb_frame.f_locals['buffers'])
    assert tb.tb_frame.f_locals['process'].poll() is not None


@pytest.mark.parametrize('callback', [False, True])
@pytest.mark.parametrize('size', [8191, 8192])
def test_at_or_below_limit_returns_complete_output(monkeypatch, callback, size):
    monkeypatch.setenv('FEEDLING_CLI_MAX_OUTPUT_BYTES', '8192')
    seen = []
    result = run(f'import os; os.write(1,b"x"*{size-1}); os.write(2,b"y")', seen.append if callback else None)
    assert result.returncode == 0
    assert result.stdout == 'x' * (size-1) and result.stderr == 'y'
    assert seen == (['x' * (size-1)] if callback else [])


def test_capture_counts_wire_bytes_not_unicode_characters(monkeypatch):
    monkeypatch.setenv('FEEDLING_CLI_MAX_OUTPUT_BYTES', '5')
    with pytest.raises(c.CliOutputTooLarge):
        run('import os; os.write(1,"猫猫".encode())')
    monkeypatch.setenv('FEEDLING_CLI_MAX_OUTPUT_BYTES', '6')
    assert run('import os; os.write(1,"猫猫".encode())').stdout == '猫猫'


@pytest.mark.parametrize('value,expected', [(None, 67108864), ('123', 123), ('0', 67108864), ('-1', 67108864), ('no', 67108864)])
def test_cap_environment_override(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv('FEEDLING_CLI_MAX_OUTPUT_BYTES', raising=False)
    else:
        monkeypatch.setenv('FEEDLING_CLI_MAX_OUTPUT_BYTES', value)
    assert c._cli_max_output_bytes() == expected


def test_streaming_preserves_utf8_newlines_and_stdin(monkeypatch):
    monkeypatch.setenv('FEEDLING_CLI_MAX_OUTPUT_BYTES', '100')
    seen = []
    result = run('import sys,os; assert sys.stdin.read()=="猫"; os.write(1,b"one\\r\\n"); os.write(1,"猫\\rtail".encode())', seen.append, input='猫')
    assert result.stdout == 'one\n猫\ntail'
    assert seen == ['one\n', '猫\n', 'tail']


def test_fake_flood_kills_and_discards(monkeypatch):
    class Process:
        stdin = None
        stdout = io.BytesIO(b'a' * 65)
        stderr = io.BytesIO(b'b' * 65)
        killed = 0

        def kill(self):
            self.killed += 1

        def wait(self, timeout=None):
            return -9 if self.killed else 0

    process = Process()
    monkeypatch.setenv('FEEDLING_CLI_MAX_OUTPUT_BYTES', '64')
    monkeypatch.setattr(c.subprocess, 'Popen', lambda *a, **kw: process)
    with pytest.raises(c.CliOutputTooLarge):
        run('unused')
    assert process.killed >= 1
    assert process.stdout.closed and process.stderr.closed


def test_cli_limit_registered_with_existing_generic_copy():
    notice = c.classify_agent_error(c.CliOutputTooLarge(64, 65))
    assert notice.error_class in error_contract.registry_export().values
    assert notice.user_text == c._notice_for_code('unknown', '').user_text


def test_timeout_still_interrupts_a_child_that_does_not_read_stdin():
    with pytest.raises(subprocess.TimeoutExpired):
        run('import time; time.sleep(30)', input='x' * 200000, timeout=0.2)


# T746: a turn killed at the output cap never finished, so its native session must
# not be resumed (prod usr_1baf… replayed the same oversized HTML turn every message).
@pytest.fixture
def stored_session(monkeypatch, tmp_path):
    monkeypatch.setattr(c, 'AGENT_SESSION_FILE_TEMPLATE', str(tmp_path / 'session_{user_id}.txt'))
    monkeypatch.setitem(c._whoami_cache, 'user_id', 'usr_t746')
    c._agent_session_id_cache.pop('usr_t746', None)
    c._agent_session_meta_cache.pop('usr_t746', None)
    c._save_agent_session_id('sid-before')
    traces = []
    monkeypatch.setattr(c, '_emit_debug_trace', lambda *a, **k: traces.append((a, k)))
    assert c._load_agent_session_meta(check_bounds=False)['session_id'] == 'sid-before'
    return traces


def _raise(exc):
    def impl(*_a, **_k):
        raise exc
    return impl


def _rotations(traces):
    return [k.get('detail', {}).get('trigger_reason') for a, k in traces
            if len(a) > 1 and a[1] == 'agent.session.rotated']


def test_output_limit_clears_the_resumable_session(monkeypatch, stored_session):
    monkeypatch.setattr(c, '_call_agent_cli_impl', _raise(c.CliOutputTooLarge(8192, 9000)))
    with pytest.raises(c.CliOutputTooLarge):
        c.call_agent_cli('make an html page', lane='chat', trace_id='t746')
    assert c._load_agent_session_meta(check_bounds=False)['session_id'] == ''
    assert not c._agent_session_file_for_user().exists()
    assert _rotations(stored_session) == ['cli_output_too_large']


def test_output_limit_in_an_isolated_call_keeps_the_main_session(monkeypatch, stored_session):
    monkeypatch.setattr(c, '_call_agent_cli_impl', _raise(c.CliOutputTooLarge(8192, 9000)))
    with pytest.raises(c.CliOutputTooLarge):
        c.call_agent_cli('x', lane='background', isolated_session=True)
    assert c._load_agent_session_meta(check_bounds=False)['session_id'] == 'sid-before'
    assert _rotations(stored_session) == []


def test_other_failures_keep_the_session(monkeypatch, stored_session):
    monkeypatch.setattr(c, '_call_agent_cli_impl', _raise(RuntimeError('provider 500')))
    with pytest.raises(RuntimeError):
        c.call_agent_cli('x', lane='chat')
    assert c._load_agent_session_meta(check_bounds=False)['session_id'] == 'sid-before'
    assert _rotations(stored_session) == []
