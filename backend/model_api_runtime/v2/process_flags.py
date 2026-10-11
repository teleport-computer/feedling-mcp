"""Allowlisted, best-effort process samples for the fixed Test worker inventory.

Reads run inside the existing bounded memory diagnostic thread. This module never
imports application wiring or changes process environment, admission or jobs.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import socket
import time

TEST_CVM_ID = '5bfa1543-c5b4-42ca-842d-fd88984e5edf'
_KEYS = {b'FEEDLING_V2_CAPTURE_ENABLED', b'FEEDLING_V2_PLAINTEXT_RECALL', b'FEEDLING_GIT_COMMIT'}


def enabled() -> bool:
    return os.environ.get('FEEDLING_V2_RUNNER_CVM_ID') == TEST_CVM_ID


def _read(path: Path, limit: int) -> bytes:
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('oversized_proc_field')
    return data


def _identity(proc: Path) -> dict:
    raw = _read(proc / 'stat', 8192).decode('ascii')
    tail = raw[raw.rfind(')') + 2:].split()
    return {'pid':int(raw.split(' ', 1)[0]), 'ppid':int(tail[1]), 'start_ticks':int(tail[19])}


def sample(pid: int | None, parent_pid: int, expected_sha: str, *,
           proc_root: Path = Path('/proc')) -> dict:
    """Sample a known owned parent or supervisor-reported child, never inventory."""
    try:
        if type(pid) is not int or pid < 1 or not re.fullmatch('[0-9a-f]{40}', expected_sha):
            raise ValueError('invalid_selection')
        proc = proc_root / str(pid)
        before = _identity(proc)
        if before['pid'] != pid or (pid != parent_pid and before['ppid'] != parent_pid):
            raise ValueError('wrong_parent')
        command = _read(proc / 'cmdline', 8192).split(b'\0')
        if pid == parent_pid:
            recognized = any(v in (b'backend/model_api_runtime/v2/serve_worker.py', b'backend.model_api_runtime.v2.serve_worker') or v.endswith(b'/backend/model_api_runtime/v2/serve_worker.py') for v in command)
        else:
            recognized = any(b'multiprocessing.spawn' in v for v in command)
        if not recognized:
            raise ValueError('wrong_process')
        values = {}
        for row in _read(proc / 'environ', 131072).split(b'\0'):
            key, sep, value = row.partition(b'=')
            if sep and key in _KEYS:
                if key in values:
                    raise ValueError('duplicate_allowed_key')
                values[key] = value
        if values.get(b'FEEDLING_GIT_COMMIT') != expected_sha.encode('ascii'):
            raise ValueError('release_mismatch')
        capture = values.get(b'FEEDLING_V2_CAPTURE_ENABLED', b'').strip().lower()
        recall = values.get(b'FEEDLING_V2_PLAINTEXT_RECALL', b'').strip().lower()
        capture_value = (True if capture in (b'1', b'true', b'yes', b'on') else
                         False if capture in (b'0', b'false', b'no', b'off') else None)
        recall_value = recall.decode('ascii') if recall in (b'on', b'off', b'shadow') else 'unknown'
        if before != _identity(proc) or command != _read(proc / 'cmdline', 8192).split(b'\0'):
            raise ValueError('process_replaced')
        return {'status':'observed', 'observed_at_epoch':time.time(), **before,
                'capture_enabled':capture_value, 'plaintext_recall':recall_value,
                'full_sha':expected_sha}
    except Exception as exc:  # diagnostics cannot interrupt normal heartbeat
        return {'status':'unavailable', 'error_type':type(exc).__name__}


def context(capture_runtime_enabled: bool) -> dict:
    """No claim that namespace hostname is a Docker container ID."""
    hostname = socket.gethostname()
    return {'schema':1, 'environment':'test', 'cvm_id':TEST_CVM_ID,
            'source':'owned_process_initial_environment',
            'container_namespace_hostname':hostname if re.fullmatch('[A-Za-z0-9_.-]{1,128}', hostname) else 'unknown',
            'parent_capture_runtime_enabled':capture_runtime_enabled if type(capture_runtime_enabled) is bool else None}
