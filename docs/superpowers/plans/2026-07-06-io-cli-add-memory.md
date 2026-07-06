# io_cli add-memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the VPS resident agent an `io_cli add-memory` verb that distills a file/text into memory (or identity) by reusing the existing genesis plaintext pipeline.

**Architecture:** Pure client wiring in `tools/io_cli.py` — a payload builder + a poll helper + a command handler. Zero backend change: POST the same body iOS sends to `/v1/genesis/imports/plaintext`, then poll `GET /v1/genesis/imports/{job_id}`. Confirmation/classification behavior lives in the VPS skill doc (owner: hx), NOT in this code — the verb stays dumb.

**Tech Stack:** Python stdlib only (urllib/argparse), pytest. Same conventions as the rest of `io_cli.py`.

## Global Constraints

- **Isolation:** Work on a NEW branch off `origin/test` (e.g. `feat/io-cli-add-memory`), NOT on `feat/genesis-onboarding-reliability`. This change is purely additive to `tools/io_cli.py` + one new test file; it must not touch any `backend/genesis/**` file.
- **Stdlib only** — no new dependencies. Reuse existing helpers: `_emit`, `_env`, `_auth_headers`, `_require_backend`, `_http_json`.
- **Output:** JSON on stdout via `_emit(obj, code)`. `_emit` calls `sys.exit(code)`.
- **Exact payload field names** (verified against iOS `uploadGenesisPlaintext` + `backend/genesis/plaintext.py`):
  - both modes: `format="auto"`, `content=""`, `fresh_start=false`, `client_job_id`, `mode`.
  - memory → `mode="add_memory"`, `memory_summary_content`, `memory_summary_filename`.
  - identity → `mode="update_identity"`, `ai_persona_content` + `character_content`, `ai_persona_filename` + `character_filename`.
- **Job status values** (from `backend/genesis/service.py`): `"done"` / `"failed"` / `"processing"`. GET response shape: `{"job": {...}, "state": {...}}`; memory count = `job["memory_action_count"]`.
- TDD, frequent commits, DRY, YAGNI.

---

### Task 1: `_add_memory_payload` builder (pure)

**Files:**
- Modify: `tools/io_cli.py` (add function near the other payload builders, e.g. after `_identity_write_payload`)
- Test: `tests/test_io_cli_add_memory.py` (new)

**Interfaces:**
- Produces: `_add_memory_payload(text: str, filename: str, as_kind: str, client_job_id: str) -> dict`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_io_cli_add_memory.py`:

```python
"""io_cli add-memory: payload builder + poll helper (pure, no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402


def test_add_memory_payload_memory_mode():
    p = io_cli._add_memory_payload("I drink oat milk.", "diet.md", "memory", "vps-add-memory-1")
    assert p["mode"] == "add_memory"
    assert p["format"] == "auto"
    assert p["content"] == ""
    assert p["fresh_start"] is False
    assert p["client_job_id"] == "vps-add-memory-1"
    assert p["memory_summary_content"] == "I drink oat milk."
    assert p["memory_summary_filename"] == "diet.md"
    # identity-only keys must be absent in memory mode
    assert "ai_persona_content" not in p
    assert "character_content" not in p


def test_add_memory_payload_identity_mode():
    p = io_cli._add_memory_payload("Be blunter, use lowercase.", "persona.md", "identity", "vps-update-identity-1")
    assert p["mode"] == "update_identity"
    assert p["ai_persona_content"] == "Be blunter, use lowercase."
    assert p["character_content"] == "Be blunter, use lowercase."
    assert p["ai_persona_filename"] == "persona.md"
    assert p["character_filename"] == "persona.md"
    # memory-only key must be absent in identity mode
    assert "memory_summary_content" not in p


def test_add_memory_payload_no_filename_omits_filename_keys():
    p = io_cli._add_memory_payload("some text", "", "memory", "vps-add-memory-2")
    assert "memory_summary_filename" not in p
    assert p["memory_summary_content"] == "some text"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_io_cli_add_memory.py -v`
Expected: FAIL with `AttributeError: module 'io_cli' has no attribute '_add_memory_payload'`

- [ ] **Step 3: Implement the builder**

In `tools/io_cli.py`, add after `_identity_write_payload`:

```python
def _add_memory_payload(text, filename, as_kind, client_job_id):
    """Shape the plaintext-genesis body for a VPS-side re-distill (pure).

    Mirrors iOS uploadGenesisPlaintext + backend/genesis/plaintext.py field names:
    memory  -> mode=add_memory,      memory_summary_content
    identity -> mode=update_identity, ai_persona_content + character_content
    """
    payload = {
        "format": "auto",
        "content": "",
        "fresh_start": False,
        "client_job_id": client_job_id,
    }
    name = (filename or "").strip()
    if as_kind == "identity":
        payload["mode"] = "update_identity"
        payload["ai_persona_content"] = text
        payload["character_content"] = text
        if name:
            payload["ai_persona_filename"] = name
            payload["character_filename"] = name
    else:  # memory (default)
        payload["mode"] = "add_memory"
        payload["memory_summary_content"] = text
        if name:
            payload["memory_summary_filename"] = name
    return payload
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_io_cli_add_memory.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add tools/io_cli.py tests/test_io_cli_add_memory.py
git commit -m "feat(io_cli): add-memory payload builder (memory/identity plaintext-genesis body)"
```

---

### Task 2: `_poll_genesis_job` helper

**Files:**
- Modify: `tools/io_cli.py` (add function after `_add_memory_payload`)
- Test: `tests/test_io_cli_add_memory.py` (append)

**Interfaces:**
- Consumes: `io_cli._http_json` (monkeypatched in tests)
- Produces: `_poll_genesis_job(api_url: str, auth: dict, job_id: str, *, timeout: float, interval: float = 2.0) -> dict`
  - done → `{"ok": True, "status": "done", "job_id", "memories_created": int}`
  - failed → `{"ok": False, "status": "failed", "job_id", "error"}`
  - still running past timeout → `{"ok": True, "status": "pending", "job_id"}`
  - HTTP error → `{"ok": False, "status": "error", "job_id", "http_status", "error"}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_io_cli_add_memory.py`:

```python
def test_poll_returns_done_with_memories_created(monkeypatch):
    def fake_http(method, url, auth, **kw):
        assert method == "GET"
        assert url.endswith("/v1/genesis/imports/job-abc")
        return 200, {"job": {"status": "done", "memory_action_count": 5}, "state": {}}
    monkeypatch.setattr(io_cli, "_http_json", fake_http)
    out = io_cli._poll_genesis_job("http://x", {"X-API-Key": "k"}, "job-abc", timeout=1.0, interval=0.0)
    assert out == {"ok": True, "status": "done", "job_id": "job-abc", "memories_created": 5}


def test_poll_returns_failed(monkeypatch):
    def fake_http(method, url, auth, **kw):
        return 200, {"job": {"status": "failed", "error": "add_memory_failed:boom"}, "state": {}}
    monkeypatch.setattr(io_cli, "_http_json", fake_http)
    out = io_cli._poll_genesis_job("http://x", {}, "job-f", timeout=1.0, interval=0.0)
    assert out["ok"] is False
    assert out["status"] == "failed"
    assert "boom" in out["error"]


def test_poll_timeout_returns_pending(monkeypatch):
    def fake_http(method, url, auth, **kw):
        return 200, {"job": {"status": "processing"}, "state": {}}
    monkeypatch.setattr(io_cli, "_http_json", fake_http)
    # timeout=0 -> first deadline check trips immediately, no sleep
    out = io_cli._poll_genesis_job("http://x", {}, "job-p", timeout=0.0, interval=0.0)
    assert out == {"ok": True, "status": "pending", "job_id": "job-p"}


def test_poll_http_error(monkeypatch):
    def fake_http(method, url, auth, **kw):
        return 500, {"error": "boom"}
    monkeypatch.setattr(io_cli, "_http_json", fake_http)
    out = io_cli._poll_genesis_job("http://x", {}, "job-e", timeout=1.0, interval=0.0)
    assert out["ok"] is False
    assert out["status"] == "error"
    assert out["http_status"] == 500
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_io_cli_add_memory.py -k poll -v`
Expected: FAIL with `AttributeError: ... has no attribute '_poll_genesis_job'`

- [ ] **Step 3: Implement the poll helper**

In `tools/io_cli.py`, add after `_add_memory_payload`:

```python
def _poll_genesis_job(api_url, auth, job_id, *, timeout, interval=2.0):
    """Poll GET /v1/genesis/imports/{job_id} until done/failed/timeout.

    Returns a plain dict for the caller to _emit(); pure w.r.t. _http_json so it
    is unit-testable by monkeypatching that seam.
    """
    import time as _time
    deadline = _time.monotonic() + max(0.0, timeout)
    while True:
        code, body = _http_json("GET", f"{api_url}/v1/genesis/imports/{job_id}", auth)
        if code >= 400:
            return {"ok": False, "status": "error", "job_id": job_id,
                    "http_status": code, "error": body}
        job = body.get("job") if isinstance(body, dict) else {}
        job = job if isinstance(job, dict) else {}
        state = body.get("state") if isinstance(body, dict) else {}
        state = state if isinstance(state, dict) else {}
        status = str(job.get("status") or state.get("status") or "").strip().lower()
        if status == "done":
            return {"ok": True, "status": "done", "job_id": job_id,
                    "memories_created": int(job.get("memory_action_count") or 0)}
        if status == "failed":
            return {"ok": False, "status": "failed", "job_id": job_id,
                    "error": job.get("error") or state.get("error") or "genesis job failed"}
        if _time.monotonic() >= deadline:
            return {"ok": True, "status": "pending", "job_id": job_id}
        _time.sleep(interval)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_io_cli_add_memory.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add tools/io_cli.py tests/test_io_cli_add_memory.py
git commit -m "feat(io_cli): genesis job poll helper (done/failed/pending/error)"
```

---

### Task 3: `add-memory` command handler + subparser wiring

**Files:**
- Modify: `tools/io_cli.py` — add `import uuid`; add `_read_add_memory_input`, `cmd_add_memory`; register subparser in `main()`
- Test: `tests/test_io_cli_add_memory.py` (append subprocess smoke test)
- Test: `tests/test_io_cli_parser.py` (add `"add-memory"` to `REAL_SUBCOMMANDS`)

**Interfaces:**
- Consumes: `_require_backend`, `_add_memory_payload`, `_poll_genesis_job`, `_http_json`, `_emit`
- Produces: CLI verb `add-memory` → `{"ok", "status", "job_id", "memories_created"?, "as"}`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_io_cli_add_memory.py`:

```python
import json as _json
import subprocess

_TOOLS = Path(__file__).parent.parent / "tools"
_IO_CLI = str(_TOOLS / "io_cli.py")


def test_add_memory_missing_env_clean_error():
    r = subprocess.run(
        [sys.executable, _IO_CLI, "add-memory", "--text", "hi"],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"},
    )
    assert "conflicting subparser" not in r.stderr
    assert "Traceback" not in r.stderr
    payload = _json.loads(r.stdout.strip().splitlines()[-1])
    assert payload.get("ok") is False  # missing FEEDLING_API_URL/auth -> clean JSON error
```

Add `"add-memory"` to the `REAL_SUBCOMMANDS` set in `tests/test_io_cli_parser.py`:

```python
REAL_SUBCOMMANDS = {
    "schedule-wake",
    "cancel-wake",
    "photo-read",
    "photo-recent",
    "identity-write",
    "add-memory",
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_io_cli_add_memory.py::test_add_memory_missing_env_clean_error -v`
Expected: FAIL — argparse errors on unknown subcommand `add-memory` (non-zero exit, no JSON `ok:false` line).

- [ ] **Step 3: Implement handler + input reader + wiring**

In `tools/io_cli.py`, add `import uuid` to the import block (alongside `import base64`). Then add after `_poll_genesis_job`:

```python
def _read_add_memory_input(args):
    """Resolve the text to distill: --file (wins) -> --text -> stdin. Empty if none."""
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            _emit({"ok": False, "error": f"could not read --file: {e}"}, 2)
    if args.text:
        return args.text
    if not sys.stdin.isatty():
        return sys.stdin.read()
    return ""


def cmd_add_memory(args):
    """Distill a file/text into memory (default) or identity via the genesis
    plaintext pipeline (POST /v1/genesis/imports/plaintext), then poll to done."""
    api_url, auth = _require_backend()
    text = _read_add_memory_input(args)
    if not text.strip():
        _emit({"ok": False, "error": "empty_input: need --file/--text with content or piped stdin"}, 2)
    as_kind = "identity" if args.as_kind == "identity" else "memory"
    filename = os.path.basename(args.file) if args.file else ""
    prefix = "vps-update-identity" if as_kind == "identity" else "vps-add-memory"
    client_job_id = f"{prefix}-{uuid.uuid4()}"
    payload = _add_memory_payload(text, filename, as_kind, client_job_id)
    code, body = _http_json("POST", f"{api_url}/v1/genesis/imports/plaintext", auth, payload=payload)
    if code >= 400:
        _emit({"ok": False, "status": "error", "http_status": code, "error": body}, 1)
    job = body.get("job") if isinstance(body, dict) else {}
    job_id = (job.get("job_id") if isinstance(job, dict) else None) or (
        body.get("job_id") if isinstance(body, dict) else None)
    if not job_id:
        _emit({"ok": False, "status": "error", "error": "no job_id in response", "body": body}, 1)
    if args.no_wait:
        _emit({"ok": True, "status": "submitted", "job_id": job_id, "as": as_kind})
    result = _poll_genesis_job(api_url, auth, job_id, timeout=args.timeout)
    result["as"] = as_kind
    _emit(result, 0 if result.get("ok") else 1)
```

In `main()`, register the subparser (place near `identity-write`'s registration):

```python
    am = sub.add_parser("add-memory",
                        help="Distill a file/text into memory (default) or identity via genesis.")
    am.add_argument("--file", default="", help="path to a file to distill")
    am.add_argument("--text", default="", help="inline text to distill (or pipe via stdin)")
    am.add_argument("--as", dest="as_kind", choices=["memory", "identity"], default="memory",
                    help="memory (default) or identity")
    am.add_argument("--no-wait", dest="no_wait", action="store_true",
                    help="submit and return job_id without polling")
    am.add_argument("--timeout", type=float, default=120.0, help="poll timeout seconds (default 120)")
    am.set_defaults(func=cmd_add_memory)
```

- [ ] **Step 4: Run the full suite for these files**

Run: `python -m pytest tests/test_io_cli_add_memory.py tests/test_io_cli_parser.py -v`
Expected: all passed (8 in add_memory file + parser tests)

- [ ] **Step 5: Commit**

```bash
git add tools/io_cli.py tests/test_io_cli_add_memory.py tests/test_io_cli_parser.py
git commit -m "feat(io_cli): add-memory verb — VPS-side file->memory/identity re-distill"
```

---

## Notes for the implementer

- **Do NOT** add confirmation/classification logic to the verb. `--as` is the agent's explicit choice; the "identity needs pre-confirm, memory reports-after" behavior is instructed in the VPS skill doc (owner: hx), out of scope here.
- **Do NOT** touch any `backend/**` file — the pipeline is reused unchanged.
- The verb inherits genesis chunking/tier-windowing and dedup server-side; it sends the whole file text and lets the pipeline window it. No client-side splitting.
